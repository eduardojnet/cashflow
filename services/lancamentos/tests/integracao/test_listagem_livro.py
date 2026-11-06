"""Listagem por intervalo contra PostgreSQL e Keycloak reais (002-T7; INFRA
9.1, 9.3).

Mesma montagem de test_api_http_livro.py: a API do processo real sobre um
PostgreSQL descartável do livro, com tokens emitidos pelo Keycloak do
compose. Os lançamentos entram pelo `POST`, com as posições atribuídas pelo
contador real, e são lidos pelo `GET` com o token do cliente de consultas.

Com o ambiente no ar, a partir da raiz do repositório:

    KEYCLOAK_URL=https://localhost:8443 uv run --env-file infra/.env \\
      pytest services/lancamentos/tests/integracao/test_listagem_livro.py

Sem docker no PATH ou sem `KEYCLOAK_URL`, o módulo é pulado com o motivo.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from livro_descartavel import livro_descartavel
from test_api_http_livro import Api, _api_sobre, _token
from test_api_http_livro import pytestmark as pytestmark


@pytest.fixture(scope="module")
def api() -> Iterator[Api]:
    with (
        livro_descartavel("listagem-livro") as livro,
        _api_sobre(livro) as api,
    ):
        yield api


@pytest.fixture(scope="module")
def token_registro() -> str:
    return _token("cliente-lancamentos")


@pytest.fixture(scope="module")
def token_consulta() -> str:
    return _token("cliente-consultas")


def _registrar(api: Api, token: str, data: str) -> int:
    resposta = api.registrar(token, f"chave-{uuid.uuid4()}", data_lancamento=data)
    assert resposta.status_code == 201, resposta.text
    posicao: int = resposta.json()["posicao"]
    return posicao


def _listar(api: Api, token: str, **parametros: str) -> Any:
    return api.cliente.get(
        "/v1/lancamentos",
        params=parametros,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_paginas_cobrem_o_intervalo_sem_repeticao_com_insercao_retroativa(
    api: Api, token_registro: str, token_consulta: str
) -> None:
    # Ano próprio, para não misturar com os demais testes do módulo.
    datas = ["2031-03-10", "2031-03-02", "2031-03-10", "2031-03-05", "2031-03-02"]
    existentes = {_registrar(api, token_registro, data): data for data in datas}
    # Fora do intervalo: não pode aparecer.
    _registrar(api, token_registro, "2031-04-01")
    intervalo = {"data_inicio": "2031-03-01", "data_fim": "2031-03-31"}

    primeira = _listar(api, token_consulta, limite="2", **intervalo)
    assert primeira.status_code == 200
    vistos = [item["posicao"] for item in primeira.json()["itens"]]
    cursor = primeira.json()["proximo_cursor"]
    assert cursor

    # Durante a paginação: um retroativo antes do cursor, que com offset
    # empurraria um item já lido para a página seguinte, e um depois.
    retroativo = _registrar(api, token_registro, "2031-03-01")
    posterior = _registrar(api, token_registro, "2031-03-20")

    itens: list[dict[str, Any]] = list(primeira.json()["itens"])
    while cursor:
        pagina = _listar(api, token_consulta, limite="2", cursor=cursor, **intervalo)
        assert pagina.status_code == 200
        itens.extend(pagina.json()["itens"])
        vistos.extend(item["posicao"] for item in pagina.json()["itens"])
        cursor = pagina.json()["proximo_cursor"]

    assert len(vistos) == len(set(vistos))
    assert set(existentes) <= set(vistos)
    assert posterior in vistos
    # Fica antes do cursor já entregue: aparece numa nova listagem.
    assert retroativo not in vistos
    ordem = [(item["data_lancamento"], item["posicao"]) for item in itens]
    assert ordem == sorted(ordem)
    for item in itens:
        if item["posicao"] in existentes:
            assert item["data_lancamento"] == existentes[item["posicao"]]

    completa = _listar(api, token_consulta, limite="500", **intervalo).json()
    assert {item["posicao"] for item in completa["itens"]} == {
        *existentes,
        retroativo,
        posterior,
    }
    assert completa["proximo_cursor"] is None


def test_item_traz_os_campos_gravados_no_livro(
    api: Api, token_registro: str, token_consulta: str
) -> None:
    posicao = _registrar(api, token_registro, "2032-01-15")

    corpo = _listar(
        api, token_consulta, data_inicio="2032-01-15", data_fim="2032-01-15"
    ).json()

    (item,) = corpo["itens"]
    assert item["posicao"] == posicao
    assert item["tipo"] == "CREDITO"
    assert item["data_lancamento"] == "2032-01-15"
    linhas = api.consultar(
        "SELECT id::text, valor::text, descricao FROM lancamento WHERE posicao = %s",
        posicao,
    )
    assert linhas == [(item["id"], item["valor"], item["descricao"])]
    assert item["registrado_em"]


def test_intervalo_vazio_e_lista_vazia(api: Api, token_consulta: str) -> None:
    resposta = _listar(
        api, token_consulta, data_inicio="2040-01-01", data_fim="2040-12-31"
    )

    assert resposta.status_code == 200
    assert resposta.json() == {"itens": [], "proximo_cursor": None}


def test_data_inicio_maior_que_data_fim_e_422(api: Api, token_consulta: str) -> None:
    resposta = _listar(
        api, token_consulta, data_inicio="2031-03-31", data_fim="2031-03-01"
    )

    assert resposta.status_code == 422
    assert resposta.json()["campo"] == "data_inicio"


def test_token_com_registrar_sem_consultar_e_403(
    api: Api, token_registro: str
) -> None:
    resposta = _listar(
        api, token_registro, data_inicio="2031-03-01", data_fim="2031-03-31"
    )

    assert resposta.status_code == 403
    assert resposta.json()["erro"] == "sem_escopo"
