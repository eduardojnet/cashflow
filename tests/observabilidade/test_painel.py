"""Painel CashFlow versionado e carregado na subida (007-T3; FR-002).

Duas partes:

- Sem ambiente: o JSON versionado tem as seções de design 007 §7 e cada
  métrica de INFRA 8.3 aparece em pelo menos uma consulta de gráfico.
- Com o ambiente no ar: o Grafana carregou fonte de dados e painel pelo
  provisionamento, e cada consulta do painel, executada pela própria API do
  Grafana contra o Prometheus, devolve dados. É o equivalente automatizado de
  abrir o painel. Antes, o teste provoca um 401 na API do consolidado para
  que o gráfico de recusas tenha série.

A partir da raiz do repositório:

    docker compose -f infra/compose.yaml up -d --wait
    uv run pytest tests/observabilidade/test_painel.py

A senha do administrador do Grafana vem de infra/.env e vai só no cabeçalho
da requisição. Sem o ambiente no ar, a parte que depende dele é pulada com o
motivo explícito.
"""

import base64
import json
import re
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

RAIZ = Path(__file__).resolve().parents[2]
GRAFANA = RAIZ / "infra/observabilidade/grafana"
PAINEL = GRAFANA / "dashboards/cashflow.json"
ENV = RAIZ / "infra/.env"
AC = RAIZ / "infra/tls/gerados/ac/ac-cashflow.crt"
PROJETO = "cashflow"
UID_PAINEL = "cashflow"
UID_FONTE = "prometheus-cashflow"
USUARIO_ADMIN = "admin-cashflow"

SECOES = [
    "Livro e outbox",
    "Mensageria",
    "Consolidado",
    "Reconstrução",
    "Segurança",
    "Recursos",
]

# INFRA 8.3, na ordem de 007 FR-001, com os nomes do mapa de design 007 §4.
METRICAS_INFRA_8_3: dict[str, list[str]] = {
    "backlog da outbox": ["cashflow_outbox_pendentes"],
    "idade do evento mais antigo": ["cashflow_outbox_idade_mais_antigo_segundos"],
    "mensagens prontas e não confirmadas": [
        "rabbitmq_detailed_queue_messages_ready",
        "rabbitmq_detailed_queue_messages_unacked",
    ],
    "reentregas": ["cashflow_worker_reentregas_total"],
    "quarentena": ["cashflow_quarentena_total"],
    "pools de conexão": ["cashflow_pool_conexoes"],
    "espaço em disco": [
        "pg_database_size_bytes",
        "rabbitmq_disk_space_available_bytes",
    ],
    "geração ativa": ["cashflow_geracao_ativa"],
    "fase da reconstrução": ["cashflow_reconstrucao_fase"],
    "checkpoint": ["cashflow_checkpoint_posicao"],
    "lacunas": ["cashflow_lacunas"],
    "falhas de JWT e JWKS": ["cashflow_jwt_falhas_total", "cashflow_jwks_falhas_total"],
}

# Primeira coleta em até um intervalo (15 s) após a subida.
ESPERA_DADOS_SEGUNDOS = 60


def _painel() -> dict[str, Any]:
    dados: dict[str, Any] = json.loads(PAINEL.read_text(encoding="utf-8"))
    return dados


def _graficos(painel: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in painel["panels"] if p["type"] != "row"]


def _consultas(painel: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(p["title"], alvo) for p in _graficos(painel) for alvo in p["targets"]]


def _secao_de_cada_grafico(painel: dict[str, Any]) -> dict[str, str]:
    secao = ""
    resultado: dict[str, str] = {}
    for p in sorted(painel["panels"], key=lambda p: p["gridPos"]["y"]):
        if p["type"] == "row":
            secao = p["title"]
        else:
            resultado[p["title"]] = secao
    return resultado


# --- Sem ambiente: o arquivo versionado. ---


def test_painel_tem_as_secoes_do_design() -> None:
    linhas = [p["title"] for p in _painel()["panels"] if p["type"] == "row"]

    assert [s for s in linhas if s in SECOES] == SECOES


@pytest.mark.parametrize("item", list(METRICAS_INFRA_8_3))
def test_cada_metrica_de_infra_8_3_esta_em_um_grafico(item: str) -> None:
    exprs = [alvo["expr"] for _, alvo in _consultas(_painel())]

    for metrica in METRICAS_INFRA_8_3[item]:
        padrao = re.compile(rf"\b{metrica}\b")
        assert any(padrao.search(e) for e in exprs), f"{item}: {metrica} sem gráfico"


def test_toda_consulta_usa_a_fonte_provisionada() -> None:
    painel = _painel()
    fontes = {alvo["datasource"]["uid"] for _, alvo in _consultas(painel)}
    fontes |= {p["datasource"]["uid"] for p in _graficos(painel)}

    assert fontes == {UID_FONTE}
    provisionada = (GRAFANA / "provisioning/datasources/prometheus.yml").read_text()
    assert f"uid: {UID_FONTE}" in provisionada


def test_graficos_nao_se_sobrepoem_e_cabem_na_grade() -> None:
    ocupadas: set[tuple[int, int]] = set()
    for p in _painel()["panels"]:
        g = p["gridPos"]
        assert g["x"] + g["w"] <= 24, p["title"]
        celulas = {
            (x, y)
            for x in range(g["x"], g["x"] + g["w"])
            for y in range(g["y"], g["y"] + g["h"])
        }
        assert not celulas & ocupadas, f"{p['title']} sobrepõe outro painel"
        ocupadas |= celulas


def test_graficos_estao_dentro_de_uma_secao() -> None:
    secoes = _secao_de_cada_grafico(_painel())

    assert all(secoes.values()), secoes


