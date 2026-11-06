"""Domínio de projeção: contribuição, saldo do dia e deduplicação (004-T2)."""

from consolidado.dominio.contribuicao import (
    CASAS_DECIMAIS,
    VALOR_MAXIMO,
    Contribuicao,
    TipoLancamento,
)
from consolidado.dominio.erros import (
    CampoInvalido,
    CampoObrigatorioAusente,
    DataDivergente,
    ErroDominio,
    VersaoNaoSuportada,
)
from consolidado.dominio.evento import VERSAO_EVENTO, EventoLancamentoRegistrado
from consolidado.dominio.saldo_diario import Recebimento, SaldoDiario, classificar

__all__ = [
    "CASAS_DECIMAIS",
    "VALOR_MAXIMO",
    "VERSAO_EVENTO",
    "CampoInvalido",
    "CampoObrigatorioAusente",
    "Contribuicao",
    "DataDivergente",
    "ErroDominio",
    "EventoLancamentoRegistrado",
    "Recebimento",
    "SaldoDiario",
    "TipoLancamento",
    "VersaoNaoSuportada",
    "classificar",
]
