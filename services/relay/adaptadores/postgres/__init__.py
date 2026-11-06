"""Adaptador PostgreSQL do relay sobre a outbox do livro (003-T1 a 003-T5)."""

from relay.adaptadores.postgres.marcacao import RegistroPublicacaoPostgres
from relay.adaptadores.postgres.medicao import EstadoOutbox, MedicaoOutboxPostgres
from relay.adaptadores.postgres.reserva import FabricaConexao, ReservaOutboxPostgres

# Reexportado para quem já o importava daqui (003-T1); o tipo é da aplicação.
from relay.aplicacao import EventoReservado

__all__ = [
    "EstadoOutbox",
    "EventoReservado",
    "FabricaConexao",
    "MedicaoOutboxPostgres",
    "RegistroPublicacaoPostgres",
    "ReservaOutboxPostgres",
]
