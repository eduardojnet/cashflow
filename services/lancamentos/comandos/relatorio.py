"""Relatório de totais por data do livro até uma posição (005-T5; D-002).

    python -m lancamentos.comandos.relatorio [--ate-posicao P] > relatorio.json

Exporta, para cada data de lançamento, a quantidade de lançamentos e os
totais de créditos e de débitos com posição até P (design 005 §4,
Reconciliação). Sem `--ate-posicao`, P é a última posição do livro no
instante da leitura. O coordenador compara o relatório com a geração ativa
do consolidado (`coordenador reconciliar`) e lista divergências e lacunas;
nada é corrigido (D-004).

O relatório sai pela saída padrão, em JSON, e chega ao coordenador por
arquivo ou pela entrada padrão, nunca por rede entre os lados (INFRA 6.7).
No compose, pelo serviço `livro-relatorio` (perfil `operacao`), só na rede
do livro:

    docker compose -f infra/compose.yaml run --rm -T livro-relatorio > relatorio.json

Formato (`FORMATO`): `ate_posicao`, `gerado_em` e `datas`, uma entrada por
data com `data` (ISO 8601), `quantidade`, `total_creditos` e
`total_debitos`. Valores em texto decimal com duas casas, sem passar por
ponto flutuante.

Leitura numa só transação REPEATABLE READ somente leitura: a última posição
e os totais vêm do mesmo instante. A posição é atribuída por um contador
travado até o COMMIT do registro (002 TD-002), então o instante lido tem
todas as posições de 1 até a última, sem buraco.

Acesso (005 FR-008; INFRA 5.11, 6.4): identidade dedicada livro_replay, que
lê só posição, tipo, valor e data do livro (migração 0004). O comando
recusa qualquer outro papel antes de ler, inclusive livro_api e o dono das
tabelas, que também leem o livro. Conexão por variáveis de ambiente, como
`relay-republicar`: `PG_LIVRO_HOST`, `PG_LIVRO_PORTA`, `PG_LIVRO_BANCO`,
`PG_LIVRO_USUARIO_REPLAY` (padrão livro_replay) e `PG_LIVRO_SENHA_REPLAY`.

O relatório é conteúdo financeiro: os valores vão só para a saída padrão.
Os logs, em JSON na saída de erro, levam só posição e contagens (design 005
§6; INFRA 8.2). Código de saída 0 em sucesso, 1 em recusa ou falha do banco
e 2 em uso inválido.
"""

import argparse
import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo

from lancamentos.adaptadores.http.logs import FormatadorJson

FORMATO = "cashflow.relatorio-livro.v1"
PAPEL_RELATORIO = "livro_replay"

log = logging.getLogger("lancamentos.relatorio")

_ULTIMA_POSICAO = "SELECT coalesce(max(posicao), 0) FROM lancamento"
_TOTAIS = """
SELECT data_lancamento,
       count(*),
       coalesce(sum(valor) FILTER (WHERE tipo = 'CREDITO'), 0),
       coalesce(sum(valor) FILTER (WHERE tipo = 'DEBITO'), 0)
FROM lancamento
WHERE posicao <= %(ate_posicao)s
GROUP BY data_lancamento
ORDER BY data_lancamento
"""


class RelatorioRecusado(Exception):
    """Identidade ou posição fora do permitido; nada foi lido."""


@dataclass(frozen=True)
class TotalDoDia:
    data: date
    quantidade: int
    total_creditos: Decimal
    total_debitos: Decimal


@dataclass(frozen=True)
class RelatorioLivro:
    ate_posicao: int
    gerado_em: datetime
    datas: tuple[TotalDoDia, ...]

    @property
    def lancamentos(self) -> int:
        return sum(dia.quantidade for dia in self.datas)


