"""Domínio do coordenador: estados e transições das gerações (005-T1), fases
da reconstrução e critério de aceite da candidata (005-T3), troca e rollback
da geração ativa (005-T4), reconciliação com o livro (005-T5)."""

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
from coordenador.dominio.reconciliacao import (
    Divergencia,
    IntervaloFaltante,
    Reconciliacao,
    RelatorioInvalido,
    RelatorioLivro,
    SaldoInconsistente,
    SaldoMaterializado,
    TipoDivergencia,
    TotalDoDia,
    comparar_saldo_materializado,
    comparar_totais,
)

__all__ = [
    "CODIGO_FASE",
    "CODIGO_SEM_RECONSTRUCAO",
    "IMPEDEM_RECONSTRUCAO",
    "RECEPTORAS",
    "TRANSICOES",
    "Divergencia",
    "ErroDominio",
    "EstadoGeracao",
    "FaseReconstrucao",
    "Geracao",
    "IntervaloFaltante",
    "Progresso",
    "Reconciliacao",
    "ReconstrucaoRecusada",
    "RelatorioInvalido",
    "RelatorioLivro",
    "SaldoInconsistente",
    "SaldoMaterializado",
    "SemAnterior",
    "SemReconstrucao",
    "TipoDivergencia",
    "TotalDoDia",
    "TransicaoInvalida",
    "TrocaRecusada",
    "comparar_saldo_materializado",
    "comparar_totais",
    "pendencias_de_troca",
    "transicao_valida",
    "trocar_ativa",
    "verificar_inicio_reconstrucao",
]
