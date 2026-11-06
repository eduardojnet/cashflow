"""Sinais do worker para a barreira dos consumidores (005-T2; design 005 §4).

`pausado = true` só é gravado por `confirmar_pausa`, que o laço chama com o
consumo já fechado; com a barreira solicitada, `sinalizar_consumo` não grava
nada, para nunca afirmar que o worker consome quando ele vai parar.
"""

import asyncio

import pytest
from consolidado.aplicacao import BarreiraConsumidor


class _Controle:
    def __init__(self, solicitada: bool) -> None:
        self.solicitada = solicitada
        self.registros: list[tuple[str, bool]] = []

    async def barreira_solicitada(self) -> bool:
        return self.solicitada

    async def registrar_consumidor(self, consumidor_id: str, pausado: bool) -> None:
        self.registros.append((consumidor_id, pausado))


def test_com_a_barreira_liberada_o_consumo_e_registrado_como_ativo() -> None:
    controle = _Controle(solicitada=False)

    solicitada = asyncio.run(BarreiraConsumidor(controle, "w1").sinalizar_consumo())

    assert solicitada is False
    assert controle.registros == [("w1", False)]


def test_com_a_barreira_solicitada_o_sinal_de_consumo_nao_grava_nada() -> None:
    controle = _Controle(solicitada=True)

    solicitada = asyncio.run(BarreiraConsumidor(controle, "w1").sinalizar_consumo())

    assert solicitada is True
    assert controle.registros == []


@pytest.mark.parametrize("solicitada", [True, False])
def test_confirmar_pausa_grava_pausado_e_devolve_o_estado_da_barreira(
    solicitada: bool,
) -> None:
    controle = _Controle(solicitada)

    resposta = asyncio.run(BarreiraConsumidor(controle, "w1").confirmar_pausa())

    assert resposta is solicitada
    assert controle.registros == [("w1", True)]


def test_consumidor_sem_identificador_e_recusado() -> None:
    with pytest.raises(ValueError, match="consumidor_id"):
        BarreiraConsumidor(_Controle(False), "")
