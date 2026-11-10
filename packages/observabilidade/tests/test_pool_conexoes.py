"""Medidor do pool de conexões, lido na coleta (INFRA 8.3; 007 design §4)."""

from collections.abc import Mapping

from observabilidade import Metricas, medir_pool
from prometheus_client import generate_latest


class _PoolFalso:
    def __init__(self) -> None:
        self.estado = {"pool_size": 2, "pool_available": 2}

    def get_stats(self) -> Mapping[str, int]:
        return self.estado


def _amostras(metricas: Metricas) -> dict[str, float]:
    linhas = generate_latest(metricas.registro).decode().splitlines()
    return {
        linha.split("estado=")[1].split('"')[1]: float(linha.rsplit(" ", 1)[1])
        for linha in linhas
        if linha.startswith("cashflow_pool_conexoes{")
    }


def test_os_quatro_estados_do_pool_nascem_com_o_componente() -> None:
    metricas = Metricas()

    medir_pool(_PoolFalso(), metricas, "relay")

    corpo = generate_latest(metricas.registro).decode()
    assert 'cashflow_pool_conexoes{componente="relay",estado="total"} 2.0' in corpo
    # Chave ausente em get_stats (ninguém esperou ainda) vale zero.
    assert _amostras(metricas) == {
        "total": 2.0,
        "disponivel": 2.0,
        "em_uso": 0.0,
        "aguardando": 0.0,
    }


def test_estado_do_pool_e_lido_na_coleta_sem_registrar_de_novo() -> None:
    metricas = Metricas()
    pool = _PoolFalso()
    medir_pool(pool, metricas, "relay")

    # Esgotado: as duas conexões em uso e uma requisição na fila.
    pool.estado = {"pool_size": 2, "pool_available": 0, "requests_waiting": 1}

    assert _amostras(metricas) == {
        "total": 2.0,
        "disponivel": 0.0,
        "em_uso": 2.0,
        "aguardando": 1.0,
    }
