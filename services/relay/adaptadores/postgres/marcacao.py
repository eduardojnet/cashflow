"""Marcação de evento publicado na outbox (003-T2; design 003 §3).

Chamada só depois do confirm positivo sem return (FR-002). Grava
`publicado_em` pelo relógio do banco e não apaga o evento, preservado para
replay (D-001; INFRA 7.2). O lease fica como estava: `publicado_em`
preenchido já tira o evento da reserva.

Não exige que o lease ainda seja deste relay. Se o lease venceu e outra
execução reservou o evento, o broker já confirmou esta cópia; marcar é
verdadeiro, e a outra publicação é a duplicata que o consumidor deduplica
(TD-001; INFRA 2.4). `publicado_em IS NULL` preserva o instante da primeira
marcação.
"""

import uuid

from relay.adaptadores.postgres.reserva import FabricaConexao

_MARCAR = """
UPDATE outbox
SET publicado_em = now()
WHERE evento_id = %(evento_id)s AND publicado_em IS NULL
"""


class RegistroPublicacaoPostgres:
    """Marca eventos publicados com o papel livro_relay."""

    def __init__(self, conexao: FabricaConexao) -> None:
        self._conexao = conexao

    async def marcar_publicado(self, evento_id: uuid.UUID) -> None:
        async with self._conexao() as conexao, conexao.transaction():
            await conexao.execute(_MARCAR, {"evento_id": evento_id})
