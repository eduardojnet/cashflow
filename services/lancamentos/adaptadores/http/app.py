"""Rotas HTTP da API de lançamentos e mapeamento de erros (002-T5).

Contrato (design 002 §4): `POST /v1/lancamentos` com `Idempotency-Key` e
escopo `lancamentos:registrar`; `GET /health/live` e `GET /health/ready`.
A listagem por intervalo é da 002-T7.

Erros (design 002 §4, §6):

| Situação | Status | `erro` |
|---|---|---|
| Corpo que não é objeto JSON; `Idempotency-Key` ausente | 400 | `requisicao_invalida` |
| Regra de domínio (tipo, valor, data, descrição); campo não previsto | 422 | `validacao` |
| Token ausente ou inválido | 401 | `nao_autenticado` |
| Token sem o escopo da rota | 403 | `sem_escopo` |
| Chave já usada com outro conteúdo (D-006) | 409 | `conflito_idempotencia` |
| Banco do livro ou JWKS indisponível | 503 | `indisponivel` |

A autenticação vem antes do corpo: sem token válido, nada da requisição é
lido. O valor chega como número ou texto JSON; números são lidos como
`Decimal`, nunca `float`, para o domínio conferir as duas casas (D-004).

Cada resposta incrementa `cashflow_http_requisicoes_total{componente,status}`
uma vez, inclusive as de erro geradas antes da rota (007 TD-005), e gera
uma linha de log com `correlation_id`, rota, status e resultado; no registro,
também `lancamento_id` e `posicao`. Valor, descrição e token nunca entram no
log (design 002 §12; INFRA 8.2).
"""

import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Annotated, Any

import psycopg
import psycopg_pool
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from observabilidade import Metricas
from seguranca import (
    EscopoInsuficiente,
    JwksIndisponivel,
    Principal,
    TokenInvalido,
    ValidadorToken,
)

from lancamentos.adaptadores.http.logs import correlation_id
from lancamentos.adaptadores.seguranca import ESCOPO_REGISTRAR
from lancamentos.aplicacao import (
    ChaveIdempotenciaInvalida,
    ConflitoIdempotencia,
    RegistrarLancamento,
    RegistrarLancamentoCmd,
)
from lancamentos.dominio import ErroDominio

COMPONENTE = "lancamentos-api"
CABECALHO_CORRELACAO = "X-Correlation-ID"

# Campos do corpo do registro (D-001); qualquer outro é recusado.
CAMPOS_REGISTRO = frozenset({"tipo", "valor", "data_lancamento", "descricao"})

# Identificador de correlação aceito do cliente; fora disso, um novo é
# gerado, para que texto arbitrário do cliente não vá parar no log.
_FORMATO_CORRELACAO = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

Prontidao = Callable[[], Awaitable[dict[str, bool]]]

log = logging.getLogger("lancamentos.api")


class RequisicaoInvalida(Exception):
    """Corpo ou cabeçalho fora do formato do contrato: 400."""

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo


def _erro(
    request: Request, status: int, erro: str, mensagem: str, **extras: str
) -> JSONResponse:
    request.state.resultado = erro
    return JSONResponse({"erro": erro, "mensagem": mensagem, **extras}, status)


def _rejeitar_constante(nome: str) -> None:
    raise RequisicaoInvalida(f"número JSON não suportado: {nome}")


async def _corpo_registro(request: Request) -> dict[str, Any]:
    bruto = await request.body()
    try:
        corpo = json.loads(
            bruto, parse_float=Decimal, parse_constant=_rejeitar_constante
        )
    except (ValueError, RecursionError):
        raise RequisicaoInvalida("corpo deve ser JSON válido") from None
    if not isinstance(corpo, dict):
        raise RequisicaoInvalida("corpo deve ser objeto JSON")
    nao_previstos = sorted(set(corpo) - CAMPOS_REGISTRO)
    if nao_previstos:
        # Só o primeiro nome, truncado: o nome também vem do cliente.
        raise ErroDominio(nao_previstos[0][:64], "campo não previsto")
    return corpo


def _token_bearer(request: Request) -> str:
    # Ausente ou fora do esquema Bearer vira token vazio, que o validador
    # recusa como `ausente` e conta na métrica.
    esquema, _, token = request.headers.get("Authorization", "").partition(" ")
    return token.strip() if esquema.lower() == "bearer" else ""


