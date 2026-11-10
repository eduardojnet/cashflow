"""Geração ativa lida do banco pela API do consolidado (INFRA 8.3; 007 design §4).

`cashflow_geracao_ativa` mostra a geração que a API serve agora:
`controle_projecao.geracao_ativa_id`, lida no mesmo laço da fase da
reconstrução (`FASE_INTERVALO_SEGUNDOS`). Sem essa leitura, o medidor só
mudava quando uma consulta respondia: valia 0 depois de cada subida e
seguia na geração antiga depois de troca ou rollback até alguém consultar
(review 007, Issue 2).

Cada consulta continua gravando a geração que a respondeu, o que torna a
troca visível na hora. As duas escritas leem a mesma linha, e qualquer
diferença momentânea durante a troca se desfaz na leitura seguinte.

Banco fora do ar não derruba nada: o medidor mantém o último valor, como o
da fase.
"""

import asyncio
from collections.abc import Awaitable, Callable

import psycopg
from psycopg_pool import PoolTimeout

from consolidado.adaptadores.http.fase import Medidor

CONSULTA_GERACAO_ATIVA = "SELECT geracao_ativa_id FROM controle_projecao WHERE id = 1"

LerGeracaoAtiva = Callable[[], Awaitable[int | None]]


async def atualizar_geracao_ativa(ler: LerGeracaoAtiva, medidor: Medidor) -> bool:
    """Uma leitura; False se o banco não respondeu ou não tem a linha."""
    try:
        geracao_id = await ler()
    except (psycopg.Error, PoolTimeout, TimeoutError, OSError):
        return False
    if geracao_id is None:
        # Esquema sem a linha de controle: o ready já mostra o banco
        # indisponível, e nenhum número de geração seria verdadeiro.
        return False
    medidor.set(geracao_id)
    return True


async def vigiar_geracao_ativa(
    ler: LerGeracaoAtiva, medidor: Medidor, intervalo_segundos: float
) -> None:
    """Laço da API: lê a geração ativa a cada intervalo até ser cancelado."""
    while True:
        await atualizar_geracao_ativa(ler, medidor)
        await asyncio.sleep(intervalo_segundos)
