"""Contrato HTTP, mapeamento de erros, métricas e logs da API (002-T5).

Sem infraestrutura: a unidade de trabalho e o validador são substituídos por
dublês nas portas. O mesmo contrato roda contra PostgreSQL e Keycloak reais
em tests/integracao/test_api_http_livro.py.
"""

import io
import json
import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from lancamentos.adaptadores.http import (
    CABECALHO_CORRELACAO,
    REDIGIDO,
    FormatadorJson,
    criar_app,
    redigir_texto,
)
from lancamentos.adaptadores.seguranca import ESCOPO_REGISTRAR
from lancamentos.aplicacao import (
    STATUS_CRIADO,
    ConflitoIdempotencia,
    RegistrarLancamento,
    RegistrarLancamentoCmd,
    RespostaRegistro,
)
from observabilidade import Metricas
from seguranca import (
    EscopoInsuficiente,
    JwksIndisponivel,
    MotivoFalhaJwt,
    Principal,
    TokenInvalido,
)

# Token com a forma de um JWT, para conferir que nem ele nem pedaço dele
# chegam ao log.
TOKEN_REGISTRO = "eyJhbGciOiJSUzI1NiJ9.eyJzY29wZSI6InJlZ2lzdHJhciJ9.YXNzaW5hdHVyYQ"
TOKEN_SEM_ESCOPO = "eyJhbGciOiJSUzI1NiJ9.eyJzY29wZSI6Im91dHJvIn0.YXNzaW5hdHVyYQ"
TOKEN_JWKS_FORA = "eyJhbGciOiJSUzI1NiJ9.eyJqd2tzIjoiZm9yYSJ9.YXNzaW5hdHVyYQ"
CLIENTE = "cliente-lancamentos"

# Marcadores fáceis de achar em qualquer saída de log.
VALOR = "4321.98"
DESCRICAO = "descricao-sigilosa-xyz"


class ValidadorFalso:
    def validar(self, token: str, escopo_exigido: str) -> Principal:
        if not token:
            raise TokenInvalido(MotivoFalhaJwt.AUSENTE)
        if token == TOKEN_JWKS_FORA:
            raise JwksIndisponivel()
        if token == TOKEN_SEM_ESCOPO:
            raise EscopoInsuficiente()
        if token != TOKEN_REGISTRO:
            raise TokenInvalido(MotivoFalhaJwt.ASSINATURA)
        assert escopo_exigido == ESCOPO_REGISTRAR
        return Principal(CLIENTE, frozenset({ESCOPO_REGISTRAR}))


@dataclass
class UnidadeFalsa:
    """Idempotência em memória com a mesma regra da unidade real (TD-004)."""

    falha: Exception | None = None
    respostas: dict[tuple[str, str], tuple[str, dict[str, object]]] = field(
        default_factory=dict
    )

    async def registrar(
        self, cmd: RegistrarLancamentoCmd, chave: str, cliente: str
    ) -> RespostaRegistro:
        if self.falha is not None:
            raise self.falha
        gravada = self.respostas.get((cliente, chave))
        if gravada is not None:
            hash_gravado, corpo = gravada
            if hash_gravado != cmd.hash_requisicao:
                raise ConflitoIdempotencia()
            return RespostaRegistro(STATUS_CRIADO, corpo, repetida=True)
        corpo = {"id": str(cmd.lancamento.id), "posicao": len(self.respostas) + 1}
        self.respostas[(cliente, chave)] = (cmd.hash_requisicao, corpo)
        return RespostaRegistro(STATUS_CRIADO, corpo)


@dataclass
class Api:
    cliente: TestClient
    unidade: UnidadeFalsa
    metricas: Metricas
    prontidao: dict[str, bool]
    saida_log: io.StringIO

    def respostas(self, status: int) -> float:
        valor = self.metricas.registro.get_sample_value(
            "cashflow_http_requisicoes_total",
            {"componente": "lancamentos-api", "status": str(status)},
        )
        return valor or 0.0

    def logs(self) -> list[dict[str, Any]]:
        return [json.loads(linha) for linha in self.saida_log.getvalue().splitlines()]


