"""Live e ready do relay em porta interna (003-T5; INFRA 8.4, 8.5)."""

import asyncio
import json
from typing import Any

from relay.adaptadores.http import ServidorSaude


async def _pedir(porta: int, linha: str) -> tuple[int, Any]:
    leitor, escritor = await asyncio.open_connection("127.0.0.1", porta)
    escritor.write(f"{linha}\r\nHost: localhost\r\n\r\n".encode())
    await escritor.drain()
    resposta = await leitor.read()
    escritor.close()
    cabecalho, corpo = resposta.split(b"\r\n\r\n", 1)
    status = int(cabecalho.split(b" ")[1])
    return status, json.loads(corpo)


def _com_servidor(verificacoes: dict[str, bool], linha: str) -> tuple[int, Any]:
    async def cenario() -> tuple[int, Any]:
        async def prontidao() -> dict[str, bool]:
            return verificacoes

        servidor = ServidorSaude(prontidao)
        await servidor.iniciar(0, endereco="127.0.0.1")
        try:
            return await _pedir(servidor.porta, linha)
        finally:
            await servidor.parar()

    return asyncio.run(cenario())


def test_live_responde_vivo_mesmo_com_dependencias_fora() -> None:
    status, corpo = _com_servidor(
        {"banco": False, "rabbitmq": False}, "GET /health/live HTTP/1.1"
    )
    assert (status, corpo) == (200, {"status": "vivo"})


def test_ready_com_banco_e_rabbitmq_conectados_responde_pronto() -> None:
    status, corpo = _com_servidor(
        {"banco": True, "rabbitmq": True}, "GET /health/ready HTTP/1.1"
    )
    assert status == 200
    assert corpo == {
        "status": "pronto",
        "verificacoes": {"banco": True, "rabbitmq": True},
    }


def test_ready_com_rabbitmq_fora_responde_503_e_aponta_a_dependencia() -> None:
    status, corpo = _com_servidor(
        {"banco": True, "rabbitmq": False}, "GET /health/ready HTTP/1.1"
    )
    assert status == 503
    assert corpo["status"] == "nao_pronto"
    assert corpo["verificacoes"] == {"banco": True, "rabbitmq": False}


def test_ready_com_banco_fora_responde_503() -> None:
    status, _ = _com_servidor(
        {"banco": False, "rabbitmq": True}, "GET /health/ready HTTP/1.1"
    )
    assert status == 503


def test_caminho_desconhecido_responde_404() -> None:
    status, _ = _com_servidor({"banco": True}, "GET /metrics HTTP/1.1")
    assert status == 404


def test_metodo_diferente_de_get_responde_405() -> None:
    status, _ = _com_servidor({"banco": True}, "POST /health/ready HTTP/1.1")
    assert status == 405


def test_linha_de_pedido_malformada_responde_400() -> None:
    status, _ = _com_servidor({"banco": True}, "GET")
    assert status == 400
