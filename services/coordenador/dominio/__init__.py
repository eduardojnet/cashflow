"""Domínio do coordenador: estados e transições das gerações (005-T1)."""

from coordenador.dominio.erros import (
    ErroDominio,
    ReconstrucaoRecusada,
    TransicaoInvalida,
)
from coordenador.dominio.geracao import (
    IMPEDEM_RECONSTRUCAO,
    RECEPTORAS,
    TRANSICOES,
    EstadoGeracao,
    Geracao,
    transicao_valida,
    verificar_inicio_reconstrucao,
)

__all__ = [
    "IMPEDEM_RECONSTRUCAO",
    "RECEPTORAS",
    "TRANSICOES",
    "ErroDominio",
    "EstadoGeracao",
    "Geracao",
    "ReconstrucaoRecusada",
    "TransicaoInvalida",
    "transicao_valida",
    "verificar_inicio_reconstrucao",
]
