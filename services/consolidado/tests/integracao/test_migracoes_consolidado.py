"""Migrações do consolidado e privilégios contra o PostgreSQL real (004-T1, 005-T1).

O módulo aplica as migrações pelo serviço `consolidado-migracao` do compose,
com o papel consolidado_migracao, e confere esquema, geração inicial e
privilégios conectando como cada papel. O banco não publica porta no host
(INFRA 6.2), então o psql roda dentro do contêiner, mas conecta pelo nome do
serviço, o que exige a senha do papel (mesmo caminho de
tests/infra/test_papeis_banco.py). Com o ambiente no ar, a partir da raiz do
repositório:

    docker compose -f infra/compose.yaml up -d --wait
    uv run pytest services/consolidado/tests/integracao

Cada sonda de escrita roda numa transação desfeita com ROLLBACK: o banco não
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
SERVICO = "pg-consolidado"
BANCO = "consolidado"
SENHAS = {
    "consolidado_migracao": "PG_CONSOLIDADO_SENHA_MIGRACAO",
    "consolidado_worker": "PG_CONSOLIDADO_SENHA_WORKER",
    "consolidado_api": "PG_CONSOLIDADO_SENHA_API",
    "consolidado_operacao": "PG_CONSOLIDADO_SENHA_OPERACAO",
}
TABELAS = [
    "geracao",
    "controle_projecao",
    "inbox",
    "contribuicao",
    "saldo_diario",
    "marcador_inclusao",
    "checkpoint",
    "lacuna",
    "quarentena",
    # Controle da reconstrução (migração 0002, 005-T1).
    "estado_consumidor",
    "fase_reconstrucao",
]
# O que a consulta usa (design 004 §4, API) e a fase da reconstrução, que a
# API expõe como métrica (migração 0003, 005-T3); o resto é invisível à API.
LIDAS_PELA_API = [
    "geracao",
    "controle_projecao",
    "saldo_diario",
    "marcador_inclusao",
    "checkpoint",
    "fase_reconstrucao",
]

ATIVA = "(SELECT geracao_ativa_id FROM controle_projecao WHERE id = 1)"
LANCAMENTO_ID = "00000000-0000-0000-0000-000000000c01"
EVENTO_ID = "00000000-0000-0000-0000-0000000000e1"
# Data no fim do calendário e posição no topo do bigint, para não colidir com
# dados reais projetados no mesmo banco.
DATA = "9999-12-31"
POSICAO = 9223372036854775807

# Uma linha válida por tabela de escrita, para as sondas de privilégio.
INBOX = (
    f"INSERT INTO inbox (geracao_id, evento_id, hash)"
    f" VALUES ({ATIVA}, '{EVENTO_ID}', 'h1')"
)
CONTRIBUICAO = (
    "INSERT INTO contribuicao"
    " (geracao_id, lancamento_id, posicao, data_lancamento, tipo, valor, hash)"
    f" VALUES ({ATIVA}, '{LANCAMENTO_ID}', {POSICAO}, '{DATA}', 'CREDITO',"
    " 10.50, 'h1')"
)
SALDO = (
    "INSERT INTO saldo_diario"
    " (geracao_id, data, total_creditos, total_debitos, saldo_dia)"
    f" VALUES ({ATIVA}, '{DATA}', 10.50, 0, 10.50)"
    " ON CONFLICT (geracao_id, data) DO UPDATE SET"
    " total_creditos = saldo_diario.total_creditos + EXCLUDED.total_creditos,"
    " total_debitos = saldo_diario.total_debitos + EXCLUDED.total_debitos,"
    " saldo_dia = saldo_diario.saldo_dia + EXCLUDED.saldo_dia"
)
MARCADOR = (
    "INSERT INTO marcador_inclusao (geracao_id, lancamento_id, posicao)"
    f" VALUES ({ATIVA}, '{LANCAMENTO_ID}', {POSICAO})"
)
QUARENTENA = (
    "INSERT INTO quarentena (evento_id, motivo, payload_bruto)"
    f" VALUES ('{EVENTO_ID}', 'sonda', '\\x7b7d')"
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


def _migrar() -> subprocess.CompletedProcess[str]:
    return _docker(
        "compose",
        "-f",
        str(COMPOSE),
        "run",
        "--rm",
        "--build",
        "consolidado-migracao",
        timeout=600,
    )


@pytest.fixture(scope="module", autouse=True)
def migracoes_aplicadas() -> None:
    """Aplica as migrações como no uso real; reaplicar em head não muda nada."""
    resultado = _migrar()
    assert resultado.returncode == 0, resultado.stderr


def test_migracao_fica_em_head_aplicada_por_consolidado_migracao() -> None:
    resultado = _psql(
        "consolidado_migracao",
        "SELECT version_num FROM alembic_version",
    )

    assert resultado.returncode == 0, resultado.stderr
    assert resultado.stdout.strip() == "0003"


def test_tabelas_do_consolidado_pertencem_a_consolidado_migracao() -> None:
    resultado = _psql(
        "consolidado_migracao",
        "SELECT tablename || '=' || tableowner FROM pg_tables"
        " WHERE schemaname = 'public' ORDER BY tablename",
    )

    assert resultado.returncode == 0, resultado.stderr
    donos = dict(linha.split("=") for linha in resultado.stdout.split())
    assert donos == {
        tabela: "consolidado_migracao" for tabela in [*TABELAS, "alembic_version"]
    }


def test_controle_aponta_para_a_geracao_ativa_lida_pela_api() -> None:
    resultado = _psql(
        "consolidado_api",
        "SELECT g.estado || ' ' || k.posicao_continua"
        " FROM controle_projecao c"
        " JOIN geracao g ON g.id = c.geracao_ativa_id"
        " JOIN checkpoint k ON k.geracao_id = c.geracao_ativa_id",
    )

    assert resultado.returncode == 0, resultado.stderr
    estado, _, posicao = resultado.stdout.strip().partition(" ")
    assert estado == "ativa"
    assert int(posicao) >= 0


def test_reaplicar_a_migracao_nao_cria_outra_geracao() -> None:
    resultado = _migrar()
    assert resultado.returncode == 0, resultado.stderr

    contagem = _psql(
        "consolidado_api",
        "SELECT (SELECT count(*) FROM geracao WHERE estado = 'ativa')"
        " || ' ' || (SELECT count(*) FROM controle_projecao)",
    )

    assert contagem.returncode == 0, contagem.stderr
    assert contagem.stdout.strip() == "1 1"


@pytest.mark.parametrize("tabela", LIDAS_PELA_API)
def test_consolidado_api_le_o_que_a_consulta_usa(tabela: str) -> None:
    resultado = _psql("consolidado_api", f"SELECT count(*) FROM {tabela}")

    assert resultado.returncode == 0, resultado.stderr


@pytest.mark.parametrize("tabela", [t for t in TABELAS if t not in LIDAS_PELA_API])
def test_consolidado_api_nao_le_dados_internos_da_projecao(tabela: str) -> None:
    resultado = _psql("consolidado_api", f"SELECT 1 FROM {tabela} LIMIT 1")

    assert resultado.returncode != 0
    assert f"permission denied for table {tabela}" in resultado.stderr


@pytest.mark.parametrize(
    ("comando", "tabela"),
    [
        (INBOX, "inbox"),
        (CONTRIBUICAO, "contribuicao"),
        (SALDO, "saldo_diario"),
        (MARCADOR, "marcador_inclusao"),
        (QUARENTENA, "quarentena"),
        (f"INSERT INTO lacuna (geracao_id, posicao) VALUES (1, {POSICAO})", "lacuna"),
        ("INSERT INTO geracao (estado) VALUES ('construindo')", "geracao"),
        ("UPDATE saldo_diario SET saldo_dia = saldo_dia", "saldo_diario"),
        ("UPDATE checkpoint SET posicao_continua = 0", "checkpoint"),
        ("UPDATE controle_projecao SET geracao_ativa_id = 1", "controle_projecao"),
        ("UPDATE geracao SET estado = 'anterior'", "geracao"),
        ("DELETE FROM saldo_diario", "saldo_diario"),
        ("DELETE FROM marcador_inclusao", "marcador_inclusao"),
        ("DELETE FROM controle_projecao", "controle_projecao"),
        ("TRUNCATE saldo_diario", "saldo_diario"),
        ("TRUNCATE checkpoint", "checkpoint"),
    ],
    ids=[
        "inbox-insert",
        "contribuicao-insert",
        "saldo-insert",
        "marcador-insert",
        "quarentena-insert",
        "lacuna-insert",
        "geracao-insert",
        "saldo-update",
        "checkpoint-update",
        "controle-update",
        "geracao-update",
        "saldo-delete",
        "marcador-delete",
        "controle-delete",
        "saldo-truncate",
        "checkpoint-truncate",
    ],
)
def test_escrita_com_consolidado_api_falha(comando: str, tabela: str) -> None:
    resultado = _psql("consolidado_api", _em_rollback(comando))

    assert resultado.returncode != 0
    assert f"permission denied for table {tabela}" in resultado.stderr


def test_consolidado_worker_executa_a_transacao_de_projecao() -> None:
    """Inbox, contribuição, saldo, marcador, checkpoint e lacuna (design §3)."""
    resultado = _psql(
        "consolidado_worker",
        _em_rollback(
            f"SELECT 1 FROM checkpoint WHERE geracao_id = {ATIVA} FOR UPDATE",
            INBOX,
            CONTRIBUICAO,
            SALDO,
            SALDO,
            MARCADOR,
            f"INSERT INTO lacuna (geracao_id, posicao) VALUES ({ATIVA}, {POSICAO})",
            f"DELETE FROM lacuna WHERE geracao_id = {ATIVA} AND posicao = {POSICAO}",
            "UPDATE checkpoint SET posicao_continua = posicao_continua,"
            f" atualizado_em = now() WHERE geracao_id = {ATIVA}",
            QUARENTENA,
            "SELECT total_creditos || ' ' || saldo_dia FROM saldo_diario"
            f" WHERE geracao_id = {ATIVA} AND data = '{DATA}'",
        ),
    )

    assert resultado.returncode == 0, resultado.stderr
    assert resultado.stdout.splitlines()[-2:] == ["21.00 21.00", "ROLLBACK"]


@pytest.mark.parametrize(
    ("comando", "tabela"),
    [
        ("UPDATE inbox SET hash = 'x'", "inbox"),
        ("DELETE FROM inbox", "inbox"),
        ("UPDATE contribuicao SET valor = 1", "contribuicao"),
        ("DELETE FROM contribuicao", "contribuicao"),
        ("UPDATE marcador_inclusao SET posicao = 1", "marcador_inclusao"),
        ("DELETE FROM marcador_inclusao", "marcador_inclusao"),
        ("UPDATE quarentena SET motivo = 'x'", "quarentena"),
        ("DELETE FROM quarentena", "quarentena"),
        ("DELETE FROM saldo_diario", "saldo_diario"),
        ("UPDATE saldo_diario SET data = data", "saldo_diario"),
        ("UPDATE checkpoint SET geracao_id = geracao_id", "checkpoint"),
        ("INSERT INTO geracao (estado) VALUES ('construindo')", "geracao"),
        ("UPDATE controle_projecao SET geracao_ativa_id = 1", "controle_projecao"),
        ("TRUNCATE inbox", "inbox"),
    ],
    ids=[
        "inbox-update",
        "inbox-delete",
        "contribuicao-update",
        "contribuicao-delete",
        "marcador-update",
        "marcador-delete",
        "quarentena-update",
        "quarentena-delete",
        "saldo-delete",
        "saldo-update-data",
        "checkpoint-update-geracao",
        "geracao-insert",
        "controle-update",
        "inbox-truncate",
    ],
)
def test_consolidado_worker_nao_tem_privilegio_alem_da_projecao(
    comando: str, tabela: str
) -> None:
    resultado = _psql("consolidado_worker", _em_rollback(comando))

    assert resultado.returncode != 0
    assert f"permission denied for table {tabela}" in resultado.stderr


# O que o coordenador usa (migração 0002); o detalhe dos privilégios da
# operação está em services/coordenador/tests/integracao.
LIDAS_PELA_OPERACAO = [
    "geracao",
    "controle_projecao",
    "checkpoint",
    "lacuna",
    "contribuicao",
    "saldo_diario",
    "estado_consumidor",
    "fase_reconstrucao",
]


@pytest.mark.parametrize("tabela", LIDAS_PELA_OPERACAO)
def test_consolidado_operacao_le_o_controle_das_geracoes(tabela: str) -> None:
    resultado = _psql("consolidado_operacao", f"SELECT count(*) FROM {tabela}")

    assert resultado.returncode == 0, resultado.stderr


@pytest.mark.parametrize("tabela", [t for t in TABELAS if t not in LIDAS_PELA_OPERACAO])
def test_consolidado_operacao_nao_le_dados_internos_da_projecao(tabela: str) -> None:
    resultado = _psql("consolidado_operacao", f"SELECT 1 FROM {tabela} LIMIT 1")

    assert resultado.returncode != 0
    assert f"permission denied for table {tabela}" in resultado.stderr


@pytest.mark.parametrize(
    ("comando", "restricao"),
    [
        (
            (
                "INSERT INTO geracao (estado) VALUES ('construindo');"
                " UPDATE geracao SET estado = 'ativa' WHERE estado = 'construindo'"
            ),
            "ux_geracao_uma_ativa",
        ),
        (
            "INSERT INTO geracao (estado) VALUES ('pronta')",
            "geracao_estado_check",
        ),
        (
            f"INSERT INTO controle_projecao (id, geracao_ativa_id) VALUES (2, {ATIVA})",
            "controle_projecao_id_check",
        ),
        (
            SALDO.replace("10.50, 0, 10.50", "10, 3, 6"),
            "saldo_diario_check",
        ),
        (
            CONTRIBUICAO.replace("'CREDITO'", "'ESTORNO'"),
            "contribuicao_tipo_check",
        ),
        (
            CONTRIBUICAO.replace("10.50", "0"),
            "contribuicao_valor_check",
        ),
        (
            INBOX.replace(ATIVA, "999999"),
            "inbox_geracao_id_fkey",
        ),
        # Migração 0002 (005-T1): ciclo de vida das gerações e barreira.
        (
            "INSERT INTO geracao (estado) VALUES ('ativa')",
            "nova nasce construindo",
        ),
        (
            "UPDATE geracao SET estado = 'construindo' WHERE estado = 'ativa'",
            "ativa para construindo",
        ),
        (
            (
                "INSERT INTO geracao (estado) VALUES ('construindo');"
                " INSERT INTO geracao (estado) VALUES ('construindo')"
            ),
            "ex_geracao_uma_alem_da_ativa",
        ),
        (
            "UPDATE controle_projecao SET barreira = 'parcial'",
            "controle_projecao_barreira_check",
        ),
    ],
    ids=[
        "uma-ativa",
        "estado-invalido",
        "controle-linha-unica",
        "saldo-incoerente",
        "tipo-invalido",
        "valor-zero",
        "geracao-inexistente",
        "nasce-construindo",
        "transicao-invalida",
        "uma-alem-da-ativa",
        "barreira-invalida",
    ],
)
def test_restricoes_recusam_linha_invalida(comando: str, restricao: str) -> None:
    resultado = _psql("consolidado_migracao", _em_rollback(comando))

    assert resultado.returncode != 0
    assert restricao in resultado.stderr


def test_posicao_e_unica_por_geracao() -> None:
    outro = CONTRIBUICAO.replace(LANCAMENTO_ID, "00000000-0000-0000-0000-000000000c02")
    resultado = _psql("consolidado_worker", _em_rollback(CONTRIBUICAO, outro))

    assert resultado.returncode != 0
    assert "contribuicao_geracao_id_posicao_key" in resultado.stderr


def test_contribuicao_e_unica_por_lancamento_na_geracao() -> None:
    resultado = _psql("consolidado_worker", _em_rollback(CONTRIBUICAO, CONTRIBUICAO))

    assert resultado.returncode != 0
    assert "contribuicao_pkey" in resultado.stderr


def test_evento_e_unico_por_geracao_na_inbox() -> None:
    resultado = _psql("consolidado_worker", _em_rollback(INBOX, INBOX))

    assert resultado.returncode != 0
    assert "inbox_pkey" in resultado.stderr
