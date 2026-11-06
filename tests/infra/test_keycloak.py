"""Emissão de token por Client Credentials contra o Keycloak real (001-T4).

O teste roda dentro da rede de identidade (001-T5), pelo nome interno, para
que a sonda de HTTP em texto claro na porta 8080 não dependa do que escuta no
host. Com o ambiente no ar, a partir da raiz do repositório:

    docker run --rm --network cashflow_rede-identidade --env-file infra/.env \\
      -e KEYCLOAK_URL=https://keycloak:8443 -e UV_PROJECT_ENVIRONMENT=/tmp/venv \\
      -v "$PWD":/w -w /w ghcr.io/astral-sh/uv:0.13.7-python3.12-alpine \\
      uv run --frozen pytest -p no:cacheprovider tests/infra/test_keycloak.py

Sem `KEYCLOAK_URL`, o módulo é pulado com o motivo explícito.
"""

import base64
import hashlib
import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest

URL = os.environ.get("KEYCLOAK_URL")
REALM = "cashflow"
EMISSOR = f"https://localhost:8443/realms/{REALM}"
AC = Path(__file__).resolve().parents[2] / "infra/tls/gerados/ac/ac-cashflow.crt"

pytestmark = pytest.mark.skipif(
    URL is None,
    reason="KEYCLOAK_URL ausente: executar dentro da rede do compose (ver docstring)",
)

# Prefixo DigestInfo do SHA-256 em PKCS#1 v1.5 (RFC 8017, 9.2, nota 1).
_DIGEST_INFO_SHA256 = bytes.fromhex("3031300d060960864801650304020105000420")


def _contexto_tls() -> ssl.SSLContext:
    # Só a AC local é confiável: valida que o Keycloak serve o certificado
    # emitido por ela (FR-006).
    return ssl.create_default_context(cafile=str(AC))


def _post(url: str, campos: dict[str, str]) -> tuple[int, dict[str, Any]]:
    requisicao = urllib.request.Request(
        url, data=urllib.parse.urlencode(campos).encode(), method="POST"
    )
    try:
        with urllib.request.urlopen(
            requisicao, context=_contexto_tls(), timeout=10
        ) as resposta:
            return resposta.status, json.load(resposta)
    except urllib.error.HTTPError as erro:
        return erro.code, json.load(erro)


def _get(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, context=_contexto_tls(), timeout=10) as resposta:
        resultado: dict[str, Any] = json.load(resposta)
        return resultado


def _endpoint_token(realm: str = REALM) -> str:
    return f"{URL}/realms/{realm}/protocol/openid-connect/token"


def _client_credentials(
    cliente: str, segredo: str, **extras: str
) -> tuple[int, dict[str, Any]]:
    campos = {
        "grant_type": "client_credentials",
        "client_id": cliente,
        "client_secret": segredo,
        **extras,
    }
    return _post(_endpoint_token(), campos)


def _segredo(cliente: str) -> str:
    variavel = {
        "cliente-lancamentos": "KEYCLOAK_SEGREDO_CLIENTE_LANCAMENTOS",
        "cliente-consultas": "KEYCLOAK_SEGREDO_CLIENTE_CONSULTAS",
    }[cliente]
    return os.environ[variavel]


def _token(cliente: str) -> str:
    status, corpo = _client_credentials(cliente, _segredo(cliente))
    assert status == 200, corpo
    token: str = corpo["access_token"]
    return token


def _b64url(trecho: str) -> bytes:
    return base64.urlsafe_b64decode(trecho + "=" * (-len(trecho) % 4))


