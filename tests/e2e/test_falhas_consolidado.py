"""Testes de falha do consolidado (004-T7; NFR-002, US-004; FR-001 a FR-003;
INFRA 4.8, 4.10, 9.6).

O cenário roda sobre o compose real (`infra/compose.yaml`), com a cadeia
inteira no ar (livro, relay, RabbitMQ, worker, banco e API do consolidado e
Keycloak), e injeta as falhas com o próprio Docker: `stop`, `kill` e `pause`
(ST-Q-010). Cada teste registra lançamentos pela API, provoca uma falha e,
depois do retorno, confere que o saldo de cada data é a soma dos lançamentos
registrados e que nenhum evento foi descartado:

1. Queda do consolidado: primeiro só o banco (o worker pausa o consumo e
   fica não pronto, design 004 §8, e retoma sozinho), depois banco, worker
   e API juntos. Os eventos esperam na fila.
2. Queda do consumidor: o worker é morto (`kill`) com mensagens já entregues
   e sem ACK; o broker as devolve à fila e o novo worker as recebe como
   reentrega.
3. Perda de ACK: o worker grava o efeito e envia o ACK com o RabbitMQ
   congelado (`pause`); o broker é morto antes de ler o ACK. Ao voltar, a
   mensagem ainda está na fila com o efeito já gravado; a inbox a reconhece
   como duplicata (TD-001, TD-002).
4. Duplicação: duas republicações completas da outbox (serviço
   `relay-republicar`, 003 TD-004) põem duas cópias de cada evento na fila;
   todas são confirmadas sem efeito.
5. RabbitMQ indisponível: lançamentos continuam registrados, os eventos
   ficam na outbox e o worker não pronto; com o retorno, tudo é projetado.
6. Keycloak indisponível: com o JWKS em cache (TTL de 300 s), tokens já
   emitidos continuam aceitos nas duas APIs (002 design §8) e a projeção não
   depende do Keycloak.

"Nenhum evento descartado" é conferido pelo banco: todo `evento_id` da
outbox está na inbox da geração ativa, todo lançamento tem contribuição e
marcador, a quarentena está vazia, o checkpoint é a última posição sem
lacunas e a fila termina vazia. O saldo é conferido no livro, no banco do
consolidado e pela API, contra a soma calculada pelo próprio teste.

Como nos outros testes de tests/e2e, o teste usa um projeto compose próprio
(`cashflow-e2e-consolidado`), com volumes próprios, portas efêmeras em
127.0.0.1, `prefetch` fixo e esperas de retentativa curtas, removido ao fim
com volumes e imagens locais. Prometheus, Grafana e exporters não sobem. A
partir da raiz do repositório, com `infra/.env` preenchido e os
certificados gerados (001-T3):

    uv run --env-file infra/.env pytest tests/e2e/test_falhas_consolidado.py

Sem docker no PATH ou sem os segredos usados, o módulo é pulado com o motivo.
"""

import json
import os
import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

RAIZ = Path(__file__).resolve().parents[2]
COMPOSE = RAIZ / "infra/compose.yaml"
AC = RAIZ / "infra/tls/gerados/ac/ac-cashflow.crt"
PROJETO = "cashflow-e2e-consolidado"
REALM = "cashflow"
FILA = "consolidado.lancamentos"
SEGREDO_LANCAMENTOS = "KEYCLOAK_SEGREDO_CLIENTE_LANCAMENTOS"
SEGREDO_CONSULTAS = "KEYCLOAK_SEGREDO_CLIENTE_CONSULTAS"

# `prefetch` fixo: o teste da queda do consumidor conta quantas mensagens o
# worker segura sem ACK (DQ-003). Esperas curtas para relay e worker
# retomarem em segundos depois de cada falha.
PREFETCH = 10
AMBIENTE_FALHAS = {
    "RELAY_INTERVALO_SEGUNDOS": "0.5",
    "RELAY_ESPERA_BASE_SEGUNDOS": "0.5",
    "RELAY_ESPERA_TETO_SEGUNDOS": "2",
    "WORKER_PREFETCH": str(PREFETCH),
    "WORKER_INTERVALO_RECONEXAO_SEGUNDOS": "1",
}

