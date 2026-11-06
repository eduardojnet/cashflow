"""Aplicação do consolidado: casos de uso e portas (004-T3, T4)."""

from consolidado.aplicacao.projetar_lancamento import (
    MOTIVO_CONFLITO_EVENTO,
    MOTIVO_CONFLITO_LANCAMENTO,
    MOTIVO_CONFLITO_POSICAO,
    MOTIVO_JSON_INVALIDO,
    ContribuicaoExistente,
    Desfecho,
    EstadoCheckpoint,
    MensagemEvento,
    ObservadorCheckpoint,
    ProjetarLancamento,
    TransacaoProjecao,
    UnidadeDeTrabalhoProjecao,
)

__all__ = [
    "MOTIVO_CONFLITO_EVENTO",
    "MOTIVO_CONFLITO_LANCAMENTO",
    "MOTIVO_CONFLITO_POSICAO",
    "MOTIVO_JSON_INVALIDO",
    "ContribuicaoExistente",
    "Desfecho",
    "EstadoCheckpoint",
    "MensagemEvento",
    "ObservadorCheckpoint",
    "ProjetarLancamento",
    "TransacaoProjecao",
    "UnidadeDeTrabalhoProjecao",
]
