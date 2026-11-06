"""Saldo do dia: crédito soma, débito subtrai (004 FR-001, D-001, D-003; TD-004)."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from consolidado.dominio import (
    CampoInvalido,
    Contribuicao,
    DataDivergente,
    SaldoDiario,
)

DIA = date(2026, 11, 5)


def _contribuicao(
    tipo: str, valor: str, posicao: int = 1, data_lancamento: date = DIA
) -> Contribuicao:
    return Contribuicao.de(uuid.uuid4(), posicao, data_lancamento, tipo, valor)


def test_dia_sem_contribuicoes_tem_totais_e_saldo_zerados() -> None:
    saldo = SaldoDiario.vazio(DIA)

    assert saldo.data == DIA
    assert saldo.total_creditos == Decimal("0.00")
    assert saldo.total_debitos == Decimal("0.00")
    assert saldo.saldo_dia == Decimal("0.00")


def test_credito_soma_ao_total_de_creditos_e_ao_saldo_do_dia() -> None:
    saldo = SaldoDiario.vazio(DIA).aplicar(_contribuicao("CREDITO", "100.50"))

    assert saldo.total_creditos == Decimal("100.50")
    assert saldo.total_debitos == Decimal("0.00")
    assert saldo.saldo_dia == Decimal("100.50")


def test_debito_soma_ao_total_de_debitos_e_subtrai_do_saldo_do_dia() -> None:
    saldo = SaldoDiario.vazio(DIA).aplicar(_contribuicao("DEBITO", "40"))

    assert saldo.total_creditos == Decimal("0.00")
    assert saldo.total_debitos == Decimal("40.00")
    assert saldo.saldo_dia == Decimal("-40.00")


def test_saldo_do_dia_e_creditos_menos_debitos_em_qualquer_ordem() -> None:
    contribuicoes = [
        _contribuicao("CREDITO", "100.00", 1),
        _contribuicao("DEBITO", "30.25", 2),
        _contribuicao("CREDITO", "0.01", 3),
        _contribuicao("DEBITO", "70.00", 4),
    ]

    saldo = SaldoDiario.vazio(DIA)
    for contribuicao in contribuicoes:
        saldo = saldo.aplicar(contribuicao)
    invertido = SaldoDiario.vazio(DIA)
    for contribuicao in reversed(contribuicoes):
        invertido = invertido.aplicar(contribuicao)

    assert saldo == invertido
    assert saldo.total_creditos == Decimal("100.01")
    assert saldo.total_debitos == Decimal("100.25")
    assert saldo.saldo_dia == Decimal("-0.24")


def test_aplicar_nao_altera_o_saldo_anterior() -> None:
    anterior = SaldoDiario(DIA, Decimal("10.00"), Decimal("5.00"))

    novo = anterior.aplicar(_contribuicao("CREDITO", "1"))

    assert anterior.total_creditos == Decimal("10.00")
    assert novo.total_creditos == Decimal("11.00")
    assert novo.total_debitos == Decimal("5.00")


def test_saldo_parte_dos_totais_ja_persistidos() -> None:
    saldo = SaldoDiario(DIA, Decimal("250.00"), Decimal("300.00"))

    assert saldo.saldo_dia == Decimal("-50.00")
    assert saldo.aplicar(_contribuicao("DEBITO", "0.50")).saldo_dia == Decimal(
        "-50.50"
    )


def test_contribuicao_de_outro_dia_e_recusada() -> None:
    contribuicao = _contribuicao("CREDITO", "1", data_lancamento=date(2026, 11, 4))

    with pytest.raises(DataDivergente) as erro:
        SaldoDiario.vazio(DIA).aplicar(contribuicao)

    assert erro.value.campo == "data_lancamento"


def test_dia_do_saldo_e_a_data_do_lancamento_sem_conversao_de_fuso() -> None:
    # 31/12 em America/Sao_Paulo já é 01/01 em UTC a partir das 21h; o dia
    # é a data informada (D-003).
    contribuicao = _contribuicao("CREDITO", "1", data_lancamento=date(2026, 12, 31))

    saldo = SaldoDiario.vazio(contribuicao.data_lancamento).aplicar(contribuicao)

    assert saldo.data == date(2026, 12, 31)


@pytest.mark.parametrize(
    ("creditos", "debitos"),
    [
        (Decimal("-0.01"), Decimal(0)),
        (Decimal(0), Decimal("-1")),
        (Decimal("NaN"), Decimal(0)),
        (10, Decimal(0)),
    ],
)
def test_total_negativo_ou_que_nao_e_decimal_e_recusado(
    creditos: Decimal, debitos: Decimal
) -> None:
    with pytest.raises(CampoInvalido):
        SaldoDiario(DIA, creditos, debitos)
