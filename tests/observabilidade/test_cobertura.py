"""Cobertura das métricas no Prometheus (007-T4; FR-001, FR-004; design 007
§4, §11, TD-005, TD-006).

Com o ambiente no ar, o teste consulta o Prometheus e confirma:

- dados para as 12 métricas de INFRA 8.3, com os nomes do mapa de design
  007 §4, cada uma vinda do componente que a origina;
- dados para as métricas do mapa fora de INFRA 8.3:
  `cashflow_http_requisicoes_total` das duas APIs (TD-005) e
  `cashflow_relay_falhas_publicacao_total` com os quatro resultados (TD-006);
- rótulos sem dado sensível (FR-004; INFRA 8.2): cada série `cashflow_*`
  só tem os rótulos da lista fechada do catálogo (TD-003), mais os que o
  Prometheus acrescenta na coleta, e nenhum valor de rótulo tem forma de
  identificador (UUID) ou de valor monetário.

O host não alcança o Prometheus (TD-007), então a API dele é consultada de
um contêiner avulso na `rede-observabilidade`. A partir da raiz do
repositório:

    docker compose -f infra/compose.yaml up -d --wait
    uv run pytest tests/observabilidade/test_cobertura.py

Sem o ambiente no ar, o módulo é pulado com o motivo explícito.
"""

import json
import re
import shutil
import subprocess
import time
import urllib.parse
from typing import Any

import pytest
from observabilidade.catalogo import ROTULOS_PERMITIDOS

PROJETO = "cashflow"

# INFRA 8.3, na ordem de 007 FR-001: item -> {métrica: jobs de origem}.
# Origens do mapa de design 007 §4, com uma diferença nos pools de conexão:
# o coordenador roda como CLI, sem /metrics (design 007 §4). O relay passou a
# expor o pool na correção da review 007 (Issue 1). Espaço em disco vem dos
# exporters e do plugin do RabbitMQ.
METRICAS_INFRA_8_3: dict[str, dict[str, set[str]]] = {
    "backlog da outbox": {"cashflow_outbox_pendentes": {"relay"}},
    "idade do evento mais antigo": {
        "cashflow_outbox_idade_mais_antigo_segundos": {"relay"}
    },
    "mensagens prontas e não confirmadas": {
        "rabbitmq_detailed_queue_messages_ready": {"rabbitmq-filas"},
        "rabbitmq_detailed_queue_messages_unacked": {"rabbitmq-filas"},
    },
    "reentregas": {"cashflow_worker_reentregas_total": {"worker"}},
    "quarentena": {"cashflow_quarentena_total": {"worker"}},
    "pools de conexão": {
        "cashflow_pool_conexoes": {
            "lancamentos-api",
            "consolidado-api",
            "worker",
            "relay",
        }
    },
    "espaço em disco": {
        "pg_database_size_bytes": {"postgres-livro", "postgres-consolidado"},
        "rabbitmq_disk_space_available_bytes": {"rabbitmq"},
    },
    "geração ativa": {"cashflow_geracao_ativa": {"consolidado-api"}},
    "fase da reconstrução": {"cashflow_reconstrucao_fase": {"consolidado-api"}},
    "checkpoint": {"cashflow_checkpoint_posicao": {"worker"}},
    "lacunas": {"cashflow_lacunas": {"worker"}},
    "falhas de JWT e JWKS": {
        "cashflow_jwt_falhas_total": {"lancamentos-api", "consolidado-api"},
        "cashflow_jwks_falhas_total": {"lancamentos-api", "consolidado-api"},
    },
}

# Rótulos que o Prometheus acrescenta na coleta (prometheus.yml).
ROTULOS_DA_COLETA = {"__name__", "job", "instance"}

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_DECIMAL = re.compile(r"^-?\d+[.,]\d+$")

# Primeira coleta em até um intervalo (15 s) após a subida.
ESPERA_DADOS_SEGUNDOS = 60


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
    ausentes = [s for s in ("prometheus", "pg-livro") if _id_do_conteiner(s) is None]
    if ausentes:
        return f"ambiente fora do ar ({', '.join(ausentes)}): ver docstring"
    return None


_MOTIVO = _motivo_para_pular()
pytestmark = pytest.mark.skipif(_MOTIVO is not None, reason=_MOTIVO or "")


