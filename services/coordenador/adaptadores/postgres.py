"""Transação do coordenador no banco do consolidado com psycopg 3 (005-T3,
005-T4).

Implementa `TransacaoGeracoes` e `TransacaoTroca` com o papel
consolidado_operacao, que cria a candidata e o checkpoint dela, muda o estado
das gerações, ergue e libera a barreira, aponta a geração ativa, grava a
fase e lê checkpoint, lacunas, contribuições e consumidores (migração 0002).
Sem
SQL no banco do livro: a rede do coordenador só alcança o pg-consolidado
(INFRA 6.7).

Conexão síncrona, uma por comando: o coordenador é uma CLI de execução
curta, como o comando de republicação do relay. A transação é a do
`psycopg.connect` em contexto: COMMIT na saída normal, ROLLBACK em exceção.
O `status` lê em REPEATABLE READ somente leitura, para que todas as
consultas vejam o mesmo instante.

Identidade (FR-008): cada transação começa conferindo `current_user`; outro
papel que não consolidado_operacao recebe `IdentidadeRecusada` antes de
qualquer outro comando. Os privilégios do banco continuam sendo o controle
principal (migração 0002); a verificação cobre o dono das tabelas.

Concorrência: dois `iniciar` simultâneos esbarram na exclusão
`ex_geracao_uma_alem_da_ativa`; o segundo espera o primeiro e, se ele
confirmar, recebe `ReconstrucaoRecusada`. Troca, rollback e descarte rodam
sob o bloqueio consultivo `CHAVE_BLOQUEIO_OPERACAO`, tomado numa conexão
própria durante toda a operação (`bloqueio_de_operacao`): com ele ocupado, a
operação é recusada sem esperar. Se o coordenador cair, o fim da sessão
solta o bloqueio.

Troca e rollback mudam duas gerações na mesma transação. A ativa sai antes
(`ux_geracao_uma_ativa` não é adiável) e, entre os dois comandos, há duas
gerações fora da ativa: por isso `adiar_exclusao` adia
`ex_geracao_uma_alem_da_ativa` para o COMMIT, que a confere.
"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from typing import Any

import psycopg

from coordenador.aplicacao import (
    PAPEL_OPERACAO,
    Controle,
    IdentidadeRecusada,
    OperacaoConcorrente,
    PedidoBarreira,
    Situacao,
    SituacaoConsumidor,
    SituacaoGeracao,
)
from coordenador.dominio import (
    EstadoGeracao,
    FaseReconstrucao,
    Geracao,
    Progresso,
    ReconstrucaoRecusada,
)

EXCLUSAO_UMA_ALEM_DA_ATIVA = "ex_geracao_uma_alem_da_ativa"
# Bloqueio consultivo das operações de geração (troca, rollback, descarte).
# Número fixo e exclusivo do coordenador no banco do consolidado.
CHAVE_BLOQUEIO_OPERACAO = 5_004

_GERACOES = """
SELECT g.id, g.estado, g.criada_em, g.ativada_em,
       c.posicao_continua, c.atualizado_em,
       (SELECT max(t.posicao) FROM contribuicao t WHERE t.geracao_id = g.id),
       (SELECT count(*) FROM lacuna l WHERE l.geracao_id = g.id),
       f.fase, f.atualizado_em
