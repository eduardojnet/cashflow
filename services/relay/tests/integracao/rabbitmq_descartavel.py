"""RabbitMQ descartável para os testes de integração do relay (INFRA 9.2).

Mesma imagem do compose, mesmo entrypoint, mesmas definições (topologia,
usuários e permissões) e mesma configuração do serviço `rabbitmq`, sem
segunda cópia que possa divergir do broker real. O broker do compose não
publica porta (INFRA 6.3); este publica a AMQP só em 127.0.0.1, numa porta
efêmera, com senhas aleatórias a cada execução, e é removido com seus
volumes ao fim, como o PostgreSQL descartável do livro.
"""

import asyncio
import os
import re
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import aio_pika
from lancamentos.tests.integracao.livro_descartavel import docker

RAIZ = Path(__file__).resolve().parents[4]
COMPOSE = RAIZ / "infra/compose.yaml"
RABBITMQ = RAIZ / "infra/rabbitmq"
FILA = "consolidado.lancamentos"
USUARIOS = ["relay", "worker"]


@dataclass(frozen=True)
class Broker:
    id_conteiner: str
    porta: int
    senhas: dict[str, str]

    def url(self, usuario: str) -> str:
        return f"amqp://{usuario}:{self.senhas[usuario]}@127.0.0.1:{self.porta}/"


def _imagem_do_compose() -> str:
    # A mesma imagem fixada para o serviço rabbitmq (001 TD-007).
    encontrada = re.search(r"image:\s*(rabbitmq:\S+)", COMPOSE.read_text())
    assert encontrada, "imagem do RabbitMQ não encontrada em infra/compose.yaml"
    return encontrada.group(1)


async def _fila_visivel(broker: Broker) -> None:
    # Pronto = definições carregadas: o worker autentica e enxerga a fila.
    conexao = await aio_pika.connect(broker.url("worker"), timeout=3)
    async with conexao:
        canal = await conexao.channel()
        await canal.get_queue(FILA, ensure=True)


def _aguardar_pronto(broker: Broker) -> None:
    limite = time.monotonic() + 120
    while time.monotonic() < limite:
        try:
            asyncio.run(_fila_visivel(broker))
            return
        except Exception:  # noqa: BLE001 - qualquer falha é "ainda não pronto"
            time.sleep(1)
    logs = docker("logs", broker.id_conteiner).stdout[-2000:]
    raise AssertionError(f"RabbitMQ descartável não ficou pronto:\n{logs}")


@contextmanager
def rabbitmq_descartavel(rotulo: str) -> Iterator[Broker]:
    """Sobe e, ao sair, remove um RabbitMQ com a topologia do CashFlow."""
    senhas = {usuario: secrets.token_hex(24) for usuario in USUARIOS}
    # Senhas por variável de ambiente, não pela linha de comando.
    env = {
        **os.environ,
        "RABBITMQ_SENHA_RELAY": senhas["relay"],
        "RABBITMQ_SENHA_WORKER": senhas["worker"],
    }
    criado = docker(
        "run",
        "-d",
        "--label",
        f"cashflow.teste={rotulo}",
        "--hostname",
        "rabbitmq",
        "-p",
        "127.0.0.1::5672",
        "-e",
        "RABBITMQ_SENHA_RELAY",
        "-e",
        "RABBITMQ_SENHA_WORKER",
        "-v",
        f"{RABBITMQ / 'entrypoint.sh'}:/etc/rabbitmq/cashflow/entrypoint.sh:ro",
        "-v",
        f"{RABBITMQ / 'definicoes.modelo.json'}"
        ":/etc/rabbitmq/cashflow/definicoes.modelo.json:ro",
        "-v",
        f"{RABBITMQ / '20-cashflow.conf'}:/etc/rabbitmq/conf.d/20-cashflow.conf:ro",
        "--entrypoint",
        "/etc/rabbitmq/cashflow/entrypoint.sh",
        _imagem_do_compose(),
        "rabbitmq-server",
        env=env,
        timeout=300,
    )
    assert criado.returncode == 0, criado.stderr
    id_ = criado.stdout.strip()
    try:
        porta = docker("port", id_, "5672/tcp").stdout.split(":")[-1].strip()
        broker = Broker(id_conteiner=id_, porta=int(porta), senhas=senhas)
        _aguardar_pronto(broker)
        yield broker
    finally:
        docker("rm", "-f", "-v", id_)
