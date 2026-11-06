"""Contrato HTTP, mapeamento de erros, métricas e logs da API do consolidado
(004-T5; design 004 §4, §6, §12).

Sem infraestrutura: a leitura e o validador são substituídos por dublês nas
portas. O mesmo contrato roda contra PostgreSQL e Keycloak reais em
tests/integracao/test_api_consolidado.py.
"""

import io
import json
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from consolidado.adaptadores.http import CABECALHO_CORRELACAO, criar_app
from consolidado.adaptadores.logs import FormatadorJson
from consolidado.adaptadores.seguranca import ESCOPO_CONSULTAR
from consolidado.aplicacao import (
    ConsultarInclusao,
    ConsultarSaldo,
    Inclusao,
    ListarSaldos,
    SaldoConsultado,
    SaldoDaData,
    SaldosLidos,
)
from fastapi.testclient import TestClient
from observabilidade import Metricas
from seguranca import (
    EscopoInsuficiente,
    JwksIndisponivel,
    MotivoFalhaJwt,
    Principal,
    TokenInvalido,
)

# Tokens com a forma de um JWT, para conferir que nem eles nem pedaço deles
# chegam ao log.
TOKEN_CONSULTA = "eyJhbGciOiJSUzI1NiJ9.eyJzY29wZSI6ImNvbnNvbGlkYWRvIn0.YXNzaW5hdHVyYQ"
TOKEN_SEM_ESCOPO = "eyJhbGciOiJSUzI1NiJ9.eyJzY29wZSI6Im91dHJvIn0.YXNzaW5hdHVyYQ"
TOKEN_JWKS_FORA = "eyJhbGciOiJSUzI1NiJ9.eyJqd2tzIjoiZm9yYSJ9.YXNzaW5hdHVyYQ"

# Marcador fácil de achar em qualquer saída de log.
SALDO_SIGILOSO = Decimal("98765.43")
GERACAO = 7
CHECKPOINT = 42


class ValidadorFalso:
    """Só TOKEN_CONSULTA carrega `consolidado:consultar` (INFRA 5.4)."""

    def validar(self, token: str, escopo_exigido: str) -> Principal:
        if not token:
            raise TokenInvalido(MotivoFalhaJwt.AUSENTE)
        if token == TOKEN_JWKS_FORA:
            raise JwksIndisponivel()
        if token == TOKEN_SEM_ESCOPO:
            raise EscopoInsuficiente()
        if token != TOKEN_CONSULTA:
            raise TokenInvalido(MotivoFalhaJwt.ASSINATURA)
        if escopo_exigido != ESCOPO_CONSULTAR:
            raise EscopoInsuficiente()
        return Principal("cliente-consultas", frozenset({ESCOPO_CONSULTAR}))


@dataclass
class LeituraFalsa:
    """Saldos por data com o acumulado da regra de TD-007."""

    dias: dict[date, tuple[Decimal, Decimal]] = field(default_factory=dict)
    incluidos: dict[uuid.UUID, int] = field(default_factory=dict)
    falha: Exception | None = None
    chamadas: list[tuple[object, ...]] = field(default_factory=list)

    def _saldo(self, data: date) -> SaldoConsultado:
        creditos, debitos = self.dias[data]
        acumulado = sum(
            (c - d for dia, (c, d) in self.dias.items() if dia <= data),
            Decimal("0.00"),
        )
        return SaldoConsultado(data, creditos, debitos, creditos - debitos, acumulado)

    def _falhar(self) -> None:
        if self.falha is not None:
            raise self.falha

    async def saldo_da_data(self, data: date) -> SaldoDaData:
        self._falhar()
        self.chamadas.append(("data", data))
        saldo = self._saldo(data) if data in self.dias else None
        return SaldoDaData(GERACAO, CHECKPOINT, saldo)

    async def saldos(
        self, inicio: date, fim: date, apos: date | None, quantidade: int
    ) -> SaldosLidos:
        self._falhar()
        self.chamadas.append(("intervalo", inicio, fim, apos, quantidade))
        dias = sorted(
            dia
            for dia in self.dias
            if inicio <= dia <= fim and (apos is None or dia > apos)
        )[:quantidade]
        return SaldosLidos(GERACAO, CHECKPOINT, [self._saldo(dia) for dia in dias])

    async def inclusao(self, lancamento_id: uuid.UUID) -> Inclusao:
        self._falhar()
        posicao = self.incluidos.get(lancamento_id)
        return Inclusao(GERACAO, posicao is not None, posicao)


