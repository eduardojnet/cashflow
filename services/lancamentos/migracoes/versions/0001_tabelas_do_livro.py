"""Tabelas do livro e privilégios de livro_api (002-T2; design 002 §4).

Revision ID: 0001
Revises:
Create Date: 2026-11-06

Cria `lancamento`, `contador_posicao`, `resposta_idempotente` e `outbox`, com
dono livro_migracao, e concede a livro_api só o que a transação de registro
usa (TD-001, TD-002, TD-004). `lancamento` fica imutável por privilégio:
livro_api tem SELECT e INSERT, sem UPDATE, DELETE nem TRUNCATE (FR-003,
TD-003; INFRA 3.1, 5.11).

Os privilégios de livro_relay e livro_replay sobre a outbox e o livro são
concedidos pelas migrações das specs que usam esses papéis (003, 005).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Livro imutável. `posicao` é atribuída pelo contador na mesma transação
    # (TD-002) e não por sequence, que deixaria lacunas em rollback. O teto de
    # `valor` é o mesmo de VALOR_MAXIMO no domínio. `data_lancamento` é o dia
    # informado, em America/Sao_Paulo, sem fuso na coluna (FR-010, D-005).
    # UNIQUE (id, posicao) existe para a chave estrangeira composta da outbox.
    op.execute(
        """
        CREATE TABLE lancamento (
            id              uuid           PRIMARY KEY,
            posicao         bigint         NOT NULL UNIQUE CHECK (posicao > 0),
            tipo            text           NOT NULL
                                           CHECK (tipo IN ('DEBITO', 'CREDITO')),
            valor           numeric(14, 2) NOT NULL CHECK (valor > 0),
            data_lancamento date           NOT NULL,
            descricao       text,
            cliente_id      text           NOT NULL CHECK (cliente_id <> ''),
            registrado_em   timestamptz    NOT NULL DEFAULT now(),
            UNIQUE (id, posicao)
        )
        """
    )
    # Listagem por intervalo com cursor (data_lancamento, posicao) (TD-006).
    op.execute(
        "CREATE INDEX ix_lancamento_data_posicao"
        " ON lancamento (data_lancamento, posicao)"
    )

    # Linha única, travada e incrementada na transação de registro (TD-002).
    # `valor` é a última posição atribuída; o primeiro lançamento recebe 1.
    op.execute(
        """
        CREATE TABLE contador_posicao (
            id    smallint PRIMARY KEY CHECK (id = 1),
            valor bigint   NOT NULL CHECK (valor >= 0)
        )
        """
    )
    op.execute("INSERT INTO contador_posicao (id, valor) VALUES (1, 0)")

    # Resposta original por chave do cliente; o hash detecta repetição da
    # chave com conteúdo diferente (TD-004, D-006; INFRA 3.1).
    op.execute(
        """
        CREATE TABLE resposta_idempotente (
            cliente_id      text        NOT NULL CHECK (cliente_id <> ''),
            chave           text        NOT NULL CHECK (chave <> ''),
            hash_requisicao text        NOT NULL,
            status_http     smallint    NOT NULL
                                        CHECK (status_http BETWEEN 100 AND 599),
            corpo           jsonb       NOT NULL,
            criado_em       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (cliente_id, chave)
        )
        """
    )

    # Um evento por lançamento, com a mesma posição do livro, preservado para
    # replay (TD-001; INFRA 3.1, 7.2). Os metadados de publicação são do relay
    # (003). A chave estrangeira composta impede evento com posição diferente
    # da do lançamento.
    op.execute(
        """
        CREATE TABLE outbox (
            evento_id            uuid        PRIMARY KEY,
            lancamento_id        uuid        NOT NULL UNIQUE,
            posicao              bigint      NOT NULL UNIQUE,
            tipo_evento          text        NOT NULL CHECK (tipo_evento <> ''),
            payload              jsonb       NOT NULL,
            criado_em            timestamptz NOT NULL DEFAULT now(),
            publicado_em         timestamptz,
            tentativas           integer     NOT NULL DEFAULT 0
                                             CHECK (tentativas >= 0),
            proxima_tentativa_em timestamptz,
            lease_dono           text,
            lease_ate            timestamptz,
            FOREIGN KEY (lancamento_id, posicao)
                REFERENCES lancamento (id, posicao)
        )
        """
    )
    # Eventos pendentes em ordem de posição, para a reserva do relay (003).
    op.execute(
        "CREATE INDEX ix_outbox_pendente ON outbox (posicao) WHERE publicado_em IS NULL"
    )

    # Privilégio mínimo (TD-003; INFRA 5.11). Nada para PUBLIC; livro_api
    # recebe só o que a transação de registro e a listagem usam. O contador é
    # atualizado só na coluna `valor`; a outbox é só escrita pela API.
    op.execute(
        "REVOKE ALL ON lancamento, contador_posicao, resposta_idempotente, outbox"
        " FROM PUBLIC"
    )
    op.execute("GRANT SELECT, INSERT ON lancamento TO livro_api")
    op.execute("GRANT SELECT, UPDATE (valor) ON contador_posicao TO livro_api")
    op.execute("GRANT SELECT, INSERT ON resposta_idempotente TO livro_api")
    op.execute("GRANT INSERT ON outbox TO livro_api")


def downgrade() -> None:
    # Desfazer apagaria o livro, que é a fonte da verdade financeira
    # (INFRA 3.1). Rollback de migração não se aplica à entrega inicial
    # (design 002 §13).
    raise NotImplementedError("a migração inicial do livro não tem downgrade")