def _partes(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    cabecalho, carga, assinatura = token.split(".")
    return (
        json.loads(_b64url(cabecalho)),
        json.loads(_b64url(carga)),
        _b64url(assinatura),
        f"{cabecalho}.{carga}".encode(),
    )


def _assinatura_rs256_valida(
    chave: dict[str, Any], assinatura: bytes, conteudo: bytes
) -> bool:
    n = int.from_bytes(_b64url(chave["n"]), "big")
    e = int.from_bytes(_b64url(chave["e"]), "big")
    tamanho = (n.bit_length() + 7) // 8
    recuperado = pow(int.from_bytes(assinatura, "big"), e, n).to_bytes(tamanho, "big")
    sufixo = _DIGEST_INFO_SHA256 + hashlib.sha256(conteudo).digest()
    esperado = b"\x00\x01" + b"\xff" * (tamanho - len(sufixo) - 3) + b"\x00" + sufixo
    return recuperado == esperado


@pytest.mark.parametrize(
    ("cliente", "escopos"),
    [
        ("cliente-lancamentos", {"lancamentos:registrar"}),
        ("cliente-consultas", {"consolidado:consultar", "lancamentos:consultar"}),
    ],
)
def test_token_de_cada_cliente_contem_somente_seu_escopo(
    cliente: str, escopos: set[str]
) -> None:
    _, carga, _, _ = _partes(_token(cliente))

    assert set(carga["scope"].split()) == escopos
    assert carga["azp"] == cliente
    assert carga["iss"] == EMISSOR


def _audiencias(carga: dict[str, Any]) -> set[str]:
    aud = carga.get("aud", [])
    return {aud} if isinstance(aud, str) else set(aud)


@pytest.mark.parametrize(
    ("cliente", "audiencias"),
    [
        ("cliente-lancamentos", {"lancamentos-api"}),
        ("cliente-consultas", {"consolidado-api", "lancamentos-api"}),
    ],
)
def test_token_traz_a_audiencia_das_apis_dos_seus_escopos(
    cliente: str, audiencias: set[str]
) -> None:
    # TD-008: a audiência acompanha o escopo; nada além das APIs autorizadas.
    _, carga, _, _ = _partes(_token(cliente))

    assert _audiencias(carga) == audiencias


def test_cliente_de_registro_nao_obtem_escopo_de_consulta() -> None:
    status, corpo = _client_credentials(
        "cliente-lancamentos",
        _segredo("cliente-lancamentos"),
        scope="consolidado:consultar lancamentos:consultar",
    )

    if status == 200:
        _, carga, _, _ = _partes(corpo["access_token"])
        assert set(carga["scope"].split()) == {"lancamentos:registrar"}
    else:
        assert corpo["error"] == "invalid_scope"


@pytest.mark.parametrize("cliente", ["cliente-lancamentos", "cliente-consultas"])
def test_token_e_assinado_com_rs256_e_confere_com_o_jwks(cliente: str) -> None:
    cabecalho, _, assinatura, conteudo = _partes(_token(cliente))
    jwks = _get(f"{URL}/realms/{REALM}/protocol/openid-connect/certs")
    chaves = {chave["kid"]: chave for chave in jwks["keys"]}

    assert cabecalho["alg"] == "RS256"
    chave = chaves[cabecalho["kid"]]
    assert chave["kty"] == "RSA"
    assert _assinatura_rs256_valida(chave, assinatura, conteudo)


def test_jwks_publica_somente_chaves_assimetricas() -> None:
    jwks = _get(f"{URL}/realms/{REALM}/protocol/openid-connect/certs")

    assert jwks["keys"]
    assert {chave["kty"] for chave in jwks["keys"]} <= {"RSA", "EC"}


def test_segredo_errado_e_recusado() -> None:
    status, corpo = _client_credentials("cliente-lancamentos", "segredo-errado")

    assert status == 401
    assert corpo["error"] == "unauthorized_client"


def test_clientes_tecnicos_nao_usam_outro_fluxo() -> None:
    status, corpo = _post(
        _endpoint_token(),
        {
            "grant_type": "password",
            "client_id": "cliente-consultas",
            "client_secret": _segredo("cliente-consultas"),
            "username": "qualquer",
            "password": "qualquer",
        },
    )

    assert status in {400, 401}
    assert corpo["error"] == "unauthorized_client"


def test_endpoint_de_token_nao_responde_sem_tls() -> None:
    assert URL is not None
    destino = urllib.parse.urlsplit(URL)
    host, porta = destino.hostname, destino.port
    assert host is not None and porta is not None

    # Sem listener HTTP: a porta 8080 recusa conexão.
    with pytest.raises(OSError):
        socket.create_connection((host, 8080), timeout=5).close()

    # HTTP em texto claro na porta TLS não recebe resposta HTTP.
    pedido = (
        f"POST /realms/{REALM}/protocol/openid-connect/token HTTP/1.1\r\n"
        f"Host: {host}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    ).encode()
    with socket.create_connection((host, porta), timeout=5) as conexao:
        conexao.sendall(pedido)
        try:
            resposta = conexao.recv(1024)
        except (ConnectionResetError, TimeoutError):
            resposta = b""
    assert not resposta.startswith(b"HTTP/")


def test_administrador_fica_no_realm_master_separado_dos_clientes() -> None:
    credenciais = {
        "grant_type": "password",
        "client_id": "admin-cli",
        "username": "admin-cashflow",
        "password": os.environ["KEYCLOAK_SENHA_ADMIN"],
    }

    status_master, _ = _post(_endpoint_token("master"), credenciais)
    status_cashflow, _ = _post(_endpoint_token(REALM), credenciais)

    assert status_master == 200
    assert status_cashflow in {400, 401}
