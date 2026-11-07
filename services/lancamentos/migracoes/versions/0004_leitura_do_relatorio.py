"""Leitura do livro por livro_replay para o relatório de totais (005-T5).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-07

A reconciliação compara o consolidado com totais por data exportados no
lado do livro, até uma posição P (005 D-002; design 005 §4, Reconciliação).
O comando `lancamentos.comandos.relatorio` roda com a identidade dedicada
livro_replay (005 FR-008; INFRA 5.11, 6.4), que recebe só a leitura das
colunas que os totais usam: posição, tipo, valor e data. Não lê descrição,
cliente nem instante de registro, e continua sem INSERT, UPDATE ou DELETE no
livro (INFRA 3.1).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "GRANT SELECT (posicao, tipo, valor, data_lancamento) ON lancamento"
        " TO livro_replay"
    )


def downgrade() -> None:
    op.execute(
        "REVOKE SELECT (posicao, tipo, valor, data_lancamento) ON lancamento"
        " FROM livro_replay"
    )
