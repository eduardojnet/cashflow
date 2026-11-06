"""Rotas HTTP da API do consolidado e mapeamento de erros (004-T5).

Contrato (design 004 §4), todas as consultas com o escopo
`consolidado:consultar` (FR-008) e sobre a geração ativa (FR-007):

- `GET /v1/consolidado/saldos/{data}`: saldo da data (FR-004);
- `GET /v1/consolidado/saldos?data_inicio=&data_fim=&limite=&cursor=`:
  saldos do intervalo, só dias com lançamentos (FR-005; TD-008);
- `GET /v1/consolidado/inclusoes/{lancamento_id}`: sinal de inclusão
  (FR-006);
- `GET /health/live` e `GET /health/ready`.

Erros (design 004 §4, §6):

| Situação | Status | `erro` |
|---|---|---|
| Data, UUID, limite ou cursor inválido; `data_inicio` > `data_fim` | 422 | `validacao` |
| Token ausente ou inválido | 401 | `nao_autenticado` |
| Token sem `consolidado:consultar` | 403 | `sem_escopo` |
| Data sem lançamentos consolidados (D-006), com `posicao_checkpoint` | 404 | `nao_encontrado` |
| Banco do consolidado ou JWKS indisponível | 503 | `indisponivel` |

A autenticação vem antes dos parâmetros: sem token válido, nada da
requisição é lido.

Cada resposta incrementa `cashflow_http_requisicoes_total{componente,status}`
uma vez, inclusive as de erro geradas antes da rota (007 TD-005), e gera
uma linha de log com `correlation_id`, rota, status e resultado. Saldos e
token nunca entram no log (design 004 §6; INFRA 8.2). Cada leitura do banco
atualiza `cashflow_geracao_ativa` com a geração que respondeu (007 design
§4; INFRA 8.3).
"""

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
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

from consolidado.adaptadores.logs import correlation_id
from consolidado.adaptadores.seguranca import ESCOPO_CONSULTAR
from consolidado.aplicacao import (
    ConsultaInvalida,
    ConsultarInclusao,
    ConsultarSaldo,
    ListarSaldos,
)

COMPONENTE = "consolidado-api"
CABECALHO_CORRELACAO = "X-Correlation-ID"

# Identificador de correlação aceito do cliente; fora disso, um novo é
# gerado, para que texto arbitrário do cliente não vá parar no log.
_FORMATO_CORRELACAO = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

Prontidao = Callable[[], Awaitable[dict[str, bool]]]

log = logging.getLogger("consolidado.api")


def _erro(
    request: Request, status: int, erro: str, mensagem: str, **extras: object
) -> JSONResponse:
    request.state.resultado = erro
    return JSONResponse({"erro": erro, "mensagem": mensagem, **extras}, status)


def _token_bearer(request: Request) -> str:
    # Ausente ou fora do esquema Bearer vira token vazio, que o validador
    # recusa como `ausente` e conta na métrica.
    esquema, _, token = request.headers.get("Authorization", "").partition(" ")
    return token.strip() if esquema.lower() == "bearer" else ""


def criar_app(
    consultar_saldo: ConsultarSaldo,
    listar_saldos: ListarSaldos,
    consultar_inclusao: ConsultarInclusao,
    validador: ValidadorToken,
    prontidao: Prontidao,
    metricas: Metricas,
    **opcoes_fastapi: Any,
) -> FastAPI:
    """Monta a API sobre as portas; a composição real fica em `principal`."""
    app = FastAPI(
        title="CashFlow - Consolidado",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        **opcoes_fastapi,
    )
    requisicoes = metricas.contador("cashflow_http_requisicoes_total")
    geracao_ativa = metricas.medidor("cashflow_geracao_ativa")

    # Dependência síncrona: o FastAPI a executa no threadpool, e a renovação
    # do JWKS (urllib) não bloqueia o laço de eventos.
    def autenticar(request: Request) -> Principal:
        return validador.validar(_token_bearer(request), ESCOPO_CONSULTAR)

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

    @app.exception_handler(ConsultaInvalida)
    async def _consulta_invalida(request: Request, erro: Exception) -> Response:
        assert isinstance(erro, ConsultaInvalida)
        return _erro(
            request,
            422,
            "validacao",
            "consulta inválida",
            campo=erro.campo,
            motivo=erro.motivo,
        )

    async def _indisponivel(request: Request, erro: Exception) -> Response:
        log.warning(
            "dependência indisponível", extra={"erro_tipo": type(erro).__name__}
        )
        return _erro(
            request, 503, "indisponivel", "serviço temporariamente indisponível"
        )

    for tipo in (JwksIndisponivel, psycopg.OperationalError, psycopg_pool.PoolTimeout):
        app.add_exception_handler(tipo, _indisponivel)

    Autenticado = Annotated[Principal, Depends(autenticar)]

    @app.get("/v1/consolidado/saldos/{data}")
    async def saldo_da_data(request: Request, data: str, _: Autenticado) -> Response:
        resposta = await consultar_saldo.executar(data)
        geracao_ativa.set(resposta.geracao_id)
        if resposta.saldo is None:
            # D-006; o checkpoint diz ao cliente até onde a projeção chegou.
            return _erro(
                request,
                404,
                "nao_encontrado",
                "data sem lançamentos consolidados",
                posicao_checkpoint=resposta.posicao_checkpoint,
            )
        request.state.resultado = "encontrado"
        return JSONResponse(resposta.como_dict())

    @app.get("/v1/consolidado/saldos")
    async def saldos_do_intervalo(request: Request, _: Autenticado) -> Response:
        # Parâmetros lidos à mão, e não pela validação do FastAPI, para o
        # erro sair no mesmo formato das demais rotas.
        parametros = request.query_params
        pagina = await listar_saldos.executar(
            parametros.get("data_inicio"),
            parametros.get("data_fim"),
            parametros.get("limite"),
            parametros.get("cursor"),
        )
        geracao_ativa.set(pagina.geracao_id)
        request.state.resultado = "listado"
        request.state.campos_log = {"itens": len(pagina.itens)}
        return JSONResponse(pagina.como_dict())

    @app.get("/v1/consolidado/inclusoes/{lancamento_id}")
    async def inclusao(
        request: Request, lancamento_id: str, _: Autenticado
    ) -> Response:
        resposta = await consultar_inclusao.executar(lancamento_id)
        geracao_ativa.set(resposta.geracao_id)
        request.state.resultado = "incluido" if resposta.incluido else "nao_incluido"
        return JSONResponse(resposta.como_dict())

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
