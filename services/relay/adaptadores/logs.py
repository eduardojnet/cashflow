"""Logs estruturados do relay, uma linha JSON por registro (INFRA 8.1, 8.2).

O relay registra só identificação do evento (`evento_id`, `posicao`),
contagens e tipos de erro, nunca o payload (design 003 §6). Mesmo assim, a
redação não depende de quem escreve o log:

- campo extra com nome sensível (`payload`, `valor`, `senha`...) sai como
  `[REDIGIDO]`;
- `user:senha@` de URL AMQP ou PostgreSQL e pares `password=...` saem
  redigidos do texto;
- exceção sai só com o tipo e as linhas de código da pilha, sem a mensagem,
  que em erro de driver pode repetir dados.

Não importa `lancamentos.adaptadores.http.logs`: a imagem do relay não leva
o código de outro serviço.
"""

import json
import logging
import re
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

REDIGIDO = "[REDIGIDO]"

_CAMPOS_SENSIVEIS = re.compile(
    r"payload|valor|descricao|cliente|token|senha|segredo|secret|password",
    re.IGNORECASE,
)
_PADROES_SENSIVEIS = (
    re.compile(r"(?<=://)[^/@\s:]*:[^/@\s]*@"),
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
