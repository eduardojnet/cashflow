"""Caso de uso `ListarLancamentos` e sua porta de saída (002-T7).

Lista os lançamentos cuja data está num intervalo fechado, em páginas
ordenadas por (`data_lancamento`, `posicao`), com cursor opaco que codifica
esse par do último item entregue (FR-009, D-002, TD-006). A página seguinte
começa estritamente depois do cursor: inserção retroativa durante a
paginação não desloca as páginas já lidas, então nada se repete nem some
entre as que já existiam no início.

O intervalo não é filtrado pelo cliente que registrou: o comerciante é um
só e o cliente de consultas é outro cliente técnico (D-002; INFRA - Fora do
escopo, multiempresa).

Limite da página (design 002 §4): padrão e máximo vêm da configuração;
`limite` fora de 1..máximo é rejeitado, e não ajustado em silêncio, para o
cliente saber quantos itens pediu.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from lancamentos.dominio import ErroDominio, Lancamento, data_do_lancamento

LIMITE_PADRAO = 50
LIMITE_MAXIMO = 500


class ConsultaInvalida(ErroDominio):
    """Parâmetro de listagem inválido: a consulta é rejeitada (422)."""


@dataclass(frozen=True, order=True)
class Cursor:
    """Chave de ordenação do último item entregue (TD-006)."""

    data_lancamento: date
    posicao: int

    def codificar(self) -> str:
        bruto = json.dumps(
            [self.data_lancamento.isoformat(), self.posicao], separators=(",", ":")
        )
        return base64.urlsafe_b64encode(bruto.encode()).decode().rstrip("=")

    @classmethod
    def decodificar(cls, texto: str) -> "Cursor":
        # O formato interno não é contrato: qualquer falha vira o mesmo erro,
        # sem repetir o texto recebido.
        try:
            preenchido = texto + "=" * (-len(texto) % 4)
            dados = json.loads(base64.urlsafe_b64decode(preenchido.encode()))
            data_texto, posicao = dados
            data_cursor = data_do_lancamento(data_texto)
        except (ValueError, TypeError, binascii.Error, ErroDominio):
            raise ConsultaInvalida("cursor", "cursor inválido") from None
        if type(posicao) is not int or posicao <= 0:
            raise ConsultaInvalida("cursor", "cursor inválido")
        return cls(data_cursor, posicao)


@dataclass(frozen=True)
class LancamentoListado:
    """Lançamento como gravado no livro, com posição e instante de registro."""

    lancamento: Lancamento
    posicao: int
    registrado_em: datetime

    @property
    def cursor(self) -> Cursor:
        return Cursor(self.lancamento.data_lancamento, self.posicao)

    def como_dict(self) -> dict[str, object]:
        # Valor em texto com duas casas, como no evento v1 (design 002 §4).
        lancamento = self.lancamento
        return {
            "id": str(lancamento.id),
            "posicao": self.posicao,
            "tipo": lancamento.tipo.value,
            "valor": str(lancamento.valor),
            "data_lancamento": lancamento.data_lancamento.isoformat(),
            "descricao": lancamento.descricao,
            "registrado_em": self.registrado_em.isoformat(),
        }


@dataclass(frozen=True)
class PaginaLancamentos:
    itens: list[LancamentoListado]
    proximo_cursor: Cursor | None

    def como_dict(self) -> dict[str, object]:
        return {
            "itens": [item.como_dict() for item in self.itens],
            "proximo_cursor": (
                None if self.proximo_cursor is None else self.proximo_cursor.codificar()
            ),
        }


class ConsultaLivro(Protocol):
    """Leitura do livro por intervalo, em ordem de (data, posição)."""

    async def listar(
        self,
        data_inicio: date,
        data_fim: date,
        apos: Cursor | None,
        quantidade: int,
    ) -> list[LancamentoListado]: ...


def _data(campo: str, bruta: object) -> date:
    if bruta is None or bruta == "":
        raise ConsultaInvalida(campo, "campo obrigatório ausente")
    try:
        return data_do_lancamento(bruta)
    except ErroDominio as erro:
        raise ConsultaInvalida(campo, erro.motivo) from None


class ListarLancamentos:
    """Orquestra a listagem paginada por intervalo (FR-009)."""

    def __init__(
        self,
        consulta: ConsultaLivro,
        limite_padrao: int = LIMITE_PADRAO,
        limite_maximo: int = LIMITE_MAXIMO,
    ) -> None:
        if not 1 <= limite_padrao <= limite_maximo:
            raise ValueError("exige 1 <= limite_padrao <= limite_maximo")
        self._consulta = consulta
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
    ) -> PaginaLancamentos:
        inicio = _data("data_inicio", data_inicio)
        fim = _data("data_fim", data_fim)
        if inicio > fim:
            raise ConsultaInvalida("data_inicio", "deve ser menor ou igual a data_fim")
        quantidade = self._limite(limite)
        apos: Cursor | None = None
        if cursor is not None and cursor != "":
            if not isinstance(cursor, str):
                raise ConsultaInvalida("cursor", "cursor inválido")
            apos = Cursor.decodificar(cursor)
        # Um item a mais diz se há página seguinte sem segunda consulta.
        linhas = await self._consulta.listar(inicio, fim, apos, quantidade + 1)
        itens = linhas[:quantidade]
        proximo = itens[-1].cursor if len(linhas) > quantidade else None
        return PaginaLancamentos(itens, proximo)