FROM geracao g
LEFT JOIN checkpoint c ON c.geracao_id = g.id
LEFT JOIN fase_reconstrucao f ON f.geracao_id = g.id
WHERE g.estado <> 'descartada'
ORDER BY g.id
"""


class TransacaoGeracoesPostgres:
    def __init__(self, conexao: psycopg.Connection[Any]) -> None:
        self._conexao = conexao

    def _um(self, consulta: str, *params: object) -> Any:
        linha = self._conexao.execute(consulta, params).fetchone()
        if linha is None:
            raise RuntimeError("consulta sem linha")
        return linha[0]

    def operador(self) -> str:
        operador: str = self._um("SELECT current_user")
        return operador

    def geracoes_vigentes(self) -> list[Geracao]:
        linhas = self._conexao.execute(
            "SELECT id, estado FROM geracao WHERE estado <> 'descartada' ORDER BY id"
        ).fetchall()
        return [Geracao(id_, EstadoGeracao(estado)) for id_, estado in linhas]

    def criar_candidata(self) -> int:
        try:
            geracao_id: int = self._um(
                "INSERT INTO geracao (estado) VALUES ('construindo') RETURNING id"
            )
        except psycopg.errors.ExclusionViolation as erro:
            if erro.diag.constraint_name != EXCLUSAO_UMA_ALEM_DA_ATIVA:
                raise
            # Outro operador criou a candidata entre a leitura e o INSERT.
            raise ReconstrucaoRecusada([str(EstadoGeracao.CONSTRUINDO)]) from None
        self._conexao.execute(
            "INSERT INTO checkpoint (geracao_id) VALUES (%s)", (geracao_id,)
        )
        return geracao_id

    def candidata_para_atualizar(self) -> Geracao | None:
        linha = self._conexao.execute(
            "SELECT id, estado FROM geracao WHERE estado = 'construindo' FOR UPDATE"
        ).fetchone()
        if linha is None:
            return None
        return Geracao(linha[0], EstadoGeracao(linha[1]))

    def mudar_estado(self, geracao_id: int, estado: EstadoGeracao) -> None:
        self._conexao.execute(
            "UPDATE geracao SET estado = %s WHERE id = %s", (str(estado), geracao_id)
        )

    def registrar_fase(self, geracao_id: int, fase: FaseReconstrucao) -> None:
        self._conexao.execute(
            "INSERT INTO fase_reconstrucao (geracao_id, fase) VALUES (%s, %s)"
            " ON CONFLICT (geracao_id) DO UPDATE"
            " SET fase = EXCLUDED.fase, atualizado_em = now()",
            (geracao_id, str(fase)),
        )

    def geracoes_para_atualizar(self) -> list[Geracao]:
        linhas = self._conexao.execute(
            "SELECT id, estado FROM geracao WHERE estado <> 'descartada'"
            " ORDER BY id FOR UPDATE"
        ).fetchall()
        return [Geracao(id_, EstadoGeracao(estado)) for id_, estado in linhas]

    def controle_para_atualizar(self) -> Controle:
        linha = self._conexao.execute(
            "SELECT geracao_ativa_id, barreira, versao FROM controle_projecao"
            " WHERE id = 1 FOR UPDATE"
        ).fetchone()
        if linha is None:
            raise RuntimeError("controle_projecao sem linha (migração 0001)")
        return Controle(*linha)

    def erguer_barreira(self) -> PedidoBarreira:
        anterior = self.controle_para_atualizar()
        # clock_timestamp(): o worker só confirma depois do COMMIT, então a
        # confirmação válida tem `visto_em` posterior a este instante.
        linha = self._conexao.execute(
            "UPDATE controle_projecao SET barreira = 'solicitada',"
            " versao = versao + 1 WHERE id = 1 RETURNING versao, clock_timestamp()"
        ).fetchone()
        if linha is None:
            raise RuntimeError("controle_projecao sem linha (migração 0001)")
        versao, instante = linha
        return PedidoBarreira(
            versao=versao, instante=instante, herdada=anterior.barreira == "solicitada"
        )

    def liberar_barreira(self) -> bool:
        cursor = self._conexao.execute(
            "UPDATE controle_projecao SET barreira = 'liberada',"
            " versao = versao + 1 WHERE id = 1 AND barreira = 'solicitada'"
        )
        return cursor.rowcount == 1

    def consumidores(self) -> list[SituacaoConsumidor]:
        return [
            SituacaoConsumidor(consumidor_id, pausado, visto_em)
            for consumidor_id, pausado, visto_em in self._conexao.execute(
                "SELECT consumidor_id, pausado, visto_em FROM estado_consumidor"
                " ORDER BY consumidor_id"
            ).fetchall()
        ]

    def progresso(self, geracao_id: int) -> Progresso:
        linha = self._conexao.execute(
            "SELECT (SELECT posicao_continua FROM checkpoint WHERE geracao_id = %s),"
            " (SELECT count(*) FROM lacuna WHERE geracao_id = %s)",
            (geracao_id, geracao_id),
        ).fetchone()
        if linha is None:
            raise RuntimeError("consulta sem linha")
        checkpoint, lacunas = linha
        return Progresso(checkpoint=checkpoint or 0, lacunas=lacunas)

    def fase_de(self, geracao_id: int) -> str | None:
        linha = self._conexao.execute(
            "SELECT fase FROM fase_reconstrucao WHERE geracao_id = %s", (geracao_id,)
        ).fetchone()
        return None if linha is None else str(linha[0])

    def adiar_exclusao(self) -> None:
        self._conexao.execute(f"SET CONSTRAINTS {EXCLUSAO_UMA_ALEM_DA_ATIVA} DEFERRED")

    def apontar_ativa(self, geracao_id: int) -> None:
        self._conexao.execute(
            "UPDATE controle_projecao SET geracao_ativa_id = %s,"
            " versao = versao + 1 WHERE id = 1",
            (geracao_id,),
        )

    def remover_dados(self, geracao_id: int) -> None:
        self._conexao.execute(
            "SELECT remover_dados_geracao_descartada(%s)", (geracao_id,)
        )

    def situacao(self, limite_lacunas: int) -> Situacao:
        controle = self._conexao.execute(
            "SELECT geracao_ativa_id, barreira, versao FROM controle_projecao"
            " WHERE id = 1"
        ).fetchone()
        if controle is None:
            raise RuntimeError("controle_projecao sem linha (migração 0001)")
        geracoes = tuple(
            SituacaoGeracao(
                id=id_,
                estado=EstadoGeracao(estado),
                criada_em=criada_em,
                ativada_em=ativada_em,
                checkpoint=posicao_continua or 0,
                checkpoint_atualizado_em=checkpoint_em,
                maior_posicao=maior or 0,
                lacunas=lacunas,
                primeiras_lacunas=self._primeiras_lacunas(id_, limite_lacunas),
                fase=fase,
                fase_atualizada_em=fase_em,
            )
            for (
                id_,
                estado,
                criada_em,
                ativada_em,
                posicao_continua,
                checkpoint_em,
                maior,
                lacunas,
                fase,
                fase_em,
            ) in self._conexao.execute(_GERACOES).fetchall()
        )
        consumidores = tuple(self.consumidores())
        geracao_ativa_id, barreira, versao = controle
        return Situacao(geracao_ativa_id, barreira, versao, geracoes, consumidores)

    def _primeiras_lacunas(self, geracao_id: int, limite: int) -> tuple[int, ...]:
        if limite == 0:
            return ()
        linhas = self._conexao.execute(
            "SELECT posicao FROM lacuna WHERE geracao_id = %s"
            " ORDER BY posicao LIMIT %s",
            (geracao_id, limite),
        ).fetchall()
        return tuple(posicao for (posicao,) in linhas)


def abridor_de_transacao(
    conninfo: str,
    *,
    somente_leitura: bool = False,
    papel_exigido: str | None = PAPEL_OPERACAO,
) -> Callable[[], AbstractContextManager[TransacaoGeracoesPostgres]]:
    """Fábrica de transações para os casos de uso (`AbrirTransacao`).

    `papel_exigido=None` desliga a verificação de identidade; só os testes
    usam, para mostrar que o banco também recusa os outros papéis.
    """

    @contextmanager
    def abrir() -> Iterator[TransacaoGeracoesPostgres]:
        with psycopg.connect(conninfo) as conexao:
            if somente_leitura:
                conexao.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
                conexao.read_only = True
            transacao = TransacaoGeracoesPostgres(conexao)
            if papel_exigido is not None:
                papel = transacao.operador()
                if papel != papel_exigido:
                    raise IdentidadeRecusada(papel)
            yield transacao

    return abrir


def bloqueio_de_operacao(
    conninfo: str, *, papel_exigido: str | None = PAPEL_OPERACAO
) -> Callable[[], AbstractContextManager[None]]:
    """Bloqueio exclusivo das operações de geração (`BloquearOperacao`).

    Conexão própria em autocommit, aberta durante toda a operação: o
    bloqueio consultivo de sessão não depende das transações da operação.
    Com ele ocupado, `OperacaoConcorrente` sem esperar.
    """

    @contextmanager
    def bloquear() -> Iterator[None]:
        with psycopg.connect(conninfo, autocommit=True) as conexao:
            if papel_exigido is not None:
                papel = TransacaoGeracoesPostgres(conexao).operador()
                if papel != papel_exigido:
                    raise IdentidadeRecusada(papel)
            linha = conexao.execute(
                "SELECT pg_try_advisory_lock(%s)", (CHAVE_BLOQUEIO_OPERACAO,)
            ).fetchone()
            if linha is None or not linha[0]:
                raise OperacaoConcorrente("outra operação de geração está em andamento")
            try:
                yield
            finally:
                # Conexão perdida já soltou o bloqueio com a sessão.
                with suppress(psycopg.Error):
                    conexao.execute(
                        "SELECT pg_advisory_unlock(%s)", (CHAVE_BLOQUEIO_OPERACAO,)
                    )

    return bloquear
