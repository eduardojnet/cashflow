"""Logs estruturados do consolidado, uma linha JSON por registro (INFRA 8.1,
8.2).

O worker registra só identificação do evento (`evento_id`), desfecho e tipos
de erro, nunca o corpo da mensagem nem valores (design 004 §6). A API de
consulta (004-T5) registra rota, status e resultado, nunca saldos nem o
token. Mesmo assim, a redação não depende de quem escreve o log:

- campo extra com nome sensível (`payload`, `valor`, `saldo`, `senha`...)
  sai como `[REDIGIDO]`;
- `user:senha@` de URL AMQP ou PostgreSQL, pares `password=...`, JWT e
  `Bearer ...` saem redigidos do texto;
- exceção sai só com o tipo e as linhas de código da pilha, sem a mensagem,
  que em erro de driver pode repetir dados.

Na API, cada linha leva o `correlation_id` da requisição, como em
`lancamentos.adaptadores.http.logs`; no worker, que não tem requisição, o
campo não aparece. Mesmas regras do relay e da API de lançamentos, sem
importá-los: a imagem do consolidado não leva o código de outro serviço.
"""

import json
import logging
import re
import sys
import traceback
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

REDIGIDO = "[REDIGIDO]"

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_CAMPOS_SENSIVEIS = re.compile(
    r"payload|corpo|valor|saldo|total|descricao|cliente"
    r"|token|authorization|senha|segredo|secret|password",
    re.IGNORECASE,
)
_PADROES_SENSIVEIS = (
    re.compile(r"(?<=://)[^/@\s:]*:[^/@\s]*@"),
    # JWT: três partes base64url, a primeira começando por `eyJ` ({"...).
    re.compile(r"eyJ[\w-]*\.[\w-]*\.[\w-]*"),
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)\b(password|senha|segredo|secret|token)=[^\s&]+"),
)

_ATRIBUTOS_PADRAO = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


def redigir_texto(texto: str) -> str:
    for padrao in _PADROES_SENSIVEIS:
        texto = padrao.sub(REDIGIDO, texto)
    return texto


def _redigir_campo(nome: str, conteudo: Any) -> Any:
    if _CAMPOS_SENSIVEIS.search(nome):
        return REDIGIDO
    if isinstance(conteudo, bool | int | float) or conteudo is None:
        return conteudo
    return redigir_texto(str(conteudo))


class FormatadorJson(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        registro: dict[str, Any] = {
            "instante": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "nivel": record.levelname,
            "logger": record.name,
            "mensagem": redigir_texto(record.getMessage()),
        }
        correlacao = correlation_id.get()
        if correlacao is not None:
            registro["correlation_id"] = correlacao
        for nome, conteudo in record.__dict__.items():
            if nome not in _ATRIBUTOS_PADRAO and not nome.startswith("_"):
                registro[nome] = _redigir_campo(nome, conteudo)
        if record.exc_info and record.exc_info[0] is not None:
            tipo, _, pilha = record.exc_info
            registro["erro_tipo"] = tipo.__name__
            registro["erro_pilha"] = "".join(traceback.format_tb(pilha))
        return json.dumps(registro, ensure_ascii=False, default=str)


def configurar_logging(nivel: str = "INFO") -> None:
    saida = logging.StreamHandler(sys.stdout)
    saida.setFormatter(FormatadorJson())
    logging.basicConfig(level=nivel, handlers=[saida], force=True)


def configuracao_logging(nivel: str = "INFO") -> dict[str, Any]:
    """`dictConfig` da API: raiz e loggers do uvicorn no formato JSON.

    O log de acesso do uvicorn fica desligado: ele grava a linha de pedido
    sem redação. A linha por requisição é a do middleware da API.
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"json": {"()": FormatadorJson}},
        "handlers": {
            "saida": {
                "class": "logging.StreamHandler",
                "formatter": "json",
                "stream": "ext://sys.stdout",
            }
        },
        "root": {"handlers": ["saida"], "level": nivel},
        "loggers": {
            "uvicorn": {"handlers": [], "propagate": True},
            "uvicorn.error": {"handlers": [], "propagate": True},
            "uvicorn.access": {"handlers": [], "propagate": False},
        },
    }
