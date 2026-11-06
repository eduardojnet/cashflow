"""Worker multigeração e barreira contra RabbitMQ e PostgreSQL reais (005-T2).

TD-001: com uma candidata `construindo`, cada mensagem altera a ativa e a
candidata na mesma transação, ou nenhuma das duas; na republicação do
replay, a ativa reconhece a duplicata e a candidata aplica (FR-007).

TD-002 (FR-004): com a barreira solicitada, o worker termina a mensagem em
curso, para de consumir e só então confirma a pausa em `estado_consumidor`
(`pausado = true`, `visto_em` renovado a cada intervalo); com a barreira
liberada, retoma e processa o que ficou na fila.

PostgreSQL descartável do consolidado com papéis e migrações reais (INFRA
9.1) e RabbitMQ descartável com a topologia, os usuários e as permissões do
compose (INFRA 9.2). O worker roda com o laço, o consumidor, o processador,
a unidade de trabalho e a barreira reais, como `worker` e
consolidado_worker. A candidata e a barreira são manipuladas como
consolidado_operacao, o papel do coordenador (migração 0002). A partir da
raiz do repositório:

    uv run pytest services/consolidado/tests/integracao

Sem docker no PATH, o módulo é pulado com o motivo explícito.
"""

import asyncio
import itertools
import json
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import aio_pika
import psycopg
import pytest
from consolidado.adaptadores.postgres import (
    ControleBarreiraPostgres,
    UnidadeDeTrabalhoProjecaoPostgres,
)
from consolidado.adaptadores.rabbitmq import (
    FILA,
    ConsumidorRabbitMQ,
    MensagemRecebida,
    ProcessadorMensagens,
)
from consolidado.aplicacao import (
    BarreiraConsumidor,
    BarreiraSolicitada,
    Desfecho,
    ProjetarLancamento,
    TransacaoProjecao,
    UnidadeDeTrabalhoProjecao,
)
from consolidado.worker.metricas import MetricasWorker
from consolidado.worker.principal import (
    ConfiguracaoWorker,
    LacoWorker,
    banco_pronto_de,
    fabrica_de_conexao,
)
from consolidado_descartavel import Consolidado, consolidado_descartavel
from observabilidade import Metricas
from psycopg_pool import AsyncConnectionPool
from rabbitmq_descartavel import Broker, rabbitmq_descartavel

# Topologia de design 003 §4, a mesma de definicoes.modelo.json.
EXCHANGE = "cashflow.lancamentos"
ROUTING_KEY = "lancamento.registrado.v1"
INTERVALO_SEGUNDOS = 0.2
TIMEOUT_SEGUNDOS = 30

# Uma data por teste: o saldo de um teste não soma ao de outro.
_DATAS = (date(2027, 1, 1) + timedelta(days=n) for n in itertools.count())
_POSICOES = itertools.count(1)

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker ausente no PATH"
)


@pytest.fixture(scope="module")
def banco() -> Iterator[Consolidado]:
    with consolidado_descartavel("worker-multigeracao") as consolidado:
        yield consolidado


@pytest.fixture(scope="module")
def broker() -> Iterator[Broker]:
    with rabbitmq_descartavel("worker-multigeracao") as rabbitmq:
        yield rabbitmq


@pytest.fixture(autouse=True)
def fila_vazia_e_barreira_liberada(
    banco: Consolidado, broker: Broker
) -> Iterator[None]:
    # Cada teste enxerga só as suas mensagens e começa sem barreira.
    asyncio.run(_esvaziar(broker))
    yield
    asyncio.run(_barreira(banco, "liberada"))


@pytest.fixture
def data() -> date:
    return next(_DATAS)


@pytest.fixture
def consumidor_id() -> str:
    return f"worker-{uuid.uuid4().hex[:8]}"


def _criar_candidata(banco: Consolidado) -> int:
    """Candidata `construindo` com checkpoint 0, como o coordenador (005-T3)."""
    with psycopg.connect(banco.conninfo("consolidado_operacao")) as conexao:
        linha = conexao.execute(
            "INSERT INTO geracao (estado) VALUES ('construindo') RETURNING id"
        ).fetchone()
        assert linha is not None
        geracao_id: int = linha[0]
        conexao.execute(
            "INSERT INTO checkpoint (geracao_id) VALUES (%s)", (geracao_id,)
        )
    return geracao_id


