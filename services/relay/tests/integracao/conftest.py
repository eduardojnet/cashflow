"""Torna importável o PostgreSQL descartável do livro (INFRA 9.1).

O relay usa o mesmo livro descartável dos testes de lançamentos: mesma
imagem, mesmos papéis e mesmas migrações. Reaproveitar o módulo evita uma
segunda cópia que poderia divergir do livro real.
"""

import sys
from pathlib import Path

_LIVRO_DESCARTAVEL = (
    Path(__file__).resolve().parents[3] / "lancamentos/tests/integracao"
)
if str(_LIVRO_DESCARTAVEL) not in sys.path:
    sys.path.append(str(_LIVRO_DESCARTAVEL))
