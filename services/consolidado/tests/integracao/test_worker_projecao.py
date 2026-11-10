"""Worker do consolidado contra RabbitMQ e PostgreSQL reais (004-T3, T4).

FR-001: cada lançamento é projetado uma única vez; duplicata não altera o
saldo. FR-002: ACK só depois do commit; falha antes do commit ou queda entre
o commit e o ACK devolvem a mensagem à fila, e a reentrega não duplica.
FR-003: conflito e mensagem ilegível vão para a quarentena, sem efeito no
saldo e sem descarte. FR-009: o checkpoint gravado na transação chega às
métricas do worker (regras de avanço em test_checkpoint_lacunas.py).

PostgreSQL descartável do consolidado com papéis e migrações reais (INFRA
9.1) e RabbitMQ descartável com a topologia, os usuários e as permissões do
compose (INFRA 9.2). As mensagens entram pelo exchange como o usuário
`relay`, com `message_id` igual ao `evento_id`, como o relay publica
(003-T2); o worker roda com o laço, o consumidor, o processador e a unidade
de trabalho reais, como `worker` e consolidado_worker. A partir da raiz do
repositório:

    uv run pytest services/consolidado/tests/integracao

Sem docker no PATH, o módulo é pulado com o motivo explícito.
"""

import asyncio
import itertools
import json
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import aio_pika
import psycopg
import pytest
from consolidado.adaptadores.postgres import UnidadeDeTrabalhoProjecaoPostgres
from consolidado.adaptadores.rabbitmq import (
    FILA,
    ConsumidorRabbitMQ,
    MensagemRecebida,
    ProcessadorMensagens,
)
from consolidado.aplicacao import (
    MOTIVO_CONFLITO_EVENTO,
    MOTIVO_CONFLITO_LANCAMENTO,
    MOTIVO_JSON_INVALIDO,
    Desfecho,
    MensagemEvento,
    ProjetarLancamento,
    TransacaoProjecao,
    UnidadeDeTrabalhoProjecao,
)
from consolidado.tests.integracao.consolidado_descartavel import (
    Consolidado,
    consolidado_descartavel,
)
from consolidado.worker.metricas import MetricasWorker
from consolidado.worker.principal import (
    ConfiguracaoWorker,
    LacoWorker,
    banco_pronto_de,
    fabrica_de_conexao,
)
from observabilidade import Metricas
from psycopg_pool import AsyncConnectionPool
from relay.tests.integracao.rabbitmq_descartavel import Broker, rabbitmq_descartavel

# Topologia de design 003 §4, a mesma de definicoes.modelo.json.
EXCHANGE = "cashflow.lancamentos"
ROUTING_KEY = "lancamento.registrado.v1"
TIMEOUT_SEGUNDOS = 60

# Uma data por teste: o saldo de um teste não soma ao de outro.
_DATAS = (date(2026, 1, 1) + timedelta(days=n) for n in itertools.count())
_POSICOES = itertools.count(1)

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker ausente no PATH"
)


@pytest.fixture(scope="module")
def banco() -> Iterator[Consolidado]:
    with consolidado_descartavel("worker-consolidado") as consolidado:
        yield consolidado


@pytest.fixture(scope="module")
def broker() -> Iterator[Broker]:
    with rabbitmq_descartavel("worker-consolidado") as rabbitmq:
        yield rabbitmq


@pytest.fixture(autouse=True)
def fila_vazia(broker: Broker) -> None:
    # Cada teste enxerga só as suas mensagens.
    asyncio.run(_esvaziar(broker))


@pytest.fixture
def data() -> date:
    return next(_DATAS)


# --- eventos e mensagens -------------------------------------------------


def _evento(
    data: date,
    tipo: str = "CREDITO",
    valor: str = "100.00",
    lancamento_id: uuid.UUID | None = None,
    posicao: int | None = None,
    evento_id: uuid.UUID | None = None,
) -> dict[str, object]:
    """Evento `LancamentoRegistrado` v1 como o livro grava (002 design §4)."""
    return {
        "evento_id": str(evento_id or uuid.uuid4()),
        "versao": 1,
        "lancamento_id": str(lancamento_id or uuid.uuid4()),
        "posicao": posicao or next(_POSICOES),
        "tipo": tipo,
        "valor": valor,
        "data_lancamento": data.isoformat(),
        "registrado_em": "2026-11-06T12:00:00+00:00",
    }