@dataclass
class Api:
    cliente: TestClient
    leitura: LeituraFalsa
    metricas: Metricas
    prontidao: dict[str, bool]
    saida_log: io.StringIO

    def respostas(self, status: int) -> float:
        valor = self.metricas.registro.get_sample_value(
            "cashflow_http_requisicoes_total",
            {"componente": "consolidado-api", "status": str(status)},
        )
        return valor or 0.0

    def logs(self) -> list[dict[str, Any]]:
        return [json.loads(linha) for linha in self.saida_log.getvalue().splitlines()]

    def get(
        self, caminho: str, token: str | None = TOKEN_CONSULTA, **params: str
    ) -> Any:
        cabecalhos = {} if token is None else {"Authorization": f"Bearer {token}"}
        # Sem `params`, a consulta já escrita em `caminho` é mantida.
        return self.cliente.get(caminho, params=params or None, headers=cabecalhos)


@contextmanager
def montar_api(leitura: LeituraFalsa | None = None, **opcoes: int) -> Iterator[Api]:
    leitura = LeituraFalsa() if leitura is None else leitura
    metricas = Metricas()
    prontidao = {"banco": True, "jwks": True}

    async def verificar() -> dict[str, bool]:
        return dict(prontidao)

    app = criar_app(
        ConsultarSaldo(leitura),
        ListarSaldos(leitura, **opcoes),
        ConsultarInclusao(leitura),
        ValidadorFalso(),
        verificar,
        metricas,
    )
    saida = io.StringIO()
    manipulador = logging.StreamHandler(saida)
    manipulador.setFormatter(FormatadorJson())
    raiz = logging.getLogger()
    raiz.addHandler(manipulador)
    nivel = raiz.level
    raiz.setLevel(logging.INFO)
    try:
        with TestClient(app, raise_server_exceptions=False) as cliente:
            yield Api(cliente, leitura, metricas, prontidao, saida)
    finally:
        raiz.removeHandler(manipulador)
        raiz.setLevel(nivel)


def _leitura() -> LeituraFalsa:
    return LeituraFalsa(
        dias={
            date(2026, 11, 1): (Decimal("100.00"), Decimal("30.00")),
            date(2026, 11, 3): (Decimal("0.00"), Decimal("50.25")),
            date(2026, 11, 5): (Decimal("10.00"), Decimal("0.00")),
        }
    )


@pytest.fixture
def api() -> Iterator[Api]:
    with montar_api(_leitura(), limite_padrao=2, limite_maximo=3) as api:
        yield api


# --- saldo por data (FR-004) ---------------------------------------------


def test_saldo_da_data_tem_totais_saldo_acumulado_e_checkpoint(api: Api) -> None:
    resposta = api.get("/v1/consolidado/saldos/2026-11-03")

    assert resposta.status_code == 200
    assert resposta.json() == {
        "data": "2026-11-03",
        "total_creditos": "0.00",
        "total_debitos": "50.25",
        "saldo_dia": "-50.25",
        "saldo_acumulado": "19.75",
        "posicao_checkpoint": CHECKPOINT,
    }


def test_data_sem_lancamentos_e_404_com_checkpoint(api: Api) -> None:
    resposta = api.get("/v1/consolidado/saldos/2026-11-02")

    assert resposta.status_code == 404
    corpo = resposta.json()
    assert corpo["erro"] == "nao_encontrado"
    assert corpo["posicao_checkpoint"] == CHECKPOINT


@pytest.mark.parametrize("data", ["2026-13-01", "06-11-2026", "20261106", "hoje"])
def test_data_invalida_e_422(api: Api, data: str) -> None:
    resposta = api.get(f"/v1/consolidado/saldos/{data}")

    assert resposta.status_code == 422
    assert resposta.json()["campo"] == "data"
    assert api.leitura.chamadas == []


# --- intervalo (FR-005; D-002, D-007; TD-008) -----------------------------


def test_intervalo_omite_dias_vazios_e_pagina_por_cursor(api: Api) -> None:
    primeira = api.get(
        "/v1/consolidado/saldos", data_inicio="2026-11-01", data_fim="2026-11-30"
    ).json()

    assert [item["data"] for item in primeira["itens"]] == ["2026-11-01", "2026-11-03"]
    assert [item["saldo_acumulado"] for item in primeira["itens"]] == ["70.00", "19.75"]
    assert primeira["posicao_checkpoint"] == CHECKPOINT
    assert primeira["proximo_cursor"]

    segunda = api.get(
        "/v1/consolidado/saldos",
        data_inicio="2026-11-01",
        data_fim="2026-11-30",
        cursor=primeira["proximo_cursor"],
    ).json()

    assert [item["data"] for item in segunda["itens"]] == ["2026-11-05"]
    assert segunda["itens"][0]["saldo_acumulado"] == "29.75"
    assert segunda["proximo_cursor"] is None


