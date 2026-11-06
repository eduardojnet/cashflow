"""Casos de uso da API do consolidado e sua porta de leitura (004-T5).

Três consultas, todas sobre a geração ativa (FR-007; INFRA 2.3):

- `ConsultarSaldo`: saldo de uma data, com total de créditos, total de
  débitos, saldo do dia, saldo acumulado até a data e checkpoint (FR-004;
  D-001, D-005). Data sem lançamentos consolidados não tem saldo: a
  resposta traz só o checkpoint, para o cliente avaliar a defasagem (D-006).
- `ListarSaldos`: um item por dia com lançamentos num intervalo fechado, em
  ordem de data, omitindo dias vazios, sem tamanho máximo de intervalo e em
  páginas (FR-005; D-002, D-007; TD-008). O cursor opaco codifica a última
  data entregue; a página seguinte começa estritamente depois dela.
- `ConsultarInclusao`: se um lançamento já está no consolidado (FR-006).

O saldo acumulado é calculado na leitura, nunca materializado (TD-007): um
lançamento retroativo muda o saldo de um dia passado e, com ele, o acumulado
de todos os dias seguintes, sem reescrita em cascata.

Limite da página (TD-008): padrão e máximo vêm da configuração; `limite`
fora de 1..máximo é rejeitado, e não ajustado em silêncio, como na listagem
de lançamentos (002-T7).
"""

import base64
import binascii
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from consolidado.dominio import ErroDominio
from consolidado.dominio.contribuicao import data_de, uuid_de

LIMITE_PADRAO = 50
LIMITE_MAXIMO = 500

_CENTAVO = Decimal("0.01")


class ConsultaInvalida(ErroDominio):
    """Parâmetro de consulta inválido: a consulta é rejeitada (422)."""


def _texto_monetario(valor: Decimal) -> str:
    # Texto com duas casas, como os valores do livro e do evento v1 (002
    # design §4): nada de float no caminho do dinheiro.
    return str(valor.quantize(_CENTAVO))


@dataclass(frozen=True)
class SaldoConsultado:
    """Saldo de um dia da geração ativa (D-001)."""

    data: date
    total_creditos: Decimal
    total_debitos: Decimal
    saldo_dia: Decimal
    saldo_acumulado: Decimal

    def como_dict(self) -> dict[str, object]:
        return {
            "data": self.data.isoformat(),
            "total_creditos": _texto_monetario(self.total_creditos),
            "total_debitos": _texto_monetario(self.total_debitos),
            "saldo_dia": _texto_monetario(self.saldo_dia),
            "saldo_acumulado": _texto_monetario(self.saldo_acumulado),
        }


@dataclass(frozen=True)
class SaldoDaData:
    """Resposta da consulta por data; `saldo` é `None` sem lançamentos."""

    geracao_id: int
    posicao_checkpoint: int
    saldo: SaldoConsultado | None

    def como_dict(self) -> dict[str, object]:
        if self.saldo is None:
            raise ValueError("data sem saldo não tem corpo de saldo (D-006)")
        return {**self.saldo.como_dict(), "posicao_checkpoint": self.posicao_checkpoint}


@dataclass(frozen=True)
class SaldosLidos:
    """O que a porta devolve para uma página do intervalo."""

    geracao_id: int
    posicao_checkpoint: int
    itens: list[SaldoConsultado]


@dataclass(frozen=True)
class CursorSaldos:
    """Última data entregue (TD-008)."""

    data: date

    def codificar(self) -> str:
        bruto = self.data.isoformat().encode()
        return base64.urlsafe_b64encode(bruto).decode().rstrip("=")

    @classmethod
    def decodificar(cls, texto: str) -> "CursorSaldos":
        # O formato interno não é contrato: qualquer falha vira o mesmo erro,
        # sem repetir o texto recebido.
        try:
            preenchido = texto + "=" * (-len(texto) % 4)
            bruto = base64.urlsafe_b64decode(preenchido.encode()).decode()
            return cls(data_de(bruto, "cursor"))
        except (ValueError, binascii.Error, ErroDominio):
            raise ConsultaInvalida("cursor", "cursor inválido") from None


@dataclass(frozen=True)
class PaginaSaldos:
    geracao_id: int
    posicao_checkpoint: int
    itens: list[SaldoConsultado]
    proximo_cursor: CursorSaldos | None

    def como_dict(self) -> dict[str, object]:
        return {
            "itens": [item.como_dict() for item in self.itens],
            "posicao_checkpoint": self.posicao_checkpoint,
            "proximo_cursor": (
                None if self.proximo_cursor is None else self.proximo_cursor.codificar()
            ),
        }


