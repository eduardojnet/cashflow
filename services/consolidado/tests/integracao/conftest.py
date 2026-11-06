"""Torna importáveis o RabbitMQ e o PostgreSQL descartáveis (INFRA 9.1, 9.2).

O worker usa o mesmo RabbitMQ descartável dos testes do relay (mesma imagem,
topologia, usuários e permissões do compose), que por sua vez usa o
utilitário `docker` do livro descartável. Reaproveitar os módulos evita uma
segunda cópia que poderia divergir do ambiente real.
"""

import sys
from pathlib import Path

_SERVICOS = Path(__file__).resolve().parents[3]
for _caminho in (
    _SERVICOS / "lancamentos/tests/integracao",
    _SERVICOS / "relay/tests/integracao",
):
    if str(_caminho) not in sys.path:
        sys.path.append(str(_caminho))