# A cadeia do lançamento até a consulta; sem a observabilidade (spec 007).
SERVICOS = [
    "pg-livro",
    "pg-consolidado",
    "rabbitmq",
    "keycloak",
    "lancamentos-api",
    "relay",
    "worker",
    "consolidado-api",
]
PAUSAVEIS = ["worker", "rabbitmq"]

# Datas passadas, em rodízio: cada cenário toca mais de um dia e todos
# chegam fora da ordem das datas (lançamento retroativo, 002 D-005).
DATAS = ["2026-11-05", "2026-10-30", "2026-11-03"]

pytestmark = [
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker ausente"),
    pytest.mark.skipif(
        not {SEGREDO_LANCAMENTOS, SEGREDO_CONSULTAS} <= set(os.environ),
        reason="segredos ausentes: executar com --env-file infra/.env (ver docstring)",
    ),
]

# Executado dentro do contêiner do worker: status e corpo de uma URL interna.
_LER_URL = """
import sys, urllib.error, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=5) as resposta:
        print(resposta.status); print(resposta.read().decode())
except urllib.error.HTTPError as erro:
    print(erro.code); print(erro.read().decode())
"""


def _porta_livre() -> int:
    with socket.socket() as soquete:
        soquete.bind(("127.0.0.1", 0))
        porta: int = soquete.getsockname()[1]
        return porta


def _aguardar(
    condicao: Callable[[], bool], limite_segundos: float, motivo: str
) -> None:
    fim = time.monotonic() + limite_segundos
    while not condicao():
        assert time.monotonic() < fim, f"tempo esgotado: {motivo}"
        time.sleep(1)


@dataclass(frozen=True)
class Lancamento:
    id: str
    posicao: int
    tipo: str
    valor: Decimal
    data: str


@dataclass(frozen=True)
class Fila:
    prontas: int
    nao_confirmadas: int
    consumidores: int

    @property
    def mensagens(self) -> int:
        return self.prontas + self.nao_confirmadas


