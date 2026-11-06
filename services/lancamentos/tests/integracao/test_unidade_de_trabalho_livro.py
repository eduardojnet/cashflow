"""Unidade de trabalho do livro contra PostgreSQL real (002-T3; INFRA 9.1).

O livro é imutável por privilégio (TD-003): lançamento gravado não sai mais,
nem com ROLLBACK posterior, e a posição consumida fica consumida. Por isso
este módulo não usa o pg-livro do compose, que é o livro do ambiente, e sim
um PostgreSQL descartável com a mesma imagem do compose, os mesmos papéis
(infra/postgres/livro/10-papeis.sh) e as mesmas migrações Alembic aplicadas
por livro_migracao. A unidade de trabalho conecta como livro_api, com os
privilégios reais da migração 0001.

O contêiner publica a porta só em 127.0.0.1, numa porta efêmera, usa senhas
aleatórias geradas a cada execução e é removido com seus volumes ao fim do
módulo. A partir da raiz do repositório:

    uv run pytest services/lancamentos/tests/integracao

Sem docker no PATH, o módulo é pulado com o motivo explícito.
"""

import asyncio
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from lancamentos.adaptadores.postgres import (
    TIPO_EVENTO,
    FabricaConexao,
    UnidadeDeTrabalhoLivroPostgres,
)
from lancamentos.aplicacao import (
    ConflitoIdempotencia,
    RegistrarLancamentoCmd,
    RespostaRegistro,
)
from psycopg.conninfo import make_conninfo

RAIZ = Path(__file__).resolve().parents[4]
COMPOSE = RAIZ / "infra/compose.yaml"
PAPEIS = RAIZ / "infra/postgres/livro"
ALEMBIC_INI = RAIZ / "services/lancamentos/migracoes/alembic.ini"
BANCO = "livro"
CONCORRENTES = 100

# A espera do contador serializa os registros; 100 conexões simultâneas
# precisam de folga acima do padrão de 100 do PostgreSQL.
MAX_CONEXOES = 200

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker ausente no PATH"
)


@dataclass(frozen=True)
class Livro:
    porta: int
    senhas: dict[str, str]

    def conninfo(self, papel: str) -> str:
        return make_conninfo(
            host="127.0.0.1",
            port=self.porta,
            dbname=BANCO,
            user=papel,
            password=self.senhas[papel],
        )


