"""Reconciliação do consolidado com o livro (005-T5; FR-001; D-002, D-004).

O lado do livro exporta os totais por data até uma posição P
(`lancamentos.comandos.relatorio`). O coordenador gera os mesmos totais da
geração ativa, só com as contribuições de posição até P, e compara data a
data; lista as posições de 1 até P que a ativa não tem (lacunas); e confere
se o saldo materializado que a API serve bate com as contribuições da ativa.
A reconciliação só reporta; a correção é a reconstrução (D-004).

O livro não tem buraco de posição (002 TD-002): até P há exatamente P
lançamentos. Um relatório em que a soma das quantidades difere de P não é
um relatório do livro e é recusado.

Mensagens de erro citam só datas, posições e campos, nunca valores (design
005 §6; INFRA 8.2).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from coordenador.dominio.erros import ErroDominio


class RelatorioInvalido(ErroDominio):
    """Relatório do livro malformado ou incoerente; nada foi comparado."""

    def __init__(self, motivo: str) -> None:
        super().__init__(f"relatório do livro inválido: {motivo}")
        self.motivo = motivo


@dataclass(frozen=True)
class TotalDoDia:
    """Quantidade de lançamentos e totais de uma data."""

    data: date
    quantidade: int
    total_creditos: Decimal
    total_debitos: Decimal

    def __post_init__(self) -> None:
        if self.quantidade < 0:
            raise RelatorioInvalido(f"quantidade negativa em {self.data}")
        if self.total_creditos < 0 or self.total_debitos < 0:
            raise RelatorioInvalido(f"total negativo em {self.data}")


@dataclass(frozen=True)
class RelatorioLivro:
    """Totais por data do livro até `ate_posicao`, uma entrada por data."""

    ate_posicao: int
    datas: tuple[TotalDoDia, ...]

    def __post_init__(self) -> None:
        if self.ate_posicao < 0:
            raise RelatorioInvalido("posição negativa")
        vistas = [dia.data for dia in self.datas]
        if len(set(vistas)) != len(vistas):
            raise RelatorioInvalido("data repetida")
        if any(dia.quantidade == 0 for dia in self.datas):
            raise RelatorioInvalido("data sem lançamentos")
        if sum(dia.quantidade for dia in self.datas) != self.ate_posicao:
            raise RelatorioInvalido(
                "a soma das quantidades difere da posição; o livro não tem"
                " buraco de posição"
            )


@dataclass(frozen=True)
class SaldoMaterializado:
    """Totais de uma data em `saldo_diario`, o que a API do consolidado lê."""

    data: date
    total_creditos: Decimal
    total_debitos: Decimal


class TipoDivergencia(StrEnum):
    # Data com lançamentos no livro e nenhuma contribuição na ativa.
    AUSENTE_NA_PROJECAO = "ausente_na_projecao"
    # Data com contribuição na ativa e nenhum lançamento no livro.
    AUSENTE_NO_LIVRO = "ausente_no_livro"
    # Data nos dois lados com quantidade ou totais diferentes.
    TOTAIS_DIFERENTES = "totais_diferentes"


@dataclass(frozen=True)
class Divergencia:
    data: date
    tipo: TipoDivergencia
    livro: TotalDoDia | None
    projecao: TotalDoDia | None


@dataclass(frozen=True)
class SaldoInconsistente:
    """Data em que o saldo materializado não é a soma das contribuições."""

    data: date
    saldo: SaldoMaterializado
    contribuicoes: SaldoMaterializado


@dataclass(frozen=True)
class IntervaloFaltante:
    """Posições de `inicio` a `fim`, inclusive, ausentes da geração."""

    inicio: int
    fim: int

    def __post_init__(self) -> None:
        if not 0 < self.inicio <= self.fim:
            raise ValueError("intervalo de posições inválido")

    @property
    def tamanho(self) -> int:
        return self.fim - self.inicio + 1


def _mesmos_totais(livro: TotalDoDia, projecao: TotalDoDia) -> bool:
    return (
        livro.quantidade == projecao.quantidade
        and livro.total_creditos == projecao.total_creditos
        and livro.total_debitos == projecao.total_debitos
    )


def comparar_totais(
    livro: Sequence[TotalDoDia], projecao: Sequence[TotalDoDia]
) -> list[Divergencia]:
    """Divergências por data entre o livro e a projeção, em ordem de data."""
    do_livro = {dia.data: dia for dia in livro}
    da_projecao = {dia.data: dia for dia in projecao}
    divergencias: list[Divergencia] = []
    for data in sorted(do_livro.keys() | da_projecao.keys()):
        no_livro, na_projecao = do_livro.get(data), da_projecao.get(data)
        if na_projecao is None:
            tipo = TipoDivergencia.AUSENTE_NA_PROJECAO
        elif no_livro is None:
            tipo = TipoDivergencia.AUSENTE_NO_LIVRO
        elif _mesmos_totais(no_livro, na_projecao):
            continue
        else:
            tipo = TipoDivergencia.TOTAIS_DIFERENTES
        divergencias.append(Divergencia(data, tipo, no_livro, na_projecao))
    return divergencias


def comparar_saldo_materializado(
    saldos: Sequence[SaldoMaterializado], contribuicoes: Sequence[SaldoMaterializado]
) -> list[SaldoInconsistente]:
    """Datas em que `saldo_diario` difere da soma das contribuições.

    Data ausente de um dos lados vale zero nos dois totais.
    """
    zero = Decimal(0)
    dos_saldos = {s.data: s for s in saldos}
    das_contribuicoes = {c.data: c for c in contribuicoes}
    inconsistentes: list[SaldoInconsistente] = []
    for data in sorted(dos_saldos.keys() | das_contribuicoes.keys()):
        saldo = dos_saldos.get(data) or SaldoMaterializado(data, zero, zero)
        soma = das_contribuicoes.get(data) or SaldoMaterializado(data, zero, zero)
        if (saldo.total_creditos, saldo.total_debitos) != (
            soma.total_creditos,
            soma.total_debitos,
        ):
            inconsistentes.append(SaldoInconsistente(data, saldo, soma))
    return inconsistentes


@dataclass(frozen=True)
class Reconciliacao:
    """Resultado da comparação da geração ativa com o relatório do livro."""

    geracao_id: int
    ate_posicao: int
    checkpoint: int
    datas_no_livro: int
    divergencias: tuple[Divergencia, ...]
    saldo_inconsistente: tuple[SaldoInconsistente, ...]
    # Total de posições de 1 até `ate_posicao` ausentes da ativa.
    posicoes_faltantes: int
    # Os primeiros intervalos faltantes, em ordem; no máximo o limite pedido.
    primeiros_faltantes: tuple[IntervaloFaltante, ...]

    @property
    def conciliada(self) -> bool:
        return (
            not self.divergencias
            and not self.saldo_inconsistente
            and self.posicoes_faltantes == 0
        )
