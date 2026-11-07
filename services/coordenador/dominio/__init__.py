"""Domínio do coordenador: estados e transições das gerações (005-T1), fases
da reconstrução e critério de aceite da candidata (005-T3), troca e rollback
da geração ativa (005-T4)."""

from coordenador.dominio.criterio import Progresso, pendencias_de_troca
from coordenador.dominio.erros import (
    ErroDominio,
    ReconstrucaoRecusada,
    SemAnterior,
    SemReconstrucao,
    TransicaoInvalida,
    TrocaRecusada,
)
from coordenador.dominio.fase import (
    CODIGO_FASE,
    CODIGO_SEM_RECONSTRUCAO,
    FaseReconstrucao,
)
from coordenador.dominio.geracao import (
    IMPEDEM_RECONSTRUCAO,
    RECEPTORAS,
    TRANSICOES,
    EstadoGeracao,
    Geracao,
    transicao_valida,
    trocar_ativa,
    verificar_inicio_reconstrucao,
)

__all__ = [
    "CODIGO_FASE",
    "CODIGO_SEM_RECONSTRUCAO",
    "IMPEDEM_RECONSTRUCAO",
    "RECEPTORAS",
    "TRANSICOES",
    "ErroDominio",
    "EstadoGeracao",
    "FaseReconstrucao",
    "Geracao",
    "Progresso",
    "ReconstrucaoRecusada",
    "SemAnterior",
    "SemReconstrucao",
    "TransicaoInvalida",
    "TrocaRecusada",
    "pendencias_de_troca",
    "transicao_valida",
    "trocar_ativa",
    "verificar_inicio_reconstrucao",
]
