"""Privilégios de livro_replay e reabertura restrita da outbox (003-T4).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-06

A republicação reabre eventos da outbox a partir de uma posição:
`publicado_em = NULL` e `tentativas = 0` (TD-004; design 003 §4; INFRA 6.4,
7.6). livro_replay recebe só isso: ler `posicao` para o filtro e atualizar
as duas colunas. Não insere, não apaga e não altera payload nem lease.

livro_relay já atualiza `publicado_em` para marcar (0002), e privilégio de
coluna não distingue valores. O gatilho garante no banco que voltar
`publicado_em` de preenchido para nulo só acontece como livro_replay
(design 003 §6), inclusive para o dono da tabela.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("GRANT SELECT (posicao) ON outbox TO livro_replay")
    op.execute("GRANT UPDATE (publicado_em, tentativas) ON outbox TO livro_replay")

    # `current_user` é o papel da sessão: a função não é SECURITY DEFINER.
    op.execute(
        """
        CREATE FUNCTION outbox_reabertura_so_por_replay() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF current_user <> 'livro_replay' THEN
                RAISE EXCEPTION 'reabrir evento publicado exige o papel livro_replay'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER outbox_reabertura
        BEFORE UPDATE OF publicado_em ON outbox
        FOR EACH ROW
        WHEN (OLD.publicado_em IS NOT NULL AND NEW.publicado_em IS NULL)
        EXECUTE FUNCTION outbox_reabertura_so_por_replay()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER outbox_reabertura ON outbox")
    op.execute("DROP FUNCTION outbox_reabertura_so_por_replay()")
    op.execute("REVOKE ALL ON outbox FROM livro_replay")
