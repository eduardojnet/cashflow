"""Adaptador HTTP da API de lançamentos (002-T5).

`criar_app` monta as rotas sobre as portas da aplicação; a composição com
PostgreSQL, Keycloak e métricas reais fica em `principal`, e o processo
HTTPS em `__main__`.
"""

from lancamentos.adaptadores.http.app import (
    CABECALHO_CORRELACAO,
    COMPONENTE,
    Prontidao,
    criar_app,
)
from lancamentos.adaptadores.http.logs import (
    REDIGIDO,
    FormatadorJson,
    configuracao_logging,
    redigir_texto,
)

__all__ = [
    "CABECALHO_CORRELACAO",
    "COMPONENTE",
    "REDIGIDO",
    "FormatadorJson",
    "Prontidao",
    "configuracao_logging",
    "criar_app",
    "redigir_texto",
]
