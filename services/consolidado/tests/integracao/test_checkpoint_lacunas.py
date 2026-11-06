"""Checkpoint e lacunas contra PostgreSQL real (004-T4; FR-009; TD-006).

O checkpoint é a maior posição do livro projetada sem lacunas; as posições
que faltam abaixo da maior já projetada ficam em `lacuna` até chegarem. Tudo
na transação da projeção, com o papel consolidado_worker (migração 0001).

PostgreSQL descartável com papéis e migrações reais (INFRA 9.1). Cada teste
começa numa geração ativa nova, com checkpoint 0, criada como
consolidado_migracao (dono das tabelas), para não herdar posições de outro
teste. O caso de uso e a unidade de trabalho são os do worker; o broker não
participa, porque o checkpoint só depende da transação. A partir da raiz do
repositório:

    uv run pytest services/consolidado/tests/integracao

Sem docker no PATH, o módulo é pulado com o motivo explícito.
"""

import asyncio
import json
import random
import shutil
import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import psycopg
import pytest
from consolidado.adaptadores.postgres import UnidadeDeTrabalhoProjecaoPostgres
from consolidado.aplicacao import (
    Desfecho,
    EstadoCheckpoint,
    MensagemEvento,
    ProjetarLancamento,
)
from consolidado.worker.metricas import MetricasWorker
from consolidado.worker.principal import fabrica_de_conexao
from consolidado_descartavel import Consolidado, consolidado_descartavel
from observabilidade import Metricas
from psycopg_pool import AsyncConnectionPool

DATA = date(2026, 11, 6)

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker ausente no PATH"
)


@pytest.fixture(scope="module")
def banco() -> Iterator[Consolidado]:
    with consolidado_descartavel("checkpoint-consolidado") as consolidado:
        yield consolidado


def _nova_geracao_ativa(banco: Consolidado) -> int:
    """Geração ativa nova, com checkpoint 0; a anterior deixa de ser ativa."""
    with psycopg.connect(banco.conninfo("consolidado_migracao")) as conexao:
        conexao.execute("UPDATE geracao SET estado = 'anterior' WHERE estado = 'ativa'")
        linha = conexao.execute(
            "INSERT INTO geracao (estado) VALUES ('ativa') RETURNING id"
        ).fetchone()
        assert linha is not None
        geracao_id: int = linha[0]
        conexao.execute(
            "UPDATE controle_projecao SET geracao_ativa_id = %s WHERE id = 1",
            (geracao_id,),
        )
        conexao.execute(
            "INSERT INTO checkpoint (geracao_id) VALUES (%s)", (geracao_id,)
        )
    return geracao_id


@pytest.fixture
def geracao(banco: Consolidado) -> int:
    return _nova_geracao_ativa(banco)


def _mensagem(posicao: int, data: date = DATA) -> MensagemEvento:
    evento: dict[str, object] = {
        "evento_id": str(uuid.uuid4()),
        "versao": 1,
        "lancamento_id": str(uuid.uuid4()),
        "posicao": posicao,
        "tipo": "CREDITO",
        "valor": "1.00",
        "data_lancamento": data.isoformat(),
        "registrado_em": "2026-11-06T12:00:00+00:00",
    }
    return MensagemEvento(
        corpo=json.dumps(evento).encode(),
        dados=evento,
        identificador=str(evento["evento_id"]),
    )


async def _projetar_async(
    banco: Consolidado,
    mensagens: list[MensagemEvento],
    metricas: Metricas,
    concorrentes: int = 1,
) -> list[Desfecho]:
    """Projeta como consolidado_worker; com `concorrentes` > 1, em paralelo."""
    pool: AsyncConnectionPool[psycopg.AsyncConnection[Any]] = AsyncConnectionPool(
        banco.conninfo("consolidado_worker"),
        min_size=concorrentes,
        max_size=concorrentes,
        kwargs={"autocommit": True},
        open=False,
    )
    await pool.open(wait=True)
    try:
        caso = ProjetarLancamento(
            UnidadeDeTrabalhoProjecaoPostgres(fabrica_de_conexao(pool)),
            MetricasWorker(metricas),
        )
        if concorrentes == 1:
            return [await caso.executar(m) for m in mensagens]
        return list(await asyncio.gather(*(caso.executar(m) for m in mensagens)))
    finally:
        await pool.close()


def _projetar(
    banco: Consolidado,
    *posicoes: int,
    metricas: Metricas | None = None,
    concorrentes: int = 1,
    data_por_posicao: bool = False,
) -> list[Desfecho]:
    mensagens = [
        _mensagem(p, DATA + timedelta(days=p) if data_por_posicao else DATA)
        for p in posicoes
    ]
    return asyncio.run(
        _projetar_async(banco, mensagens, metricas or Metricas(), concorrentes)
    )


