"""Saldo do dia e detecção de duplicata ou conflito (004 D-001; TD-001, TD-004).

O saldo do dia é materializado por data (TD-004); o saldo acumulado é
calculado na consulta (TD-007, 004-T5) e não pertence a esta linha.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum

from consolidado.dominio.contribuicao import Contribuicao, TipoLancamento, data_de
from consolidado.dominio.erros import CampoInvalido, DataDivergente

_ZERO = Decimal("0.00")


def _total_de(bruto: Decimal, campo: str) -> Decimal:
    if not isinstance(bruto, Decimal) or not bruto.is_finite() or bruto < 0:
        raise CampoInvalido(campo, "deve ser número decimal não negativo")
    return bruto


@dataclass(frozen=True)
class SaldoDiario:
    """Totais de um dia: créditos, débitos e saldo do dia (D-001).

    Imutável: aplicar uma contribuição devolve o saldo novo, que a aplicação
    persiste na mesma transação da contribuição (004-T3).
    """

    data: date
    total_creditos: Decimal = _ZERO
    total_debitos: Decimal = _ZERO

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", data_de(self.data, "data"))
        object.__setattr__(
            self, "total_creditos", _total_de(self.total_creditos, "total_creditos")
        )
        object.__setattr__(
            self, "total_debitos", _total_de(self.total_debitos, "total_debitos")
        )

    @classmethod
    def vazio(cls, data: date) -> "SaldoDiario":
        """Dia ainda sem contribuições."""
        return cls(data)

    @property
    def saldo_dia(self) -> Decimal:
        """Créditos menos débitos do dia; negativo quando os débitos superam."""
        return self.total_creditos - self.total_debitos

    def aplicar(self, contribuicao: Contribuicao) -> "SaldoDiario":
        """Soma o crédito ou subtrai o débito do dia do lançamento (FR-001).

        Lançamento de outro dia é recusado: o dia é a data informada no
        lançamento, sem conversão de fuso (D-003).
        """
        if contribuicao.data_lancamento != self.data:
            raise DataDivergente(
                "data_lancamento", "difere da data do saldo a que foi aplicada"
            )
        if contribuicao.tipo is TipoLancamento.CREDITO:
            return SaldoDiario(
                self.data, self.total_creditos + contribuicao.valor, self.total_debitos
            )
        return SaldoDiario(
            self.data, self.total_creditos, self.total_debitos + contribuicao.valor
        )


class Recebimento(Enum):
    """Como tratar um evento ou lançamento já visto, comparando hashes."""

    NOVO = "NOVO"
    DUPLICATA = "DUPLICATA"
    CONFLITO = "CONFLITO"


def classificar(hash_registrado: str | None, hash_recebido: str) -> Recebimento:
    """Decide entre aplicar, ignorar ou quarentenar (TD-001, FR-003).

    Vale para as duas chaves de deduplicação: o hash do evento na inbox e o
    hash da contribuição. Sem registro anterior, é novo; com o mesmo hash, é
    duplicata e não tem efeito; com hash diferente, é conflito e vai para a
    quarentena sem ser aplicado.
    """
    if not hash_recebido:
        raise CampoInvalido("hash", "não pode ser vazio")
    if hash_registrado is None:
        return Recebimento.NOVO
    if hash_registrado == hash_recebido:
        return Recebimento.DUPLICATA
    return Recebimento.CONFLITO