def _imagem_da_sonda() -> str:
    # Imagem Alpine do PostgreSQL, já presente e fixada (001 TD-007): o wget
    # do BusyBox dela resolve nomes pelo DNS do Docker (como em test_alvos.py).
    resultado = _docker(
        "inspect", "--format", "{{.Config.Image}}", _id_do_conteiner("pg-livro") or ""
    )
    assert resultado.returncode == 0, resultado.stderr
    return resultado.stdout.strip()


def _consulta(expr: str) -> list[dict[str, Any]]:
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
        "http://prometheus:9090/api/v1/query?query=" + urllib.parse.quote(expr),
    )
    assert resultado.returncode == 0, resultado.stderr
    dados: dict[str, Any] = json.loads(resultado.stdout)
    assert dados["status"] == "success", dados
    series: list[dict[str, Any]] = dados["data"]["result"]
    return series


def _series_com_dado(metrica: str) -> list[dict[str, Any]]:
    """Séries da métrica com valor numérico; espera a primeira coleta."""
    limite = time.monotonic() + ESPERA_DADOS_SEGUNDOS
    series = _consulta(metrica)
    while not series and time.monotonic() < limite:
        time.sleep(5)
        series = _consulta(metrica)
    for serie in series:
        float(serie["value"][1])
    return series


def test_mapa_cobre_as_12_metricas_de_infra_8_3() -> None:
    assert len(METRICAS_INFRA_8_3) == 12


@pytest.mark.parametrize("item", list(METRICAS_INFRA_8_3))
def test_metrica_de_infra_8_3_tem_dados(item: str) -> None:
    for metrica, origens in METRICAS_INFRA_8_3[item].items():
        series = _series_com_dado(metrica)

        assert series, f"{item}: {metrica} sem dados"
        jobs = {s["metric"]["job"] for s in series}
        assert jobs == origens, f"{item}: {metrica} coletada de {jobs}"


def test_requisicoes_http_por_status_tem_dados_das_duas_apis() -> None:
    # As sondas de health das APIs passam pelo contador: a série de 200 nasce
    # logo após a subida (TD-005).
    series = _series_com_dado("cashflow_http_requisicoes_total")

    assert {s["metric"]["componente"] for s in series} == {
        "lancamentos-api",
        "consolidado-api",
    }
    assert all(re.fullmatch(r"[1-5]\d\d", s["metric"]["status"]) for s in series)


def test_falhas_de_publicacao_do_relay_tem_os_quatro_resultados() -> None:
    # O relay inicializa as quatro séries com zero na subida (TD-006).
    series = _series_com_dado("cashflow_relay_falhas_publicacao_total")

    assert {s["metric"]["resultado"] for s in series} == {
        "devolvido",
        "recusado",
        "sem_confirmacao",
        "falha",
    }
    assert {s["metric"]["job"] for s in series} == {"relay"}


def _no_catalogo(nome: str) -> str:
    """Nome no catálogo de uma série coletada.

    A biblioteca cliente acompanha cada contador `X_total` de uma série
    `X_created`, com os mesmos rótulos, que guarda o instante de criação.
    """
    if nome.endswith("_created"):
        return nome.removesuffix("_created") + "_total"
    return nome


def test_metricas_de_aplicacao_coletadas_estao_no_catalogo() -> None:
    series = _consulta('{__name__=~"cashflow_.+"}')

    assert {_no_catalogo(s["metric"]["__name__"]) for s in series} == set(
        ROTULOS_PERMITIDOS
    )


def test_rotulos_das_metricas_de_aplicacao_sem_dado_sensivel() -> None:
    for serie in _consulta('{__name__=~"cashflow_.+"}'):
        rotulos = serie["metric"]
        nome = rotulos["__name__"]
        permitidos = ROTULOS_PERMITIDOS[_no_catalogo(nome)]
        extras = set(rotulos) - ROTULOS_DA_COLETA - permitidos

        assert not extras, f"{nome}: rótulos fora do catálogo {extras}"
        for rotulo, valor in rotulos.items():
            assert not _UUID.search(valor), f"{nome}: {rotulo} com identificador"
            assert not _DECIMAL.match(valor), f"{nome}: {rotulo} com valor decimal"
