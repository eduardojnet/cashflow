"""Publicador da outbox no RabbitMQ com confirms e `mandatory` (003-T2).

Cada evento vira uma mensagem persistente (INFRA 4.5) no exchange durável
`cashflow.lancamentos` (INFRA 4.3), com `message_id` igual ao `evento_id`
para a deduplicação no consumidor (INFRA 2.4). O canal usa publisher
confirms (INFRA 4.6) e a publicação usa `mandatory` (INFRA 4.7): só um
Basic.Ack sem Basic.Return conta como publicado (FR-002; TD-002).

A topologia é declarada pelas definições que o RabbitMQ carrega na subida
(infra/rabbitmq/definicoes.modelo.json; design 003 §4). O usuário `relay`
não tem permissão de configuração (INFRA 5.11), então o exchange é usado
sem declaração.

Com `on_return_raises=False`, o aiormq entrega a mensagem devolvida como
resultado do publish, sem exceção; o Ack que o broker manda depois do Return
é descartado pelo aiormq. Nack chega como `DeliveryError`.

Falha de conexão ou de canal propaga: o ciclo de publicação a registra e
reagenda o evento, que continua pendente (003-T3; design 003 §8).
"""

import json
from types import TracebackType
from typing import Self

import aio_pika
import aiormq
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractRobustConnection,
)
from aio_pika.exceptions import DeliveryError
from aiormq.abc import DeliveredMessage

from relay.aplicacao import EventoReservado, ResultadoPublicacao

# Topologia de design 003 §4, a mesma de definicoes.modelo.json.
EXCHANGE = "cashflow.lancamentos"
ROUTING_KEY = "lancamento.registrado.v1"
CONTENT_TYPE = "application/json"


def mensagem_do_evento(evento: EventoReservado) -> aio_pika.Message:
    """Mensagem persistente com o payload v1 gravado na outbox (002 §4)."""
    corpo = json.dumps(evento.payload, ensure_ascii=False, separators=(",", ":"))
    return aio_pika.Message(
        body=corpo.encode("utf-8"),
        content_type=CONTENT_TYPE,
        content_encoding="utf-8",
        message_id=str(evento.evento_id),
        type=evento.tipo_evento,
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
    )


class PublicadorRabbitMQ:
    """Publica eventos reservados com o usuário `relay`.

    Uso: `async with PublicadorRabbitMQ(url) as publicador: ...`. A conexão é
    robusta (reconecta em segundo plano), mas publicação perdida numa queda
    não é reenviada aqui: volta pela outbox.
    """

    def __init__(
        self,
        url: str,
        timeout_confirmacao: float,
        exchange: str = EXCHANGE,
        routing_key: str = ROUTING_KEY,
    ) -> None:
        if timeout_confirmacao <= 0:
            raise ValueError("timeout de confirmação deve ser positivo")
        self._url = url
        self._timeout = timeout_confirmacao
        self._nome_exchange = exchange
        self._routing_key = routing_key
        self._conexao: AbstractRobustConnection | None = None
        self._canal: AbstractChannel | None = None
        self._exchange: AbstractExchange | None = None

    async def abrir(self) -> None:
        self._conexao = await aio_pika.connect_robust(self._url)
        self._canal = await self._conexao.channel(
            publisher_confirms=True, on_return_raises=False
        )
        self._exchange = await self._canal.get_exchange(
            self._nome_exchange, ensure=False
        )

    async def fechar(self) -> None:
        if self._conexao is not None:
            await self._conexao.close()
        self._conexao = self._canal = self._exchange = None

    async def __aenter__(self) -> Self:
        await self.abrir()
        return self

    async def __aexit__(
        self,
        tipo: type[BaseException] | None,
        erro: BaseException | None,
        rastro: TracebackType | None,
    ) -> None:
        await self.fechar()

    async def publicar(self, evento: EventoReservado) -> ResultadoPublicacao:
        if self._exchange is None:
            raise RuntimeError("publicador não aberto")
        try:
            resposta = await self._exchange.publish(
                mensagem_do_evento(evento),
                routing_key=self._routing_key,
                mandatory=True,
                timeout=self._timeout,
            )
        except DeliveryError:
            return ResultadoPublicacao.RECUSADO
        except TimeoutError:
            return ResultadoPublicacao.SEM_CONFIRMACAO
        if isinstance(resposta, aiormq.spec.Basic.Ack):
            return ResultadoPublicacao.CONFIRMADO
        if isinstance(resposta, DeliveredMessage):
            return ResultadoPublicacao.DEVOLVIDO
        # Sem Ack não há publicação a marcar (FR-002).
        return ResultadoPublicacao.SEM_CONFIRMACAO
