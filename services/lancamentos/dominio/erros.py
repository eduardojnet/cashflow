"""Erros de domínio do lançamento (002 FR-002).

Cada erro informa o campo e o motivo da rejeição, para o adaptador HTTP
devolver ao cliente. A mensagem descreve a regra violada e nunca repete o
valor recebido, para que nada financeiro chegue a log (INFRA 8.2).
"""


class ErroDominio(ValueError):
    """Requisição rejeitada por regra do domínio, sem registro."""

    def __init__(self, campo: str, motivo: str) -> None:
        super().__init__(f"{campo}: {motivo}")
        self.campo = campo
        self.motivo = motivo


class CampoObrigatorioAusente(ErroDominio):
    def __init__(self, campo: str) -> None:
        super().__init__(campo, "campo obrigatório ausente")


class TipoInvalido(ErroDominio):
    pass


class ValorInvalido(ErroDominio):
    pass


class DataInvalida(ErroDominio):
    pass


class DescricaoInvalida(ErroDominio):
    pass
