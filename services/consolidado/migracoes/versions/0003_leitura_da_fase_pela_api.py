"""Leitura da fase da reconstrução pela API do consolidado (005-T3).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-06

O coordenador roda como CLI e não é alvo do Prometheus: grava a fase em
`fase_reconstrucao` e a API do consolidado a expõe em
`cashflow_reconstrucao_fase` (007 design §4; INFRA 8.3). Para isso,
consolidado_api passa a ler `fase_reconstrucao`: só leitura, sem escrita e
sem acesso a `estado_consumidor`. A tabela guarda geração, nome da fase e
instante, nenhum dado financeiro (INFRA 8.2).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON fase_reconstrucao TO consolidado_api")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON fase_reconstrucao FROM consolidado_api")
