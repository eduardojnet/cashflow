"""Republicação restrita a livro_replay contra PostgreSQL e RabbitMQ reais (003-T4).

O comando reabre os eventos a partir de uma posição e o ciclo normal do
relay os publica de novo (FR-005; TD-004; INFRA 7.6). Outro papel falha sem
reabrir nada: livro_api não tem privilégio na outbox; livro_relay e o dono
da tabela são barrados pelo gatilho de reabertura (migração 0003; NFR-002).

Mesmos descartáveis de test_publicacao_rabbitmq.py (INFRA 9.1, 9.2). A
partir da raiz do repositório:

    uv run pytest services/relay/tests/integracao

Sem docker no PATH, o módulo é pulado com o motivo explícito.
"""

import asyncio
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import aio_pika
import psycopg
import pytest
from lancamentos.adaptadores.postgres import UnidadeDeTrabalhoLivroPostgres
from lancamentos.aplicacao import RegistrarLancamentoCmd
from lancamentos.tests.integracao.livro_descartavel import Livro, livro_descartavel
from relay.adaptadores.postgres import (
    FabricaConexao,
    RegistroPublicacaoPostgres,
    ReservaOutboxPostgres,
)
from relay.adaptadores.rabbitmq import PublicadorRabbitMQ
from relay.aplicacao import PoliticaRetentativa, PublicarPendentes, ResultadoPublicacao
from relay.comandos.republicar import main
from relay.tests.integracao.rabbitmq_descartavel import (
    FILA,
    Broker,
    rabbitmq_descartavel,
)

SERVICOS = Path(__file__).resolve().parents[3]
# Lease curto: o evento recém-publicado volta à reserva logo após reaberto.
LEASE = timedelta(milliseconds=200)
POLITICA = PoliticaRetentativa(base=timedelta(minutes=1), teto=timedelta(minutes=4))

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker ausente no PATH"
)


@pytest.fixture(scope="module")
def livro() -> Iterator[Livro]:
    with livro_descartavel("republicacao-relay") as banco:
        yield banco


@pytest.fixture(scope="module")
def broker() -> Iterator[Broker]:
    with rabbitmq_descartavel("republicacao-relay") as rabbitmq:
        yield rabbitmq


@pytest.fixture(autouse=True)
def isolado(livro: Livro, broker: Broker) -> None:
    # Cada teste parte da outbox publicada e da fila vazia.
    _ciclo(livro, broker)
    asyncio.run(_consumir(broker))


def _fabrica(livro: Livro, papel: str) -> FabricaConexao:
    @asynccontextmanager
    async def conexao() -> AsyncIterator[psycopg.AsyncConnection[Any]]:
        async with await psycopg.AsyncConnection.connect(
            livro.conninfo(papel), autocommit=True
        ) as aberta:
            yield aberta

    return conexao