@pytest.fixture
def api() -> Iterator[Api]:
    unidade = UnidadeFalsa()
    metricas = Metricas()
    prontidao = {"banco": True, "jwks": True}

    async def verificar() -> dict[str, bool]:
        return dict(prontidao)

    app = criar_app(RegistrarLancamento(unidade), ValidadorFalso(), verificar, metricas)
    saida = io.StringIO()
    manipulador = logging.StreamHandler(saida)
    manipulador.setFormatter(FormatadorJson())
    raiz = logging.getLogger()
    raiz.addHandler(manipulador)
    nivel = raiz.level
    raiz.setLevel(logging.INFO)
    try:
        with TestClient(app, raise_server_exceptions=False) as cliente:
            yield Api(cliente, unidade, metricas, prontidao, saida)
    finally:
        raiz.removeHandler(manipulador)
        raiz.setLevel(nivel)


def _corpo(**campos: object) -> dict[str, object]:
    corpo: dict[str, object] = {
        "tipo": "CREDITO",
        "valor": VALOR,
        "data_lancamento": "2026-11-06",
        "descricao": DESCRICAO,
    }
    corpo.update(campos)
    return {nome: valor for nome, valor in corpo.items() if valor is not None}


def _post(
    api: Api,
    corpo: object = None,
    token: str | None = TOKEN_REGISTRO,
    chave: str | None = "chave-1",
    conteudo: bytes | None = None,
    cabecalhos: dict[str, str] | None = None,
) -> Any:
    extras = dict(cabecalhos or {})
    if token is not None:
        extras["Authorization"] = f"Bearer {token}"
    if chave is not None:
        extras["Idempotency-Key"] = chave
    if conteudo is None:
        conteudo = json.dumps(_corpo() if corpo is None else corpo).encode()
    extras.setdefault("Content-Type", "application/json")
    return api.cliente.post("/v1/lancamentos", content=conteudo, headers=extras)


def test_registro_valido_devolve_201_com_id_e_posicao(api: Api) -> None:
    resposta = _post(api)

    assert resposta.status_code == 201
    corpo = resposta.json()
    assert set(corpo) == {"id", "posicao"}
    assert corpo["posicao"] == 1


def test_valor_como_numero_json_e_lido_sem_float(api: Api) -> None:
    # 10.30 em float não é exatamente 10.30; lido como Decimal, é.
    conteudo = b'{"tipo": "DEBITO", "valor": 10.30, "data_lancamento": "2026-11-06"}'
    resposta = _post(api, conteudo=conteudo)

    assert resposta.status_code == 201


def test_repeticao_identica_devolve_a_resposta_original(api: Api) -> None:
    primeira = _post(api)
    segunda = _post(api)

    assert segunda.status_code == primeira.status_code == 201
    assert segunda.json() == primeira.json()
    assert len(api.unidade.respostas) == 1


def test_mesma_chave_com_conteudo_diferente_e_409(api: Api) -> None:
    _post(api)
    resposta = _post(api, _corpo(valor="1.00"))

    assert resposta.status_code == 409
    assert resposta.json()["erro"] == "conflito_idempotencia"


@pytest.mark.parametrize(
    ("campos", "campo"),
    [
        ({"tipo": "ESTORNO"}, "tipo"),
        ({"valor": "0"}, "valor"),
        ({"valor": "-1.00"}, "valor"),
        ({"valor": "1.001"}, "valor"),
        ({"valor": True}, "valor"),
        ({"data_lancamento": "06/11/2026"}, "data_lancamento"),
        ({"descricao": 12}, "descricao"),
        ({"categoria": "vendas"}, "categoria"),
    ],
)
def test_regra_de_dominio_violada_e_422_com_campo_e_motivo(
    api: Api, campos: dict[str, object], campo: str
) -> None:
    resposta = _post(api, {**_corpo(), **campos})

    assert resposta.status_code == 422
    corpo = resposta.json()
    assert corpo["erro"] == "validacao"
    assert corpo["campo"] == campo
    assert corpo["motivo"]
    assert api.unidade.respostas == {}


