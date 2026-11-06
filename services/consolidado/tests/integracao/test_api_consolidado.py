"""API do consolidado contra PostgreSQL e Keycloak reais (004-T5; INFRA 9.1,
9.3).

A API é montada pela mesma composição do processo real
(`criar_app_do_ambiente`): pool psycopg do papel consolidado_api num
PostgreSQL descartável do consolidado (ver consolidado_descartavel.py) e
validador JWT com o JWKS do Keycloak do compose, lido por TLS. Os tokens são
emitidos pelo Keycloak por Client Credentials.

Os saldos entram pelo caminho real do worker: `ProjetarLancamento` com a
unidade de trabalho PostgreSQL do papel consolidado_worker (004-T3), sem
SQL de teste nas tabelas da projeção. Só o teste da geração ativa escreve
direto no banco, como superusuário, para simular a geração candidata e a
troca de geração da spec 005.

Autorização (FR-008): o realm só emite a audiência `consolidado-api` junto
com o escopo `consolidado:consultar` (001 TD-008). O token do cliente de
lançamentos é recusado aqui com 401, pela audiência; o 403 de token aceito
sem o escopo é conferido no contrato HTTP, em
tests/unit/test_api_consolidado_http.py.

O último teste para um segundo banco descartável com `docker stop` e
confere o ready falso e o 503 nas consultas.

Com o ambiente no ar, a partir da raiz do repositório:

    KEYCLOAK_URL=https://localhost:8443 uv run --env-file infra/.env \\
      pytest services/consolidado/tests/integracao/test_api_consolidado.py

Sem docker no PATH ou sem `KEYCLOAK_URL`, o módulo é pulado com o motivo.
"""

import asyncio
import io
import itertools
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
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from consolidado.adaptadores.http.principal import criar_app_do_ambiente
from consolidado.adaptadores.logs import FormatadorJson
from consolidado.adaptadores.postgres import UnidadeDeTrabalhoProjecaoPostgres
from consolidado.aplicacao import Desfecho, MensagemEvento, ProjetarLancamento
from consolidado.worker.principal import fabrica_de_conexao
from consolidado_descartavel import Consolidado, consolidado_descartavel
from fastapi.testclient import TestClient
from livro_descartavel import docker
from observabilidade import Metricas
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

URL = os.environ.get("KEYCLOAK_URL")
RAIZ = Path(__file__).resolve().parents[4]
AC = RAIZ / "infra/tls/gerados/ac/ac-cashflow.crt"
REALM = "cashflow"
EMISSOR = f"https://localhost:8443/realms/{REALM}"

# Marcador fácil de achar em qualquer saída de log.
VALOR_SIGILOSO = "8765.43"

# Um mês por teste: os saldos de um teste não entram no acumulado do outro
# a não ser pelo que vem antes, que cada teste mede antes de projetar.
_MESES = (date(2030, 1, 1) + timedelta(days=31 * n) for n in itertools.count())
_POSICOES = itertools.count(1)

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


# --- projeção pelo caminho do worker -------------------------------------


def _evento(
    data: date, tipo: str, valor: str, lancamento_id: uuid.UUID | None = None
) -> dict[str, object]:
    """Evento `LancamentoRegistrado` v1 como o livro grava (002 design §4)."""
    return {
        "evento_id": str(uuid.uuid4()),
        "versao": 1,
        "lancamento_id": str(lancamento_id or uuid.uuid4()),
        "posicao": next(_POSICOES),
        "tipo": tipo,
        "valor": valor,
        "data_lancamento": data.isoformat(),
        "registrado_em": "2026-11-06T12:00:00+00:00",
    }


def _projetar(banco: Consolidado, *eventos: dict[str, object]) -> None:
    async def projetar() -> None:
        pool: AsyncConnectionPool[AsyncConnection[Any]] = AsyncConnectionPool(
            banco.conninfo("consolidado_worker"),
            min_size=1,
            max_size=1,
            kwargs={"autocommit": True},
            open=False,
        )
        await pool.open()
        try:
            caso = ProjetarLancamento(
                UnidadeDeTrabalhoProjecaoPostgres(fabrica_de_conexao(pool))
            )
            for evento in eventos:
                corpo = json.dumps(evento).encode()
                mensagem = MensagemEvento(corpo, evento, str(evento["evento_id"]))
                assert await caso.executar(mensagem) is Desfecho.APLICADO
        finally:
            await pool.close()

    asyncio.run(projetar())


# --- API ------------------------------------------------------------------


