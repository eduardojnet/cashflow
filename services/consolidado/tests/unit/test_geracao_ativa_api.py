"""Leitura periódica da geração ativa pela API, sem banco (007 design §4).

O medidor mostra a geração apontada em `controle_projecao` desde a subida,
sem esperar a primeira consulta (review 007, Issue 2).
"""

import asyncio

import psycopg
from consolidado.adaptadores.http.geracao import (
    atualizar_geracao_ativa,
    vigiar_geracao_ativa,
)
from observabilidade import Metricas

METRICA = "cashflow_geracao_ativa"


def _valor(metricas: Metricas) -> float | None:
    return metricas.registro.get_sample_value(METRICA)


def test_medidor_recebe_a_geracao_apontada_no_controle() -> None:
    metricas = Metricas()
    medidor = metricas.medidor(METRICA)

    async def ler() -> int | None:
        return 3

    assert asyncio.run(atualizar_geracao_ativa(ler, medidor)) is True
    assert _valor(metricas) == 3


def test_banco_fora_mantem_o_ultimo_valor() -> None:
    metricas = Metricas()
    medidor = metricas.medidor(METRICA)
    medidor.set(2)

    async def ler() -> int | None:
        raise psycopg.OperationalError("banco fora")

    assert asyncio.run(atualizar_geracao_ativa(ler, medidor)) is False
    assert _valor(metricas) == 2


def test_sem_linha_de_controle_o_medidor_nao_inventa_geracao() -> None:
    metricas = Metricas()
    medidor = metricas.medidor(METRICA)
    medidor.set(2)

    async def ler() -> int | None:
        return None

    assert asyncio.run(atualizar_geracao_ativa(ler, medidor)) is False
    assert _valor(metricas) == 2


def test_vigia_acompanha_a_troca_sem_depender_de_consulta() -> None:
    metricas = Metricas()
    medidor = metricas.medidor(METRICA)
    # Subida na geração 1; troca para a 2 entre duas leituras.
    geracoes = iter([1, 2])
    leituras = 0

    async def ler() -> int | None:
        nonlocal leituras
        leituras += 1
        return next(geracoes, 2)

    async def cenario() -> None:
        vigia = asyncio.create_task(vigiar_geracao_ativa(ler, medidor, 0.01))
        while leituras < 2:
            await asyncio.sleep(0.01)
        vigia.cancel()
        try:
            await vigia
        except asyncio.CancelledError:
            pass

    asyncio.run(cenario())

    assert _valor(metricas) == 2
