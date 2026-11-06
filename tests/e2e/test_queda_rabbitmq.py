"""Queda e perda de dados do RabbitMQ com o relay em execução (003-T5; FR-005,
FR-006, NFR-001; INFRA 4.10, 7.6, 9.6).

O cenário roda sobre o compose real (`infra/compose.yaml`) e injeta as falhas
com o próprio Docker (ST-Q-010):

1. `relay` fica `healthy` no `up` e só alcança pg-livro e RabbitMQ, nunca
   pg-consolidado (design 003 §12; INFRA 6.7).
2. RabbitMQ parado (`docker compose stop`): lançamentos continuam sendo
   registrados, os eventos ficam pendentes na outbox, o ready do relay fica
   falso e as métricas mostram backlog e falhas. RabbitMQ retomado: o
   backlog zera e cada evento chega à fila.
3. Perda do volume do RabbitMQ (contêiner e volume removidos): o broker
   volta vazio, só com a topologia das definições. A republicação pelo
   serviço `relay-republicar` (papel livro_replay) reabre a outbox desde a
   posição 1 e o relay publica de novo todos os eventos (INFRA 7.6).

A fila é conferida pelo `message_id` de cada mensagem, consumida com o
usuário `worker` num contêiner avulso da imagem do relay na rede da
mensageria: o RabbitMQ não é publicado no host (INFRA 6.3). O serviço
`worker` do consolidado (004-T3) não sobe neste projeto, para não consumir
as mensagens conferidas.

Como em test_independencia_lancamentos.py, o teste usa um projeto compose
próprio (`cashflow-e2e-rabbitmq`), com volumes próprios, portas efêmeras em
127.0.0.1 e espera de retentativa curta, removido ao fim com volumes e
imagens locais. A partir da raiz do repositório, com `infra/.env` preenchido
e os certificados gerados (001-T3):

    uv run --env-file infra/.env pytest tests/e2e/test_queda_rabbitmq.py

Sem docker no PATH ou sem os segredos usados, o módulo é pulado com o motivo.
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
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

RAIZ = Path(__file__).resolve().parents[2]
COMPOSE = RAIZ / "infra/compose.yaml"
AC = RAIZ / "infra/tls/gerados/ac/ac-cashflow.crt"
PROJETO = "cashflow-e2e-rabbitmq"
VOLUME_RABBITMQ = f"{PROJETO}_vol-rabbitmq"
REALM = "cashflow"
FILA = "consolidado.lancamentos"
SEGREDO = "KEYCLOAK_SEGREDO_CLIENTE_LANCAMENTOS"
SENHA_WORKER = "RABBITMQ_SENHA_WORKER"

# Espera de retentativa curta (D-002 com teto configurável; DQ-002), para o
# backlog escoar em segundos depois do retorno do RabbitMQ.
AMBIENTE_RELAY = {
    "RELAY_INTERVALO_SEGUNDOS": "0.5",
    "RELAY_ESPERA_BASE_SEGUNDOS": "0.5",
    "RELAY_ESPERA_TETO_SEGUNDOS": "2",
}

# Tudo menos o worker do consolidado, que esvaziaria a fila conferida aqui.
SERVICOS = [
    "pg-livro",
    "pg-consolidado",
    "rabbitmq",
    "keycloak",
    "lancamentos-api",
    "relay",
]

LANCAMENTOS_NA_QUEDA = 20
LANCAMENTOS_ANTES_DA_PERDA = 10

pytestmark = [
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker ausente"),
    pytest.mark.skipif(
        not {SEGREDO, SENHA_WORKER} <= set(os.environ),
        reason="segredos ausentes: executar com --env-file infra/.env (ver docstring)",
    ),
]

# Executado dentro do contêiner do relay: o que ele alcança pela rede.
_ALCANCE = """
import json, socket
alvos = {"pg-livro": 5432, "rabbitmq": 5672, "pg-consolidado": 5432}
resultado = {}
for host, porta in alvos.items():
    try:
        socket.create_connection((host, porta), timeout=3).close()
        resultado[host] = True
    except OSError:
        resultado[host] = False
print(json.dumps(resultado))
"""

# Executado num contêiner avulso da imagem do relay, com o usuário worker:
# esvazia a fila e devolve o message_id de cada mensagem.
_CONSUMIR = """
import asyncio, json, os, aio_pika
async def main():
    url = "amqp://worker:" + os.environ["RABBITMQ_SENHA_WORKER"] + "@rabbitmq:5672/"
    async with await aio_pika.connect(url, timeout=10) as conexao:
        canal = await conexao.channel()
        fila = await canal.get_queue("consolidado.lancamentos", ensure=False)
        ids = []
        while (mensagem := await fila.get(no_ack=False, fail=False)) is not None:
            ids.append(mensagem.message_id)
            await mensagem.ack()
        print(json.dumps(ids))
asyncio.run(main())
"""

# Executado dentro do contêiner do relay: status e corpo de uma URL interna.
_LER_URL = """
import sys, urllib.error, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=5) as resposta:
        print(resposta.status); print(resposta.read().decode())
