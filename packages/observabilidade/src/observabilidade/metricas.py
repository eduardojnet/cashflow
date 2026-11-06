"""Registro de métricas por componente e endpoint /metrics (007 design §4).

Cada processo cria um `Metricas` e registra só as métricas que produz, para
que o Prometheus não mostre zeros de métricas que o componente não mede.
O endpoint fica em porta interna, sem publicação no host (INFRA 6.1).
"""

from threading import Thread
from wsgiref.simple_server import WSGIServer

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
    start_http_server,
)

from observabilidade.catalogo import (
    CATALOGO,
    DefinicaoMetrica,
    ErroCatalogo,
    MetricaDesconhecida,
    TipoMetrica,
    validar_definicao,
)


class ServidorMetricas:
    """Servidor HTTP do /metrics rodando em thread de fundo."""

    def __init__(self, servidor: WSGIServer, thread: Thread) -> None:
        self._servidor = servidor
        self._thread = thread

    @property
    def porta(self) -> int:
        return self._servidor.server_port

    def parar(self) -> None:
        self._servidor.shutdown()
        self._servidor.server_close()
        self._thread.join()


class Metricas:
    def __init__(self) -> None:
        self.registro = CollectorRegistry()
        self._metricas: dict[str, Counter | Gauge] = {}

    def registrar(self, definicao: DefinicaoMetrica) -> Counter | Gauge:
        """Registra a métrica após validar nome e rótulos (TD-003)."""
        validar_definicao(definicao)
        if CATALOGO[definicao.nome] != definicao:
            raise ErroCatalogo(f"definição diverge do catálogo: {definicao.nome}")
        existente = self._metricas.get(definicao.nome)
        if existente is not None:
            return existente
        classe = Counter if definicao.tipo is TipoMetrica.CONTADOR else Gauge
        metrica = classe(
            definicao.nome,
            definicao.descricao,
            labelnames=definicao.rotulos,
            registry=self.registro,
        )
        self._metricas[definicao.nome] = metrica
        return metrica

    def contador(self, nome: str) -> Counter:
        metrica = self.registrar(self._definicao(nome, TipoMetrica.CONTADOR))
        assert isinstance(metrica, Counter)
        return metrica

    def medidor(self, nome: str) -> Gauge:
        metrica = self.registrar(self._definicao(nome, TipoMetrica.MEDIDOR))
        assert isinstance(metrica, Gauge)
        return metrica

    def exposicao(self) -> bytes:
        """Texto no formato de exposição do Prometheus."""
        return generate_latest(self.registro)

    def iniciar_servidor(
        self, porta: int, endereco: str = "0.0.0.0"
    ) -> ServidorMetricas:
        """Sobe o /metrics em porta interna; porta 0 escolhe uma livre."""
        servidor, thread = start_http_server(
            porta, addr=endereco, registry=self.registro
        )
        return ServidorMetricas(servidor, thread)

    @staticmethod
    def _definicao(nome: str, tipo: TipoMetrica) -> DefinicaoMetrica:
        definicao = CATALOGO.get(nome)
        if definicao is None:
            raise MetricaDesconhecida(f"métrica fora do catálogo: {nome}")
        if definicao.tipo is not tipo:
            raise ErroCatalogo(f"{nome} é {definicao.tipo.value}, não {tipo.value}")
        return definicao
