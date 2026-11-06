"""Leitura da geração ativa com psycopg 3 e o papel consolidado_api (004-T5).

O papel só tem SELECT em geração, controle, saldo, marcador e checkpoint
(migração 0001): esta leitura não escreve em nada.

Geração ativa (FR-007; INFRA 2.3): cada consulta é um único comando SQL que
parte de `controle_projecao` e junta checkpoint e saldos pela geração que o
ponteiro indica. Um comando lê um único instante do banco, mesmo em READ
COMMITTED: a troca de geração da spec 005, que altera o ponteiro, nunca
mistura numa resposta o checkpoint de uma geração com o saldo de outra.

Saldo acumulado na leitura (TD-007): soma de `saldo_dia` das datas até a
consultada, pela chave primária (geracao_id, data). No intervalo, soma das
datas anteriores ao primeiro item da página mais a soma corrente na página.

Existir linha em `saldo_diario` é ter lançamento no dia: o worker cria a
linha e aplica a contribuição na mesma transação (004-T3), e contribuição
tem valor positivo.
"""

from datetime import date
from uuid import UUID

from consolidado.adaptadores.postgres.projecao import FabricaConexao
from consolidado.aplicacao import (
    Inclusao,
    SaldoConsultado,
    SaldoDaData,
    SaldosLidos,
)

_SALDO_DA_DATA = """
SELECT p.geracao_ativa_id, c.posicao_continua,
       s.total_creditos, s.total_debitos, s.saldo_dia,
       (SELECT sum(a.saldo_dia) FROM saldo_diario a
         WHERE a.geracao_id = p.geracao_ativa_id AND a.data <= %(data)s)
  FROM controle_projecao p
  JOIN checkpoint c ON c.geracao_id = p.geracao_ativa_id
  LEFT JOIN saldo_diario s
    ON s.geracao_id = p.geracao_ativa_id AND s.data = %(data)s
 WHERE p.id = 1
"""

# Sem página, a junção à esquerda ainda devolve uma linha, com o checkpoint.
_SALDOS = """
WITH ativa AS (
    SELECT p.geracao_ativa_id AS geracao_id, c.posicao_continua
      FROM controle_projecao p
      JOIN checkpoint c ON c.geracao_id = p.geracao_ativa_id
     WHERE p.id = 1
), pagina AS (
    SELECT s.data, s.total_creditos, s.total_debitos, s.saldo_dia
      FROM saldo_diario s JOIN ativa ON s.geracao_id = ativa.geracao_id
     WHERE s.data BETWEEN %(inicio)s AND %(fim)s AND s.data > %(apos)s
     ORDER BY s.data
     LIMIT %(quantidade)s
), anterior AS (
    SELECT coalesce(sum(s.saldo_dia), 0) AS saldo
      FROM saldo_diario s JOIN ativa ON s.geracao_id = ativa.geracao_id
     WHERE s.data < (SELECT min(data) FROM pagina)
)
SELECT ativa.geracao_id, ativa.posicao_continua,
       pagina.data, pagina.total_creditos, pagina.total_debitos, pagina.saldo_dia,
       anterior.saldo + sum(pagina.saldo_dia) OVER (ORDER BY pagina.data)
  FROM ativa
 CROSS JOIN anterior
  LEFT JOIN pagina ON true
 ORDER BY pagina.data
"""

_INCLUSAO = """
SELECT p.geracao_ativa_id, m.posicao
  FROM controle_projecao p
  LEFT JOIN marcador_inclusao m
    ON m.geracao_id = p.geracao_ativa_id AND m.lancamento_id = %(lancamento_id)s
 WHERE p.id = 1
"""


def _sem_geracao_ativa() -> RuntimeError:
    return RuntimeError("controle_projecao sem geração ativa (migração 0001)")


class LeituraConsolidadoPostgres:
    """Implementa `LeituraConsolidado` com o papel consolidado_api."""

    def __init__(self, conexao: FabricaConexao) -> None:
        self._conexao = conexao

    async def saldo_da_data(self, data: date) -> SaldoDaData:
        async with self._conexao() as conexao:
            cursor = await conexao.execute(_SALDO_DA_DATA, {"data": data})
            linha = await cursor.fetchone()
        if linha is None:
            raise _sem_geracao_ativa()
        geracao_id, checkpoint, creditos, debitos, saldo_dia, acumulado = linha
        saldo = (
            None
            if saldo_dia is None
            else SaldoConsultado(data, creditos, debitos, saldo_dia, acumulado)
        )
        return SaldoDaData(geracao_id, checkpoint, saldo)

    async def saldos(
        self, inicio: date, fim: date, apos: date | None, quantidade: int
    ) -> SaldosLidos:
        # Sem cursor, o dia anterior ao início fica antes de qualquer item.
        parametros = {
            "inicio": inicio,
            "fim": fim,
            "apos": date.min if apos is None else apos,
            "quantidade": quantidade,
        }
        async with self._conexao() as conexao:
            cursor = await conexao.execute(_SALDOS, parametros)
            linhas = await cursor.fetchall()
        if not linhas:
            raise _sem_geracao_ativa()
        geracao_id, checkpoint = linhas[0][0], linhas[0][1]
        itens = [
            SaldoConsultado(data, creditos, debitos, saldo_dia, acumulado)
            for _, _, data, creditos, debitos, saldo_dia, acumulado in linhas
            if data is not None
        ]
        return SaldosLidos(geracao_id, checkpoint, itens)

    async def inclusao(self, lancamento_id: UUID) -> Inclusao:
        async with self._conexao() as conexao:
            cursor = await conexao.execute(_INCLUSAO, {"lancamento_id": lancamento_id})
            linha = await cursor.fetchone()
        if linha is None:
            raise _sem_geracao_ativa()
        geracao_id, posicao = linha
        return Inclusao(geracao_id, incluido=posicao is not None, posicao=posicao)
