"""Composição e laço do processo do relay (003-T5; design 003 §3, §12).

A cada intervalo, um ciclo de `PublicarPendentes` reserva um lote, publica e
marca ou reagenda (003-T1 a 003-T3); em seguida a outbox é medida para as
métricas. Lote cheio repete o ciclo sem esperar, para o backlog acumulado
numa queda do RabbitMQ escoar logo após o retorno.

Nenhuma falha encerra o laço: banco ou broker fora deixam os eventos
pendentes (design §8), o ready fica falso e o ciclo seguinte tenta de novo.
O publicador que não abriu na subida (RabbitMQ fora) é reaberto a cada
ciclo; depois de aberto, a conexão robusta do aio-pika reconecta sozinha.

Variáveis (ver infra/compose.yaml; DQ-002):

- `PG_LIVRO_SENHA_RELAY` (obrigatória), `PG_LIVRO_HOST`, `PG_LIVRO_PORTA`,
  `PG_LIVRO_BANCO`, `PG_LIVRO_USUARIO_RELAY`;
- `RABBITMQ_SENHA_RELAY` (obrigatória), `RABBITMQ_HOST`, `RABBITMQ_PORTA`,
  `RABBITMQ_USUARIO_RELAY`;
- `RELAY_INTERVALO_SEGUNDOS`, `RELAY_LOTE`, `RELAY_LEASE_SEGUNDOS`,
  `RELAY_TIMEOUT_CONFIRMACAO_SEGUNDOS`, `RELAY_ESPERA_BASE_SEGUNDOS`,
  `RELAY_ESPERA_TETO_SEGUNDOS`, `RELAY_DONO` (padrão: hostname e PID);
- `HEALTH_PORTA` (padrão 8080), `METRICAS_PORTA` (padrão 9464),
  `PRONTIDAO_TIMEOUT_SEGUNDOS`.
"""

import asyncio
import logging
import os
import socket
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol
from urllib.parse import quote

import psycopg
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from relay.adaptadores.http import Prontidao
from relay.adaptadores.metricas import MetricasRelay
from relay.adaptadores.postgres import EstadoOutbox, FabricaConexao
from relay.adaptadores.rabbitmq import PublicadorRabbitMQ
from relay.aplicacao import PoliticaRetentativa, ResumoCiclo

COMPONENTE = "relay"

log = logging.getLogger("relay.laco")

Pool = AsyncConnectionPool[AsyncConnection[Any]]


def _obrigatoria(ambiente: Mapping[str, str], nome: str) -> str:
    valor = ambiente.get(nome)
    if not valor:
        raise ValueError(f"defina {nome} (ver .env.example)")
    return valor


def _segundos(ambiente: Mapping[str, str], nome: str, padrao: float) -> float:
    valor = float(ambiente.get(nome, str(padrao)))
    if valor <= 0:
        raise ValueError(f"{nome} deve ser positivo")
    return valor


@dataclass(frozen=True)
class ConfiguracaoRelay:
    conninfo: str
    url_rabbitmq: str
    dono: str
    intervalo_segundos: float = 1.0
    lote: int = 100
    lease: timedelta = timedelta(seconds=30)
    timeout_confirmacao_segundos: float = 5.0
    politica: PoliticaRetentativa = field(
        default_factory=lambda: PoliticaRetentativa(
            base=timedelta(seconds=1), teto=timedelta(seconds=60)
        )
    )
    health_porta: int = 8080
    metricas_porta: int = 9464
    prontidao_timeout_segundos: float = 2.0

    @classmethod
    def de_ambiente(cls, ambiente: Mapping[str, str]) -> "ConfiguracaoRelay":
        conninfo = make_conninfo(
            host=ambiente.get("PG_LIVRO_HOST", "pg-livro"),
            port=int(ambiente.get("PG_LIVRO_PORTA", "5432")),
            dbname=ambiente.get("PG_LIVRO_BANCO", "livro"),
            user=ambiente.get("PG_LIVRO_USUARIO_RELAY", "livro_relay"),
            password=_obrigatoria(ambiente, "PG_LIVRO_SENHA_RELAY"),
            connect_timeout=5,
            application_name=COMPONENTE,
        )
        usuario = quote(ambiente.get("RABBITMQ_USUARIO_RELAY", "relay"), safe="")
        senha = quote(_obrigatoria(ambiente, "RABBITMQ_SENHA_RELAY"), safe="")
        host = ambiente.get("RABBITMQ_HOST", "rabbitmq")
        porta = int(ambiente.get("RABBITMQ_PORTA", "5672"))
        return cls(
            conninfo=conninfo,
            url_rabbitmq=f"amqp://{usuario}:{senha}@{host}:{porta}/",
            dono=ambiente.get("RELAY_DONO") or f"{socket.gethostname()}:{os.getpid()}",
            intervalo_segundos=_segundos(ambiente, "RELAY_INTERVALO_SEGUNDOS", 1),
            lote=int(ambiente.get("RELAY_LOTE", "100")),
            lease=timedelta(seconds=_segundos(ambiente, "RELAY_LEASE_SEGUNDOS", 30)),
            timeout_confirmacao_segundos=_segundos(
                ambiente, "RELAY_TIMEOUT_CONFIRMACAO_SEGUNDOS", 5
            ),
            politica=PoliticaRetentativa(
                base=timedelta(
                    seconds=_segundos(ambiente, "RELAY_ESPERA_BASE_SEGUNDOS", 1)
                ),
                teto=timedelta(
                    seconds=_segundos(ambiente, "RELAY_ESPERA_TETO_SEGUNDOS", 60)
                ),
            ),
            health_porta=int(ambiente.get("HEALTH_PORTA", "8080")),
            metricas_porta=int(ambiente.get("METRICAS_PORTA", "9464")),
            prontidao_timeout_segundos=_segundos(
                ambiente, "PRONTIDAO_TIMEOUT_SEGUNDOS", 2
            ),
        )


