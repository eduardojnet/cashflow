"""Ciclo de publicação da outbox e suas portas (003-T2; design 003 §3).

Um ciclo reserva um lote com lease, publica cada evento e marca como
publicado somente o que o broker confirmou sem devolver (FR-002; INFRA 2.2,
4.6, 4.7). Qualquer outro resultado deixa o evento pendente: o lease vence e
uma reserva seguinte o pega de novo. A política de retentativa com espera
crescente (D-002) é de 003-T3.

Nada aqui conhece banco ou broker: reserva, publicação e marcação são portas
implementadas em `relay.adaptadores`.
"""

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


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
    """Resposta do broker a uma publicação com confirms e `mandatory`."""

    # Confirm positivo (Basic.Ack) sem Basic.Return: único que marca.
    CONFIRMADO = "confirmado"
    # Basic.Return: nenhuma fila ligada à routing key (INFRA 4.7).
    DEVOLVIDO = "devolvido"
    # Basic.Nack: o broker não assumiu a mensagem.
    RECUSADO = "recusado"
    # Confirm não chegou no prazo; a mensagem pode ter sido aceita, e a
    # nova tentativa pode duplicar, o que o consumidor deduplica (design §8).
    SEM_CONFIRMACAO = "sem_confirmacao"


class ReservaOutbox(Protocol):
    async def reservar(self) -> list[EventoReservado]: ...


class Publicador(Protocol):
    async def publicar(self, evento: EventoReservado) -> ResultadoPublicacao: ...


class RegistroPublicacao(Protocol):
    async def marcar_publicado(self, evento_id: uuid.UUID) -> None: ...


@dataclass(frozen=True)
class ResumoCiclo:
    """Contagem de um ciclo, por resultado, para log e métricas (003-T5)."""

    reservados: int
    resultados: dict[ResultadoPublicacao, int]


class PublicarPendentes:
    """Reserva, publica e marca um lote da outbox."""

    def __init__(
        self,
        reserva: ReservaOutbox,
        publicador: Publicador,
        registro: RegistroPublicacao,
    ) -> None:
        self._reserva = reserva
        self._publicador = publicador
        self._registro = registro

    async def executar(self) -> ResumoCiclo:
        eventos = await self._reserva.reservar()
        resultados = dict.fromkeys(ResultadoPublicacao, 0)
        # Em ordem de posição, um por vez: marcar depende do confirm daquele
        # evento. A ordem de entrega não é garantia (D-003).
        for evento in eventos:
            resultado = await self._publicador.publicar(evento)
            resultados[resultado] += 1
            if resultado is ResultadoPublicacao.CONFIRMADO:
                await self._registro.marcar_publicado(evento.evento_id)
        return ResumoCiclo(reservados=len(eventos), resultados=resultados)
