"""Torna importável o PostgreSQL descartável do consolidado (INFRA 9.1).

O coordenador opera no banco do consolidado: os testes usam o mesmo
descartável dos testes do worker (mesma imagem, papéis e migrações), que por
sua vez usa o utilitário `docker` do livro descartável.
"""

import sys
from pathlib import Path

_SERVICOS = Path(__file__).resolve().parents[3]
for _caminho in (
    _SERVICOS / "lancamentos/tests/integracao",
    _SERVICOS / "consolidado/tests/integracao",
):
    if str(_caminho) not in sys.path:
        sys.path.append(str(_caminho))
