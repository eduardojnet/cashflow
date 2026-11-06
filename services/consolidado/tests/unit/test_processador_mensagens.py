"""Processador de mensagens do worker sem broker (004-T3; FR-002, TD-002).

Confere a ordem que importa: o ACK só sai depois de a transação terminar, e
nenhuma falha antes disso confirma a mensagem. Confere também a leitura do
corpo e as métricas de reentrega e quarentena.
"""

import asyncio
import json
import logging
import uuid

import pytest
from consolidado.adaptadores.logs import FormatadorJson
from consolidado.adaptadores.rabbitmq import ProcessadorMensagens, decodificar
from consolidado.aplicacao import Desfecho, EstadoCheckpoint, MensagemEvento
from consolidado.worker.metricas import MetricasWorker
from observabilidade import Metricas


class _Mensagem:
    def __init__(
        self, corpo: bytes, redelivered: bool = False, eventos: list[str] | None = None
    ) -> None:
        self.body = corpo
        self.message_id: str | None = str(uuid.uuid4())
        self.redelivered: bool | None = redelivered
        self.eventos = eventos if eventos is not None else []
        self.falha_no_ack: Exception | None = None

    async def ack(self, multiple: bool = False) -> None:
        if self.falha_no_ack is not None:
            raise self.falha_no_ack
        self.eventos.append("ack")


class _Projetar:
    def __init__(self, eventos: list[str], resposta: Desfecho | Exception) -> None:
        self.eventos = eventos
        self.resposta = resposta
        self.recebidas: list[MensagemEvento] = []

    async def executar(self, mensagem: MensagemEvento) -> Desfecho:
        self.recebidas.append(mensagem)
        if isinstance(self.resposta, Exception):
            self.eventos.append("rollback")
            raise self.resposta
        self.eventos.append("commit")
        return self.resposta


def _cenario(
    resposta: Desfecho | Exception, redelivered: bool = False
) -> tuple[ProcessadorMensagens, _Mensagem, _Projetar, Metricas]:
    eventos: list[str] = []
    metricas = Metricas()
    projetar = _Projetar(eventos, resposta)
    processador = ProcessadorMensagens(projetar, MetricasWorker(metricas))
    mensagem = _Mensagem(b'{"evento_id": "x"}', redelivered, eventos)
    return processador, mensagem, projetar, metricas


def _amostra(metricas: Metricas, nome: str) -> float:
    valor = metricas.registro.get_sample_value(nome)
    assert valor is not None
    return valor


@pytest.mark.parametrize("desfecho", list(Desfecho))
def test_ack_so_depois_da_transacao_terminar(desfecho: Desfecho) -> None:
    processador, mensagem, _, _ = _cenario(desfecho)

    resultado = asyncio.run(processador.processar(mensagem))

    assert resultado is desfecho
    assert mensagem.eventos == ["commit", "ack"]


def test_falha_na_transacao_propaga_sem_ack() -> None:
    processador, mensagem, _, metricas = _cenario(ConnectionError("banco fora"))

    with pytest.raises(ConnectionError):
        asyncio.run(processador.processar(mensagem))

    assert mensagem.eventos == ["rollback"]
    assert _amostra(metricas, "cashflow_quarentena_total") == 0


def test_falha_no_ack_propaga_para_a_sessao_ser_reaberta() -> None:
    processador, mensagem, _, _ = _cenario(Desfecho.APLICADO)
    mensagem.falha_no_ack = ConnectionError("canal fechado")

    with pytest.raises(ConnectionError):
        asyncio.run(processador.processar(mensagem))

    assert mensagem.eventos == ["commit"]


def test_reentrega_conta_na_metrica_mesmo_quando_a_transacao_falha() -> None:
    processador, mensagem, _, metricas = _cenario(
        ConnectionError("banco fora"), redelivered=True
    )

    with pytest.raises(ConnectionError):
        asyncio.run(processador.processar(mensagem))

    assert _amostra(metricas, "cashflow_worker_reentregas_total") == 1


def test_primeira_entrega_nao_conta_como_reentrega() -> None:
    processador, mensagem, _, metricas = _cenario(Desfecho.APLICADO)

    asyncio.run(processador.processar(mensagem))

    assert _amostra(metricas, "cashflow_worker_reentregas_total") == 0


@pytest.mark.parametrize("desfecho", list(Desfecho))
def test_quarentena_conta_uma_vez_por_mensagem_quarentenada(desfecho: Desfecho) -> None:
    processador, mensagem, _, metricas = _cenario(desfecho)

    asyncio.run(processador.processar(mensagem))

    esperado = 1 if desfecho.quarentena else 0
    assert _amostra(metricas, "cashflow_quarentena_total") == esperado


def test_processador_entrega_corpo_e_message_id_ao_caso_de_uso() -> None:
    processador, mensagem, projetar, _ = _cenario(Desfecho.APLICADO)

    asyncio.run(processador.processar(mensagem))

    [recebida] = projetar.recebidas
    assert recebida.corpo == mensagem.body
    assert recebida.identificador == mensagem.message_id
    assert recebida.dados == {"evento_id": "x"}


@pytest.mark.parametrize(
    "corpo",
    [
        b"",
        b"nao e json",
        b"\xff\xfe",
        b"[1, 2]",
        b'"texto"',
        b"10",
        b"null",
        b"[" * 100_000,
    ],
    ids=[
        "vazio",
        "texto",
        "utf8-invalido",
        "lista",
        "string",
        "numero",
        "null",
        "aninhamento-profundo",
    ],
)
def test_corpo_que_nao_e_objeto_json_e_ilegivel(corpo: bytes) -> None:
    mensagem = decodificar(corpo, "id")

    assert mensagem.dados is None
    assert (mensagem.corpo, mensagem.identificador) == (corpo, "id")


def test_objeto_json_em_utf8_e_lido() -> None:
    corpo = json.dumps({"evento_id": "é"}, ensure_ascii=False).encode()

    assert decodificar(corpo, None).dados == {"evento_id": "é"}


def test_log_do_processamento_nao_repete_o_corpo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    processador, mensagem, _, _ = _cenario(Desfecho.QUARENTENA_CONFLITO_EVENTO)
    mensagem.body = b'{"valor": "987654.32"}'

    with caplog.at_level(logging.INFO, logger="consolidado.worker.mensagens"):
        asyncio.run(processador.processar(mensagem))

    [registro] = caplog.records
    linha = json.loads(FormatadorJson().format(registro))
    assert linha["nivel"] == "WARNING"
    assert linha["desfecho"] == "quarentena_conflito_evento"
    assert linha["evento_id"] == mensagem.message_id
    assert "987654" not in json.dumps(linha)


def test_checkpoint_e_lacunas_viram_medidores_por_geracao() -> None:
    metricas = Metricas()
    observador = MetricasWorker(metricas)

    # Os medidores por geração só aparecem com a primeira leitura (004-T4).
    assert metricas.registro.get_sample_value(
        "cashflow_checkpoint_posicao", {"geracao": "1"}
    ) is None
    observador.checkpoint(EstadoCheckpoint(geracao_id=1, posicao_continua=2, lacunas=1))
    observador.checkpoint(EstadoCheckpoint(geracao_id=1, posicao_continua=4, lacunas=0))

    rotulos = {"geracao": "1"}
    assert metricas.registro.get_sample_value(
        "cashflow_checkpoint_posicao", rotulos
    ) == 4
    assert metricas.registro.get_sample_value("cashflow_lacunas", rotulos) == 0
