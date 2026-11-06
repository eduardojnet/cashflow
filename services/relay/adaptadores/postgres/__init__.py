"""Adaptador PostgreSQL do relay sobre a outbox do livro (003-T1 a 003-T3)."""

from relay.adaptadores.postgres.marcacao import RegistroPublicacaoPostgres
from relay.adaptadores.postgres.reserva import FabricaConexao, ReservaOutboxPostgres

# Reexportado para quem já o importava daqui (003-T1); o tipo é da aplicação.
from relay.aplicacao import EventoReservado

__all__ = [
    "EventoReservado",
    "FabricaConexao",
    "RegistroPublicacaoPostgres",
    "ReservaOutboxPostgres",
]
