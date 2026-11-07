"""Erros de domínio do coordenador (005 FR-002, FR-006; INFRA 7.5).

A operação recusada não muda nenhuma geração; a mensagem cita só estados,
nunca dados da projeção.
"""


class ErroDominio(Exception):
    """Operação recusada por regra do ciclo de vida das gerações."""


class TransicaoInvalida(ErroDominio):
    """Transição fora do diagrama de estados de design 005 §3."""

    def __init__(self, origem: str, destino: str) -> None:
        super().__init__(f"transição de geração inválida: {origem} para {destino}")
        self.origem = origem
        self.destino = destino


class ReconstrucaoRecusada(ErroDominio):
    """Já existe geração `construindo` ou `anterior` (INFRA 7.5; D-005)."""

    def __init__(self, impedimentos: list[str]) -> None:
        super().__init__(
            "reconstrução recusada: já existe geração "
            + " e ".join(impedimentos)
            + "; cancelar ou descartar antes"
        )
        self.impedimentos = impedimentos


class SemReconstrucao(ErroDominio):
    """Não há geração `construindo` para a operação pedida."""

    def __init__(self) -> None:
        super().__init__("nenhuma reconstrução em andamento (sem geração construindo)")


class SemAnterior(ErroDominio):
    """Não há geração `anterior` para rollback ou descarte (D-005)."""

    def __init__(self) -> None:
        super().__init__("nenhuma geração anterior (sem troca a desfazer ou descartar)")


class TrocaRecusada(ErroDominio):
    """Candidata fora do critério de aceite no momento da barreira (FR-009)."""

    def __init__(self, pendencias: list[str]) -> None:
        super().__init__(
            "troca recusada pelo critério de aceite (FR-009): " + "; ".join(pendencias)
        )
        self.pendencias = pendencias
