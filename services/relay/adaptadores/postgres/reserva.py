"""Reserva de eventos da outbox com lease e SKIP LOCKED (003-T1; TD-001).

Uma transação curta escolhe um lote de eventos pendentes, grava `lease_dono`
e `lease_ate` e confirma. A publicação acontece depois, fora da transação
(design 003 §4), sem manter trava no livro enquanto espera o RabbitMQ.

Pendente é `publicado_em` nulo, `proxima_tentativa_em` nula ou vencida e
lease nulo ou vencido. Exclusividade por tentativa (FR-004):
- `FOR UPDATE SKIP LOCKED`: duas reservas simultâneas não esperam uma pela
  outra e não escolhem a mesma linha; a que chega depois pula as travadas.
- Depois do COMMIT, o lease ativo tira o evento da seleção até `lease_ate`.
  Se a outra reserva já tinha lido a linha antes do COMMIT, o READ COMMITTED
  reavalia o filtro sobre a versão confirmada ao travar, e o lease a exclui.

Relay que cai com lease ativo não perde o evento (NFR-003; INFRA 7.2): o
lease vence e outra execução o reserva. Isso pode publicar duas vezes, o que
o consumidor deduplica pelo `evento_id` (TD-001; INFRA 2.4).

Os instantes vêm do relógio do banco (`now()`), comum a todas as execuções.
Conexões como em `lancamentos.adaptadores.postgres`: a fábrica entrega uma
conexão, por exemplo `pool.connection` de psycopg_pool, com `autocommit=True`
recomendado; `conexao.transaction()` é a transação inteira.
"""

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import class_row

FabricaConexao = Callable[[], AbstractAsyncContextManager[AsyncConnection[Any]]]

# A CTE trava e escolhe; o UPDATE grava o lease nas linhas travadas. O índice
# parcial ix_outbox_pendente (002 migração 0001) serve a ordem por posição.
_RESERVAR = """
WITH escolhidos AS (
    SELECT evento_id
    FROM outbox
    WHERE publicado_em IS NULL
      AND (proxima_tentativa_em IS NULL OR proxima_tentativa_em <= now())
      AND (lease_ate IS NULL OR lease_ate <= now())
    ORDER BY posicao
    LIMIT %(lote)s
    FOR UPDATE SKIP LOCKED
)
UPDATE outbox
SET lease_dono = %(dono)s, lease_ate = now() + %(lease)s
FROM escolhidos
WHERE outbox.evento_id = escolhidos.evento_id
RETURNING outbox.evento_id, outbox.posicao, outbox.tipo_evento, outbox.payload,
          outbox.tentativas, outbox.lease_ate
"""


@dataclass(frozen=True)
class EventoReservado:
    """Evento reservado para uma tentativa de publicação."""

    evento_id: uuid.UUID
    posicao: int
    tipo_evento: str
    payload: dict[str, Any]
    tentativas: int
    lease_ate: datetime


class ReservaOutboxPostgres:
    """Reserva lotes da outbox com o papel livro_relay."""

    def __init__(
        self, conexao: FabricaConexao, dono: str, lote: int, lease: timedelta
    ) -> None:
        if not dono:
            raise ValueError("dono do lease vazio")
        if lote <= 0:
            raise ValueError("lote deve ser positivo")
        if lease <= timedelta(0):
            raise ValueError("lease deve ser positivo")
        self._conexao = conexao
        self._dono = dono
        self._lote = lote
        self._lease = lease

    async def reservar(self) -> list[EventoReservado]:
        """Reserva até `lote` eventos pendentes, em ordem de posição."""
        async with self._conexao() as conexao:
            async with conexao.transaction():
                cursor = conexao.cursor(row_factory=class_row(EventoReservado))
                await cursor.execute(
                    _RESERVAR,
                    {"lote": self._lote, "dono": self._dono, "lease": self._lease},
                )
                eventos = await cursor.fetchall()
        # RETURNING não preserva a ordem da CTE.
        return sorted(eventos, key=lambda evento: evento.posicao)
