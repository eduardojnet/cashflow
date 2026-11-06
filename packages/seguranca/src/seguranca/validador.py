"""Validação local de access token JWT (002 design TD-005, §6; INFRA 5.5).

Verifica, nesta ordem: formato, algoritmo permitido, chave pelo `kid` no
JWKS em cache, assinatura, expiração, vigência, emissor, audiência e escopo.
Cada falha incrementa `cashflow_jwt_falhas_total{motivo}` com um valor da
lista fechada `MotivoFalhaJwt` (INFRA 8.3); o token nunca vai para rótulo,
mensagem ou log (INFRA 8.2).

A validação é síncrona: o JWKS é lido com urllib sob uma trava. Em FastAPI,
uma dependência síncrona roda no threadpool e não bloqueia o laço de eventos.
"""

import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import jwt
from observabilidade import Metricas

from seguranca.erros import (
    ErroAutenticacao,
    EscopoInsuficiente,
    KidDesconhecido,
    MotivoFalhaJwt,
    TokenInvalido,
)
from seguranca.jwks import CacheJwks, fonte_https

# TTL padrão do cache de JWKS quando a configuração não informa (TD-005: TTL
# configurável). Cinco minutos equivalem ao tempo de vida padrão do access
# token no Keycloak.
TTL_JWKS_PADRAO_SEGUNDOS = 300.0


@dataclass(frozen=True)
class Principal:
    """Cliente técnico autenticado (Client Credentials, INFRA 5.2)."""

    cliente_id: str
    escopos: frozenset[str]


class ValidadorToken(Protocol):
    def validar(self, token: str, escopo_exigido: str) -> Principal: ...


@dataclass(frozen=True)
class ConfiguracaoJwt:
    emissor: str
    audiencia: str
    url_jwks: str
    arquivo_ac: str
    ttl_jwks_segundos: float = TTL_JWKS_PADRAO_SEGUNDOS
    intervalo_minimo_renovacao_segundos: float = 10.0
    timeout_jwks_segundos: float = 5.0
    folga_segundos: float = 0.0
    algoritmos: tuple[str, ...] = ("RS256",)

    @classmethod
    def de_ambiente(
        cls, ambiente: Mapping[str, str], audiencia_padrao: str | None = None
    ) -> "ConfiguracaoJwt":
        """Lê `JWT_*` do ambiente; ausência de obrigatória é erro explícito."""

        def obrigatoria(nome: str, padrao: str | None = None) -> str:
            valor = ambiente.get(nome, padrao)
            if not valor:
                raise ValueError(f"defina {nome}")
            return valor

        return cls(
            emissor=obrigatoria("JWT_EMISSOR"),
            audiencia=obrigatoria("JWT_AUDIENCIA", audiencia_padrao),
            url_jwks=obrigatoria("JWT_JWKS_URL"),
            arquivo_ac=obrigatoria("JWT_AC_ARQUIVO"),
            ttl_jwks_segundos=float(
                ambiente.get("JWT_JWKS_TTL_SEGUNDOS", TTL_JWKS_PADRAO_SEGUNDOS)
            ),
        )


def _escopos(claims: Mapping[str, Any]) -> frozenset[str]:
    escopo = claims.get("scope", "")
    return frozenset(escopo.split()) if isinstance(escopo, str) else frozenset()


# Exceções do PyJWT mapeadas para o motivo da métrica; as subclasses vêm
# antes de `InvalidTokenError`, que cobre o restante como malformado.
_MOTIVOS_PYJWT: tuple[tuple[type[jwt.PyJWTError], MotivoFalhaJwt], ...] = (
    (jwt.ExpiredSignatureError, MotivoFalhaJwt.EXPIRADO),
    (jwt.ImmatureSignatureError, MotivoFalhaJwt.NAO_VIGENTE),
    (jwt.InvalidSignatureError, MotivoFalhaJwt.ASSINATURA),
    (jwt.InvalidIssuerError, MotivoFalhaJwt.EMISSOR),
    (jwt.InvalidAudienceError, MotivoFalhaJwt.AUDIENCIA),
    (jwt.InvalidAlgorithmError, MotivoFalhaJwt.ALGORITMO),
)


class ValidadorJwt:
    def __init__(
        self, configuracao: ConfiguracaoJwt, cache: CacheJwks, metricas: Metricas
    ) -> None:
        self._configuracao = configuracao
        self._cache = cache
        self._falhas = metricas.contador("cashflow_jwt_falhas_total")
        # Séries em zero desde o início, para alertas por taxa (spec 007).
        for motivo in MotivoFalhaJwt:
            self._falhas.labels(motivo=motivo.value)

    @property
    def cache(self) -> CacheJwks:
        return self._cache

    def validar(self, token: str, escopo_exigido: str) -> Principal:
        try:
            return self._validar(token, escopo_exigido)
        except ErroAutenticacao as erro:
            self._falhas.labels(motivo=erro.motivo.value).inc()
            raise

    def _validar(self, token: str, escopo_exigido: str) -> Principal:
        if not token:
            raise TokenInvalido(MotivoFalhaJwt.AUSENTE)
        try:
            cabecalho = jwt.get_unverified_header(token)
        except jwt.PyJWTError as erro:
            raise TokenInvalido(MotivoFalhaJwt.MALFORMADO) from erro
        algoritmo = cabecalho.get("alg")
        if algoritmo not in self._configuracao.algoritmos:
            raise TokenInvalido(MotivoFalhaJwt.ALGORITMO)
        kid = cabecalho.get("kid")
        if not isinstance(kid, str):
            raise TokenInvalido(MotivoFalhaJwt.MALFORMADO)
        try:
            chave = self._cache.chave(kid)
        except KidDesconhecido as erro:
            raise TokenInvalido(MotivoFalhaJwt.KID_DESCONHECIDO) from erro
        claims = self._decodificar(token, chave)

        cliente_id = claims.get("azp")
        if not isinstance(cliente_id, str) or not cliente_id:
            raise TokenInvalido(MotivoFalhaJwt.MALFORMADO)
        escopos = _escopos(claims)
        if escopo_exigido not in escopos:
            raise EscopoInsuficiente()
        return Principal(cliente_id=cliente_id, escopos=escopos)

    def _decodificar(self, token: str, chave: jwt.PyJWK) -> dict[str, Any]:
        configuracao = self._configuracao
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                chave,
                algorithms=list(configuracao.algoritmos),
                audience=configuracao.audiencia,
                issuer=configuracao.emissor,
                leeway=configuracao.folga_segundos,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
            return claims
        except jwt.PyJWTError as erro:
            for tipo, motivo in _MOTIVOS_PYJWT:
                if isinstance(erro, tipo):
                    raise TokenInvalido(motivo) from erro
            raise TokenInvalido(MotivoFalhaJwt.MALFORMADO) from erro


def criar_validador(
    configuracao: ConfiguracaoJwt,
    metricas: Metricas,
    relogio: Callable[[], float] | None = None,
) -> ValidadorJwt:
    """Validador com JWKS lido por HTTPS e certificado conferido pela AC local
    (INFRA 5.7, 5.8)."""
    contexto = ssl.create_default_context(cafile=configuracao.arquivo_ac)
    fonte = fonte_https(
        configuracao.url_jwks, contexto, configuracao.timeout_jwks_segundos
    )
    extras = {} if relogio is None else {"relogio": relogio}
    cache = CacheJwks(
        fonte,
        configuracao.ttl_jwks_segundos,
        metricas,
        intervalo_minimo_segundos=configuracao.intervalo_minimo_renovacao_segundos,
        **extras,
    )
    return ValidadorJwt(configuracao, cache, metricas)
