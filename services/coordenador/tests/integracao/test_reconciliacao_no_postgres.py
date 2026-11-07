"""Reconciliação com os dois bancos reais, sem conexão cruzada (005-T5).

Um PostgreSQL descartável do livro e outro do consolidado, com as mesmas
imagens, papéis e migrações do compose (INFRA 9.1). Os lançamentos são
registrados pelo caminho real da API (livro_api) e projetados pelo caminho
real do worker (consolidado_worker), a partir do payload gravado na outbox,
como o relay o publica.

O relatório do livro é gerado pelo comando real, em subprocesso, só com as
variáveis do lado do livro; o coordenador o lê de arquivo, só com as
variáveis do lado do consolidado. Nenhum dos dois recebe endereço ou senha
do outro banco (INFRA 6.7). No compose, o mesmo vale pelas redes: o teste
confere que livro-relatorio só está na rede do livro e o coordenador só na
do consolidado.

- divergência injetada (posição sem projeção e saldo materializado
  alterado) aparece no relatório, com código de saída 3;
- livro e ativa iguais: conciliada, código 0;
- nenhum valor nos logs dos dois comandos (design 005 §6; INFRA 8.2).

A partir da raiz:

    uv run pytest services/coordenador

Sem docker no PATH, o módulo é pulado com o motivo explícito.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from consolidado.adaptadores.postgres import UnidadeDeTrabalhoProjecaoPostgres
from consolidado.adaptadores.rabbitmq.consumidor import decodificar
from consolidado.aplicacao import Desfecho, ProjetarLancamento
from consolidado.worker.principal import fabrica_de_conexao
from consolidado_descartavel import Consolidado, consolidado_descartavel
from coordenador.cli import main
from lancamentos.adaptadores.postgres import (
    FabricaConexao,
    UnidadeDeTrabalhoLivroPostgres,
)
from lancamentos.aplicacao import RegistrarLancamentoCmd
from livro_descartavel import Livro, livro_descartavel
from psycopg_pool import AsyncConnectionPool

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker ausente no PATH"
)

RAIZ = Path(__file__).resolve().parents[4]
SERVICOS = RAIZ / "services"
COMPOSE = RAIZ / "infra/compose.yaml"

# (tipo, valor, data) em ordem de registro: posições 1 a 5. Valores
# incomuns, para a busca nos logs não achar coincidência.
LANCAMENTOS = [
    ("CREDITO", "1234.56", "2026-11-03"),
    ("DEBITO", "789.01", "2026-11-03"),
    ("CREDITO", "4321.09", "2026-11-04"),
    ("CREDITO", "555.37", "2026-11-05"),
    ("DEBITO", "98.76", "2026-11-05"),
]
# A posição 4 fica sem projeção até o teste de conciliação.
SEM_PROJECAO = 4
VALORES = [valor for _, valor, _ in LANCAMENTOS] + [
    "445.55",  # créditos de 2026-11-03 menos débitos
    "456.61",  # saldo de 2026-11-05
    "1789.56",  # créditos de 2026-11-03 com a divergência injetada
]


@pytest.fixture(scope="module")
def livro() -> Iterator[Livro]:
    with livro_descartavel("coordenador-reconciliacao") as banco:
        yield banco


@pytest.fixture(scope="module")
def consolidado() -> Iterator[Consolidado]:
    with consolidado_descartavel("coordenador-reconciliacao") as banco:
        yield banco


@pytest.fixture(autouse=True)
def logging_restaurado() -> Iterator[None]:
    # `main` troca os handlers da raiz pelo JSON na saída de erro.
    raiz = logging.getLogger()
    handlers, nivel = raiz.handlers[:], raiz.level
    yield
    raiz.handlers[:] = handlers
    raiz.setLevel(nivel)


def _fabrica_livro(livro: Livro) -> FabricaConexao:
    @asynccontextmanager
    async def conexao() -> AsyncIterator[psycopg.AsyncConnection[Any]]:
        async with await psycopg.AsyncConnection.connect(
            livro.conninfo("livro_api"), autocommit=True
        ) as aberta:
            yield aberta

    return conexao


def _registrar(livro: Livro) -> list[tuple[int, uuid.UUID, Any]]:
    """Registra pela API do livro e devolve (posição, evento, payload)."""
    unidade = UnidadeDeTrabalhoLivroPostgres(_fabrica_livro(livro))

    async def registrar() -> None:
        for tipo, valor, data in LANCAMENTOS:
            cmd = RegistrarLancamentoCmd.de(
                tipo=tipo, valor=valor, data_lancamento=data
            )
            await unidade.registrar(cmd, f"chave-{uuid.uuid4()}", "cliente-reconcilia")

    asyncio.run(registrar())
    with psycopg.connect(livro.conninfo("livro_migracao")) as conexao:
        linhas = conexao.execute(
            "SELECT posicao, evento_id, payload FROM outbox ORDER BY posicao"
        ).fetchall()
    return [(posicao, evento_id, payload) for posicao, evento_id, payload in linhas]


def _projetar(consolidado: Consolidado, eventos: list[tuple[uuid.UUID, Any]]) -> None:
    """Aplica os eventos como o worker, a partir do corpo publicado pelo relay."""

    async def projetar() -> list[Desfecho]:
        pool: AsyncConnectionPool[psycopg.AsyncConnection[Any]] = AsyncConnectionPool(
            consolidado.conninfo("consolidado_worker"),
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
            return [
                await caso.executar(
                    decodificar(
                        json.dumps(payload, separators=(",", ":")).encode(),
                        str(evento_id),
                    )
                )
                for evento_id, payload in eventos
            ]
        finally:
            await pool.close()

    assert asyncio.run(projetar()) == [Desfecho.APLICADO] * len(eventos)


@pytest.fixture(scope="module")
def eventos(livro: Livro, consolidado: Consolidado) -> list[tuple[int, uuid.UUID, Any]]:
    registrados = _registrar(livro)
    assert [p for p, _, _ in registrados] == [1, 2, 3, 4, 5]
    _projetar(
        consolidado,
        [(e, payload) for p, e, payload in registrados if p != SEM_PROJECAO],
    )
    return registrados


def _ambiente_livro(livro: Livro, papel: str = "livro_replay") -> dict[str, str]:
    """Mesmas variáveis do serviço livro-relatorio; nada do consolidado."""
    return {
        "PG_LIVRO_HOST": "127.0.0.1",
        "PG_LIVRO_PORTA": str(livro.porta),
        "PG_LIVRO_USUARIO_REPLAY": papel,
        "PG_LIVRO_SENHA_REPLAY": livro.senhas[papel],
    }


def _ambiente_consolidado(
    consolidado: Consolidado, papel: str = "consolidado_operacao"
) -> dict[str, str]:
    """Mesmas variáveis do serviço coordenador; nada do livro."""
    return {
        "PG_CONSOLIDADO_HOST": "127.0.0.1",
        "PG_CONSOLIDADO_PORTA": str(consolidado.porta),
        "PG_CONSOLIDADO_USUARIO_OPERACAO": papel,
        "PG_CONSOLIDADO_SENHA_OPERACAO": consolidado.senhas[papel],
    }


def _relatorio_do_livro(
    livro: Livro, *argumentos: str, papel: str = "livro_replay"
) -> subprocess.CompletedProcess[str]:
    """O comando real, como o operador o executa no lado do livro."""
    ambiente = {
        chave: valor
        for chave, valor in os.environ.items()
        if not chave.startswith(("PG_LIVRO_", "PG_CONSOLIDADO_"))
    }
    return subprocess.run(
        [sys.executable, "-m", "lancamentos.comandos.relatorio", *argumentos],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env={**ambiente, "PYTHONPATH": str(SERVICOS), **_ambiente_livro(livro, papel)},
    )


def _gravar_relatorio(livro: Livro, destino: Path) -> tuple[Path, str]:
    feito = _relatorio_do_livro(livro)
    assert feito.returncode == 0, feito.stderr
    destino.write_text(feito.stdout, encoding="utf-8")
    return destino, feito.stderr


def _reconciliar(
    consolidado: Consolidado,
    capsys: pytest.CaptureFixture[str],
    relatorio: Path,
    *argumentos: str,
    papel: str = "consolidado_operacao",
) -> tuple[int, str, str]:
    codigo = main(
        ["reconciliar", "--relatorio-livro", str(relatorio), *argumentos],
        ambiente=_ambiente_consolidado(consolidado, papel),
    )
    saida = capsys.readouterr()
    return codigo, saida.out, saida.err


def _sem_valores(log: str) -> None:
    linhas = [linha for linha in log.strip().splitlines() if linha]
    assert linhas, "nenhum log emitido"
    for linha in linhas:
        json.loads(linha)
        for valor in VALORES:
            assert valor not in linha, f"valor {valor} no log: {linha}"


def test_relatorio_do_livro_tem_os_totais_por_data_ate_a_ultima_posicao(
    livro: Livro, eventos: list[tuple[int, uuid.UUID, Any]], tmp_path: Path
) -> None:
    caminho, log = _gravar_relatorio(livro, tmp_path / "relatorio.json")
    relatorio = json.loads(caminho.read_text(encoding="utf-8"))

    assert relatorio["formato"] == "cashflow.relatorio-livro.v1"
    assert relatorio["ate_posicao"] == 5
    assert relatorio["datas"] == [
        {
            "data": "2026-11-03",
            "quantidade": 2,
            "total_creditos": "1234.56",
            "total_debitos": "789.01",
        },
        {
            "data": "2026-11-04",
            "quantidade": 1,
            "total_creditos": "4321.09",
            "total_debitos": "0.00",
        },
        {
            "data": "2026-11-05",
            "quantidade": 2,
            "total_creditos": "555.37",
            "total_debitos": "98.76",
        },
    ]
    _sem_valores(log)
    registro = json.loads(log.strip().splitlines()[-1])
    assert registro["mensagem"] == "relatório do livro exportado"
    assert (registro["ate_posicao"], registro["datas"], registro["lancamentos"]) == (
        5,
        3,
        5,
    )


def test_relatorio_ate_uma_posicao_inclui_so_as_anteriores(
    livro: Livro, eventos: list[tuple[int, uuid.UUID, Any]]
) -> None:
    feito = _relatorio_do_livro(livro, "--ate-posicao", "2")

    assert feito.returncode == 0, feito.stderr
    relatorio = json.loads(feito.stdout)
    assert relatorio["ate_posicao"] == 2
    assert [d["data"] for d in relatorio["datas"]] == ["2026-11-03"]


def test_posicao_lacunada_e_saldo_alterado_aparecem_no_relatorio(
    livro: Livro,
    consolidado: Consolidado,
    eventos: list[tuple[int, uuid.UUID, Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caminho, _ = _gravar_relatorio(livro, tmp_path / "relatorio.json")
    # Divergência injetada também no saldo que a API serve: como o dono das
    # tabelas, fora dos papéis da aplicação.
    with psycopg.connect(consolidado.conninfo("consolidado_migracao")) as conexao:
        conexao.execute(
            "UPDATE saldo_diario SET total_creditos = total_creditos + 555,"
            " saldo_dia = saldo_dia + 555 WHERE data = '2026-11-03'"
        )
    try:
        codigo, saida, erro = _reconciliar(consolidado, capsys, caminho, "--json")
        codigo_texto, texto, erro_texto = _reconciliar(consolidado, capsys, caminho)
    finally:
        with psycopg.connect(consolidado.conninfo("consolidado_migracao")) as conexao:
            conexao.execute(
                "UPDATE saldo_diario SET total_creditos = total_creditos - 555,"
                " saldo_dia = saldo_dia - 555 WHERE data = '2026-11-03'"
            )

    assert codigo == 3, erro
    resultado = json.loads(saida)
    assert resultado["conciliada"] is False
    assert resultado["ate_posicao"] == 5
    assert resultado["checkpoint"] == 3
    assert resultado["datas_no_livro"] == 3
    assert resultado["divergencias"] == [
        {
            "data": "2026-11-05",
            "tipo": "totais_diferentes",
            "livro": {
                "quantidade": 2,
                "total_creditos": "555.37",
                "total_debitos": "98.76",
            },
            "projecao": {
                "quantidade": 1,
                "total_creditos": "0.00",
                "total_debitos": "98.76",
            },
        }
    ]
    assert resultado["posicoes_faltantes"] == 1
    assert resultado["primeiros_faltantes"] == [{"inicio": 4, "fim": 4}]
    assert resultado["saldo_inconsistente"] == [
        {
            "data": "2026-11-03",
            "saldo": {"total_creditos": "1789.56", "total_debitos": "789.01"},
            "contribuicoes": {"total_creditos": "1234.56", "total_debitos": "789.01"},
        }
    ]

    assert codigo_texto == 3, erro_texto
    assert "divergência em 2026-11-05: totais diferentes" in texto
    assert "posições faltantes na ativa (primeiras): 4" in texto
    assert "saldo materializado diferente das contribuições em 2026-11-03" in texto
    assert "resultado: divergente" in texto

    for log in (erro, erro_texto):
        _sem_valores(log)
        registro = json.loads(log.strip().splitlines()[-1])
        assert registro["mensagem"] == "reconciliação concluída"
        assert registro["conciliada"] is False
        assert registro["posicoes_faltantes"] == 1
        assert registro["operador"] == "consolidado_operacao"


def test_sem_divergencia_a_reconciliacao_concilia(
    livro: Livro,
    consolidado: Consolidado,
    eventos: list[tuple[int, uuid.UUID, Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    faltante = [(e, payload) for p, e, payload in eventos if p == SEM_PROJECAO]
    _projetar(consolidado, faltante)
    caminho, _ = _gravar_relatorio(livro, tmp_path / "relatorio.json")

    codigo, saida, erro = _reconciliar(consolidado, capsys, caminho)

    assert codigo == 0, erro
    assert "datas divergentes: 0; posições faltantes: 0" in saida
    assert "resultado: conciliada" in saida
    _sem_valores(erro)


def test_relatorio_com_outra_identidade_falha_sem_saida(
    livro: Livro, eventos: list[tuple[int, uuid.UUID, Any]]
) -> None:
    for papel in ("livro_api", "livro_migracao"):
        feito = _relatorio_do_livro(livro, papel=papel)

        assert feito.returncode == 1
        assert feito.stdout == ""
        assert f"identidade {papel} não autorizada" in feito.stderr


def test_relatorio_alem_da_ultima_posicao_e_recusado(
    livro: Livro, eventos: list[tuple[int, uuid.UUID, Any]]
) -> None:
    feito = _relatorio_do_livro(livro, "--ate-posicao", "6")

    assert feito.returncode == 1
    assert feito.stdout == ""
    assert "posição 6 além da última do livro (5)" in feito.stderr


def test_reconciliacao_com_outra_identidade_falha(
    livro: Livro,
    consolidado: Consolidado,
    eventos: list[tuple[int, uuid.UUID, Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caminho, _ = _gravar_relatorio(livro, tmp_path / "relatorio.json")

    for papel in ("consolidado_api", "consolidado_worker", "consolidado_migracao"):
        codigo, saida, erro = _reconciliar(consolidado, capsys, caminho, papel=papel)

        assert codigo == 1
        assert saida == ""
        assert "reconciliar recusado" in erro


def test_no_compose_cada_lado_so_alcanca_o_proprio_banco() -> None:
    if not (RAIZ / "infra/.env").exists():
        pytest.skip("infra/.env ausente: o compose não interpola as senhas")
    feito = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE),
            "--profile",
            "operacao",
            "config",
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert feito.returncode == 0, feito.stderr
    servicos = json.loads(feito.stdout)["services"]

    relatorio, coordenador = servicos["livro-relatorio"], servicos["coordenador"]
    assert set(relatorio["networks"]) == {"rede-livro"}
    assert set(coordenador["networks"]) == {"rede-consolidado"}
    assert not relatorio.get("ports") and not coordenador.get("ports")
    assert relatorio["environment"]["PG_LIVRO_HOST"] == "pg-livro"
    assert not any(v.startswith("PG_CONSOLIDADO") for v in relatorio["environment"])
    assert not any(v.startswith("PG_LIVRO") for v in coordenador["environment"])
