"""Adaptador HTTP da API do consolidado (004-T5).

`criar_app` monta as rotas sobre os casos de uso de consulta; a composição
com PostgreSQL, Keycloak e métricas reais fica em `principal`, e o processo
HTTPS em `__main__`.
"""

from consolidado.adaptadores.http.app import (
    CABECALHO_CORRELACAO,
    COMPONENTE,
    Prontidao,
    criar_app,
)

__all__ = ["CABECALHO_CORRELACAO", "COMPONENTE", "Prontidao", "criar_app"]
