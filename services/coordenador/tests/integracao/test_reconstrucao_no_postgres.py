"""CLI de reconstrução contra PostgreSQL real (005-T3; INFRA 9.1).

PostgreSQL descartável do consolidado (consolidado_descartavel.py), com as
migrações aplicadas por consolidado_migracao. A CLI roda pela mesma função
do contêiner (`coordenador.cli.main`), com as variáveis de ambiente do
compose apontando para o banco descartável.

- `iniciar`, `status` e `cancelar` como consolidado_operacao, com
  checkpoint e lacunas da candidata projetados pelo caminho real do worker
  (`ProjetarLancamento` como consolidado_worker);
- execução com outra identidade falha sem mudar nada (FR-008), tanto pela
  verificação do adaptador quanto pelos privilégios do banco;
- dois `iniciar` concorrentes: só um cria a candidata (INFRA 7.5);
- a fase gravada pelo coordenador chega à métrica da API do consolidado
  lida como consolidado_api (migração 0003; 007 design §4).

Cada teste termina sem geração `construindo`, para o seguinte poder
iniciar. Só o primeiro projeta lançamentos. A partir da raiz:

    uv run pytest services/coordenador

Sem docker no PATH, o módulo é pulado com o motivo explícito.
"""

import asyncio
import json
import logging
import shutil
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import date
from typing import Any

import psycopg
import pytest
from consolidado.adaptadores.http.fase import atualizar_fase
from consolidado.adaptadores.http.principal import leitor_de_fase
from consolidado.adaptadores.postgres import UnidadeDeTrabalhoProjecaoPostgres
from consolidado.aplicacao import Desfecho, MensagemEvento, ProjetarLancamento
from consolidado.worker.principal import fabrica_de_conexao
from consolidado_descartavel import PAPEIS_DO_CONSOLIDADO, Consolidado
from consolidado_descartavel import consolidado_descartavel as descartavel
from coordenador.adaptadores.postgres import abridor_de_transacao
from coordenador.aplicacao import ConsultarSituacao, IniciarReconstrucao
from coordenador.cli import main
from observabilidade import Metricas
from psycopg_pool import AsyncConnectionPool

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker ausente no PATH"
)

DATA = date(2026, 11, 6)
OUTRAS_IDENTIDADES = ["consolidado_api", "consolidado_worker", "consolidado_migracao"]


@pytest.fixture(scope="module")
def banco() -> Iterator[Consolidado]:
    with descartavel("coordenador-reconstrucao") as consolidado:
        yield consolidado


@pytest.fixture(autouse=True)
def logging_restaurado() -> Iterator[None]:
    # `main` troca os handlers da raiz pelo JSON na saída de erro.
    raiz = logging.getLogger()
    handlers, nivel = raiz.handlers[:], raiz.level
    yield
    raiz.handlers[:] = handlers
    raiz.setLevel(nivel)


def _ambiente(
    banco: Consolidado, papel: str = "consolidado_operacao"
) -> dict[str, str]:
    """Mesmas variáveis do serviço `coordenador` do compose."""
    return {
        "PG_CONSOLIDADO_HOST": "127.0.0.1",
        "PG_CONSOLIDADO_PORTA": str(banco.porta),
        "PG_CONSOLIDADO_USUARIO_OPERACAO": papel,
        "PG_CONSOLIDADO_SENHA_OPERACAO": banco.senhas[papel],
    }


def _cli(
    banco: Consolidado,
    capsys: pytest.CaptureFixture[str],
    *argumentos: str,
    papel: str = "consolidado_operacao",
) -> tuple[int, str, str]:
    codigo = main(list(argumentos), ambiente=_ambiente(banco, papel))
    saida = capsys.readouterr()
    return codigo, saida.out, saida.err


