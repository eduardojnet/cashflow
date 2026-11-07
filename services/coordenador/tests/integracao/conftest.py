"""Torna importáveis o PostgreSQL do consolidado e o RabbitMQ descartáveis
(INFRA 9.1, 9.2).

O coordenador opera no banco do consolidado: os testes usam o mesmo
descartável dos testes do worker (mesma imagem, papéis e migrações), que por
sua vez usa o utilitário `docker` do livro descartável. A troca e o rollback
(005-T4) rodam com o worker real em execução, sobre o RabbitMQ descartável
dos testes do relay.
"""

import sys
from pathlib import Path

_SERVICOS = Path(__file__).resolve().parents[3]
for _caminho in (
    _SERVICOS / "lancamentos/tests/integracao",
    _SERVICOS / "consolidado/tests/integracao",
    _SERVICOS / "relay/tests/integracao",
):
    if str(_caminho) not in sys.path:
        sys.path.append(str(_caminho))
