"""Permissões mínimas dos usuários do RabbitMQ contra o broker real (001-T2).

O RabbitMQ não publica porta no host (INFRA 6.3), então o teste roda dentro
da rede de mensageria (001-T5). Essa rede é interna e sem saída; a rede de
identidade entra só para o uv baixar as dependências. Com o ambiente no ar, a
partir da raiz do repositório:

    docker run --rm --network cashflow_rede-identidade \\
      --network cashflow_rede-mensageria --env-file infra/.env \\
      -e RABBITMQ_HOST=rabbitmq -e UV_PROJECT_ENVIRONMENT=/tmp/venv \\
      -v "$PWD":/w -w /w ghcr.io/astral-sh/uv:0.13.7-python3.12-alpine \\
      uv run --frozen pytest -p no:cacheprovider tests/infra/test_rabbitmq_permissoes.py

Sem `RABBITMQ_HOST`, o módulo é pulado com o motivo explícito.

Com o worker do consolidado no ar (004-T3), a fila tem consumidor: mensagem
publicada nela seria projetada ou quarentenada no banco do ambiente. Por
isso a escrita do `relay` é conferida com uma routing key sem binding (o
broker confirma e descarta) e a leitura do `worker` com um Basic.Get que
devolve à fila o que pegar. O roteamento do exchange para a fila é conferido
contra o RabbitMQ descartável com as mesmas definições
(services/relay/tests/integracao/test_publicacao_rabbitmq.py).
"""

import asyncio
import os
import uuid
from collections.abc import Callable

import aio_pika
import aiormq
import pytest
from aio_pika.exceptions import ProbableAuthenticationError
from aiormq.exceptions import ChannelAccessRefused

EXCHANGE = "cashflow.lancamentos"
FILA = "consolidado.lancamentos"
ROUTING_KEY = "lancamento.registrado.v1"
# Sem binding: a mensagem não chega a nenhuma fila (ver docstring).
ROUTING_KEY_SEM_FILA = "teste.permissao"

HOST = os.environ.get("RABBITMQ_HOST")

pytestmark = pytest.mark.skipif(
    HOST is None,
    reason="RABBITMQ_HOST ausente: executar dentro da rede do compose (ver docstring)",
)


def _url(usuario: str, senha: str) -> str:
    return f"amqp://{usuario}:{senha}@{HOST}:5672/"


def _url_relay() -> str:
    return _url("relay", os.environ["RABBITMQ_SENHA_RELAY"])


def _url_worker() -> str:
    return _url("worker", os.environ["RABBITMQ_SENHA_WORKER"])


async def _publicar(url: str, routing_key: str = ROUTING_KEY_SEM_FILA) -> object:
    conexao = await aio_pika.connect(url)
    async with conexao:
        canal = await conexao.channel(publisher_confirms=True)
        exchange = await canal.get_exchange(EXCHANGE, ensure=False)
        return await exchange.publish(
            aio_pika.Message(
                body=b"{}",
                message_id=str(uuid.uuid4()),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=routing_key,
            # Sem `mandatory`: a mensagem sem rota é confirmada e descartada
            # pelo broker, em vez de devolvida.
            mandatory=False,
        )


async def _ler_sem_consumir(url: str) -> None:
    """Basic.Get na fila; o que vier volta à fila, sem ACK (ver docstring)."""
    conexao = await aio_pika.connect(url)
    async with conexao:
        canal = await conexao.channel()
        fila = await canal.get_queue(FILA, ensure=False)
        mensagem = await fila.get(no_ack=False, fail=False, timeout=5)
        if mensagem is not None:
            await mensagem.nack(requeue=True)


def test_relay_publica_no_exchange() -> None:
    confirmacao = asyncio.run(_publicar(_url_relay()))

    assert isinstance(confirmacao, aiormq.spec.Basic.Ack)


def test_worker_le_da_fila() -> None:
    asyncio.run(_ler_sem_consumir(_url_worker()))


def test_worker_nao_publica_no_exchange() -> None:
    with pytest.raises(ChannelAccessRefused):
        asyncio.run(_publicar(_url_worker()))


def test_relay_nao_consome_da_fila() -> None:
    with pytest.raises(ChannelAccessRefused):
        asyncio.run(_ler_sem_consumir(_url_relay()))


async def _declarar_fila(url: str) -> None:
    conexao = await aio_pika.connect(url)
    async with conexao:
        canal = await conexao.channel()
        await canal.declare_queue(f"intrusa-{uuid.uuid4()}", durable=True)


@pytest.mark.parametrize("url", [_url_relay, _url_worker], ids=["relay", "worker"])
def test_relay_e_worker_nao_declaram_topologia(url: Callable[[], str]) -> None:
    with pytest.raises(ChannelAccessRefused):
        asyncio.run(_declarar_fila(url()))


def test_usuario_padrao_guest_nao_existe() -> None:
    with pytest.raises(ProbableAuthenticationError):
        asyncio.run(aio_pika.connect(_url("guest", "guest")))
