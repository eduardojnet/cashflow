"""Logs estruturados e correlacionados com redação (002-T5; INFRA 8.1, 8.2).

Cada registro sai como uma linha JSON com `correlation_id`. A redação tem
três camadas, para não depender de quem escreve o log lembrar a regra:

- campos extras com nome sensível (`valor`, `descricao`, `token`...) saem
  como `[REDIGIDO]`, seja qual for o conteúdo;
- texto de mensagem e de campos passa por padrões de token (JWT, `Bearer`)
  e de segredo em pares `chave=valor`;
- exceção sai só com o tipo e as linhas de código da pilha, sem a mensagem:
  erro de banco pode repetir no `DETAIL` o valor e a descrição gravados.
"""

import json
import logging
import re
import traceback
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

REDIGIDO = "[REDIGIDO]"

correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

# Nomes de campo cujo conteúdo nunca vai a log (INFRA 8.2): conteúdo
# financeiro do lançamento, credenciais e a chave de idempotência, que é
# texto livre do cliente.
_CAMPOS_SENSIVEIS = re.compile(
    r"valor|descricao|token|authorization|senha|segredo|secret|password"
    r"|idempotency|chave",
    re.IGNORECASE,
)
_PADROES_SENSIVEIS = (
    # JWT: três partes base64url, a primeira começando por `eyJ` ({"...).
    re.compile(r"eyJ[\w-]*\.[\w-]*\.[\w-]*"),
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)\b(password|senha|segredo|secret|client_secret|token)=[^\s&]+"),
)

# Atributos de `LogRecord` que não são campos extras do chamador.
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
    if isinstance(conteudo, str):
        return redigir_texto(conteudo)
    if isinstance(conteudo, bool | int | float) or conteudo is None:
        return conteudo
    return redigir_texto(str(conteudo))


class FormatadorJson(logging.Formatter):
    """Uma linha JSON por registro, com redação aplicada a todo o conteúdo."""

    def format(self, record: logging.LogRecord) -> str:
        registro: dict[str, Any] = {
            "instante": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "nivel": record.levelname,
            "logger": record.name,
            "mensagem": redigir_texto(record.getMessage()),
            "correlation_id": correlation_id.get(),
        }
        for nome, conteudo in record.__dict__.items():
            if nome not in _ATRIBUTOS_PADRAO and not nome.startswith("_"):
                registro[nome] = _redigir_campo(nome, conteudo)
        if record.exc_info and record.exc_info[0] is not None:
            tipo, _, pilha = record.exc_info
            registro["erro_tipo"] = tipo.__name__
            registro["erro_pilha"] = "".join(traceback.format_tb(pilha))
        return json.dumps(registro, ensure_ascii=False, default=str)


def configuracao_logging(nivel: str = "INFO") -> dict[str, Any]:
    """`dictConfig` do processo: raiz e loggers do uvicorn no formato JSON.

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
