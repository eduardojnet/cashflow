"""Validação JWT da API de lançamentos contra o Keycloak real (002-T4; INFRA
9.3, 9.6).

Tokens emitidos pelo Keycloak do compose por Client Credentials são
validados pelo adaptador da API (`lancamentos-api`), com JWKS lido por TLS e
certificado conferido pela AC local. O último teste para o Keycloak com
`docker compose stop` (ST-Q-010), confirma que o token continua aceito dentro
do TTL e recusado com 503 depois dele, e religa o Keycloak ao fim.

Com o ambiente no ar, a partir da raiz do repositório:

    KEYCLOAK_URL=https://localhost:8443 uv run --env-file infra/.env \\
      pytest services/lancamentos/tests/integracao/test_validacao_jwt_keycloak.py

Sem `KEYCLOAK_URL`, o módulo é pulado com o motivo explícito.
"""

import json
import os
import shutil
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from lancamentos.adaptadores.seguranca import (
    ESCOPO_CONSULTAR,
    ESCOPO_REGISTRAR,
    criar_validador_lancamentos,
)
from observabilidade import Metricas
from seguranca import (
    EscopoInsuficiente,
    JwksIndisponivel,
    MotivoFalhaJwt,
    TokenInvalido,
    ValidadorJwt,
)

URL = os.environ.get("KEYCLOAK_URL")
RAIZ = Path(__file__).resolve().parents[4]
COMPOSE = RAIZ / "infra/compose.yaml"
AC = RAIZ / "infra/tls/gerados/ac/ac-cashflow.crt"
REALM = "cashflow"
EMISSOR = f"https://localhost:8443/realms/{REALM}"
TTL = 300.0

pytestmark = pytest.mark.skipif(
    URL is None,
    reason="KEYCLOAK_URL ausente: executar com o ambiente no ar (ver docstring)",
)


@dataclass
class Relogio:
    agora: float = 0.0

    def __call__(self) -> float:
        return self.agora


@dataclass
class Ambiente:
    metricas: Metricas
    relogio: Relogio
    validador: ValidadorJwt

    def falhas_jwt(self, motivo: MotivoFalhaJwt) -> float:
        valor = self.metricas.registro.get_sample_value(
            "cashflow_jwt_falhas_total", {"motivo": motivo.value}
        )
        return valor or 0.0

    def falhas_jwks(self) -> float:
        valor = self.metricas.registro.get_sample_value("cashflow_jwks_falhas_total")
        return valor or 0.0


def _token(cliente: str) -> str:
    variavel = {
        "cliente-lancamentos": "KEYCLOAK_SEGREDO_CLIENTE_LANCAMENTOS",
        "cliente-consultas": "KEYCLOAK_SEGREDO_CLIENTE_CONSULTAS",
    }[cliente]
    campos = {
        "grant_type": "client_credentials",
        "client_id": cliente,
        "client_secret": os.environ[variavel],
    }
    requisicao = urllib.request.Request(
        f"{URL}/realms/{REALM}/protocol/openid-connect/token",
        data=urllib.parse.urlencode(campos).encode(),
        method="POST",
    )
    contexto = ssl.create_default_context(cafile=str(AC))
    with urllib.request.urlopen(requisicao, context=contexto, timeout=10) as resposta:
        token: str = json.load(resposta)["access_token"]
        return token


@pytest.fixture
def amb() -> Ambiente:
    metricas = Metricas()
    relogio = Relogio()
    ambiente = {
        "JWT_EMISSOR": EMISSOR,
        "JWT_JWKS_URL": f"{URL}/realms/{REALM}/protocol/openid-connect/certs",
        "JWT_AC_ARQUIVO": str(AC),
        "JWT_JWKS_TTL_SEGUNDOS": str(TTL),
    }
    validador = criar_validador_lancamentos(metricas, ambiente, relogio)
    return Ambiente(metricas, relogio, validador)


def test_token_de_registro_do_keycloak_e_aceito(amb: Ambiente) -> None:
    principal = amb.validador.validar(_token("cliente-lancamentos"), ESCOPO_REGISTRAR)

    assert principal.cliente_id == "cliente-lancamentos"
    assert ESCOPO_REGISTRAR in principal.escopos


