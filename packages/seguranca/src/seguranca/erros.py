"""Erros de autenticação e autorização das APIs (002 design §4, §6).

Cada erro corresponde a um status HTTP do contrato: `TokenInvalido` a 401,
`EscopoInsuficiente` a 403 e `JwksIndisponivel` a 503 (TD-005: sem chave
válida, 503). As mensagens nunca contêm o token nem suas claims (INFRA 8.2).
"""

from enum import Enum


class MotivoFalhaJwt(Enum):
    """Lista fechada do rótulo `motivo` de `cashflow_jwt_falhas_total`."""

    AUSENTE = "ausente"
    MALFORMADO = "malformado"
    ALGORITMO = "algoritmo"
    KID_DESCONHECIDO = "kid_desconhecido"
    ASSINATURA = "assinatura"
    EXPIRADO = "expirado"
    NAO_VIGENTE = "nao_vigente"
    EMISSOR = "emissor"
    AUDIENCIA = "audiencia"
    ESCOPO = "escopo"
    JWKS_INDISPONIVEL = "jwks_indisponivel"


class ErroAutenticacao(Exception):
    """Base dos erros de validação do token."""

    def __init__(self, motivo: MotivoFalhaJwt) -> None:
        super().__init__(motivo.value)
        self.motivo = motivo


class TokenInvalido(ErroAutenticacao):
    """Token ausente, malformado, com assinatura, prazo, emissor ou audiência
    inválidos: 401."""


class EscopoInsuficiente(ErroAutenticacao):
    """Token válido sem o escopo exigido pela rota: 403."""

    def __init__(self) -> None:
        super().__init__(MotivoFalhaJwt.ESCOPO)


class JwksIndisponivel(ErroAutenticacao):
    """Sem chave válida em cache e sem conseguir renovar o JWKS: 503."""

    def __init__(self) -> None:
        super().__init__(MotivoFalhaJwt.JWKS_INDISPONIVEL)


class KidDesconhecido(Exception):
    """O JWKS vigente não tem a chave pedida pelo token (uso interno)."""
