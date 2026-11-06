"""Registro de lançamentos com consolidado, worker e RabbitMQ parados (002-T6;
FR-005, NFR-001; INFRA 2.1, 9.6).

O cenário roda sobre o compose real (`infra/compose.yaml`) e injeta a falha
com `docker compose stop` (ST-Q-010): fica no ar só o caminho do registro
(pg-livro, Keycloak e lancamentos-api, design 002 §5); todo o resto, que hoje
inclui RabbitMQ e pg-consolidado e, quando existirem, worker e consolidado,
é parado. Com isso, toda requisição válida deve ser confirmada com 201 e cada
lançamento deve deixar o seu evento pendente na outbox, à espera do relay.

O livro é imutável por privilégio (TD-003): lançamento gravado não sai mais.
Por isso o teste não usa o projeto `cashflow` do ambiente, e sim um projeto
compose próprio (`cashflow-e2e`), com volumes próprios, portas efêmeras em
127.0.0.1 e as mesmas imagens, migrações e segredos de `infra/.env`. Ao fim,
o projeto é removido com volumes e imagens locais (`down -v --rmi local`).

A partir da raiz do repositório, com `infra/.env` preenchido e os
certificados gerados (001-T3):

    uv run --env-file infra/.env pytest tests/e2e/test_independencia_lancamentos.py

Sem docker no PATH ou sem o segredo do cliente técnico, o módulo é pulado
com o motivo.
"""

import json
import os
import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

RAIZ = Path(__file__).resolve().parents[2]
COMPOSE = RAIZ / "infra/compose.yaml"
AC = RAIZ / "infra/tls/gerados/ac/ac-cashflow.crt"
PROJETO = "cashflow-e2e"
REALM = "cashflow"
SEGREDO = "KEYCLOAK_SEGREDO_CLIENTE_LANCAMENTOS"

# Caminho do registro (design 002 §5): o que a API usa e fica no ar.
CAMINHO_DO_REGISTRO = {"pg-livro", "keycloak", "lancamentos-api"}
# Precisam estar entre os parados; worker e consolidado entram pelo
# complemento de CAMINHO_DO_REGISTRO quando forem acrescentados ao compose.
OBRIGATORIOS_PARADOS = {"rabbitmq", "pg-consolidado"}

REQUISICOES = 60
PARALELISMO = 6

pytestmark = [
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker ausente"),
    pytest.mark.skipif(
        SEGREDO not in os.environ,
        reason=f"{SEGREDO} ausente: executar com --env-file infra/.env (ver docstring)",
    ),
]


def _porta_livre() -> int:
    with socket.socket() as soquete:
        soquete.bind(("127.0.0.1", 0))
        porta: int = soquete.getsockname()[1]
        return porta


