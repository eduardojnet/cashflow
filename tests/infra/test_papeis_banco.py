"""Papéis de banco com privilégio mínimo contra o PostgreSQL real (001-T6).

Os bancos não publicam porta no host (INFRA 6.2), então o psql roda dentro do
próprio contêiner, mas conecta pelo nome do serviço na rede interna. Esse
caminho cai na regra `host ... scram-sha-256` do pg_hba e exige a senha do
papel; o loopback do contêiner (`trust`) não é usado. Com o ambiente no ar, a
partir da raiz do repositório:

    docker compose -f infra/compose.yaml up -d --wait
    uv run pytest tests/infra/test_papeis_banco.py

As senhas vêm de infra/.env e chegam ao psql por variável de ambiente, nunca
pela linha de comando. Sem o ambiente no ar, o módulo é pulado com o motivo
explícito.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJETO = "cashflow"
ENV = Path(__file__).resolve().parents[2] / "infra/.env"


@dataclass(frozen=True)
class Papel:
    nome: str
    servico: str
    banco: str
    variavel_senha: str

    def __str__(self) -> str:
        return self.nome


# design 001 §4. O papel de migração é o único com DDL em cada banco.
PAPEIS_LIVRO = [
    Papel("livro_migracao", "pg-livro", "livro", "PG_LIVRO_SENHA_MIGRACAO"),
    Papel("livro_api", "pg-livro", "livro", "PG_LIVRO_SENHA_API"),
    Papel("livro_relay", "pg-livro", "livro", "PG_LIVRO_SENHA_RELAY"),
    Papel("livro_replay", "pg-livro", "livro", "PG_LIVRO_SENHA_REPLAY"),
    # Exporter de métricas (007-T1).
    Papel("livro_monitoramento", "pg-livro", "livro", "PG_LIVRO_SENHA_MONITORAMENTO"),
]
PAPEIS_CONSOLIDADO = [
    Papel(
        "consolidado_migracao",
        "pg-consolidado",
        "consolidado",
        "PG_CONSOLIDADO_SENHA_MIGRACAO",
    ),
    Papel(
        "consolidado_worker",
        "pg-consolidado",
        "consolidado",
        "PG_CONSOLIDADO_SENHA_WORKER",
    ),
    Papel(
        "consolidado_api", "pg-consolidado", "consolidado", "PG_CONSOLIDADO_SENHA_API"
    ),
    Papel(
        "consolidado_operacao",
        "pg-consolidado",
        "consolidado",
        "PG_CONSOLIDADO_SENHA_OPERACAO",
    ),
    # Exporter de métricas (007-T1).
    Papel(
        "consolidado_monitoramento",
        "pg-consolidado",
        "consolidado",
        "PG_CONSOLIDADO_SENHA_MONITORAMENTO",
    ),
]
PAPEIS = PAPEIS_LIVRO + PAPEIS_CONSOLIDADO
MIGRACAO = [p for p in PAPEIS if p.nome.endswith("_migracao")]
RUNTIME = [p for p in PAPEIS if not p.nome.endswith("_migracao")]


def _docker(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
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
    ausentes = [
        s for s in ("pg-livro", "pg-consolidado") if _id_do_conteiner(s) is None
    ]
    if ausentes:
        return f"ambiente fora do ar ({', '.join(ausentes)}): ver docstring"
    return None


_MOTIVO = _motivo_para_pular()
pytestmark = pytest.mark.skipif(_MOTIVO is not None, reason=_MOTIVO or "")


def _psql(
    papel: Papel,
    sql: str,
    *,
    servico: str | None = None,
    banco: str | None = None,
    senha: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Executa `sql` como `papel`, autenticando por senha na rede interna."""
    servico = servico or papel.servico
    id_ = _id_do_conteiner(servico)
    assert id_ is not None, f"{servico} não está em execução"
    env = {**os.environ, "PGPASSWORD": senha or _ler_env()[papel.variavel_senha]}
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
        servico,
        "-U",
        papel.nome,
        "-d",
        banco or papel.banco,
        "-c",
        sql,
        env=env,
    )


@pytest.mark.parametrize("papel", PAPEIS, ids=str)
def test_cada_papel_entra_no_proprio_banco_com_a_propria_senha(papel: Papel) -> None:
    resultado = _psql(papel, "SELECT current_user, current_database()")

    assert resultado.returncode == 0, resultado.stderr
    assert resultado.stdout.strip() == f"{papel.nome}|{papel.banco}"


@pytest.mark.parametrize("papel", PAPEIS, ids=str)
def test_senha_errada_e_recusada(papel: Papel) -> None:
    resultado = _psql(papel, "SELECT 1", senha="senha-errada")

    assert resultado.returncode != 0
    assert "password authentication failed" in resultado.stderr


@pytest.mark.parametrize("papel", PAPEIS, ids=str)
def test_papel_nao_tem_atributo_privilegiado(papel: Papel) -> None:
    resultado = _psql(
        papel,
        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls"
        " FROM pg_roles WHERE rolname = current_user",
    )

    assert resultado.returncode == 0, resultado.stderr
    assert resultado.stdout.strip() == "f|f|f|f|f"


@pytest.mark.parametrize("banco", ["postgres", "template1"])
@pytest.mark.parametrize("papel", PAPEIS, ids=str)
def test_papel_nao_conecta_aos_outros_bancos_da_instancia(
    papel: Papel, banco: str
) -> None:
    resultado = _psql(papel, "SELECT 1", banco=banco)

    assert resultado.returncode != 0
    assert f'permission denied for database "{banco}"' in resultado.stderr


@pytest.mark.parametrize("papel", PAPEIS_LIVRO, ids=str)
def test_papel_do_livro_nao_existe_no_consolidado(papel: Papel) -> None:
    resultado = _psql(papel, "SELECT 1", servico="pg-consolidado", banco="consolidado")

    assert resultado.returncode != 0
    assert "password authentication failed" in resultado.stderr


@pytest.mark.parametrize("papel", PAPEIS_CONSOLIDADO, ids=str)
def test_papel_do_consolidado_nao_existe_no_livro(papel: Papel) -> None:
    resultado = _psql(papel, "SELECT 1", servico="pg-livro", banco="livro")

    assert resultado.returncode != 0
    assert "password authentication failed" in resultado.stderr


@pytest.mark.parametrize("papel", MIGRACAO, ids=str)
def test_papel_de_migracao_cria_tabela_no_esquema(papel: Papel) -> None:
    resultado = _psql(
        papel,
        "BEGIN; CREATE TABLE public.sonda_t6 (id int); ROLLBACK;",
    )

    assert resultado.returncode == 0, resultado.stderr


@pytest.mark.parametrize("papel", RUNTIME, ids=str)
def test_papel_de_runtime_nao_cria_tabela(papel: Papel) -> None:
    resultado = _psql(papel, "CREATE TABLE public.sonda_t6 (id int)")

    assert resultado.returncode != 0
    assert "permission denied for schema public" in resultado.stderr


@pytest.mark.parametrize("papel", PAPEIS, ids=str)
def test_papel_nao_cria_esquema(papel: Papel) -> None:
    resultado = _psql(papel, "CREATE SCHEMA sonda_t6")

    assert resultado.returncode != 0
    assert f"permission denied for database {papel.banco}" in resultado.stderr


@pytest.mark.parametrize("papel", PAPEIS, ids=str)
def test_papel_nao_cria_tabela_temporaria(papel: Papel) -> None:
    resultado = _psql(papel, "CREATE TEMP TABLE sonda_t6 (id int)")

    assert resultado.returncode != 0
    assert "permission denied to create temporary tables" in resultado.stderr