def criar_pool(configuracao: ConfiguracaoRelay) -> Pool:
    # Como em lancamentos (002-T5): autocommit para `conexao.transaction()`
    # ser a transação inteira, e `check` para o pool se recuperar sozinho
    # depois de queda do banco. O laço usa uma conexão por vez; o ready, outra.
    return AsyncConnectionPool(
        configuracao.conninfo,
        min_size=1,
        max_size=2,
        timeout=configuracao.prontidao_timeout_segundos,
        kwargs={"autocommit": True},
        check=AsyncConnectionPool.check_connection,
        open=False,
        name=COMPONENTE,
    )


def fabrica_de_conexao(pool: Pool) -> FabricaConexao:
    def conexao() -> AbstractAsyncContextManager[AsyncConnection[Any]]:
        return pool.connection()

    return conexao


def prontidao_de(
    pool: Pool, publicador: PublicadorRabbitMQ, timeout_segundos: float
) -> Prontidao:
    """Ready = outbox legível como livro_relay e RabbitMQ conectado (§12)."""

    async def banco() -> bool:
        async def consultar() -> None:
            async with pool.connection(timeout=timeout_segundos) as conexao:
                # Tabela e privilégio das migrações 0001 e 0002.
                await conexao.execute("SELECT evento_id FROM outbox LIMIT 0")

        try:
            await asyncio.wait_for(consultar(), timeout_segundos)
        except (psycopg.Error, PoolTimeout, TimeoutError, OSError):
            return False
        return True

    async def verificar() -> dict[str, bool]:
        return {"banco": await banco(), "rabbitmq": publicador.conectado()}

    return verificar


# O que o laço usa de PublicarPendentes, MedicaoOutboxPostgres e
# PublicadorRabbitMQ.
class Ciclo(Protocol):
    async def executar(self) -> ResumoCiclo: ...


class Medicao(Protocol):
    async def medir(self) -> EstadoOutbox: ...


class Conexao(Protocol):
    async def abrir(self) -> None: ...

    async def fechar(self) -> None: ...


class LacoRelay:
    def __init__(
        self,
        configuracao: ConfiguracaoRelay,
        ciclo: Ciclo,
        medicao: Medicao,
        publicador: Conexao,
        metricas: MetricasRelay,
    ) -> None:
        self._configuracao = configuracao
        self._ciclo = ciclo
        self._medicao = medicao
        self._publicador = publicador
        self._metricas = metricas
        self._publicador_aberto = False

    async def executar(self, parar: asyncio.Event) -> None:
        while not parar.is_set():
            lote_cheio = await self.passo()
            if lote_cheio:
                continue
            try:
                await asyncio.wait_for(
                    parar.wait(), self._configuracao.intervalo_segundos
                )
            except TimeoutError:
                pass

    async def passo(self) -> bool:
        """Um ciclo e uma medição; verdadeiro se o lote veio cheio."""
        await self._abrir_publicador()
        lote_cheio = False
        try:
            resumo = await self._ciclo.executar()
        except Exception as erro:  # noqa: BLE001 - o laço não para (design §8)
            log.warning("ciclo de publicação falhou", extra=_erro(erro))
        else:
            self._metricas.registrar_ciclo(resumo)
            lote_cheio = resumo.reservados >= self._configuracao.lote
            if resumo.reservados:
                log.info(
                    "ciclo de publicação",
                    extra={
                        "reservados": resumo.reservados,
                        **{r.value: n for r, n in resumo.resultados.items()},
                    },
                )
        try:
            self._metricas.registrar_outbox(await self._medicao.medir())
        except Exception as erro:  # noqa: BLE001 - métrica não derruba o relay
            log.warning("medição da outbox falhou", extra=_erro(erro))
        return lote_cheio

    async def _abrir_publicador(self) -> None:
        if self._publicador_aberto:
            return
        try:
            await self._publicador.abrir()
        except Exception as erro:  # noqa: BLE001 - RabbitMQ fora na subida
            await self._publicador.fechar()
            log.warning("RabbitMQ indisponível", extra=_erro(erro))
            return
        self._publicador_aberto = True
        log.info("publicador conectado ao RabbitMQ")


def _erro(erro: BaseException) -> dict[str, str]:
    # Só o tipo: a mensagem do driver pode repetir dados (INFRA 8.2).
    return {"erro_tipo": type(erro).__name__}