def test_token_de_consulta_do_keycloak_e_aceito_na_listagem(amb: Ambiente) -> None:
    principal = amb.validador.validar(_token("cliente-consultas"), ESCOPO_CONSULTAR)

    assert principal.cliente_id == "cliente-consultas"


@pytest.mark.parametrize(
    ("cliente", "escopo"),
    [
        ("cliente-lancamentos", ESCOPO_CONSULTAR),
        ("cliente-consultas", ESCOPO_REGISTRAR),
    ],
)
def test_token_sem_o_escopo_da_rota_e_recusado(
    amb: Ambiente, cliente: str, escopo: str
) -> None:
    with pytest.raises(EscopoInsuficiente):
        amb.validador.validar(_token(cliente), escopo)
    assert amb.falhas_jwt(MotivoFalhaJwt.ESCOPO) == 1


def test_token_do_keycloak_com_assinatura_adulterada_e_recusado(
    amb: Ambiente,
) -> None:
    cabecalho, carga, assinatura = _token("cliente-lancamentos").split(".")
    trocado = "A" if assinatura[10] != "A" else "B"
    adulterada = assinatura[:10] + trocado + assinatura[11:]

    with pytest.raises(TokenInvalido) as erro:
        amb.validador.validar(f"{cabecalho}.{carga}.{adulterada}", ESCOPO_REGISTRAR)
    assert erro.value.motivo is MotivoFalhaJwt.ASSINATURA
    assert amb.falhas_jwt(MotivoFalhaJwt.ASSINATURA) == 1


def test_validador_de_outra_audiencia_recusa_o_token(amb: Ambiente) -> None:
    # cliente-lancamentos só recebe aud=lancamentos-api (001 TD-008).
    metricas = Metricas()
    ambiente = {
        "JWT_EMISSOR": EMISSOR,
        "JWT_AUDIENCIA": "consolidado-api",
        "JWT_JWKS_URL": f"{URL}/realms/{REALM}/protocol/openid-connect/certs",
        "JWT_AC_ARQUIVO": str(AC),
    }
    validador = criar_validador_lancamentos(metricas, ambiente)

    with pytest.raises(TokenInvalido) as erro:
        validador.validar(_token("cliente-lancamentos"), ESCOPO_REGISTRAR)
    assert erro.value.motivo is MotivoFalhaJwt.AUDIENCIA


def _docker_compose(*argumentos: str) -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), *argumentos],
        check=True,
        capture_output=True,
        timeout=180,
    )


def _aguardar_keycloak_pronto(limite_segundos: float = 180) -> None:
    fim = time.monotonic() + limite_segundos
    while time.monotonic() < fim:
        estado = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.State.Health.Status}}",
                "cashflow-keycloak-1",
            ],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if estado == "healthy":
            return
        time.sleep(2)
    raise TimeoutError("Keycloak não voltou a ficar pronto")


@contextmanager
def _keycloak_parado() -> Iterator[None]:
    _docker_compose("stop", "keycloak")
    try:
        yield
    finally:
        _docker_compose("start", "keycloak")
        _aguardar_keycloak_pronto()


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker ausente no PATH")
def test_keycloak_parado_token_valido_e_aceito_dentro_do_ttl(amb: Ambiente) -> None:
    # Token e JWKS obtidos com o Keycloak no ar; só então ele é parado.
    token = _token("cliente-lancamentos")
    amb.validador.validar(token, ESCOPO_REGISTRAR)

    with _keycloak_parado():
        amb.relogio.agora += TTL - 1
        principal = amb.validador.validar(token, ESCOPO_REGISTRAR)
        assert principal.cliente_id == "cliente-lancamentos"
        assert amb.falhas_jwks() == 0

        amb.relogio.agora += 1
        with pytest.raises(JwksIndisponivel):
            amb.validador.validar(token, ESCOPO_REGISTRAR)
        assert amb.falhas_jwks() == 1
        assert amb.falhas_jwt(MotivoFalhaJwt.JWKS_INDISPONIVEL) == 1
        assert not amb.validador.cache.pronto()
