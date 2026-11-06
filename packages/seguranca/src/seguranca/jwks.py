"""Cache de JWKS com TTL configurável (002 design TD-005; INFRA 5.6).

Regras:
- Dentro do TTL, as chaves em cache são usadas sem consultar o Keycloak:
  com o Keycloak parado, tokens válidos continuam aceitos até o TTL vencer
  (design 002 §8).
- TTL vencido: a próxima validação renova o JWKS. Se a renovação falhar, não
  há chave válida e a validação devolve `JwksIndisponivel` (503).
- `kid` desconhecido dentro do TTL força uma renovação, para aceitar rotação
  de chave no Keycloak. Se a renovação falhar, `JwksIndisponivel` (design 002
  §5); se o `kid` continuar ausente, o token é inválido.
- Renovações seguidas ficam limitadas por `intervalo_minimo_segundos`, para
  que tokens com `kid` inventado ou um Keycloak fora do ar não gerem uma
  consulta por requisição.

Toda falha de obtenção ou de leitura do JWKS incrementa
`cashflow_jwks_falhas_total` (INFRA 8.3).
"""

import json
import ssl
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

import jwt
from observabilidade import Metricas

from seguranca.erros import JwksIndisponivel, KidDesconhecido

FonteJwks = Callable[[], Mapping[str, Any]]


class ErroObtencaoJwks(Exception):
    """JWKS inacessível ou sem nenhuma chave de assinatura utilizável."""


def fonte_https(
    url: str, contexto_tls: ssl.SSLContext, timeout_segundos: float
) -> FonteJwks:
    """Fonte que lê o JWKS por HTTPS validando o certificado pela AC dada."""
    if not url.startswith("https://"):
        raise ValueError("o JWKS só é obtido por TLS (INFRA 5.7)")

    def obter() -> Mapping[str, Any]:
        try:
            with urllib.request.urlopen(
                url, context=contexto_tls, timeout=timeout_segundos
            ) as resposta:
                documento: Mapping[str, Any] = json.load(resposta)
                return documento
        except (OSError, ValueError) as erro:
            raise ErroObtencaoJwks(type(erro).__name__) from erro

    return obter


def _chaves_de_assinatura(documento: Mapping[str, Any]) -> dict[str, jwt.PyJWK]:
    # O Keycloak publica também a chave de cifragem (use=enc); só entram as
    # chaves de assinatura com `kid`.
    chaves: dict[str, jwt.PyJWK] = {}
    entradas = documento.get("keys")
    if not isinstance(entradas, list):
        raise ErroObtencaoJwks("JWKS sem lista de chaves")
    for entrada in entradas:
        if not isinstance(entrada, dict):
            continue
        kid = entrada.get("kid")
        if not isinstance(kid, str) or entrada.get("use", "sig") != "sig":
            continue
        try:
            chaves[kid] = jwt.PyJWK(entrada)
        except jwt.PyJWTError:
            continue
    if not chaves:
        raise ErroObtencaoJwks("JWKS sem chave de assinatura utilizável")
    return chaves


class CacheJwks:
    def __init__(
        self,
        fonte: FonteJwks,
        ttl_segundos: float,
        metricas: Metricas,
        intervalo_minimo_segundos: float = 10.0,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_segundos <= 0:
            raise ValueError("o TTL do JWKS precisa ser positivo")
        self._fonte = fonte
        self._ttl = ttl_segundos
        self._intervalo_minimo = intervalo_minimo_segundos
        self._relogio = relogio
        self._falhas = metricas.contador("cashflow_jwks_falhas_total")
        self._trava = threading.Lock()
        self._chaves: dict[str, jwt.PyJWK] = {}
        self._obtido_em: float | None = None
        self._ultima_tentativa: float | None = None
        self._ultima_falhou = False

    def chave(self, kid: str) -> jwt.PyJWK:
        """Chave do `kid`; renova o JWKS quando necessário (ver módulo)."""
        with self._trava:
            agora = self._relogio()
            valido = self._dentro_do_ttl(agora)
            if valido and kid in self._chaves:
                return self._chaves[kid]
            if self._renovacao_recente(agora):
                # Renovação limitada: sem chave válida é 503; com cache válido,
                # o kid simplesmente não existe.
                if not valido or self._ultima_falhou:
                    raise JwksIndisponivel()
                raise KidDesconhecido()
            self._renovar(agora)
            if kid in self._chaves:
                return self._chaves[kid]
            raise KidDesconhecido()

    def pronto(self) -> bool:
        """Há chaves válidas, renovando se o TTL venceu (readiness, INFRA 8.5)."""
        with self._trava:
            agora = self._relogio()
            if self._dentro_do_ttl(agora):
                return True
            if self._renovacao_recente(agora):
                return False
            try:
                self._renovar(agora)
            except JwksIndisponivel:
                return False
            return True

    def _dentro_do_ttl(self, agora: float) -> bool:
        return self._obtido_em is not None and agora - self._obtido_em < self._ttl

    def _renovacao_recente(self, agora: float) -> bool:
        return (
            self._ultima_tentativa is not None
            and agora - self._ultima_tentativa < self._intervalo_minimo
        )

    def _renovar(self, agora: float) -> None:
        self._ultima_tentativa = agora
        try:
            chaves = _chaves_de_assinatura(self._fonte())
        except ErroObtencaoJwks as erro:
            self._ultima_falhou = True
            self._falhas.inc()
            raise JwksIndisponivel() from erro
        self._ultima_falhou = False
        self._chaves = chaves
        self._obtido_em = agora
