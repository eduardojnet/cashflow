"""Transições de geração e início da reconstrução (005-T1; design 005 §3)."""

from itertools import product

import pytest
from coordenador.dominio import (
    RECEPTORAS,
    EstadoGeracao,
    Geracao,
    ReconstrucaoRecusada,
    TransicaoInvalida,
    transicao_valida,
    verificar_inicio_reconstrucao,
)

E = EstadoGeracao
VALIDAS = {
    (E.CONSTRUINDO, E.ATIVA),
    (E.ATIVA, E.ANTERIOR),
    (E.ANTERIOR, E.ATIVA),
    (E.CONSTRUINDO, E.DESCARTADA),
    (E.ANTERIOR, E.DESCARTADA),
}
INVALIDAS = [par for par in product(E, repeat=2) if par not in VALIDAS]


@pytest.mark.parametrize(("origem", "destino"), sorted(VALIDAS))
def test_transicao_do_diagrama_e_aceita(
    origem: EstadoGeracao, destino: EstadoGeracao
) -> None:
    geracao = Geracao(id=7, estado=origem)

    assert geracao.transitar(destino) == Geracao(id=7, estado=destino)
    assert transicao_valida(origem, destino)


@pytest.mark.parametrize(("origem", "destino"), INVALIDAS)
def test_transicao_invalida_e_recusada(
    origem: EstadoGeracao, destino: EstadoGeracao
) -> None:
    geracao = Geracao(id=7, estado=origem)

    with pytest.raises(TransicaoInvalida) as erro:
        geracao.transitar(destino)

    assert (erro.value.origem, erro.value.destino) == (origem, destino)
    assert not transicao_valida(origem, destino)


def test_descartada_e_estado_final() -> None:
    assert not any(transicao_valida(E.DESCARTADA, destino) for destino in E)


def test_nenhuma_transicao_volta_a_construindo() -> None:
    assert not any(transicao_valida(origem, E.CONSTRUINDO) for origem in E)


def test_somente_descartada_deixa_de_receber_eventos() -> None:
    assert RECEPTORAS == {E.ATIVA, E.CONSTRUINDO, E.ANTERIOR}
    assert not Geracao(id=1, estado=E.DESCARTADA).recebe_eventos
    assert Geracao(id=1, estado=E.ANTERIOR).recebe_eventos


@pytest.mark.parametrize(
    "estados",
    [
        [E.ATIVA],
        [E.ATIVA, E.DESCARTADA],
        [E.ATIVA, E.DESCARTADA, E.DESCARTADA],
    ],
    ids=["so-ativa", "ativa-e-descartada", "varias-descartadas"],
)
def test_reconstrucao_inicia_sem_construindo_nem_anterior(
    estados: list[EstadoGeracao],
) -> None:
    geracoes = [Geracao(id=i, estado=estado) for i, estado in enumerate(estados)]

    verificar_inicio_reconstrucao(geracoes)


@pytest.mark.parametrize(
    ("estados", "impedimentos"),
    [
        ([E.ATIVA, E.CONSTRUINDO], ["construindo"]),
        ([E.ATIVA, E.ANTERIOR], ["anterior"]),
        ([E.ATIVA, E.ANTERIOR, E.DESCARTADA], ["anterior"]),
        ([E.CONSTRUINDO, E.ANTERIOR], ["construindo", "anterior"]),
    ],
    ids=["com-construindo", "com-anterior", "anterior-e-descartada", "ambas"],
)
def test_reconstrucao_recusada_com_anterior_ou_construindo_existente(
    estados: list[EstadoGeracao], impedimentos: list[str]
) -> None:
    geracoes = [Geracao(id=i, estado=estado) for i, estado in enumerate(estados)]

    with pytest.raises(ReconstrucaoRecusada) as erro:
        verificar_inicio_reconstrucao(geracoes)

    assert erro.value.impedimentos == impedimentos
