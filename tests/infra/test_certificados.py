"""AC local e certificados TLS gerados por OpenSSL (001-T3, FR-006, TD-004).

Não depende do compose: executa o script em diretório temporário e confere a
cadeia com `openssl verify` e com um handshake TLS real em loopback.

    uv run pytest tests/infra/test_certificados.py
"""

import shutil
import socket
import ssl
import subprocess
import threading
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
SCRIPT = RAIZ / "infra" / "tls" / "gerar-certificados.sh"
SERVICOS = ["lancamentos-api", "consolidado-api", "keycloak", "grafana"]

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl ausente no PATH"
)


def _openssl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["openssl", *args], capture_output=True, text=True)


def _gerar(destino: Path) -> None:
    subprocess.run(["bash", str(SCRIPT), str(destino)], check=True, capture_output=True)


@pytest.fixture(scope="module")
def saida(tmp_path_factory: pytest.TempPathFactory) -> Path:
    destino = tmp_path_factory.mktemp("tls")
    _gerar(destino)
    return destino


@pytest.mark.parametrize("servico", SERVICOS)
def test_certificado_valida_contra_a_ac_local(saida: Path, servico: str) -> None:
    resultado = _openssl(
        "verify",
        "-CAfile",
        str(saida / "ac" / "ac-cashflow.crt"),
        "-purpose",
        "sslserver",
        str(saida / f"{servico}.crt"),
    )
    assert resultado.returncode == 0, resultado.stderr
    assert resultado.stdout.strip().endswith(": OK")


@pytest.mark.parametrize("servico", SERVICOS)
def test_certificado_cobre_nome_interno_e_loopback(saida: Path, servico: str) -> None:
    resultado = _openssl(
        "x509", "-in", str(saida / f"{servico}.crt"), "-noout", "-ext", "subjectAltName"
    )
    assert resultado.returncode == 0, resultado.stderr
    assert f"DNS:{servico}" in resultado.stdout
    assert "DNS:localhost" in resultado.stdout
    assert "IP Address:127.0.0.1" in resultado.stdout


def test_certificado_nao_valida_contra_outra_ac(saida: Path, tmp_path: Path) -> None:
    _gerar(tmp_path)
    resultado = _openssl(
        "verify",
        "-CAfile",
        str(tmp_path / "ac" / "ac-cashflow.crt"),
        str(saida / "keycloak.crt"),
    )
    assert resultado.returncode != 0


def test_nova_execucao_reaproveita_a_ac(tmp_path: Path) -> None:
    _gerar(tmp_path)
    ac = tmp_path / "ac" / "ac-cashflow.crt"
    antes = ac.read_bytes()
    _gerar(tmp_path)
    assert ac.read_bytes() == antes
    resultado = _openssl("verify", "-CAfile", str(ac), str(tmp_path / "grafana.crt"))
    assert resultado.returncode == 0, resultado.stderr


def test_chave_privada_da_ac_so_legivel_pelo_dono(saida: Path) -> None:
    modo = (saida / "ac" / "ac-cashflow.key").stat().st_mode & 0o777
    assert modo == 0o600


def test_handshake_tls_com_a_ac_local(saida: Path) -> None:
    servidor_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    servidor_ctx.load_cert_chain(saida / "keycloak.crt", saida / "keycloak.key")
    cliente_ctx = ssl.create_default_context(cafile=saida / "ac" / "ac-cashflow.crt")

    with socket.create_server(("127.0.0.1", 0)) as escuta:
        porta = escuta.getsockname()[1]

        def aceitar() -> None:
            conexao, _ = escuta.accept()
            with servidor_ctx.wrap_socket(conexao, server_side=True) as tls:
                tls.recv(1)

        tarefa = threading.Thread(target=aceitar)
        tarefa.start()
        with socket.create_connection(("127.0.0.1", porta)) as bruto:
            with cliente_ctx.wrap_socket(bruto, server_hostname="localhost") as tls:
                assert tls.version() in ("TLSv1.2", "TLSv1.3")
                tls.sendall(b"x")
        tarefa.join(timeout=5)


@pytest.mark.parametrize(
    "caminho",
    [
        "infra/tls/gerados/ac/ac-cashflow.key",
        "infra/tls/gerados/ac/ac-cashflow.crt",
        "infra/tls/gerados/keycloak.key",
    ],
)
def test_saida_do_script_fica_fora_do_git(caminho: str) -> None:
    resultado = subprocess.run(
        ["git", "check-ignore", "-q", caminho], cwd=RAIZ, capture_output=True
    )
    assert resultado.returncode == 0, f"{caminho} não está ignorado pelo Git"
