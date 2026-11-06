"""Laço, configuração, ready e logs do worker sem infraestrutura (004-T3).

O laço não para por falha de banco ou de broker (design 004 §8): com o banco
fora, o consumo nem começa; falha na sessão fecha o consumidor, para o broker
devolver o que não foi confirmado, e a sessão seguinte recomeça.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import pytest
from consolidado.adaptadores.logs import REDIGIDO, FormatadorJson
from consolidado.adaptadores.rabbitmq.consumidor import Tratar
from consolidado.worker.principal import (
    BancoPronto,
    ConfiguracaoWorker,
    LacoWorker,
    prontidao_de,
)
from consolidado.worker.saude import ServidorSaude

AMBIENTE = {
    "PG_CONSOLIDADO_SENHA_WORKER": "senha-pg-de-teste",
    "RABBITMQ_SENHA_WORKER": "senha/amqp@de:teste",
}
CONFIGURACAO = ConfiguracaoWorker(
    conninfo="nao-usado", url_rabbitmq="nao-usado", intervalo_reconexao_segundos=0.01
)


class _Consumidor:
    """Consumidor falso: cada sessão segue o roteiro dado, em ordem.

    - "parar": sinaliza o encerramento do processo (SIGTERM) e espera a
      sessão ser encerrada;
    - "aguardar": consome até a sessão ser encerrada pelo laço;
    - "falha_ao_consumir" e "canal_fechado": a sessão termina sozinha;
    - "falha_ao_abrir": a abertura falha antes de consumir.
    """

    def __init__(self, roteiro: list[str]) -> None:
        self._roteiro = roteiro
        self.aberturas = 0
        self.fechamentos = 0
        self.sessoes = 0
        self.encerradas = 0
        self.parar_processo = asyncio.Event()
        self._conectado = False

    async def abrir(self) -> None:
        self.aberturas += 1
        if self._passo() == "falha_ao_abrir":
            raise ConnectionError("broker fora")
        self._conectado = True

    async def fechar(self) -> None:
        self.fechamentos += 1
        self._conectado = False

    def conectado(self) -> bool:
        return self._conectado

    async def consumir(self, tratar: Tratar, parar: asyncio.Event) -> None:
        self.sessoes += 1
        passo = self._roteiro.pop(0)
        if passo == "falha_ao_consumir":
            raise RuntimeError("falha simulada")
        if passo == "parar":
            self.parar_processo.set()
        if passo in ("parar", "aguardar"):
            await parar.wait()
            self.encerradas += 1
        # "canal_fechado": o iterador terminou sem `parar`.

    def _passo(self) -> str:
        if self._roteiro and self._roteiro[0] == "falha_ao_abrir":
            return self._roteiro.pop(0)
        return ""


async def _nada(_: object) -> None:
    return None


def _banco(respostas: list[bool]) -> tuple[list[int], BancoPronto]:
    chamadas = [0]

    async def pronto() -> bool:
        chamadas[0] += 1
        return respostas.pop(0) if respostas else True

    return chamadas, pronto


def _executar(
    consumidor: _Consumidor,
    banco_respostas: list[bool],
    preparar: Callable[[], Awaitable[object]] | None = None,
) -> list[int]:
    chamadas, pronto = _banco(banco_respostas)
    laco = LacoWorker(CONFIGURACAO, consumidor, _nada, pronto, preparar)

    async def cenario() -> None:
        await asyncio.wait_for(laco.executar(consumidor.parar_processo), 5)

    asyncio.run(cenario())
    return chamadas


def test_banco_fora_pausa_o_consumo_ate_responder() -> None:
    consumidor = _Consumidor(["parar"])

    chamadas = _executar(consumidor, [False, False, True])

    assert chamadas == [3]
    assert (consumidor.aberturas, consumidor.sessoes) == (1, 1)


def test_queda_do_banco_durante_a_sessao_encerra_o_consumo() -> None:
    # Fila parada: nenhuma mensagem revelaria a queda; a vigia a detecta.
    consumidor = _Consumidor(["aguardar", "parar"])

    chamadas = _executar(consumidor, [True, False, False, True])

    assert (consumidor.sessoes, consumidor.encerradas) == (2, 2)
    assert consumidor.fechamentos == 2
    assert chamadas[0] >= 4


def test_parar_encerra_a_sessao_e_fecha_o_consumidor() -> None:
    consumidor = _Consumidor(["parar"])

    _executar(consumidor, [])

    assert (consumidor.sessoes, consumidor.encerradas) == (1, 1)
    assert consumidor.fechamentos == 1
    assert not consumidor.conectado()


def test_falha_na_sessao_fecha_o_consumidor_e_recomeca() -> None:
    consumidor = _Consumidor(["falha_ao_consumir", "canal_fechado", "parar"])

    _executar(consumidor, [])

    assert consumidor.sessoes == 3
    assert consumidor.fechamentos == 3


def test_broker_fora_na_abertura_e_tentado_de_novo() -> None:
    consumidor = _Consumidor(["falha_ao_abrir", "falha_ao_abrir", "parar"])

    _executar(consumidor, [])

    assert (consumidor.aberturas, consumidor.sessoes) == (3, 1)
    assert consumidor.fechamentos == 3


def test_configuracao_le_o_ambiente_com_padroes() -> None:
    configuracao = ConfiguracaoWorker.de_ambiente(AMBIENTE)

    assert "host=pg-consolidado" in configuracao.conninfo
    assert "user=consolidado_worker" in configuracao.conninfo
    assert "dbname=consolidado" in configuracao.conninfo
    # Senha com caracteres reservados vai codificada na URL.
    assert configuracao.url_rabbitmq == (
        "amqp://worker:senha%2Famqp%40de%3Ateste@rabbitmq:5672/"
    )
    assert configuracao.prefetch == 10
    assert (configuracao.health_porta, configuracao.metricas_porta) == (8080, 9464)


@pytest.mark.parametrize(
    "faltando", ["PG_CONSOLIDADO_SENHA_WORKER", "RABBITMQ_SENHA_WORKER"]
)
def test_configuracao_exige_as_senhas(faltando: str) -> None:
    ambiente = {k: v for k, v in AMBIENTE.items() if k != faltando}

    with pytest.raises(ValueError, match=faltando):
        ConfiguracaoWorker.de_ambiente(ambiente)


@pytest.mark.parametrize(
    "variavel", ["WORKER_PREFETCH", "WORKER_INTERVALO_RECONEXAO_SEGUNDOS"]
)
def test_configuracao_recusa_valor_nao_positivo(variavel: str) -> None:
    with pytest.raises(ValueError, match=variavel):
        ConfiguracaoWorker.de_ambiente({**AMBIENTE, variavel: "0"})


@pytest.mark.parametrize(
    ("banco", "rabbitmq", "status"),
    [(True, True, 200), (False, True, 503), (True, False, 503)],
)
def test_ready_exige_banco_e_rabbitmq(banco: bool, rabbitmq: bool, status: int) -> None:
    consumidor = _Consumidor([])
    consumidor._conectado = rabbitmq

    async def pronto() -> bool:
        return banco

    async def cenario() -> tuple[int, dict[str, object]]:
        servidor = ServidorSaude(prontidao_de(pronto, consumidor))
        await servidor.iniciar(0, endereco="127.0.0.1")
        try:
            leitor, escritor = await asyncio.open_connection(
                "127.0.0.1", servidor.porta
            )
            escritor.write(b"GET /health/ready HTTP/1.1\r\nHost: x\r\n\r\n")
            await escritor.drain()
            resposta = await leitor.read()
            escritor.close()
        finally:
            await servidor.parar()
        cabecalho, corpo = resposta.split(b"\r\n\r\n", 1)
        return int(cabecalho.split(b" ")[1]), json.loads(corpo)

    codigo, corpo = asyncio.run(cenario())

    assert codigo == status
    assert corpo["verificacoes"] == {"banco": banco, "rabbitmq": rabbitmq}


def test_log_redige_campos_e_credenciais_sensiveis() -> None:
    texto = "conexão amqp://worker:s3nh4@rabbitmq"
    registro = logging.LogRecord(
        "consolidado", logging.WARNING, "", 0, texto, None, None
    )
    extra = {"valor": "10.00", "saldo_dia": "5.00", "evento_id": "e1"}
    for nome, conteudo in extra.items():
        setattr(registro, nome, conteudo)

    linha = json.loads(FormatadorJson().format(registro))

    assert "s3nh4" not in linha["mensagem"]
    assert (linha["valor"], linha["saldo_dia"]) == (REDIGIDO, REDIGIDO)
    assert linha["evento_id"] == "e1"


def test_preparar_roda_a_cada_sessao_com_o_banco_pronto_antes_de_consumir() -> None:
    # O worker publica ali o checkpoint nas métricas (004-T4).
    consumidor = _Consumidor(["canal_fechado", "parar"])
    ordem: list[tuple[str, int]] = []

    async def preparar() -> None:
        ordem.append(("preparar", consumidor.aberturas))

    _executar(consumidor, [False, True, True], preparar)

    # Banco fora na 1ª tentativa: nada roda; depois, uma vez por sessão,
    # sempre antes de abrir o consumidor.
    assert ordem == [("preparar", 0), ("preparar", 1)]


def test_falha_em_preparar_encerra_a_sessao_sem_consumir_e_recomeca() -> None:
    consumidor = _Consumidor(["parar"])
    falhas = [RuntimeError("banco caiu")]

    async def preparar() -> None:
        if falhas:
            raise falhas.pop()

    _executar(consumidor, [], preparar)

    assert (consumidor.aberturas, consumidor.sessoes) == (1, 1)
    assert consumidor.fechamentos == 2
