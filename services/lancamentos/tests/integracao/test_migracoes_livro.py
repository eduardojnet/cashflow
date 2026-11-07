"""Migrações do livro e privilégios contra o PostgreSQL real (002-T2).

O módulo aplica as migrações pelo serviço `livro-migracao` do compose, com o
papel livro_migracao, e confere esquema e privilégios conectando como cada
papel. O banco não publica porta no host (INFRA 6.2), então o psql roda
dentro do contêiner, mas conecta pelo nome do serviço, o que exige a senha do
papel (mesmo caminho de tests/infra/test_papeis_banco.py). Com o ambiente no
ar, a partir da raiz do repositório:

    docker compose -f infra/compose.yaml up -d --wait
    uv run pytest services/lancamentos/tests/integracao

Cada sonda de escrita roda numa transação desfeita com ROLLBACK: o livro não
fica com dados de teste. Sem o ambiente no ar, o módulo é pulado com o motivo
explícito.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJETO = "cashflow"
RAIZ = Path(__file__).resolve().parents[4]
COMPOSE = RAIZ / "infra/compose.yaml"
ENV = RAIZ / "infra/.env"
SERVICO = "pg-livro"
BANCO = "livro"
SENHAS = {
    "livro_migracao": "PG_LIVRO_SENHA_MIGRACAO",
    "livro_api": "PG_LIVRO_SENHA_API",
    "livro_relay": "PG_LIVRO_SENHA_RELAY",
    "livro_replay": "PG_LIVRO_SENHA_REPLAY",
}
TABELAS = ["lancamento", "contador_posicao", "resposta_idempotente", "outbox"]

# Uma linha válida de cada tabela, para as sondas de privilégio e restrição.
# Posição no topo do bigint e identificadores fixos para não colidir com
# lançamentos reais gravados no mesmo banco.
LANCAMENTO = (
    "INSERT INTO lancamento"
    " (id, posicao, tipo, valor, data_lancamento, descricao, cliente_id)"
    " VALUES ('00000000-0000-0000-0000-000000000001', 9223372036854775807,"
    " 'CREDITO', 10.50,"
    " '2026-11-06', 'sonda', 'cliente-sonda')"
)
OUTBOX = (
    "INSERT INTO outbox (evento_id, lancamento_id, posicao, tipo_evento, payload)"
    " VALUES ('00000000-0000-0000-0000-0000000000e1',"
    " '00000000-0000-0000-0000-000000000001', 9223372036854775807,"
    " 'LancamentoRegistrado', '{}')"
)
RESPOSTA = (
    "INSERT INTO resposta_idempotente"
    " (cliente_id, chave, hash_requisicao, status_http, corpo)"
    " VALUES ('cliente-sonda', 'chave-sonda', 'h', 201, '{}')"
)


def _docker(
    *args: str, env: dict[str, str] | None = None, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _id_do_conteiner() -> str | None:
    resultado = _docker(
        "ps",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={PROJETO}",
        "--filter",
        f"label=com.docker.compose.service={SERVICO}",
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
    if _id_do_conteiner() is None:
        return f"ambiente fora do ar ({SERVICO}): ver docstring"
    return None


_MOTIVO = _motivo_para_pular()
pytestmark = pytest.mark.skipif(_MOTIVO is not None, reason=_MOTIVO or "")


def _psql(papel: str, sql: str) -> subprocess.CompletedProcess[str]:
    """Executa `sql` como `papel`, autenticando por senha na rede interna."""
    id_ = _id_do_conteiner()
    assert id_ is not None, f"{SERVICO} não está em execução"
    env = {**os.environ, "PGPASSWORD": _ler_env()[SENHAS[papel]]}
    return _docker(
        "exec",
        "-e",
        "PGPASSWORD",
        id_,
        "psql",
        "-X",
        "-At",
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        SERVICO,
        "-U",
        papel,
        "-d",
        BANCO,
        "-c",
        sql,
        env=env,
    )


def _em_rollback(*comandos: str) -> str:
    return "BEGIN; " + "; ".join(comandos) + "; ROLLBACK;"


@pytest.fixture(scope="module", autouse=True)
def migracoes_aplicadas() -> None:
    """Aplica as migrações como no uso real; reaplicar em head não muda nada."""
    resultado = _docker(
        "compose",
        "-f",
        str(COMPOSE),
        "run",
        "--rm",
        "--build",
        "livro-migracao",
        timeout=600,
    )
    assert resultado.returncode == 0, resultado.stderr


def test_migracao_fica_em_head_aplicada_por_livro_migracao() -> None:
    resultado = _psql(
        "livro_migracao",
        "SELECT version_num FROM alembic_version",
    )

    assert resultado.returncode == 0, resultado.stderr
    assert resultado.stdout.strip() == "0004"


def test_tabelas_do_livro_pertencem_a_livro_migracao() -> None:
    resultado = _psql(
        "livro_migracao",
        "SELECT tablename || '=' || tableowner FROM pg_tables"
        " WHERE schemaname = 'public' ORDER BY tablename",
    )

    assert resultado.returncode == 0, resultado.stderr
    donos = dict(linha.split("=") for linha in resultado.stdout.split())
    assert donos == {
        tabela: "livro_migracao" for tabela in [*TABELAS, "alembic_version"]
    }


def test_contador_de_posicao_tem_linha_unica() -> None:
    resultado = _psql(
        "livro_migracao",
        _em_rollback(
            "SELECT count(*) FROM contador_posicao WHERE id = 1",
            "INSERT INTO contador_posicao (id, valor) VALUES (2, 0)",
        ),
    )

    assert resultado.returncode != 0
    assert resultado.stdout.splitlines() == ["BEGIN", "1"]
    assert "contador_posicao_id_check" in resultado.stderr


def test_livro_api_insere_e_le_lancamento() -> None:
    resultado = _psql(
        "livro_api",
        _em_rollback(
            LANCAMENTO,
            "SELECT tipo || ' ' || valor || ' ' || data_lancamento FROM lancamento"
            " WHERE posicao = 9223372036854775807",
        ),
    )

    assert resultado.returncode == 0, resultado.stderr
    assert "CREDITO 10.50 2026-11-06" in resultado.stdout.splitlines()


@pytest.mark.parametrize(
    "comando",
    [
        "UPDATE lancamento SET valor = 1",
        "DELETE FROM lancamento",
        "TRUNCATE lancamento",
    ],
    ids=["update", "delete", "truncate"],
)
def test_livro_api_nao_altera_nem_apaga_lancamento(comando: str) -> None:
    resultado = _psql("livro_api", _em_rollback(LANCAMENTO, comando))

    assert resultado.returncode != 0
    assert "permission denied for table lancamento" in resultado.stderr


def test_livro_api_registra_transacao_completa() -> None:
    """Contador, lançamento, outbox e resposta: o que a transação de T3 usa."""
    resultado = _psql(
        "livro_api",
        _em_rollback(
            "UPDATE contador_posicao SET valor = valor + 1 WHERE id = 1"
            " RETURNING valor",
            LANCAMENTO,
            OUTBOX,
            RESPOSTA,
            "SELECT status_http FROM resposta_idempotente"
            " WHERE cliente_id = 'cliente-sonda' AND chave = 'chave-sonda'",
        ),
    )

    assert resultado.returncode == 0, resultado.stderr
    assert resultado.stdout.splitlines()[-2:] == ["201", "ROLLBACK"]


@pytest.mark.parametrize(
    ("comando", "tabela"),
    [
        ("DELETE FROM contador_posicao", "contador_posicao"),
        ("UPDATE contador_posicao SET id = 1", "contador_posicao"),
        ("UPDATE resposta_idempotente SET status_http = 200", "resposta_idempotente"),
        ("DELETE FROM resposta_idempotente", "resposta_idempotente"),
        ("SELECT * FROM outbox", "outbox"),
        ("UPDATE outbox SET publicado_em = now()", "outbox"),
        ("DELETE FROM outbox", "outbox"),
    ],
    ids=[
        "contador-delete",
        "contador-update-id",
        "resposta-update",
        "resposta-delete",
        "outbox-select",
        "outbox-update",
        "outbox-delete",
    ],
)
def test_livro_api_nao_tem_privilegio_alem_do_registro(
    comando: str, tabela: str
) -> None:
    resultado = _psql("livro_api", comando)

    assert resultado.returncode != 0
    assert f"permission denied for table {tabela}" in resultado.stderr


@pytest.mark.parametrize(
    ("papel", "tabela"),
    [
        *(("livro_relay", tabela) for tabela in TABELAS if tabela != "outbox"),
        *(
            ("livro_replay", tabela)
            for tabela in TABELAS
            if tabela not in ("outbox", "lancamento")
        ),
    ],
)
def test_outros_papeis_ainda_nao_acessam_as_tabelas(papel: str, tabela: str) -> None:
    # Privilégios concedidos pelas migrações das specs 003 e 005; livro_relay
    # lê a outbox desde a 0002 (003-T1), livro_replay lê `posicao` da outbox
    # desde a 0003 (003-T4) e as colunas do relatório do livro desde a 0004
    # (005-T5).
    resultado = _psql(papel, f"SELECT 1 FROM {tabela} LIMIT 1")

    assert resultado.returncode != 0
    assert f"permission denied for table {tabela}" in resultado.stderr


def test_livro_replay_le_as_colunas_do_relatorio_do_livro() -> None:
    resultado = _psql(
        "livro_replay",
        "SELECT count(*), coalesce(max(posicao), 0) >= 0,"
        " count(DISTINCT data_lancamento) >= 0, coalesce(sum(valor), 0) >= 0"
        " FROM lancamento WHERE tipo IN ('CREDITO', 'DEBITO')",
    )

    assert resultado.returncode == 0, resultado.stderr
    assert resultado.stdout.strip().split("|")[1:] == ["t", "t", "t"]


@pytest.mark.parametrize(
    "comando",
    [
        "SELECT descricao FROM lancamento",
        "SELECT cliente_id FROM lancamento",
        "SELECT * FROM lancamento",
        LANCAMENTO,
        "UPDATE lancamento SET valor = 1",
        "DELETE FROM lancamento",
    ],
    ids=["descricao", "cliente", "todas-as-colunas", "insert", "update", "delete"],
)
def test_livro_replay_nao_le_alem_do_relatorio_nem_escreve_no_livro(
    comando: str,
) -> None:
    # Migração 0004 (005-T5): só posição, tipo, valor e data.
    resultado = _psql("livro_replay", _em_rollback(comando))

    assert resultado.returncode != 0
    assert "permission denied for table lancamento" in resultado.stderr


@pytest.mark.parametrize(
    ("valores", "restricao"),
    [
        ("1, 'ESTORNO', 10.00", "lancamento_tipo_check"),
        ("1, 'DEBITO', 0", "lancamento_valor_check"),
        ("1, 'DEBITO', -5.00", "lancamento_valor_check"),
        ("0, 'DEBITO', 5.00", "lancamento_posicao_check"),
    ],
    ids=["tipo-invalido", "valor-zero", "valor-negativo", "posicao-zero"],
)
def test_restricoes_do_lancamento_recusam_linha_invalida(
    valores: str, restricao: str
) -> None:
    resultado = _psql(
        "livro_api",
        _em_rollback(
            "INSERT INTO lancamento"
            " (id, posicao, tipo, valor, data_lancamento, cliente_id)"
            f" VALUES (gen_random_uuid(), {valores}, '2026-11-06', 'cliente-sonda')"
        ),
    )

    assert resultado.returncode != 0
    assert restricao in resultado.stderr


def test_posicao_e_unica_no_livro() -> None:
    segundo = LANCAMENTO.replace(
        "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"
    )
    resultado = _psql("livro_api", _em_rollback(LANCAMENTO, segundo))

    assert resultado.returncode != 0
    assert "lancamento_posicao_key" in resultado.stderr


def test_evento_da_outbox_tem_a_posicao_do_lancamento() -> None:
    divergente = OUTBOX.replace("9223372036854775807", "9223372036854775806")
    resultado = _psql("livro_api", _em_rollback(LANCAMENTO, divergente))

    assert resultado.returncode != 0
    assert "outbox_lancamento_id_posicao_fkey" in resultado.stderr


def test_chave_idempotente_e_unica_por_cliente() -> None:
    resultado = _psql("livro_api", _em_rollback(RESPOSTA, RESPOSTA))

    assert resultado.returncode != 0
    assert "resposta_idempotente_pkey" in resultado.stderr