@dataclass
class Api:
    cliente: TestClient
    banco: Consolidado
    metricas: Metricas
    saida_log: io.StringIO
    token: str

    def amostra(self, nome: str, rotulos: dict[str, str] | None = None) -> float:
        return self.metricas.registro.get_sample_value(nome, rotulos or {}) or 0.0

    def get(self, caminho: str, token: str | None = None, **params: str) -> Any:
        token = self.token if token is None else token
        return self.cliente.get(
            caminho,
            params=params or None,
            headers={"Authorization": f"Bearer {token}"},
        )

    def saldo(self, data: date) -> Any:
        return self.get(f"/v1/consolidado/saldos/{data.isoformat()}")

    def saldos(self, inicio: date, fim: date, **params: str) -> Any:
        return self.get(
            "/v1/consolidado/saldos",
            data_inicio=inicio.isoformat(),
            data_fim=fim.isoformat(),
            **params,
        )

    def como_superusuario(self, sql: str, *params: object) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.banco.conninfo("postgres")) as conexao:
            cursor = conexao.execute(sql, params)
            return cursor.fetchall() if cursor.description else []


def _aguardar_ready(cliente: TestClient, status: int, limite: float = 15) -> Any:
    # O pool abre sem esperar o banco; a primeira conexão leva um instante.
    fim = time.monotonic() + limite
    while True:
        resposta = cliente.get("/health/ready")
        if resposta.status_code == status or time.monotonic() > fim:
            return resposta
        time.sleep(0.5)