def test_item_do_intervalo_tem_os_campos_do_saldo_por_data(api: Api) -> None:
    (item,) = api.get(
        "/v1/consolidado/saldos", data_inicio="2026-11-03", data_fim="2026-11-03"
    ).json()["itens"]

    por_data = api.get("/v1/consolidado/saldos/2026-11-03").json()
    del por_data["posicao_checkpoint"]
    assert item == por_data


def test_intervalo_sem_lancamentos_e_200_com_lista_vazia(api: Api) -> None:
    resposta = api.get(
        "/v1/consolidado/saldos", data_inicio="2026-12-01", data_fim="2026-12-31"
    )

    assert resposta.status_code == 200
    assert resposta.json() == {
        "itens": [],
        "posicao_checkpoint": CHECKPOINT,
        "proximo_cursor": None,
    }


def test_intervalo_nao_tem_tamanho_maximo(api: Api) -> None:
    resposta = api.get(
        "/v1/consolidado/saldos", data_inicio="0001-01-01", data_fim="9999-12-31"
    )

    assert resposta.status_code == 200
    assert len(resposta.json()["itens"]) == 2


def test_pede_um_item_a_mais_para_saber_se_ha_proxima_pagina(api: Api) -> None:
    api.get(
        "/v1/consolidado/saldos",
        data_inicio="2026-11-01",
        data_fim="2026-11-30",
        limite="3",
    )

    assert api.leitura.chamadas[-1][-1] == 4


@pytest.mark.parametrize(
    ("parametros", "campo"),
    [
        ({"data_fim": "2026-11-30"}, "data_inicio"),
        ({"data_inicio": "2026-11-01"}, "data_fim"),
        ({"data_inicio": "2026-11-30", "data_fim": "2026-11-01"}, "data_inicio"),
        ({"data_inicio": "2026-11-01", "data_fim": "2026-11-31"}, "data_fim"),
        (
            {"data_inicio": "2026-11-01", "data_fim": "2026-11-30", "limite": "4"},
            "limite",
        ),
        (
            {"data_inicio": "2026-11-01", "data_fim": "2026-11-30", "limite": "0"},
            "limite",
        ),
        (
            {"data_inicio": "2026-11-01", "data_fim": "2026-11-30", "limite": "x"},
            "limite",
        ),
        (
            {"data_inicio": "2026-11-01", "data_fim": "2026-11-30", "cursor": "%%%"},
            "cursor",
        ),
    ],
)
def test_parametro_de_intervalo_invalido_e_422(
    api: Api, parametros: dict[str, str], campo: str
) -> None:
    resposta = api.get("/v1/consolidado/saldos", **parametros)

    assert resposta.status_code == 422
    corpo = resposta.json()
    assert (corpo["erro"], corpo["campo"]) == ("validacao", campo)


# --- inclusão (FR-006) ----------------------------------------------------


def test_lancamento_incluido_e_nao_incluido() -> None:
    incluido = uuid.uuid4()
    leitura = LeituraFalsa(incluidos={incluido: 9})
    with montar_api(leitura) as api:
        sim = api.get(f"/v1/consolidado/inclusoes/{incluido}")
        nao = api.get(f"/v1/consolidado/inclusoes/{uuid.uuid4()}")

    assert (sim.status_code, sim.json()) == (200, {"incluido": True, "posicao": 9})
    assert (nao.status_code, nao.json()) == (200, {"incluido": False, "posicao": None})


def test_lancamento_id_invalido_e_422(api: Api) -> None:
    resposta = api.get("/v1/consolidado/inclusoes/nao-e-uuid")

    assert resposta.status_code == 422
    assert resposta.json()["campo"] == "lancamento_id"


# --- autenticação e autorização (FR-008) ----------------------------------

ROTAS = [
    "/v1/consolidado/saldos/2026-11-01",
    "/v1/consolidado/saldos?data_inicio=2026-11-01&data_fim=2026-11-30",
    f"/v1/consolidado/inclusoes/{uuid.UUID(int=1)}",
]


