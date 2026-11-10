"""Reserva de eventos da outbox com lease contra PostgreSQL real (003-T1).

Usa o PostgreSQL descartável do livro (INFRA 9.1), com a imagem do compose,
os papéis reais e as migrações Alembic, inclusive a 0002 com os privilégios
de livro_relay. Os eventos entram pela unidade de trabalho real de
lançamentos, como livro_api; a reserva conecta como livro_relay. A partir da
raiz do repositório:

    uv run pytest services/relay/tests/integracao

Cada teste começa marcando como publicados os pendentes deixados pelos
anteriores (como livro_migracao, dono da tabela), para enxergar só os seus.
Sem docker no PATH, o módulo é pulado com o motivo explícito.
"""

import asyncio
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from lancamentos.adaptadores.postgres import UnidadeDeTrabalhoLivroPostgres
from lancamentos.aplicacao import RegistrarLancamentoCmd
from lancamentos.tests.integracao.livro_descartavel import Livro, livro_descartavel
from relay.adaptadores.postgres import (
    EventoReservado,
    FabricaConexao,
    ReservaOutboxPostgres,
)

EXECUCOES = 12
EVENTOS_CONCORRENTES = 60
LEASE_LONGO = timedelta(minutes=5)

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker ausente no PATH"
)


@pytest.fixture(scope="module")
def livro() -> Iterator[Livro]:
    with livro_descartavel("reserva-outbox-relay") as banco:
        yield banco


@pytest.fixture(autouse=True)
def sem_pendentes_anteriores(livro: Livro) -> None:
    _executar(
        livro, "UPDATE outbox SET publicado_em = now() WHERE publicado_em IS NULL"
    )


def _fabrica(livro: Livro, papel: str) -> FabricaConexao:
    @asynccontextmanager
    async def conexao() -> AsyncIterator[psycopg.AsyncConnection[Any]]:
        async with await psycopg.AsyncConnection.connect(
            livro.conninfo(papel), autocommit=True
        ) as aberta:
            yield aberta

    return conexao


def _reserva(
    livro: Livro, dono: str, lote: int = 100, lease: timedelta = LEASE_LONGO
) -> ReservaOutboxPostgres:
    return ReservaOutboxPostgres(_fabrica(livro, "livro_relay"), dono, lote, lease)


def _executar(livro: Livro, sql: str, *params: object) -> None:
    with psycopg.connect(livro.conninfo("livro_migracao")) as conexao:
        conexao.execute(sql, params)


