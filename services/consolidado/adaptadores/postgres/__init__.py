"""Adaptador PostgreSQL do consolidado: transação da projeção (004-T3),
leitura da geração ativa pela API (004-T5) e barreira dos consumidores
(005-T2)."""

from consolidado.adaptadores.postgres.barreira import ControleBarreiraPostgres
from consolidado.adaptadores.postgres.consulta import LeituraConsolidadoPostgres
from consolidado.adaptadores.postgres.projecao import (
    FabricaConexao,
    TransacaoProjecaoPostgres,
    UnidadeDeTrabalhoProjecaoPostgres,
)

__all__ = [
    "ControleBarreiraPostgres",
    "FabricaConexao",
    "LeituraConsolidadoPostgres",
    "TransacaoProjecaoPostgres",
    "UnidadeDeTrabalhoProjecaoPostgres",
]
