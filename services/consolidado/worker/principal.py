"""Composição e laço do processo do worker (004-T3; design 004 §3, §8, §12).

O laço abre o consumidor, entrega as mensagens ao processador, uma por vez, e
reabre tudo quando a sessão termina por falha. Nenhuma falha encerra o
processo:

- banco do consolidado fora: o consumo fica pausado (nenhum consumidor na
  fila) até o banco responder; o ready fica falso (design 004 §8). Durante a
  sessão, o banco é conferido a cada intervalo de reconexão, para pausar
  também com a fila parada;
- falha na transação ou no ACK: a sessão termina sem ACK, a conexão fecha e
  o broker devolve as mensagens pendentes, que voltam como reentrega;
- RabbitMQ fora: a abertura falha e é tentada de novo a cada intervalo.

Com o banco pronto, a sessão roda `preparar` antes de consumir: o worker
publica ali o checkpoint da geração ativa nas métricas (004-T4). Falha em
`preparar` encerra a sessão como qualquer outra falha.

Mensagem que falha sempre fica na fila, sem descarte (INFRA 4.10), e aparece
em `cashflow_worker_reentregas_total`.

Variáveis (ver infra/compose.yaml; DQ-003):

- `PG_CONSOLIDADO_SENHA_WORKER` (obrigatória), `PG_CONSOLIDADO_HOST`,
  `PG_CONSOLIDADO_PORTA`, `PG_CONSOLIDADO_BANCO`,
  `PG_CONSOLIDADO_USUARIO_WORKER`;
- `RABBITMQ_SENHA_WORKER` (obrigatória), `RABBITMQ_HOST`, `RABBITMQ_PORTA`,
  `RABBITMQ_USUARIO_WORKER`;
- `WORKER_PREFETCH`, `WORKER_INTERVALO_RECONEXAO_SEGUNDOS`;
- `HEALTH_PORTA` (padrão 8080), `METRICAS_PORTA` (padrão 9464),
  `PRONTIDAO_TIMEOUT_SEGUNDOS`.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import psycopg
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from consolidado.adaptadores.postgres import FabricaConexao
from consolidado.adaptadores.rabbitmq.consumidor import Tratar
from consolidado.worker.metricas import COMPONENTE
from consolidado.worker.saude import Prontidao

log = logging.getLogger("consolidado.worker.laco")

Pool = AsyncConnectionPool[AsyncConnection[Any]]
BancoPronto = Callable[[], Awaitable[bool]]
Preparar = Callable[[], Awaitable[object]]


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


def _positivo(ambiente: Mapping[str, str], nome: str, padrao: int) -> int:
    valor = int(ambiente.get(nome, str(padrao)))
    if valor <= 0:
        raise ValueError(f"{nome} deve ser positivo")
    return valor


@dataclass(frozen=True)
class ConfiguracaoWorker:
    conninfo: str
    url_rabbitmq: str
    prefetch: int = 10
    intervalo_reconexao_segundos: float = 2.0
    health_porta: int = 8080
    metricas_porta: int = 9464
    prontidao_timeout_segundos: float = 2.0

    @classmethod
    def de_ambiente(cls, ambiente: Mapping[str, str]) -> "ConfiguracaoWorker":
        conninfo = make_conninfo(
            host=ambiente.get("PG_CONSOLIDADO_HOST", "pg-consolidado"),
            port=int(ambiente.get("PG_CONSOLIDADO_PORTA", "5432")),
            dbname=ambiente.get("PG_CONSOLIDADO_BANCO", "consolidado"),
            user=ambiente.get("PG_CONSOLIDADO_USUARIO_WORKER", "consolidado_worker"),
            password=_obrigatoria(ambiente, "PG_CONSOLIDADO_SENHA_WORKER"),
            connect_timeout=5,
            application_name=COMPONENTE,
        )
        usuario = quote(ambiente.get("RABBITMQ_USUARIO_WORKER", "worker"), safe="")
        senha = quote(_obrigatoria(ambiente, "RABBITMQ_SENHA_WORKER"), safe="")
        host = ambiente.get("RABBITMQ_HOST", "rabbitmq")
        porta = int(ambiente.get("RABBITMQ_PORTA", "5672"))
        return cls(
            conninfo=conninfo,
            url_rabbitmq=f"amqp://{usuario}:{senha}@{host}:{porta}/",
            prefetch=_positivo(ambiente, "WORKER_PREFETCH", 10),
            intervalo_reconexao_segundos=_segundos(
                ambiente, "WORKER_INTERVALO_RECONEXAO_SEGUNDOS", 2
            ),
            health_porta=int(ambiente.get("HEALTH_PORTA", "8080")),
            metricas_porta=int(ambiente.get("METRICAS_PORTA", "9464")),
            prontidao_timeout_segundos=_segundos(
                ambiente, "PRONTIDAO_TIMEOUT_SEGUNDOS", 2
            ),
        )


def criar_pool(configuracao: ConfiguracaoWorker) -> Pool:
    # Como no relay: autocommit para `conexao.transaction()` ser a transação
    # inteira e `check` para o pool se recuperar depois de queda do banco. O
    # processamento usa uma conexão por vez; o ready, outra.
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


def banco_pronto_de(pool: Pool, timeout_segundos: float) -> BancoPronto:
    """Controle da projeção legível como consolidado_worker (migração 0001)."""

    async def banco() -> bool:
        async def consultar() -> None:
            async with pool.connection(timeout=timeout_segundos) as conexao:
                await conexao.execute(
                    "SELECT geracao_ativa_id FROM controle_projecao LIMIT 0"
                )

        try:
            await asyncio.wait_for(consultar(), timeout_segundos)
        except (psycopg.Error, PoolTimeout, TimeoutError, OSError):
            return False
        return True

    return banco


# O que o laço usa de ConsumidorRabbitMQ.
class Consumidor(Protocol):
    async def abrir(self) -> None: ...

    async def fechar(self) -> None: ...

    def conectado(self) -> bool: ...

    async def consumir(self, tratar: Tratar, parar: asyncio.Event) -> None: ...


def prontidao_de(banco_pronto: BancoPronto, consumidor: Consumidor) -> Prontidao:
    """Ready = banco do consolidado e RabbitMQ conectados (design 004 §12)."""

    async def verificar() -> dict[str, bool]:
        return {"banco": await banco_pronto(), "rabbitmq": consumidor.conectado()}

    return verificar


class LacoWorker:
    def __init__(
        self,
        configuracao: ConfiguracaoWorker,
        consumidor: Consumidor,
        tratar: Tratar,
        banco_pronto: BancoPronto,
        preparar: Preparar | None = None,
    ) -> None:
        self._configuracao = configuracao
        self._consumidor = consumidor
        self._tratar = tratar
        self._banco_pronto = banco_pronto
        self._preparar = preparar

    async def executar(self, parar: asyncio.Event) -> None:
        while not parar.is_set():
            await self.sessao(parar)
            if not parar.is_set():
                await _esperar(parar, self._configuracao.intervalo_reconexao_segundos)

    async def sessao(self, parar: asyncio.Event) -> None:
        """Uma sessão de consumo, do banco pronto até parar ou falhar."""
        if not await self._banco_pronto():
            # Sem consumidor na fila: nada é entregue enquanto o banco não
            # puder gravar (design 004 §8).
            log.warning("banco do consolidado indisponível: consumo pausado")
            return
        encerrar = asyncio.Event()
        vigia = asyncio.create_task(self._vigiar_banco(parar, encerrar))
        try:
            if self._preparar is not None:
                await self._preparar()
            await self._consumidor.abrir()
            log.info("consumo iniciado")
            await self._consumidor.consumir(self._tratar, encerrar)
        except Exception as erro:  # noqa: BLE001 - o laço não para (design §8)
            log.warning(
                "consumo interrompido: mensagens sem ACK voltam à fila",
                extra=_erro(erro),
            )
        else:
            if not parar.is_set() and not encerrar.is_set():
                log.warning("canal do RabbitMQ fechado: consumo interrompido")
        finally:
            vigia.cancel()
            await self._fechar()

    async def _vigiar_banco(
        self, parar: asyncio.Event, encerrar: asyncio.Event
    ) -> None:
        """Encerra a sessão em `parar` ou quando o banco deixa de responder.

        Com a fila parada, nenhuma mensagem revelaria a queda; sem a vigia, o
        worker seguiria inscrito e receberia mensagens que não pode gravar.
        """
        while not parar.is_set():
            await _esperar(parar, self._configuracao.intervalo_reconexao_segundos)
            if not parar.is_set() and not await self._banco_pronto():
                log.warning("banco do consolidado indisponível: consumo pausado")
                break
        encerrar.set()

    async def _fechar(self) -> None:
        try:
            await self._consumidor.fechar()
        except Exception as erro:  # noqa: BLE001 - conexão já perdida
            log.warning("fechamento do consumidor falhou", extra=_erro(erro))


async def _esperar(parar: asyncio.Event, segundos: float) -> None:
    try:
        await asyncio.wait_for(parar.wait(), segundos)
    except TimeoutError:
        pass


def _erro(erro: BaseException) -> dict[str, str]:
    # Só o tipo: a mensagem do driver pode repetir dados (INFRA 8.2).
    return {"erro_tipo": type(erro).__name__}
