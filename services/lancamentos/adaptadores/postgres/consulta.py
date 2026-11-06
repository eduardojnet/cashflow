"""Leitura do livro por intervalo de datas com psycopg 3 (002-T7).

Paginação por chave (TD-006): a condição `(data_lancamento, posicao) > cursor`
e a ordem `data_lancamento, posicao` usam o índice
`ix_lancamento_data_posicao` da migração 0001. Como `posicao` é única, o par
é uma ordem total e nenhum item fica entre duas páginas. Só SELECT em
`lancamento`, o que o papel livro_api já tem (TD-003).
"""

from datetime import date

from lancamentos.adaptadores.postgres.unidade_de_trabalho import FabricaConexao
from lancamentos.aplicacao import Cursor, LancamentoListado
from lancamentos.dominio import Lancamento

_COLUNAS = "id, posicao, tipo, valor, data_lancamento, descricao, registrado_em"


class ConsultaLivroPostgres:
    """Implementa `ConsultaLivro` com o papel livro_api."""

    def __init__(self, conexao: FabricaConexao) -> None:
        self._conexao = conexao

    async def listar(
        self,
        data_inicio: date,
        data_fim: date,
        apos: Cursor | None,
        quantidade: int,
    ) -> list[LancamentoListado]:
        # Sem cursor, (data_inicio, 0) fica antes de qualquer item do
        # intervalo, porque posição é sempre positiva.
        data_apos, posicao_apos = (
            (data_inicio, 0) if apos is None else (apos.data_lancamento, apos.posicao)
        )
        async with self._conexao() as conexao:
            cursor = await conexao.execute(
                f"SELECT {_COLUNAS} FROM lancamento"
                " WHERE data_lancamento BETWEEN %s AND %s"
                " AND (data_lancamento, posicao) > (%s, %s)"
                " ORDER BY data_lancamento, posicao"
                " LIMIT %s",
                (data_inicio, data_fim, data_apos, posicao_apos, quantidade),
            )
            linhas = await cursor.fetchall()
        return [
            LancamentoListado(
                lancamento=Lancamento(
                    tipo=tipo,
                    valor=valor,
                    data_lancamento=data_lancamento,
                    descricao=descricao,
                    id=id_,
                ),
                posicao=posicao,
                registrado_em=registrado_em,
            )
            for (
                id_,
                posicao,
                tipo,
                valor,
                data_lancamento,
                descricao,
                registrado_em,
            ) in linhas
        ]
