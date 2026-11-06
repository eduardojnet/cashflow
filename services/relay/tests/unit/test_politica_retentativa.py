"""Política de retentativa com espera exponencial e teto (003-T3; D-002)."""

from datetime import timedelta

import pytest
from relay.aplicacao import PoliticaRetentativa


def test_espera_dobra_a_cada_falha_a_partir_da_base() -> None:
    politica = PoliticaRetentativa(
        base=timedelta(milliseconds=500), teto=timedelta(hours=1)
    )

    assert [politica.espera(n) for n in range(5)] == [
        timedelta(milliseconds=500),
        timedelta(seconds=1),
        timedelta(seconds=2),
        timedelta(seconds=4),
        timedelta(seconds=8),
    ]


def test_espera_para_no_teto_e_as_tentativas_nao_tem_limite() -> None:
    politica = PoliticaRetentativa(base=timedelta(seconds=1), teto=timedelta(minutes=5))

    assert politica.espera(8) == timedelta(seconds=256)
    assert politica.espera(9) == timedelta(minutes=5)
    # Sem estouro com contadores grandes: a espera só para no teto (D-002).
    assert politica.espera(10_000) == timedelta(minutes=5)
    assert politica.espera(2**31 - 1) == timedelta(minutes=5)


def test_teto_igual_a_base_e_espera_constante() -> None:
    politica = PoliticaRetentativa(base=timedelta(seconds=3), teto=timedelta(seconds=3))

    assert {politica.espera(n) for n in range(10)} == {timedelta(seconds=3)}


@pytest.mark.parametrize(
    ("base", "teto"),
    [
        (timedelta(0), timedelta(seconds=1)),
        (timedelta(seconds=-1), timedelta(seconds=1)),
        (timedelta(seconds=2), timedelta(seconds=1)),
    ],
    ids=["base_zero", "base_negativa", "teto_menor_que_base"],
)
def test_politica_invalida_e_recusada(base: timedelta, teto: timedelta) -> None:
    with pytest.raises(ValueError):
        PoliticaRetentativa(base=base, teto=teto)


def test_tentativas_negativas_sao_recusadas() -> None:
    politica = PoliticaRetentativa(base=timedelta(seconds=1), teto=timedelta(minutes=1))

    with pytest.raises(ValueError):
        politica.espera(-1)