def _docker(
    *args: str, env: dict[str, str] | None = None, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _imagem_do_compose() -> str:
    # A mesma imagem fixada para pg-livro (001 TD-007), sem segunda cópia.
    encontrada = re.search(r"image:\s*(postgres:\S+)", COMPOSE.read_text())
    assert encontrada, "imagem do PostgreSQL não encontrada em infra/compose.yaml"
    return encontrada.group(1)


def _aguardar_pronto(id_: str, livro: Livro) -> None:
    # Durante a criação do volume o entrypoint sobe um servidor temporário só
    # no soquete local; pronto é aceitar conexão TCP como livro_migracao.
    limite = time.monotonic() + 90
    while time.monotonic() < limite:
        pronto = _docker(
            "exec", id_, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", "-d", BANCO
        )
        if pronto.returncode == 0:
            try:
                with psycopg.connect(
                    livro.conninfo("livro_migracao"), connect_timeout=3
                ):
                    return
            except psycopg.OperationalError:
                pass
        time.sleep(1)
    logs = _docker("logs", id_).stderr[-2000:]
    raise AssertionError(f"PostgreSQL descartável não ficou pronto:\n{logs}")


@pytest.fixture(scope="module")
def livro() -> Iterator[Livro]:
    senhas = {
        papel: secrets.token_hex(24)
        for papel in [
            "postgres",
            "livro_migracao",
            "livro_api",
            "livro_relay",
            "livro_replay",
        ]
    }
    # As senhas chegam ao docker por variável de ambiente, não pela linha de
    # comando, como em infra/postgres/livro/10-papeis.sh.
    env = {
        **os.environ,
        "POSTGRES_PASSWORD": senhas["postgres"],
        "PG_LIVRO_SENHA_MIGRACAO": senhas["livro_migracao"],
        "PG_LIVRO_SENHA_API": senhas["livro_api"],
        "PG_LIVRO_SENHA_RELAY": senhas["livro_relay"],
        "PG_LIVRO_SENHA_REPLAY": senhas["livro_replay"],
    }
    criado = _docker(
        "run",
        "-d",
        "--label",
        "cashflow.teste=unidade-de-trabalho-livro",
        "-p",
        "127.0.0.1::5432",
        "-e",
        f"POSTGRES_DB={BANCO}",
        "-e",
        "POSTGRES_PASSWORD",
        "-e",
        "PG_LIVRO_SENHA_MIGRACAO",
        "-e",
        "PG_LIVRO_SENHA_API",
        "-e",
        "PG_LIVRO_SENHA_RELAY",
        "-e",
        "PG_LIVRO_SENHA_REPLAY",
        "-v",
        f"{PAPEIS}:/docker-entrypoint-initdb.d:ro",
        _imagem_do_compose(),
        "-c",
        f"max_connections={MAX_CONEXOES}",
        env=env,
        timeout=300,
    )
    assert criado.returncode == 0, criado.stderr
    id_ = criado.stdout.strip()
    try:
        porta = _docker("port", id_, "5432/tcp").stdout.split(":")[-1].strip()
        banco = Livro(porta=int(porta), senhas=senhas)
        _aguardar_pronto(id_, banco)
        migracao = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(ALEMBIC_INI),
                "upgrade",
                "head",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env={
                **os.environ,
                "PG_LIVRO_HOST": "127.0.0.1",
                "PG_LIVRO_PORTA": porta,
                "PG_LIVRO_SENHA_MIGRACAO": senhas["livro_migracao"],
            },
        )
        assert migracao.returncode == 0, migracao.stderr
        yield banco
    finally:
        _docker("rm", "-f", "-v", id_)


def _fabrica(livro: Livro) -> FabricaConexao:
    @asynccontextmanager
    async def conexao() -> AsyncIterator[psycopg.AsyncConnection[Any]]:
        async with await psycopg.AsyncConnection.connect(
            livro.conninfo("livro_api"), autocommit=True
        ) as aberta:
            yield aberta

    return conexao


@dataclass(frozen=True)
class Estado:
    contador: int
    lancamentos: int
    eventos: int
    respostas: int


def _estado(livro: Livro) -> Estado:
    # livro_migracao, dono das tabelas: livro_api não lê a outbox.
    with psycopg.connect(livro.conninfo("livro_migracao")) as conexao:
        linha = conexao.execute(
            "SELECT (SELECT valor FROM contador_posicao),"
            " (SELECT count(*) FROM lancamento),"
            " (SELECT count(*) FROM outbox),"
            " (SELECT count(*) FROM resposta_idempotente)"
        ).fetchone()
    assert linha is not None
    return Estado(*linha)


