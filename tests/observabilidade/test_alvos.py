"""Coleta de métricas no ar e isolada do host (007-T1; design 007 §3, TD-007).

Roda no host, com o ambiente no ar, a partir da raiz do repositório:

    docker compose -f infra/compose.yaml up -d --wait
    uv run pytest tests/observabilidade/test_alvos.py

O host não alcança o Prometheus, então a API dele é consultada de um
contêiner efêmero na `rede-observabilidade`. Do host, sondas TCP diretas
provam que nem o Prometheus nem os `/metrics` respondem; o Grafana responde
só em loopback, por TLS.

Sem o ambiente no ar, o módulo é pulado com o motivo explícito.
"""

import json
import shutil
import socket
import ssl
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

PROJETO = "cashflow"
AC = Path(__file__).resolve().parents[2] / "infra/tls/gerados/ac/ac-cashflow.crt"

# Jobs de infra/observabilidade/prometheus.yml.
JOBS = {
    "prometheus",
    "lancamentos-api",
    "consolidado-api",
    "relay",
    "worker",
    "rabbitmq",
    "rabbitmq-filas",
    "postgres-livro",
    "postgres-consolidado",
}

# Primeira coleta em até um intervalo (15 s) após a subida.
ESPERA_ALVOS_SEGUNDOS = 60

# Endpoints de métricas e do Prometheus, nenhum publicado no host.
SEM_PUBLICACAO = [
    ("prometheus", 9090),
    ("postgres-exporter-livro", 9187),
    ("postgres-exporter-consolidado", 9187),
    ("lancamentos-api", 9464),
    ("consolidado-api", 9464),
    ("relay", 9464),
    ("worker", 9464),
    ("rabbitmq", 15692),
]


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
        for servico in ("prometheus", "grafana", "pg-livro")
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


def _redes(servico: str) -> dict[str, dict[str, Any]]:
    redes: dict[str, dict[str, Any]] = _inspecionar(servico)["NetworkSettings"][
        "Networks"
    ]
    return redes


def _ips(servico: str) -> list[str]:
    return [rede["IPAddress"] for rede in _redes(servico).values() if rede["IPAddress"]]


def _rede(nome: str) -> dict[str, Any]:
    resultado = _docker("network", "inspect", f"{PROJETO}_{nome}")
    assert resultado.returncode == 0, resultado.stderr
    dados: dict[str, Any] = json.loads(resultado.stdout)[0]
    return dados


def _imagem_da_sonda() -> str:
    # A imagem Alpine do PostgreSQL, já presente e com versão fixada (001
    # TD-007), traz o wget do BusyBox com resolução de nomes pelo DNS do
    # Docker; o BusyBox da imagem do Prometheus não resolve nomes.
    imagem: str = _inspecionar("pg-livro")["Config"]["Image"]
    return imagem


def _consultar_prometheus(caminho: str) -> dict[str, Any]:
    resultado = _docker(
        "run",
        "--rm",
        "--pull=never",
        "--network",
        f"{PROJETO}_rede-observabilidade",
        _imagem_da_sonda(),
        "wget",
        "-q",
        "-O",
        "-",
        "-T",
        "5",
        f"http://prometheus:9090{caminho}",
    )
    assert resultado.returncode == 0, resultado.stderr
    dados: dict[str, Any] = json.loads(resultado.stdout)
    assert dados["status"] == "success", dados
    return dados


def _saude_dos_alvos() -> dict[str, str]:
    dados = _consultar_prometheus("/api/v1/targets?state=active")
    return {
        alvo["labels"]["job"]: alvo["health"] for alvo in dados["data"]["activeTargets"]
    }


def _host_conecta(ip: str, porta: int) -> bool:
    try:
        socket.create_connection((ip, porta), timeout=3).close()
    except OSError:
        return False
    return True


def test_todos_os_alvos_estao_up() -> None:
    limite = time.monotonic() + ESPERA_ALVOS_SEGUNDOS
    saude = _saude_dos_alvos()
    while any(v != "up" for v in saude.values()) and time.monotonic() < limite:
        time.sleep(3)
        saude = _saude_dos_alvos()

    assert set(saude) == JOBS
    assert {job: v for job, v in saude.items() if v != "up"} == {}


def test_metrica_up_vale_um_em_todos_os_alvos() -> None:
    dados = _consultar_prometheus("/api/v1/query?query=up")
    valores = {
        serie["metric"]["job"]: serie["value"][1] for serie in dados["data"]["result"]
    }

    assert valores == dict.fromkeys(JOBS, "1")