def criar_app(
    registrar: RegistrarLancamento,
    validador: ValidadorToken,
    prontidao: Prontidao,
    metricas: Metricas,
    **opcoes_fastapi: Any,
) -> FastAPI:
    """Monta a API sobre as portas; a composição real fica em `principal`."""
    app = FastAPI(
        title="CashFlow - Lançamentos",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        **opcoes_fastapi,
    )
    requisicoes = metricas.contador("cashflow_http_requisicoes_total")

    def exigir(escopo: str) -> Callable[[Request], Principal]:
        # Dependência síncrona: o FastAPI a executa no threadpool, e a
        # renovação do JWKS (urllib) não bloqueia o laço de eventos.
        def autenticar(request: Request) -> Principal:
            return validador.validar(_token_bearer(request), escopo)

        return autenticar

    @app.middleware("http")
    async def observar(
        request: Request, seguinte: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        recebido = request.headers.get(CABECALHO_CORRELACAO, "")
        correlacao = (
            recebido if _FORMATO_CORRELACAO.match(recebido) else str(uuid.uuid4())
        )
        marca = correlation_id.set(correlacao)
        inicio = time.perf_counter()
        try:
            try:
                resposta = await seguinte(request)
            except Exception:
                log.exception("erro não tratado na requisição")
                resposta = _erro(
                    request, 500, "erro_interno", "erro interno; ver correlation_id"
                )
            resposta.headers[CABECALHO_CORRELACAO] = correlacao
            requisicoes.labels(
                componente=COMPONENTE, status=str(resposta.status_code)
            ).inc()
            rota = request.scope.get("route")
            log.info(
                "requisição atendida",
                extra={
                    "metodo": request.method,
                    "rota": getattr(rota, "path", "nao_encontrada"),
                    "status": resposta.status_code,
                    "duracao_ms": round((time.perf_counter() - inicio) * 1000, 1),
                    "resultado": getattr(request.state, "resultado", None),
                    **getattr(request.state, "campos_log", {}),
                },
            )
            return resposta
        finally:
            correlation_id.reset(marca)

    @app.exception_handler(TokenInvalido)
    async def _nao_autenticado(request: Request, _: Exception) -> Response:
        resposta = _erro(request, 401, "nao_autenticado", "token ausente ou inválido")
        resposta.headers["WWW-Authenticate"] = 'Bearer error="invalid_token"'
        return resposta

    @app.exception_handler(EscopoInsuficiente)
    async def _sem_escopo(request: Request, _: Exception) -> Response:
        resposta = _erro(
            request, 403, "sem_escopo", "token sem o escopo exigido pela rota"
        )
        resposta.headers["WWW-Authenticate"] = 'Bearer error="insufficient_scope"'
        return resposta

    @app.exception_handler(RequisicaoInvalida)
    async def _requisicao_invalida(request: Request, erro: Exception) -> Response:
        assert isinstance(erro, RequisicaoInvalida)
        return _erro(request, 400, "requisicao_invalida", erro.motivo)

    @app.exception_handler(ChaveIdempotenciaInvalida)
    async def _sem_chave(request: Request, erro: Exception) -> Response:
        assert isinstance(erro, ChaveIdempotenciaInvalida)
        return _erro(
            request,
            400,
            "requisicao_invalida",
            f"{erro.campo}: {erro.motivo}",
        )

    @app.exception_handler(ErroDominio)
    async def _validacao(request: Request, erro: Exception) -> Response:
        assert isinstance(erro, ErroDominio)
        return _erro(
            request,
            422,
            "validacao",
            "lançamento inválido",
            campo=erro.campo,
            motivo=erro.motivo,
        )

    @app.exception_handler(ConflitoIdempotencia)
    async def _conflito(request: Request, erro: Exception) -> Response:
        return _erro(request, 409, "conflito_idempotencia", str(erro))

    async def _indisponivel(request: Request, erro: Exception) -> Response:
        log.warning(
            "dependência indisponível", extra={"erro_tipo": type(erro).__name__}
        )
        return _erro(
            request, 503, "indisponivel", "serviço temporariamente indisponível"
        )

    for tipo in (JwksIndisponivel, psycopg.OperationalError, psycopg_pool.PoolTimeout):
        app.add_exception_handler(tipo, _indisponivel)

    autenticar_registro = exigir(ESCOPO_REGISTRAR)

    @app.post("/v1/lancamentos")
    async def registrar_lancamento(
        request: Request,
        principal: Annotated[Principal, Depends(autenticar_registro)],
    ) -> Response:
        corpo = await _corpo_registro(request)
        cmd = RegistrarLancamentoCmd.de(
            corpo.get("tipo"),
            corpo.get("valor"),
            corpo.get("data_lancamento"),
            corpo.get("descricao"),
        )
        resposta = await registrar.executar(
            cmd, request.headers.get("Idempotency-Key", ""), principal.cliente_id
        )
        request.state.resultado = "repetido" if resposta.repetida else "registrado"
        request.state.campos_log = {
            "lancamento_id": resposta.corpo.get("id"),
            "posicao": resposta.corpo.get("posicao"),
        }
        return JSONResponse(resposta.corpo, resposta.status_http)

    @app.get("/health/live")
    async def vivo() -> Response:
        return JSONResponse({"status": "vivo"})

    @app.get("/health/ready")
    async def pronto(request: Request) -> Response:
        verificacoes = await prontidao()
        if all(verificacoes.values()):
            return JSONResponse({"status": "pronto", "verificacoes": verificacoes})
        request.state.resultado = "nao_pronto"
        return JSONResponse({"status": "nao_pronto", "verificacoes": verificacoes}, 503)

    return app
