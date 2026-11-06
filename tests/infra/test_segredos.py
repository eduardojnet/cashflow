"""Segredos fora do repositório e modelo de variáveis completo (001-T6, FR-008).

Roda no host, a partir da raiz do repositório, sem o ambiente no ar:

    uv run pytest tests/infra/test_segredos.py

"Repositório" é o que o Git versionaria: arquivos rastreados e não rastreados
que o .gitignore não exclui. Os valores procurados são os de infra/.env; sem
esse arquivo, o teste correspondente é pulado com o motivo explícito.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
ENV = RAIZ / "infra/.env"
MODELO = RAIZ / ".env.example"
COMPOSE = RAIZ / "infra/compose.yaml"

# Menor segredo aceito pela busca: valores curtos, como uma porta, casariam por
# acaso com texto comum.
TAMANHO_MINIMO = 12


def _ler(arquivo: Path) -> dict[str, str]:
    variaveis: dict[str, str] = {}
    for linha in arquivo.read_text().splitlines():
        linha = linha.strip()
        if linha and not linha.startswith("#") and "=" in linha:
            nome, valor = linha.split("=", 1)
            variaveis[nome.strip()] = valor.strip()
    return variaveis


def _arquivos_versionaveis() -> list[Path]:
    resultado = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=RAIZ,
        check=True,
        capture_output=True,
        text=True,
    )
    caminhos = [RAIZ / nome for nome in resultado.stdout.split("\0") if nome]
    return [caminho for caminho in caminhos if caminho.is_file()]


@pytest.mark.skipif(shutil.which("git") is None, reason="git ausente no PATH")
def test_env_local_e_ignorado_pelo_git() -> None:
    resultado = subprocess.run(
        ["git", "check-ignore", "-q", "infra/.env"], cwd=RAIZ, check=False
    )

    assert resultado.returncode == 0


@pytest.mark.skipif(not ENV.is_file(), reason="infra/.env ausente")
@pytest.mark.skipif(shutil.which("git") is None, reason="git ausente no PATH")
def test_nenhum_segredo_local_aparece_no_repositorio() -> None:
    segredos = {
        nome: valor for nome, valor in _ler(ENV).items() if len(valor) >= TAMANHO_MINIMO
    }
    assert segredos, "infra/.env sem segredos para procurar"

    vazamentos = [
        f"{caminho.relative_to(RAIZ)}: {nome}"
        for caminho in _arquivos_versionaveis()
        for nome, valor in segredos.items()
        if valor.encode() in caminho.read_bytes()
    ]

    assert vazamentos == []


def test_modelo_nao_traz_valor_real() -> None:
    preenchidas = {nome: valor for nome, valor in _ler(MODELO).items() if valor}

    assert preenchidas == {}


def test_modelo_declara_toda_variavel_obrigatoria_do_compose() -> None:
    obrigatorias = set(re.findall(r"\$\{([A-Z0-9_]+):\?", COMPOSE.read_text()))

    assert obrigatorias - set(_ler(MODELO)) == set()