def _corpo(evento: dict[str, object]) -> bytes:
    return json.dumps(evento, separators=(",", ":")).encode()


async def _publicar(broker: Broker, *mensagens: tuple[bytes, str | None]) -> None:
    """Publica como `relay`, com confirms, cada (corpo, message_id)."""
    conexao = await aio_pika.connect(broker.url("relay"))
    async with conexao:
        canal = await conexao.channel(publisher_confirms=True)
        exchange = await canal.get_exchange(EXCHANGE, ensure=False)
        for corpo, message_id in mensagens:
            await exchange.publish(
                aio_pika.Message(
                    body=corpo,
                    content_type="application/json",
                    message_id=message_id,
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=ROUTING_KEY,
                mandatory=True,
            )


def _publicar_eventos(broker: Broker, *eventos: dict[str, object]) -> None:
    asyncio.run(_publicar(broker, *((_corpo(e), str(e["evento_id"])) for e in eventos)))


async def _esvaziar(broker: Broker) -> None:
    conexao = await aio_pika.connect(broker.url("worker"))
    async with conexao:
        canal = await conexao.channel()
        fila = await canal.get_queue(FILA, ensure=False)
        while (mensagem := await fila.get(no_ack=False, fail=False)) is not None:
            await mensagem.ack()


def _mensagens_na_fila(broker: Broker) -> int:
    async def contar() -> int:
        conexao = await aio_pika.connect(broker.url("worker"))
        async with conexao:
            canal = await conexao.channel()
            fila = await canal.declare_queue(FILA, passive=True)
            assert fila.declaration_result.message_count is not None
            return fila.declaration_result.message_count

    return asyncio.run(contar())


# --- worker ----------------------------------------------------------------


@dataclass
class Execucao:
    desfechos: list[Desfecho] = field(default_factory=list)
    falhas: int = 0
    metricas: Metricas = field(default_factory=Metricas)

    def contador(self, nome: str) -> float:
        valor = self.metricas.registro.get_sample_value(nome)
        assert valor is not None
        return valor


class _FalhaAntesDoCommit:
    """Unidade real que falha no fim da transação, antes do COMMIT."""

    def __init__(self, real: UnidadeDeTrabalhoProjecao, falhas: int) -> None:
        self._real = real
        self.falhas = falhas

    @asynccontextmanager
    async def _transacao(self) -> AsyncIterator[TransacaoProjecao]:
        async with self._real.transacao() as transacao:
            yield transacao
            if self.falhas:
                self.falhas -= 1
                raise RuntimeError("falha simulada antes do commit")

    def transacao(self) -> AbstractAsyncContextManager[TransacaoProjecao]:
        return self._transacao()


class _QuedaEntreCommitEAck:
    """Projeção real seguida de queda antes do ACK (kill entre commit e ACK)."""

    def __init__(self, real: ProjetarLancamento, quedas: int) -> None:
        self._real = real
        self.quedas = quedas

    async def executar(self, mensagem: MensagemEvento) -> Desfecho:
        desfecho = await self._real.executar(mensagem)
        if self.quedas:
            self.quedas -= 1
            raise RuntimeError("queda simulada entre commit e ACK")
        return desfecho


async def _executar_worker(
    banco: Consolidado,
    broker: Broker,
    esperadas: int,
    falhas_antes_do_commit: int = 0,
    quedas_antes_do_ack: int = 0,
    parar_na_falha: bool = False,
) -> Execucao:
    """Roda o laço real até `esperadas` mensagens confirmadas (ou a 1ª falha)."""
    execucao = Execucao()
    parar = asyncio.Event()
    pool: AsyncConnectionPool[psycopg.AsyncConnection[Any]] = AsyncConnectionPool(
        banco.conninfo("consolidado_worker"),
        min_size=1,
        max_size=2,
        kwargs={"autocommit": True},
        open=False,
    )
    await pool.open(wait=True)
    try:
        real = UnidadeDeTrabalhoProjecaoPostgres(fabrica_de_conexao(pool))
        unidade = _FalhaAntesDoCommit(real, falhas_antes_do_commit)
        observador = MetricasWorker(execucao.metricas)
        projetar = _QuedaEntreCommitEAck(
            ProjetarLancamento(unidade, observador), quedas_antes_do_ack
        )
        # A leitura do checkpoint na abertura da sessão usa a unidade real:
        # as falhas simuladas são das transações das mensagens.
        publicar_checkpoint = ProjetarLancamento(real, observador).publicar_checkpoint
        processador = ProcessadorMensagens(projetar, observador)

        async def tratar(mensagem: MensagemRecebida) -> None:
            try:
                execucao.desfechos.append(await processador.processar(mensagem))
            except Exception:
                execucao.falhas += 1
                if parar_na_falha:
                    parar.set()
                raise
            if len(execucao.desfechos) >= esperadas:
                parar.set()

        laco = LacoWorker(
            ConfiguracaoWorker(
                conninfo="nao-usado",
                url_rabbitmq="nao-usado",
                intervalo_reconexao_segundos=0.2,
            ),
            ConsumidorRabbitMQ(broker.url("worker"), prefetch=10),
            tratar,
            banco_pronto_de(pool, 2.0),
            publicar_checkpoint,
        )
        await asyncio.wait_for(laco.executar(parar), TIMEOUT_SEGUNDOS)
    finally:
        await pool.close()
    return execucao


def _rodar(
    banco: Consolidado, broker: Broker, esperadas: int, **opcoes: Any
) -> Execucao:
    return asyncio.run(_executar_worker(banco, broker, esperadas, **opcoes))


# --- leitura do banco --------------------------------------------------------


def _consultar(banco: Consolidado, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with psycopg.connect(banco.conninfo("consolidado_migracao")) as conexao:
        return conexao.execute(sql, params).fetchall()


def _saldo(banco: Consolidado, data: date) -> tuple[Decimal, Decimal, Decimal] | None:
    linhas = _consultar(
        banco,
        "SELECT total_creditos, total_debitos, saldo_dia FROM saldo_diario"
        " WHERE data = %s",
        data,
    )
    if not linhas:
        return None
    [(creditos, debitos, saldo)] = linhas
    return creditos, debitos, saldo


def _quarentena(banco: Consolidado, evento_id: str) -> list[tuple[Any, ...]]:
    return _consultar(
        banco,
        "SELECT motivo, payload_bruto FROM quarentena WHERE evento_id = %s ORDER BY id",
        uuid.UUID(evento_id),
    )


def _inbox(banco: Consolidado, evento_id: str) -> int:
    [(quantidade,)] = _consultar(
        banco, "SELECT count(*) FROM inbox WHERE evento_id = %s", uuid.UUID(evento_id)
    )
    assert isinstance(quantidade, int)
    return quantidade


# --- FR-001: projeção única ---------------------------------------------------


def test_credito_soma_e_debito_subtrai_no_saldo_do_dia(
    banco: Consolidado, broker: Broker, data: date
) -> None:
    credito = _evento(data, "CREDITO", "100.00")
    debito = _evento(data, "DEBITO", "30.25")
    _publicar_eventos(broker, credito, debito)

    execucao = _rodar(banco, broker, esperadas=2)

    assert execucao.desfechos == [Desfecho.APLICADO, Desfecho.APLICADO]
    assert _saldo(banco, data) == (
        Decimal("100.00"),
        Decimal("30.25"),
        Decimal("69.75"),
    )
    marcadores = _consultar(
        banco,
        "SELECT lancamento_id, posicao FROM marcador_inclusao"
        " WHERE lancamento_id = ANY(%s) ORDER BY posicao",
        [uuid.UUID(str(e["lancamento_id"])) for e in (credito, debito)],
    )
    assert marcadores == [
        (uuid.UUID(str(e["lancamento_id"])), e["posicao"]) for e in (credito, debito)
    ]
    assert _mensagens_na_fila(broker) == 0


def test_mensagem_duplicada_nao_altera_o_saldo(
    banco: Consolidado, broker: Broker, data: date
) -> None:
    evento = _evento(data, "CREDITO", "40.00")
    _publicar_eventos(broker, evento, evento, evento)

    execucao = _rodar(banco, broker, esperadas=3)

    assert execucao.desfechos == [
        Desfecho.APLICADO,
        Desfecho.EVENTO_DUPLICADO,
        Desfecho.EVENTO_DUPLICADO,
    ]
    assert _saldo(banco, data) == (Decimal("40.00"), Decimal("0.00"), Decimal("40.00"))
    assert _inbox(banco, str(evento["evento_id"])) == 1
    assert _quarentena(banco, str(evento["evento_id"])) == []
    assert _mensagens_na_fila(broker) == 0


def test_republicacao_do_mesmo_lancamento_com_outro_evento_nao_altera_o_saldo(
    banco: Consolidado, broker: Broker, data: date
) -> None:
    original = _evento(data, "DEBITO", "12.50")
    # Replay (003 TD-004): mesmo lançamento e conteúdo, outro `evento_id`.
    republicado = {**original, "evento_id": str(uuid.uuid4())}
    _publicar_eventos(broker, original, republicado)

    execucao = _rodar(banco, broker, esperadas=2)

    assert execucao.desfechos == [Desfecho.APLICADO, Desfecho.LANCAMENTO_DUPLICADO]
    assert _saldo(banco, data) == (Decimal("0.00"), Decimal("12.50"), Decimal("-12.50"))
    # O evento republicado fica na inbox: sua própria reentrega é duplicata.
    assert _inbox(banco, str(republicado["evento_id"])) == 1
    assert _quarentena(banco, str(republicado["evento_id"])) == []


# --- FR-003: quarentena -------------------------------------------------------


def test_evento_repetido_com_outro_conteudo_vai_para_quarentena(
    banco: Consolidado, broker: Broker, data: date
) -> None:
    original = _evento(data, "CREDITO", "10.00")
    adulterado = {**original, "valor": "99.00"}
    _publicar_eventos(broker, original, adulterado)

    execucao = _rodar(banco, broker, esperadas=2)

    assert execucao.desfechos == [
        Desfecho.APLICADO,
        Desfecho.QUARENTENA_CONFLITO_EVENTO,
    ]
    assert _saldo(banco, data) == (Decimal("10.00"), Decimal("0.00"), Decimal("10.00"))
    assert _quarentena(banco, str(original["evento_id"])) == [
        (MOTIVO_CONFLITO_EVENTO, _corpo(adulterado))
    ]
    assert execucao.contador("cashflow_quarentena_total") == 1
    assert _mensagens_na_fila(broker) == 0


def test_lancamento_repetido_com_outro_conteudo_vai_para_quarentena(
    banco: Consolidado, broker: Broker, data: date
) -> None:
    original = _evento(data, "CREDITO", "10.00")
    divergente = {**original, "evento_id": str(uuid.uuid4()), "tipo": "DEBITO"}
    _publicar_eventos(broker, original, divergente)

    execucao = _rodar(banco, broker, esperadas=2)

    assert execucao.desfechos == [
        Desfecho.APLICADO,
        Desfecho.QUARENTENA_CONFLITO_LANCAMENTO,
    ]
    assert _saldo(banco, data) == (Decimal("10.00"), Decimal("0.00"), Decimal("10.00"))
    assert _quarentena(banco, str(divergente["evento_id"])) == [
        (MOTIVO_CONFLITO_LANCAMENTO, _corpo(divergente))
    ]


def test_mensagem_ilegivel_vai_para_quarentena_com_o_payload_bruto(
    banco: Consolidado, broker: Broker
) -> None:
    identificador = str(uuid.uuid4())
    corpo = b"\xff isto n\xe3o \xe9 JSON"
    asyncio.run(_publicar(broker, (corpo, identificador)))

    execucao = _rodar(banco, broker, esperadas=1)

    assert execucao.desfechos == [Desfecho.QUARENTENA_ILEGIVEL]
    assert _quarentena(banco, identificador) == [(MOTIVO_JSON_INVALIDO, corpo)]
    assert execucao.contador("cashflow_quarentena_total") == 1
    assert _mensagens_na_fila(broker) == 0


def test_evento_fora_do_contrato_vai_para_quarentena_sem_efeito_no_saldo(
    banco: Consolidado, broker: Broker, data: date
) -> None:
    invalido = _evento(data, "CREDITO", "-5.00")
    _publicar_eventos(broker, invalido)

    execucao = _rodar(banco, broker, esperadas=1)

    assert execucao.desfechos == [Desfecho.QUARENTENA_ILEGIVEL]
    [(motivo, payload)] = _quarentena(banco, str(invalido["evento_id"]))
    assert motivo == "evento inválido: valor: deve ser positivo"
    assert payload == _corpo(invalido)
    assert _saldo(banco, data) is None
    assert _inbox(banco, str(invalido["evento_id"])) == 0


# --- FR-002: ACK só após commit ----------------------------------------------


def test_falha_antes_do_commit_nao_confirma_e_a_mensagem_fica_na_fila(
    banco: Consolidado, broker: Broker, data: date
) -> None:
    evento = _evento(data, "CREDITO", "70.00")
    _publicar_eventos(broker, evento)

    execucao = _rodar(
        banco, broker, esperadas=1, falhas_antes_do_commit=1, parar_na_falha=True
    )

    assert (execucao.falhas, execucao.desfechos) == (1, [])
    assert _saldo(banco, data) is None
    assert _inbox(banco, str(evento["evento_id"])) == 0
    # O canal fechou sem ACK: o broker devolveu a mensagem à fila.
    assert _aguardar_mensagens(broker, 1)

    retomada = _rodar(banco, broker, esperadas=1)

    assert retomada.desfechos == [Desfecho.APLICADO]
    assert retomada.contador("cashflow_worker_reentregas_total") == 1
    assert _saldo(banco, data) == (Decimal("70.00"), Decimal("0.00"), Decimal("70.00"))
    assert _mensagens_na_fila(broker) == 0


def test_queda_entre_commit_e_ack_gera_reentrega_sem_duplicar_o_saldo(
    banco: Consolidado, broker: Broker, data: date
) -> None:
    evento = _evento(data, "DEBITO", "8.00")
    _publicar_eventos(broker, evento)

    execucao = _rodar(banco, broker, esperadas=1, quedas_antes_do_ack=1)

    # A 1ª entrega gravou e caiu antes do ACK; a reentrega é duplicata.
    assert execucao.falhas == 1
    assert execucao.desfechos == [Desfecho.EVENTO_DUPLICADO]
    assert execucao.contador("cashflow_worker_reentregas_total") == 1
    assert _saldo(banco, data) == (Decimal("0.00"), Decimal("8.00"), Decimal("-8.00"))
    assert _inbox(banco, str(evento["evento_id"])) == 1
    assert _mensagens_na_fila(broker) == 0


def _aguardar_mensagens(broker: Broker, quantidade: int) -> bool:
    # O broker devolve as mensagens logo após o fechamento do canal.
    limite = time.monotonic() + 10
    while time.monotonic() < limite:
        if _mensagens_na_fila(broker) == quantidade:
            return True
        time.sleep(0.2)
    return False


# --- FR-009: checkpoint nas métricas -----------------------------------------


def _estado_checkpoint(banco: Consolidado) -> tuple[str, int, int]:
    [(geracao, posicao, lacunas)] = _consultar(
        banco,
        "SELECT k.geracao_id, k.posicao_continua,"
        " (SELECT count(*) FROM lacuna l WHERE l.geracao_id = k.geracao_id)"
        " FROM controle_projecao c"
        " JOIN checkpoint k ON k.geracao_id = c.geracao_ativa_id",
    )
    return str(geracao), posicao, lacunas


def test_worker_publica_checkpoint_e_lacunas_da_geracao_ativa_nas_metricas(
    banco: Consolidado, broker: Broker, data: date
) -> None:
    # As posições dos outros testes do módulo deixam o checkpoint onde
    # estiver; a métrica tem de repetir o que a transação gravou.
    _publicar_eventos(broker, _evento(data, "CREDITO", "5.00"))

    execucao = _rodar(banco, broker, esperadas=1)

    assert execucao.desfechos == [Desfecho.APLICADO]
    geracao, posicao, lacunas = _estado_checkpoint(banco)
    rotulos = {"geracao": geracao}
    registro = execucao.metricas.registro
    assert registro.get_sample_value("cashflow_checkpoint_posicao", rotulos) == posicao
    assert registro.get_sample_value("cashflow_lacunas", rotulos) == lacunas
