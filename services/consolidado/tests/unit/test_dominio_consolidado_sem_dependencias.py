"""Domínio da projeção sem dependência de framework, banco ou broker (INFRA 2.1; 004-T2)."""

import ast
import sys
from pathlib import Path

DOMINIO = Path(__file__).resolve().parents[2] / "dominio"

# Fora da biblioteca padrão, o domínio só pode importar a si mesmo. Nem o
# domínio do livro: o consolidado conhece só o contrato do evento (INFRA 6.7).
PREFIXO_PERMITIDO = "consolidado.dominio"


def _modulos_importados(arquivo: Path) -> set[str]:
    arvore = ast.parse(arquivo.read_text(encoding="utf-8"))
    modulos: set[str] = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            modulos.update(alias.name for alias in no.names)
        # Import relativo (level > 0) fica dentro do próprio pacote do domínio.
        elif isinstance(no, ast.ImportFrom) and no.level == 0 and no.module:
            modulos.add(no.module)
    return modulos


def test_dominio_importa_somente_biblioteca_padrao_e_o_proprio_dominio() -> None:
    arquivos = sorted(DOMINIO.rglob("*.py"))
    assert arquivos, "nenhum módulo de domínio encontrado"

    proibidos = {
        f"{arquivo.name}: {modulo}"
        for arquivo in arquivos
        for modulo in _modulos_importados(arquivo)
        if modulo.split(".")[0] not in sys.stdlib_module_names
        and not modulo.startswith(PREFIXO_PERMITIDO)
    }

    assert not proibidos, f"imports fora do domínio: {sorted(proibidos)}"
