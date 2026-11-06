"""Caso de uso `ListarLancamentos`: intervalo, limite e cursor (002-T7).

Sem infraestrutura: a porta `ConsultaLivro` é um livro em memória com a
mesma ordem e a mesma condição de cursor da consulta real. O comportamento
contra PostgreSQL real está em tests/integracao/test_listagem_livro.py.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import pytest
from lancamentos.aplicacao import (
    ConsultaInvalida,
    Cursor,
    LancamentoListado,
    ListarLancamentos,
    PaginaLancamentos,
)
from lancamentos.dominio import Lancamento


@dataclass
class LivroEmMemoria:
    itens: list[LancamentoListado] = field(default_factory=list)
    chamadas: list[int] = field(default_factory=list)

    def registrar(
        self, data: str, valor: str = "10.00", descricao: str | None = None
    ) -> LancamentoListado:
        item = LancamentoListado(
            Lancamento.novo("CREDITO", valor, data, descricao),
            posicao=len(self.itens) + 1,
            registrado_em=datetime(2026, 11, 6, tzinfo=UTC),
        )
        self.itens.append(item)
        return item

    async def listar(
        self, data_inicio: date, data_fim: date, apos: Cursor | None, quantidade: int
    ) -> list[LancamentoListado]:
        self.chamadas.append(quantidade)
        no_intervalo = sorted(
            (
                item
                for item in self.itens
                if data_inicio <= item.lancamento.data_lancamento <= data_fim
                and (apos is None or item.cursor > apos)
            ),
            key=lambda item: item.cursor,
        )
        return no_intervalo[:quantidade]


def _executar(
    listar: ListarLancamentos,
    data_inicio: object = "2026-11-01",
    data_fim: object = "2026-11-30",
    limite: object = None,
    cursor: object = None,
) -> PaginaLancamentos:
    return asyncio.run(listar.executar(data_inicio, data_fim, limite, cursor))


def test_cursor_codificado_volta_ao_mesmo_par() -> None:
    cursor = Cursor(date(2026, 11, 6), 42)

    assert Cursor.decodificar(cursor.codificar()) == cursor


@pytest.mark.parametrize(
    "texto",
    ["", "nao-base64!", "e30", "WyIyMDI2LTEzLTAxIiwxXQ", "WyIyMDI2LTExLTAxIiwwXQ"],
)
def test_cursor_invalido_e_rejeitado(texto: str) -> None:
    # "e30" = {}; os dois últimos: mês 13 e posição 0.
    with pytest.raises(ConsultaInvalida) as erro:
        Cursor.decodificar(texto)

    assert erro.value.campo == "cursor"


def test_paginas_consecutivas_cobrem_o_intervalo_sem_repeticao() -> None:
    livro = LivroEmMemoria()
    for dia in (5, 3, 3, 7, 1, 3, 9):
        livro.registrar(f"2026-11-0{dia}")
    listar = ListarLancamentos(livro, limite_padrao=2)

    vistos: list[int] = []
    cursor: str | None = None
    while True:
        pagina = _executar(listar, cursor=cursor)
        vistos.extend(item.posicao for item in pagina.itens)
        if pagina.proximo_cursor is None:
            break
        cursor = pagina.proximo_cursor.codificar()

    assert sorted(vistos) == list(range(1, 8))
    assert len(vistos) == len(set(vistos))


def test_ultima_pagina_cheia_nao_tem_proximo_cursor() -> None:
    livro = LivroEmMemoria()
    livro.registrar("2026-11-06")
    livro.registrar("2026-11-06")

    pagina = _executar(ListarLancamentos(livro), limite="2")

    assert len(pagina.itens) == 2
    assert pagina.proximo_cursor is None
    # Um item a mais na consulta decide se há página seguinte.
    assert livro.chamadas == [3]


def test_intervalo_sem_lancamentos_e_lista_vazia() -> None:
    livro = LivroEmMemoria()
    livro.registrar("2026-10-31")

    pagina = _executar(ListarLancamentos(livro))

    assert pagina.como_dict() == {"itens": [], "proximo_cursor": None}


def test_data_inicio_maior_que_data_fim_e_rejeitada() -> None:
    with pytest.raises(ConsultaInvalida) as erro:
        _executar(ListarLancamentos(LivroEmMemoria()), "2026-11-02", "2026-11-01")

    assert erro.value.campo == "data_inicio"


def test_intervalo_de_um_dia_e_aceito() -> None:
    livro = LivroEmMemoria()
    livro.registrar("2026-11-06")

    pagina = _executar(ListarLancamentos(livro), "2026-11-06", "2026-11-06")

    assert len(pagina.itens) == 1


@pytest.mark.parametrize(
    ("campos", "campo"),
    [
        ({"data_inicio": None}, "data_inicio"),
        ({"data_fim": ""}, "data_fim"),
        ({"data_inicio": "01/11/2026"}, "data_inicio"),
        ({"data_fim": "2026-02-30"}, "data_fim"),
        ({"limite": "0"}, "limite"),
        ({"limite": "-1"}, "limite"),
        ({"limite": "abc"}, "limite"),
        ({"limite": "501"}, "limite"),
        ({"cursor": "invalido"}, "cursor"),
    ],
)
def test_parametro_invalido_e_rejeitado_com_campo(
    campos: dict[str, object], campo: str
) -> None:
    livro = LivroEmMemoria()
    with pytest.raises(ConsultaInvalida) as erro:
        _executar(ListarLancamentos(livro), **campos)

    assert erro.value.campo == campo
    assert livro.chamadas == []


def test_limite_padrao_e_maximo_vem_da_configuracao() -> None:
    livro = LivroEmMemoria()
    listar = ListarLancamentos(livro, limite_padrao=3, limite_maximo=5)

    _executar(listar)
    _executar(listar, limite="5")
    with pytest.raises(ConsultaInvalida):
        _executar(listar, limite="6")

    assert livro.chamadas == [4, 6]


def test_configuracao_de_limite_incoerente_e_recusada() -> None:
    with pytest.raises(ValueError):
        ListarLancamentos(LivroEmMemoria(), limite_padrao=10, limite_maximo=5)


def test_item_listado_tem_valor_em_texto_com_duas_casas() -> None:
    livro = LivroEmMemoria()
    item = livro.registrar("2026-11-06")

    corpo = item.como_dict()

    assert corpo["valor"] == "10.00"
    assert corpo["posicao"] == 1
    assert corpo["data_lancamento"] == "2026-11-06"
    assert set(corpo) == {
        "id",
        "posicao",
        "tipo",
        "valor",
        "data_lancamento",
        "descricao",
        "registrado_em",
    }
