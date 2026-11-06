"""PostgreSQL descartável do consolidado para os testes do worker (INFRA 9.1).

Os testes do worker gravam inbox, contribuições e quarentena, que o papel do
worker não apaga (migração 0001). Por isso não usam o pg-consolidado do
compose, que é a projeção do ambiente, e sim um PostgreSQL descartável com a
mesma imagem do compose, os mesmos papéis
(infra/postgres/consolidado/10-papeis.sh) e as mesmas migrações Alembic
aplicadas por consolidado_migracao.

Mesmo padrão do livro descartável (lancamentos/tests/integracao): porta só em
127.0.0.1, efêmera, senhas aleatórias a cada execução e remoção com os
volumes ao fim.
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
from livro_descartavel import docker
from psycopg.conninfo import make_conninfo

RAIZ = Path(__file__).resolve().parents[4]
COMPOSE = RAIZ / "infra/compose.yaml"
PAPEIS = RAIZ / "infra/postgres/consolidado"
ALEMBIC_INI = RAIZ / "services/consolidado/migracoes/alembic.ini"
BANCO = "consolidado"
PAPEIS_DO_CONSOLIDADO = {
    "postgres": "POSTGRES_PASSWORD",
    "consolidado_migracao": "PG_CONSOLIDADO_SENHA_MIGRACAO",
    "consolidado_worker": "PG_CONSOLIDADO_SENHA_WORKER",
    "consolidado_api": "PG_CONSOLIDADO_SENHA_API",
    "consolidado_operacao": "PG_CONSOLIDADO_SENHA_OPERACAO",
    "consolidado_monitoramento": "PG_CONSOLIDADO_SENHA_MONITORAMENTO",
}


@dataclass(frozen=True)
class Consolidado:
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


def _imagem_do_compose() -> str:
    # A mesma imagem fixada para pg-consolidado (001 TD-007).
    encontrada = re.search(r"image:\s*(postgres:\S+)", COMPOSE.read_text())
    assert encontrada, "imagem do PostgreSQL não encontrada em infra/compose.yaml"
    return encontrada.group(1)


def _aguardar_pronto(banco: Consolidado) -> None:
    # Pronto = aceitar conexão TCP como consolidado_migracao, depois do
    # servidor temporário da criação do volume.
    limite = time.monotonic() + 90
    while time.monotonic() < limite:
        pronto = docker(
            "exec",
            banco.id_conteiner,
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
                    banco.conninfo("consolidado_migracao"), connect_timeout=3
                ):
                    return
            except psycopg.OperationalError:
                pass
        time.sleep(1)
    logs = docker("logs", banco.id_conteiner).stderr[-2000:]
    raise AssertionError(f"PostgreSQL descartável não ficou pronto:\n{logs}")


@contextmanager
def consolidado_descartavel(rotulo: str) -> Iterator[Consolidado]:
    """Sobe, migra e, ao sair, remove um PostgreSQL do consolidado."""
    senhas = {papel: secrets.token_hex(24) for papel in PAPEIS_DO_CONSOLIDADO}
    # Senhas por variável de ambiente, não pela linha de comando.
    env = {
        **os.environ,
        **{
            variavel: senhas[papel]
            for papel, variavel in PAPEIS_DO_CONSOLIDADO.items()
        },
    }
    variaveis = [
        argumento
        for variavel in PAPEIS_DO_CONSOLIDADO.values()
        for argumento in ("-e", variavel)
    ]
    criado = docker(
        "run",
        "-d",
        "--label",
        f"cashflow.teste={rotulo}",
        "-p",
        "127.0.0.1::5432",
        "-e",
        f"POSTGRES_DB={BANCO}",
        *variaveis,
        "-v",
        f"{PAPEIS}:/docker-entrypoint-initdb.d:ro",
        _imagem_do_compose(),
        env=env,
        timeout=300,
    )
    assert criado.returncode == 0, criado.stderr
    id_ = criado.stdout.strip()
    try:
        porta = docker("port", id_, "5432/tcp").stdout.split(":")[-1].strip()
        banco = Consolidado(id_conteiner=id_, porta=int(porta), senhas=senhas)
        _aguardar_pronto(banco)
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
                "PG_CONSOLIDADO_HOST": "127.0.0.1",
                "PG_CONSOLIDADO_PORTA": porta,
                "PG_CONSOLIDADO_SENHA_MIGRACAO": senhas["consolidado_migracao"],
            },
        )
        assert migracao.returncode == 0, migracao.stderr
        yield banco
    finally:
        docker("rm", "-f", "-v", id_)
