"""Republicação da outbox a partir de uma posição (003-T4; TD-004).

    python -m relay.comandos.republicar --desde-posicao N

Reabre os eventos com `posicao >= N`: `publicado_em = NULL` e
`tentativas = 0` (design 003 §4). O relay em execução os reserva e publica
de novo pelo ciclo normal, com confirms e `mandatory` (FR-005; INFRA 7.6).
Nada é apagado nem reescrito além dessas duas colunas (D-001; INFRA 7.2).

Executado pelo operador no lado do livro, com o papel livro_replay (INFRA
5.11, 6.4); o consolidado não tem rota até aqui (INFRA 6.7). A restrição é
do banco, não deste comando (migração 0003): outro papel não tem o
privilégio ou é barrado pelo gatilho de reabertura, e o comando termina com
erro sem reabrir nada.

O lease não é tocado: evento ainda com lease ativo de uma reserva recente
volta à publicação quando o lease vence, sem duas execuções publicarem ao
mesmo tempo (FR-004; TD-001). O consumidor deduplica pelo `evento_id`
(INFRA 2.4).

Conexão por variáveis de ambiente, como as migrações: `PG_LIVRO_HOST`,
`PG_LIVRO_PORTA`, `PG_LIVRO_BANCO`, `PG_LIVRO_USUARIO_REPLAY` (padrão
livro_replay) e `PG_LIVRO_SENHA_REPLAY`. A senha não vai a log nem à saída.
"""

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence

import psycopg
from psycopg.conninfo import make_conninfo

log = logging.getLogger("relay.republicacao")

_REABRIR = """
UPDATE outbox
SET publicado_em = NULL, tentativas = 0
WHERE posicao >= %(desde_posicao)s
"""


def reabrir_desde(conninfo: str, desde_posicao: int) -> int:
    """Reabre os eventos a partir de `desde_posicao` e devolve quantos."""
    if desde_posicao <= 0:
        raise ValueError("posição deve ser positiva")
    with psycopg.connect(conninfo) as conexao, conexao.transaction():
        cursor = conexao.execute(_REABRIR, {"desde_posicao": desde_posicao})
        return cursor.rowcount


def conninfo_do_ambiente(ambiente: Mapping[str, str]) -> str:
    senha = ambiente.get("PG_LIVRO_SENHA_REPLAY")
    if not senha:
        raise SystemExit("defina PG_LIVRO_SENHA_REPLAY (ver .env.example)")
    return make_conninfo(
        host=ambiente.get("PG_LIVRO_HOST", "pg-livro"),
        port=ambiente.get("PG_LIVRO_PORTA", "5432"),
        dbname=ambiente.get("PG_LIVRO_BANCO", "livro"),
        user=ambiente.get("PG_LIVRO_USUARIO_REPLAY", "livro_replay"),
        password=senha,
    )


def _posicao(texto: str) -> int:
    valor = int(texto)
    if valor <= 0:
        raise argparse.ArgumentTypeError("a posição deve ser um inteiro positivo")
    return valor


def main(
    argv: Sequence[str] | None = None, ambiente: Mapping[str, str] | None = None
) -> int:
    parser = argparse.ArgumentParser(
        prog="republicar",
        description="Reabre eventos da outbox a partir de uma posição.",
    )
    parser.add_argument("--desde-posicao", type=_posicao, required=True)
    argumentos = parser.parse_args(argv)
    conninfo = conninfo_do_ambiente(os.environ if ambiente is None else ambiente)

    try:
        reabertos = reabrir_desde(conninfo, argumentos.desde_posicao)
    except psycopg.Error as erro:
        # Só o SQLSTATE e a mensagem principal, sem a conninfo.
        primaria = erro.diag.message_primary or type(erro).__name__
        print(f"republicação recusada ({erro.sqlstate}): {primaria}", file=sys.stderr)
        log.error(
            "republicação recusada",
            extra={
                "desde_posicao": argumentos.desde_posicao,
                "sqlstate": erro.sqlstate,
            },
        )
        return 1

    log.info(
        "eventos reabertos para republicação",
        extra={"desde_posicao": argumentos.desde_posicao, "reabertos": reabertos},
    )
    print(
        f"{reabertos} eventos reabertos a partir da posição {argumentos.desde_posicao}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
