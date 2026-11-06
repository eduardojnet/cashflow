"""Adaptador de segurança da API de lançamentos (002-T4).

Usa o pacote `seguranca` com a audiência `lancamentos-api` (002 design §17,
DQ-003) e os escopos das rotas (FR-008; INFRA 5.4; D-002). A configuração
vem de `JWT_*` no ambiente (ver `ConfiguracaoJwt.de_ambiente`).
"""

from lancamentos.adaptadores.seguranca.validacao import (
    AUDIENCIA,
    ESCOPO_CONSULTAR,
    ESCOPO_REGISTRAR,
    configuracao_de_ambiente,
    criar_validador_lancamentos,
)

__all__ = [
    "AUDIENCIA",
    "ESCOPO_CONSULTAR",
    "ESCOPO_REGISTRAR",
    "configuracao_de_ambiente",
    "criar_validador_lancamentos",
]
