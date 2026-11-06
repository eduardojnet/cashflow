"""Entidade `Lancamento` e objetos de valor (002 D-001, D-004, D-005).

Só biblioteca padrão: o domínio não depende de framework, banco ou broker
(INFRA 2.1). Posição no livro, cliente e instante de registro são
atribuídos na transação de persistência (002-T3), não aqui.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from enum import Enum

from lancamentos.dominio.erros import (
    CampoObrigatorioAusente,
    DataInvalida,
    DescricaoInvalida,
    TipoInvalido,
    ValorInvalido,
)

# Moeda única e precisão fixa (D-004). O limite superior vem da coluna
# `valor numeric(14,2)` de design.md §4: 12 dígitos inteiros e 2 decimais.
MOEDA = "BRL"
CASAS_DECIMAIS = 2
VALOR_MAXIMO = Decimal("999999999999.99")
_QUANTUM = Decimal(1).scaleb(-CASAS_DECIMAIS)

# Somente AAAA-MM-DD. `date.fromisoformat` aceitaria também a forma básica
# (AAAAMMDD) e semanas ISO, que o contrato da API não prevê.
_FORMATO_DATA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TipoLancamento(Enum):
    """Tipo do lançamento; os valores são os do evento v1 (design.md §4)."""

    DEBITO = "DEBITO"
    CREDITO = "CREDITO"

    @classmethod
    def de(cls, bruto: object) -> "TipoLancamento":
        if bruto is None:
            raise CampoObrigatorioAusente("tipo")
        if isinstance(bruto, TipoLancamento):
            return bruto
        if isinstance(bruto, str):
            try:
                return cls(bruto)
            except ValueError:
                pass
        raise TipoInvalido("tipo", "deve ser DEBITO ou CREDITO")


@dataclass(frozen=True)
class Valor:
    """Quantia positiva em BRL com até duas casas decimais (D-004).

    O sinal não faz parte do valor: quem o define é o tipo do lançamento.
    Aceita `Decimal`, `int` ou texto decimal; recusa `float`, cuja
    representação binária não garante as duas casas.
    """

    quantia: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "quantia", _normalizar_quantia(self.quantia))

    @classmethod
    def de(cls, bruto: object) -> "Valor":
        if bruto is None:
            raise CampoObrigatorioAusente("valor")
        if isinstance(bruto, Valor):
            return bruto
        if isinstance(bruto, (bool, float)) or not isinstance(
            bruto, (Decimal, int, str)
        ):
            raise ValorInvalido("valor", "deve ser número decimal exato")
        return cls(_para_decimal(bruto))

    @property
    def moeda(self) -> str:
        return MOEDA

    def __str__(self) -> str:
        return f"{self.quantia:f}"


def _para_decimal(bruto: Decimal | int | str) -> Decimal:
    if isinstance(bruto, str):
        try:
            return Decimal(bruto.strip())
        except InvalidOperation:
            raise ValorInvalido("valor", "deve ser número decimal") from None
    return Decimal(bruto)


def _normalizar_quantia(quantia: Decimal) -> Decimal:
    if not isinstance(quantia, Decimal) or not quantia.is_finite():
        raise ValorInvalido("valor", "deve ser número decimal finito")
    if quantia <= 0:
        raise ValorInvalido("valor", "deve ser positivo")
    # Antes de quantizar: valor enorme estouraria a precisão do contexto.
    if quantia > VALOR_MAXIMO:
        raise ValorInvalido("valor", "excede o máximo permitido")
    if quantia != quantia.quantize(_QUANTUM, rounding=ROUND_DOWN):
        raise ValorInvalido(
            "valor", f"deve ter no máximo {CASAS_DECIMAIS} casas decimais"
        )
    return quantia.quantize(_QUANTUM)


def data_do_lancamento(bruto: object) -> date:
    """Dia do lançamento, como informado pelo cliente (FR-010, D-005).

    A data é um dia do calendário de America/Sao_Paulo e não sofre
    conversão de fuso. Instante com hora (`datetime`) é recusado para não
    deixar a escolha do dia implícita. Data retroativa é permitida (D-005).
    """
    if bruto is None:
        raise CampoObrigatorioAusente("data_lancamento")
    if isinstance(bruto, datetime):
        raise DataInvalida("data_lancamento", "deve ser data sem hora (AAAA-MM-DD)")
    if isinstance(bruto, date):
        return bruto
    if isinstance(bruto, str) and _FORMATO_DATA.match(bruto):
        try:
            return date.fromisoformat(bruto)
        except ValueError:
            pass
    raise DataInvalida("data_lancamento", "deve ser data válida no formato AAAA-MM-DD")


def _descricao(bruta: object) -> str | None:
    # Opcional (D-001). As fontes não definem tamanho máximo nem tratamento
    # de texto em branco; o texto é mantido como recebido.
    if bruta is None or isinstance(bruta, str):
        return bruta
    raise DescricaoInvalida("descricao", "deve ser texto")


@dataclass(frozen=True)
class Lancamento:
    """Lançamento de débito ou crédito, imutável depois de criado (FR-003)."""

    tipo: TipoLancamento
    valor: Valor
    data_lancamento: date
    descricao: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self) -> None:
        # Revalida mesmo quando construído direto, sem passar por `novo`.
        object.__setattr__(self, "tipo", TipoLancamento.de(self.tipo))
        object.__setattr__(self, "valor", Valor.de(self.valor))
        object.__setattr__(
            self, "data_lancamento", data_do_lancamento(self.data_lancamento)
        )
        object.__setattr__(self, "descricao", _descricao(self.descricao))

    @classmethod
    def novo(
        cls,
        tipo: object,
        valor: object,
        data_lancamento: object,
        descricao: object = None,
    ) -> "Lancamento":
        """Cria o lançamento a partir de dados brutos, validando cada campo (FR-002)."""
        return cls(
            tipo=TipoLancamento.de(tipo),
            valor=Valor.de(valor),
            data_lancamento=data_do_lancamento(data_lancamento),
            descricao=_descricao(descricao),
        )

    @property
    def efeito_no_saldo(self) -> Decimal:
        """Valor com o sinal dado pelo tipo: crédito soma, débito subtrai (D-004)."""
        if self.tipo is TipoLancamento.CREDITO:
            return self.valor.quantia
        return -self.valor.quantia
