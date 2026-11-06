"""Marcação e reagendamento de eventos na outbox (003-T2, 003-T3; design 003 §3).

Marcar é chamado só depois do confirm positivo sem return (FR-002). Grava
`publicado_em` pelo relógio do banco e não apaga o evento, preservado para
replay (D-001; INFRA 7.2). O lease fica como estava: `publicado_em`
preenchido já tira o evento da reserva.

Marcar não exige que o lease ainda seja deste relay. Se o lease venceu e
outra execução reservou o evento, o broker já confirmou esta cópia; marcar é
verdadeiro, e a outra publicação é a duplicata que o consumidor deduplica
(TD-001; INFRA 2.4). `publicado_em IS NULL` preserva o instante da primeira
marcação.

Reagendar (FR-003; D-002) soma uma tentativa, agenda `proxima_tentativa_em`
pelo relógio do banco, o mesmo da reserva, e libera o lease, para o evento
voltar à reserva assim que a espera vencer. Ao contrário de marcar, exige a
própria reserva: `lease_ate` igual ao gravado por ela. Se outra execução já
reservou o evento depois do lease vencido, a tentativa dela está em curso e
não é tocada; evento já publicado também não.
"""

import uuid
from datetime import timedelta

from relay.adaptadores.postgres.reserva import FabricaConexao
from relay.aplicacao import EventoReservado

_MARCAR = """
UPDATE outbox
SET publicado_em = now()
WHERE evento_id = %(evento_id)s AND publicado_em IS NULL
"""

_REAGENDAR = """
UPDATE outbox
SET tentativas = tentativas + 1,
    proxima_tentativa_em = now() + %(espera)s,
    lease_dono = NULL,
    lease_ate = NULL
WHERE evento_id = %(evento_id)s
  AND publicado_em IS NULL
  AND lease_ate = %(lease_ate)s
"""


class RegistroPublicacaoPostgres:
    """Marca e reagenda eventos com o papel livro_relay."""

    def __init__(self, conexao: FabricaConexao) -> None:
        self._conexao = conexao

    async def marcar_publicado(self, evento_id: uuid.UUID) -> None:
        async with self._conexao() as conexao, conexao.transaction():
            await conexao.execute(_MARCAR, {"evento_id": evento_id})

    async def reagendar(self, evento: EventoReservado, espera: timedelta) -> None:
        if espera < timedelta(0):
            raise ValueError("espera não pode ser negativa")
        async with self._conexao() as conexao, conexao.transaction():
            await conexao.execute(
                _REAGENDAR,
                {
                    "evento_id": evento.evento_id,
                    "espera": espera,
                    "lease_ate": evento.lease_ate,
                },
            )