def _status(banco: Consolidado, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    codigo, saida, erro = _cli(banco, capsys, "reconstruir", "status", "--json")
    assert codigo == 0, erro
    documento: dict[str, Any] = json.loads(saida)
    return documento


def _geracoes(banco: Consolidado) -> list[tuple[int, str]]:
    with psycopg.connect(banco.conninfo("consolidado_operacao")) as conexao:
        return conexao.execute("SELECT id, estado FROM geracao ORDER BY id").fetchall()


def _um(banco: Consolidado, consulta: str, *params: object) -> Any:
    with psycopg.connect(banco.conninfo("consolidado_operacao")) as conexao:
        linha = conexao.execute(consulta, params).fetchone()
    assert linha is not None
    return linha[0]


def _mensagem(posicao: int) -> MensagemEvento:
    evento: dict[str, object] = {
        "evento_id": str(uuid.uuid4()),
        "versao": 1,
        "lancamento_id": str(uuid.uuid4()),
        "posicao": posicao,
        "tipo": "CREDITO",
        "valor": "1.00",
        "data_lancamento": DATA.isoformat(),
        "registrado_em": "2026-11-06T12:00:00+00:00",
    }
    return MensagemEvento(
        corpo=json.dumps(evento).encode(),
        dados=evento,
        identificador=str(evento["evento_id"]),
    )


def _projetar(banco: Consolidado, *posicoes: int) -> list[Desfecho]:
    """Projeta como o worker: em todas as gerações receptoras (005-T2)."""

    async def projetar() -> list[Desfecho]:
        pool: AsyncConnectionPool[psycopg.AsyncConnection[Any]] = AsyncConnectionPool(
            banco.conninfo("consolidado_worker"),
            min_size=1,
            max_size=1,
            kwargs={"autocommit": True},
            open=False,
        )
        await pool.open(wait=True)
        try:
            caso = ProjetarLancamento(
                UnidadeDeTrabalhoProjecaoPostgres(fabrica_de_conexao(pool))
            )
            return [await caso.executar(_mensagem(p)) for p in posicoes]
        finally:
            await pool.close()

    return asyncio.run(projetar())


def _codigo_da_metrica_de_fase(banco: Consolidado) -> float:
    """Leitura da API do consolidado, como consolidado_api (migração 0003)."""
    metricas = Metricas()
    medidor = metricas.medidor("cashflow_reconstrucao_fase")

    async def ler() -> bool:
        pool: AsyncConnectionPool[psycopg.AsyncConnection[Any]] = AsyncConnectionPool(
            banco.conninfo("consolidado_api"),
            min_size=1,
            max_size=1,
            kwargs={"autocommit": True},
            open=False,
        )
        await pool.open(wait=True)
        try:
            return await atualizar_fase(leitor_de_fase(pool, 5), medidor)
        finally:
            await pool.close()

    assert asyncio.run(ler()), "a API não leu fase_reconstrucao"
    for linha in metricas.exposicao().decode().splitlines():
        if linha.startswith("cashflow_reconstrucao_fase "):
            return float(linha.split()[1])
    raise AssertionError("cashflow_reconstrucao_fase ausente da exposição")


def test_ciclo_iniciar_status_cancelar_pela_cli(
    banco: Consolidado, capsys: pytest.CaptureFixture[str]
) -> None:
    antes = _status(banco, capsys)
    assert antes["criterio_de_troca"] is None
    assert [g["estado"] for g in antes["geracoes"]] == ["ativa"]
    assert antes["geracoes"][0]["checkpoint"] == 0
    assert _codigo_da_metrica_de_fase(banco) == 0

    codigo, saida, erro = _cli(banco, capsys, "reconstruir", "iniciar")

    assert codigo == 0, erro
    candidata = _um(banco, "SELECT id FROM geracao WHERE estado = 'construindo'")
    assert f"geração candidata {candidata} criada" in saida
    assert "relay-republicar --desde-posicao 1" in saida
    assert (
        _um(
            banco,
            "SELECT posicao_continua FROM checkpoint WHERE geracao_id = %s",
            candidata,
        )
        == 0
    )
    assert (
        _um(
            banco, "SELECT fase FROM fase_reconstrucao WHERE geracao_id = %s", candidata
        )
        == "construindo"
    )
    registro = json.loads(erro.strip().splitlines()[-1])
    assert registro["mensagem"] == "geração candidata criada"
    assert registro["geracao_id"] == candidata
    assert registro["operador"] == "consolidado_operacao"
    assert _codigo_da_metrica_de_fase(banco) == 1

    # Posição 3 ainda não chegou: lacuna nas duas gerações receptoras.
    assert _projetar(banco, 1, 2, 4) == [Desfecho.APLICADO] * 3

    status = _status(banco, capsys)
    ativa, nova = status["geracoes"]
    assert (nova["id"], nova["estado"], nova["fase"]) == (
        candidata,
        "construindo",
        "construindo",
    )
    assert (nova["checkpoint"], nova["maior_posicao"]) == (2, 4)
    assert (nova["lacunas"], nova["primeiras_lacunas"]) == (1, [3])
    assert (ativa["checkpoint"], ativa["lacunas"]) == (2, 1)
    assert status["criterio_de_troca"] == {
        "atendido": False,
        "pendencias": ["candidata com 1 lacunas"],
    }
    codigo, texto, _ = _cli(banco, capsys, "reconstruir", "status")
    assert codigo == 0
    assert f"geração {candidata}: construindo" in texto
    assert "checkpoint: 2 (maior posição projetada: 4)" in texto
    assert "lacunas: 1 (menores posições: 3)" in texto

    codigo, _, erro = _cli(banco, capsys, "reconstruir", "iniciar")
    assert codigo == 1
    assert "reconstrução recusada: já existe geração construindo" in erro

    codigo, saida, erro = _cli(banco, capsys, "reconstruir", "cancelar")

    assert codigo == 0, erro
    assert f"geração candidata {candidata} cancelada" in saida
    assert (candidata, "descartada") in _geracoes(banco)
    assert (
        _um(
            banco, "SELECT fase FROM fase_reconstrucao WHERE geracao_id = %s", candidata
        )
        == "cancelada"
    )
    for tabela in ("checkpoint", "lacuna", "contribuicao", "saldo_diario"):
        assert (
            _um(
                banco, f"SELECT count(*) FROM {tabela} WHERE geracao_id = %s", candidata
            )
            == 0
        ), tabela
    assert _codigo_da_metrica_de_fase(banco) == 5
    assert _status(banco, capsys)["criterio_de_troca"] is None

    codigo, _, erro = _cli(banco, capsys, "reconstruir", "cancelar")
    assert codigo == 1
    assert "nenhuma reconstrução em andamento" in erro

    # Depois do cancelamento, uma nova reconstrução pode começar.
    codigo, _, erro = _cli(banco, capsys, "reconstruir", "iniciar")
    assert codigo == 0, erro
    codigo, _, erro = _cli(banco, capsys, "reconstruir", "cancelar")
    assert codigo == 0, erro


@pytest.fixture
def candidata(banco: Consolidado, capsys: pytest.CaptureFixture[str]) -> Iterator[int]:
    codigo, _, erro = _cli(banco, capsys, "reconstruir", "iniciar")
    assert codigo == 0, erro
    yield _um(banco, "SELECT id FROM geracao WHERE estado = 'construindo'")
    codigo, _, erro = _cli(banco, capsys, "reconstruir", "cancelar")
    assert codigo == 0, erro


@pytest.mark.parametrize("papel", OUTRAS_IDENTIDADES)
@pytest.mark.parametrize("acao", ["iniciar", "status"])
def test_outra_identidade_nao_inicia_nem_le(
    banco: Consolidado, capsys: pytest.CaptureFixture[str], papel: str, acao: str
) -> None:
    assert papel in PAPEIS_DO_CONSOLIDADO
    geracoes = _geracoes(banco)

    codigo, saida, erro = _cli(banco, capsys, "reconstruir", acao, papel=papel)

    assert codigo == 1
    assert saida == ""
    assert f"identidade {papel} não autorizada" in erro
    assert banco.senhas[papel] not in erro
    assert _geracoes(banco) == geracoes


@pytest.mark.parametrize("papel", OUTRAS_IDENTIDADES)
def test_outra_identidade_nao_cancela(
    banco: Consolidado,
    capsys: pytest.CaptureFixture[str],
    candidata: int,
    papel: str,
) -> None:
    codigo, _, erro = _cli(banco, capsys, "reconstruir", "cancelar", papel=papel)

    assert codigo == 1
    assert f"identidade {papel} não autorizada" in erro
    assert (candidata, "construindo") in _geracoes(banco)


@pytest.mark.parametrize("papel", ["consolidado_api", "consolidado_worker"])
def test_banco_recusa_os_outros_papeis_mesmo_sem_a_verificacao(
    banco: Consolidado, papel: str
) -> None:
    geracoes = _geracoes(banco)
    conninfo = banco.conninfo(papel)

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        IniciarReconstrucao(abridor_de_transacao(conninfo, papel_exigido=None))()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ConsultarSituacao(
            abridor_de_transacao(conninfo, somente_leitura=True, papel_exigido=None)
        )()

    assert _geracoes(banco) == geracoes


def _aguardar_bloqueio(banco: Consolidado) -> None:
    """Espera a CLI parada no bloqueio da exclusão (pg_stat_activity)."""
    limite = time.monotonic() + 10
    with psycopg.connect(banco.conninfo("postgres"), autocommit=True) as conexao:
        while time.monotonic() < limite:
            esperando = conexao.execute(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE application_name = 'coordenador' AND wait_event_type = 'Lock'"
            ).fetchone()
            if esperando == (1,):
                return
            time.sleep(0.05)
    raise AssertionError("a CLI não esperou pela outra reconstrução")


def test_iniciar_concorrente_so_um_cria_a_candidata(
    banco: Consolidado, capsys: pytest.CaptureFixture[str]
) -> None:
    resultado: list[int] = []
    with psycopg.connect(banco.conninfo("consolidado_operacao")) as primeiro:
        linha = primeiro.execute(
            "INSERT INTO geracao (estado) VALUES ('construindo') RETURNING id"
        ).fetchone()
        assert linha is not None
        primeira = linha[0]
        segundo = threading.Thread(
            target=lambda: resultado.append(
                main(["reconstruir", "iniciar"], ambiente=_ambiente(banco))
            )
        )
        segundo.start()
        _aguardar_bloqueio(banco)
        primeiro.commit()
        segundo.join(timeout=10)

    assert resultado == [1]
    assert "reconstrução recusada: já existe geração construindo" in (
        capsys.readouterr().err
    )
    assert [g for g in _geracoes(banco) if g[1] == "construindo"] == [
        (primeira, "construindo")
    ]
    codigo, _, erro = _cli(banco, capsys, "reconstruir", "cancelar")
    assert codigo == 0, erro
