"""Publicação com confirms e `mandatory` contra RabbitMQ real (003-T2).

PostgreSQL descartável do livro (INFRA 9.1) e RabbitMQ descartável com a
topologia, os usuários e as permissões do compose (INFRA 9.2). Os eventos
entram pela unidade de trabalho real de lançamentos, como livro_api; o relay
reserva e marca como livro_relay e publica como o usuário `relay`; a fila é
lida como `worker`, como o consumidor fará (spec 004). A partir da raiz do
repositório:

    uv run pytest services/relay/tests/integracao

Sem docker no PATH, o módulo é pulado com o motivo explícito.
"""

import asyncio
import json
import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import aio_pika
import psycopg
import pytest
from lancamentos.adaptadores.postgres import UnidadeDeTrabalhoLivroPostgres
from lancamentos.aplicacao import RegistrarLancamentoCmd
from livro_descartavel import Livro, livro_descartavel
from rabbitmq_descartavel import FILA, Broker, rabbitmq_descartavel
from relay.adaptadores.postgres import (
    FabricaConexao,
    RegistroPublicacaoPostgres,
    ReservaOutboxPostgres,
)
from relay.adaptadores.rabbitmq import EXCHANGE, ROUTING_KEY, PublicadorRabbitMQ
from relay.aplicacao import (
    EventoReservado,
    PublicarPendentes,
    ResultadoPublicacao,
    ResumoCiclo,
)

TIMEOUT_CONFIRMACAO = 10.0
# Lease longo: o evento não confirmado não volta à reserva durante o teste.
LEASE = timedelta(minutes=5)

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker ausente no PATH"
)


@pytest.fixture(scope="module")
def livro() -> Iterator[Livro]:
    with livro_descartavel("publicacao-relay") as banco:
        yield banco


@pytest.fixture(scope="module")
def broker() -> Iterator[Broker]:
    with rabbitmq_descartavel("publicacao-relay") as rabbitmq:
        yield rabbitmq


@pytest.fixture(autouse=True)
def isolado(livro: Livro, broker: Broker) -> None:
    # Cada teste enxerga só os seus eventos e as suas mensagens.
    _executar(
        livro, "UPDATE outbox SET publicado_em = now() WHERE publicado_em IS NULL"
    )
    asyncio.run(_consumir(broker))


def _fabrica(livro: Livro, papel: str) -> FabricaConexao:
    @asynccontextmanager
    async def conexao() -> AsyncIterator[psycopg.AsyncConnection[Any]]:
        async with await psycopg.AsyncConnection.connect(
            livro.conninfo(papel), autocommit=True
        ) as aberta:
            yield aberta

    return conexao


def _executar(livro: Livro, sql: str, *params: object) -> None:
    with psycopg.connect(livro.conninfo("livro_migracao")) as conexao:
        conexao.execute(sql, params)


