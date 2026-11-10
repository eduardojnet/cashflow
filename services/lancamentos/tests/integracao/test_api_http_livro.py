"""API de lançamentos contra PostgreSQL e Keycloak reais (002-T5; INFRA 9.1,
9.3).

A API é montada pela mesma composição do processo real
(`criar_app_do_ambiente`): pool psycopg do papel livro_api num PostgreSQL
descartável do livro (ver livro_descartavel.py) e validador JWT com o JWKS
do Keycloak do compose, lido por TLS. Os tokens são emitidos pelo Keycloak
por Client Credentials. O último teste para um segundo banco descartável com
`docker stop` e confere o ready falso e o 503 no registro.

Com o ambiente no ar, a partir da raiz do repositório:

    KEYCLOAK_URL=https://localhost:8443 uv run --env-file infra/.env \\
      pytest services/lancamentos/tests/integracao/test_api_http_livro.py

Sem docker no PATH ou sem `KEYCLOAK_URL`, o módulo é pulado com o motivo.
"""

import io
import json
import logging
import os
import shutil
import ssl
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from lancamentos.adaptadores.http import FormatadorJson
from lancamentos.adaptadores.http.principal import criar_app_do_ambiente
from lancamentos.tests.integracao.livro_descartavel import (
    Livro,
    docker,
    livro_descartavel,
)
from observabilidade import Metricas

URL = os.environ.get("KEYCLOAK_URL")
RAIZ = Path(__file__).resolve().parents[4]
AC = RAIZ / "infra/tls/gerados/ac/ac-cashflow.crt"
REALM = "cashflow"
EMISSOR = f"https://localhost:8443/realms/{REALM}"

# Marcadores fáceis de achar em qualquer saída de log.
VALOR = "8765.43"
DESCRICAO = "descricao-sigilosa-integracao"

pytestmark = [
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker ausente"),
    pytest.mark.skipif(
        URL is None,
        reason="KEYCLOAK_URL ausente: executar com o ambiente no ar (ver docstring)",
    ),
]


def _token(cliente: str) -> str:
    variavel = {
        "cliente-lancamentos": "KEYCLOAK_SEGREDO_CLIENTE_LANCAMENTOS",
        "cliente-consultas": "KEYCLOAK_SEGREDO_CLIENTE_CONSULTAS",
    }[cliente]
    campos = {
        "grant_type": "client_credentials",
        "client_id": cliente,
        "client_secret": os.environ[variavel],
    }
    requisicao = urllib.request.Request(
        f"{URL}/realms/{REALM}/protocol/openid-connect/token",
        data=urllib.parse.urlencode(campos).encode(),
        method="POST",
    )
    contexto = ssl.create_default_context(cafile=str(AC))
    with urllib.request.urlopen(requisicao, context=contexto, timeout=10) as resposta:
        token: str = json.load(resposta)["access_token"]
        return token


@dataclass
class Api:
    cliente: TestClient
    livro: Livro
    metricas: Metricas
    saida_log: io.StringIO

    def amostra(self, nome: str, rotulos: dict[str, str]) -> float:
        return self.metricas.registro.get_sample_value(nome, rotulos) or 0.0

    def consultar(self, sql: str, *params: object) -> list[tuple[Any, ...]]:
        # livro_migracao, dono das tabelas: livro_api não lê a outbox.
        with psycopg.connect(self.livro.conninfo("livro_migracao")) as conexao:
            return conexao.execute(sql, params).fetchall()

    def registrar(self, token: str | None, chave: str, **campos: object) -> Any:
        corpo: dict[str, object] = {
            "tipo": "CREDITO",
            "valor": VALOR,
            "data_lancamento": "2026-11-06",
            "descricao": DESCRICAO,
            **campos,
        }
        cabecalhos = {"Idempotency-Key": chave}
        if token is not None:
            cabecalhos["Authorization"] = f"Bearer {token}"
        return self.cliente.post("/v1/lancamentos", json=corpo, headers=cabecalhos)


@contextmanager
def _api_sobre(livro: Livro) -> Iterator[Api]:
    ambiente = {
        "PG_LIVRO_HOST": "127.0.0.1",
        "PG_LIVRO_PORTA": str(livro.porta),
        "PG_LIVRO_SENHA_API": livro.senhas["livro_api"],
        "JWT_EMISSOR": EMISSOR,
        "JWT_JWKS_URL": f"{URL}/realms/{REALM}/protocol/openid-connect/certs",
        "JWT_AC_ARQUIVO": str(AC),
    }
    metricas = Metricas()
    app = criar_app_do_ambiente(ambiente, metricas)
    saida = io.StringIO()
    manipulador = logging.StreamHandler(saida)
    manipulador.setFormatter(FormatadorJson())
    raiz = logging.getLogger()
    nivel = raiz.level
    raiz.addHandler(manipulador)
    raiz.setLevel(logging.INFO)
    try:
        with TestClient(app) as cliente:
            api = Api(cliente, livro, metricas, saida)
            _aguardar_ready(api, 200)
            yield api
    finally:
        raiz.removeHandler(manipulador)
        raiz.setLevel(nivel)


