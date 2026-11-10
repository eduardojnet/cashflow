"""Evento `LancamentoRegistrado` v1 lido pela projeção (002 design §4).

O domínio recebe o evento já decodificado em mapeamento; ler bytes e JSON da
mensagem é do adaptador RabbitMQ (004-T3). Evento que não respeita o contrato
gera `ErroDominio` e vai para a quarentena (FR-003).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from consolidado.dominio.contribuicao import Contribuicao, hash_canonico, uuid_de
from consolidado.dominio.erros import (
    CampoInvalido,
    CampoObrigatorioAusente,
    VersaoNaoSuportada,
)

VERSAO_EVENTO = 1


def _versao_de(bruto: object) -> int:
    if bruto is None:
        raise CampoObrigatorioAusente("versao")
    if isinstance(bruto, bool) or not isinstance(bruto, int) or bruto != VERSAO_EVENTO:
        raise VersaoNaoSuportada("versao", f"somente a versão {VERSAO_EVENTO}")
    return VERSAO_EVENTO


def _instante_de(bruto: object) -> datetime:
    """Instante com fuso, normalizado para UTC (ISO 8601)."""
    if bruto is None:
        raise CampoObrigatorioAusente("registrado_em")
    if isinstance(bruto, str):
        try:
            bruto = datetime.fromisoformat(bruto)
        except ValueError:
            raise CampoInvalido("registrado_em", "deve ser instante ISO 8601") from None
    if not isinstance(bruto, datetime):
        raise CampoInvalido("registrado_em", "deve ser instante ISO 8601")
    if bruto.utcoffset() is None:
        raise CampoInvalido("registrado_em", "deve informar o fuso")
    return bruto.astimezone(UTC)


@dataclass(frozen=True)
class EventoLancamentoRegistrado:
    """Evento do livro com a contribuição que ele carrega."""

    evento_id: UUID
    contribuicao: Contribuicao
    registrado_em: datetime

    @classmethod
    def de_dados(cls, dados: Mapping[str, object]) -> "EventoLancamentoRegistrado":
        """Valida o evento v1 decodificado; campo fora do contrato é ignorado."""
        _versao_de(dados.get("versao"))
        return cls(
            evento_id=uuid_de(dados.get("evento_id"), "evento_id"),
            contribuicao=Contribuicao.de(
                lancamento_id=dados.get("lancamento_id"),
                posicao=dados.get("posicao"),
                data_lancamento=dados.get("data_lancamento"),
                tipo=dados.get("tipo"),
                valor=dados.get("valor"),
            ),
            registrado_em=_instante_de(dados.get("registrado_em")),
        )

    @property
    def hash(self) -> str:
        """Hash do conteúdo do evento, guardado na inbox (TD-001).

        O mesmo `evento_id` com o mesmo hash é reentrega; com hash diferente,
        conflito (FR-003). Os campos entram normalizados: o mesmo instante com
        outro deslocamento de fuso é o mesmo conteúdo.
        """
        contribuicao = self.contribuicao
        return hash_canonico(
            {
                "evento_id": str(self.evento_id),
                "versao": VERSAO_EVENTO,
                "lancamento_id": str(contribuicao.lancamento_id),
                "posicao": contribuicao.posicao,
                "tipo": contribuicao.tipo.value,
                "valor": f"{contribuicao.valor:f}",
                "data_lancamento": contribuicao.data_lancamento.isoformat(),
                "registrado_em": self.registrado_em.isoformat(),
            }
        )
