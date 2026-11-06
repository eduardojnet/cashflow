"""Ciclo de publicação da outbox e suas portas (003-T2, 003-T3; design 003 §3).

Um ciclo reserva um lote com lease, publica cada evento e marca como
publicado somente o que o broker confirmou sem devolver (FR-002; INFRA 2.2,
4.6, 4.7). Qualquer outro desfecho (Return, Nack, confirm fora do prazo ou
falha de conexão e de canal) mantém o evento pendente e o reagenda pela
política de retentativa, liberando o lease (FR-003; D-002). Nenhum evento sai
da outbox (D-001; INFRA 4.10): cada falha vai a log com `evento_id`,
`posicao` e tentativas, sem payload (design 003 §6; INFRA 8.2).

Falha ao marcar ou ao reagendar (banco fora) propaga: o evento continua
pendente, o lease vence e uma reserva seguinte o pega de novo (NFR-003).

Nada aqui conhece banco ou broker: reserva, publicação e registro são portas
implementadas em `relay.adaptadores`.
"""

import enum
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from relay.aplicacao.retentativa import PoliticaRetentativa

log = logging.getLogger("relay.publicacao")


@dataclass(frozen=True)
class EventoReservado:
    """Evento reservado para uma tentativa de publicação."""

    evento_id: uuid.UUID
    posicao: int
    tipo_evento: str
    payload: dict[str, Any]
    tentativas: int
    lease_ate: datetime


class ResultadoPublicacao(enum.Enum):
    """Desfecho de uma publicação com confirms e `mandatory`."""

    # Confirm positivo (Basic.Ack) sem Basic.Return: único que marca.
    CONFIRMADO = "confirmado"
    # Basic.Return: nenhuma fila ligada à routing key (INFRA 4.7).
    DEVOLVIDO = "devolvido"
    # Basic.Nack: o broker não assumiu a mensagem.
    RECUSADO = "recusado"
    # Confirm não chegou no prazo; a mensagem pode ter sido aceita, e a
    # nova tentativa pode duplicar, o que o consumidor deduplica (design §8).
    SEM_CONFIRMACAO = "sem_confirmacao"
    # Exceção do publicador (conexão ou canal fora): não há resposta do
    # broker. Atribuído pelo ciclo, nunca pelo adaptador.
    FALHA = "falha"


class ReservaOutbox(Protocol):
    async def reservar(self) -> list[EventoReservado]: ...


class Publicador(Protocol):
    async def publicar(self, evento: EventoReservado) -> ResultadoPublicacao: ...


class RegistroPublicacao(Protocol):
    async def marcar_publicado(self, evento_id: uuid.UUID) -> None: ...

    async def reagendar(self, evento: EventoReservado, espera: timedelta) -> None:
        """Soma uma tentativa, agenda a próxima após `espera` e libera o lease.

        Só vale para a reserva de `evento`: se o lease venceu e outra execução
        reservou o evento, a reserva dela prevalece.
        """
        ...


@dataclass(frozen=True)
class ResumoCiclo:
    """Contagem de um ciclo, por resultado, para log e métricas (003-T5)."""

    reservados: int
    resultados: dict[ResultadoPublicacao, int]


class PublicarPendentes:
    """Reserva, publica e marca ou reagenda um lote da outbox."""

    def __init__(
        self,
        reserva: ReservaOutbox,
        publicador: Publicador,
        registro: RegistroPublicacao,
        politica: PoliticaRetentativa,
    ) -> None:
        self._reserva = reserva
        self._publicador = publicador
        self._registro = registro
        self._politica = politica

    async def executar(self) -> ResumoCiclo:
        eventos = await self._reserva.reservar()
        resultados = dict.fromkeys(ResultadoPublicacao, 0)
        # Em ordem de posição, um por vez: marcar depende do confirm daquele
        # evento. A ordem de entrega não é garantia (D-003).
        for evento in eventos:
            resultado = await self._publicar(evento)
            resultados[resultado] += 1
            if resultado is ResultadoPublicacao.CONFIRMADO:
                await self._registro.marcar_publicado(evento.evento_id)
            else:
                await self._reagendar(evento, resultado)
        return ResumoCiclo(reservados=len(eventos), resultados=resultados)

    async def _publicar(self, evento: EventoReservado) -> ResultadoPublicacao:
        try:
            return await self._publicador.publicar(evento)
        except Exception as erro:  # noqa: BLE001 - toda falha mantém pendente (FR-003)
            # Só o tipo: a mensagem de erro do driver pode repetir dados.
            log.warning(
                "publicação falhou",
                extra={
                    "evento_id": str(evento.evento_id),
                    "posicao": evento.posicao,
                    "erro_tipo": type(erro).__name__,
                },
            )
            return ResultadoPublicacao.FALHA

    async def _reagendar(
        self, evento: EventoReservado, resultado: ResultadoPublicacao
    ) -> None:
        espera = self._politica.espera(evento.tentativas)
        await self._registro.reagendar(evento, espera)
        log.warning(
            "evento mantido pendente e reagendado",
            extra={
                "evento_id": str(evento.evento_id),
                "posicao": evento.posicao,
                "resultado": resultado.value,
                "tentativas": evento.tentativas + 1,
                "espera_s": espera.total_seconds(),
            },
        )