def _checkpoint(banco: Consolidado, geracao: int) -> tuple[int, list[int]]:
    with psycopg.connect(banco.conninfo("consolidado_migracao")) as conexao:
        linha = conexao.execute(
            "SELECT posicao_continua FROM checkpoint WHERE geracao_id = %s",
            (geracao,),
        ).fetchone()
        assert linha is not None
        lacunas = conexao.execute(
            "SELECT posicao FROM lacuna WHERE geracao_id = %s ORDER BY posicao",
            (geracao,),
        ).fetchall()
    return linha[0], [p for (p,) in lacunas]


def _medidor(metricas: Metricas, nome: str, geracao: int) -> float | None:
    return metricas.registro.get_sample_value(nome, {"geracao": str(geracao)})


def test_eventos_1_2_4_deixam_checkpoint_2_e_lacuna_3(
    banco: Consolidado, geracao: int
) -> None:
    metricas = Metricas()

    desfechos = _projetar(banco, 1, 2, 4, metricas=metricas)

    assert desfechos == [Desfecho.APLICADO] * 3
    assert _checkpoint(banco, geracao) == (2, [3])
    assert _medidor(metricas, "cashflow_checkpoint_posicao", geracao) == 2
    assert _medidor(metricas, "cashflow_lacunas", geracao) == 1


def test_chegada_do_evento_3_leva_o_checkpoint_a_4_e_remove_a_lacuna(
    banco: Consolidado, geracao: int
) -> None:
    _projetar(banco, 1, 2, 4)
    metricas = Metricas()

    _projetar(banco, 3, metricas=metricas)

    assert _checkpoint(banco, geracao) == (4, [])
    assert _medidor(metricas, "cashflow_checkpoint_posicao", geracao) == 4
    assert _medidor(metricas, "cashflow_lacunas", geracao) == 0


def test_lacunas_multiplas_fecham_em_qualquer_ordem(
    banco: Consolidado, geracao: int
) -> None:
    _projetar(banco, 3, 6)
    assert _checkpoint(banco, geracao) == (0, [1, 2, 4, 5])

    _projetar(banco, 2, 1)
    assert _checkpoint(banco, geracao) == (3, [4, 5])

    _projetar(banco, 5)
    assert _checkpoint(banco, geracao) == (3, [4])

    _projetar(banco, 4)
    assert _checkpoint(banco, geracao) == (6, [])


def test_projecoes_concorrentes_fora_de_ordem_terminam_continuas(
    banco: Consolidado, geracao: int
) -> None:
    # Uma data por posição: o saldo do dia não serializa as transações, só a
    # linha do checkpoint. Em qualquer ordem de commit, nenhuma lacuna sobra
    # e o checkpoint chega à maior posição.
    posicoes = list(range(1, 41))
    random.Random(4).shuffle(posicoes)

    desfechos = _projetar(banco, *posicoes, concorrentes=8, data_por_posicao=True)

    assert desfechos == [Desfecho.APLICADO] * 40
    assert _checkpoint(banco, geracao) == (40, [])


def test_duplicata_e_conflito_de_posicao_nao_movem_o_checkpoint(
    banco: Consolidado, geracao: int
) -> None:
    primeira, intruso, terceira = _mensagem(1), _mensagem(1), _mensagem(3)
    mensagens = [primeira, primeira, intruso, terceira]

    desfechos = asyncio.run(_projetar_async(banco, mensagens, Metricas()))

    assert desfechos == [
        Desfecho.APLICADO,
        Desfecho.EVENTO_DUPLICADO,
        Desfecho.QUARENTENA_CONFLITO_POSICAO,
        Desfecho.APLICADO,
    ]
    assert _checkpoint(banco, geracao) == (1, [2])


def test_checkpoint_e_lacunas_sao_por_geracao(
    banco: Consolidado, geracao: int
) -> None:
    _projetar(banco, 1, 3)
    anterior = geracao
    nova = _nova_geracao_ativa(banco)

    _projetar(banco, 2)

    assert _checkpoint(banco, anterior) == (1, [2])
    assert _checkpoint(banco, nova) == (0, [1])


def test_publicar_checkpoint_le_o_estado_da_geracao_ativa(
    banco: Consolidado, geracao: int
) -> None:
    _projetar(banco, 1, 2, 5)
    metricas = Metricas()

    async def publicar() -> EstadoCheckpoint:
        pool: AsyncConnectionPool[psycopg.AsyncConnection[Any]] = (
            AsyncConnectionPool(
                banco.conninfo("consolidado_worker"),
                min_size=1,
                max_size=1,
                kwargs={"autocommit": True},
                open=False,
            )
        )
        await pool.open(wait=True)
        try:
            caso = ProjetarLancamento(
                UnidadeDeTrabalhoProjecaoPostgres(fabrica_de_conexao(pool)),
                MetricasWorker(metricas),
            )
            return await caso.publicar_checkpoint()
        finally:
            await pool.close()

    estado = asyncio.run(publicar())

    assert estado == EstadoCheckpoint(geracao_id=geracao, posicao_continua=2, lacunas=2)
    assert _medidor(metricas, "cashflow_checkpoint_posicao", geracao) == 2
    assert _medidor(metricas, "cashflow_lacunas", geracao) == 2
    assert _checkpoint(banco, geracao) == (2, [3, 4])

