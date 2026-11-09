"""Regras de alerta de FR-006 (007-T4; design 007 TD-004).

Três partes:

- Sem ambiente (só docker): o `promtool` da própria imagem do Prometheus
  valida `infra/observabilidade/regras.yml` e roda testes de unidade das
  regras com séries sintéticas: cada alerta dispara acima do limite e não
  abaixo, e mudar o número do limite muda o disparo (limite configurável).
- Com o ambiente `cashflow` no ar: o Prometheus carregou o grupo de regras,
  sem erro de avaliação, e as séries de limite têm dado.
- Quarentena injetada: num projeto compose próprio (`cashflow-alertas`),
  só com RabbitMQ, banco e worker do consolidado e Prometheus, uma mensagem
  ilegível publicada na fila vai para a quarentena e dispara o alerta
  CashFlowQuarentena. O projeto próprio evita deixar uma linha de quarentena
  permanente (a tabela é só de inserção) e o alerta disparado no ambiente
  `cashflow`; é removido ao fim, com volumes e imagens locais.

A partir da raiz do repositório, com `infra/.env` preenchido:

    docker compose -f infra/compose.yaml up -d --wait
    uv run pytest tests/observabilidade/test_regras.py

O host não alcança o Prometheus (007 TD-007): a API dele é consultada de um
contêiner avulso na `rede-observabilidade` do projeto. Sem docker, sem
`infra/.env` ou sem o ambiente `cashflow` no ar, a parte correspondente é
pulada com o motivo.
"""

import json
import os
import shutil
import subprocess
import time
import urllib.parse
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

RAIZ = Path(__file__).resolve().parents[2]
COMPOSE = RAIZ / "infra/compose.yaml"
ENV = RAIZ / "infra/.env"
REGRAS = RAIZ / "infra/observabilidade/regras.yml"
PROJETO = "cashflow"
PROJETO_ALERTAS = "cashflow-alertas"

ALERTAS = {"CashFlowOutboxEventoAntigo", "CashFlowQuarentena", "CashFlowLacunas"}
LIMITES = {
    "cashflow:limite_outbox_idade_mais_antigo_segundos": "300",
    "cashflow:limite_quarentena": "0",
    "cashflow:limite_lacunas": "0",
}

# Uma coleta (15 s) e uma avaliação (15 s), com folga.
ESPERA_ALERTA_SEGUNDOS = 120


def _docker(*args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], check=False, capture_output=True, text=True, timeout=timeout
    )


def _imagem(servico: str) -> str:
    """Imagem fixada no compose (001 TD-007), lida do próprio arquivo."""
    resultado = _docker(
        "compose", "-f", str(COMPOSE), "config", "--images", servico, timeout=60
    )
    assert resultado.returncode == 0, resultado.stderr
    return resultado.stdout.strip()


def _aguardar(condicao: Callable[[], bool], motivo: str) -> None:
    limite = time.monotonic() + ESPERA_ALERTA_SEGUNDOS
    while not condicao():
        assert time.monotonic() < limite, f"tempo esgotado: {motivo}"
        time.sleep(3)


# --- Sem ambiente: promtool. ---

_SEM_DOCKER = shutil.which("docker") is None
_SEM_ENV = not ENV.is_file()
precisa_docker = pytest.mark.skipif(
    _SEM_DOCKER or _SEM_ENV,
    reason="docker ausente no PATH ou infra/.env ausente (o compose o exige)",
)


