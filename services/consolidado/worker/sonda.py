"""Sonda do health check do contêiner: `GET /health/ready` no loopback.

    python -m consolidado.worker.sonda

Sai com 0 se o worker responder 200 e com 1 em qualquer outro caso. O health
do worker é HTTP simples em porta interna: não sai do contêiner pela sonda e
não carrega dado de cliente.
"""

import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    porta = os.environ.get("HEALTH_PORTA", "8080")
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{porta}/health/ready", timeout=5
        ) as resposta:
            return 0 if resposta.status == 200 else 1
    except (OSError, urllib.error.URLError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
