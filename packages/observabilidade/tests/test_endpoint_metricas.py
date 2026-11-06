"""Endpoint /metrics servido em porta interna (007 design §4)."""

from collections.abc import Iterator
from urllib.request import urlopen

import pytest
from observabilidade import CATALOGO, Metricas, ServidorMetricas, TipoMetrica


@pytest.fixture
def metricas() -> Metricas:
    return Metricas()


@pytest.fixture
def servidor(metricas: Metricas) -> Iterator[ServidorMetricas]:
    servidor = metricas.iniciar_servidor(0, endereco="127.0.0.1")
    yield servidor
    servidor.parar()


def _ler_metrics(servidor: ServidorMetricas) -> tuple[int, str, str]:
    with urlopen(f"http://127.0.0.1:{servidor.porta}/metrics", timeout=5) as resp:
        return resp.status, resp.headers["Content-Type"], resp.read().decode()


def test_metrics_expoe_valores_no_formato_do_prometheus(
    metricas: Metricas, servidor: ServidorMetricas
) -> None:
    metricas.medidor("cashflow_outbox_pendentes").set(3)
    metricas.contador("cashflow_jwt_falhas_total").labels(motivo="expirado").inc()
    metricas.medidor("cashflow_lacunas").labels(geracao="1").set(2)

    status, tipo, corpo = _ler_metrics(servidor)

    assert status == 200
    assert tipo.startswith("text/plain")
    assert "cashflow_outbox_pendentes 3.0" in corpo
    assert 'cashflow_jwt_falhas_total{motivo="expirado"} 1.0' in corpo
    assert 'cashflow_lacunas{geracao="1"} 2.0' in corpo


def test_todas_as_metricas_do_catalogo_sao_expostas_com_seus_nomes(
    metricas: Metricas, servidor: ServidorMetricas
) -> None:
    for definicao in CATALOGO.values():
        if definicao.tipo is TipoMetrica.CONTADOR:
            metricas.contador(definicao.nome)
        else:
            metricas.medidor(definicao.nome)

    _, _, corpo = _ler_metrics(servidor)

    for nome in CATALOGO:
        assert f"# TYPE {nome} " in corpo
