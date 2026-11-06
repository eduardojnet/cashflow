"""Unidade de trabalho do livro sobre PostgreSQL com psycopg 3 (002-T3).

Uma transação local grava a resposta idempotente, a próxima posição do
contador, o lançamento e o evento `LancamentoRegistrado` v1 na outbox, ou não
grava nada (TD-001, TD-002, TD-004; INFRA 3.1, 3.3). Nenhuma chamada a
RabbitMQ ou ao consolidado: a publicação é do relay (spec 003, INFRA 2.1).

Concorrência:
- A linha única de `contador_posicao` é travada pelo UPDATE e só é liberada no
  COMMIT ou ROLLBACK. Registros ficam serializados no contador e a posição é
  contígua: rollback devolve o incremento, ao contrário de uma sequence
  (TD-002).
- Duas requisições simultâneas com a mesma chave passam ambas pela leitura
  inicial sem encontrar resposta. A segunda espera o contador, grava e falha
  na chave primária de `resposta_idempotente`; o ROLLBACK desfaz tudo o que
  ela gravou, e a resposta da primeira é relida (design 002 §8).

Conexões: a fábrica entrega uma conexão por registro, por exemplo
`pool.connection` de psycopg_pool. Cada etapa roda dentro de
`conexao.transaction()`; com `autocommit=True` (recomendado pelo psycopg)
esse bloco é a transação inteira.
"""

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection, errors
from psycopg.types.json import Jsonb

from lancamentos.aplicacao import (
    STATUS_CRIADO,
    ConflitoIdempotencia,
    RegistrarLancamentoCmd,
    RespostaRegistro,
)
from lancamentos.dominio import Lancamento

FabricaConexao = Callable[[], AbstractAsyncContextManager[AsyncConnection[Any]]]

# Contrato do evento consumido por 003 e 004 (design 002 §4).
TIPO_EVENTO = "LancamentoRegistrado"
VERSAO_EVENTO = 1

# Restrição violada quando outra transação gravou a mesma chave antes.
_PK_RESPOSTA = "resposta_idempotente_pkey"


class UnidadeDeTrabalhoLivroPostgres:
    """Implementa `UnidadeDeTrabalhoLivro` com o papel livro_api."""

    def __init__(self, conexao: FabricaConexao) -> None:
        self._conexao = conexao

    async def registrar(
        self, cmd: RegistrarLancamentoCmd, chave: str, cliente: str
    ) -> RespostaRegistro:
        hash_requisicao = cmd.hash_requisicao
        async with self._conexao() as conexao:
            try:
                async with conexao.transaction():
                    original = await self._resposta_gravada(
                        conexao, cliente, chave, hash_requisicao
                    )
                    if original is not None:
                        return original
                    return await self._gravar(
                        conexao, cmd.lancamento, chave, cliente, hash_requisicao
                    )
            except errors.UniqueViolation as erro:
                if erro.diag.constraint_name != _PK_RESPOSTA:
                    raise
            # A transação concorrente com a mesma chave já confirmou.
            async with conexao.transaction():
                original = await self._resposta_gravada(
                    conexao, cliente, chave, hash_requisicao
                )
        if original is None:
            raise RuntimeError("resposta idempotente ausente após conflito de chave")
        return original

    async def _resposta_gravada(
        self,
        conexao: AsyncConnection[Any],
        cliente: str,
        chave: str,
        hash_requisicao: str,
    ) -> RespostaRegistro | None:
        cursor = await conexao.execute(
            "SELECT hash_requisicao, status_http, corpo FROM resposta_idempotente"
            " WHERE cliente_id = %s AND chave = %s",
            (cliente, chave),
        )
        linha = await cursor.fetchone()
        if linha is None:
            return None
        hash_gravado, status_http, corpo = linha
        if hash_gravado != hash_requisicao:
            raise ConflitoIdempotencia()
        return RespostaRegistro(status_http=status_http, corpo=corpo, repetida=True)

    async def _gravar(
        self,
        conexao: AsyncConnection[Any],
        lancamento: Lancamento,
        chave: str,
        cliente: str,
        hash_requisicao: str,
    ) -> RespostaRegistro:
        posicao = await self._proxima_posicao(conexao)
        registrado_em = await self._inserir_lancamento(
            conexao, lancamento, posicao, cliente
        )
        await self._inserir_evento(conexao, lancamento, posicao, registrado_em)
        corpo: dict[str, object] = {"id": str(lancamento.id), "posicao": posicao}
        await self._inserir_resposta(
            conexao, cliente, chave, hash_requisicao, STATUS_CRIADO, corpo
        )
        return RespostaRegistro(status_http=STATUS_CRIADO, corpo=corpo)

    async def _proxima_posicao(self, conexao: AsyncConnection[Any]) -> int:
        # Trava a linha até o fim da transação (TD-002).
        cursor = await conexao.execute(
            "UPDATE contador_posicao SET valor = valor + 1 WHERE id = 1 RETURNING valor"
        )
        linha = await cursor.fetchone()
        if linha is None:
            raise RuntimeError("contador_posicao sem a linha id = 1")
        posicao: int = linha[0]
        return posicao

    async def _inserir_lancamento(
        self,
        conexao: AsyncConnection[Any],
        lancamento: Lancamento,
        posicao: int,
        cliente: str,
    ) -> datetime:
        cursor = await conexao.execute(
            "INSERT INTO lancamento"
            " (id, posicao, tipo, valor, data_lancamento, descricao, cliente_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " RETURNING registrado_em",
            (
                lancamento.id,
                posicao,
                lancamento.tipo.value,
                lancamento.valor.quantia,
                lancamento.data_lancamento,
                lancamento.descricao,
                cliente,
            ),
        )
        linha = await cursor.fetchone()
        if linha is None:
            raise RuntimeError("INSERT em lancamento sem RETURNING")
        registrado_em: datetime = linha[0]
        return registrado_em

    async def _inserir_evento(
        self,
        conexao: AsyncConnection[Any],
        lancamento: Lancamento,
        posicao: int,
        registrado_em: datetime,
    ) -> None:
        evento_id = uuid.uuid4()
        # Evento v1 (design 002 §4): valor em texto com duas casas, sem
        # descrição nem cliente.
        payload = {
            "evento_id": str(evento_id),
            "versao": VERSAO_EVENTO,
            "lancamento_id": str(lancamento.id),
            "posicao": posicao,
            "tipo": lancamento.tipo.value,
            "valor": str(lancamento.valor),
            "data_lancamento": lancamento.data_lancamento.isoformat(),
            "registrado_em": registrado_em.isoformat(),
        }
        await conexao.execute(
            "INSERT INTO outbox"
            " (evento_id, lancamento_id, posicao, tipo_evento, payload)"
            " VALUES (%s, %s, %s, %s, %s)",
            (evento_id, lancamento.id, posicao, TIPO_EVENTO, Jsonb(payload)),
        )

    async def _inserir_resposta(
        self,
        conexao: AsyncConnection[Any],
        cliente: str,
        chave: str,
        hash_requisicao: str,
        status_http: int,
        corpo: dict[str, object],
    ) -> None:
        await conexao.execute(
            "INSERT INTO resposta_idempotente"
            " (cliente_id, chave, hash_requisicao, status_http, corpo)"
            " VALUES (%s, %s, %s, %s, %s)",
            (cliente, chave, hash_requisicao, status_http, Jsonb(corpo)),
        )
