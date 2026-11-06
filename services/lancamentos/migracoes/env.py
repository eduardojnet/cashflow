"""Ambiente Alembic do banco do livro (002-T2).

A conexão é montada a partir de variáveis de ambiente, com o papel
livro_migracao por padrão. A senha vem de infra/.env pelo compose e não é
escrita em arquivo nem registrada em log (INFRA 5.9, 8.2).

As migrações usam SQL explícito (ST-Q-005), sem metadados de modelo.
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import URL, create_engine, pool

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _url() -> URL:
    senha = os.environ.get("PG_LIVRO_SENHA_MIGRACAO")
    if not senha:
        raise RuntimeError("defina PG_LIVRO_SENHA_MIGRACAO (ver .env.example)")
    return URL.create(
        "postgresql+psycopg",
        username=os.environ.get("PG_LIVRO_USUARIO_MIGRACAO", "livro_migracao"),
        password=senha,
        host=os.environ.get("PG_LIVRO_HOST", "pg-livro"),
        port=int(os.environ.get("PG_LIVRO_PORTA", "5432")),
        database=os.environ.get("PG_LIVRO_BANCO", "livro"),
    )


def executar_offline() -> None:
    """Gera o SQL sem conectar (`alembic upgrade head --sql`)."""
    context.configure(dialect_name="postgresql", literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def executar_online() -> None:
    # Uma conexão só, sem pool: a migração é um processo de curta duração.
    motor = create_engine(_url(), poolclass=pool.NullPool)
    with motor.connect() as conexao:
        context.configure(connection=conexao)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    executar_offline()
else:
    executar_online()