def _consultar(livro: Livro, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with psycopg.connect(livro.conninfo("livro_migracao")) as conexao:
        return conexao.execute(sql, params).fetchall()


def _registrar(livro: Livro, quantidade: int) -> list[uuid.UUID]:
    """Registra lançamentos reais e devolve os `evento_id` em ordem de posição."""
    unidade = UnidadeDeTrabalhoLivroPostgres(_fabrica(livro, "livro_api"))

    async def registrar() -> list[uuid.UUID]:
        lancamentos = []
        for _ in range(quantidade):
            cmd = RegistrarLancamentoCmd.de(
                tipo="DEBITO", valor="25.40", data_lancamento="2026-11-06"
            )
            await unidade.registrar(cmd, f"chave-{uuid.uuid4()}", "cliente-relay")
            lancamentos.append(cmd.lancamento.id)
        return lancamentos

    lancamentos = asyncio.run(registrar())
    linhas = _consultar(
        livro,
        "SELECT evento_id FROM outbox WHERE lancamento_id = ANY(%s) ORDER BY posicao",
        lancamentos,
    )
    return [evento_id for (evento_id,) in linhas]


def _publicado_em(livro: Livro, eventos: list[uuid.UUID]) -> dict[uuid.UUID, Any]:
    return dict(
        _consultar(
            livro,
            "SELECT evento_id, publicado_em FROM outbox WHERE evento_id = ANY(%s)",
            eventos,
        )
    )


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


class _PublicadorObservado:
    """Publicador real que anota `publicado_em` no instante do confirm."""

    def __init__(self, real: PublicadorRabbitMQ, livro: Livro) -> None:
        self._real = real
        self._livro = livro
        self.no_confirm: dict[uuid.UUID, Any] = {}

    async def publicar(self, evento: EventoReservado) -> ResultadoPublicacao:
        resultado = await self._real.publicar(evento)
        [(publicado_em,)] = await asyncio.to_thread(
            _consultar,
            self._livro,
            "SELECT publicado_em FROM outbox WHERE evento_id = %s",
            evento.evento_id,
        )
        self.no_confirm[evento.evento_id] = publicado_em
        return resultado


def _ciclo(
    livro: Livro, broker: Broker, routing_key: str = ROUTING_KEY
) -> tuple[ResumoCiclo, dict[uuid.UUID, Any]]:
    async def executar() -> tuple[ResumoCiclo, dict[uuid.UUID, Any]]:
        async with PublicadorRabbitMQ(
            broker.url("relay"), TIMEOUT_CONFIRMACAO, routing_key=routing_key
        ) as real:
            publicador = _PublicadorObservado(real, livro)
            caso = PublicarPendentes(
                ReservaOutboxPostgres(
                    _fabrica(livro, "livro_relay"), "relay-teste", 100, LEASE
                ),
                publicador,
                RegistroPublicacaoPostgres(_fabrica(livro, "livro_relay")),
            )
            return await caso.executar(), publicador.no_confirm

    return asyncio.run(executar())


def test_evento_confirmado_chega_a_fila_persistente_e_e_marcado(
    livro: Livro, broker: Broker
) -> None:
    eventos = _registrar(livro, 3)
    payloads = dict(
        _consultar(
            livro,
            "SELECT evento_id, payload FROM outbox WHERE evento_id = ANY(%s)",
            eventos,
        )
    )

    resumo, no_confirm = _ciclo(livro, broker)

    assert resumo.reservados == 3
    assert resumo.resultados[ResultadoPublicacao.CONFIRMADO] == 3
    # Pendente no instante do confirm; publicado só depois dele (FR-002).
    assert no_confirm == dict.fromkeys(eventos)
    assert all(_publicado_em(livro, eventos).values())

    mensagens = asyncio.run(_consumir(broker))
    assert sorted(m.message_id or "" for m in mensagens) == sorted(
        str(evento) for evento in eventos
    )
    for mensagem in mensagens:
        assert mensagem.exchange == EXCHANGE
        assert mensagem.routing_key == ROUTING_KEY
        assert mensagem.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
        assert mensagem.content_type == "application/json"
        assert mensagem.type == "LancamentoRegistrado"
        evento_id = uuid.UUID(mensagem.message_id)
        assert json.loads(mensagem.body) == payloads[evento_id]


def test_mensagem_devolvida_sem_binding_nao_marca_o_evento(
    livro: Livro, broker: Broker
) -> None:
    eventos = _registrar(livro, 2)

    # Routing key sem fila ligada: o broker confirma, mas devolve (mandatory).
    resumo, _ = _ciclo(livro, broker, routing_key="lancamento.sem.binding")

    assert resumo.resultados[ResultadoPublicacao.DEVOLVIDO] == 2
    assert resumo.resultados[ResultadoPublicacao.CONFIRMADO] == 0
    assert _publicado_em(livro, eventos) == dict.fromkeys(eventos)
    assert asyncio.run(_consumir(broker)) == []


def test_evento_publicado_nao_e_publicado_de_novo(livro: Livro, broker: Broker) -> None:
    eventos = _registrar(livro, 1)

    primeiro, _ = _ciclo(livro, broker)
    segundo, _ = _ciclo(livro, broker)

    assert primeiro.resultados[ResultadoPublicacao.CONFIRMADO] == 1
    assert segundo.reservados == 0
    [mensagem] = asyncio.run(_consumir(broker))
    assert mensagem.message_id == str(eventos[0])
    # Publicado continua na outbox (D-001; INFRA 7.2).
    assert len(_publicado_em(livro, eventos)) == 1
