"""Adaptador PostgreSQL do consolidado: transação da projeção (004-T3)."""

from consolidado.adaptadores.postgres.projecao import (
    FabricaConexao,
    TransacaoProjecaoPostgres,
    UnidadeDeTrabalhoProjecaoPostgres,
)

__all__ = [
    "FabricaConexao",
    "TransacaoProjecaoPostgres",
    "UnidadeDeTrabalhoProjecaoPostgres",
]
