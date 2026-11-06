"""Métricas do worker expostas ao Prometheus (004-T3, T4; design 004 §12).

- `cashflow_worker_reentregas_total`: mensagens que o broker entregou de novo
  (flag `redelivered`), por queda do worker, ACK perdido ou canal fechado
  com a mensagem pendente (INFRA 8.3).
- `cashflow_quarentena_total`: mensagens guardadas na quarentena, ilegíveis
  ou em conflito, contadas depois do commit (FR-003; INFRA 8.3).
- `cashflow_pool_conexoes{componente="worker",estado}`: pool do banco do
  consolidado, lido na coleta (INFRA 8.3).
- `cashflow_checkpoint_posicao{geracao}` e `cashflow_lacunas{geracao}`:
  posição contínua e lacunas abertas da geração, gravadas depois de cada
  commit que aplica um lançamento e na abertura de cada sessão de consumo
  (FR-009; TD-006; INFRA 8.3).

Os contadores nascem com zero na subida, para o Prometheus ter dado mesmo
sem reentrega nem quarentena. Os medidores por geração nascem na primeira
leitura do banco, que traz o número da geração.
"""

from collections.abc import Callable
from typing import Any

from observabilidade import Metricas
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from consolidado.aplicacao import Desfecho, EstadoCheckpoint

COMPONENTE = "worker"


class MetricasWorker:
    """Implementa o `Observador` do processador e o `ObservadorCheckpoint`."""

    def __init__(self, metricas: Metricas) -> None:
        self._reentregas = metricas.contador("cashflow_worker_reentregas_total")
        self._quarentena = metricas.contador("cashflow_quarentena_total")
        self._checkpoint = metricas.medidor("cashflow_checkpoint_posicao")
        self._lacunas = metricas.medidor("cashflow_lacunas")

    def reentrega(self) -> None:
        self._reentregas.inc()

    def desfecho(self, desfecho: Desfecho) -> None:
        if desfecho.quarentena:
            self._quarentena.inc()

    def checkpoint(self, estado: EstadoCheckpoint) -> None:
        geracao = str(estado.geracao_id)
        self._checkpoint.labels(geracao=geracao).set(estado.posicao_continua)
        self._lacunas.labels(geracao=geracao).set(estado.lacunas)


def medir_pool(
    pool: AsyncConnectionPool[AsyncConnection[Any]], metricas: Metricas
) -> None:
    """Mesmos estados da API de lançamentos (002-T5), com `componente=worker`."""
    medidor = metricas.medidor("cashflow_pool_conexoes")

    def estatistica(chave: str) -> Callable[[], float]:
        return lambda: pool.get_stats().get(chave, 0)

    def em_uso() -> float:
        estado = pool.get_stats()
        return estado.get("pool_size", 0) - estado.get("pool_available", 0)

    rotulos = {"componente": COMPONENTE}
    medidor.labels(**rotulos, estado="total").set_function(estatistica("pool_size"))
    medidor.labels(**rotulos, estado="disponivel").set_function(
        estatistica("pool_available")
    )
    medidor.labels(**rotulos, estado="em_uso").set_function(em_uso)
    medidor.labels(**rotulos, estado="aguardando").set_function(
        estatistica("requests_waiting")
    )
