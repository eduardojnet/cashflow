"""Troca, rollback e descarte com o worker em execução (005-T4; INFRA 9.1, 9.2).

PostgreSQL descartável do consolidado, com papéis e migrações reais, e
RabbitMQ descartável com a topologia, os usuários e as permissões do compose.
O worker roda o laço, o consumidor, a projeção e a barreira reais, como
`worker` e consolidado_worker, em segundo plano durante todo o módulo. A CLI
roda pela mesma função do contêiner (`coordenador.cli.main`), como
consolidado_operacao.

O "livro" deste módulo é a lista dos eventos publicados: a republicação da
outbox (D-002) é a publicação de novo de todos eles, com o mesmo
`evento_id`, que a ativa reconhece como duplicata e a candidata aplica.

- troca recusada com lacuna na candidata (FR-009), decidida com o worker em
  pausa, e consumo retomado depois da recusa;
- falha simulada no meio da transação da troca, depois de os estados já
  terem mudado nela: a ativa e o ponteiro ficam como estavam (FR-005);
- troca, eventos novos nas duas gerações, rollback (FR-006) e descarte, com
  o saldo de cada data igual ao do livro;
- consumidor registrado que não confirma a pausa: troca cancelada no tempo
  limite e barreira liberada (design 005 §8);
- outra identidade e operação concorrente são recusadas sem mudar nada.

Cada teste termina sem candidata e sem anterior. A partir da raiz:

    uv run pytest services/coordenador

Sem docker no PATH, o módulo é pulado com o motivo explícito.
"""

import asyncio
import itertools
import json
import logging
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from types import TracebackType
from typing import Any, Self

import aio_pika
import psycopg
import pytest
from consolidado.adaptadores.postgres import (
    ControleBarreiraPostgres,
    UnidadeDeTrabalhoProjecaoPostgres,
)
from consolidado.adaptadores.rabbitmq import (
    ConsumidorRabbitMQ,
    MensagemRecebida,
    ProcessadorMensagens,
)
from consolidado.aplicacao import BarreiraConsumidor, ProjetarLancamento
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
from coordenador.adaptadores.postgres import (
    CHAVE_BLOQUEIO_OPERACAO,
    TransacaoGeracoesPostgres,
    abridor_de_transacao,
    bloqueio_de_operacao,
)
from coordenador.aplicacao import ConfiguracaoBarreira, TrocarGeracao
from coordenador.cli import main
from observabilidade import Metricas
from psycopg_pool import AsyncConnectionPool
from relay.tests.integracao.rabbitmq_descartavel import Broker, rabbitmq_descartavel

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker ausente no PATH"
)

# Topologia de design 003 §4, a mesma de definicoes.modelo.json.
EXCHANGE = "cashflow.lancamentos"
ROUTING_KEY = "lancamento.registrado.v1"
CONSUMIDOR = "worker"
INTERVALO_WORKER_SEGUNDOS = 0.2
TIMEOUT_SEGUNDOS = 30
DATAS = (date(2026, 11, 2), date(2026, 11, 3))
OUTRAS_IDENTIDADES = ["consolidado_api", "consolidado_worker", "consolidado_migracao"]


# --- o livro deste módulo ----------------------------------------------------


@dataclass
class Livro:
    """Eventos publicados, na ordem; a fonte da verdade do teste."""

    broker: Broker
    eventos: list[dict[str, object]] = field(default_factory=list)
    _posicoes: Iterator[int] = field(default_factory=lambda: itertools.count(1))

    def novo(self, tipo: str = "CREDITO", valor: str = "10.00") -> dict[str, object]:
        """Evento `LancamentoRegistrado` v1 na próxima posição, sem publicar."""
        posicao = next(self._posicoes)
        return {
            "evento_id": str(uuid.uuid4()),
            "versao": 1,
            "lancamento_id": str(uuid.uuid4()),
            "posicao": posicao,
            "tipo": tipo,
            "valor": valor,
            "data_lancamento": DATAS[posicao % len(DATAS)].isoformat(),
            "registrado_em": "2026-11-07T12:00:00+00:00",
        }

    def publicar(self, *eventos: dict[str, object]) -> None:
        self.eventos.extend(eventos)
        asyncio.run(_publicar(self.broker, eventos))

    def republicar(self) -> None:
        """A republicação da outbox desde a posição 1 (D-002)."""
        asyncio.run(_publicar(self.broker, self.eventos))

    @property
    def maior_posicao(self) -> int:
        return max((int(str(e["posicao"])) for e in self.eventos), default=0)

    def saldos(self) -> dict[date, Decimal]:
        saldos: dict[date, Decimal] = {}
        for evento in self.eventos:
            data = date.fromisoformat(str(evento["data_lancamento"]))
            valor = Decimal(str(evento["valor"]))
            sinal = 1 if evento["tipo"] == "CREDITO" else -1
            saldos[data] = saldos.get(data, Decimal(0)) + sinal * valor
        return saldos


