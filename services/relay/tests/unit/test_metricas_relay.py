"""Métricas do relay pelo catálogo fechado (003-T5; NFR-001, FR-006; 007 TD-006)."""

from observabilidade import Metricas
from relay.adaptadores.metricas import RESULTADOS_DE_FALHA, MetricasRelay
from relay.adaptadores.postgres import EstadoOutbox
from relay.aplicacao import ResultadoPublicacao, ResumoCiclo


def _exposicao(metricas: Metricas) -> str:
    return metricas.exposicao().decode()


def test_falhas_de_publicacao_nascem_com_zero_nos_quatro_resultados() -> None:
    metricas = Metricas()
    MetricasRelay(metricas)

    texto = _exposicao(metricas)

    assert {r.value for r in RESULTADOS_DE_FALHA} == {
        "devolvido",
        "recusado",
        "sem_confirmacao",
        "falha",
    }
    for resultado in RESULTADOS_DE_FALHA:
        linha = (
            "cashflow_relay_falhas_publicacao_total"
            f'{{resultado="{resultado.value}"}} 0.0'
        )
        assert linha in texto
    assert 'resultado="confirmado"' not in texto


def test_ciclo_soma_falhas_por_resultado_sem_contar_confirmados() -> None:
    metricas = Metricas()
    relay = MetricasRelay(metricas)
    resultados = dict.fromkeys(ResultadoPublicacao, 0)
    resultados[ResultadoPublicacao.CONFIRMADO] = 5
    resultados[ResultadoPublicacao.DEVOLVIDO] = 2
    resultados[ResultadoPublicacao.FALHA] = 3

    relay.registrar_ciclo(ResumoCiclo(reservados=10, resultados=resultados))
    relay.registrar_ciclo(ResumoCiclo(reservados=10, resultados=resultados))

    texto = _exposicao(metricas)
    nome = "cashflow_relay_falhas_publicacao_total"
    assert f'{nome}{{resultado="devolvido"}} 4.0' in texto
    assert f'{nome}{{resultado="falha"}} 6.0' in texto
    assert f'{nome}{{resultado="recusado"}} 0.0' in texto
    assert 'resultado="confirmado"' not in texto


def test_outbox_expoe_backlog_e_idade_do_mais_antigo() -> None:
    metricas = Metricas()
    relay = MetricasRelay(metricas)

    relay.registrar_outbox(EstadoOutbox(pendentes=7, idade_mais_antigo_segundos=42.5))
    texto = _exposicao(metricas)
    assert "cashflow_outbox_pendentes 7.0" in texto
    assert "cashflow_outbox_idade_mais_antigo_segundos 42.5" in texto

    relay.registrar_outbox(EstadoOutbox(pendentes=0, idade_mais_antigo_segundos=0))
    texto = _exposicao(metricas)
    assert "cashflow_outbox_pendentes 0.0" in texto
    assert "cashflow_outbox_idade_mais_antigo_segundos 0.0" in texto
