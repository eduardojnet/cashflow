"""Publicação, confirms, `mandatory` e retentativa contra RabbitMQ real.

003-T2: só o confirm positivo sem return marca. 003-T3: Return e Nack do
broker real mantêm o evento pendente, reagendado com espera crescente e
lease liberado, sem removê-lo da outbox; vencida a espera, ele é publicado.

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
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from typing import Any

import aio_pika
import psycopg
import pytest
from lancamentos.adaptadores.postgres import UnidadeDeTrabalhoLivroPostgres
from lancamentos.aplicacao import RegistrarLancamentoCmd
from lancamentos.tests.integracao.livro_descartavel import (
    Livro,
    docker,
    livro_descartavel,
)
from relay.adaptadores.postgres import (
    FabricaConexao,
    RegistroPublicacaoPostgres,
    ReservaOutboxPostgres,
)
from relay.adaptadores.rabbitmq import EXCHANGE, ROUTING_KEY, PublicadorRabbitMQ
from relay.aplicacao import (
    EventoReservado,
    PoliticaRetentativa,
    PublicarPendentes,
    ResultadoPublicacao,
    ResumoCiclo,
)
from relay.tests.integracao.rabbitmq_descartavel import (
    FILA,
    Broker,
    rabbitmq_descartavel,
)

TIMEOUT_CONFIRMACAO = 10.0
# Lease longo: o evento não confirmado não volta à reserva durante o teste.
LEASE = timedelta(minutes=5)
# Base longa: o evento reagendado não volta à reserva durante o teste, salvo
# quando o teste antecipa `proxima_tentativa_em`.
POLITICA = PoliticaRetentativa(base=timedelta(minutes=1), teto=timedelta(minutes=4))
# Fila cheia com `reject-publish`: o broker responde Basic.Nack.
POLITICA_RECUSA = json.dumps({"max-length": 0, "overflow": "reject-publish"})

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


def _agora(livro: Livro) -> datetime:
    [(agora,)] = _consultar(livro, "SELECT now()")
    assert isinstance(agora, datetime)
    return agora


def _estado(livro: Livro, eventos: list[uuid.UUID]) -> dict[uuid.UUID, Any]:
    """Metadados de publicação por evento, para conferir o reagendamento."""
    linhas = _consultar(
        livro,
        "SELECT evento_id, publicado_em, tentativas, proxima_tentativa_em,"
        " lease_dono, lease_ate FROM outbox WHERE evento_id = ANY(%s)",
        eventos,
    )
    return {linha[0]: linha[1:] for linha in linhas}


def _antecipar(livro: Livro, eventos: list[uuid.UUID]) -> None:
    # Simula a passagem da espera sem dormir no teste.
    _executar(
        livro,
        "UPDATE outbox SET proxima_tentativa_em = now() WHERE evento_id = ANY(%s)",
        eventos,
    )


@contextmanager
def _fila_recusando(broker: Broker) -> Iterator[None]:
    """Fila de consolidado cheia e recusando publicações (Basic.Nack).

    Política aplicada pelo administrador do nó, como faria o operador; o
    relay segue sem permissão de configuração (INFRA 5.11).
    """

    def rabbitmqctl(*args: str) -> None:
        feito = docker("exec", broker.id_conteiner, "rabbitmqctl", "-q", *args)
        assert feito.returncode == 0, feito.stderr

    padrao = "^" + FILA.replace(".", "[.]") + "$"
    rabbitmqctl(
        "set_policy", "--apply-to", "queues", "teste-recusa", padrao, POLITICA_RECUSA
    )
    try:
        yield
    finally:
        rabbitmqctl("clear_policy", "teste-recusa")


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
                POLITICA,
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


def test_mensagem_devolvida_sem_binding_fica_pendente_e_e_reagendada(
    livro: Livro, broker: Broker
) -> None:
    eventos = _registrar(livro, 2)
    antes = _agora(livro)

    # Routing key sem fila ligada: o broker confirma, mas devolve (mandatory).
    resumo, _ = _ciclo(livro, broker, routing_key="lancamento.sem.binding")

    depois = _agora(livro)
    assert resumo.resultados[ResultadoPublicacao.DEVOLVIDO] == 2
    assert resumo.resultados[ResultadoPublicacao.CONFIRMADO] == 0
    assert asyncio.run(_consumir(broker)) == []
    estado = _estado(livro, eventos)
    assert set(estado) == set(eventos)
    for publicado_em, tentativas, proxima, lease_dono, lease_ate in estado.values():
        assert publicado_em is None
        assert tentativas == 1
        # Primeira falha espera a base, pelo relógio do banco (D-002).
        assert antes + POLITICA.base <= proxima <= depois + POLITICA.base
        assert (lease_dono, lease_ate) == (None, None)

    # Dentro da espera, nenhuma execução reserva o evento.
    durante, _ = _ciclo(livro, broker)
    assert durante.reservados == 0


def test_devolucoes_seguidas_aumentam_a_espera_ate_o_teto(
    livro: Livro, broker: Broker
) -> None:
    [evento] = _registrar(livro, 1)

    medidas = []
    for _ in range(4):
        antes = _agora(livro)
        resumo, _ = _ciclo(livro, broker, routing_key="lancamento.sem.binding")
        depois = _agora(livro)
        assert resumo.resultados[ResultadoPublicacao.DEVOLVIDO] == 1
        _, tentativas, proxima, _, _ = _estado(livro, [evento])[evento]
        medidas.append((proxima - depois, proxima - antes, tentativas))
        _antecipar(livro, [evento])

    # 1, 2 e 4 minutos, depois o teto de 4; uma tentativa a mais por ciclo.
    esperadas = [(1, 1), (2, 2), (4, 3), (4, 4)]
    for (minima, maxima, tentativas), (minutos, contador) in zip(
        medidas, esperadas, strict=True
    ):
        assert minima <= timedelta(minutes=minutos) <= maxima
        assert tentativas == contador


def test_nack_do_broker_mantem_pendente_e_a_retentativa_publica_depois(
    livro: Livro, broker: Broker
) -> None:
    eventos = _registrar(livro, 2)

    with _fila_recusando(broker):
        recusado, _ = _ciclo(livro, broker)

    assert recusado.resultados[ResultadoPublicacao.RECUSADO] == 2
    assert recusado.resultados[ResultadoPublicacao.CONFIRMADO] == 0
    estado = _estado(livro, eventos)
    assert {linha[0] for linha in estado.values()} == {None}
    assert {linha[1] for linha in estado.values()} == {1}
    assert asyncio.run(_consumir(broker)) == []

    # Vencida a espera e com a fila aceitando, a retentativa publica e marca.
    _antecipar(livro, eventos)
    retentativa, _ = _ciclo(livro, broker)

    assert retentativa.resultados[ResultadoPublicacao.CONFIRMADO] == 2
    assert all(_publicado_em(livro, eventos).values())
    mensagens = asyncio.run(_consumir(broker))
    assert sorted(m.message_id or "" for m in mensagens) == sorted(
        str(evento) for evento in eventos
    )
    # Nenhum evento removido da outbox (D-001; INFRA 4.10).
    assert set(_estado(livro, eventos)) == set(eventos)


def test_reagendar_nao_mexe_em_reserva_de_outra_execucao(livro: Livro) -> None:
    [evento_id] = _registrar(livro, 1)

    async def cenario() -> None:
        relay = _fabrica(livro, "livro_relay")
        [velho] = await ReservaOutboxPostgres(
            relay, "relay-a", 10, timedelta(milliseconds=1)
        ).reservar()
        # O lease de "relay-a" vence e "relay-b" reserva o mesmo evento.
        await asyncio.sleep(0.05)
        [novo] = await ReservaOutboxPostgres(relay, "relay-b", 10, LEASE).reservar()
        assert novo.evento_id == velho.evento_id

        await RegistroPublicacaoPostgres(relay).reagendar(velho, POLITICA.base)

    asyncio.run(cenario())

    _, tentativas, proxima, lease_dono, _ = _estado(livro, [evento_id])[evento_id]
    assert (tentativas, proxima, lease_dono) == (0, None, "relay-b")


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
