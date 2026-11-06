"""Barreira dos consumidores sobre PostgreSQL (005-T2; design 005 §4; TD-002).

Implementa `ControleBarreira` com o papel consolidado_worker, que lê
`controle_projecao` e grava só `pausado` e `visto_em` da própria linha em
`estado_consumidor` (migração 0002). Cada chamada usa uma conexão da fábrica
em autocommit: o registro fica visível ao coordenador assim que retorna.
"""

from consolidado.adaptadores.postgres.projecao import (
    FabricaConexao,
    barreira_solicitada,
)


class ControleBarreiraPostgres:
    def __init__(self, conexao: FabricaConexao) -> None:
        self._conexao = conexao

    async def barreira_solicitada(self) -> bool:
        async with self._conexao() as conexao:
            return await barreira_solicitada(conexao)

    async def registrar_consumidor(self, consumidor_id: str, pausado: bool) -> None:
        async with self._conexao() as conexao:
            await conexao.execute(
                "INSERT INTO estado_consumidor (consumidor_id, pausado, visto_em)"
                " VALUES (%s, %s, now())"
                " ON CONFLICT (consumidor_id) DO UPDATE"
                " SET pausado = EXCLUDED.pausado, visto_em = EXCLUDED.visto_em",
                (consumidor_id, pausado),
            )
