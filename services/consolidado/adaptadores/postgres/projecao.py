"""Unidade de trabalho da projeção sobre PostgreSQL com psycopg 3 (004-T3).

Implementa a transação de design 004 §3 com o papel consolidado_worker, que
só insere na inbox, na contribuição, no marcador e na quarentena e só
atualiza as colunas de valor do saldo (migração 0001).

Deduplicação pelas chaves do banco, não por consulta prévia (TD-001): o
INSERT com `ON CONFLICT DO NOTHING` espera a transação concorrente que
inseriu a mesma chave e só então decide. Sem linha inserida, a linha que já
ocupa a chave é lida para comparar hashes.

Saldo do dia: a linha (geração, data) é criada zerada se não existir e
travada com `FOR UPDATE` até o COMMIT; o domínio calcula os totais novos
(TD-004). Contribuições concorrentes na mesma data serializam nessa linha.

Conexões: a fábrica entrega uma conexão por transação, por exemplo
`pool.connection` de psycopg_pool, com `autocommit=True`, para que
`conexao.transaction()` seja a transação inteira (mesmo padrão do livro).
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import date
from typing import Any
from uuid import UUID

import psycopg
from psycopg import AsyncConnection

from consolidado.aplicacao import ContribuicaoExistente
from consolidado.dominio import Contribuicao, SaldoDiario

FabricaConexao = Callable[[], AbstractAsyncContextManager[AsyncConnection[Any]]]


class TransacaoProjecaoPostgres:
    def __init__(self, conexao: AsyncConnection[Any]) -> None:
        self._conexao = conexao
        self.desfeita = False

    def desfazer(self) -> None:
        self.desfeita = True

    async def geracao_ativa(self) -> int:
        cursor = await self._conexao.execute(
            "SELECT geracao_ativa_id FROM controle_projecao WHERE id = 1"
        )
        linha = await cursor.fetchone()
        if linha is None:
            raise RuntimeError("controle_projecao sem geração ativa (migração 0001)")
        geracao_id: int = linha[0]
        return geracao_id

    async def registrar_inbox(
        self, geracao_id: int, evento_id: UUID, hash_evento: str
    ) -> str | None:
        cursor = await self._conexao.execute(
            "INSERT INTO inbox (geracao_id, evento_id, hash) VALUES (%s, %s, %s)"
            " ON CONFLICT (geracao_id, evento_id) DO NOTHING RETURNING 1",
            (geracao_id, evento_id, hash_evento),
        )
        if await cursor.fetchone() is not None:
            return None
        cursor = await self._conexao.execute(
            "SELECT hash FROM inbox WHERE geracao_id = %s AND evento_id = %s",
            (geracao_id, evento_id),
        )
        linha = await cursor.fetchone()
        if linha is None:
            raise RuntimeError("inbox sem a linha que recusou a inserção")
        registrado: str = linha[0]
        return registrado

    async def registrar_contribuicao(
        self, geracao_id: int, contribuicao: Contribuicao
    ) -> ContribuicaoExistente | None:
        # Sem alvo: recusa tanto o lançamento repetido (chave primária) quanto
        # outro lançamento na mesma posição (UNIQUE (geracao_id, posicao)).
        cursor = await self._conexao.execute(
            "INSERT INTO contribuicao"
            " (geracao_id, lancamento_id, posicao, data_lancamento, tipo, valor, hash)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT DO NOTHING RETURNING 1",
            (
                geracao_id,
                contribuicao.lancamento_id,
                contribuicao.posicao,
                contribuicao.data_lancamento,
                contribuicao.tipo.value,
                contribuicao.valor,
                contribuicao.hash,
            ),
        )
        if await cursor.fetchone() is not None:
            return None
        # O mesmo lançamento tem precedência sobre outro na mesma posição.
        cursor = await self._conexao.execute(
            "SELECT lancamento_id, hash FROM contribuicao"
            " WHERE geracao_id = %s AND (lancamento_id = %s OR posicao = %s)"
            " ORDER BY lancamento_id = %s DESC LIMIT 1",
            (
                geracao_id,
                contribuicao.lancamento_id,
                contribuicao.posicao,
                contribuicao.lancamento_id,
            ),
        )
        linha = await cursor.fetchone()
        if linha is None:
            raise RuntimeError("contribuição sem a linha que recusou a inserção")
        return ContribuicaoExistente(lancamento_id=linha[0], hash=linha[1])

    async def saldo_para_atualizar(self, geracao_id: int, data: date) -> SaldoDiario:
        await self._conexao.execute(
            "INSERT INTO saldo_diario (geracao_id, data) VALUES (%s, %s)"
            " ON CONFLICT (geracao_id, data) DO NOTHING",
            (geracao_id, data),
        )
        cursor = await self._conexao.execute(
            "SELECT total_creditos, total_debitos FROM saldo_diario"
            " WHERE geracao_id = %s AND data = %s FOR UPDATE",
            (geracao_id, data),
        )
        linha = await cursor.fetchone()
        if linha is None:
            raise RuntimeError("saldo do dia ausente após a criação")
        return SaldoDiario(data, total_creditos=linha[0], total_debitos=linha[1])

    async def gravar_saldo(self, geracao_id: int, saldo: SaldoDiario) -> None:
        await self._conexao.execute(
            "UPDATE saldo_diario"
            " SET total_creditos = %s, total_debitos = %s, saldo_dia = %s"
            " WHERE geracao_id = %s AND data = %s",
            (
                saldo.total_creditos,
                saldo.total_debitos,
                saldo.saldo_dia,
                geracao_id,
                saldo.data,
            ),
        )

    async def marcar_inclusao(
        self, geracao_id: int, contribuicao: Contribuicao
    ) -> None:
        await self._conexao.execute(
            "INSERT INTO marcador_inclusao (geracao_id, lancamento_id, posicao)"
            " VALUES (%s, %s, %s)",
            (geracao_id, contribuicao.lancamento_id, contribuicao.posicao),
        )

    async def quarentenar(
        self, evento_id: UUID | None, motivo: str, payload_bruto: bytes
    ) -> None:
        await self._conexao.execute(
            "INSERT INTO quarentena (evento_id, motivo, payload_bruto)"
            " VALUES (%s, %s, %s)",
            (evento_id, motivo, payload_bruto),
        )


class UnidadeDeTrabalhoProjecaoPostgres:
    """Implementa `UnidadeDeTrabalhoProjecao` com o papel consolidado_worker."""

    def __init__(self, conexao: FabricaConexao) -> None:
        self._conexao = conexao

    @asynccontextmanager
    async def transacao(self) -> AsyncIterator[TransacaoProjecaoPostgres]:
        async with self._conexao() as conexao, conexao.transaction():
            transacao = TransacaoProjecaoPostgres(conexao)
            yield transacao
            if transacao.desfeita:
                # Absorvido pelo bloco da transação: ROLLBACK sem exceção.
                raise psycopg.Rollback()
