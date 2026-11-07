"""Composição da API do consolidado a partir do ambiente (004-T5).

Liga as portas aos adaptadores reais: pool psycopg 3 assíncrono do papel
consolidado_api, somente leitura (design 004 §5, §6), validador JWT com
cache de JWKS e métricas expostas em porta interna (007 design §4). O pool
abre sem esperar o banco: a API sobe com o banco fora e responde `ready`
falso até conectar (INFRA 8.5).

Variáveis (ver infra/compose.yaml):

- `PG_CONSOLIDADO_SENHA_API` (obrigatória), `PG_CONSOLIDADO_HOST`,
  `PG_CONSOLIDADO_PORTA`, `PG_CONSOLIDADO_BANCO`, `PG_CONSOLIDADO_USUARIO_API`;
- `POOL_MIN`, `POOL_MAX`, `POOL_TIMEOUT_SEGUNDOS`;
- `JWT_*`, lidas por `ConfiguracaoJwt.de_ambiente`;
- `PRONTIDAO_TIMEOUT_SEGUNDOS`: espera máxima de cada verificação do ready;
- `SALDOS_LIMITE_PADRAO` e `SALDOS_LIMITE_MAXIMO`: dias por página do
  intervalo quando `limite` é omitido e maior `limite` aceito (TD-008);
- `FASE_INTERVALO_SEGUNDOS`: intervalo de leitura da fase da reconstrução
  para `cashflow_reconstrucao_fase` (005-T3; ver `fase`).
"""

import asyncio
import contextlib
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

from consolidado.adaptadores.http.app import COMPONENTE, Prontidao, criar_app
from consolidado.adaptadores.http.fase import CONSULTA_FASE, LerFase, vigiar_fase
from consolidado.adaptadores.postgres import (
    FabricaConexao,
    LeituraConsolidadoPostgres,
)
from consolidado.adaptadores.seguranca import criar_validador_consolidado
from consolidado.aplicacao import (
    LIMITE_MAXIMO,
    LIMITE_PADRAO,
    ConsultarInclusao,
    ConsultarSaldo,
    ListarSaldos,
)

Pool = AsyncConnectionPool[AsyncConnection[Any]]


@dataclass(frozen=True)
class ConfiguracaoConsolidado:
    conninfo: str
    pool_min: int = 1
    pool_max: int = 10
    pool_timeout_segundos: float = 5.0

    @classmethod
    def de_ambiente(cls, ambiente: Mapping[str, str]) -> "ConfiguracaoConsolidado":
        senha = ambiente.get("PG_CONSOLIDADO_SENHA_API")
        if not senha:
            raise ValueError("defina PG_CONSOLIDADO_SENHA_API (ver .env.example)")
        conninfo = make_conninfo(
            host=ambiente.get("PG_CONSOLIDADO_HOST", "pg-consolidado"),
            port=int(ambiente.get("PG_CONSOLIDADO_PORTA", "5432")),
            dbname=ambiente.get("PG_CONSOLIDADO_BANCO", "consolidado"),
            user=ambiente.get("PG_CONSOLIDADO_USUARIO_API", "consolidado_api"),
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


def criar_pool(configuracao: ConfiguracaoConsolidado) -> Pool:
    # autocommit: cada consulta é um comando só, sem transação aberta entre
    # requisições. `check` descarta a conexão quebrada na retirada, para o
    # pool se recuperar sozinho depois de queda do banco.
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
    """Mesmos estados da API de lançamentos, com `componente=consolidado-api`."""
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


def leitor_de_fase(pool: Pool, timeout_segundos: float) -> LerFase:
    """Fase da linha mais recente de `fase_reconstrucao` (migração 0003)."""

    async def ler() -> str | None:
        async def consultar() -> str | None:
            async with pool.connection(timeout=timeout_segundos) as conexao:
                cursor = await conexao.execute(CONSULTA_FASE)
                linha = await cursor.fetchone()
            return None if linha is None else str(linha[0])

        return await asyncio.wait_for(consultar(), timeout_segundos)

    return ler


def prontidao_de(
    pool: Pool, validador: ValidadorJwt, timeout_segundos: float
) -> Prontidao:
    """Ready = geração ativa legível como consolidado_api e JWKS com chave
    válida (design 004 §12; INFRA 8.5)."""

    async def banco() -> bool:
        async def consultar() -> bool:
            async with pool.connection(timeout=timeout_segundos) as conexao:
                # Tabela e linha da migração 0001: ready só com o esquema
                # aplicado e a geração ativa apontada.
                cursor = await conexao.execute(
                    "SELECT geracao_ativa_id FROM controle_projecao WHERE id = 1"
                )
                return await cursor.fetchone() is not None

        try:
            return await asyncio.wait_for(consultar(), timeout_segundos)
        except (psycopg.Error, PoolTimeout, TimeoutError, OSError):
            return False

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
    pool = criar_pool(ConfiguracaoConsolidado.de_ambiente(ambiente))
    medir_pool(pool, metricas)
    validador = criar_validador_consolidado(metricas, ambiente)
    leitura = LeituraConsolidadoPostgres(fabrica_de_conexao(pool))
    listar = ListarSaldos(
        leitura,
        limite_padrao=int(ambiente.get("SALDOS_LIMITE_PADRAO", str(LIMITE_PADRAO))),
        limite_maximo=int(ambiente.get("SALDOS_LIMITE_MAXIMO", str(LIMITE_MAXIMO))),
    )
    timeout = float(ambiente.get("PRONTIDAO_TIMEOUT_SEGUNDOS", "2"))
    fase = metricas.medidor("cashflow_reconstrucao_fase")
    intervalo_fase = float(ambiente.get("FASE_INTERVALO_SEGUNDOS", "10"))

    @asynccontextmanager
    async def ciclo_de_vida(_: FastAPI) -> AsyncIterator[None]:
        await pool.open(wait=False)
        vigia = asyncio.create_task(
            vigiar_fase(leitor_de_fase(pool, timeout), fase, intervalo_fase)
        )
        try:
            yield
        finally:
            vigia.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await vigia
            await pool.close()

    return criar_app(
        ConsultarSaldo(leitura),
        listar,
        ConsultarInclusao(leitura),
        validador,
        prontidao_de(pool, validador, timeout),
        metricas,
        lifespan=ciclo_de_vida,
    )