async def _publicar(broker: Broker, eventos: Any) -> None:
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


# --- worker real em segundo plano ----------------------------------------------


class WorkerEmSegundoPlano:
    """Laço real do worker num event loop próprio, numa thread."""

    def __init__(self, banco: Consolidado, broker: Broker) -> None:
        self._banco = banco
        self._broker = broker
        self._loop = asyncio.new_event_loop()
        self._fio = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._parar = asyncio.Event()

    def __enter__(self) -> Self:
        self._fio.start()
        self._execucao = asyncio.run_coroutine_threadsafe(self._executar(), self._loop)
        return self

    async def _executar(self) -> None:
        pool: AsyncConnectionPool[psycopg.AsyncConnection[Any]] = AsyncConnectionPool(
            self._banco.conninfo("consolidado_worker"),
            min_size=1,
            max_size=3,
            kwargs={"autocommit": True},
            open=False,
        )
        await pool.open(wait=True)
        try:
            conexao = fabrica_de_conexao(pool)
            processador = ProcessadorMensagens(
                ProjetarLancamento(UnidadeDeTrabalhoProjecaoPostgres(conexao)),
                MetricasWorker(Metricas()),
            )

            async def tratar(mensagem: MensagemRecebida) -> None:
                await processador.processar(mensagem)

            laco = LacoWorker(
                ConfiguracaoWorker(
                    conninfo="nao-usado",
                    url_rabbitmq="nao-usado",
                    intervalo_reconexao_segundos=INTERVALO_WORKER_SEGUNDOS,
                ),
                ConsumidorRabbitMQ(self._broker.url("worker"), prefetch=10),
                tratar,
                banco_pronto_de(pool, 2.0),
                barreira=BarreiraConsumidor(
                    ControleBarreiraPostgres(conexao), CONSUMIDOR
                ),
            )
            await laco.executar(self._parar)
        finally:
            await pool.close()

    def __exit__(
        self,
        tipo: type[BaseException] | None,
        erro: BaseException | None,
        pilha: TracebackType | None,
    ) -> None:
        self._loop.call_soon_threadsafe(self._parar.set)
        try:
            self._execucao.result(timeout=TIMEOUT_SEGUNDOS)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._fio.join(timeout=TIMEOUT_SEGUNDOS)
            self._loop.close()


# --- fixtures ------------------------------------------------------------------


@pytest.fixture(scope="module")
def banco() -> Iterator[Consolidado]:
    with consolidado_descartavel("coordenador-troca") as consolidado:
        yield consolidado


@pytest.fixture(scope="module")
def broker() -> Iterator[Broker]:
    with rabbitmq_descartavel("coordenador-troca") as rabbitmq:
        yield rabbitmq


@pytest.fixture(scope="module")
def livro(broker: Broker) -> Livro:
    return Livro(broker)


@pytest.fixture(scope="module")
def worker(banco: Consolidado, broker: Broker) -> Iterator[WorkerEmSegundoPlano]:
    with WorkerEmSegundoPlano(banco, broker) as em_execucao:
        _aguardar(
            lambda: (
                _um(
                    banco,
                    "SELECT count(*) FROM estado_consumidor"
                    " WHERE consumidor_id = %s AND NOT pausado",
                    CONSUMIDOR,
                )
                == 1
            ),
            "o worker não começou a consumir",
        )
        yield em_execucao