def _promtool(diretorio: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _docker(
        "run",
        "--rm",
        "--network",
        "none",
        # Usuário do host: o diretório temporário do pytest tem modo 0700, e
        # o usuário padrão da imagem (nobody) não o lê.
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-v",
        f"{diretorio}:/regras:ro",
        "-w",
        "/regras",
        "--entrypoint",
        "promtool",
        _imagem("prometheus"),
        *args,
    )


def _alertas_disparados(
    instante: str, alerta: str, *rotulos: dict[str, str]
) -> dict[str, Any]:
    """Caso de `promql_expr_test`: instâncias em firing do alerta no instante.

    Compara pela série ALERTS, em vez de `alert_rule_test`, para não repetir
    nos testes o texto das anotações.
    """
    amostras = [
        {
            "labels": "ALERTS{"
            + ",".join(
                f'{k}="{v}"'
                for k, v in sorted(
                    {"alertname": alerta, "alertstate": "firing", **r}.items()
                )
            )
            + "}",
            "value": 1,
        }
        for r in rotulos
    ]
    return {
        "expr": f'ALERTS{{alertname="{alerta}",alertstate="firing"}}',
        "eval_time": instante,
        "exp_samples": amostras,
    }


def _testar_regras(
    tmp_path: Path, series: list[dict[str, str]], casos: list[dict[str, Any]]
) -> subprocess.CompletedProcess[str]:
    # JSON é YAML válido: sem dependência de biblioteca YAML.
    teste = {
        "rule_files": ["regras.yml"],
        "evaluation_interval": "15s",
        "tests": [
            {"interval": "15s", "input_series": series, "promql_expr_test": casos}
        ],
    }
    (tmp_path / "teste.yml").write_text(json.dumps(teste, ensure_ascii=False))
    return _promtool(tmp_path, "test", "rules", "teste.yml")


@pytest.fixture
def regras(tmp_path: Path) -> Path:
    shutil.copy(REGRAS, tmp_path / "regras.yml")
    return tmp_path


@precisa_docker
def test_promtool_valida_as_regras() -> None:
    resultado = _promtool(REGRAS.parent, "check", "rules", REGRAS.name)

    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    assert "SUCCESS: 6 rules found" in resultado.stdout


@precisa_docker
def test_idade_do_evento_mais_antigo_dispara_acima_do_limite(regras: Path) -> None:
    # Idade sobe 30 s a cada coleta: 240 s em 2 min, 360 s em 3 min.
    serie = {
        "series": 'cashflow_outbox_idade_mais_antigo_segundos{job="relay"}',
        "values": "0+30x20",
    }
    casos = [
        _alertas_disparados("2m", "CashFlowOutboxEventoAntigo"),
        _alertas_disparados(
            "3m", "CashFlowOutboxEventoAntigo", {"severidade": "aviso"}
        ),
    ]

    resultado = _testar_regras(regras, [serie], casos)

    assert resultado.returncode == 0, resultado.stdout + resultado.stderr


@precisa_docker
def test_limite_da_idade_e_configuravel(regras: Path) -> None:
    # Mesmo arquivo com o limite de 300 s trocado por 60 s: 120 s em 1 min
    # passa a disparar.
    arquivo = regras / "regras.yml"
    texto = arquivo.read_text()
    assert texto.count("vector(300)") == 1
    arquivo.write_text(texto.replace("vector(300)", "vector(60)"))
    serie = {
        "series": 'cashflow_outbox_idade_mais_antigo_segundos{job="relay"}',
        "values": "0+30x20",
    }
    casos = [
        _alertas_disparados(
            "1m", "CashFlowOutboxEventoAntigo", {"severidade": "aviso"}
        ),
        {
            "expr": "cashflow:limite_outbox_idade_mais_antigo_segundos",
            "eval_time": "1m",
            "exp_samples": [
                {
                    "labels": "cashflow:limite_outbox_idade_mais_antigo_segundos",
                    "value": 60,
                }
            ],
        },
    ]

    resultado = _testar_regras(regras, [serie], casos)

    assert resultado.returncode == 0, resultado.stdout + resultado.stderr


@precisa_docker
def test_quarentena_maior_que_zero_dispara(regras: Path) -> None:
    serie = {
        "series": 'cashflow_quarentena_total{job="worker"}',
        "values": "0x4 1x8",
    }
    casos = [
        _alertas_disparados("45s", "CashFlowQuarentena"),
        _alertas_disparados("75s", "CashFlowQuarentena", {"severidade": "critico"}),
    ]

    resultado = _testar_regras(regras, [serie], casos)

    assert resultado.returncode == 0, resultado.stdout + resultado.stderr


@precisa_docker
def test_lacunas_maiores_que_zero_disparam_depois_de_um_minuto(regras: Path) -> None:
    # Lacunas na geração 2 a partir de 75 s; `for: 1m` espera até 135 s.
    series = [
        {"series": 'cashflow_lacunas{job="worker",geracao="1"}', "values": "0x20"},
        {"series": 'cashflow_lacunas{job="worker",geracao="2"}', "values": "0x4 2x16"},
    ]
    casos = [
        _alertas_disparados("90s", "CashFlowLacunas"),
        _alertas_disparados(
            "150s", "CashFlowLacunas", {"geracao": "2", "severidade": "aviso"}
        ),
    ]

    resultado = _testar_regras(regras, series, casos)

    assert resultado.returncode == 0, resultado.stdout + resultado.stderr


@precisa_docker
def test_solucao_saudavel_nao_dispara_alerta(regras: Path) -> None:
    series = [
        {
            "series": 'cashflow_outbox_idade_mais_antigo_segundos{job="relay"}',
            "values": "0+2x20",
        },
        {"series": 'cashflow_quarentena_total{job="worker"}', "values": "0x20"},
        {"series": 'cashflow_lacunas{job="worker",geracao="1"}', "values": "0x20"},
    ]
    casos = [{"expr": "ALERTS", "eval_time": "5m", "exp_samples": []}]

    resultado = _testar_regras(regras, series, casos)

    assert resultado.returncode == 0, resultado.stdout + resultado.stderr


# --- Com o ambiente `cashflow` no ar. ---


def _id_do_conteiner(projeto: str, servico: str) -> str | None:
    resultado = _docker(
        "ps",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={projeto}",
        "--filter",
        f"label=com.docker.compose.service={servico}",
    )
    return resultado.stdout.strip() or None


def _consultar(projeto: str, caminho: str) -> dict[str, Any]:
    resultado = _docker(
        "run",
        "--rm",
        "--pull=never",
        "--network",
        f"{projeto}_rede-observabilidade",
        # Imagem Alpine do PostgreSQL, fixada no compose: o wget do BusyBox
        # dela resolve nomes pelo DNS do Docker (como em test_alvos.py).
        _imagem("pg-livro"),
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


def _consulta(projeto: str, expr: str) -> list[dict[str, Any]]:
    caminho = "/api/v1/query?query=" + urllib.parse.quote(expr)
    resultado: list[dict[str, Any]] = _consultar(projeto, caminho)["data"]["result"]
    return resultado


def _disparados(projeto: str) -> set[str]:
    alertas = _consultar(projeto, "/api/v1/alerts")["data"]["alerts"]
    return {a["labels"]["alertname"] for a in alertas if a["state"] == "firing"}


no_ar = pytest.mark.skipif(
    _SEM_DOCKER or _id_do_conteiner(PROJETO, "prometheus") is None,
    reason="ambiente cashflow fora do ar: ver docstring",
)


@no_ar
def test_prometheus_do_ambiente_carregou_as_regras() -> None:
    regras: dict[str, dict[str, Any]] = {}

    def avaliadas() -> bool:
        grupos = _consultar(PROJETO, "/api/v1/rules")["data"]["groups"]
        (grupo,) = [g for g in grupos if g["name"] == "cashflow"]
        regras.update({r["name"]: r for r in grupo["rules"]})
        # `unknown` até a primeira avaliação depois da subida.
        return all(r["health"] != "unknown" for r in regras.values())

    _aguardar(avaliadas, "primeira avaliação das regras")
    assert set(regras) == ALERTAS | set(LIMITES)
    assert {r["type"] for n, r in regras.items() if n in ALERTAS} == {"alerting"}
    assert {nome: r["health"] for nome, r in regras.items()} == dict.fromkeys(
        regras, "ok"
    )


@no_ar
@pytest.mark.parametrize(("limite", "valor"), LIMITES.items())
def test_series_de_limite_tem_dado(limite: str, valor: str) -> None:
    serie: list[dict[str, Any]] = []

    def gravada() -> bool:
        serie[:] = _consulta(PROJETO, limite)
        return bool(serie)

    _aguardar(gravada, f"{limite} gravada")
    assert [s["value"][1] for s in serie] == [valor]


# --- Quarentena injetada, em projeto compose próprio. ---


class AmbienteAlertas:
    def compose(self, *args: str, timeout: int = 120) -> str:
        resultado = _docker(
            "compose",
            "-p",
            PROJETO_ALERTAS,
            "-f",
            str(COMPOSE),
            *args,
            timeout=timeout,
        )
        assert resultado.returncode == 0, (
            f"docker compose {' '.join(args)}:\n{resultado.stderr[-3000:]}"
        )
        return resultado.stdout

    def quarentena_no_banco(self) -> int:
        saida = self.compose(
            "exec",
            "-T",
            "pg-consolidado",
            "psql",
            "-U",
            "postgres",
            "-d",
            "consolidado",
            "-X",
            "-A",
            "-t",
            "-c",
            "SELECT count(*) FROM quarentena",
        )
        return int(saida.strip())

    def publicar_mensagem_ilegivel(self) -> None:
        """Corpo que não é JSON, publicado como o relay publica (003-T2):
        exchange, routing key, `mandatory` e confirm. O worker a guarda na
        quarentena como ilegível (004 FR-003)."""
        script = """
import asyncio, os, uuid
import aio_pika

async def publicar():
    conexao = await aio_pika.connect(
        host="rabbitmq", login="relay", password=os.environ["RABBITMQ_SENHA_RELAY"]
    )
    async with conexao:
        canal = await conexao.channel(publisher_confirms=True)
        exchange = await canal.get_exchange("cashflow.lancamentos", ensure=False)
        await exchange.publish(
            aio_pika.Message(
                body=b"isto nao e um evento",
                message_id=str(uuid.uuid4()),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key="lancamento.registrado.v1",
            mandatory=True,
        )

asyncio.run(publicar())
"""
        # Imagem do relay, sem subir o relay nem o banco do livro.
        self.compose("run", "--rm", "--no-deps", "-T", "relay", "python", "-c", script)


@pytest.fixture(scope="module")
def ambiente_alertas() -> Iterator[AmbienteAlertas]:
    ambiente = AmbienteAlertas()
    try:
        ambiente.compose(
            "up", "-d", "--wait", "pg-consolidado", "rabbitmq", timeout=300
        )
        ambiente.compose("run", "--rm", "--build", "consolidado-migracao", timeout=600)
        ambiente.compose("build", "relay", timeout=600)
        ambiente.compose(
            "up", "-d", "--build", "--wait", "worker", "prometheus", timeout=600
        )
        yield ambiente
    finally:
        _docker(
            "compose",
            "-p",
            PROJETO_ALERTAS,
            "-f",
            str(COMPOSE),
            "--profile",
            "migracao",
            "down",
            "-v",
            "--rmi",
            "local",
            "--remove-orphans",
            timeout=300,
        )


@precisa_docker
def test_quarentena_injetada_dispara_o_alerta(
    ambiente_alertas: AmbienteAlertas,
) -> None:
    def worker_coletado_sem_quarentena() -> bool:
        return [
            s["value"][1]
            for s in _consulta(PROJETO_ALERTAS, "sum(cashflow_quarentena_total)")
        ] == ["0"] and bool(_consulta(PROJETO_ALERTAS, "cashflow:limite_quarentena"))

    _aguardar(worker_coletado_sem_quarentena, "worker coletado com quarentena zero")
    assert "CashFlowQuarentena" not in _disparados(PROJETO_ALERTAS)
    assert ambiente_alertas.quarentena_no_banco() == 0

    ambiente_alertas.publicar_mensagem_ilegivel()

    _aguardar(
        lambda: "CashFlowQuarentena" in _disparados(PROJETO_ALERTAS),
        "CashFlowQuarentena disparado",
    )
    assert ambiente_alertas.quarentena_no_banco() == 1
    # Só a regra da quarentena: idade e lacunas seguem abaixo do limite.
    assert _disparados(PROJETO_ALERTAS) == {"CashFlowQuarentena"}
