"""Métricas do relay expostas ao Prometheus (003-T5; design 003 §12).

- `cashflow_outbox_pendentes` e `cashflow_outbox_idade_mais_antigo_segundos`:
  backlog e idade do evento pendente mais antigo (INFRA 8.3; NFR-001). A
  idade é o sinal de alerta da falha persistente (D-002; FR-006).
- `cashflow_relay_falhas_publicacao_total{resultado}`: publicações sem
  confirm positivo, por desfecho (007 TD-006). As quatro séries nascem com
  zero na subida, para o Prometheus ter dado mesmo sem falha.
- `cashflow_pool_conexoes{componente="relay",estado}`: pool do livro, lido
  na coleta por `observabilidade.medir_pool`, registrado em `__main__`
  junto do pool (review 007, Issue 1). Esgotado, trava a publicação inteira.

Mensagens prontas e não confirmadas vêm do plugin Prometheus do RabbitMQ
(007 design §4), não daqui.
"""

from observabilidade import Metricas

from relay.adaptadores.postgres import EstadoOutbox
from relay.aplicacao import ResultadoPublicacao, ResumoCiclo

# Valores fechados do rótulo `resultado` (007 TD-006): todo desfecho que não
# marca o evento como publicado.
RESULTADOS_DE_FALHA = tuple(
    resultado
    for resultado in ResultadoPublicacao
    if resultado is not ResultadoPublicacao.CONFIRMADO
)


class MetricasRelay:
    def __init__(self, metricas: Metricas) -> None:
        self._pendentes = metricas.medidor("cashflow_outbox_pendentes")
        self._idade = metricas.medidor("cashflow_outbox_idade_mais_antigo_segundos")
        self._falhas = metricas.contador("cashflow_relay_falhas_publicacao_total")
        for resultado in RESULTADOS_DE_FALHA:
            self._falhas.labels(resultado=resultado.value)

    def registrar_ciclo(self, resumo: ResumoCiclo) -> None:
        for resultado in RESULTADOS_DE_FALHA:
            quantidade = resumo.resultados.get(resultado, 0)
            if quantidade:
                self._falhas.labels(resultado=resultado.value).inc(quantidade)

    def registrar_outbox(self, estado: EstadoOutbox) -> None:
        self._pendentes.set(estado.pendentes)
        self._idade.set(estado.idade_mais_antigo_segundos)
