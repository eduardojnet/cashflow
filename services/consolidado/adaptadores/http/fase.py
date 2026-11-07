"""Fase da reconstrução exposta pela API do consolidado (005-T3; INFRA 8.3).

O coordenador roda como CLI e grava a fase em `fase_reconstrucao`; a API a
lê a cada intervalo e publica em `cashflow_reconstrucao_fase`, medidor sem
rótulos (007 design §4). O valor é o código da fase da linha atualizada por
último:

| Código | Fase |
|---|---|
| 0 | nenhuma reconstrução registrada |
| 1 | `construindo` |
| 2 | `barreira` |
| 3 | `concluida` |
| 4 | `revertida` |
| 5 | `cancelada` |
| 6 | `descartada` |
| -1 | nome de fase desconhecido |

Os códigos são os de `coordenador.dominio.fase`, repetidos aqui porque a
imagem do consolidado não leva o coordenador; um teste do coordenador
confere que os dois mapas são iguais.

Banco fora do ar não derruba nada: a leitura falha em silêncio, o medidor
mantém o último valor e o ready da API já mostra o banco indisponível.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

import psycopg
from psycopg_pool import PoolTimeout

CODIGO_SEM_RECONSTRUCAO = 0
CODIGO_FASE_DESCONHECIDA = -1
CODIGO_FASE: dict[str, int] = {
    "construindo": 1,
    "barreira": 2,
    "concluida": 3,
    "revertida": 4,
    "cancelada": 5,
    "descartada": 6,
}

# Linha atualizada por último; empate pelo id da geração mais nova.
CONSULTA_FASE = (
    "SELECT fase FROM fase_reconstrucao"
    " ORDER BY atualizado_em DESC, geracao_id DESC LIMIT 1"
)

LerFase = Callable[[], Awaitable[str | None]]


class Medidor(Protocol):
    """O `Gauge` de `Metricas.medidor`, só no que este módulo usa."""

    def set(self, value: float) -> None: ...


def codigo_da_fase(fase: str | None) -> int:
    if fase is None:
        return CODIGO_SEM_RECONSTRUCAO
    return CODIGO_FASE.get(fase, CODIGO_FASE_DESCONHECIDA)


async def atualizar_fase(ler: LerFase, medidor: Medidor) -> bool:
    """Uma leitura; False se o banco não respondeu (o medidor não muda)."""
    try:
        fase = await ler()
    except (psycopg.Error, PoolTimeout, TimeoutError, OSError):
        return False
    medidor.set(codigo_da_fase(fase))
    return True


async def vigiar_fase(
    ler: LerFase, medidor: Medidor, intervalo_segundos: float
) -> None:
    """Laço da API: lê a fase a cada intervalo até ser cancelado."""
    while True:
        await atualizar_fase(ler, medidor)
        await asyncio.sleep(intervalo_segundos)