@dataclass(frozen=True)
class Ambiente:
    porta_keycloak: int
    porta_api: int

    def compose(
        self, *args: str, timeout: int = 120
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "KEYCLOAK_PORTA_HOST": str(self.porta_keycloak),
            "LANCAMENTOS_API_PORTA_HOST": str(self.porta_api),
        }
        return subprocess.run(
            ["docker", "compose", "-p", PROJETO, "-f", str(COMPOSE), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

    def exigir(self, *args: str, timeout: int = 120) -> str:
        resultado = self.compose(*args, timeout=timeout)
        assert resultado.returncode == 0, (
            f"docker compose {' '.join(args)}:\n{resultado.stderr[-3000:]}"
        )
        return resultado.stdout

    def servicos(self, *filtros: str) -> set[str]:
        return set(self.exigir("ps", *filtros, "--services").split())

    def sql(self, consulta: str) -> list[list[str]]:
        # Soquete local do contêiner, como superusuário: só leitura de
        # conferência; livro_api não lê a outbox (002-T2).
        saida = self.exigir(
            "exec",
            "-T",
            "pg-livro",
            "psql",
            "-U",
            "postgres",
            "-d",
            "livro",
            "-X",
            "-A",
            "-t",
            "-F",
            "|",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            consulta,
        )
        return [linha.split("|") for linha in saida.splitlines() if linha]

    def contexto_tls(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(AC))

    def token(self) -> str:
        campos = {
            "grant_type": "client_credentials",
            "client_id": "cliente-lancamentos",
            "client_secret": os.environ[SEGREDO],
        }
        requisicao = urllib.request.Request(
            f"https://localhost:{self.porta_keycloak}/realms/{REALM}"
            "/protocol/openid-connect/token",
            data=urllib.parse.urlencode(campos).encode(),
            method="POST",
        )
        with urllib.request.urlopen(
            requisicao, context=self.contexto_tls(), timeout=10
        ) as resposta:
            token: str = json.load(resposta)["access_token"]
            return token

    def api(
        self, metodo: str, caminho: str, corpo: object = None, **cabecalhos: str
    ) -> tuple[int, Any]:
        dados = None if corpo is None else json.dumps(corpo).encode()
        if dados is not None:
            cabecalhos["Content-Type"] = "application/json"
        requisicao = urllib.request.Request(
            f"https://localhost:{self.porta_api}{caminho}",
            data=dados,
            method=metodo,
            headers=cabecalhos,
        )
        try:
            with urllib.request.urlopen(
                requisicao, context=self.contexto_tls(), timeout=30
            ) as resposta:
                return resposta.status, json.load(resposta)
        except urllib.error.HTTPError as erro:
            return erro.code, json.loads(erro.read() or b"null")


@pytest.fixture(scope="module")
def ambiente() -> Iterator[Ambiente]:
    ambiente = Ambiente(porta_keycloak=_porta_livre(), porta_api=_porta_livre())
    try:
        # Banco primeiro, migração do livro com livro_migracao (002-T2) e só
        # então o restante, aguardando todos os health checks.
        ambiente.exigir("up", "-d", "--wait", "pg-livro", timeout=300)
        ambiente.exigir("run", "--rm", "--build", "livro-migracao", timeout=600)
        ambiente.exigir("up", "-d", "--build", "--wait", timeout=900)
        yield ambiente
    finally:
        ambiente.compose(
            "--profile",
            "migracao",
            "down",
            "-v",
            "--rmi",
            "local",
            "--remove-orphans",
            timeout=300,
        )


def _registrar(ambiente: Ambiente, token: str, indice: int) -> tuple[int, Any]:
    corpo = {
        "tipo": "CREDITO" if indice % 2 else "DEBITO",
        "valor": f"{indice + 1}.25",
        "data_lancamento": "2026-11-06",
        "descricao": f"independencia {indice}",
    }
    return ambiente.api(
        "POST",
        "/v1/lancamentos",
        corpo,
        Authorization=f"Bearer {token}",
        **{"Idempotency-Key": f"e2e-independencia-{uuid.uuid4()}"},
    )


def _aguardar_ready(ambiente: Ambiente, limite_segundos: float = 60) -> tuple[int, Any]:
    fim = time.monotonic() + limite_segundos
    while True:
        resultado = ambiente.api("GET", "/health/ready")
        if resultado[0] == 200 or time.monotonic() > fim:
            return resultado
        time.sleep(1)


def test_registro_confirma_com_consolidado_worker_e_rabbitmq_parados(
    ambiente: Ambiente,
) -> None:
    todos = ambiente.servicos()
    assert OBRIGATORIOS_PARADOS <= todos
    fora_do_caminho = sorted(todos - CAMINHO_DO_REGISTRO)

    # Falha injetada pelo próprio Docker (ST-Q-010).
    ambiente.exigir("stop", *fora_do_caminho, timeout=300)

    assert ambiente.servicos("--status", "running") == CAMINHO_DO_REGISTRO
    assert not ambiente.servicos("--status", "running") & OBRIGATORIOS_PARADOS
    pronto = _aguardar_ready(ambiente)
    assert pronto[0] == 200, pronto

    token = ambiente.token()
    with ThreadPoolExecutor(max_workers=PARALELISMO) as executor:
        respostas = list(
            executor.map(
                lambda indice: _registrar(ambiente, token, indice),
                range(REQUISICOES),
            )
        )

    # 100% das requisições válidas confirmadas.
    status = [codigo for codigo, _ in respostas]
    assert status == [201] * REQUISICOES, status
    confirmados = {(corpo["id"], str(corpo["posicao"])) for _, corpo in respostas}
    assert len(confirmados) == REQUISICOES

    # Cada confirmação deixou o seu evento pendente na outbox, sem publicação.
    pendentes = ambiente.sql(
        "SELECT lancamento_id, posicao FROM outbox WHERE publicado_em IS NULL"
    )
    assert {(id_, posicao) for id_, posicao in pendentes} == confirmados
    assert ambiente.sql(
        "SELECT count(*) FROM outbox WHERE publicado_em IS NOT NULL"
    ) == [["0"]]
    assert ambiente.sql("SELECT count(*) FROM lancamento") == [[str(REQUISICOES)]]

    # As dependências continuavam paradas durante todo o cenário.
    assert ambiente.servicos("--status", "running") == CAMINHO_DO_REGISTRO