except urllib.error.HTTPError as erro:
    print(erro.code); print(erro.read().decode())
"""


def _porta_livre() -> int:
    with socket.socket() as soquete:
        soquete.bind(("127.0.0.1", 0))
        porta: int = soquete.getsockname()[1]
        return porta


def _aguardar(
    condicao: Callable[[], bool], limite_segundos: float, motivo: str
) -> None:
    fim = time.monotonic() + limite_segundos
    while not condicao():
        assert time.monotonic() < fim, f"tempo esgotado: {motivo}"
        time.sleep(1)


@dataclass(frozen=True)
class Ambiente:
    porta_keycloak: int
    porta_api: int

    def compose(
        self, *args: str, timeout: int = 120
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            **AMBIENTE_RELAY,
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

    def sql(self, consulta: str) -> list[list[str]]:
        # Soquete local do contêiner, como superusuário: só leitura de
        # conferência.
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

    def pendentes(self) -> int:
        linhas = self.sql("SELECT count(*) FROM outbox WHERE publicado_em IS NULL")
        return int(linhas[0][0])

    def eventos(self) -> set[str]:
        return {linha[0] for linha in self.sql("SELECT evento_id FROM outbox")}

    def no_relay(self, *args: str) -> str:
        return self.exigir("exec", "-T", "relay", "python", "-c", *args)

    def url_no_relay(self, url: str) -> tuple[int, str]:
        status, corpo = self.no_relay(_LER_URL, url).split("\n", 1)
        return int(status), corpo

    def ready_do_relay(self) -> int:
        return self.url_no_relay("http://127.0.0.1:8080/health/ready")[0]

    def metrica(self, linha_inicio: str) -> float:
        _, corpo = self.url_no_relay("http://127.0.0.1:9464/metrics")
        for linha in corpo.splitlines():
            if linha.startswith(linha_inicio + " "):
                return float(linha.rsplit(" ", 1)[1])
        raise AssertionError(f"métrica ausente: {linha_inicio}")

    def consumir_fila(self) -> list[str]:
        saida = self.exigir(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "-e",
            SENHA_WORKER,
            "--entrypoint",
            "python",
            "relay",
            "-c",
            _CONSUMIR,
        )
        ids: list[str] = json.loads(saida.strip().splitlines()[-1])
        return ids

    def mensagens_na_fila(self) -> int:
        saida = self.exigir(
            "exec",
            "-T",
            "rabbitmq",
            "rabbitmqctl",
            "list_queues",
            "--quiet",
            "--no-table-headers",
            "name",
            "messages",
        )
        for linha in saida.splitlines():
            nome, _, quantidade = linha.partition("\t")
            if nome == FILA:
                return int(quantidade)
        raise AssertionError(f"fila {FILA} ausente:\n{saida}")

    def saude(self, servico: str) -> str:
        saida = self.exigir("ps", "-a", "--format", "json", servico)
        linhas = [json.loads(linha) for linha in saida.splitlines() if linha]
        assert len(linhas) == 1, saida
        return str(linhas[0].get("Health", ""))

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
            requisicao, context=ssl.create_default_context(cafile=str(AC)), timeout=10
        ) as resposta:
            token: str = json.load(resposta)["access_token"]
            return token

    def registrar(self, token: str, indice: int) -> dict[str, Any]:
        corpo = {
            "tipo": "CREDITO" if indice % 2 else "DEBITO",
            "valor": f"{indice + 1}.25",
            "data_lancamento": "2026-11-06",
            "descricao": f"queda rabbitmq {indice}",
        }
        requisicao = urllib.request.Request(
            f"https://localhost:{self.porta_api}/v1/lancamentos",
            data=json.dumps(corpo).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": f"e2e-queda-rabbitmq-{uuid.uuid4()}",
            },
        )
        with urllib.request.urlopen(
            requisicao, context=ssl.create_default_context(cafile=str(AC)), timeout=30
        ) as resposta:
            assert resposta.status == 201
            registrado: dict[str, Any] = json.load(resposta)
            return registrado

    def eventos_de(self, lancamentos: list[dict[str, Any]]) -> set[str]:
        ids = ", ".join(
            f"'{uuid.UUID(lancamento['id'])}'" for lancamento in lancamentos
        )
        consulta = f"SELECT evento_id FROM outbox WHERE lancamento_id IN ({ids})"
        return {linha[0] for linha in self.sql(consulta)}


@pytest.fixture(scope="module")
def ambiente() -> Iterator[Ambiente]:
    ambiente = Ambiente(porta_keycloak=_porta_livre(), porta_api=_porta_livre())
    try:
        # Banco primeiro, migração do livro com livro_migracao (002-T2) e só
        # então o restante, aguardando todos os health checks, inclusive o
        # do relay. Sem o worker (004-T3): ele consumiria da fila as
        # mensagens que o teste confere.
        ambiente.exigir("up", "-d", "--wait", "pg-livro", timeout=300)
        ambiente.exigir("run", "--rm", "--build", "livro-migracao", timeout=600)
        ambiente.exigir("up", "-d", "--build", "--wait", *SERVICOS, timeout=900)
        yield ambiente
    finally:
        ambiente.compose(
            "--profile",
            "migracao",
            "--profile",
            "operacao",
            "down",
            "-v",
            "--rmi",
            "local",
            "--remove-orphans",
            timeout=300,
        )


def test_relay_fica_healthy_e_nao_alcanca_pg_consolidado(ambiente: Ambiente) -> None:
    assert ambiente.saude("relay") == "healthy"
    assert ambiente.ready_do_relay() == 200

    alcance = json.loads(ambiente.no_relay(_ALCANCE))

    # Controle positivo: as dependências do relay respondem pela rede.
    assert alcance == {"pg-livro": True, "rabbitmq": True, "pg-consolidado": False}


def test_backlog_zera_apos_retorno_do_rabbitmq(ambiente: Ambiente) -> None:
    ambiente.consumir_fila()

    # Falha injetada pelo próprio Docker (ST-Q-010).
    ambiente.exigir("stop", "rabbitmq", timeout=120)
    falhas_antes = ambiente.metrica(
        'cashflow_relay_falhas_publicacao_total{resultado="falha"}'
    )

    token = ambiente.token()
    lancamentos = [
        ambiente.registrar(token, indice) for indice in range(LANCAMENTOS_NA_QUEDA)
    ]
    eventos = ambiente.eventos_de(lancamentos)
    assert len(eventos) == LANCAMENTOS_NA_QUEDA

    # Com o broker fora: nada é marcado, o backlog aparece na métrica, as
    # tentativas falhas são contadas e o ready fica falso (NFR-001, FR-006).
    _aguardar(
        lambda: (
            ambiente.metrica(
                'cashflow_relay_falhas_publicacao_total{resultado="falha"}'
            )
            > falhas_antes
        ),
        30,
        "falhas de publicação contadas com o RabbitMQ parado",
    )
    _aguardar(
        lambda: ambiente.metrica("cashflow_outbox_pendentes") == LANCAMENTOS_NA_QUEDA,
        30,
        "backlog na métrica",
    )
    assert ambiente.pendentes() == LANCAMENTOS_NA_QUEDA
    assert ambiente.metrica("cashflow_outbox_idade_mais_antigo_segundos") > 0
    assert ambiente.ready_do_relay() == 503

    ambiente.exigir("up", "-d", "--wait", "rabbitmq", timeout=300)

    _aguardar(lambda: ambiente.pendentes() == 0, 120, "backlog zerado")
    _aguardar(
        lambda: ambiente.metrica("cashflow_outbox_pendentes") == 0,
        30,
        "backlog zerado na métrica",
    )
    _aguardar(lambda: ambiente.ready_do_relay() == 200, 60, "relay pronto")
    assert ambiente.metrica("cashflow_outbox_idade_mais_antigo_segundos") == 0

    # Cada evento registrado na queda chegou à fila (duplicata é aceitável:
    # o consumidor deduplica pelo message_id; INFRA 2.4).
    assert set(ambiente.consumir_fila()) == eventos


def test_perda_do_volume_do_rabbitmq_e_republicacao(ambiente: Ambiente) -> None:
    token = ambiente.token()
    for indice in range(LANCAMENTOS_ANTES_DA_PERDA):
        ambiente.registrar(token, 100 + indice)
    _aguardar(lambda: ambiente.pendentes() == 0, 60, "eventos publicados")
    todos = ambiente.eventos()
    total = LANCAMENTOS_NA_QUEDA + LANCAMENTOS_ANTES_DA_PERDA
    assert len(todos) == total
    _aguardar(
        lambda: ambiente.mensagens_na_fila() == LANCAMENTOS_ANTES_DA_PERDA,
        30,
        "mensagens persistidas na fila antes da perda",
    )

    # Perda dos dados do broker: contêiner e volume removidos (INFRA 7.6).
    ambiente.exigir("stop", "rabbitmq", timeout=120)
    ambiente.exigir("rm", "-f", "rabbitmq", timeout=120)
    removido = subprocess.run(
        ["docker", "volume", "rm", VOLUME_RABBITMQ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert removido.returncode == 0, removido.stderr
    ambiente.exigir("up", "-d", "--wait", "rabbitmq", timeout=300)

    # O broker voltou só com a topologia das definições, sem mensagens, e a
    # outbox continua toda marcada: sem republicação, nada mais chegaria.
    assert ambiente.mensagens_na_fila() == 0
    assert ambiente.pendentes() == 0

    saida = ambiente.exigir(
        "--profile",
        "operacao",
        "run",
        "--rm",
        "-T",
        "relay-republicar",
        "--desde-posicao",
        "1",
        timeout=300,
    )
    assert f"{total} eventos reabertos a partir da posição 1" in saida

    _aguardar(lambda: ambiente.pendentes() == 0, 120, "republicação concluída")
    _aguardar(lambda: ambiente.ready_do_relay() == 200, 60, "relay pronto")

    # Todos os eventos da outbox chegaram à fila (INFRA 7.6).
    assert set(ambiente.consumir_fila()) == todos