@dataclass
class Ambiente:
    porta_keycloak: int
    porta_lancamentos: int
    porta_consolidado: int
    # Tudo o que a API de lançamentos confirmou com 201, em ordem.
    registrados: list[Lancamento] = field(default_factory=list)

    def compose(
        self, *args: str, timeout: int = 120
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            **AMBIENTE_FALHAS,
            "KEYCLOAK_PORTA_HOST": str(self.porta_keycloak),
            "LANCAMENTOS_API_PORTA_HOST": str(self.porta_lancamentos),
            "CONSOLIDADO_API_PORTA_HOST": str(self.porta_consolidado),
        }
        return subprocess.run(
            ["docker", "compose", "-p", PROJETO, "-f", str(COMPOSE), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

    def exigir(self, *args: str, timeout: int = 120) -> str:
        resultado = self.compose(*args, timeout=timeout)
        assert resultado.returncode == 0, (
            f"docker compose {' '.join(args)}:\n{resultado.stderr[-3000:]}"
        )
        return resultado.stdout

    def restaurar(self) -> None:
        """Tudo no ar e saudável, mesmo depois de um teste interrompido."""
        for servico in PAUSAVEIS:
            # Falha quando o serviço não está pausado, o que é o normal.
            self.compose("unpause", servico)
        self.exigir("up", "-d", "--wait", *SERVICOS, timeout=600)

    # Conferência nos bancos: soquete local do contêiner, como superusuário,
    # só leitura (os papéis da aplicação não leem tudo o que é conferido).

    def _sql(self, servico: str, banco: str, consulta: str) -> list[list[str]]:
        saida = self.exigir(
            "exec",
            "-T",
            servico,
            "psql",
            "-U",
            "postgres",
            "-d",
            banco,
            "-X",
            "-A",
            "-t",
            "-F",
            "|",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            consulta,
        )
        return [linha.split("|") for linha in saida.splitlines() if linha]

    def livro(self, consulta: str) -> list[list[str]]:
        return self._sql("pg-livro", "livro", consulta)

    def consolidado(self, consulta: str) -> list[list[str]]:
        return self._sql("pg-consolidado", "consolidado", consulta)

    def da_geracao_ativa(self, tabela: str, colunas: str) -> list[list[str]]:
        return self.consolidado(
            f"SELECT {colunas} FROM {tabela} t"
            " JOIN controle_projecao c ON t.geracao_id = c.geracao_ativa_id"
        )

    def pendentes(self) -> int:
        linhas = self.livro("SELECT count(*) FROM outbox WHERE publicado_em IS NULL")
        return int(linhas[0][0])

    def eventos_de(self, lancamentos: list[Lancamento]) -> set[str]:
        ids = ", ".join(f"'{uuid.UUID(lancamento.id)}'" for lancamento in lancamentos)
        consulta = f"SELECT evento_id FROM outbox WHERE lancamento_id IN ({ids})"
        return {linha[0] for linha in self.livro(consulta)}

    def contribuicoes(self) -> set[str]:
        return {
            linha[0]
            for linha in self.da_geracao_ativa("contribuicao", "t.lancamento_id")
        }

    def fila(self) -> Fila:
        saida = self.exigir(
            "exec",
            "-T",
            "rabbitmq",
            "rabbitmqctl",
            "list_queues",
            "--quiet",
            "--no-table-headers",
            "name",
            "messages_ready",
            "messages_unacknowledged",
            "consumers",
        )
        for linha in saida.splitlines():
            nome, *numeros = linha.split("\t")
            if nome == FILA:
                prontas, nao_confirmadas, consumidores = map(int, numeros)
                return Fila(prontas, nao_confirmadas, consumidores)
        raise AssertionError(f"fila {FILA} ausente:\n{saida}")

    # Worker: health e /metrics em portas internas, lidos de dentro dele.

    def url_no_worker(self, url: str) -> tuple[int, str]:
        saida = self.exigir("exec", "-T", "worker", "python", "-c", _LER_URL, url)
        status, corpo = saida.split("\n", 1)
        return int(status), corpo

    def ready_do_worker(self) -> int:
        return self.url_no_worker("http://127.0.0.1:8080/health/ready")[0]

    def metrica_do_worker(self, nome: str) -> float:
        _, corpo = self.url_no_worker("http://127.0.0.1:9464/metrics")
        for linha in corpo.splitlines():
            if linha.startswith(nome + " "):
                return float(linha.rsplit(" ", 1)[1])
        raise AssertionError(f"métrica ausente: {nome}")

    def desfechos(self) -> Counter[tuple[str, str]]:
        """(evento_id, desfecho) de cada mensagem processada, pelos logs."""
        saida = self.exigir("logs", "--no-log-prefix", "worker", timeout=60)
        contagem: Counter[tuple[str, str]] = Counter()
        for linha in saida.splitlines():
            try:
                registro = json.loads(linha)
            except ValueError:
                continue
            if isinstance(registro, dict) and "desfecho" in registro:
                contagem[(registro["evento_id"], registro["desfecho"])] += 1
        return contagem

    # HTTP pelas portas publicadas em loopback, confiando na AC local.

    def contexto_tls(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(AC))

    def token(self, cliente: str, segredo: str) -> str:
        campos = {
            "grant_type": "client_credentials",
            "client_id": cliente,
            "client_secret": os.environ[segredo],
        }
        requisicao = urllib.request.Request(
            f"https://localhost:{self.porta_keycloak}/realms/{REALM}"
            "/protocol/openid-connect/token",
            data=urllib.parse.urlencode(campos).encode(),
            method="POST",
        )
        with urllib.request.urlopen(
            requisicao, context=self.contexto_tls(), timeout=10
        ) as resposta:
            token: str = json.load(resposta)["access_token"]
            return token

    def token_lancamentos(self) -> str:
        return self.token("cliente-lancamentos", SEGREDO_LANCAMENTOS)

    def token_consultas(self) -> str:
        return self.token("cliente-consultas", SEGREDO_CONSULTAS)

    def api(
        self,
        porta: int,
        metodo: str,
        caminho: str,
        token: str,
        corpo: object = None,
        **cabecalhos: str,
    ) -> tuple[int, Any]:
        dados = None if corpo is None else json.dumps(corpo).encode()
        if dados is not None:
            cabecalhos["Content-Type"] = "application/json"
        requisicao = urllib.request.Request(
            f"https://localhost:{porta}{caminho}",
            data=dados,
            method=metodo,
            headers={**cabecalhos, "Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(
                requisicao, context=self.contexto_tls(), timeout=30
            ) as resposta:
                return resposta.status, json.load(resposta)
        except urllib.error.HTTPError as erro:
            return erro.code, json.loads(erro.read() or b"null")

    def registrar(self, token: str, quantidade: int, rotulo: str) -> list[Lancamento]:
        novos = []
        for _ in range(quantidade):
            indice = len(self.registrados)
            tipo = "CREDITO" if indice % 2 else "DEBITO"
            valor = Decimal(indice % 37 + 1) + Decimal(indice % 100) / 100
            data = DATAS[indice % len(DATAS)]
            status, corpo = self.api(
                self.porta_lancamentos,
                "POST",
                "/v1/lancamentos",
                token,
                {
                    "tipo": tipo,
                    "valor": f"{valor:.2f}",
                    "data_lancamento": data,
                    "descricao": f"{rotulo} {indice}",
                },
                **{"Idempotency-Key": f"e2e-falhas-consolidado-{uuid.uuid4()}"},
            )
            assert status == 201, (status, corpo)
            lancamento = Lancamento(corpo["id"], corpo["posicao"], tipo, valor, data)
            self.registrados.append(lancamento)
            novos.append(lancamento)
        return novos

    def consultar(self, token: str, caminho: str) -> tuple[int, Any]:
        return self.api(self.porta_consolidado, "GET", caminho, token)

    def conferir(self, token_consultas: str | None = None) -> None:
        """Saldo final igual à soma dos registrados; nenhum evento descartado."""
        total = len(self.registrados)
        _aguardar(lambda: self.pendentes() == 0, 120, "outbox publicada")
        _aguardar(lambda: self.fila().mensagens == 0, 120, "fila esvaziada")
        _aguardar(
            lambda: len(self.da_geracao_ativa("inbox", "t.evento_id")) == total,
            60,
            "inbox com todos os eventos",
        )

        esperado: dict[str, tuple[Decimal, Decimal]] = {}
        for lancamento in self.registrados:
            creditos, debitos = esperado.get(lancamento.data, (Decimal(0), Decimal(0)))
            if lancamento.tipo == "CREDITO":
                creditos += lancamento.valor
            else:
                debitos += lancamento.valor
            esperado[lancamento.data] = (creditos, debitos)

        # O livro guardou exatamente o que a API confirmou.
        no_livro = {
            data: (Decimal(creditos), Decimal(debitos))
            for data, creditos, debitos in self.livro(
                "SELECT data_lancamento,"
                " coalesce(sum(valor) FILTER (WHERE tipo = 'CREDITO'), 0),"
                " coalesce(sum(valor) FILTER (WHERE tipo = 'DEBITO'), 0)"
                " FROM lancamento GROUP BY data_lancamento"
            )
        }
        assert no_livro == esperado

        # Saldo materializado da geração ativa, sem duplicar nem perder.
        no_consolidado = {}
        for data, creditos, debitos, saldo_dia in self.da_geracao_ativa(
            "saldo_diario", "t.data, t.total_creditos, t.total_debitos, t.saldo_dia"
        ):
            assert Decimal(saldo_dia) == Decimal(creditos) - Decimal(debitos)
            no_consolidado[data] = (Decimal(creditos), Decimal(debitos))
        assert no_consolidado == esperado

        # O mesmo saldo pela API, com acumulado e checkpoint (FR-004).
        token = token_consultas or self.token_consultas()
        acumulado = Decimal(0)
        for data in sorted(esperado):
            creditos, debitos = esperado[data]
            acumulado += creditos - debitos
            status, corpo = self.consultar(token, f"/v1/consolidado/saldos/{data}")
            assert status == 200, (data, status, corpo)
            assert {
                chave: Decimal(corpo[chave])
                for chave in (
                    "total_creditos",
                    "total_debitos",
                    "saldo_dia",
                    "saldo_acumulado",
                )
            } == {
                "total_creditos": creditos,
                "total_debitos": debitos,
                "saldo_dia": creditos - debitos,
                "saldo_acumulado": acumulado,
            }, data
            assert corpo["posicao_checkpoint"] == total

        # Nenhum evento descartado (INFRA 4.10): cada evento da outbox na
        # inbox, cada lançamento com contribuição e marcador, quarentena
        # vazia e checkpoint contínuo até a última posição (TD-006).
        na_inbox = {linha[0] for linha in self.da_geracao_ativa("inbox", "t.evento_id")}
        assert na_inbox == {
            linha[0] for linha in self.livro("SELECT evento_id FROM outbox")
        }
        ids = {lancamento.id for lancamento in self.registrados}
        assert self.contribuicoes() == ids
        assert {
            linha[0]
            for linha in self.da_geracao_ativa("marcador_inclusao", "t.lancamento_id")
        } == ids
        assert self.consolidado("SELECT count(*) FROM quarentena") == [["0"]]
        assert self.da_geracao_ativa("checkpoint", "t.posicao_continua") == [
            [str(total)]
        ]
        assert self.da_geracao_ativa("lacuna", "t.posicao") == []
        assert sorted(lancamento.posicao for lancamento in self.registrados) == list(
            range(1, total + 1)
        )

    def conferir_inclusao(
        self, lancamentos: list[Lancamento], token_consultas: str | None = None
    ) -> None:
        token = token_consultas or self.token_consultas()
        for lancamento in lancamentos:
            status, corpo = self.consultar(
                token, f"/v1/consolidado/inclusoes/{lancamento.id}"
            )
            assert (status, corpo) == (
                200,
                {"incluido": True, "posicao": lancamento.posicao},
            )


@pytest.fixture(scope="module")
def ambiente() -> Iterator[Ambiente]:
    ambiente = Ambiente(
        porta_keycloak=_porta_livre(),
        porta_lancamentos=_porta_livre(),
        porta_consolidado=_porta_livre(),
    )
    try:
        # Bancos primeiro, migrações do livro (002-T2) e do consolidado
        # (004-T1) e só então a cadeia, aguardando todos os health checks.
        ambiente.exigir("up", "-d", "--wait", "pg-livro", "pg-consolidado", timeout=300)
        ambiente.exigir("run", "--rm", "--build", "livro-migracao", timeout=600)
        ambiente.exigir("run", "--rm", "--build", "consolidado-migracao", timeout=600)
        ambiente.exigir("up", "-d", "--build", "--wait", *SERVICOS, timeout=900)
        yield ambiente
    finally:
        ambiente.compose(
            "--profile",
            "migracao",
            "--profile",
            "operacao",
            "down",
            "-v",
            "--rmi",
            "local",
            "--remove-orphans",
            timeout=300,
        )


@pytest.fixture(autouse=True)
def _restaurar_ao_fim(ambiente: Ambiente) -> Iterator[None]:
    # Cada cenário começa com a cadeia inteira no ar, mesmo se o anterior
    # falhou no meio da falha injetada.
    yield
    ambiente.restaurar()


def test_cadeia_projeta_lancamentos_antes_das_falhas(ambiente: Ambiente) -> None:
    lancamentos = ambiente.registrar(ambiente.token_lancamentos(), 12, "base")

    ambiente.conferir()
    ambiente.conferir_inclusao(lancamentos)
    assert ambiente.ready_do_worker() == 200


def test_queda_do_consolidado(ambiente: Ambiente) -> None:
    token = ambiente.token_lancamentos()

    # Só o banco fora: o worker deixa a fila e fica não pronto, sem
    # confirmar nada (design 004 §8).
    ambiente.exigir("stop", "pg-consolidado")
    _aguardar(lambda: ambiente.fila().consumidores == 0, 60, "consumo pausado")
    _aguardar(lambda: ambiente.ready_do_worker() == 503, 30, "worker não pronto")
    com_banco_fora = ambiente.registrar(token, 15, "queda do banco do consolidado")
    _aguardar(lambda: ambiente.pendentes() == 0, 60, "eventos publicados")
    assert ambiente.fila() == Fila(prontas=15, nao_confirmadas=0, consumidores=0)

    # O worker retoma sozinho quando o banco volta.
    ambiente.exigir("up", "-d", "--wait", "pg-consolidado", timeout=300)
    _aguardar(
        lambda: ambiente.fila() == Fila(prontas=0, nao_confirmadas=0, consumidores=1),
        120,
        "backlog projetado depois do retorno do banco",
    )

    # O consolidado inteiro fora: banco, worker e API.
    ambiente.exigir("stop", "consolidado-api", "worker", "pg-consolidado")
    com_consolidado_fora = ambiente.registrar(token, 15, "queda do consolidado")
    _aguardar(lambda: ambiente.pendentes() == 0, 60, "eventos publicados")
    assert ambiente.fila() == Fila(prontas=15, nao_confirmadas=0, consumidores=0)

    ambiente.exigir(
        "up", "-d", "--wait", "pg-consolidado", "worker", "consolidado-api", timeout=300
    )

    ambiente.conferir()
    ambiente.conferir_inclusao(com_banco_fora + com_consolidado_fora)


def test_queda_do_consumidor_com_mensagens_sem_ack(ambiente: Ambiente) -> None:
    token = ambiente.token_lancamentos()
    quantidade = 3 * PREFETCH

    # Worker congelado: o broker entrega até `prefetch` mensagens, que ficam
    # no soquete do worker sem processamento nem ACK.
    ambiente.exigir("pause", "worker")
    lancamentos = ambiente.registrar(token, quantidade, "queda do consumidor")
    _aguardar(lambda: ambiente.pendentes() == 0, 60, "eventos publicados")
    _aguardar(
        lambda: (
            ambiente.fila()
            == Fila(
                prontas=quantidade - PREFETCH, nao_confirmadas=PREFETCH, consumidores=1
            )
        ),
        30,
        "mensagens entregues ao worker congelado",
    )

    # Queda do consumidor: SIGKILL, sem desligamento ordenado.
    ambiente.exigir("kill", "worker")
    _aguardar(
        lambda: ambiente.fila()
        == Fila(prontas=quantidade, nao_confirmadas=0, consumidores=0),
        30,
        "mensagens sem ACK devolvidas à fila",
    )
    assert not {lancamento.id for lancamento in lancamentos} & ambiente.contribuicoes()

    ambiente.exigir("up", "-d", "--wait", "worker", timeout=300)

    ambiente.conferir()
    ambiente.conferir_inclusao(lancamentos)
    # As devolvidas chegaram ao novo processo marcadas como reentrega.
    assert ambiente.metrica_do_worker("cashflow_worker_reentregas_total") == PREFETCH
    desfechos = ambiente.desfechos()
    for evento in ambiente.eventos_de(lancamentos):
        assert desfechos[(evento, "aplicado")] == 1, evento


def test_perda_de_ack_depois_do_commit(ambiente: Ambiente) -> None:
    token = ambiente.token_lancamentos()
    quantidade = PREFETCH // 2

    # Mensagens entregues ao worker congelado, ainda sem processamento.
    ambiente.exigir("pause", "worker")
    lancamentos = ambiente.registrar(token, quantidade, "perda de ack")
    _aguardar(lambda: ambiente.pendentes() == 0, 60, "eventos publicados")
    _aguardar(
        lambda: (
            ambiente.fila()
            == Fila(prontas=0, nao_confirmadas=quantidade, consumidores=1)
        ),
        30,
        "mensagens entregues ao worker congelado",
    )

    # Broker congelado e worker de volta: o worker grava o efeito e envia o
    # ACK, que fica no soquete do broker sem ser lido.
    ambiente.exigir("pause", "rabbitmq")
    ambiente.exigir("unpause", "worker")
    ids = {lancamento.id for lancamento in lancamentos}
    _aguardar(lambda: ids <= ambiente.contribuicoes(), 30, "efeito gravado")

    # O broker morre sem processar os ACKs. O worker é parado para que a
    # fila possa ser conferida antes do novo consumo.
    ambiente.exigir("kill", "rabbitmq")
    ambiente.exigir("stop", "worker")
    ambiente.exigir("up", "-d", "--wait", "rabbitmq", timeout=300)

    # ACK perdido: efeito gravado e mensagem ainda na fila (persistente).
    _aguardar(
        lambda: ambiente.fila()
        == Fila(prontas=quantidade, nao_confirmadas=0, consumidores=0),
        30,
        "mensagens com ACK perdido de volta à fila",
    )
    assert ids <= ambiente.contribuicoes()

    ambiente.exigir("up", "-d", "--wait", "worker", timeout=300)

    ambiente.conferir()
    ambiente.conferir_inclusao(lancamentos)
    # Cada evento aplicado uma vez; a reentrega foi reconhecida pela inbox.
    desfechos = ambiente.desfechos()
    for evento in ambiente.eventos_de(lancamentos):
        assert desfechos[(evento, "aplicado")] == 1, evento
        assert desfechos[(evento, "evento_duplicado")] == 1, evento


def test_duplicacao_de_eventos(ambiente: Ambiente) -> None:
    total = len(ambiente.registrados)
    assert total > 0

    # Duas republicações completas com o worker parado: a fila recebe pelo
    # menos duas cópias de cada evento, com o mesmo `evento_id`.
    ambiente.exigir("stop", "worker")
    for _ in range(2):
        saida = ambiente.exigir(
            "--profile",
            "operacao",
            "run",
            "--rm",
            "-T",
            "relay-republicar",
            "--desde-posicao",
            "1",
            timeout=300,
        )
        assert f"{total} eventos reabertos a partir da posição 1" in saida
        _aguardar(lambda: ambiente.pendentes() == 0, 120, "republicação publicada")
    copias = ambiente.fila()
    assert copias.prontas >= 2 * total
    assert copias.nao_confirmadas == copias.consumidores == 0

    antes = ambiente.desfechos()
    ambiente.exigir("up", "-d", "--wait", "worker", timeout=300)

    ambiente.conferir()
    # Toda cópia foi confirmada como duplicata, sem efeito nem quarentena.
    novos = ambiente.desfechos() - antes
    assert {desfecho for _, desfecho in novos} == {"evento_duplicado"}
    assert sum(novos.values()) == copias.prontas
    assert {evento for evento, _ in novos} == ambiente.eventos_de(ambiente.registrados)


def test_rabbitmq_indisponivel(ambiente: Ambiente) -> None:
    token = ambiente.token_lancamentos()

    ambiente.exigir("stop", "rabbitmq")
    _aguardar(lambda: ambiente.ready_do_worker() == 503, 30, "worker não pronto")

    # O registro segue; os eventos esperam na outbox (FR-005 da 003).
    lancamentos = ambiente.registrar(token, 15, "rabbitmq indisponível")
    assert ambiente.pendentes() == 15
    assert not {lancamento.id for lancamento in lancamentos} & ambiente.contribuicoes()

    # Relay e worker reconectam sozinhos.
    ambiente.exigir("up", "-d", "--wait", "rabbitmq", timeout=300)
    _aguardar(lambda: ambiente.ready_do_worker() == 200, 60, "worker pronto")

    ambiente.conferir()
    ambiente.conferir_inclusao(lancamentos)


def test_keycloak_indisponivel(ambiente: Ambiente) -> None:
    # APIs reiniciadas leem o JWKS no primeiro ready: o cache vale 300 s
    # (JWT_JWKS_TTL_SEGUNDOS), mais que o cenário. Tokens emitidos agora
    # também valem 300 s (accessTokenLifespan do realm).
    ambiente.exigir("restart", "lancamentos-api", "consolidado-api")
    ambiente.exigir(
        "up", "-d", "--wait", "lancamentos-api", "consolidado-api", timeout=300
    )
    token_lancamentos = ambiente.token_lancamentos()
    token_consultas = ambiente.token_consultas()

    ambiente.exigir("stop", "keycloak")
    # Controle positivo: nenhum token novo é emitido.
    with pytest.raises(urllib.error.URLError):
        ambiente.token_consultas()

    lancamentos = ambiente.registrar(token_lancamentos, 10, "keycloak indisponível")
    ambiente.conferir(token_consultas)
    ambiente.conferir_inclusao(lancamentos, token_consultas)

    ambiente.exigir("up", "-d", "--wait", "keycloak", timeout=300)

    # De volta, com tokens novos.
    ambiente.conferir()
