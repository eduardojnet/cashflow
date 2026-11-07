"""Leitura da fase da reconstrução pela API, sem banco (005-T3; 007 design §4)."""

import asyncio

import psycopg
from consolidado.adaptadores.http.fase import atualizar_fase, vigiar_fase
from observabilidade import Metricas

METRICA = "cashflow_reconstrucao_fase"


def _valor(metricas: Metricas) -> float | None:
    for linha in metricas.exposicao().decode().splitlines():
        if linha.startswith(f"{METRICA} "):
            return float(linha.split()[1])
    return None


def test_medidor_recebe_o_codigo_da_fase_lida() -> None:
    metricas = Metricas()
    medidor = metricas.medidor(METRICA)

    async def ler() -> str | None:
        return "construindo"

    assert asyncio.run(atualizar_fase(ler, medidor)) is True
    assert _valor(metricas) == 1


def test_sem_linha_de_fase_o_medidor_fica_em_zero() -> None:
    metricas = Metricas()
    medidor = metricas.medidor(METRICA)
    medidor.set(5)

    async def ler() -> str | None:
        return None

    asyncio.run(atualizar_fase(ler, medidor))

    assert _valor(metricas) == 0


def test_banco_fora_mantem_o_ultimo_valor() -> None:
    metricas = Metricas()
    medidor = metricas.medidor(METRICA)
    medidor.set(1)

    async def ler() -> str | None:
        raise psycopg.OperationalError("banco fora")

    assert asyncio.run(atualizar_fase(ler, medidor)) is False
    assert _valor(metricas) == 1


def test_vigia_le_de_novo_a_cada_intervalo_ate_ser_cancelada() -> None:
    metricas = Metricas()
    medidor = metricas.medidor(METRICA)
    fases = iter(["construindo", "cancelada"])
    leituras = 0

    async def ler() -> str | None:
        nonlocal leituras
        leituras += 1
        return next(fases, "cancelada")

    async def cenario() -> None:
        vigia = asyncio.create_task(vigiar_fase(ler, medidor, 0.01))
        while leituras < 2:
            await asyncio.sleep(0.01)
        vigia.cancel()
        try:
            await vigia
        except asyncio.CancelledError:
            pass

    asyncio.run(cenario())

    assert _valor(metricas) == 5
