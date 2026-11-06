"""PostgreSQL descartável do livro para os testes de integração (INFRA 9.1).

O livro é imutável por privilégio (TD-003): lançamento gravado não sai mais.
Por isso os testes que registram não usam o pg-livro do compose, que é o
livro do ambiente, e sim um PostgreSQL descartável com a mesma imagem do
compose, os mesmos papéis (infra/postgres/livro/10-papeis.sh) e as mesmas
migrações Alembic aplicadas por livro_migracao.

O contêiner publica a porta só em 127.0.0.1, numa porta efêmera, usa senhas
aleatórias geradas a cada execução e é removido com seus volumes ao fim.
Extraído de test_unidade_de_trabalho_livro.py (002-T3) para servir também à
API (002-T5).
"""

import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

RAIZ = Path(__file__).resolve().parents[4]
COMPOSE = RAIZ / "infra/compose.yaml"
PAPEIS = RAIZ / "infra/postgres/livro"
ALEMBIC_INI = RAIZ / "services/lancamentos/migracoes/alembic.ini"
BANCO = "livro"
PAPEIS_DO_LIVRO = [
    "postgres",
    "livro_migracao",
    "livro_api",
    "livro_relay",
    "livro_replay",
]


@dataclass(frozen=True)
class Livro:
    id_conteiner: str
    porta: int
    senhas: dict[str, str]

    def conninfo(self, papel: str) -> str:
        return make_conninfo(
            host="127.0.0.1",
            port=self.porta,
            dbname=BANCO,
            user=papel,
            password=self.senhas[papel],
        )


def docker(
    *args: str, env: dict[str, str] | None = None, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _imagem_do_compose() -> str:
    # A mesma imagem fixada para pg-livro (001 TD-007), sem segunda cópia.
    encontrada = re.search(r"image:\s*(postgres:\S+)", COMPOSE.read_text())
    assert encontrada, "imagem do PostgreSQL não encontrada em infra/compose.yaml"
    return encontrada.group(1)


def _aguardar_pronto(livro: Livro) -> None:
    # Durante a criação do volume o entrypoint sobe um servidor temporário só
    # no soquete local; pronto é aceitar conexão TCP como livro_migracao.
    limite = time.monotonic() + 90
    while time.monotonic() < limite:
        pronto = docker(
            "exec",
            livro.id_conteiner,
            "pg_isready",
            "-h",
            "127.0.0.1",
            "-U",
            "postgres",
            "-d",
            BANCO,
        )
        if pronto.returncode == 0:
            try:
                with psycopg.connect(
                    livro.conninfo("livro_migracao"), connect_timeout=3
                ):
                    return
            except psycopg.OperationalError:
                pass
        time.sleep(1)
    logs = docker("logs", livro.id_conteiner).stderr[-2000:]
    raise AssertionError(f"PostgreSQL descartável não ficou pronto:\n{logs}")


@contextmanager
def livro_descartavel(rotulo: str, max_conexoes: int = 100) -> Iterator[Livro]:
    """Sobe, migra e, ao sair, remove um PostgreSQL do livro descartável."""
    senhas = {papel: secrets.token_hex(24) for papel in PAPEIS_DO_LIVRO}
    # As senhas chegam ao docker por variável de ambiente, não pela linha de
    # comando, como em infra/postgres/livro/10-papeis.sh.
    env = {
        **os.environ,
        "POSTGRES_PASSWORD": senhas["postgres"],
        "PG_LIVRO_SENHA_MIGRACAO": senhas["livro_migracao"],
        "PG_LIVRO_SENHA_API": senhas["livro_api"],
        "PG_LIVRO_SENHA_RELAY": senhas["livro_relay"],
        "PG_LIVRO_SENHA_REPLAY": senhas["livro_replay"],
    }
    criado = docker(
        "run",
        "-d",
        "--label",
        f"cashflow.teste={rotulo}",
        "-p",
        "127.0.0.1::5432",
        "-e",
        f"POSTGRES_DB={BANCO}",
        "-e",
        "POSTGRES_PASSWORD",
        "-e",
        "PG_LIVRO_SENHA_MIGRACAO",
        "-e",
        "PG_LIVRO_SENHA_API",
        "-e",
        "PG_LIVRO_SENHA_RELAY",
        "-e",
        "PG_LIVRO_SENHA_REPLAY",
        "-v",
        f"{PAPEIS}:/docker-entrypoint-initdb.d:ro",
        _imagem_do_compose(),
        "-c",
        f"max_connections={max_conexoes}",
        env=env,
        timeout=300,
    )
    assert criado.returncode == 0, criado.stderr
    id_ = criado.stdout.strip()
    try:
        porta = docker("port", id_, "5432/tcp").stdout.split(":")[-1].strip()
        livro = Livro(id_conteiner=id_, porta=int(porta), senhas=senhas)
        _aguardar_pronto(livro)
        migracao = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                str(ALEMBIC_INI),
                "upgrade",
                "head",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env={
                **os.environ,
                "PG_LIVRO_HOST": "127.0.0.1",
                "PG_LIVRO_PORTA": porta,
                "PG_LIVRO_SENHA_MIGRACAO": senhas["livro_migracao"],
            },
        )
        assert migracao.returncode == 0, migracao.stderr
        yield livro
    finally:
        docker("rm", "-f", "-v", id_)
