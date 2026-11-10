"""`cashflow_pool_conexoes{componente,estado}` lido do pool na coleta (INFRA 8.3).

Um só lugar para os quatro componentes com pool de longa duração: APIs de
lançamentos e do consolidado, worker e relay (007 design §4). O pool é lido
na coleta, sem laço próprio, e os quatro estados nascem juntos, para o
Prometheus ter série desde a subida.

O pacote não depende do psycopg: basta o objeto ter `get_stats()`, como o
`AsyncConnectionPool` do psycopg_pool.
"""

from collections.abc import Callable, Mapping
from typing import Protocol

from observabilidade.metricas import Metricas


class PoolComEstatisticas(Protocol):
    def get_stats(self) -> Mapping[str, int]: ...


def medir_pool(pool: PoolComEstatisticas, metricas: Metricas, componente: str) -> None:
    """Registra total, disponível, em uso e aguardando do pool de `componente`."""
    medidor = metricas.medidor("cashflow_pool_conexoes")

    def estatistica(chave: str) -> Callable[[], float]:
        return lambda: pool.get_stats().get(chave, 0)

    def em_uso() -> float:
        estado = pool.get_stats()
        return estado.get("pool_size", 0) - estado.get("pool_available", 0)

    rotulos = {"componente": componente}
    medidor.labels(**rotulos, estado="total").set_function(estatistica("pool_size"))
    medidor.labels(**rotulos, estado="disponivel").set_function(
        estatistica("pool_available")
    )
    medidor.labels(**rotulos, estado="em_uso").set_function(em_uso)
    medidor.labels(**rotulos, estado="aguardando").set_function(
        estatistica("requests_waiting")
    )