def _consultar(livro: Livro, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with psycopg.connect(livro.conninfo("livro_migracao")) as conexao:
        return conexao.execute(sql, params).fetchall()


def _registrar(livro: Livro, quantidade: int) -> list[uuid.UUID]:
    """Registra lançamentos reais e devolve os `evento_id` em ordem de posição."""
    unidade = UnidadeDeTrabalhoLivroPostgres(_fabrica(livro, "livro_api"))

    async def registrar() -> list[uuid.UUID]:
        lancamentos = []
        for _ in range(quantidade):
            cmd = RegistrarLancamentoCmd.de(
                tipo="CREDITO", valor="10.50", data_lancamento="2026-11-06"
            )
            await unidade.registrar(cmd, f"chave-{uuid.uuid4()}", "cliente-relay")
            lancamentos.append(cmd.lancamento.id)
        return lancamentos

    lancamentos = asyncio.run(registrar())
    linhas = _consultar(
        livro,
        "SELECT evento_id FROM outbox WHERE lancamento_id = ANY(%s) ORDER BY posicao",
        lancamentos,
    )
    return [evento_id for (evento_id,) in linhas]


def _ids(eventos: list[EventoReservado]) -> list[uuid.UUID]:
    return [evento.evento_id for evento in eventos]


def _donos(livro: Livro, eventos: list[uuid.UUID]) -> dict[uuid.UUID, str | None]:
    linhas = _consultar(
        livro,
        "SELECT evento_id, lease_dono FROM outbox WHERE evento_id = ANY(%s)",
        eventos,
    )
    return dict(linhas)


def test_reserva_grava_lease_e_devolve_eventos_em_ordem_de_posicao(
    livro: Livro,
) -> None:
    eventos = _registrar(livro, 3)

    reservados = asyncio.run(_reserva(livro, "relay-a").reservar())

    assert _ids(reservados) == eventos
    posicoes = [evento.posicao for evento in reservados]
    assert posicoes == sorted(posicoes)
    for evento in reservados:
        assert evento.tipo_evento == "LancamentoRegistrado"
        assert evento.payload["evento_id"] == str(evento.evento_id)
        assert evento.payload["posicao"] == evento.posicao
        assert evento.tentativas == 0
    [(dono, publicado_em, lease_restante)] = _consultar(
        livro,
        "SELECT DISTINCT lease_dono, publicado_em, lease_ate - now() > interval '4 min'"
        " FROM outbox WHERE evento_id = ANY(%s)",
        eventos,
    )
    assert (dono, publicado_em, lease_restante) == ("relay-a", None, True)


def test_lote_limita_a_reserva_aos_eventos_de_menor_posicao(livro: Livro) -> None:
    eventos = _registrar(livro, 5)
    reserva = _reserva(livro, "relay-a", lote=2)

    primeiro = asyncio.run(reserva.reservar())
    segundo = asyncio.run(reserva.reservar())
    terceiro = asyncio.run(reserva.reservar())

    assert _ids(primeiro) == eventos[:2]
    assert _ids(segundo) == eventos[2:4]
    assert _ids(terceiro) == eventos[4:]


def test_lease_ativo_impede_reserva_por_outra_execucao(livro: Livro) -> None:
    eventos = _registrar(livro, 2)

    assert _ids(asyncio.run(_reserva(livro, "relay-a").reservar())) == eventos
    assert asyncio.run(_reserva(livro, "relay-b").reservar()) == []
    assert set(_donos(livro, eventos).values()) == {"relay-a"}


def test_reserva_pula_eventos_travados_sem_esperar(livro: Livro) -> None:
    eventos = _registrar(livro, 4)

    # Outra execução no meio da reserva, com as duas primeiras linhas travadas.
    with psycopg.connect(livro.conninfo("livro_relay")) as travando:
        travando.execute(
            "SELECT 1 FROM outbox WHERE evento_id = ANY(%s) FOR UPDATE",
            (eventos[:2],),
        )

        reservados = asyncio.run(
            asyncio.wait_for(_reserva(livro, "relay-b").reservar(), timeout=5)
        )

        assert _ids(reservados) == eventos[2:]
        travando.rollback()

    liberados = asyncio.run(_reserva(livro, "relay-c").reservar())
    assert _ids(liberados) == eventos[:2]


def test_execucoes_concorrentes_nunca_reservam_o_mesmo_evento(livro: Livro) -> None:
    eventos = _registrar(livro, EVENTOS_CONCORRENTES)

    async def execucao(
        dono: str, largada: asyncio.Event
    ) -> tuple[str, list[EventoReservado]]:
        reserva = _reserva(livro, dono, lote=5)
        obtidos: list[EventoReservado] = []
        await largada.wait()
        # Limitado: reserva que repete eventos falha no assert, não trava.
        for _ in range(EVENTOS_CONCORRENTES):
            lote = await reserva.reservar()
            if not lote:
                break
            obtidos.extend(lote)
        return dono, obtidos

    async def disputar() -> list[tuple[str, list[EventoReservado]]]:
        largada = asyncio.Event()
        tarefas = [
            asyncio.create_task(execucao(f"relay-{indice}", largada))
            for indice in range(EXECUCOES)
        ]
        await asyncio.sleep(0)
        largada.set()
        return await asyncio.gather(*tarefas)

    resultados = asyncio.run(disputar())

    por_execucao = {dono: _ids(obtidos) for dono, obtidos in resultados}
    todos = [evento for ids in por_execucao.values() for evento in ids]
    assert len(todos) == len(set(todos)), "evento reservado por duas execuções"
    assert sorted(todos) == sorted(eventos)
    # Mais de uma execução obteve eventos: houve disputa de fato.
    assert sum(1 for ids in por_execucao.values() if ids) > 1
    donos = _donos(livro, eventos)
    for dono, ids in por_execucao.items():
        assert all(donos[evento] == dono for evento in ids)


def test_lease_vencido_permite_nova_reserva(livro: Livro) -> None:
    eventos = _registrar(livro, 2)
    lease_curto = timedelta(seconds=1)

    assert _ids(asyncio.run(_reserva(livro, "relay-a", lease=lease_curto).reservar()))
    assert asyncio.run(_reserva(livro, "relay-b").reservar()) == []

    # O relay-a "cai" sem publicar; o lease vence pelo relógio do banco.
    time.sleep(1.5)
    reassumidos = asyncio.run(_reserva(livro, "relay-b").reservar())

    assert _ids(reassumidos) == eventos
    assert set(_donos(livro, eventos).values()) == {"relay-b"}
    # O evento continua na outbox, pendente (NFR-003).
    [(pendentes,)] = _consultar(
        livro,
        "SELECT count(*) FROM outbox WHERE evento_id = ANY(%s)"
        " AND publicado_em IS NULL",
        eventos,
    )
    assert pendentes == 2


def test_publicado_ou_com_tentativa_futura_nao_e_reservado(livro: Livro) -> None:
    publicado, futuro, vencido = _registrar(livro, 3)
    _executar(
        livro, "UPDATE outbox SET publicado_em = now() WHERE evento_id = %s", publicado
    )
    _executar(
        livro,
        "UPDATE outbox SET proxima_tentativa_em = now() + interval '1 hour'"
        " WHERE evento_id = %s",
        futuro,
    )
    _executar(
        livro,
        "UPDATE outbox SET proxima_tentativa_em = now() - interval '1 second'"
        " WHERE evento_id = %s",
        vencido,
    )

    reservados = asyncio.run(_reserva(livro, "relay-a").reservar())

    assert _ids(reservados) == [vencido]


@pytest.mark.parametrize(
    ("comando", "tabela"),
    [
        ("UPDATE outbox SET payload = '{}'", "outbox"),
        ("UPDATE outbox SET posicao = posicao", "outbox"),
        ("DELETE FROM outbox", "outbox"),
        ("TRUNCATE outbox", "outbox"),
        (
            (
                "INSERT INTO outbox (evento_id, lancamento_id, posicao, tipo_evento,"
                " payload) VALUES (gen_random_uuid(), gen_random_uuid(), 1, 'x', '{}')"
            ),
            "outbox",
        ),
        ("SELECT 1 FROM lancamento", "lancamento"),
        ("UPDATE contador_posicao SET valor = valor", "contador_posicao"),
    ],
    ids=[
        "payload",
        "posicao",
        "delete",
        "truncate",
        "insert",
        "ler-livro",
        "contador",
    ],
)
def test_livro_relay_so_atualiza_metadados_da_outbox(
    livro: Livro, comando: str, tabela: str
) -> None:
    with (
        psycopg.connect(livro.conninfo("livro_relay")) as conexao,
        pytest.raises(psycopg.errors.InsufficientPrivilege) as erro,
    ):
        conexao.execute(comando)

    assert f"permission denied for table {tabela}" in str(erro.value)


@pytest.mark.parametrize(
    ("dono", "lote", "lease"),
    [("", 1, LEASE_LONGO), ("relay-a", 0, LEASE_LONGO), ("relay-a", 1, timedelta(0))],
    ids=["dono-vazio", "lote-zero", "lease-zero"],
)
def test_parametros_invalidos_da_reserva_sao_recusados(
    dono: str, lote: int, lease: timedelta
) -> None:
    with pytest.raises(ValueError):
        ReservaOutboxPostgres(_fabrica_inexistente, dono, lote, lease)


@asynccontextmanager
async def _fabrica_inexistente() -> AsyncIterator[psycopg.AsyncConnection[Any]]:
    raise AssertionError("não deve conectar")
    yield  # pragma: no cover
