"""Isolamento de rede do ambiente local (001-T5, FR-007, TD-002).

Roda no host, com o ambiente no ar, a partir da raiz do repositório:

    docker compose -f infra/compose.yaml up -d --wait
    uv run pytest tests/infra/test_isolamento.py

Do host, sondas TCP diretas. Do lado do consolidado, contêineres efêmeros
ligados às redes desse lado tentam alcançar `pg-livro`; a mesma sonda nas
redes de origem prova que a falha vem do isolamento, não da sonda.

Sem o ambiente no ar, o módulo é pulado com o motivo explícito.
"""

import json
import shutil
import socket
import ssl
import subprocess
from pathlib import Path
from typing import Any

import pytest

PROJETO = "cashflow"
AC = Path(__file__).resolve().parents[2] / "infra/tls/gerados/ac/ac-cashflow.crt"

# Redes em que vivem worker, consolidado-api e coordenador (design 001 §3).
REDES_DO_CONSOLIDADO = ["rede-consolidado", "rede-mensageria", "rede-identidade"]
REDES_INTERNAS = ["rede-livro", "rede-consolidado", "rede-mensageria"]


def _docker(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], check=False, capture_output=True, text=True, timeout=timeout
    )


def _id_do_conteiner(servico: str) -> str | None:
    resultado = _docker(
        "ps",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={PROJETO}",
        "--filter",
        f"label=com.docker.compose.service={servico}",
    )
    return resultado.stdout.strip() or None


def _motivo_para_pular() -> str | None:
    if shutil.which("docker") is None:
        return "docker ausente no PATH"
    ausentes = [
        servico
        for servico in ("pg-livro", "pg-consolidado", "rabbitmq", "keycloak")
        if _id_do_conteiner(servico) is None
    ]
    if ausentes:
        return f"ambiente fora do ar ({', '.join(ausentes)}): ver docstring"
    return None


_MOTIVO = _motivo_para_pular()
pytestmark = pytest.mark.skipif(_MOTIVO is not None, reason=_MOTIVO or "")


def _inspecionar(servico: str) -> dict[str, Any]:
    id_ = _id_do_conteiner(servico)
    assert id_ is not None, f"{servico} não está em execução"
    resultado = _docker("inspect", id_)
    assert resultado.returncode == 0, resultado.stderr
    dados: dict[str, Any] = json.loads(resultado.stdout)[0]
    return dados


def _publicacoes(servico: str) -> list[dict[str, str]]:
    portas = _inspecionar(servico)["NetworkSettings"]["Ports"] or {}
    return [ligacao for ligacoes in portas.values() for ligacao in ligacoes or []]


def _ips(servico: str) -> list[str]:
    redes = _inspecionar(servico)["NetworkSettings"]["Networks"]
    return [rede["IPAddress"] for rede in redes.values() if rede["IPAddress"]]


def _imagem_da_sonda() -> str:
    # A própria imagem do PostgreSQL, já presente e com versão fixada
    # (TD-007): traz pg_isready e o nc do BusyBox.
    imagem: str = _inspecionar("pg-livro")["Config"]["Image"]
    return imagem


def _sondar(rede: str, *comando: str) -> subprocess.CompletedProcess[str]:
    return _docker(
        "run",
        "--rm",
        "--pull=never",
        "--network",
        f"{PROJETO}_{rede}",
        _imagem_da_sonda(),
        *comando,
    )


def _host_conecta(ip: str, porta: int) -> bool:
    try:
        socket.create_connection((ip, porta), timeout=3).close()
    except OSError:
        return False
    return True


@pytest.mark.parametrize("rede", REDES_INTERNAS)
def test_redes_de_dados_e_mensageria_sao_internas(rede: str) -> None:
    resultado = _docker("network", "inspect", f"{PROJETO}_{rede}")
    assert resultado.returncode == 0, resultado.stderr
    dados = json.loads(resultado.stdout)[0]

    assert dados["Internal"] is True
    assert dados["Options"].get("com.docker.network.bridge.inhibit_ipv4") == "true"


@pytest.mark.parametrize("servico", ["pg-livro", "pg-consolidado", "rabbitmq"])
def test_bancos_e_rabbitmq_nao_publicam_porta_no_host(servico: str) -> None:
    assert _publicacoes(servico) == []


@pytest.mark.parametrize(
    ("servico", "porta"),
    [
        ("pg-livro", 5432),
        ("pg-consolidado", 5432),
        ("rabbitmq", 5672),
        ("rabbitmq", 15672),
    ],
)
def test_host_nao_conecta_a_bancos_e_rabbitmq(servico: str, porta: int) -> None:
    ips = _ips(servico)

    assert ips, f"{servico} sem IP nas redes do compose"
    for ip in ips:
        assert not _host_conecta(ip, porta), f"host alcança {servico} em {ip}:{porta}"


@pytest.mark.parametrize(
    ("rede", "servico", "porta"),
    [
        ("rede-livro", "pg-livro", 5432),
        ("rede-consolidado", "pg-consolidado", 5432),
        ("rede-mensageria", "rabbitmq", 5672),
        ("rede-mensageria", "rabbitmq", 15672),
    ],
)
def test_servico_responde_dentro_da_propria_rede(
    rede: str, servico: str, porta: int
) -> None:
    # Controle positivo: sem ele, as recusas acima passariam com o serviço
    # simplesmente parado.
    resultado = _sondar(rede, "nc", "-z", "-w", "3", servico, str(porta))

    assert resultado.returncode == 0, resultado.stderr


@pytest.mark.parametrize("rede", REDES_DO_CONSOLIDADO)
def test_lado_do_consolidado_nao_resolve_pg_livro(rede: str) -> None:
    resultado = _sondar(rede, "pg_isready", "-h", "pg-livro", "-t", "3")

    assert resultado.returncode != 0, resultado.stdout


@pytest.mark.parametrize("rede", REDES_DO_CONSOLIDADO)
def test_lado_do_consolidado_nao_alcanca_pg_livro_pelo_ip(rede: str) -> None:
    (ip,) = _ips("pg-livro")

    resultado = _sondar(rede, "pg_isready", "-h", ip, "-t", "3")

    assert resultado.returncode != 0, resultado.stdout


def test_keycloak_publicado_somente_em_loopback() -> None:
    publicacoes = _publicacoes("keycloak")

    assert publicacoes
    assert {ligacao["HostIp"] for ligacao in publicacoes} == {"127.0.0.1"}


def _porta_do_keycloak() -> int:
    (ligacao,) = _publicacoes("keycloak")
    return int(ligacao["HostPort"])


def test_keycloak_atende_por_tls_em_loopback() -> None:
    contexto = ssl.create_default_context(cafile=str(AC))
    bruto = socket.create_connection(("127.0.0.1", _porta_do_keycloak()), timeout=5)

    with contexto.wrap_socket(bruto, server_hostname="localhost") as tls:
        assert tls.version() in ("TLSv1.2", "TLSv1.3")


def test_keycloak_nao_atende_em_endereco_do_host_fora_do_loopback() -> None:
    # O gateway da rede de identidade é um endereço do próprio host fora do
    # loopback; a publicação em 127.0.0.1 não deve atendê-lo.
    resultado = _docker("network", "inspect", f"{PROJETO}_rede-identidade")
    (config,) = json.loads(resultado.stdout)[0]["IPAM"]["Config"]

    assert not _host_conecta(config["Gateway"], _porta_do_keycloak())
