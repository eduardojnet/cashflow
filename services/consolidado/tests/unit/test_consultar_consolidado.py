"""Casos de uso de consulta do consolidado (004-T5; FR-004 a FR-006, TD-008).

Sem infraestrutura: a leitura é um dublê da porta, como em
test_api_consolidado_http.py.
"""

import asyncio
import uuid
from datetime import date
from decimal import Decimal

import pytest
from consolidado.aplicacao import (
    ConsultaInvalida,
    ConsultarInclusao,
    CursorSaldos,
    ListarSaldos,
    SaldoConsultado,
)
from consolidado.tests.unit.test_api_consolidado_http import LeituraFalsa


def test_cursor_codifica_e_decodifica_a_ultima_data() -> None:
    cursor = CursorSaldos(date(2026, 11, 6))

    assert CursorSaldos.decodificar(cursor.codificar()) == cursor


@pytest.mark.parametrize("texto", ["", "nao-e-cursor", "MjAyNi0xMy0wMQ", "%%%"])
def test_cursor_invalido_e_recusado_sem_repetir_o_texto(texto: str) -> None:
    with pytest.raises(ConsultaInvalida) as erro:
        CursorSaldos.decodificar(texto)

    assert erro.value.campo == "cursor"
    assert texto not in erro.value.motivo or texto == ""


@pytest.mark.parametrize(("padrao", "maximo"), [(0, 10), (11, 10)])
def test_limites_de_pagina_incoerentes_sao_recusados(padrao: int, maximo: int) -> None:
    with pytest.raises(ValueError):
        ListarSaldos(LeituraFalsa(), limite_padrao=padrao, limite_maximo=maximo)


def test_limite_omitido_usa_o_padrao() -> None:
    leitura = LeituraFalsa()
    listar = ListarSaldos(leitura, limite_padrao=7, limite_maximo=10)

    asyncio.run(listar.executar("2026-11-01", "2026-11-30"))

    assert leitura.chamadas[-1][-1] == 8


def test_intervalo_de_um_dia_e_aceito() -> None:
    leitura = LeituraFalsa(dias={date(2026, 11, 6): (Decimal("1.00"), Decimal(0))})

    pagina = asyncio.run(ListarSaldos(leitura).executar("2026-11-06", "2026-11-06"))

    assert [item.data for item in pagina.itens] == [date(2026, 11, 6)]
    assert pagina.proximo_cursor is None


def test_inclusao_exige_uuid() -> None:
    with pytest.raises(ConsultaInvalida) as erro:
        asyncio.run(ConsultarInclusao(LeituraFalsa()).executar("123"))

    assert erro.value.campo == "lancamento_id"


def test_inclusao_aceita_uuid_valido() -> None:
    lancamento = uuid.uuid4()
    leitura = LeituraFalsa(incluidos={lancamento: 3})

    inclusao = asyncio.run(ConsultarInclusao(leitura).executar(str(lancamento)))

    assert (inclusao.incluido, inclusao.posicao) == (True, 3)


def test_valores_saem_como_texto_com_duas_casas() -> None:
    saldo = SaldoConsultado(
        date(2026, 11, 6), Decimal(10), Decimal("2.5"), Decimal("7.5"), Decimal(0)
    )

    assert saldo.como_dict() == {
        "data": "2026-11-06",
        "total_creditos": "10.00",
        "total_debitos": "2.50",
        "saldo_dia": "7.50",
        "saldo_acumulado": "0.00",
    }
