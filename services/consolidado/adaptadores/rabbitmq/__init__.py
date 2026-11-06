"""Adaptador RabbitMQ do consolidado: consumo com ACK após commit (004-T3)."""

from consolidado.adaptadores.rabbitmq.consumidor import (
    FILA,
    ConsumidorRabbitMQ,
    MensagemRecebida,
    Observador,
    ProcessadorMensagens,
    Projetar,
    decodificar,
)

__all__ = [
    "FILA",
    "ConsumidorRabbitMQ",
    "MensagemRecebida",
    "Observador",
    "ProcessadorMensagens",
    "Projetar",
    "decodificar",
]
