"""Laço e configuração do processo do relay sem infraestrutura (003-T5).

O laço não para por falha de banco ou de broker (design 003 §8): o ciclo
seguinte tenta de novo, o publicador que não abriu é reaberto e as métricas
continuam atualizadas.
"""

import asyncio
from datetime import timedelta

import pytest
from observabilidade import Metricas
from relay.adaptadores.metricas import MetricasRelay
from relay.adaptadores.postgres import EstadoOutbox
from relay.aplicacao import ResultadoPublicacao, ResumoCiclo
from relay.principal import ConfiguracaoRelay, LacoRelay

AMBIENTE = {
    "PG_LIVRO_SENHA_RELAY": "senha-pg-de-teste",
    "RABBITMQ_SENHA_RELAY": "senha/amqp@de:teste",
}


class _Ciclo:
    def __init__(self, respostas: list[ResumoCiclo | Exception]) -> None:
        self._respostas = respostas
        self.chamadas = 0

    async def executar(self) -> ResumoCiclo:
        resposta = self._respostas[min(self.chamadas, len(self._respostas) - 1)]
        self.chamadas += 1
        if isinstance(resposta, Exception):
            raise resposta
        return resposta


class _Medicao:
    def __init__(self, estado: EstadoOutbox | Exception) -> None:
        self.estado = estado

    async def medir(self) -> EstadoOutbox:
        if isinstance(self.estado, Exception):
            raise self.estado
        return self.estado


class _Publicador:
    def __init__(self, falhas_ao_abrir: int) -> None:
        self._falhas = falhas_ao_abrir
        self.aberturas = 0
        self.fechamentos = 0

    async def abrir(self) -> None:
        self.aberturas += 1
        if self.aberturas <= self._falhas:
            raise ConnectionError("broker fora")

    async def fechar(self) -> None:
        self.fechamentos += 1


def _resumo(reservados: int, **resultados: int) -> ResumoCiclo:
    contagem = dict.fromkeys(ResultadoPublicacao, 0)
    for nome, quantidade in resultados.items():
        contagem[ResultadoPublicacao(nome)] = quantidade
    return ResumoCiclo(reservados=reservados, resultados=contagem)


def _laco(
    ciclo: _Ciclo, medicao: _Medicao, publicador: _Publicador, lote: int = 10
) -> tuple[LacoRelay, Metricas]:
    configuracao = ConfiguracaoRelay(
        conninfo="", url_rabbitmq="", dono="teste", lote=lote, intervalo_segundos=0.01
    )
    metricas = Metricas()
    return (
        LacoRelay(configuracao, ciclo, medicao, publicador, MetricasRelay(metricas)),
        metricas,
    )


def test_rabbitmq_fora_na_subida_nao_para_o_laco_e_o_publicador_e_reaberto() -> None:
    ciclo = _Ciclo([_resumo(2, falha=2), _resumo(2, confirmado=2)])
    publicador = _Publicador(falhas_ao_abrir=1)
    laco, metricas = _laco(ciclo, _Medicao(EstadoOutbox(2, 3.0)), publicador)

    asyncio.run(laco.passo())
    asyncio.run(laco.passo())
    asyncio.run(laco.passo())

    # Falhou na primeira, abriu na segunda e não reabre mais.
    assert publicador.aberturas == 2
    assert publicador.fechamentos == 1
    assert ciclo.chamadas == 3
    texto = metricas.exposicao().decode()
    assert 'cashflow_relay_falhas_publicacao_total{resultado="falha"} 2.0' in texto


def test_falha_do_ciclo_e_da_medicao_nao_derruba_o_laco() -> None:
    ciclo = _Ciclo([OSError("banco fora"), _resumo(1, confirmado=1)])
    medicao = _Medicao(OSError("banco fora"))
    laco, metricas = _laco(ciclo, medicao, _Publicador(falhas_ao_abrir=0))

    assert asyncio.run(laco.passo()) is False
    medicao.estado = EstadoOutbox(0, 0.0)
    asyncio.run(laco.passo())

    assert ciclo.chamadas == 2
    assert "cashflow_outbox_pendentes 0.0" in metricas.exposicao().decode()


def test_lote_cheio_repete_o_ciclo_sem_esperar() -> None:
    ciclo = _Ciclo([_resumo(10, confirmado=10), _resumo(3, confirmado=3)])
    laco, _ = _laco(ciclo, _Medicao(EstadoOutbox(0, 0.0)), _Publicador(0), lote=10)

    assert asyncio.run(laco.passo()) is True
    assert asyncio.run(laco.passo()) is False


def test_laco_encerra_quando_o_sinal_de_parada_chega() -> None:
    ciclo = _Ciclo([_resumo(0)])
    laco, _ = _laco(ciclo, _Medicao(EstadoOutbox(0, 0.0)), _Publicador(0))

    async def cenario() -> None:
        parar = asyncio.Event()
        tarefa = asyncio.create_task(laco.executar(parar))
        await asyncio.sleep(0.05)
        parar.set()
        await asyncio.wait_for(tarefa, 1)

    asyncio.run(cenario())
    assert ciclo.chamadas >= 2


def test_configuracao_padrao_usa_os_papeis_e_hosts_do_compose() -> None:
    configuracao = ConfiguracaoRelay.de_ambiente(AMBIENTE)

    assert "user=livro_relay" in configuracao.conninfo
    assert "host=pg-livro" in configuracao.conninfo
    # Senha com caracteres reservados de URL vai codificada.
    assert configuracao.url_rabbitmq == (
        "amqp://relay:senha%2Famqp%40de%3Ateste@rabbitmq:5672/"
    )
    assert configuracao.lease == timedelta(seconds=30)
    assert configuracao.politica.teto == timedelta(seconds=60)
    assert (configuracao.health_porta, configuracao.metricas_porta) == (8080, 9464)


def test_intervalo_lote_lease_e_espera_vem_do_ambiente() -> None:
    configuracao = ConfiguracaoRelay.de_ambiente(
        {
            **AMBIENTE,
            "RELAY_INTERVALO_SEGUNDOS": "0.5",
            "RELAY_LOTE": "20",
            "RELAY_LEASE_SEGUNDOS": "10",
            "RELAY_ESPERA_BASE_SEGUNDOS": "0.5",
            "RELAY_ESPERA_TETO_SEGUNDOS": "2",
            "RELAY_DONO": "relay-1",
        }
    )

    assert configuracao.intervalo_segundos == 0.5
    assert configuracao.lote == 20
    assert configuracao.lease == timedelta(seconds=10)
    assert configuracao.politica.base == timedelta(seconds=0.5)
    assert configuracao.politica.teto == timedelta(seconds=2)
    assert configuracao.dono == "relay-1"


@pytest.mark.parametrize("ausente", ["PG_LIVRO_SENHA_RELAY", "RABBITMQ_SENHA_RELAY"])
def test_senha_ausente_impede_a_subida(ausente: str) -> None:
    ambiente = {nome: valor for nome, valor in AMBIENTE.items() if nome != ausente}
    with pytest.raises(ValueError, match=ausente):
        ConfiguracaoRelay.de_ambiente(ambiente)


def test_espera_nao_positiva_e_recusada() -> None:
    with pytest.raises(ValueError, match="RELAY_INTERVALO_SEGUNDOS"):
        ConfiguracaoRelay.de_ambiente({**AMBIENTE, "RELAY_INTERVALO_SEGUNDOS": "0"})
