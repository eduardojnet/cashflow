"""Logs do relay sem payload, credenciais nem mensagem de exceção (INFRA 8.2)."""

import json
import logging
import sys

from relay.adaptadores.logs import REDIGIDO, FormatadorJson


def _formatar(
    mensagem: str, extra: dict[str, object], erro: Exception | None = None
) -> dict[str, object]:
    registro = logging.LogRecord(
        "relay.teste", logging.WARNING, __file__, 1, mensagem, None, None
    )
    for nome, conteudo in extra.items():
        setattr(registro, nome, conteudo)
    if erro is not None:
        try:
            raise erro
        except type(erro):
            registro.exc_info = sys.exc_info()
    saida: dict[str, object] = json.loads(FormatadorJson().format(registro))
    return saida


def test_identificacao_do_evento_sai_e_payload_nao() -> None:
    saida = _formatar(
        "evento mantido pendente",
        {"evento_id": "e-1", "posicao": 7, "payload": {"valor": "25.40"}},
    )
    assert saida["evento_id"] == "e-1"
    assert saida["posicao"] == 7
    assert saida["payload"] == REDIGIDO


def test_credencial_em_url_e_em_par_chave_valor_e_redigida() -> None:
    saida = _formatar(
        "falha em amqp://relay:s3cr3t@rabbitmq:5672/ com password=s3cr3t", {}
    )
    assert "s3cr3t" not in json.dumps(saida)


def test_excecao_sai_so_com_o_tipo() -> None:
    saida = _formatar("falhou", {}, ValueError("DETAIL: valor=25.40"))
    assert saida["erro_tipo"] == "ValueError"
    assert "25.40" not in json.dumps(saida)
