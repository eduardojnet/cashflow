"""Adaptador PostgreSQL do consolidado: transação da projeção (004-T3) e
leitura da geração ativa pela API (004-T5)."""

from consolidado.adaptadores.postgres.consulta import LeituraConsolidadoPostgres
from consolidado.adaptadores.postgres.projecao import (
    FabricaConexao,
    TransacaoProjecaoPostgres,
    UnidadeDeTrabalhoProjecaoPostgres,
)

__all__ = [
    "FabricaConexao",
    "LeituraConsolidadoPostgres",
    "TransacaoProjecaoPostgres",
    "UnidadeDeTrabalhoProjecaoPostgres",
]