@dataclass(frozen=True)
class Inclusao:
    """Sinal de inclusão de um lançamento na geração ativa (FR-006)."""

    geracao_id: int
    incluido: bool
    posicao: int | None

    def como_dict(self) -> dict[str, object]:
        return {"incluido": self.incluido, "posicao": self.posicao}


class LeituraConsolidado(Protocol):
    """Leitura da geração ativa; cada método lê um único instante da projeção,
    para que geração, checkpoint e saldos não venham de momentos diferentes
    (troca de geração da spec 005)."""

    async def saldo_da_data(self, data: date) -> SaldoDaData: ...

    async def saldos(
        self, inicio: date, fim: date, apos: date | None, quantidade: int
    ) -> SaldosLidos:
        """Até `quantidade` dias com saldo em [inicio, fim], depois de `apos`,
        em ordem de data, com o acumulado de cada um."""
        ...

    async def inclusao(self, lancamento_id: UUID) -> Inclusao: ...


def _data(campo: str, bruta: object) -> date:
    if bruta is None or bruta == "":
        raise ConsultaInvalida(campo, "campo obrigatório ausente")
    try:
        return data_de(bruta, campo)
    except ErroDominio as erro:
        raise ConsultaInvalida(campo, erro.motivo) from None


class ConsultarSaldo:
    """Saldo de uma data (FR-004)."""

    def __init__(self, leitura: LeituraConsolidado) -> None:
        self._leitura = leitura

    async def executar(self, data: object) -> SaldoDaData:
        return await self._leitura.saldo_da_data(_data("data", data))


class ListarSaldos:
    """Saldos de um intervalo, em páginas (FR-005)."""

    def __init__(
        self,
        leitura: LeituraConsolidado,
        limite_padrao: int = LIMITE_PADRAO,
        limite_maximo: int = LIMITE_MAXIMO,
    ) -> None:
        if not 1 <= limite_padrao <= limite_maximo:
            raise ValueError("exige 1 <= limite_padrao <= limite_maximo")
        self._leitura = leitura
        self.limite_padrao = limite_padrao
        self.limite_maximo = limite_maximo

    def _limite(self, bruto: object) -> int:
        if bruto is None or bruto == "":
            return self.limite_padrao
        if isinstance(bruto, str) and bruto.isdecimal():
            limite = int(bruto)
        elif isinstance(bruto, int) and not isinstance(bruto, bool):
            limite = bruto
        else:
            raise ConsultaInvalida("limite", "deve ser inteiro")
        if not 1 <= limite <= self.limite_maximo:
            raise ConsultaInvalida(
                "limite", f"deve estar entre 1 e {self.limite_maximo}"
            )
        return limite

    async def executar(
        self,
        data_inicio: object,
        data_fim: object,
        limite: object = None,
        cursor: object = None,
    ) -> PaginaSaldos:
        inicio = _data("data_inicio", data_inicio)
        fim = _data("data_fim", data_fim)
        if inicio > fim:
            raise ConsultaInvalida("data_inicio", "deve ser menor ou igual a data_fim")
        quantidade = self._limite(limite)
        apos: date | None = None
        if cursor is not None and cursor != "":
            if not isinstance(cursor, str):
                raise ConsultaInvalida("cursor", "cursor inválido")
            apos = CursorSaldos.decodificar(cursor).data
        # Um item a mais diz se há página seguinte sem segunda consulta.
        lidos = await self._leitura.saldos(inicio, fim, apos, quantidade + 1)
        itens = lidos.itens[:quantidade]
        proximo = (
            CursorSaldos(itens[-1].data) if len(lidos.itens) > quantidade else None
        )
        return PaginaSaldos(lidos.geracao_id, lidos.posicao_checkpoint, itens, proximo)


class ConsultarInclusao:
    """Sinal de inclusão de um lançamento (FR-006)."""

    def __init__(self, leitura: LeituraConsolidado) -> None:
        self._leitura = leitura

    async def executar(self, lancamento_id: object) -> Inclusao:
        try:
            identificador = uuid_de(lancamento_id, "lancamento_id")
        except ErroDominio as erro:
            raise ConsultaInvalida("lancamento_id", erro.motivo) from None
        return await self._leitura.inclusao(identificador)
