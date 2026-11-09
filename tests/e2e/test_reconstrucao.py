"""Reconstrução concorrente, troca, rollback e perda do RabbitMQ (005-T6;
FR-003, FR-007; INFRA 7.6, 9.6; design 005 §11).

O cenário roda sobre o compose real (`infra/compose.yaml`), com a cadeia
inteira no ar (livro, relay, RabbitMQ, worker, banco e API do consolidado e
Keycloak), e opera a projeção como o operador: pelos serviços sob o perfil
`operacao` (`coordenador`, `relay-republicar` e `livro-relatorio`), com as
identidades dedicadas (FR-008). As falhas são injetadas com o próprio
Docker: `stop`, `pause`, `rm` e remoção de volume (ST-Q-010).

1. Reconstrução concorrente e troca: com lançamentos sendo registrados o
   tempo todo, o operador cria a candidata, reabre a outbox desde a posição
   1 e troca a geração ativa assim que o critério FR-009 é atendido. A
   candidata recebe os eventos republicados e os novos (FR-007, TD-001).
2. Rollback controlado: a anterior, que seguiu recebendo eventos, volta a
   ser a ativa (FR-006); nova reconstrução é recusada até o descarte da
   anterior (D-005), que remove os dados dela.
3. Troca recusada sem mudar a ativa: candidata com lacuna (FR-009) e worker
   congelado (`pause`) que não confirma a barreira no tempo limite (design
   005 §8). A candidata é cancelada ao fim.
4. Perda do RabbitMQ (INFRA 7.6): mensagens publicadas e ainda não
   consumidas se perdem com o volume do broker. A reconciliação acusa as
   posições faltantes; a recuperação é a republicação da outbox com
   reconstrução e troca, e a reconciliação seguinte sai conciliada.

Durante os cenários 1 a 3, uma tarefa em segundo plano consulta a API do
consolidado sem parar. Ela confere uma data que só recebe lançamentos antes
da reconstrução: qualquer resposta que não seja 200 com os totais dessa
data é falha de consulta causada pela reconstrução (FR-003). Ao fim de cada
cenário, o saldo de cada data é conferido no livro, em cada geração
receptora no banco do consolidado e pela API, contra a soma calculada pelo
próprio teste, e a reconciliação com o relatório do livro sai conciliada.

Como nos outros testes de tests/e2e, o teste usa um projeto compose próprio
(`cashflow-e2e-reconstrucao`), com volumes próprios, portas efêmeras em
127.0.0.1 e esperas de retentativa curtas, removido ao fim com volumes e
imagens locais. Prometheus, Grafana e exporters não sobem. Os cenários
dependem da ordem do módulo: cada um parte das gerações deixadas pelo
anterior. A partir da raiz do repositório, com `infra/.env` preenchido e os
certificados gerados (001-T3):

    uv run --env-file infra/.env pytest tests/e2e/test_reconstrucao.py

Sem docker no PATH ou sem os segredos usados, o módulo é pulado com o motivo.
"""

import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

import pytest

RAIZ = Path(__file__).resolve().parents[2]
COMPOSE = RAIZ / "infra/compose.yaml"
AC = RAIZ / "infra/tls/gerados/ac/ac-cashflow.crt"
PROJETO = "cashflow-e2e-reconstrucao"
VOLUME_RABBITMQ = f"{PROJETO}_vol-rabbitmq"
REALM = "cashflow"
FILA = "consolidado.lancamentos"
SEGREDO_LANCAMENTOS = "KEYCLOAK_SEGREDO_CLIENTE_LANCAMENTOS"
SEGREDO_CONSULTAS = "KEYCLOAK_SEGREDO_CLIENTE_CONSULTAS"
SEGREDOS = {
    SEGREDO_LANCAMENTOS,
    SEGREDO_CONSULTAS,
    "PG_LIVRO_SENHA_REPLAY",
    "PG_CONSOLIDADO_SENHA_OPERACAO",
}

