"""Privilégios de livro_relay sobre a outbox (003-T1; design 003 §6).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-06

O relay lê a outbox e atualiza só os metadados de publicação: reserva com
lease, marcação de publicado e reagendamento (TD-001; INFRA 2.2, 5.11). Não
altera `payload`, `posicao` nem a identidade do evento, não insere nem apaga
eventos (D-001; INFRA 7.2) e não lê o livro.

`SELECT ... FOR UPDATE` exige UPDATE em ao menos uma coluna, que as colunas
de metadados já dão.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON outbox TO livro_relay")
    op.execute(
        "GRANT UPDATE (publicado_em, tentativas, proxima_tentativa_em,"
        " lease_dono, lease_ate) ON outbox TO livro_relay"
    )


def downgrade() -> None:
    op.execute("REVOKE ALL ON outbox FROM livro_relay")
