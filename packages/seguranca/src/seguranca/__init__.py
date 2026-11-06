"""Validação JWT com cache de JWKS compartilhada pelas APIs (002 TD-005)."""

from seguranca.erros import (
    ErroAutenticacao,
    EscopoInsuficiente,
    JwksIndisponivel,
    MotivoFalhaJwt,
    TokenInvalido,
)
from seguranca.jwks import CacheJwks, ErroObtencaoJwks, FonteJwks, fonte_https
from seguranca.validador import (
    TTL_JWKS_PADRAO_SEGUNDOS,
    ConfiguracaoJwt,
    Principal,
    ValidadorJwt,
    ValidadorToken,
    criar_validador,
)

__all__ = [
    "TTL_JWKS_PADRAO_SEGUNDOS",
    "CacheJwks",
    "ConfiguracaoJwt",
    "ErroAutenticacao",
    "ErroObtencaoJwks",
    "EscopoInsuficiente",
    "FonteJwks",
    "JwksIndisponivel",
    "MotivoFalhaJwt",
    "Principal",
    "TokenInvalido",
    "ValidadorJwt",
    "ValidadorToken",
    "criar_validador",
    "fonte_https",
]