@pytest.fixture
def candidata(banco: Consolidado) -> Iterator[int]:
    geracao_id = _criar_candidata(banco)
    yield geracao_id
    # Cancelar (design 005 §3): a próxima reconstrução pode começar.
    with psycopg.connect(banco.conninfo("consolidado_operacao")) as conexao:
        conexao.execute(
            "UPDATE geracao SET estado = 'descartada' WHERE id = %s", (geracao_id,)
        )


# --- eventos e mensagens -------------------------------------------------


def _evento(
    data: date, tipo: str = "CREDITO", valor: str = "100.00"
) -> dict[str, object]:
    """Evento `LancamentoRegistrado` v1 como o livro grava (002 design §4)."""
    return {
        "evento_id": str(uuid.uuid4()),
        "versao": 1,
        "lancamento_id": str(uuid.uuid4()),
        "posicao": next(_POSICOES),
        "tipo": tipo,
        "valor": valor,
        "data_lancamento": data.isoformat(),
        "registrado_em": "2026-11-06T12:00:00+00:00",
    }


async def _publicar(broker: Broker, *eventos: dict[str, object]) -> None:
    """Publica como `relay`, com confirms e `message_id` = `evento_id`."""
    conexao = await aio_pika.connect(broker.url("relay"))
    async with conexao:
        canal = await conexao.channel(publisher_confirms=True)
        exchange = await canal.get_exchange(EXCHANGE, ensure=False)
        for evento in eventos:
            await exchange.publish(
                aio_pika.Message(
                    body=json.dumps(evento, separators=(",", ":")).encode(),
                    content_type="application/json",
                    message_id=str(evento["evento_id"]),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=ROUTING_KEY,
                mandatory=True,
            )


async def _esvaziar(broker: Broker) -> None:
    conexao = await aio_pika.connect(broker.url("worker"))
    async with conexao:
        canal = await conexao.channel()
        fila = await canal.get_queue(FILA, ensure=False)
        while (mensagem := await fila.get(no_ack=False, fail=False)) is not None:
            await mensagem.ack()


async def _fila(broker: Broker) -> tuple[int, int]:
    """Mensagens prontas e consumidores inscritos na fila."""
    conexao = await aio_pika.connect(broker.url("worker"))
    async with conexao:
        canal = await conexao.channel()
        fila = await canal.declare_queue(FILA, passive=True)
        resultado = fila.declaration_result
        assert resultado.message_count is not None
        assert resultado.consumer_count is not None
        return resultado.message_count, resultado.consumer_count


# --- banco -----------------------------------------------------------------


async def _sql(
    banco: Consolidado, sql: str, *params: object, papel: str = "consolidado_migracao"
) -> list[tuple[Any, ...]]:
    conexao = await psycopg.AsyncConnection.connect(
        banco.conninfo(papel), autocommit=True
    )
    async with conexao:
        cursor = await conexao.execute(sql, params)
        return await cursor.fetchall() if cursor.description else []


async def _barreira(banco: Consolidado, estado: str) -> datetime:
    """Ergue ou libera a barreira como o coordenador; devolve o instante."""
    [(instante,)] = await _sql(
        banco,
        "UPDATE controle_projecao SET barreira = %s, versao = versao + 1"
        " WHERE id = 1 RETURNING clock_timestamp()",
        estado,
        papel="consolidado_operacao",
    )
    assert isinstance(instante, datetime)
    return instante


async def _consumidor(
    banco: Consolidado, consumidor_id: str
) -> tuple[bool, datetime] | None:
    """`pausado` e `visto_em`, lidos como o coordenador lê (design 005 §4)."""
    linhas = await _sql(
        banco,
        "SELECT pausado, visto_em FROM estado_consumidor WHERE consumidor_id = %s",
        consumidor_id,
        papel="consolidado_operacao",
    )
    if not linhas:
        return None
    [(pausado, visto_em)] = linhas
    return pausado, visto_em


async def _pausa_confirmada(
    banco: Consolidado, consumidor_id: str, desde: datetime
) -> bool:
    estado = await _consumidor(banco, consumidor_id)
    return estado is not None and estado[0] and estado[1] > desde


async def _ativa(banco: Consolidado) -> int:
    [(geracao_id,)] = await _sql(
        banco, "SELECT geracao_ativa_id FROM controle_projecao WHERE id = 1"
    )
    assert isinstance(geracao_id, int)
    return geracao_id


async def _saldos(
    banco: Consolidado, data: date
) -> dict[int, tuple[Decimal, Decimal, Decimal]]:
    linhas = await _sql(
        banco,
        "SELECT geracao_id, total_creditos, total_debitos, saldo_dia"
        " FROM saldo_diario WHERE data = %s",
        data,
    )
    return {linha[0]: (linha[1], linha[2], linha[3]) for linha in linhas}


async def _aguardar(
    condicao: Callable[[], Awaitable[bool]], timeout: float = TIMEOUT_SEGUNDOS
) -> None:
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if await condicao():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("condição não atingida no tempo limite")


# --- worker ----------------------------------------------------------------


Envoltorio = Callable[[UnidadeDeTrabalhoProjecao], UnidadeDeTrabalhoProjecao]


@dataclass
class Worker:
    desfechos: list[Desfecho] = field(default_factory=list)
    recusas: int = 0
    falhas: int = 0

    async def aguardar(self, quantidade: int) -> None:
        async def processadas() -> bool:
            return len(self.desfechos) >= quantidade

        await _aguardar(processadas)


@asynccontextmanager
async def _worker(
    banco: Consolidado,
    broker: Broker,
    consumidor_id: str,
    envoltorio: Envoltorio | None = None,
) -> AsyncIterator[Worker]:
    """Laço real do worker em segundo plano, encerrado na saída."""
    worker = Worker()
    pool: AsyncConnectionPool[psycopg.AsyncConnection[Any]] = AsyncConnectionPool(
        banco.conninfo("consolidado_worker"),
        min_size=1,
        max_size=3,
        kwargs={"autocommit": True},
        open=False,
    )
    await pool.open(wait=True)
    conexao = fabrica_de_conexao(pool)
    unidade: UnidadeDeTrabalhoProjecao = UnidadeDeTrabalhoProjecaoPostgres(conexao)
    if envoltorio is not None:
        unidade = envoltorio(unidade)
    processador = ProcessadorMensagens(
        ProjetarLancamento(unidade), MetricasWorker(Metricas())
    )

    async def tratar(mensagem: MensagemRecebida) -> None:
        try:
            worker.desfechos.append(await processador.processar(mensagem))
        except BarreiraSolicitada:
            worker.recusas += 1
            raise
        except Exception:
            worker.falhas += 1
            raise

    laco = LacoWorker(
        ConfiguracaoWorker(
            conninfo="nao-usado",
            url_rabbitmq="nao-usado",
            intervalo_reconexao_segundos=INTERVALO_SEGUNDOS,
        ),
        ConsumidorRabbitMQ(broker.url("worker"), prefetch=10),
        tratar,
        banco_pronto_de(pool, 2.0),
        barreira=BarreiraConsumidor(ControleBarreiraPostgres(conexao), consumidor_id),
    )
    parar = asyncio.Event()
    tarefa = asyncio.create_task(laco.executar(parar))
    try:
        yield worker
    finally:
        parar.set()
        await asyncio.wait_for(tarefa, TIMEOUT_SEGUNDOS)
        await pool.close()


class _FalhaAntesDoCommit:
    """Unidade real que falha no fim de toda transação, antes do COMMIT."""

    def __init__(self, real: UnidadeDeTrabalhoProjecao) -> None:
        self._real = real

    @asynccontextmanager
    async def _transacao(self) -> AsyncIterator[TransacaoProjecao]:
        async with self._real.transacao() as transacao:
            yield transacao
            raise RuntimeError("falha simulada antes do commit")

    def transacao(self) -> AbstractAsyncContextManager[TransacaoProjecao]:
        return self._transacao()


class _SeguraPrimeira:
    """Unidade real que segura a 1ª transação antes do COMMIT até `liberar`.

    Simula uma mensagem em processamento quando a barreira é erguida.
    """

    def __init__(self, real: UnidadeDeTrabalhoProjecao) -> None:
        self._real = real
        self.em_processamento = asyncio.Event()
        self.liberar = asyncio.Event()

    @asynccontextmanager
    async def _transacao(self) -> AsyncIterator[TransacaoProjecao]:
        primeira = not self.em_processamento.is_set()
        async with self._real.transacao() as transacao:
            yield transacao
            if primeira:
                self.em_processamento.set()
                await self.liberar.wait()

    def transacao(self) -> AbstractAsyncContextManager[TransacaoProjecao]:
        return self._transacao()


# --- TD-001: todas as gerações receptoras -------------------------------------


def test_com_candidata_cada_mensagem_altera_as_duas_geracoes_na_mesma_transacao(
    banco: Consolidado, broker: Broker, data: date, candidata: int, consumidor_id: str
) -> None:
    credito = _evento(data, "CREDITO", "100.00")
    debito = _evento(data, "DEBITO", "30.25")

    async def cenario() -> None:
        ativa = await _ativa(banco)
        await _publicar(broker, credito, debito)
        async with _worker(banco, broker, consumidor_id) as worker:
            await worker.aguardar(2)

        assert worker.desfechos == [Desfecho.APLICADO, Desfecho.APLICADO]
        esperado = (Decimal("100.00"), Decimal("30.25"), Decimal("69.75"))
        assert await _saldos(banco, data) == {ativa: esperado, candidata: esperado}
        for evento in (credito, debito):
            # A mesma transação (xmin) gravou a contribuição nas duas gerações.
            [(geracoes, transacoes)] = await _sql(
                banco,
                "SELECT array_agg(geracao_id ORDER BY geracao_id),"
                " count(DISTINCT xmin::text)"
                " FROM contribuicao WHERE lancamento_id = %s",
                uuid.UUID(str(evento["lancamento_id"])),
            )
            assert (geracoes, transacoes) == ([ativa, candidata], 1)
            for tabela in ("inbox", "marcador_inclusao"):
                coluna = "evento_id" if tabela == "inbox" else "lancamento_id"
                [(quantidade,)] = await _sql(
                    banco,
                    f"SELECT count(*) FROM {tabela} WHERE {coluna} = %s",
                    uuid.UUID(str(evento[coluna])),
                )
                assert quantidade == 2
        # O saldo do dia das duas gerações saiu da última transação.
        [(transacoes,)] = await _sql(
            banco,
            "SELECT count(DISTINCT xmin::text) FROM saldo_diario WHERE data = %s",
            data,
        )
        assert transacoes == 1
        assert await _fila(broker) == (0, 0)

    asyncio.run(cenario())


def test_falha_antes_do_commit_nao_grava_em_nenhuma_das_geracoes(
    banco: Consolidado, broker: Broker, data: date, candidata: int, consumidor_id: str
) -> None:
    evento = _evento(data, "CREDITO", "70.00")

    async def cenario() -> None:
        ativa = await _ativa(banco)
        await _publicar(broker, evento)

        async with _worker(banco, broker, consumidor_id, _FalhaAntesDoCommit) as falho:

            async def falhou() -> bool:
                return falho.falhas >= 1

            await _aguardar(falhou)
        # A transação gravou nas duas gerações e foi desfeita: nada ficou, e
        # a mensagem, sem ACK, voltou à fila.
        assert falho.desfechos == []
        assert await _saldos(banco, data) == {}
        [(contribuicoes,)] = await _sql(
            banco,
            "SELECT count(*) FROM contribuicao WHERE lancamento_id = %s",
            uuid.UUID(str(evento["lancamento_id"])),
        )
        assert contribuicoes == 0

        async def devolvida() -> bool:
            return await _fila(broker) == (1, 0)

        await _aguardar(devolvida)

        async with _worker(banco, broker, consumidor_id) as worker:
            await worker.aguardar(1)

        # A reentrega aplicou nas duas, de uma vez.
        assert worker.desfechos == [Desfecho.APLICADO]
        esperado = (Decimal("70.00"), Decimal("0.00"), Decimal("70.00"))
        assert await _saldos(banco, data) == {ativa: esperado, candidata: esperado}

    asyncio.run(cenario())


def test_replay_e_duplicata_na_ativa_e_aplicado_na_candidata(
    banco: Consolidado, broker: Broker, data: date, consumidor_id: str
) -> None:
    evento = _evento(data, "DEBITO", "12.50")
    esperado = (Decimal("0.00"), Decimal("12.50"), Decimal("-12.50"))

    async def cenario() -> int:
        ativa = await _ativa(banco)
        await _publicar(broker, evento)
        async with _worker(banco, broker, consumidor_id) as worker:
            await worker.aguardar(1)
        assert await _saldos(banco, data) == {ativa: esperado}
        return ativa

    ativa = asyncio.run(cenario())
    candidata = _criar_candidata(banco)

    async def replay() -> None:
        # O replay republica o mesmo evento a partir da outbox (D-002).
        await _publicar(broker, evento)
        async with _worker(banco, broker, consumidor_id) as worker:
            await worker.aguardar(1)
        assert worker.desfechos == [Desfecho.APLICADO]
        # A ativa não soma de novo; a candidata converge para o mesmo saldo.
        assert await _saldos(banco, data) == {ativa: esperado, candidata: esperado}

    try:
        asyncio.run(replay())
    finally:
        with psycopg.connect(banco.conninfo("consolidado_operacao")) as conexao:
            conexao.execute(
                "UPDATE geracao SET estado = 'descartada' WHERE id = %s", (candidata,)
            )


# --- TD-002: barreira dos consumidores ----------------------------------------


def test_com_barreira_solicitada_o_worker_para_de_consumir_e_confirma_a_pausa(
    banco: Consolidado, broker: Broker, data: date, consumidor_id: str
) -> None:
    evento = _evento(data, "CREDITO", "15.00")

    async def cenario() -> None:
        async with _worker(banco, broker, consumidor_id) as worker:

            async def consumindo() -> bool:
                estado = await _consumidor(banco, consumidor_id)
                return estado is not None and not estado[0]

            await _aguardar(consumindo)
            pedido = await _barreira(banco, "solicitada")

            async def confirmada() -> bool:
                return await _pausa_confirmada(banco, consumidor_id, pedido)

            await _aguardar(confirmada)
            # Pausado: sem consumidor na fila; a mensagem nova fica nela.
            await _publicar(broker, evento)
            await asyncio.sleep(5 * INTERVALO_SEGUNDOS)
            assert await _fila(broker) == (1, 0)
            assert await _saldos(banco, data) == {}
            assert worker.desfechos == []
            # A confirmação é renovada a cada intervalo (`visto_em` recente).
            antes = await _consumidor(banco, consumidor_id)
            await asyncio.sleep(3 * INTERVALO_SEGUNDOS)
            depois = await _consumidor(banco, consumidor_id)
            assert antes is not None and depois is not None
            assert depois[0] and depois[1] > antes[1]

            await _barreira(banco, "liberada")
            await worker.aguardar(1)

            assert worker.desfechos == [Desfecho.APLICADO]
            await _aguardar(consumindo)
        assert await _fila(broker) == (0, 0)

    asyncio.run(cenario())


def test_barreira_espera_a_mensagem_em_curso_e_devolve_as_seguintes_a_fila(
    banco: Consolidado, broker: Broker, data: date, consumidor_id: str
) -> None:
    em_curso = _evento(data, "CREDITO", "50.00")
    seguinte = _evento(data, "DEBITO", "20.00")

    async def cenario() -> None:
        ativa = await _ativa(banco)
        unidades: list[_SeguraPrimeira] = []

        def segurar(real: UnidadeDeTrabalhoProjecao) -> UnidadeDeTrabalhoProjecao:
            unidades.append(_SeguraPrimeira(real))
            return unidades[0]

        await _publicar(broker, em_curso, seguinte)
        async with _worker(banco, broker, consumidor_id, segurar) as worker:
            [unidade] = unidades
            await asyncio.wait_for(unidade.em_processamento.wait(), TIMEOUT_SEGUNDOS)
            pedido = await _barreira(banco, "solicitada")

            # Com a mensagem em processamento, a pausa não é confirmada.
            await asyncio.sleep(5 * INTERVALO_SEGUNDOS)
            assert not await _pausa_confirmada(banco, consumidor_id, pedido)

            unidade.liberar.set()

            async def confirmada() -> bool:
                return await _pausa_confirmada(banco, consumidor_id, pedido)

            await _aguardar(confirmada)
            # A mensagem em curso terminou (COMMIT e ACK); a seguinte não foi
            # aplicada e voltou à fila, sem consumidor inscrito.
            assert worker.desfechos == [Desfecho.APLICADO]
            assert await _saldos(banco, data) == {
                ativa: (Decimal("50.00"), Decimal("0.00"), Decimal("50.00"))
            }
            assert await _fila(broker) == (1, 0)

            await _barreira(banco, "liberada")
            await worker.aguardar(2)

        assert worker.desfechos == [Desfecho.APLICADO, Desfecho.APLICADO]
        assert await _saldos(banco, data) == {
            ativa: (Decimal("50.00"), Decimal("20.00"), Decimal("30.00"))
        }
        assert await _fila(broker) == (0, 0)

    asyncio.run(cenario())