def gerar_relatorio(conninfo: str, ate_posicao: int | None = None) -> RelatorioLivro:
    """Totais por data até `ate_posicao` (padrão: a última posição lida)."""
    with psycopg.connect(conninfo) as conexao:
        conexao.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        conexao.read_only = True
        linha = conexao.execute("SELECT current_user, clock_timestamp()").fetchone()
        if linha is None:
            raise RuntimeError("consulta sem linha")
        papel, gerado_em = linha
        if papel != PAPEL_RELATORIO:
            raise RelatorioRecusado(
                f"identidade {papel} não autorizada; o relatório do livro roda só"
                f" como {PAPEL_RELATORIO}"
            )
        ultima = conexao.execute(_ULTIMA_POSICAO).fetchone()
        ultima_posicao: int = 0 if ultima is None else ultima[0]
        if ate_posicao is None:
            ate_posicao = ultima_posicao
        elif ate_posicao > ultima_posicao:
            raise RelatorioRecusado(
                f"posição {ate_posicao} além da última do livro ({ultima_posicao})"
            )
        datas = tuple(
            TotalDoDia(data, quantidade, creditos, debitos)
            for data, quantidade, creditos, debitos in conexao.execute(
                _TOTAIS, {"ate_posicao": ate_posicao}
            ).fetchall()
        )
    return RelatorioLivro(ate_posicao, gerado_em, datas)


def _decimal(valor: Decimal) -> str:
    return str(valor.quantize(Decimal("0.01")))


def relatorio_em_json(relatorio: RelatorioLivro) -> dict[str, Any]:
    return {
        "formato": FORMATO,
        "ate_posicao": relatorio.ate_posicao,
        "gerado_em": relatorio.gerado_em.astimezone(UTC).isoformat(),
        "datas": [
            {
                "data": dia.data.isoformat(),
                "quantidade": dia.quantidade,
                "total_creditos": _decimal(dia.total_creditos),
                "total_debitos": _decimal(dia.total_debitos),
            }
            for dia in relatorio.datas
        ],
    }


def conninfo_do_ambiente(ambiente: Mapping[str, str]) -> str:
    senha = ambiente.get("PG_LIVRO_SENHA_REPLAY")
    if not senha:
        raise SystemExit("defina PG_LIVRO_SENHA_REPLAY (ver .env.example)")
    return make_conninfo(
        host=ambiente.get("PG_LIVRO_HOST", "pg-livro"),
        port=ambiente.get("PG_LIVRO_PORTA", "5432"),
        dbname=ambiente.get("PG_LIVRO_BANCO", "livro"),
        user=ambiente.get("PG_LIVRO_USUARIO_REPLAY", PAPEL_RELATORIO),
        password=senha,
        connect_timeout=5,
        application_name="livro-relatorio",
    )


def _posicao(texto: str) -> int:
    try:
        valor = int(texto)
    except ValueError:
        raise argparse.ArgumentTypeError("informe a posição em número") from None
    if valor <= 0:
        raise argparse.ArgumentTypeError("a posição deve ser um inteiro positivo")
    return valor


def _configurar_logging(nivel: str) -> None:
    # Saída de erro: a saída padrão é o relatório.
    saida = logging.StreamHandler(sys.stderr)
    saida.setFormatter(FormatadorJson())
    logging.basicConfig(level=nivel, handlers=[saida], force=True)


def main(
    argv: Sequence[str] | None = None, ambiente: Mapping[str, str] | None = None
) -> int:
    ambiente = os.environ if ambiente is None else ambiente
    parser = argparse.ArgumentParser(
        prog="livro-relatorio",
        description="Exporta os totais por data do livro até uma posição.",
    )
    parser.add_argument(
        "--ate-posicao",
        type=_posicao,
        default=None,
        metavar="P",
        help="última posição incluída (padrão: a última do livro)",
    )
    argumentos = parser.parse_args(argv)
    _configurar_logging(ambiente.get("LOG_NIVEL", "INFO"))
    conninfo = conninfo_do_ambiente(ambiente)

    try:
        relatorio = gerar_relatorio(conninfo, argumentos.ate_posicao)
    except RelatorioRecusado as erro:
        print(f"relatório recusado: {erro}", file=sys.stderr)
        log.warning("relatório recusado", extra={"erro_tipo": type(erro).__name__})
        return 1
    except psycopg.Error as erro:
        # Só o SQLSTATE e a mensagem principal, sem a conninfo.
        primaria = erro.diag.message_primary or type(erro).__name__
        print(f"relatório recusado ({erro.sqlstate}): {primaria}", file=sys.stderr)
        log.error("relatório recusado pelo banco", extra={"sqlstate": erro.sqlstate})
        return 1

    print(json.dumps(relatorio_em_json(relatorio), ensure_ascii=False, indent=2))
    log.info(
        "relatório do livro exportado",
        extra={
            "ate_posicao": relatorio.ate_posicao,
            "datas": len(relatorio.datas),
            "lancamentos": relatorio.lancamentos,
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