def _consultar(livro: Livro, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with psycopg.connect(livro.conninfo("livro_migracao")) as conexao:
        return conexao.execute(sql, params).fetchall()


def _cmd(**campos: object) -> RegistrarLancamentoCmd:
    dados: dict[str, object] = {
        "tipo": "CREDITO",
        "valor": "10.50",
        "data_lancamento": "2026-11-06",
        "descricao": "venda",
    }
    dados.update(campos)
    return RegistrarLancamentoCmd.de(**dados)


def _chave() -> str:
    return f"chave-{uuid.uuid4()}"


def test_registro_grava_lancamento_evento_e_resposta(livro: Livro) -> None:
    antes = _estado(livro)
    cmd = _cmd()
    unidade = UnidadeDeTrabalhoLivroPostgres(_fabrica(livro))

    resposta = asyncio.run(unidade.registrar(cmd, _chave(), "cliente-a"))

    posicao = antes.contador + 1
    assert resposta == RespostaRegistro(
        status_http=201, corpo={"id": str(cmd.lancamento.id), "posicao": posicao}
    )
    assert _estado(livro) == Estado(
        contador=posicao,
        lancamentos=antes.lancamentos + 1,
        eventos=antes.eventos + 1,
        respostas=antes.respostas + 1,
    )
    [lancamento] = _consultar(
        livro,
        "SELECT posicao, tipo, valor, data_lancamento, descricao, cliente_id,"
        " registrado_em FROM lancamento WHERE id = %s",
        cmd.lancamento.id,
    )
    assert lancamento[:6] == (
        posicao,
        "CREDITO",
        Decimal("10.50"),
        date(2026, 11, 6),
        "venda",
        "cliente-a",
    )
    [(evento_id, posicao_evento, tipo_evento, payload)] = _consultar(
        livro,
        "SELECT evento_id, posicao, tipo_evento, payload FROM outbox"
        " WHERE lancamento_id = %s",
        cmd.lancamento.id,
    )
    assert (posicao_evento, tipo_evento) == (posicao, TIPO_EVENTO)
    # Contrato v1 (design 002 §4): sem descrição nem cliente no evento.
    assert payload == {
        "evento_id": str(evento_id),
        "versao": 1,
        "lancamento_id": str(cmd.lancamento.id),
        "posicao": posicao,
        "tipo": "CREDITO",
        "valor": "10.50",
        "data_lancamento": "2026-11-06",
        "registrado_em": lancamento[6].isoformat(),
    }


class FalhaInjetada(Exception):
    pass


@pytest.mark.parametrize(
    "etapa",
    ["_proxima_posicao", "_inserir_lancamento", "_inserir_evento", "_inserir_resposta"],
)
def test_falha_no_meio_nao_deixa_registro_parcial(livro: Livro, etapa: str) -> None:
    class UnidadeQueFalha(UnidadeDeTrabalhoLivroPostgres):
        pass

    original = getattr(UnidadeDeTrabalhoLivroPostgres, etapa)

    async def executa_e_falha(self: Any, *args: Any) -> Any:
        # A etapa grava de fato; a falha vem depois, antes do COMMIT.
        await original(self, *args)
        raise FalhaInjetada(etapa)

    setattr(UnidadeQueFalha, etapa, executa_e_falha)
    antes = _estado(livro)
    cmd = _cmd()
    chave = _chave()

    with pytest.raises(FalhaInjetada):
        asyncio.run(UnidadeQueFalha(_fabrica(livro)).registrar(cmd, chave, "cliente-a"))

    assert _estado(livro) == antes
    assert (
        _consultar(livro, "SELECT 1 FROM lancamento WHERE id = %s", cmd.lancamento.id)
        == []
    )

    # Sem lacuna: a mesma chave registra depois com a posição seguinte.
    resposta = asyncio.run(
        UnidadeDeTrabalhoLivroPostgres(_fabrica(livro)).registrar(
            cmd, chave, "cliente-a"
        )
    )
    assert resposta.repetida is False
    assert resposta.corpo["posicao"] == antes.contador + 1


def test_concorrentes_geram_posicoes_unicas_e_contiguas(livro: Livro) -> None:
    antes = _estado(livro)
    unidade = UnidadeDeTrabalhoLivroPostgres(_fabrica(livro))
    comandos = [_cmd(valor=f"{i}.01") for i in range(1, CONCORRENTES + 1)]

    async def todos() -> list[RespostaRegistro]:
        return await asyncio.gather(
            *(unidade.registrar(cmd, _chave(), "cliente-a") for cmd in comandos)
        )

    respostas = asyncio.run(todos())

    posicoes = sorted(int(str(r.corpo["posicao"])) for r in respostas)
    esperadas = list(range(antes.contador + 1, antes.contador + CONCORRENTES + 1))
    assert posicoes == esperadas
    assert all(r.status_http == 201 and not r.repetida for r in respostas)
    assert _estado(livro) == Estado(
        contador=antes.contador + CONCORRENTES,
        lancamentos=antes.lancamentos + CONCORRENTES,
        eventos=antes.eventos + CONCORRENTES,
        respostas=antes.respostas + CONCORRENTES,
    )
    # Lançamento e evento de cada resposta compartilham a posição devolvida.
    ids = [str(cmd.lancamento.id) for cmd in comandos]
    gravadas = _consultar(
        livro,
        "SELECT l.id::text, l.posicao, o.posicao FROM lancamento l"
        " JOIN outbox o ON o.lancamento_id = l.id WHERE l.id::text = ANY(%s)",
        ids,
    )
    por_id = {id_: (p_lancamento, p_evento) for id_, p_lancamento, p_evento in gravadas}
    for cmd, resposta in zip(comandos, respostas, strict=True):
        posicao = resposta.corpo["posicao"]
        assert resposta.corpo["id"] == str(cmd.lancamento.id)
        assert por_id[str(cmd.lancamento.id)] == (posicao, posicao)


def test_repeticao_identica_devolve_a_resposta_original(livro: Livro) -> None:
    unidade = UnidadeDeTrabalhoLivroPostgres(_fabrica(livro))
    chave = _chave()
    primeira = asyncio.run(unidade.registrar(_cmd(), chave, "cliente-a"))
    depois_da_primeira = _estado(livro)

    # Novo comando com o mesmo conteúdo: outro `id` gerado, mesmo hash.
    repetida = asyncio.run(unidade.registrar(_cmd(valor="10.5"), chave, "cliente-a"))

    assert repetida == RespostaRegistro(
        status_http=primeira.status_http, corpo=primeira.corpo, repetida=True
    )
    assert _estado(livro) == depois_da_primeira


def test_mesma_chave_com_conteudo_diferente_e_conflito(livro: Livro) -> None:
    unidade = UnidadeDeTrabalhoLivroPostgres(_fabrica(livro))
    chave = _chave()
    asyncio.run(unidade.registrar(_cmd(), chave, "cliente-a"))
    depois_da_primeira = _estado(livro)

    with pytest.raises(ConflitoIdempotencia):
        asyncio.run(unidade.registrar(_cmd(valor="99.00"), chave, "cliente-a"))

    assert _estado(livro) == depois_da_primeira


def test_mesma_chave_simultanea_registra_uma_vez(livro: Livro) -> None:
    antes = _estado(livro)
    unidade = UnidadeDeTrabalhoLivroPostgres(_fabrica(livro))
    chave = _chave()
    simultaneas = 20

    async def todas() -> list[RespostaRegistro]:
        return await asyncio.gather(
            *(unidade.registrar(_cmd(), chave, "cliente-a") for _ in range(simultaneas))
        )

    respostas = asyncio.run(todas())

    assert len({str(r.corpo) for r in respostas}) == 1
    assert sum(not r.repetida for r in respostas) == 1
    # As perdedoras desfizeram o incremento: o contador avança uma vez só.
    assert _estado(livro) == Estado(
        contador=antes.contador + 1,
        lancamentos=antes.lancamentos + 1,
        eventos=antes.eventos + 1,
        respostas=antes.respostas + 1,
    )


def test_mesma_chave_de_outro_cliente_e_outro_registro(livro: Livro) -> None:
    unidade = UnidadeDeTrabalhoLivroPostgres(_fabrica(livro))
    chave = _chave()

    de_a = asyncio.run(unidade.registrar(_cmd(), chave, "cliente-a"))
    de_b = asyncio.run(unidade.registrar(_cmd(), chave, "cliente-b"))

    assert not de_a.repetida and not de_b.repetida
    assert de_a.corpo["posicao"] != de_b.corpo["posicao"]
