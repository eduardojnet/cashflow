"""Adaptador de segurança da API do consolidado (004-T5).

Usa o pacote `seguranca` (DQ-002) com a audiência `consolidado-api`, que o
realm emite a partir do escopo `consolidado:consultar` (001 TD-008), e
exige esse escopo em todas as consultas (FR-008; INFRA 5.4, 5.5). A
configuração vem de `JWT_*` no ambiente (ver `ConfiguracaoJwt.de_ambiente`).
"""

from consolidado.adaptadores.seguranca.validacao import (
    AUDIENCIA,
    ESCOPO_CONSULTAR,
    configuracao_de_ambiente,
    criar_validador_consolidado,
)

__all__ = [
    "AUDIENCIA",
    "ESCOPO_CONSULTAR",
    "configuracao_de_ambiente",
    "criar_validador_consolidado",
]