@pytest.mark.parametrize("caminho", ROTAS)
def test_token_sem_escopo_e_403(api: Api, caminho: str) -> None:
    resposta = api.get(caminho, token=TOKEN_SEM_ESCOPO)

    assert resposta.status_code == 403
    assert resposta.json()["erro"] == "sem_escopo"
    assert "insufficient_scope" in resposta.headers["WWW-Authenticate"]
    assert api.leitura.chamadas == []


@pytest.mark.parametrize("caminho", ROTAS)
@pytest.mark.parametrize("token", [None, "outro-token"])
def test_sem_token_valido_e_401(api: Api, caminho: str, token: str | None) -> None:
    resposta = api.get(caminho, token=token)

    assert resposta.status_code == 401
    assert resposta.json()["erro"] == "nao_autenticado"
    assert api.leitura.chamadas == []


def test_autenticacao_vem_antes_dos_parametros(api: Api) -> None:
    resposta = api.get("/v1/consolidado/saldos/invalida", token=TOKEN_SEM_ESCOPO)

    assert resposta.status_code == 403


def test_jwks_indisponivel_e_503(api: Api) -> None:
    resposta = api.get("/v1/consolidado/saldos/2026-11-01", token=TOKEN_JWKS_FORA)

    assert resposta.status_code == 503
    assert resposta.json()["erro"] == "indisponivel"


@pytest.mark.parametrize("caminho", ROTAS)
def test_banco_indisponivel_e_503(caminho: str) -> None:
    leitura = LeituraFalsa(falha=psycopg.OperationalError("conexão recusada"))
    with montar_api(leitura) as api:
        resposta = api.get(caminho)

    assert resposta.status_code == 503
    assert resposta.json()["erro"] == "indisponivel"


# --- health, métricas e logs (design 004 §12; INFRA 8.2 a 8.5) -----------


def test_health_live_e_ready(api: Api) -> None:
    assert api.cliente.get("/health/live").status_code == 200
    assert api.cliente.get("/health/ready").status_code == 200

    api.prontidao["jwks"] = False
    resposta = api.cliente.get("/health/ready")

    assert resposta.status_code == 503
    assert resposta.json()["verificacoes"] == {"banco": True, "jwks": False}


def test_cada_resposta_conta_uma_vez_por_status(api: Api) -> None:
    api.get("/v1/consolidado/saldos/2026-11-01")
    api.get("/v1/consolidado/saldos/2026-11-02")
    api.get("/v1/consolidado/saldos/2026-11-01", token=None)
    api.get("/v1/consolidado/saldos/2026-11-01", token=TOKEN_SEM_ESCOPO)
    api.get("/v1/consolidado/saldos/x")

    contagens = {status: api.respostas(status) for status in (200, 401, 403, 404, 422)}
    assert contagens == {200: 1, 401: 1, 403: 1, 404: 1, 422: 1}


def test_geracao_ativa_vai_para_a_metrica(api: Api) -> None:
    api.get("/v1/consolidado/saldos/2026-11-01")

    valor = api.metricas.registro.get_sample_value("cashflow_geracao_ativa")
    assert valor == GERACAO


def test_correlation_id_do_cliente_volta_e_vai_ao_log(api: Api) -> None:
    resposta = api.cliente.get(
        "/v1/consolidado/saldos/2026-11-01",
        headers={
            "Authorization": f"Bearer {TOKEN_CONSULTA}",
            CABECALHO_CORRELACAO: "corr-123",
        },
    )

    assert resposta.headers[CABECALHO_CORRELACAO] == "corr-123"
    (linha,) = [r for r in api.logs() if r["mensagem"] == "requisição atendida"]
    assert linha["correlation_id"] == "corr-123"
    assert linha["rota"] == "/v1/consolidado/saldos/{data}"
    assert linha["status"] == 200


def test_log_nao_contem_saldos_nem_token() -> None:
    leitura = LeituraFalsa(dias={date(2026, 11, 6): (SALDO_SIGILOSO, Decimal(0))})
    with montar_api(leitura) as api:
        por_data = api.get("/v1/consolidado/saldos/2026-11-06").json()
        api.get(
            "/v1/consolidado/saldos", data_inicio="2026-11-01", data_fim="2026-11-30"
        )
        api.get("/v1/consolidado/saldos/2026-11-06", token="eyJx.eyJy.assinatura")
        saida = api.saida_log.getvalue()

    # A resposta traz o saldo; o log, nem ele nem o token.
    assert por_data["saldo_dia"] == str(SALDO_SIGILOSO)
    assert saida
    for sensivel in (str(SALDO_SIGILOSO), TOKEN_CONSULTA, "eyJ", "Bearer"):
        assert sensivel not in saida
