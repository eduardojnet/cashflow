"""Caso de uso da reconciliação com o livro (005-T5; FR-001; D-002, D-004).

Recebe o relatório de totais exportado no lado do livro e o compara com a
geração ativa, numa só transação somente leitura no papel
consolidado_operacao (FR-008): totais por data das contribuições até a
posição do relatório, posições faltantes de 1 até ela e coerência do saldo
materializado. O relatório chega por arquivo ou pela entrada padrão; o
coordenador não alcança o livro (INFRA 6.7).

Só reporta (D-004). O log leva geração, posição, contagens e o operador
técnico, nunca valores (design 005 §6; INFRA 8.2): os totais ficam só no
relatório devolvido.
"""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol

from coordenador.dominio import (
    IntervaloFaltante,
    Reconciliacao,
    RelatorioLivro,
    SaldoMaterializado,
    TotalDoDia,
    comparar_saldo_materializado,
    comparar_totais,
)

log = logging.getLogger("coordenador.reconciliacao")


class TransacaoReconciliacao(Protocol):
    """Leitura da projeção para a reconciliação, num só instante."""

    def operador(self) -> str: ...

    def geracao_ativa(self) -> tuple[int, int]:
        """Identificador e checkpoint da geração ativa."""
        ...

    def totais_ate(self, geracao_id: int, ate_posicao: int) -> list[TotalDoDia]:
        """Totais por data das contribuições com posição até `ate_posicao`."""
        ...

    def faltantes_ate(
        self, geracao_id: int, ate_posicao: int, limite: int
    ) -> tuple[int, list[IntervaloFaltante]]:
        """Total de posições de 1 até `ate_posicao` sem contribuição e os
        primeiros `limite` intervalos delas, em ordem."""
        ...

    def saldos(self, geracao_id: int) -> list[SaldoMaterializado]:
        """`saldo_diario` da geração, por data."""
        ...

    def totais_das_contribuicoes(self, geracao_id: int) -> list[SaldoMaterializado]:
        """Soma de todas as contribuições da geração, por data."""
        ...


AbrirTransacaoReconciliacao = Callable[
    [], AbstractContextManager[TransacaoReconciliacao]
]


class Reconciliar:
    """Compara a geração ativa com o relatório do livro, sem alterar nada."""

    def __init__(self, abrir: AbrirTransacaoReconciliacao) -> None:
        self._abrir = abrir

    def __call__(
        self, relatorio: RelatorioLivro, limite_faltantes: int = 20
    ) -> Reconciliacao:
        if limite_faltantes < 0:
            raise ValueError("o limite de faltantes não pode ser negativo")
        ate = relatorio.ate_posicao
        with self._abrir() as transacao:
            geracao_id, checkpoint = transacao.geracao_ativa()
            projecao = transacao.totais_ate(geracao_id, ate)
            faltantes, primeiros = transacao.faltantes_ate(
                geracao_id, ate, limite_faltantes
            )
            inconsistentes = comparar_saldo_materializado(
                transacao.saldos(geracao_id),
                transacao.totais_das_contribuicoes(geracao_id),
            )
            operador = transacao.operador()
        resultado = Reconciliacao(
            geracao_id=geracao_id,
            ate_posicao=ate,
            checkpoint=checkpoint,
            datas_no_livro=len(relatorio.datas),
            divergencias=tuple(comparar_totais(relatorio.datas, projecao)),
            saldo_inconsistente=tuple(inconsistentes),
            posicoes_faltantes=faltantes,
            primeiros_faltantes=tuple(primeiros),
        )
        log.info(
            "reconciliação concluída",
            extra={
                "geracao_id": geracao_id,
                "ate_posicao": ate,
                "checkpoint": checkpoint,
                "conciliada": resultado.conciliada,
                "datas_divergentes": len(resultado.divergencias),
                "datas_inconsistentes": len(resultado.saldo_inconsistente),
                "posicoes_faltantes": faltantes,
                "operador": operador,
            },
        )
        return resultado
