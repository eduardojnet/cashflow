"""Política de retentativa da publicação (003-T3; D-002, TD-003).

Tentativas sem limite, com espera exponencial a partir de `base` até `teto`:
a n-ésima falha seguida de um evento espera `base * 2**(n-1)`, nunca mais que
`teto`. Não há descarte: o evento continua na outbox, e a falha persistente
aparece pela idade do evento mais antigo (FR-006; métricas em 003-T5).
"""

from dataclasses import dataclass
from datetime import timedelta

# A partir daqui `base * 2**n` já passa de qualquer teto representável em
# timedelta; limitar o expoente evita estouro de float e de timedelta.
_EXPOENTE_MAXIMO = 64


@dataclass(frozen=True)
class PoliticaRetentativa:
    base: timedelta
    teto: timedelta

    def __post_init__(self) -> None:
        if self.base <= timedelta(0):
            raise ValueError("espera base deve ser positiva")
        if self.teto < self.base:
            raise ValueError("teto da espera deve ser maior ou igual à base")

    def espera(self, tentativas: int) -> timedelta:
        """Espera até a próxima tentativa de quem já falhou `tentativas` vezes.

        `tentativas` é o contador da outbox antes desta falha: 0 na primeira.
        """
        if tentativas < 0:
            raise ValueError("tentativas não pode ser negativo")
        segundos = self.base.total_seconds() * 2.0 ** min(tentativas, _EXPOENTE_MAXIMO)
        if segundos >= self.teto.total_seconds():
            return self.teto
        return timedelta(seconds=segundos)
