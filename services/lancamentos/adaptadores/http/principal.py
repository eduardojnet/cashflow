"""Composição da API de lançamentos a partir do ambiente (002-T5).

Liga as portas aos adaptadores reais: pool psycopg 3 assíncrono do papel
livro_api, validador JWT com cache de JWKS e métricas expostas em porta
interna (007 design §4). O pool abre sem esperar o banco: a API sobe com o
banco fora e responde `ready` falso até conectar (INFRA 8.5).

Variáveis (ver infra/compose.yaml):

- `PG_LIVRO_SENHA_API` (obrigatória), `PG_LIVRO_HOST`, `PG_LIVRO_PORTA`,
  `PG_LIVRO_BANCO`, `PG_LIVRO_USUARIO_API`;
- `POOL_MIN`, `POOL_MAX`, `POOL_TIMEOUT_SEGUNDOS`;
- `JWT_*`, lidas por `ConfiguracaoJwt.de_ambiente`;
- `PRONTIDAO_TIMEOUT_SEGUNDOS`: espera máxima de cada verificação do ready.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
from fastapi import FastAPI
from observabilidade import Metricas
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from seguranca import ValidadorJwt
from starlette.concurrency import run_in_threadpool

from lancamentos.adaptadores.http.app import COMPONENTE, Prontidao, criar_app
from lancamentos.adaptadores.postgres import (
    FabricaConexao,
    UnidadeDeTrabalhoLivroPostgres,
)
from lancamentos.adaptadores.seguranca import criar_validador_lancamentos
from lancamentos.aplicacao import RegistrarLancamento

Pool = AsyncConnectionPool[AsyncConnection[Any]]


@dataclass(frozen=True)
class ConfiguracaoLivro:
    conninfo: str
    pool_min: int = 1
    pool_max: int = 10
    pool_timeout_segundos: float = 5.0

    @classmethod
    def de_ambiente(cls, ambiente: Mapping[str, str]) -> "ConfiguracaoLivro":
        senha = ambiente.get("PG_LIVRO_SENHA_API")
        if not senha:
            raise ValueError("defina PG_LIVRO_SENHA_API (ver .env.example)")
        conninfo = make_conninfo(
            host=ambiente.get("PG_LIVRO_HOST", "pg-livro"),
            port=int(ambiente.get("PG_LIVRO_PORTA", "5432")),
            dbname=ambiente.get("PG_LIVRO_BANCO", "livro"),
            user=ambiente.get("PG_LIVRO_USUARIO_API", "livro_api"),
            password=senha,
            connect_timeout=5,
            application_name=COMPONENTE,
        )
        return cls(
            conninfo=conninfo,
            pool_min=int(ambiente.get("POOL_MIN", "1")),
            pool_max=int(ambiente.get("POOL_MAX", "10")),
            pool_timeout_segundos=float(ambiente.get("POOL_TIMEOUT_SEGUNDOS", "5")),
        )


def criar_pool(configuracao: ConfiguracaoLivro) -> Pool:
    # autocommit: cada `conexao.transaction()` da unidade de trabalho é a
    # transação inteira (002-T3). `check` descarta a conexão quebrada na
    # retirada, para o pool se recuperar sozinho depois de queda do banco.
    return AsyncConnectionPool(
        configuracao.conninfo,
        min_size=configuracao.pool_min,
        max_size=configuracao.pool_max,
        timeout=configuracao.pool_timeout_segundos,
        kwargs={"autocommit": True},
        check=AsyncConnectionPool.check_connection,
        open=False,
        name=COMPONENTE,
    )


def medir_pool(pool: Pool, metricas: Metricas) -> None:
    """`cashflow_pool_conexoes{componente,estado}` lido do pool na coleta."""
    medidor = metricas.medidor("cashflow_pool_conexoes")

    def estatistica(chave: str) -> Callable[[], float]:
        return lambda: pool.get_stats().get(chave, 0)

    def em_uso() -> float:
        estado = pool.get_stats()
        return estado.get("pool_size", 0) - estado.get("pool_available", 0)

    rotulos = {"componente": COMPONENTE}
    medidor.labels(**rotulos, estado="total").set_function(estatistica("pool_size"))
    medidor.labels(**rotulos, estado="disponivel").set_function(
        estatistica("pool_available")
    )
    medidor.labels(**rotulos, estado="em_uso").set_function(em_uso)
    medidor.labels(**rotulos, estado="aguardando").set_function(
        estatistica("requests_waiting")
    )


def fabrica_de_conexao(pool: Pool) -> FabricaConexao:
    def conexao() -> AbstractAsyncContextManager[AsyncConnection[Any]]:
        return pool.connection()

    return conexao


def prontidao_de(
    pool: Pool, validador: ValidadorJwt, timeout_segundos: float
) -> Prontidao:
    """Ready = banco do livro respondendo como livro_api e JWKS com chave
    válida (design 002 §4, §5; INFRA 8.5)."""

    async def banco() -> bool:
        async def consultar() -> None:
            async with pool.connection(timeout=timeout_segundos) as conexao:
                # Tabela da migração 0001: ready só com o esquema aplicado.
                await conexao.execute("SELECT valor FROM contador_posicao")

        try:
            await asyncio.wait_for(consultar(), timeout_segundos)
        except (psycopg.Error, PoolTimeout, TimeoutError, OSError):
            return False
        return True

    async def jwks() -> bool:
        try:
            return await asyncio.wait_for(
                run_in_threadpool(validador.cache.pronto), timeout_segundos
            )
        except TimeoutError:
            return False

    async def verificar() -> dict[str, bool]:
        resultado_banco, resultado_jwks = await asyncio.gather(banco(), jwks())
        return {"banco": resultado_banco, "jwks": resultado_jwks}

    return verificar


def criar_app_do_ambiente(
    ambiente: Mapping[str, str] | None = None,
    metricas: Metricas | None = None,
) -> FastAPI:
    ambiente = os.environ if ambiente is None else ambiente
    metricas = Metricas() if metricas is None else metricas
    pool = criar_pool(ConfiguracaoLivro.de_ambiente(ambiente))
    medir_pool(pool, metricas)
    validador = criar_validador_lancamentos(metricas, ambiente)
    registrar = RegistrarLancamento(
        UnidadeDeTrabalhoLivroPostgres(fabrica_de_conexao(pool))
    )
    timeout = float(ambiente.get("PRONTIDAO_TIMEOUT_SEGUNDOS", "2"))

    @asynccontextmanager
    async def ciclo_de_vida(_: FastAPI) -> AsyncIterator[None]:
        await pool.open(wait=False)
        try:
            yield
        finally:
            await pool.close()

    return criar_app(
        registrar,
        validador,
        prontidao_de(pool, validador, timeout),
        metricas,
        lifespan=ciclo_de_vida,
    )
