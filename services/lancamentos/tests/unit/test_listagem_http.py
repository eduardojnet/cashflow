"""Contrato HTTP da listagem por intervalo (002-T7; design 002 §4).

Sem infraestrutura, com os dublês de test_api_http.py. O mesmo contrato roda
contra PostgreSQL e Keycloak reais em tests/integracao/test_listagem_livro.py.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from lancamentos.tests.unit.test_api_http import (
    DESCRICAO,
    TOKEN_CONSULTA,
    TOKEN_REGISTRO,
    TOKEN_SEM_ESCOPO,
    VALOR,
    Api,
    montar_api,
)
from lancamentos.tests.unit.test_listar_lancamentos import LivroEmMemoria


@pytest.fixture
def livro() -> LivroEmMemoria:
    livro = LivroEmMemoria()
    for data in ("2026-11-03", "2026-11-01", "2026-11-03", "2026-11-05"):
        livro.registrar(data)
    return livro


@pytest.fixture
def api(livro: LivroEmMemoria) -> Iterator[Api]:
    with montar_api(livro, limite_padrao=2, limite_maximo=3) as api:
        yield api


def _get(api: Api, token: str | None = TOKEN_CONSULTA, **parametros: str) -> Any:
    consulta = {"data_inicio": "2026-11-01", "data_fim": "2026-11-30", **parametros}
    cabecalhos = {} if token is None else {"Authorization": f"Bearer {token}"}
    return api.cliente.get("/v1/lancamentos", params=consulta, headers=cabecalhos)


def test_listagem_pagina_em_ordem_de_data_e_posicao(api: Api) -> None:
    primeira = _get(api)
    assert primeira.status_code == 200
    corpo = primeira.json()
    assert [item["posicao"] for item in corpo["itens"]] == [2, 1]
    assert corpo["proximo_cursor"]

    segunda = _get(api, cursor=corpo["proximo_cursor"]).json()

    assert [item["posicao"] for item in segunda["itens"]] == [3, 4]
    assert segunda["proximo_cursor"] is None


def test_limite_informado_e_respeitado(api: Api) -> None:
    corpo = _get(api, limite="3").json()

    assert len(corpo["itens"]) == 3


def test_limite_acima_do_maximo_e_422(api: Api) -> None:
    resposta = _get(api, limite="4")

    assert resposta.status_code == 422
    assert resposta.json()["campo"] == "limite"


def test_intervalo_vazio_e_200_com_lista_vazia(api: Api) -> None:
    resposta = _get(api, data_inicio="2026-12-01", data_fim="2026-12-31")

    assert resposta.status_code == 200
    assert resposta.json() == {"itens": [], "proximo_cursor": None}


def test_data_inicio_maior_que_data_fim_e_422(api: Api) -> None:
    resposta = _get(api, data_inicio="2026-11-30", data_fim="2026-11-01")

    assert resposta.status_code == 422
    corpo = resposta.json()
    assert corpo["erro"] == "validacao"
    assert corpo["mensagem"] == "consulta inválida"
    assert corpo["campo"] == "data_inicio"


def test_sem_datas_e_422(api: Api) -> None:
    resposta = api.cliente.get(
        "/v1/lancamentos", headers={"Authorization": f"Bearer {TOKEN_CONSULTA}"}
    )

    assert resposta.status_code == 422
    assert resposta.json()["campo"] == "data_inicio"


def test_cursor_invalido_e_422(api: Api) -> None:
    resposta = _get(api, cursor="nao-e-cursor")

    assert resposta.status_code == 422
    assert resposta.json()["campo"] == "cursor"


def test_token_de_registro_sem_consultar_e_403(api: Api) -> None:
    resposta = _get(api, token=TOKEN_REGISTRO)

    assert resposta.status_code == 403
    assert resposta.json()["erro"] == "sem_escopo"


def test_token_de_consulta_no_registro_e_403(api: Api) -> None:
    resposta = api.cliente.post(
        "/v1/lancamentos",
        json={"tipo": "CREDITO", "valor": "1.00", "data_lancamento": "2026-11-06"},
        headers={"Authorization": f"Bearer {TOKEN_CONSULTA}", "Idempotency-Key": "k"},
    )

    assert resposta.status_code == 403


@pytest.mark.parametrize("token", [None, "outro-token"])
def test_sem_token_valido_e_401(api: Api, token: str | None) -> None:
    assert _get(api, token=token).status_code == 401


def test_autenticacao_vem_antes_dos_parametros(api: Api) -> None:
    resposta = _get(api, token=TOKEN_SEM_ESCOPO, data_inicio="invalida")

    assert resposta.status_code == 403


def test_log_da_listagem_nao_contem_valor_nem_descricao() -> None:
    livro = LivroEmMemoria()
    livro.registrar("2026-11-06", VALOR, DESCRICAO)
    with montar_api(livro) as api:
        corpo = _get(api).json()
        saida = api.saida_log.getvalue()
        (linha,) = [r for r in api.logs() if r["mensagem"] == "requisição atendida"]

    # A resposta traz valor e descrição; o log, só a contagem.
    assert corpo["itens"][0]["valor"] == VALOR
    assert corpo["itens"][0]["descricao"] == DESCRICAO
    assert linha["resultado"] == "listado"
    assert linha["itens"] == 1
    for sensivel in (TOKEN_CONSULTA, "eyJ", VALOR, DESCRICAO):
        assert sensivel not in saida