def _consultar(livro: Livro, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with psycopg.connect(livro.conninfo("livro_migracao")) as conexao:
        return conexao.execute(sql, params).fetchall()


def _registrar(livro: Livro, quantidade: int) -> list[tuple[uuid.UUID, int]]:
    """Registra lançamentos reais e devolve (`evento_id`, `posicao`) em ordem."""
    unidade = UnidadeDeTrabalhoLivroPostgres(_fabrica(livro, "livro_api"))

    async def registrar() -> list[uuid.UUID]:
        lancamentos = []
        for _ in range(quantidade):
            cmd = RegistrarLancamentoCmd.de(
                tipo="CREDITO", valor="10.00", data_lancamento="2026-11-06"
            )
            await unidade.registrar(cmd, f"chave-{uuid.uuid4()}", "cliente-replay")
            lancamentos.append(cmd.lancamento.id)
        return lancamentos

    lancamentos = asyncio.run(registrar())
    linhas = _consultar(
        livro,
        "SELECT evento_id, posicao FROM outbox WHERE lancamento_id = ANY(%s)"
        " ORDER BY posicao",
        lancamentos,
    )
    return [(evento_id, posicao) for evento_id, posicao in linhas]


def _estado(livro: Livro) -> dict[uuid.UUID, tuple[Any, int]]:
    """`publicado_em` e `tentativas` de toda a outbox."""
    linhas = _consultar(livro, "SELECT evento_id, publicado_em, tentativas FROM outbox")
    return {
        evento_id: (publicado_em, tentativas)
        for evento_id, publicado_em, tentativas in linhas
    }


async def _consumir(broker: Broker) -> list[aio_pika.abc.AbstractIncomingMessage]:
    """Esvazia a fila como `worker`, com ACK, e devolve o que leu."""
    conexao = await aio_pika.connect(broker.url("worker"))
    lidas = []
    async with conexao:
        canal = await conexao.channel()
        fila = await canal.get_queue(FILA, ensure=False)
        while (mensagem := await fila.get(no_ack=False, fail=False)) is not None:
            await mensagem.ack()
            lidas.append(mensagem)
    return lidas


def _ciclo(livro: Livro, broker: Broker) -> dict[ResultadoPublicacao, int]:
    async def executar() -> dict[ResultadoPublicacao, int]:
        async with PublicadorRabbitMQ(broker.url("relay"), 10.0) as publicador:
            caso = PublicarPendentes(
                ReservaOutboxPostgres(
                    _fabrica(livro, "livro_relay"), "relay-teste", 100, LEASE
                ),
                publicador,
                RegistroPublicacaoPostgres(_fabrica(livro, "livro_relay")),
                POLITICA,
            )
            return (await caso.executar()).resultados

    return asyncio.run(executar())


def _ambiente(livro: Livro, papel: str) -> dict[str, str]:
    return {
        "PG_LIVRO_HOST": "127.0.0.1",
        "PG_LIVRO_PORTA": str(livro.porta),
        "PG_LIVRO_USUARIO_REPLAY": papel,
        "PG_LIVRO_SENHA_REPLAY": livro.senhas[papel],
    }


def test_eventos_a_partir_da_posicao_voltam_a_pendentes_e_sao_publicados(
    livro: Livro, broker: Broker
) -> None:
    eventos = _registrar(livro, 5)
    assert _ciclo(livro, broker)[ResultadoPublicacao.CONFIRMADO] == 5
    assert len(asyncio.run(_consumir(broker))) == 5
    desde = eventos[2][1]
    reabrir = {evento_id for evento_id, posicao in eventos if posicao >= desde}
    antes = _estado(livro)

    # O comando real, como o operador o executa.
    feito = subprocess.run(
        [
            sys.executable,
            "-m",
            "relay.comandos.republicar",
            "--desde-posicao",
            str(desde),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "PYTHONPATH": str(SERVICOS),
            **_ambiente(livro, "livro_replay"),
        },
    )

    assert feito.returncode == 0, feito.stderr
    assert feito.stdout.strip() == f"3 eventos reabertos a partir da posição {desde}"
    depois = _estado(livro)
    assert set(depois) == set(antes)
    for evento_id, (publicado_em, tentativas) in depois.items():
        if evento_id in reabrir:
            assert (publicado_em, tentativas) == (None, 0)
        else:
            assert depois[evento_id] == antes[evento_id]

    # Vencido o lease da reserva anterior, o ciclo normal republica.
    time.sleep(LEASE.total_seconds() * 2)
    resultados = _ciclo(livro, broker)

    assert resultados[ResultadoPublicacao.CONFIRMADO] == 3
    mensagens = asyncio.run(_consumir(broker))
    assert sorted(m.message_id or "" for m in mensagens) == sorted(
        str(evento_id) for evento_id in reabrir
    )
    assert all(publicado_em for publicado_em, _ in _estado(livro).values())


@pytest.mark.parametrize(
    ("papel", "sqlstate"),
    [
        # Sem SELECT nem UPDATE na outbox (002 migração 0001).
        ("livro_api", "42501"),
        # Tem UPDATE em `publicado_em` para marcar (0002); o gatilho barra.
        ("livro_relay", "42501"),
        # Dono da tabela: o gatilho barra também.
        ("livro_migracao", "42501"),
    ],
)
def test_republicacao_com_outro_papel_falha_sem_reabrir(
    livro: Livro,
    broker: Broker,
    papel: str,
    sqlstate: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    eventos = _registrar(livro, 2)
    assert _ciclo(livro, broker)[ResultadoPublicacao.CONFIRMADO] == 2
    antes = _estado(livro)

    codigo = main(
        ["--desde-posicao", str(eventos[0][1])], ambiente=_ambiente(livro, papel)
    )

    assert codigo == 1
    assert f"republicação recusada ({sqlstate})" in capsys.readouterr().err
    assert _estado(livro) == antes


def test_posicao_nao_positiva_e_recusada(livro: Livro) -> None:
    with pytest.raises(SystemExit) as saida:
        main(["--desde-posicao", "0"], ambiente=_ambiente(livro, "livro_replay"))

    assert saida.value.code == 2