# --- Com o ambiente no ar: o Grafana. ---


def _id_do_conteiner(servico: str) -> str | None:
    resultado = subprocess.run(
        [
            "docker",
            "ps",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={PROJETO}",
            "--filter",
            f"label=com.docker.compose.service={servico}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return resultado.stdout.strip() or None


def _ler_env() -> dict[str, str]:
    variaveis: dict[str, str] = {}
    for linha in ENV.read_text().splitlines():
        linha = linha.strip()
        if linha and not linha.startswith("#") and "=" in linha:
            nome, valor = linha.split("=", 1)
            variaveis[nome.strip()] = valor.strip()
    return variaveis


def _motivo_para_pular() -> str | None:
    if shutil.which("docker") is None:
        return "docker ausente no PATH"
    if not ENV.is_file():
        return "infra/.env ausente: copiar de .env.example e preencher"
    ausentes = [s for s in ("prometheus", "grafana") if _id_do_conteiner(s) is None]
    if ausentes:
        return f"ambiente fora do ar ({', '.join(ausentes)}): ver docstring"
    return None


_MOTIVO = _motivo_para_pular()
no_ar = pytest.mark.skipif(_MOTIVO is not None, reason=_MOTIVO or "")


def _porta_no_host(servico: str, porta: str) -> int:
    resultado = subprocess.run(
        ["docker", "port", _id_do_conteiner(servico) or "", porta],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Uma só publicação, em 127.0.0.1 (001 FR-007; 007-T1).
    return int(resultado.stdout.split()[0].rsplit(":", 1)[1])


def _grafana(caminho: str, corpo: dict[str, Any] | None = None) -> Any:
    credencial = f"{USUARIO_ADMIN}:{_ler_env()['GRAFANA_SENHA_ADMIN']}"
    requisicao = urllib.request.Request(
        f"https://localhost:{_porta_no_host('grafana', '3000/tcp')}{caminho}",
        data=None if corpo is None else json.dumps(corpo).encode(),
        headers={
            "Authorization": "Basic " + base64.b64encode(credencial.encode()).decode(),
            "Content-Type": "application/json",
        },
    )
    contexto = ssl.create_default_context(cafile=str(AC))
    with urllib.request.urlopen(requisicao, context=contexto, timeout=30) as resposta:
        assert resposta.status == 200
        return json.loads(resposta.read())


@no_ar
def test_painel_carregado_pelo_provisionamento_na_subida() -> None:
    dados = _grafana(f"/api/dashboards/uid/{UID_PAINEL}")

    assert dados["meta"]["provisioned"] is True
    assert dados["meta"]["folderTitle"] == "CashFlow"
    assert dados["dashboard"]["title"] == "CashFlow"
    assert len(_graficos(dados["dashboard"])) == len(_graficos(_painel()))


@no_ar
def test_fonte_de_dados_provisionada_alcanca_o_prometheus() -> None:
    fonte = _grafana(f"/api/datasources/uid/{UID_FONTE}")
    saude = _grafana(f"/api/datasources/uid/{UID_FONTE}/health")

    assert fonte["readOnly"] is True
    assert fonte["isDefault"] is True
    assert saude["status"] == "OK", saude


def _valores(resposta: dict[str, Any]) -> list[float]:
    valores: list[float] = []
    for resultado in resposta["results"].values():
        assert resultado.get("status", 200) == 200, resultado
        assert "error" not in resultado, resultado["error"]
        for quadro in resultado.get("frames", []):
            # Coluna 0: instantes; demais: valores.
            for coluna in quadro["data"]["values"][1:]:
                valores.extend(v for v in coluna if v is not None)
    return valores


def _executar(alvo: dict[str, Any]) -> list[float]:
    consulta = {
        "refId": alvo["refId"],
        "datasource": alvo["datasource"],
        "expr": alvo["expr"],
        "range": True,
        "instant": False,
        "intervalMs": 15000,
        "maxDataPoints": 100,
    }
    resposta = _grafana(
        "/api/ds/query", {"queries": [consulta], "from": "now-5m", "to": "now"}
    )
    return _valores(resposta)


@pytest.fixture(scope="module")
def recusa_provocada() -> None:
    """Uma consulta sem token à API do consolidado, recusada com 401.

    A série de 401 do contador por status só nasce na primeira recusa
    (007 TD-005); sem ela o gráfico "Respostas 401 e 403" fica vazio, o que
    é correto num ambiente sem ataque, mas não prova que o gráfico funciona.
    """
    porta = _porta_no_host("consolidado-api", "8443/tcp")
    contexto = ssl.create_default_context(cafile=str(AC))
    url = f"https://localhost:{porta}/v1/consolidado/saldos/2026-10-01"
    with pytest.raises(urllib.error.HTTPError) as erro:
        urllib.request.urlopen(url, context=contexto, timeout=10)
    assert erro.value.code == 401


@no_ar
@pytest.mark.usefixtures("recusa_provocada")
@pytest.mark.parametrize(
    ("titulo", "alvo"),
    _consultas(_painel()),
    ids=[f"{t} {a['refId']}" for t, a in _consultas(_painel())],
)
def test_cada_consulta_do_painel_devolve_dados(
    titulo: str, alvo: dict[str, Any]
) -> None:
    limite = time.monotonic() + ESPERA_DADOS_SEGUNDOS
    valores = _executar(alvo)
    while not valores and time.monotonic() < limite:
        time.sleep(5)
        valores = _executar(alvo)

    assert valores, f"{titulo}: consulta sem dados: {alvo['expr']}"