@contextmanager
def _api_sobre(banco: Consolidado, token: str) -> Iterator[Api]:
    ambiente = {
        "PG_CONSOLIDADO_HOST": "127.0.0.1",
        "PG_CONSOLIDADO_PORTA": str(banco.porta),
        "PG_CONSOLIDADO_SENHA_API": banco.senhas["consolidado_api"],
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
            _aguardar_ready(cliente, 200)
            yield Api(cliente, banco, metricas, saida, token)
    finally:
        raiz.removeHandler(manipulador)
        raiz.setLevel(nivel)


@pytest.fixture(scope="module")
def token_consultas() -> str:
    return _token("cliente-consultas")


@pytest.fixture(scope="module")
def api(token_consultas: str) -> Iterator[Api]:
    with (
        consolidado_descartavel("api-consolidado") as banco,
        _api_sobre(banco, token_consultas) as api,
    ):
        yield api


@pytest.fixture
def mes() -> date:
    return next(_MESES)


def _checkpoint(api: Api) -> int:
    (linha,) = api.como_superusuario(
        "SELECT c.posicao_continua FROM checkpoint c"
        " JOIN controle_projecao p ON p.geracao_ativa_id = c.geracao_id"
    )
    posicao: int = linha[0]
    return posicao


def _acumulado_antes(api: Api, dia: date) -> str:
    # Acumulado até a véspera do mês do teste, lido pela própria API: o
    # módulo projeta bem menos de 500 dias, então uma página basta.
    itens = api.saldos(date(2000, 1, 1), dia - timedelta(days=1), limite="500").json()[
        "itens"
    ]
    return str(itens[-1]["saldo_acumulado"]) if itens else "0.00"


def _soma(*valores: str) -> str:
    return str(sum((Decimal(v) for v in valores), Decimal("0.00")))


# --- testes ---------------------------------------------------------------


def test_ready_com_banco_e_jwks_reais(api: Api) -> None:
    resposta = api.cliente.get("/health/ready")

    assert resposta.status_code == 200
    assert resposta.json()["verificacoes"] == {"banco": True, "jwks": True}


def test_saldo_da_data_com_token_do_keycloak(api: Api, mes: date) -> None:
    antes = _acumulado_antes(api, mes)
    _projetar(
        api.banco,
        _evento(mes, "CREDITO", "100.00"),
        _evento(mes, "DEBITO", "30.50"),
        _evento(mes, "CREDITO", "0.75"),
    )

    resposta = api.saldo(mes)

    assert resposta.status_code == 200
    assert resposta.json() == {
        "data": mes.isoformat(),
        "total_creditos": "100.75",
        "total_debitos": "30.50",
        "saldo_dia": "70.25",
        "saldo_acumulado": _soma(antes, "70.25"),
        "posicao_checkpoint": _checkpoint(api),
    }


def test_data_sem_lancamentos_e_404_com_checkpoint(api: Api, mes: date) -> None:
    _projetar(api.banco, _evento(mes, "CREDITO", "1.00"))

    resposta = api.saldo(mes + timedelta(days=1))

    assert resposta.status_code == 404
    corpo = resposta.json()
    assert corpo["erro"] == "nao_encontrado"
    assert corpo["posicao_checkpoint"] == _checkpoint(api) > 0


def test_intervalo_omite_dias_vazios_e_pagina(api: Api, mes: date) -> None:
    dias = [mes, mes + timedelta(days=2), mes + timedelta(days=5)]
    antes = _acumulado_antes(api, mes)
    _projetar(
        api.banco,
        _evento(dias[0], "CREDITO", "10.00"),
        _evento(dias[1], "DEBITO", "4.00"),
        _evento(dias[2], "CREDITO", "1.50"),
    )
    fim = mes + timedelta(days=30)

    primeira = api.saldos(mes, fim, limite="2").json()
    segunda = api.saldos(mes, fim, limite="2", cursor=primeira["proximo_cursor"]).json()

    itens = primeira["itens"] + segunda["itens"]
    assert [item["data"] for item in itens] == [dia.isoformat() for dia in dias]
    assert [item["saldo_acumulado"] for item in itens] == [
        _soma(antes, "10.00"),
        _soma(antes, "6.00"),
        _soma(antes, "7.50"),
    ]
    assert segunda["proximo_cursor"] is None
    assert primeira["posicao_checkpoint"] == _checkpoint(api)


def test_intervalo_sem_lancamentos_e_lista_vazia(api: Api) -> None:
    resposta = api.saldos(date(1990, 1, 1), date(1990, 12, 31))

    assert resposta.status_code == 200
    corpo = resposta.json()
    assert corpo["itens"] == []
    assert corpo["proximo_cursor"] is None


def test_acumulado_correto_apos_lancamento_retroativo(api: Api, mes: date) -> None:
    dia_1, dia_2, dia_3 = mes, mes + timedelta(days=1), mes + timedelta(days=2)
    antes = _acumulado_antes(api, mes)
    _projetar(
        api.banco,
        _evento(dia_1, "CREDITO", "50.00"),
        _evento(dia_3, "CREDITO", "20.00"),
    )
    assert api.saldo(dia_3).json()["saldo_acumulado"] == _soma(antes, "70.00")

    # Registrado depois, com data anterior (002 D-005): muda o dia 2 e o
    # acumulado do dia 3, sem tocar no saldo do dia 3.
    _projetar(api.banco, _evento(dia_2, "DEBITO", "15.00"))

    dia_3_depois = api.saldo(dia_3).json()
    assert dia_3_depois["saldo_dia"] == "20.00"
    assert dia_3_depois["saldo_acumulado"] == _soma(antes, "55.00")
    assert api.saldo(dia_2).json()["saldo_acumulado"] == _soma(antes, "35.00")
    acumulados = [
        item["saldo_acumulado"] for item in api.saldos(dia_1, dia_3).json()["itens"]
    ]
    assert acumulados == [
        _soma(antes, "50.00"),
        _soma(antes, "35.00"),
        _soma(antes, "55.00"),
    ]


def test_sinal_de_inclusao(api: Api, mes: date) -> None:
    lancamento = uuid.uuid4()
    evento = _evento(mes, "CREDITO", "3.00", lancamento_id=lancamento)
    pendente = api.get(f"/v1/consolidado/inclusoes/{lancamento}")
    _projetar(api.banco, evento)

    incluido = api.get(f"/v1/consolidado/inclusoes/{lancamento}")

    assert pendente.json() == {"incluido": False, "posicao": None}
    assert incluido.json() == {"incluido": True, "posicao": evento["posicao"]}


def test_consulta_le_somente_a_geracao_ativa(api: Api, mes: date) -> None:
    _projetar(api.banco, _evento(mes, "CREDITO", "5.00"))
    ativa = api.saldo(mes).json()
    (geracao_ativa,) = api.como_superusuario(
        "SELECT geracao_ativa_id FROM controle_projecao WHERE id = 1"
    )[0]
    # Geração candidata com outro saldo na mesma data e outro checkpoint,
    # como a reconstrução da spec 005 deixaria antes da troca.
    (candidata,) = api.como_superusuario(
        "INSERT INTO geracao (estado) VALUES ('construindo') RETURNING id"
    )[0]
    api.como_superusuario(
        "INSERT INTO checkpoint (geracao_id, posicao_continua) VALUES (%s, 999999)",
        candidata,
    )
    api.como_superusuario(
        "INSERT INTO saldo_diario"
        " (geracao_id, data, total_creditos, total_debitos, saldo_dia)"
        " VALUES (%s, %s, 777.00, 0, 777.00)",
        candidata,
        mes,
    )
    api.como_superusuario(
        "INSERT INTO marcador_inclusao (geracao_id, lancamento_id, posicao)"
        " VALUES (%s, %s, 999999)",
        candidata,
        uuid.UUID(int=99),
    )

    try:
        # Candidata existe, mas o ponteiro segue na ativa.
        assert api.saldo(mes).json() == ativa
        assert api.saldo(mes + timedelta(days=1)).status_code == 404
        inclusao = api.get(f"/v1/consolidado/inclusoes/{uuid.UUID(int=99)}").json()
        assert inclusao["incluido"] is False
        assert api.amostra("cashflow_geracao_ativa") == geracao_ativa

        # Troca do ponteiro (spec 005): a consulta seguinte lê só a nova.
        api.como_superusuario(
            "UPDATE controle_projecao SET geracao_ativa_id = %s WHERE id = 1",
            candidata,
        )
        trocada = api.saldo(mes).json()
        assert trocada["total_creditos"] == "777.00"
        assert trocada["saldo_acumulado"] == "777.00"
        assert trocada["posicao_checkpoint"] == 999999
        intervalo = api.saldos(date(2000, 1, 1), date(2100, 1, 1)).json()
        assert [item["data"] for item in intervalo["itens"]] == [mes.isoformat()]
        inclusao = api.get(f"/v1/consolidado/inclusoes/{uuid.UUID(int=99)}").json()
        assert inclusao == {"incluido": True, "posicao": 999999}
        assert api.amostra("cashflow_geracao_ativa") == candidata
    finally:
        api.como_superusuario(
            "UPDATE controle_projecao SET geracao_ativa_id = %s WHERE id = 1",
            geracao_ativa,
        )


def test_papel_da_api_nao_escreve(api: Api) -> None:
    with (
        psycopg.connect(api.banco.conninfo("consolidado_api")) as conexao,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conexao.execute(
            "UPDATE saldo_diario SET total_creditos = 0 WHERE geracao_id = 0"
        )


def test_token_do_cliente_de_lancamentos_e_recusado(api: Api, mes: date) -> None:
    antes = api.amostra("cashflow_jwt_falhas_total", {"motivo": "audiencia"})

    resposta = api.get(
        f"/v1/consolidado/saldos/{mes.isoformat()}",
        token=_token("cliente-lancamentos"),
    )

    # Sem `consolidado:consultar`, o token não tem a audiência desta API.
    assert resposta.status_code == 401
    assert resposta.json()["erro"] == "nao_autenticado"
    depois = api.amostra("cashflow_jwt_falhas_total", {"motivo": "audiencia"})
    assert depois == antes + 1


def test_token_adulterado_e_401(api: Api, mes: date) -> None:
    cabecalho, carga, assinatura = api.token.split(".")
    trocado = "A" if assinatura[10] != "A" else "B"
    adulterado = f"{cabecalho}.{carga}.{assinatura[:10]}{trocado}{assinatura[11:]}"

    resposta = api.get(f"/v1/consolidado/saldos/{mes.isoformat()}", token=adulterado)

    assert resposta.status_code == 401
    assert api.amostra("cashflow_jwt_falhas_total", {"motivo": "assinatura"}) >= 1


def test_metricas_de_requisicoes_e_pool(api: Api) -> None:
    rotulos = {"componente": "consolidado-api", "status": "200"}
    assert api.amostra("cashflow_http_requisicoes_total", rotulos) >= 1
    total = api.amostra(
        "cashflow_pool_conexoes", {"componente": "consolidado-api", "estado": "total"}
    )
    assert total >= 1


def test_logs_nao_contem_token_nem_saldos(api: Api, mes: date) -> None:
    # Roda depois dos demais: inspeciona tudo o que o módulo registrou.
    _projetar(api.banco, _evento(mes, "CREDITO", VALOR_SIGILOSO))
    assert api.saldo(mes).json()["total_creditos"] == VALOR_SIGILOSO
    saida = api.saida_log.getvalue()
    linhas = [json.loads(linha) for linha in saida.splitlines()]

    assert any(linha.get("rota") == "/v1/consolidado/saldos/{data}" for linha in linhas)
    for sensivel in (api.token, "eyJ", VALOR_SIGILOSO):
        assert sensivel not in saida


def test_ready_falso_e_consulta_503_com_banco_parado(token_consultas: str) -> None:
    with (
        consolidado_descartavel("api-consolidado-banco-parado") as banco,
        _api_sobre(banco, token_consultas) as api,
    ):
        assert api.cliente.get("/health/ready").status_code == 200

        parado = docker("stop", banco.id_conteiner, timeout=60)
        assert parado.returncode == 0, parado.stderr

        pronto = api.cliente.get("/health/ready")
        assert pronto.status_code == 503
        assert pronto.json()["verificacoes"] == {"banco": False, "jwks": True}
        consulta = api.saldo(date(2026, 11, 6))
        assert consulta.status_code == 503
        assert consulta.json()["erro"] == "indisponivel"
        assert api.cliente.get("/health/live").status_code == 200
