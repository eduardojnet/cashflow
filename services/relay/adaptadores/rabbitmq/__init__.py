"""Adaptador RabbitMQ do relay: publicação com confirms (003-T2)."""

from relay.adaptadores.rabbitmq.publicador import (
    CONTENT_TYPE,
    EXCHANGE,
    ROUTING_KEY,
    PublicadorRabbitMQ,
    mensagem_do_evento,
)

__all__ = [
    "CONTENT_TYPE",
    "EXCHANGE",
    "ROUTING_KEY",
    "PublicadorRabbitMQ",
    "mensagem_do_evento",
]