@pytest.mark.parametrize("ausente", ["tipo", "valor", "data_lancamento"])
def test_campo_obrigatorio_ausente_e_422(api: Api, ausente: str) -> None:
    corpo = _corpo()
    del corpo[ausente]

    resposta = _post(api, corpo)

    assert resposta.status_code == 422
    assert resposta.json()["campo"] == ausente


def test_descricao_e_opcional(api: Api) -> None:
    assert _post(api, _corpo(descricao=None)).status_code == 201


@pytest.mark.parametrize("conteudo", [b"{", b"[]", b'"texto"', b'{"valor": NaN}', b""])
def test_corpo_que_nao_e_objeto_json_e_400(api: Api, conteudo: bytes) -> None:
    resposta = _post(api, conteudo=conteudo)

    assert resposta.status_code == 400
    assert resposta.json()["erro"] == "requisicao_invalida"


@pytest.mark.parametrize("chave", [None, "", "   "])
def test_sem_chave_de_idempotencia_e_400(api: Api, chave: str | None) -> None:
    resposta = _post(api, chave=chave)

    assert resposta.status_code == 400
    assert "Idempotency-Key" in resposta.json()["mensagem"]


@pytest.mark.parametrize("token", [None, "", "outro-token"])
def test_sem_token_valido_e_401(api: Api, token: str | None) -> None:
    resposta = _post(api, token=token)

    assert resposta.status_code == 401
    assert resposta.json()["erro"] == "nao_autenticado"
    assert resposta.headers["WWW-Authenticate"].startswith("Bearer")


def test_autenticacao_vem_antes_do_corpo(api: Api) -> None:
    resposta = _post(api, token=None, conteudo=b"{")

    assert resposta.status_code == 401


def test_esquema_diferente_de_bearer_e_401(api: Api) -> None:
    resposta = api.cliente.post(
        "/v1/lancamentos",
        json=_corpo(),
        headers={"Authorization": f"Basic {TOKEN_REGISTRO}", "Idempotency-Key": "k"},
    )

    assert resposta.status_code == 401


def test_token_sem_escopo_e_403(api: Api) -> None:
    resposta = _post(api, token=TOKEN_SEM_ESCOPO)

    assert resposta.status_code == 403
    assert resposta.json()["erro"] == "sem_escopo"


def test_jwks_indisponivel_e_503(api: Api) -> None:
    resposta = _post(api, token=TOKEN_JWKS_FORA)

    assert resposta.status_code == 503
    assert resposta.json()["erro"] == "indisponivel"


def test_banco_indisponivel_e_503(api: Api) -> None:
    api.unidade.falha = psycopg.OperationalError("conexão recusada")

    resposta = _post(api)

    assert resposta.status_code == 503
    assert resposta.json()["erro"] == "indisponivel"


def test_erro_inesperado_e_500_sem_detalhe(api: Api) -> None:
    api.unidade.falha = RuntimeError(f"falha com {VALOR} e {DESCRICAO}")

    resposta = _post(api)

    assert resposta.status_code == 500
    assert VALOR not in resposta.text
    assert api.respostas(500) == 1


def test_live_responde_200(api: Api) -> None:
    resposta = api.cliente.get("/health/live")

    assert resposta.status_code == 200


def test_ready_responde_200_com_banco_e_jwks(api: Api) -> None:
    resposta = api.cliente.get("/health/ready")

    assert resposta.status_code == 200
    assert resposta.json()["verificacoes"] == {"banco": True, "jwks": True}


@pytest.mark.parametrize("dependencia", ["banco", "jwks"])
def test_ready_falso_com_dependencia_fora(api: Api, dependencia: str) -> None:
    api.prontidao[dependencia] = False

    resposta = api.cliente.get("/health/ready")

    assert resposta.status_code == 503
    assert resposta.json()["verificacoes"][dependencia] is False
    # Live não depende de banco nem de JWKS.
    assert api.cliente.get("/health/live").status_code == 200


