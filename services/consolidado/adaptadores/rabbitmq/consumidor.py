"""Consumidor da fila do consolidado com ACK manual após commit (004-T3).

O worker lê a fila durável `consolidado.lancamentos` (003 design §4) com o
usuário `worker`, que só tem permissão de leitura nela (INFRA 5.11): a fila é
usada sem declaração, como o exchange no relay.

Cada mensagem passa por `ProcessadorMensagens`, uma por vez, na ordem de
entrega: decodifica o corpo, executa a transação da projeção e só então
envia o ACK (FR-002; TD-002; INFRA 4.8). Exceção na transação ou no ACK
propaga sem confirmar; o laço do worker fecha o canal e o broker devolve à
fila tudo o que não foi confirmado (design 004 §5, §8). O ACK perdido depois
do commit vira reentrega, que a inbox reconhece como duplicata (TD-001).

`prefetch` limita quantas mensagens não confirmadas o canal recebe (DQ-003).

A conexão não é robusta de propósito: na queda, o iterador termina, o laço
reabre conexão e canal e só retoma o consumo com o banco pronto. Mensagem
recebida num canal que caiu não pode ser confirmada em outro.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractConnection,
    AbstractIncomingMessage,
    AbstractQueue,
    AbstractQueueIterator,
)

from consolidado.aplicacao import Desfecho, MensagemEvento

# Topologia de design 003 §4, a mesma de infra/rabbitmq/definicoes.modelo.json.
FILA = "consolidado.lancamentos"

log = logging.getLogger("consolidado.worker.mensagens")


class MensagemRecebida(Protocol):
    """O que o processador usa de `AbstractIncomingMessage`."""

    @property
    def body(self) -> bytes: ...

    @property
    def message_id(self) -> str | None: ...

    @property
    def redelivered(self) -> bool | None: ...

    async def ack(self, multiple: bool = False) -> None: ...


class Projetar(Protocol):
    async def executar(self, mensagem: MensagemEvento) -> Desfecho: ...


class Observador(Protocol):
    """Métricas do worker (design 004 §12)."""

    def reentrega(self) -> None: ...

    def desfecho(self, desfecho: Desfecho) -> None: ...


def decodificar(corpo: bytes, identificador: str | None) -> MensagemEvento:
    """Lê o corpo como objeto JSON em UTF-8; qualquer outra coisa é ilegível."""
    try:
        dados = json.loads(corpo.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        dados = None
    if not isinstance(dados, Mapping):
        dados = None
    return MensagemEvento(corpo=corpo, dados=dados, identificador=identificador)


class ProcessadorMensagens:
    def __init__(self, projetar: Projetar, observador: Observador) -> None:
        self._projetar = projetar
        self._observador = observador

    async def processar(self, mensagem: MensagemRecebida) -> Desfecho:
        if mensagem.redelivered:
            self._observador.reentrega()
        desfecho = await self._projetar.executar(
            decodificar(mensagem.body, mensagem.message_id)
        )
        # A transação já terminou em COMMIT (ou ROLLBACK da duplicata): só
        # agora a mensagem pode sair da fila (FR-002).
        self._observador.desfecho(desfecho)
        await mensagem.ack()
        # Só identificação e desfecho, nunca o corpo (INFRA 8.2).
        nivel = logging.WARNING if desfecho.quarentena else logging.INFO
        log.log(
            nivel,
            "mensagem processada",
            extra={
                "evento_id": mensagem.message_id,
                "desfecho": desfecho.value,
                "reentrega": bool(mensagem.redelivered),
            },
        )
        return desfecho


Tratar = Callable[[AbstractIncomingMessage], Awaitable[object]]


class ConsumidorRabbitMQ:
    """Conexão, canal com `prefetch` e consumo sequencial da fila."""

    def __init__(
        self,
        url: str,
        prefetch: int,
        fila: str = FILA,
        timeout_conexao_segundos: float = 5.0,
    ) -> None:
        if prefetch <= 0:
            raise ValueError("prefetch deve ser positivo")
        self._url = url
        self._prefetch = prefetch
        self._nome_fila = fila
        self._timeout = timeout_conexao_segundos
        self._conexao: AbstractConnection | None = None
        self._canal: AbstractChannel | None = None
        self._fila: AbstractQueue | None = None

    async def abrir(self) -> None:
        self._conexao = await aio_pika.connect(self._url, timeout=self._timeout)
        self._canal = await self._conexao.channel()
        await self._canal.set_qos(prefetch_count=self._prefetch)
        self._fila = await self._canal.get_queue(self._nome_fila, ensure=False)

    async def fechar(self) -> None:
        """Fecha a conexão; o broker devolve à fila o que não foi confirmado."""
        conexao = self._conexao
        self._conexao = self._canal = self._fila = None
        if conexao is not None and not conexao.is_closed:
            await conexao.close()

    def conectado(self) -> bool:
        """Conexão e canal abertos agora (ready; INFRA 8.5)."""
        return (
            self._conexao is not None
            and self._canal is not None
            and not self._conexao.is_closed
            and not self._canal.is_closed
        )

    async def consumir(self, tratar: Tratar, parar: asyncio.Event) -> None:
        """Entrega cada mensagem a `tratar`, em sequência.

        Retorna quando `parar` é sinalizado (a mensagem em curso termina) ou
        quando o canal fecha. Exceção de `tratar` propaga sem ACK.
        """
        if self._fila is None:
            raise RuntimeError("consumidor não aberto")
        async with self._fila.iterator() as iterador:
            vigia = asyncio.create_task(_fechar_ao_parar(parar, iterador))
            try:
                async for mensagem in iterador:
                    await tratar(mensagem)
            finally:
                vigia.cancel()


async def _fechar_ao_parar(
    parar: asyncio.Event, iterador: AbstractQueueIterator
) -> None:
    # Cancela o consumo (Basic.Cancel): o iterador termina depois da mensagem
    # em curso, e as já recebidas sem ACK voltam à fila ao fechar o canal.
    await parar.wait()
    await iterador.close()