@pytest.mark.parametrize("job", ["postgres-livro", "postgres-consolidado"])
def test_exporter_autentica_no_banco_com_papel_de_monitoramento(job: str) -> None:
    # O exporter responde com up = 1 mesmo sem conectar; pg_up mostra se o
    # papel de monitoramento entrou no banco.
    dados = _consultar_prometheus(f'/api/v1/query?query=pg_up{{job="{job}"}}')

    assert [serie["value"][1] for serie in dados["data"]["result"]] == ["1"]


@pytest.mark.parametrize(
    "rede",
    ["rede-observabilidade", "rede-livro", "rede-consolidado", "rede-mensageria"],
)
def test_redes_do_prometheus_sao_internas(rede: str) -> None:
    dados = _rede(rede)

    assert dados["Internal"] is True
    assert dados["Options"].get("com.docker.network.bridge.inhibit_ipv4") == "true"


def test_prometheus_so_esta_em_redes_internas() -> None:
    redes = {nome.removeprefix(f"{PROJETO}_") for nome in _redes("prometheus")}

    assert redes == {
        "rede-livro",
        "rede-consolidado",
        "rede-mensageria",
        "rede-observabilidade",
    }


def test_rede_de_publicacao_do_grafana_so_tem_o_grafana() -> None:
    conteineres = _rede("rede-publicacao-grafana")["Containers"] or {}
    id_grafana = _inspecionar("grafana")["Id"]

    assert list(conteineres) == [id_grafana]


@pytest.mark.parametrize(
    "servico",
    ["prometheus", "postgres-exporter-livro", "postgres-exporter-consolidado"],
)
def test_prometheus_e_exporters_nao_publicam_porta_no_host(servico: str) -> None:
    assert _publicacoes(servico) == []


@pytest.mark.parametrize(("servico", "porta"), SEM_PUBLICACAO)
def test_host_nao_alcanca_metricas_nem_prometheus(servico: str, porta: int) -> None:
    ips = _ips(servico)

    assert ips, f"{servico} sem IP nas redes do compose"
    for ip in ips:
        assert not _host_conecta(ip, porta), f"host alcança {servico} em {ip}:{porta}"


@pytest.mark.parametrize("servico", ["lancamentos-api", "consolidado-api"])
def test_host_alcanca_a_api_pela_rede_de_identidade(servico: str) -> None:
    # Controle positivo do teste acima: o host chega ao IP da API na rede de
    # identidade, e só o /metrics recusa nesse endereço (007 TD-007).
    ip = _redes(servico)[f"{PROJETO}_rede-identidade"]["IPAddress"]

    assert _host_conecta(ip, 8443)


@pytest.mark.parametrize("servico", ["prometheus", "grafana"])
def test_coletor_e_visualizacao_com_health_check_saudavel(servico: str) -> None:
    saude = _inspecionar(servico)["State"].get("Health")

    assert saude is not None, f"{servico} sem health check declarado"
    assert saude["Status"] == "healthy"


def test_grafana_publicado_somente_em_loopback() -> None:
    publicacoes = _publicacoes("grafana")

    assert publicacoes
    assert {ligacao["HostIp"] for ligacao in publicacoes} == {"127.0.0.1"}


def _porta_do_grafana() -> int:
    (ligacao,) = _publicacoes("grafana")
    return int(ligacao["HostPort"])


def test_grafana_atende_por_tls_em_loopback() -> None:
    contexto = ssl.create_default_context(cafile=str(AC))
    url = f"https://localhost:{_porta_do_grafana()}/api/health"

    with urllib.request.urlopen(url, context=contexto, timeout=10) as resposta:
        assert resposta.status == 200
        assert json.loads(resposta.read())["database"] == "ok"


def test_grafana_recusa_http_sem_tls() -> None:
    bruto = socket.create_connection(("127.0.0.1", _porta_do_grafana()), timeout=5)
    with bruto:
        bruto.sendall(b"GET /api/health HTTP/1.1\r\nHost: localhost\r\n\r\n")
        resposta = bruto.recv(64)

    assert not resposta.startswith(b"HTTP/1.1 200")


def test_grafana_nao_atende_em_endereco_do_host_fora_do_loopback() -> None:
    # O gateway da rede de publicação é um endereço do próprio host fora do
    # loopback; a publicação em 127.0.0.1 não deve atendê-lo.
    (config,) = _rede("rede-publicacao-grafana")["IPAM"]["Config"]

    assert not _host_conecta(config["Gateway"], _porta_do_grafana())
