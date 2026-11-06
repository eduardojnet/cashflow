"""Processo da API do consolidado: HTTPS com uvicorn e /metrics interno.

    python -m consolidado.adaptadores.http

Mesma imagem do worker, com outro comando (004-T3). TLS obrigatório com o
certificado emitido pela AC local (INFRA 5.7, 5.8): `TLS_CERTIFICADO` e
`TLS_CHAVE`. A API escuta em `API_PORTA` (padrão 8443) e o /metrics em
`METRICAS_PORTA` (padrão 9464), porta interna sem publicação no host (007
design §4; INFRA 6.1). `METRICAS_ENDERECO` (padrão 0.0.0.0) restringe o
/metrics ao endereço da API na rede interna do consolidado, porque a rede de
identidade é alcançável pelo host (007 TD-007).
"""

import logging.config
import os

import uvicorn
from observabilidade import Metricas

from consolidado.adaptadores.http.principal import criar_app_do_ambiente
from consolidado.adaptadores.logs import configuracao_logging


def _obrigatoria(nome: str) -> str:
    valor = os.environ.get(nome)
    if not valor:
        raise SystemExit(f"defina {nome}")
    return valor


def main() -> None:
    configuracao = configuracao_logging(os.environ.get("LOG_NIVEL", "INFO"))
    logging.config.dictConfig(configuracao)
    certificado = _obrigatoria("TLS_CERTIFICADO")
    chave = _obrigatoria("TLS_CHAVE")

    metricas = Metricas()
    try:
        app = criar_app_do_ambiente(metricas=metricas)
    except ValueError as erro:
        raise SystemExit(str(erro)) from None
    metricas.iniciar_servidor(
        int(os.environ.get("METRICAS_PORTA", "9464")),
        endereco=os.environ.get("METRICAS_ENDERECO", "0.0.0.0"),
    )
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("API_PORTA", "8443")),
        ssl_certfile=certificado,
        ssl_keyfile=chave,
        log_config=configuracao,
        access_log=False,
        server_header=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
