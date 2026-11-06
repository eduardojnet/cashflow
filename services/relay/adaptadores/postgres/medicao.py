"""Backlog e idade do evento pendente mais antigo da outbox (003-T5; §12).

Pendente é todo evento com `publicado_em` nulo, inclusive os reagendados e
os que estão com lease: é o que ainda não chegou ao broker (FR-006; INFRA
8.3). A idade conta de `criado_em`, gravado na transação do lançamento, pelo
relógio do banco; sem pendentes, a idade é zero.

O índice parcial ix_outbox_pendente (002 migração 0001) cobre o filtro.
Leitura com o papel livro_relay, que já tem SELECT na outbox (migração 0002).
"""

from dataclasses import dataclass

from relay.adaptadores.postgres.reserva import FabricaConexao

_MEDIR = """
SELECT count(*),
       coalesce(extract(epoch FROM now() - min(criado_em)), 0)::float8
FROM outbox
WHERE publicado_em IS NULL
"""


@dataclass(frozen=True)
class EstadoOutbox:
    pendentes: int
    idade_mais_antigo_segundos: float


class MedicaoOutboxPostgres:
    def __init__(self, conexao: FabricaConexao) -> None:
        self._conexao = conexao

    async def medir(self) -> EstadoOutbox:
        async with self._conexao() as conexao:
            cursor = await conexao.execute(_MEDIR)
            linha = await cursor.fetchone()
        assert linha is not None
        pendentes, idade = linha
        return EstadoOutbox(pendentes=pendentes, idade_mais_antigo_segundos=idade)