@pytest.fixture(autouse=True)
def logging_restaurado() -> Iterator[None]:
    # `main` troca os handlers da raiz pelo JSON na saída de erro.
    raiz = logging.getLogger()
    handlers, nivel = raiz.handlers[:], raiz.level
    yield
    raiz.handlers[:] = handlers
    raiz.setLevel(nivel)


@pytest.fixture(autouse=True)
def sem_reconstrucao(banco: Consolidado) -> Iterator[None]:
    """Mesmo se um teste falhar, o seguinte começa sem candidata nem anterior."""
    yield
    for comando in (["reconstruir", "cancelar"], ["geracao", "descartar"]):
        main(comando, ambiente=_ambiente(banco))


# --- utilitários -------------------------------------------------------------------


def _ambiente(
    banco: Consolidado, papel: str = "consolidado_operacao"
) -> dict[str, str]:
    """Mesmas variáveis do serviço `coordenador` do compose."""
    return {
        "PG_CONSOLIDADO_HOST": "127.0.0.1",
        "PG_CONSOLIDADO_PORTA": str(banco.porta),
        "PG_CONSOLIDADO_USUARIO_OPERACAO": papel,
        "PG_CONSOLIDADO_SENHA_OPERACAO": banco.senhas[papel],
    }


def _cli(
    banco: Consolidado,
    capsys: pytest.CaptureFixture[str],
    *argumentos: str,
    papel: str = "consolidado_operacao",
) -> tuple[int, str, str]:
    codigo = main(list(argumentos), ambiente=_ambiente(banco, papel))
    saida = capsys.readouterr()
    return codigo, saida.out, saida.err


def _logs(erro: str) -> list[dict[str, Any]]:
    """Registros JSON do coordenador na saída de erro."""
    registros = []
    for linha in erro.splitlines():
        if linha.startswith("{"):
            registro = json.loads(linha)
            if str(registro.get("logger", "")).startswith("coordenador"):
                registros.append(registro)
    return registros


def _um(banco: Consolidado, consulta: str, *params: object) -> Any:
    with psycopg.connect(banco.conninfo("consolidado_operacao")) as conexao:
        linha = conexao.execute(consulta, params).fetchone()
    assert linha is not None
    return linha[0]


def _geracoes(banco: Consolidado) -> list[tuple[int, str]]:
    with psycopg.connect(banco.conninfo("consolidado_operacao")) as conexao:
        return conexao.execute(
            "SELECT id, estado FROM geracao WHERE estado <> 'descartada' ORDER BY id"
        ).fetchall()


def _controle(banco: Consolidado) -> tuple[int, str]:
    with psycopg.connect(banco.conninfo("consolidado_operacao")) as conexao:
        linha = conexao.execute(
            "SELECT geracao_ativa_id, barreira FROM controle_projecao WHERE id = 1"
        ).fetchone()
    assert linha is not None
    return linha[0], linha[1]


def _no_estado(banco: Consolidado, estado: str) -> int:
    geracao_id: int = _um(banco, "SELECT id FROM geracao WHERE estado = %s", estado)
    return geracao_id


def _fase(banco: Consolidado, geracao_id: int) -> str | None:
    with psycopg.connect(banco.conninfo("consolidado_operacao")) as conexao:
        linha = conexao.execute(
            "SELECT fase FROM fase_reconstrucao WHERE geracao_id = %s", (geracao_id,)
        ).fetchone()
    return None if linha is None else str(linha[0])


def _progresso(banco: Consolidado, geracao_id: int) -> tuple[int, int, int]:
    """Checkpoint, lacunas e maior posição projetada da geração."""
    with psycopg.connect(banco.conninfo("consolidado_operacao")) as conexao:
        linha = conexao.execute(
            "SELECT (SELECT posicao_continua FROM checkpoint WHERE geracao_id = %s),"
            " (SELECT count(*) FROM lacuna WHERE geracao_id = %s),"
            " (SELECT coalesce(max(posicao), 0) FROM contribuicao"
            "  WHERE geracao_id = %s)",
            (geracao_id, geracao_id, geracao_id),
        ).fetchone()
    assert linha is not None
    return linha[0] or 0, linha[1], linha[2]


