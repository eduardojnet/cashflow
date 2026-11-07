"""Critério de aceite da candidata (005 FR-009; D-006).

A candidata só pode virar a ativa sem lacunas e com checkpoint maior ou
igual ao da ativa. A troca (005-T4) aplica o critério com os consumidores em
pausa, no momento da barreira; o `status` da reconstrução (005-T3) mostra o
mesmo critério sobre a leitura corrente, só para acompanhamento.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Progresso:
    """Checkpoint (maior posição contínua) e lacunas abertas de uma geração."""

    checkpoint: int
    lacunas: int


def pendencias_de_troca(candidata: Progresso, ativa: Progresso) -> list[str]:
    """O que impede a troca pelo FR-009; lista vazia = critério atendido."""
    pendencias: list[str] = []
    if candidata.lacunas > 0:
        pendencias.append(f"candidata com {candidata.lacunas} lacunas")
    if candidata.checkpoint < ativa.checkpoint:
        pendencias.append(
            f"checkpoint da candidata ({candidata.checkpoint})"
            f" abaixo do da ativa ({ativa.checkpoint})"
        )
    return pendencias