def test_cada_resposta_conta_uma_vez_por_status(api: Api) -> None:
    _post(api)
    _post(api)
    _post(api, token=None)
    _post(api, token=TOKEN_SEM_ESCOPO)
    _post(api, _corpo(tipo="X"), chave="chave-2")
    api.cliente.get("/rota-inexistente")

    assert api.respostas(201) == 2
    assert api.respostas(401) == 1
    assert api.respostas(403) == 1
    assert api.respostas(422) == 1
    assert api.respostas(404) == 1


def test_correlation_id_do_cliente_e_devolvido_e_registrado(api: Api) -> None:
    resposta = _post(api, cabecalhos={CABECALHO_CORRELACAO: "pedido-123"})

    assert resposta.headers[CABECALHO_CORRELACAO] == "pedido-123"
    (linha,) = [r for r in api.logs() if r["mensagem"] == "requisição atendida"]
    assert linha["correlation_id"] == "pedido-123"
    assert linha["lancamento_id"] == resposta.json()["id"]
    assert linha["posicao"] == resposta.json()["posicao"]
    assert linha["resultado"] == "registrado"
    assert linha["rota"] == "/v1/lancamentos"
    assert linha["status"] == 201


def test_correlation_id_fora_do_formato_e_substituido(api: Api) -> None:
    resposta = _post(api, cabecalhos={CABECALHO_CORRELACAO: f"x {DESCRICAO} y"})

    gerado = resposta.headers[CABECALHO_CORRELACAO]
    assert DESCRICAO not in gerado
    assert len(gerado) == 36


def test_logs_nao_contem_token_valor_nem_descricao(api: Api) -> None:
    # Todos os caminhos que leem token ou corpo, inclusive erro inesperado
    # com o conteúdo na mensagem da exceção.
    _post(api)
    _post(api)
    _post(api, _corpo(valor="1.00"))
    _post(api, token=TOKEN_SEM_ESCOPO)
    _post(api, token="eyJmYWxzbyJ9.eyJmYWxzbyJ9.ZmFsc28")
    _post(api, _corpo(tipo="X"), chave="chave-2")
    _post(api, conteudo=f'{{"valor": "{VALOR}", "descricao": "{DESCRICAO}"'.encode())
    api.unidade.falha = RuntimeError(f"Key (valor)=({VALOR}) {DESCRICAO}")
    _post(api, chave="chave-3")

    saida = api.saida_log.getvalue()
    assert saida
    for sensivel in (TOKEN_REGISTRO, TOKEN_SEM_ESCOPO, "eyJ", VALOR, DESCRICAO):
        assert sensivel not in saida
    assert "chave-1" not in saida
    assert any(r.get("erro_tipo") == "RuntimeError" for r in api.logs())


def test_formatador_redige_campos_e_padroes_sensiveis() -> None:
    registro = logging.LogRecord(
        "teste", logging.INFO, __file__, 1, "Bearer abc.def senha=s3gr3d0", None, None
    )
    registro.valor = VALOR
    registro.descricao = DESCRICAO
    registro.authorization = TOKEN_REGISTRO
    registro.detalhe = f"token {TOKEN_REGISTRO} no texto"
    registro.posicao = 7

    linha = json.loads(FormatadorJson().format(registro))

    assert linha["valor"] == linha["descricao"] == linha["authorization"] == REDIGIDO
    assert TOKEN_REGISTRO not in linha["detalhe"]
    assert "abc.def" not in linha["mensagem"]
    assert "s3gr3d0" not in linha["mensagem"]
    assert linha["posicao"] == 7


def test_formatador_registra_so_o_tipo_da_excecao() -> None:
    try:
        raise ValueError(f"DETAIL: Key (valor)=({VALOR})")
    except ValueError:
        registro = logging.LogRecord(
            "teste", logging.ERROR, __file__, 1, "falhou", None, None
        )
        registro.exc_info = sys.exc_info()

    linha = json.loads(FormatadorJson().format(registro))

    assert linha["erro_tipo"] == "ValueError"
    assert VALOR not in json.dumps(linha)


def test_redigir_texto_remove_jwt() -> None:
    assert redigir_texto(f"recebido {TOKEN_REGISTRO}") == f"recebido {REDIGIDO}"