def _saldos(banco: Consolidado, geracao_id: int) -> dict[date, Decimal]:
    with psycopg.connect(banco.conninfo("consolidado_operacao")) as conexao:
        linhas = conexao.execute(
            "SELECT data, saldo_dia FROM saldo_diario WHERE geracao_id = %s",
            (geracao_id,),
        ).fetchall()
    return {data: saldo for data, saldo in linhas}


def _aguardar(condicao: Callable[[], bool], motivo: str) -> None:
    limite = time.monotonic() + TIMEOUT_SEGUNDOS
    while time.monotonic() < limite:
        if condicao():
            return
        time.sleep(0.1)
    raise AssertionError(motivo)


def _aguardar_em_dia(banco: Consolidado, livro: Livro, *geracoes: int) -> None:
    """Espera cada geração com todas as posições do livro, sem lacuna."""
    esperado = (livro.maior_posicao, 0, livro.maior_posicao)
    _aguardar(
        lambda: all(_progresso(banco, g) == esperado for g in geracoes),
        f"gerações {geracoes} não alcançaram o livro",
    )


def _candidata_pronta(
    banco: Consolidado, livro: Livro, capsys: pytest.CaptureFixture[str]
) -> tuple[int, int]:
    """`reconstruir iniciar` e replay do livro até a candidata convergir."""
    codigo, _, erro = _cli(banco, capsys, "reconstruir", "iniciar")
    assert codigo == 0, erro
    ativa, candidata = _no_estado(banco, "ativa"), _no_estado(banco, "construindo")
    livro.publicar(livro.novo(), livro.novo("DEBITO", "3.25"))
    livro.republicar()
    _aguardar_em_dia(banco, livro, ativa, candidata)
    return ativa, candidata


# --- testes ----------------------------------------------------------------------


def test_troca_recusada_com_lacuna_na_candidata(
    banco: Consolidado,
    livro: Livro,
    worker: WorkerEmSegundoPlano,
    capsys: pytest.CaptureFixture[str],
) -> None:
    codigo, _, erro = _cli(banco, capsys, "reconstruir", "iniciar")
    assert codigo == 0, erro
    ativa, candidata = _no_estado(banco, "ativa"), _no_estado(banco, "construindo")
    primeiro, faltante, terceiro = livro.novo(), livro.novo(), livro.novo()
    livro.publicar(primeiro, terceiro)
    livro.republicar()
    _aguardar(
        lambda: _progresso(banco, candidata)[1:] == (1, livro.maior_posicao),
        "a candidata não recebeu o replay",
    )

    codigo, saida, erro = _cli(
        banco, capsys, "geracao", "trocar", "--tempo-limite", "10"
    )

    assert codigo == 1
    assert saida == ""
    assert (
        "troca recusada pelo critério de aceite (FR-009): candidata com 1 lacunas"
        in erro
    )
    # Decidida com o worker em pausa (FR-004).
    mensagens = [r["mensagem"] for r in _logs(erro)]
    assert mensagens[:2] == [
        "barreira solicitada",
        "pausa confirmada pelos consumidores",
    ]
    assert "barreira liberada" in mensagens
    assert _logs(erro)[1]["consumidores"] == 1
    assert _controle(banco) == (ativa, "liberada")
    assert _geracoes(banco) == [(ativa, "ativa"), (candidata, "construindo")]
    assert _fase(banco, candidata) == "construindo"

    # Barreira liberada: o worker volta a consumir e a lacuna fecha.
    livro.publicar(faltante)
    _aguardar_em_dia(banco, livro, ativa, candidata)


class _FalhaAoApontar(TransacaoGeracoesPostgres):
    """Transação real que falha ao apontar a nova ativa, já no fim da troca."""

    def __init__(self, conexao: psycopg.Connection[Any]) -> None:
        super().__init__(conexao)
        self._conexao_real = conexao
        self.estados_na_falha: list[tuple[int, str]] = []

    def apontar_ativa(self, geracao_id: int) -> None:
        self.estados_na_falha = self._conexao_real.execute(
            "SELECT id, estado FROM geracao WHERE estado <> 'descartada' ORDER BY id"
        ).fetchall()
        raise RuntimeError("falha simulada durante a troca")