def _aguardar_ready(api: Api, status: int, limite_segundos: float = 15) -> Any:
    # O pool abre sem esperar o banco; a primeira conexão leva um instante.
    fim = time.monotonic() + limite_segundos
    while True:
        resposta = api.cliente.get("/health/ready")
        if resposta.status_code == status or time.monotonic() > fim:
            return resposta
        time.sleep(0.5)


@pytest.fixture(scope="module")
def api() -> Iterator[Api]:
    with (
        livro_descartavel("api-http-livro") as livro,
        _api_sobre(livro) as api,
    ):
        yield api


@pytest.fixture(scope="module")
def token_registro() -> str:
    return _token("cliente-lancamentos")


def test_ready_com_banco_e_jwks_reais(api: Api) -> None:
    resposta = api.cliente.get("/health/ready")

    assert resposta.status_code == 200
    assert resposta.json()["verificacoes"] == {"banco": True, "jwks": True}


def test_registro_com_token_do_keycloak_grava_no_livro(
    api: Api, token_registro: str
) -> None:
    resposta = api.registrar(token_registro, f"chave-{uuid.uuid4()}")

    assert resposta.status_code == 201
    corpo = resposta.json()
    linhas = api.consultar(
        "SELECT posicao, cliente_id, valor::text, descricao FROM lancamento"
        " WHERE id = %s",
        uuid.UUID(corpo["id"]),
    )
    assert linhas == [(corpo["posicao"], "cliente-lancamentos", VALOR, DESCRICAO)]
    eventos = api.consultar(
        "SELECT posicao FROM outbox WHERE lancamento_id = %s", uuid.UUID(corpo["id"])
    )
    assert eventos == [(corpo["posicao"],)]


def test_repeticao_identica_devolve_a_resposta_original(
    api: Api, token_registro: str
) -> None:
    chave = f"chave-{uuid.uuid4()}"
    primeira = api.registrar(token_registro, chave)
    (antes,) = api.consultar("SELECT count(*) FROM lancamento")[0]

    segunda = api.registrar(token_registro, chave)

    assert segunda.status_code == primeira.status_code == 201
    assert segunda.json() == primeira.json()
    assert api.consultar("SELECT count(*) FROM lancamento")[0] == (antes,)


def test_mesma_chave_com_outro_conteudo_e_409(api: Api, token_registro: str) -> None:
    chave = f"chave-{uuid.uuid4()}"
    api.registrar(token_registro, chave)

    resposta = api.registrar(token_registro, chave, valor="1.00")

    assert resposta.status_code == 409


def test_lancamento_invalido_e_422_sem_registro(api: Api, token_registro: str) -> None:
    (antes,) = api.consultar("SELECT count(*) FROM lancamento")[0]

    resposta = api.registrar(token_registro, f"chave-{uuid.uuid4()}", valor="1.001")

    assert resposta.status_code == 422
    assert resposta.json()["campo"] == "valor"
    assert api.consultar("SELECT count(*) FROM lancamento")[0] == (antes,)


def test_token_do_cliente_de_consultas_e_403_no_registro(api: Api) -> None:
    resposta = api.registrar(_token("cliente-consultas"), f"chave-{uuid.uuid4()}")

    assert resposta.status_code == 403


def test_token_adulterado_e_401(api: Api, token_registro: str) -> None:
    cabecalho, carga, assinatura = token_registro.split(".")
    trocado = "A" if assinatura[10] != "A" else "B"
    adulterado = f"{cabecalho}.{carga}.{assinatura[:10]}{trocado}{assinatura[11:]}"

    resposta = api.registrar(adulterado, f"chave-{uuid.uuid4()}")

    assert resposta.status_code == 401
    assert api.amostra("cashflow_jwt_falhas_total", {"motivo": "assinatura"}) >= 1


def test_metricas_de_requisicoes_e_pool(api: Api) -> None:
    assert (
        api.amostra(
            "cashflow_http_requisicoes_total",
            {"componente": "lancamentos-api", "status": "201"},
        )
        >= 1
    )
    total = api.amostra(
        "cashflow_pool_conexoes", {"componente": "lancamentos-api", "estado": "total"}
    )
    assert total >= 1


def test_logs_nao_contem_token_valor_nem_descricao(
    api: Api, token_registro: str
) -> None:
    # Roda depois dos demais: inspeciona tudo o que o módulo registrou.
    saida = api.saida_log.getvalue()
    linhas = [json.loads(linha) for linha in saida.splitlines()]

    assert any(linha.get("lancamento_id") for linha in linhas)
    for sensivel in (token_registro, "eyJ", VALOR, DESCRICAO):
        assert sensivel not in saida


def test_ready_falso_e_registro_503_com_banco_parado(token_registro: str) -> None:
    with (
        livro_descartavel("api-http-banco-parado") as livro,
        _api_sobre(livro) as api,
    ):
        assert api.cliente.get("/health/ready").status_code == 200

        parado = docker("stop", livro.id_conteiner, timeout=60)
        assert parado.returncode == 0, parado.stderr

        pronto = api.cliente.get("/health/ready")
        assert pronto.status_code == 503
        assert pronto.json()["verificacoes"] == {"banco": False, "jwks": True}
        registro = api.registrar(token_registro, f"chave-{uuid.uuid4()}")
        assert registro.status_code == 503
        assert registro.json()["erro"] == "indisponivel"
        assert api.cliente.get("/health/live").status_code == 200