# Esperas curtas para relay e worker retomarem em segundos depois de cada
# falha e da barreira (o worker lê a barreira a cada intervalo de
# reconexão, 005-T2).
AMBIENTE_RECONSTRUCAO = {
    "RELAY_INTERVALO_SEGUNDOS": "0.5",
    "RELAY_ESPERA_BASE_SEGUNDOS": "0.5",
    "RELAY_ESPERA_TETO_SEGUNDOS": "2",
    "WORKER_INTERVALO_RECONEXAO_SEGUNDOS": "1",
    "COORDENADOR_BARREIRA_TEMPO_LIMITE_SEGUNDOS": "30",
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
OPERACAO = ["coordenador", "relay-republicar", "livro-relatorio"]

# Data que só recebe lançamentos no início: os totais dela não mudam durante
# a reconstrução, e a consulta contínua os confere (FR-003).
DATA_FIXA = "2026-10-01"
# Datas da carga, em rodízio e fora da ordem (lançamento retroativo).
DATAS = ["2026-11-05", "2026-10-30", "2026-11-03"]

pytestmark = [
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker ausente"),
    pytest.mark.skipif(
        not SEGREDOS <= set(os.environ),
        reason="segredos ausentes: executar com --env-file infra/.env (ver docstring)",
    ),
]


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


def _somar(lancamentos: list["Lancamento"]) -> dict[str, tuple[Decimal, Decimal]]:
    """(créditos, débitos) por data."""
    somas: dict[str, tuple[Decimal, Decimal]] = {}
    for lancamento in lancamentos:
        creditos, debitos = somas.get(lancamento.data, (Decimal(0), Decimal(0)))
        if lancamento.tipo == "CREDITO":
            creditos += lancamento.valor
        else:
            debitos += lancamento.valor
        somas[lancamento.data] = (creditos, debitos)
    return somas


@dataclass(frozen=True)
class Lancamento:
    id: str
    posicao: int
    tipo: str
    valor: Decimal
    data: str


@dataclass
class Ambiente:
    porta_keycloak: int
    porta_lancamentos: int
    porta_consolidado: int
    # Tudo o que a API de lançamentos confirmou com 201; a carga em segundo
    # plano grava aqui também, sob a trava.
    registrados: list[Lancamento] = field(default_factory=list)
    trava: threading.Lock = field(default_factory=threading.Lock)

    def compose(
        self, *args: str, timeout: int = 120, entrada: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            **AMBIENTE_RECONSTRUCAO,
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
            input=entrada,
        )

    def exigir(self, *args: str, timeout: int = 120) -> str:
        resultado = self.compose(*args, timeout=timeout)
        assert resultado.returncode == 0, (
            f"docker compose {' '.join(args)}:\n{resultado.stderr[-3000:]}"
        )
        return resultado.stdout

    def restaurar(self) -> None:
        """Tudo no ar e saudável, mesmo depois de um teste interrompido."""
        # Falha quando o worker não está pausado, o que é o normal.
        self.compose("unpause", "worker")
        self.exigir("up", "-d", "--wait", *SERVICOS, timeout=600)

    # Operação, pelos serviços do perfil `operacao`, como o operador.

    def operar(
        self, servico: str, *args: str, entrada: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self.compose(
            "--profile",
            "operacao",
            "run",
            "--rm",
            "-T",
            servico,
            *args,
            timeout=300,
            entrada=entrada,
        )

    def coordenador(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.operar("coordenador", *args)

    def exigir_coordenador(self, *args: str) -> str:
        resultado = self.coordenador(*args)
        assert resultado.returncode == 0, (
            f"coordenador {' '.join(args)}: {resultado.returncode}\n"
            f"{resultado.stdout[-2000:]}\n{resultado.stderr[-3000:]}"
        )
        return resultado.stdout

    def status(self) -> dict[str, Any]:
        documento: dict[str, Any] = json.loads(
            self.exigir_coordenador("reconstruir", "status", "--json", "--lacunas", "0")
        )
        return documento

    def geracoes(self) -> dict[str, int]:
        """Estado -> id das gerações não descartadas."""
        return {g["estado"]: g["id"] for g in self.status()["geracoes"]}

    def iniciar_reconstrucao(self) -> int:
        self.exigir_coordenador("reconstruir", "iniciar")
        geracoes = self.geracoes()
        assert set(geracoes) == {"ativa", "construindo"}, geracoes
        return geracoes["construindo"]

    def republicar(self) -> None:
        """Reabre a outbox desde a posição 1, no lado do livro (D-002)."""
        resultado = self.operar("relay-republicar", "--desde-posicao", "1")
        assert resultado.returncode == 0, resultado.stderr[-3000:]
        assert "eventos reabertos a partir da posição 1" in resultado.stdout

    def criterio_atendido(self) -> bool:
        criterio = self.status()["criterio_de_troca"]
        assert criterio is not None, "sem reconstrução em andamento"
        return bool(criterio["atendido"])

    def trocar(self) -> None:
        """Troca quando o critério é atendido; com a carga ativa, uma lacuna
        transitória na candidata no momento da barreira recusa a troca, e o
        operador tenta de novo (design 005 §3, passos 3 e 4)."""
        for _ in range(10):
            _aguardar(self.criterio_atendido, 180, "critério de troca atendido")
            resultado = self.coordenador("geracao", "trocar")
            if resultado.returncode == 0:
                return
            assert "critério de aceite" in resultado.stderr, resultado.stderr[-3000:]
        raise AssertionError("troca recusada em todas as tentativas")

    def relatorio_livro(self) -> str:
        resultado = self.operar("livro-relatorio")
        assert resultado.returncode == 0, resultado.stderr[-3000:]
        return resultado.stdout

    def reconciliar(self) -> tuple[int, dict[str, Any]]:
        """Relatório do livro entregue ao coordenador pela entrada padrão,
        sem rede entre os lados (INFRA 6.7)."""
        resultado = self.operar(
            "coordenador", "reconciliar", "--json", entrada=self.relatorio_livro()
        )
        assert resultado.returncode in (0, 3), resultado.stderr[-3000:]
        documento: dict[str, Any] = json.loads(resultado.stdout)
        return resultado.returncode, documento

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

    def da_geracao(self, geracao_id: int, tabela: str, colunas: str) -> list[list[str]]:
        return self.consolidado(
            f"SELECT {colunas} FROM {tabela} WHERE geracao_id = {int(geracao_id)}"
        )

    def controle(self) -> tuple[int, str]:
        [[ativa, barreira]] = self.consolidado(
            "SELECT geracao_ativa_id, barreira FROM controle_projecao"
        )
        return int(ativa), barreira

    def pendentes(self) -> int:
        linhas = self.livro("SELECT count(*) FROM outbox WHERE publicado_em IS NULL")
        return int(linhas[0][0])

    def mensagens_na_fila(self) -> int:
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
        )
        for linha in saida.splitlines():
            nome, prontas, nao_confirmadas = linha.split("\t")
            if nome == FILA:
                return int(prontas) + int(nao_confirmadas)
        raise AssertionError(f"fila {FILA} ausente:\n{saida}")

    def aguardar_escoamento(self) -> None:
        _aguardar(lambda: self.pendentes() == 0, 120, "outbox publicada")
        _aguardar(lambda: self.mensagens_na_fila() == 0, 120, "fila esvaziada")

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

    def registrar_um(self, token: str, rotulo: str, data: str | None = None) -> None:
        with self.trava:
            indice = len(self.registrados)
        tipo = "CREDITO" if indice % 2 else "DEBITO"
        valor = Decimal(indice % 37 + 1) + Decimal(indice % 100) / 100
        data = data or DATAS[indice % len(DATAS)]
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
            **{"Idempotency-Key": f"e2e-reconstrucao-{uuid.uuid4()}"},
        )
        assert status == 201, (status, corpo)
        lancamento = Lancamento(corpo["id"], corpo["posicao"], tipo, valor, data)
        with self.trava:
            self.registrados.append(lancamento)

    def registrar(
        self, token: str, quantidade: int, rotulo: str, data: str | None = None
    ) -> None:
        for _ in range(quantidade):
            self.registrar_um(token, rotulo, data)

    def total(self) -> int:
        with self.trava:
            return len(self.registrados)

    def consultar(self, token: str, caminho: str) -> tuple[int, Any]:
        return self.api(self.porta_consolidado, "GET", caminho, token)

    def saldos_da_geracao(self, geracao_id: int) -> dict[str, tuple[Decimal, Decimal]]:
        """Saldo materializado de cada data: (créditos, débitos)."""
        saldos = {}
        for data, creditos, debitos, saldo_dia in self.da_geracao(
            geracao_id, "saldo_diario", "data, total_creditos, total_debitos, saldo_dia"
        ):
            assert Decimal(saldo_dia) == Decimal(creditos) - Decimal(debitos)
            saldos[data] = (Decimal(creditos), Decimal(debitos))
        return saldos

    def conferir(self, *geracoes: int) -> None:
        """Saldo de cada data igual ao livro na ativa, em cada geração dada e
        pela API; nenhum evento perdido nem duplicado; reconciliação ok."""
        self.aguardar_escoamento()
        total = self.total()
        ativa, barreira = self.controle()
        assert barreira == "liberada"
        conferidas = {ativa, *geracoes}
        for geracao in conferidas:

            def inbox_completa(geracao: int = geracao) -> bool:
                return len(self.da_geracao(geracao, "inbox", "evento_id")) == total

            _aguardar(
                inbox_completa, 60, f"inbox da geração {geracao} com todos os eventos"
            )

        with self.trava:
            registrados = list(self.registrados)
        esperado = _somar(registrados)

        # O livro guardou exatamente o que a API confirmou, sem buraco.
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
        assert sorted(lancamento.posicao for lancamento in registrados) == list(
            range(1, total + 1)
        )

        eventos = {linha[0] for linha in self.livro("SELECT evento_id FROM outbox")}
        ids = {lancamento.id for lancamento in registrados}
        for geracao in conferidas:
            assert self.saldos_da_geracao(geracao) == no_livro, geracao
            assert {
                linha[0] for linha in self.da_geracao(geracao, "inbox", "evento_id")
            } == eventos, geracao
            assert {
                linha[0]
                for linha in self.da_geracao(geracao, "contribuicao", "lancamento_id")
            } == ids, geracao
            assert self.da_geracao(geracao, "checkpoint", "posicao_continua") == [
                [str(total)]
            ], geracao
            assert self.da_geracao(geracao, "lacuna", "posicao") == [], geracao
        assert self.consolidado("SELECT count(*) FROM quarentena") == [["0"]]

        # O mesmo saldo pela API, da geração ativa, com acumulado e checkpoint.
        token = self.token_consultas()
        acumulado = Decimal(0)
        for data in sorted(no_livro):
            creditos, debitos = no_livro[data]
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

        # Reconciliação da ativa com o relatório do livro (FR-001).
        codigo, reconciliacao = self.reconciliar()
        assert (codigo, reconciliacao["conciliada"]) == (0, True), reconciliacao
        assert reconciliacao["geracao_id"] == ativa
        assert reconciliacao["ate_posicao"] == total
        assert reconciliacao["datas_no_livro"] == len(no_livro)


class EmSegundoPlano:
    """Repete `passo` numa thread até `parar`; guarda a primeira exceção."""

    def __init__(self, passo: Callable[[], None]) -> None:
        self._passo = passo
        self._parar = threading.Event()
        self._thread = threading.Thread(target=self._executar, daemon=True)
        self.erro: Exception | None = None
        self.execucoes = 0

    def _executar(self) -> None:
        while not self._parar.is_set():
            try:
                self._passo()
            except (AssertionError, OSError, ValueError, KeyError) as erro:
                # Reportada por `parar`, na thread do teste.
                self.erro = erro
                return
            self.execucoes += 1
            time.sleep(0.05)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.parar()

    def parar(self) -> None:
        self._parar.set()
        self._thread.join(timeout=60)
        # Reportada uma vez só: `parar` explícito e depois o `with`.
        erro, self.erro = self.erro, None
        if erro is not None:
            raise AssertionError(f"falha em segundo plano: {erro!r}") from erro


def carga(ambiente: Ambiente, rotulo: str) -> EmSegundoPlano:
    """Registro contínuo de lançamentos nas datas da carga."""
    token = ambiente.token_lancamentos()
    return EmSegundoPlano(lambda: ambiente.registrar_um(token, rotulo))


def consultas(ambiente: Ambiente) -> EmSegundoPlano:
    """Consulta contínua da API (FR-003): a data fixa com os totais dela e
    uma data da carga, que muda, só pelo status."""
    with ambiente.trava:
        fixa = _somar([x for x in ambiente.registrados if x.data == DATA_FIXA])
    creditos, debitos = fixa[DATA_FIXA]
    token = ambiente.token_consultas()

    def consultar() -> None:
        status, corpo = ambiente.consultar(token, f"/v1/consolidado/saldos/{DATA_FIXA}")
        assert status == 200, (DATA_FIXA, status, corpo)
        assert (
            Decimal(corpo["total_creditos"]),
            Decimal(corpo["total_debitos"]),
        ) == (creditos, debitos), corpo
        status, corpo = ambiente.consultar(token, f"/v1/consolidado/saldos/{DATAS[0]}")
        assert status == 200, (DATAS[0], status, corpo)

    return EmSegundoPlano(consultar)


@pytest.fixture(scope="module")
def ambiente() -> Iterator[Ambiente]:
    ambiente = Ambiente(
        porta_keycloak=_porta_livre(),
        porta_lancamentos=_porta_livre(),
        porta_consolidado=_porta_livre(),
    )
    try:
        # Bancos primeiro, migrações do livro (002-T2) e do consolidado
        # (004-T1), as imagens de operação e só então a cadeia, aguardando
        # todos os health checks.
        ambiente.exigir("up", "-d", "--wait", "pg-livro", "pg-consolidado", timeout=300)
        ambiente.exigir("run", "--rm", "--build", "livro-migracao", timeout=600)
        ambiente.exigir("run", "--rm", "--build", "consolidado-migracao", timeout=600)
        ambiente.exigir("--profile", "operacao", "build", *OPERACAO, timeout=900)
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


def test_reconstrucao_concorrente_e_troca(ambiente: Ambiente) -> None:
    token = ambiente.token_lancamentos()
    ambiente.registrar(token, 6, "data fixa", DATA_FIXA)
    ambiente.registrar(token, 24, "base")
    ambiente.conferir()
    inicial, _ = ambiente.controle()
    assert ambiente.geracoes() == {"ativa": inicial}

    with consultas(ambiente) as leitura, carga(ambiente, "concorrente") as escrita:
        candidata = ambiente.iniciar_reconstrucao()
        antes_do_replay = ambiente.total()
        # A candidata nasce vazia e a API segue na ativa (FR-003).
        _aguardar(lambda: ambiente.total() >= antes_do_replay + 20, 60, "carga")
        ambiente.republicar()
        ambiente.trocar()
        trocou_em = ambiente.total()
        # A carga segue depois da troca: a ativa nova e a anterior recebem.
        _aguardar(lambda: ambiente.total() >= trocou_em + 10, 60, "carga")
        escrita.parar()
        ambiente.aguardar_escoamento()
        leitura.parar()

    assert ambiente.controle() == (candidata, "liberada")
    assert ambiente.geracoes() == {"ativa": candidata, "anterior": inicial}
    # Houve registro concorrente durante toda a reconstrução (FR-007) e
    # consulta durante ela e durante a troca, sem falha (FR-003).
    assert trocou_em - antes_do_replay >= 20
    assert escrita.execucoes >= 30
    assert leitura.execucoes > 0
    ambiente.conferir(inicial, candidata)
    fases = {g["id"]: g["fase"] for g in ambiente.status()["geracoes"]}
    assert fases[candidata] == "concluida"


def test_rollback_controlado(ambiente: Ambiente) -> None:
    geracoes = ambiente.geracoes()
    assert set(geracoes) == {"ativa", "anterior"}, geracoes
    reconstruida, anterior = geracoes["ativa"], geracoes["anterior"]

    # A anterior seguiu recebendo eventos depois da troca (TD-001).
    ambiente.registrar(ambiente.token_lancamentos(), 10, "antes do rollback")
    ambiente.conferir(anterior)

    # Nova reconstrução é recusada enquanto a anterior existir (D-005).
    recusada = ambiente.coordenador("reconstruir", "iniciar")
    assert recusada.returncode == 1, recusada.stderr[-3000:]
    assert ambiente.geracoes() == geracoes

    with consultas(ambiente) as leitura:
        ambiente.exigir_coordenador("geracao", "rollback")
        leitura.parar()
    assert leitura.execucoes > 0

    assert ambiente.controle() == (anterior, "liberada")
    assert ambiente.geracoes() == {"ativa": anterior, "anterior": reconstruida}
    ambiente.registrar(ambiente.token_lancamentos(), 5, "depois do rollback")
    ambiente.conferir(reconstruida)

    # Descarte da anterior: dados removidos e nova reconstrução liberada.
    ambiente.exigir_coordenador("geracao", "descartar")
    assert ambiente.geracoes() == {"ativa": anterior}
    for tabela in ("saldo_diario", "contribuicao", "inbox", "marcador_inclusao"):
        assert ambiente.da_geracao(reconstruida, tabela, "count(*)") == [["0"]]
    ambiente.conferir()


def test_troca_recusada_sem_mudar_a_ativa(ambiente: Ambiente) -> None:
    ativa, _ = ambiente.controle()
    assert ambiente.geracoes() == {"ativa": ativa}
    token = ambiente.token_lancamentos()

    with consultas(ambiente) as leitura:
        candidata = ambiente.iniciar_reconstrucao()
        # Sem republicação, a candidata recebe só o evento novo: todas as
        # posições abaixo dele ficam em lacuna.
        ambiente.registrar(token, 1, "lacuna na candidata")
        ambiente.aguardar_escoamento()
        total = ambiente.total()
        _aguardar(
            lambda: (
                len(ambiente.da_geracao(candidata, "lacuna", "posicao")) == total - 1
            ),
            60,
            "lacunas na candidata",
        )
        assert not ambiente.criterio_atendido()

        # Critério FR-009 não atendido no momento da barreira.
        recusada = ambiente.coordenador("geracao", "trocar")
        assert recusada.returncode == 1, recusada.stderr[-3000:]
        assert "critério de aceite" in recusada.stderr
        assert "lacunas" in recusada.stderr
        assert ambiente.controle() == (ativa, "liberada")
        assert ambiente.geracoes() == {"ativa": ativa, "construindo": candidata}

        # Worker congelado com a fila parada: não confirma a pausa e a troca
        # é cancelada no tempo limite, com a barreira liberada.
        ambiente.exigir("pause", "worker")
        try:
            recusada = ambiente.coordenador("geracao", "trocar", "--tempo-limite", "3")
        finally:
            ambiente.exigir("unpause", "worker")
        assert recusada.returncode == 1, recusada.stderr[-3000:]
        assert "barreira não confirmada" in recusada.stderr
        assert ambiente.controle() == (ativa, "liberada")
        assert ambiente.geracoes() == {"ativa": ativa, "construindo": candidata}
        leitura.parar()
    assert leitura.execucoes > 0

    # O worker retoma o consumo; a candidata é cancelada e os dados dela,
    # removidos.
    ambiente.registrar(token, 3, "depois da barreira")
    ambiente.conferir()
    ambiente.exigir_coordenador("reconstruir", "cancelar")
    assert ambiente.geracoes() == {"ativa": ativa}
    assert ambiente.da_geracao(candidata, "contribuicao", "count(*)") == [["0"]]


def test_recuperacao_apos_perda_do_rabbitmq(ambiente: Ambiente) -> None:
    ativa, _ = ambiente.controle()
    assert ambiente.geracoes() == {"ativa": ativa}
    token = ambiente.token_lancamentos()
    antes = ambiente.total()

    # Eventos publicados e ainda na fila, sem consumidor.
    ambiente.exigir("stop", "worker")
    ambiente.registrar(token, 15, "perdido com o broker")
    _aguardar(lambda: ambiente.pendentes() == 0, 60, "eventos publicados")
    assert ambiente.mensagens_na_fila() == 15

    # Perda dos dados do broker: contêiner e volume removidos. Ele volta
    # vazio, só com a topologia das definições (003-T5).
    ambiente.exigir("stop", "rabbitmq")
    ambiente.exigir("rm", "-f", "rabbitmq")
    removido = subprocess.run(
        ["docker", "volume", "rm", VOLUME_RABBITMQ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert removido.returncode == 0, removido.stderr
    ambiente.exigir("up", "-d", "--wait", "rabbitmq", "relay", "worker", timeout=300)
    assert ambiente.mensagens_na_fila() == 0

    # Os eventos seguintes chegam; os perdidos ficam em lacuna na ativa.
    ambiente.registrar(token, 5, "depois da perda")
    ambiente.aguardar_escoamento()
    _aguardar(
        lambda: len(ambiente.da_geracao(ativa, "contribuicao", "1")) == antes + 5,
        60,
        "eventos depois da perda projetados",
    )

    # A reconciliação acusa as posições faltantes e as datas divergentes.
    codigo, reconciliacao = ambiente.reconciliar()
    assert codigo == 3, reconciliacao
    assert reconciliacao["conciliada"] is False
    assert reconciliacao["posicoes_faltantes"] == 15
    assert reconciliacao["primeiros_faltantes"] == [
        {"inicio": antes + 1, "fim": antes + 15}
    ]
    assert reconciliacao["divergencias"]

    # Recuperação (INFRA 7.6): reconstrução a partir da outbox republicada.
    candidata = ambiente.iniciar_reconstrucao()
    ambiente.republicar()
    ambiente.trocar()
    assert ambiente.controle() == (candidata, "liberada")
    # A ativa de antes também recebeu a republicação e ficou completa.
    ambiente.conferir(ativa)

    ambiente.exigir_coordenador("geracao", "descartar")
    assert ambiente.geracoes() == {"ativa": candidata}
    ambiente.conferir()