def test_falha_simulada_durante_a_troca_nao_altera_a_ativa(
    banco: Consolidado,
    livro: Livro,
    worker: WorkerEmSegundoPlano,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ativa, candidata = _candidata_pronta(banco, livro, capsys)
    conninfo = banco.conninfo("consolidado_operacao")
    transacoes: list[_FalhaAoApontar] = []

    @contextmanager
    def abrir() -> Iterator[_FalhaAoApontar]:
        with psycopg.connect(conninfo) as conexao:
            transacao = _FalhaAoApontar(conexao)
            transacoes.append(transacao)
            yield transacao

    troca = TrocarGeracao(
        abrir,
        bloqueio_de_operacao(conninfo),
        ConfiguracaoBarreira(tempo_limite_segundos=10),
    )

    with pytest.raises(RuntimeError, match="falha simulada"):
        troca()

    # Na transação, os estados já tinham mudado quando a falha veio.
    (na_falha,) = [t.estados_na_falha for t in transacoes if t.estados_na_falha]
    assert na_falha == [(ativa, "anterior"), (candidata, "ativa")]
    # Desfeita inteira: ativa, ponteiro e fase como antes; barreira liberada.
    assert _geracoes(banco) == [(ativa, "ativa"), (candidata, "construindo")]
    assert _controle(banco) == (ativa, "liberada")
    assert _fase(banco, candidata) == "construindo"

    # O consumo segue: um evento novo chega às duas gerações.
    livro.publicar(livro.novo())
    _aguardar_em_dia(banco, livro, ativa, candidata)


def test_troca_rollback_e_descarte_com_worker_em_execucao(
    banco: Consolidado,
    livro: Livro,
    worker: WorkerEmSegundoPlano,
    capsys: pytest.CaptureFixture[str],
) -> None:
    antiga, candidata = _candidata_pronta(banco, livro, capsys)

    codigo, saida, erro = _cli(banco, capsys, "geracao", "trocar")

    assert codigo == 0, erro
    assert f"geração {candidata} ativa; geração {antiga} anterior" in saida
    assert _controle(banco) == (candidata, "liberada")
    assert _geracoes(banco) == [(antiga, "anterior"), (candidata, "ativa")]
    assert _fase(banco, candidata) == "concluida"
    trocou = next(r for r in _logs(erro) if r["mensagem"] == "geração ativa trocada")
    assert (trocou["geracao_id"], trocou["geracao_anterior_id"]) == (candidata, antiga)
    assert trocou["operador"] == "consolidado_operacao"

    # As duas seguem recebendo eventos (TD-001), com o saldo do livro.
    livro.publicar(livro.novo("DEBITO", "1.50"))
    _aguardar_em_dia(banco, livro, candidata, antiga)
    assert _saldos(banco, candidata) == _saldos(banco, antiga) == livro.saldos()

    codigo, saida, erro = _cli(banco, capsys, "geracao", "rollback")

    assert codigo == 0, erro
    assert f"rollback concluído: geração {antiga} ativa" in saida
    assert _controle(banco) == (antiga, "liberada")
    assert _geracoes(banco) == [(antiga, "ativa"), (candidata, "anterior")]
    assert _fase(banco, candidata) == "revertida"
    livro.publicar(livro.novo())
    _aguardar_em_dia(banco, livro, antiga, candidata)
    assert _saldos(banco, antiga) == _saldos(banco, candidata) == livro.saldos()

    # Com a anterior retida, nova reconstrução espera o descarte (D-005).
    codigo, _, erro = _cli(banco, capsys, "reconstruir", "iniciar")
    assert codigo == 1
    assert "já existe geração anterior" in erro

    codigo, saida, erro = _cli(banco, capsys, "geracao", "descartar")

    assert codigo == 0, erro
    assert f"geração {candidata} descartada" in saida
    assert _geracoes(banco) == [(antiga, "ativa")]
    assert _controle(banco) == (antiga, "liberada")
    assert _fase(banco, candidata) == "descartada"
    for tabela in ("checkpoint", "lacuna", "contribuicao", "saldo_diario"):
        assert (
            _um(
                banco, f"SELECT count(*) FROM {tabela} WHERE geracao_id = %s", candidata
            )
            == 0
        ), tabela

    for acao in ("rollback", "descartar"):
        codigo, _, erro = _cli(banco, capsys, "geracao", acao)
        assert codigo == 1
        assert "nenhuma geração anterior" in erro
    codigo, _, erro = _cli(banco, capsys, "geracao", "trocar")
    assert codigo == 1
    assert "nenhuma reconstrução em andamento" in erro
    assert _controle(banco) == (antiga, "liberada")


def test_consumidor_sem_confirmacao_cancela_a_troca(
    banco: Consolidado,
    livro: Livro,
    worker: WorkerEmSegundoPlano,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Um segundo worker registrado que não roda mais: nunca confirma a pausa.
    with psycopg.connect(banco.conninfo("consolidado_worker")) as conexao:
        conexao.execute(
            "INSERT INTO estado_consumidor (consumidor_id, pausado, visto_em)"
            " VALUES ('worker-parado', true, now() - interval '1 hour')"
        )
    try:
        ativa, candidata = _candidata_pronta(banco, livro, capsys)

        codigo, _, erro = _cli(
            banco, capsys, "geracao", "trocar", "--tempo-limite", "1"
        )

        assert codigo == 1
        assert (
            "barreira não confirmada em 1 s pelos consumidores: worker-parado" in erro
        )
        assert _controle(banco) == (ativa, "liberada")
        assert _geracoes(banco) == [(ativa, "ativa"), (candidata, "construindo")]
        assert _fase(banco, candidata) == "construindo"
    finally:
        with psycopg.connect(banco.conninfo("postgres")) as conexao:
            conexao.execute(
                "DELETE FROM estado_consumidor WHERE consumidor_id = 'worker-parado'"
            )

    # Sem o consumidor parado, a mesma troca passa.
    codigo, _, erro = _cli(banco, capsys, "geracao", "trocar")
    assert codigo == 0, erro
    assert _controle(banco) == (candidata, "liberada")


@pytest.mark.parametrize("papel", OUTRAS_IDENTIDADES)
@pytest.mark.parametrize("acao", ["trocar", "rollback", "descartar"])
def test_outra_identidade_nao_troca_nem_reverte_nem_descarta(
    banco: Consolidado, capsys: pytest.CaptureFixture[str], papel: str, acao: str
) -> None:
    antes = (_geracoes(banco), _controle(banco))

    codigo, saida, erro = _cli(banco, capsys, "geracao", acao, papel=papel)

    assert codigo == 1
    assert saida == ""
    assert f"identidade {papel} não autorizada" in erro
    assert banco.senhas[papel] not in erro
    assert (_geracoes(banco), _controle(banco)) == antes


@pytest.mark.parametrize("papel", ["consolidado_api", "consolidado_worker"])
def test_banco_recusa_a_troca_aos_outros_papeis_mesmo_sem_a_verificacao(
    banco: Consolidado, papel: str
) -> None:
    antes = (_geracoes(banco), _controle(banco))
    conninfo = banco.conninfo(papel)
    troca = TrocarGeracao(
        abridor_de_transacao(conninfo, papel_exigido=None),
        bloqueio_de_operacao(conninfo, papel_exigido=None),
    )

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        troca()

    assert (_geracoes(banco), _controle(banco)) == antes


@pytest.mark.parametrize("acao", ["trocar", "rollback", "descartar"])
def test_operacao_concorrente_e_recusada_sem_esperar(
    banco: Consolidado, capsys: pytest.CaptureFixture[str], acao: str
) -> None:
    antes = (_geracoes(banco), _controle(banco))
    with psycopg.connect(
        banco.conninfo("consolidado_operacao"), autocommit=True
    ) as outro:
        outro.execute("SELECT pg_advisory_lock(%s)", (CHAVE_BLOQUEIO_OPERACAO,))

        codigo, _, erro = _cli(banco, capsys, "geracao", acao)

    assert codigo == 1
    assert "outra operação de geração está em andamento" in erro
    assert (_geracoes(banco), _controle(banco)) == antes
