"""Contribuição de um lançamento ao saldo do dia (004 FR-001, D-003; TD-001).

Só biblioteca padrão: o domínio não depende de framework, banco ou broker
(INFRA 2.1). As regras de tipo, valor e data repetem as do livro (002 D-004,
D-005) sem importar o serviço de lançamentos: o consolidado só conhece o
contrato do evento v1 (002 design §4) e não compartilha código com o livro
(INFRA 6.7).
"""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from enum import Enum

from consolidado.dominio.erros import CampoInvalido, CampoObrigatorioAusente

# Mesma precisão e mesmo teto da coluna `contribuicao.valor numeric(14, 2)`
# (migração 0001 do consolidado), que repete a do livro.
CASAS_DECIMAIS = 2
VALOR_MAXIMO = Decimal("999999999999.99")
_QUANTUM = Decimal(1).scaleb(-CASAS_DECIMAIS)

# Somente AAAA-MM-DD, como o livro publica (002 design §4).
_FORMATO_DATA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TipoLancamento(Enum):
    """Tipo do lançamento; os valores são os do evento v1 (002 design §4)."""

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
        raise CampoInvalido("tipo", "deve ser DEBITO ou CREDITO")


def valor_de(bruto: object) -> Decimal:
    """Quantia positiva com até duas casas, normalizada para duas casas.

    Aceita `Decimal`, `int` ou texto decimal; recusa `float`, cuja
    representação binária não garante as duas casas. O sinal não faz parte do
    valor: quem o define é o tipo (002 D-004).
    """
    if bruto is None:
        raise CampoObrigatorioAusente("valor")
    if isinstance(bruto, (bool, float)) or not isinstance(bruto, (Decimal, int, str)):
        raise CampoInvalido("valor", "deve ser número decimal exato")
    if isinstance(bruto, str):
        try:
            quantia = Decimal(bruto.strip())
        except InvalidOperation:
            raise CampoInvalido("valor", "deve ser número decimal") from None
    else:
        quantia = Decimal(bruto)
    if not quantia.is_finite():
        raise CampoInvalido("valor", "deve ser número decimal finito")
    if quantia <= 0:
        raise CampoInvalido("valor", "deve ser positivo")
    # Antes de quantizar: valor enorme estouraria a precisão do contexto.
    if quantia > VALOR_MAXIMO:
        raise CampoInvalido("valor", "excede o máximo permitido")
    if quantia != quantia.quantize(_QUANTUM, rounding=ROUND_DOWN):
        raise CampoInvalido(
            "valor", f"deve ter no máximo {CASAS_DECIMAIS} casas decimais"
        )
    return quantia.quantize(_QUANTUM)


def data_de(bruto: object, campo: str) -> date:
    """Dia do calendário de America/Sao_Paulo, sem conversão de fuso (D-003)."""
    if bruto is None:
        raise CampoObrigatorioAusente(campo)
    if isinstance(bruto, datetime):
        raise CampoInvalido(campo, "deve ser data sem hora (AAAA-MM-DD)")
    if isinstance(bruto, date):
        return bruto
    if isinstance(bruto, str) and _FORMATO_DATA.match(bruto):
        try:
            return date.fromisoformat(bruto)
        except ValueError:
            pass
    raise CampoInvalido(campo, "deve ser data válida no formato AAAA-MM-DD")


def uuid_de(bruto: object, campo: str) -> uuid.UUID:
    if bruto is None:
        raise CampoObrigatorioAusente(campo)
    if isinstance(bruto, uuid.UUID):
        return bruto
    if isinstance(bruto, str):
        try:
            return uuid.UUID(bruto)
        except ValueError:
            pass
    raise CampoInvalido(campo, "deve ser UUID")


def posicao_de(bruto: object) -> int:
    """Posição do lançamento no livro: inteiro positivo (002 TD-002)."""
    if bruto is None:
        raise CampoObrigatorioAusente("posicao")
    if isinstance(bruto, bool) or not isinstance(bruto, int):
        raise CampoInvalido("posicao", "deve ser inteiro")
    if bruto <= 0:
        raise CampoInvalido("posicao", "deve ser positiva")
    return bruto


def hash_canonico(conteudo: dict[str, object]) -> str:
    """SHA-256 do JSON canônico: chaves ordenadas, sem espaços."""
    canonico = json.dumps(
        conteudo, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonico.encode()).hexdigest()


@dataclass(frozen=True)
class Contribuicao:
    """Efeito de um lançamento do livro no saldo do dia (TD-001).

    A identidade é o `lancamento_id`: o mesmo lançamento pode chegar em
    eventos diferentes, por republicação (003 TD-004), e contribui uma única
    vez por geração.
    """

    lancamento_id: uuid.UUID
    posicao: int
    data_lancamento: date
    tipo: TipoLancamento
    valor: Decimal

    def __post_init__(self) -> None:
        # Revalida mesmo quando construída direto, sem passar por `de`.
        object.__setattr__(
            self, "lancamento_id", uuid_de(self.lancamento_id, "lancamento_id")
        )
        object.__setattr__(self, "posicao", posicao_de(self.posicao))
        object.__setattr__(
            self, "data_lancamento", data_de(self.data_lancamento, "data_lancamento")
        )
        object.__setattr__(self, "tipo", TipoLancamento.de(self.tipo))
        object.__setattr__(self, "valor", valor_de(self.valor))

    @classmethod
    def de(
        cls,
        lancamento_id: object,
        posicao: object,
        data_lancamento: object,
        tipo: object,
        valor: object,
    ) -> "Contribuicao":
        """Cria a contribuição a partir de dados brutos, validando cada campo."""
        return cls(
            lancamento_id=uuid_de(lancamento_id, "lancamento_id"),
            posicao=posicao_de(posicao),
            data_lancamento=data_de(data_lancamento, "data_lancamento"),
            tipo=TipoLancamento.de(tipo),
            valor=valor_de(valor),
        )

    @property
    def efeito_no_saldo(self) -> Decimal:
        """Valor com o sinal dado pelo tipo: crédito soma, débito subtrai."""
        if self.tipo is TipoLancamento.CREDITO:
            return self.valor
        return -self.valor

    @property
    def hash(self) -> str:
        """Hash do conteúdo do lançamento, sem o evento que o trouxe (TD-001).

        Usa os valores normalizados, para que "10.5" e "10.50" sejam o mesmo
        conteúdo. Mesmo lançamento com o mesmo hash é duplicata; com hash
        diferente, conflito (FR-003).
        """
        return hash_canonico(
            {
                "lancamento_id": str(self.lancamento_id),
                "posicao": self.posicao,
                "data_lancamento": self.data_lancamento.isoformat(),
                "tipo": self.tipo.value,
                "valor": f"{self.valor:f}",
            }
        )
