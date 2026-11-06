"""Erros de domínio da projeção (004 FR-001, FR-003).

Evento que viola uma regra é ilegível para a projeção: o worker o guarda na
quarentena sem aplicá-lo (004-T3). A mensagem descreve a regra violada e
nunca repete o valor recebido, para que nada financeiro chegue a log
(INFRA 8.2).
"""


class ErroDominio(ValueError):
    """Dado rejeitado por regra do domínio da projeção."""

    def __init__(self, campo: str, motivo: str) -> None:
        super().__init__(f"{campo}: {motivo}")
        self.campo = campo
        self.motivo = motivo


class CampoObrigatorioAusente(ErroDominio):
    def __init__(self, campo: str) -> None:
        super().__init__(campo, "campo obrigatório ausente")


class CampoInvalido(ErroDominio):
    pass


class VersaoNaoSuportada(ErroDominio):
    pass


class DataDivergente(ErroDominio):
    """Contribuição aplicada ao saldo de outro dia (FR-001)."""
