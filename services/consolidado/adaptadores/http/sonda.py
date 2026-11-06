"""Sonda do health check do contêiner: `GET /health/ready` por TLS.

    python -m consolidado.adaptadores.http.sonda

Sai com 0 se a API responder 200 e com 1 em qualquer outro caso. O
certificado é conferido pela AC local (`JWT_AC_ARQUIVO`, a mesma do JWKS),
com o nome `localhost` que consta do certificado (001-T3).
"""

import os
import ssl
import sys
import urllib.error
import urllib.request


def main() -> int:
    contexto = ssl.create_default_context(cafile=os.environ["JWT_AC_ARQUIVO"])
    porta = os.environ.get("API_PORTA", "8443")
    try:
        with urllib.request.urlopen(
            f"https://localhost:{porta}/health/ready", context=contexto, timeout=5
        ) as resposta:
            return 0 if resposta.status == 200 else 1
    except (OSError, urllib.error.URLError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
