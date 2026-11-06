"""Health do worker: `GET /health/live` e `GET /health/ready` (004-T3; §12).

O worker não é API: não recebe tráfego de cliente e não precisa de framework
HTTP. Um servidor asyncio mínimo, no mesmo laço do worker, responde às duas
rotas em porta interna, sem publicação no host (INFRA 6.1), com o mesmo
formato JSON do relay e da API de lançamentos:

- live: o processo e o laço respondem (INFRA 8.4);
- ready: banco do consolidado e RabbitMQ conectados (INFRA 8.5; design 004
  §12). 503 com o resultado de cada verificação quando algum falha.

Qualquer outro caminho ou método recebe 404 ou 405. O pedido é lido até o
fim do cabeçalho, com limite de tamanho e de tempo. Mesmo código de
`relay.adaptadores.http.saude`, sem importá-lo: a imagem do consolidado não
leva o código de outro serviço.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable

Prontidao = Callable[[], Awaitable[dict[str, bool]]]

_LIMITE_CABECALHO = 8192
_TIMEOUT_LEITURA_SEGUNDOS = 5.0
_RAZOES = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    503: "Service Unavailable",
}


def _resposta(status: int, corpo: object) -> bytes:
    conteudo = json.dumps(corpo, ensure_ascii=False).encode()
    cabecalho = (
        f"HTTP/1.1 {status} {_RAZOES[status]}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(conteudo)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return cabecalho.encode("ascii") + conteudo


class ServidorSaude:
    def __init__(self, prontidao: Prontidao) -> None:
        self._prontidao = prontidao
        self._servidor: asyncio.Server | None = None

    @property
    def porta(self) -> int:
        if self._servidor is None:
            raise RuntimeError("servidor de health não iniciado")
        porta: int = self._servidor.sockets[0].getsockname()[1]
        return porta

    async def iniciar(self, porta: int, endereco: str = "0.0.0.0") -> None:
        """Escuta em `endereco:porta`; porta 0 escolhe uma livre."""
        self._servidor = await asyncio.start_server(
            self._atender, endereco, porta, limit=_LIMITE_CABECALHO
        )

    async def parar(self) -> None:
        if self._servidor is not None:
            self._servidor.close()
            await self._servidor.wait_closed()
            self._servidor = None

    async def _atender(
        self, leitor: asyncio.StreamReader, escritor: asyncio.StreamWriter
    ) -> None:
        try:
            try:
                pedido = await asyncio.wait_for(
                    leitor.readuntil(b"\r\n\r\n"), _TIMEOUT_LEITURA_SEGUNDOS
                )
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                escritor.write(_resposta(400, {"status": "pedido_invalido"}))
                return
            escritor.write(await self._rotear(pedido))
            await escritor.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            escritor.close()

    async def _rotear(self, pedido: bytes) -> bytes:
        partes = pedido.split(b"\r\n", 1)[0].split(b" ")
        if len(partes) != 3:
            return _resposta(400, {"status": "pedido_invalido"})
        metodo, caminho = partes[0], partes[1].split(b"?", 1)[0]
        if caminho not in (b"/health/live", b"/health/ready"):
            return _resposta(404, {"status": "nao_encontrado"})
        if metodo != b"GET":
            return _resposta(405, {"status": "metodo_nao_permitido"})
        if caminho == b"/health/live":
            return _resposta(200, {"status": "vivo"})
        verificacoes = await self._prontidao()
        if all(verificacoes.values()):
            return _resposta(200, {"status": "pronto", "verificacoes": verificacoes})
        return _resposta(503, {"status": "nao_pronto", "verificacoes": verificacoes})
