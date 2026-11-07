"""CLI do coordenador da projeção (005-T3; design 005 §3, §4).

    python -m coordenador reconstruir iniciar
    python -m coordenador reconstruir status [--json] [--lacunas N]
    python -m coordenador reconstruir cancelar

No compose, pelo serviço `coordenador` (perfil `operacao`), só na rede do
consolidado:

    docker compose -f infra/compose.yaml run --rm coordenador reconstruir status

Sequência de uma reconstrução (design 005 §3):

1. `reconstruir iniciar`: cria a geração candidata; o worker passa a aplicar
   cada mensagem também nela (TD-001).
2. No lado do livro, o operador reabre a outbox desde a posição 1:
   `docker compose -f infra/compose.yaml run --rm relay-republicar
   --desde-posicao 1` (D-002). O coordenador não alcança o livro (INFRA 6.7).
3. `reconstruir status` até o critério de troca (FR-009) ficar atendido.
4. Troca, rollback e descarte: `geracao trocar|rollback|descartar` (005-T4).

`reconstruir cancelar` descarta a candidata antes da troca.

Acesso (FR-008; INFRA 5.11, 6.4): conexão como consolidado_operacao, por
variáveis de ambiente: `PG_CONSOLIDADO_HOST`, `PG_CONSOLIDADO_PORTA`,
`PG_CONSOLIDADO_BANCO`, `PG_CONSOLIDADO_USUARIO_OPERACAO` (padrão
consolidado_operacao) e `PG_CONSOLIDADO_SENHA_OPERACAO`. Com outra
identidade, o comando termina com erro sem mudar nada: o banco nega os
privilégios da migração 0002 aos papéis do worker e da API, e o adaptador
recusa qualquer papel que não seja consolidado_operacao, inclusive o dono
das tabelas. A senha não vai a log nem à saída.

Saída: o resultado vai para a saída padrão; os logs JSON, para a de erro.
Código de saída 0 em sucesso, 1 em operação recusada ou falha do banco e 2
em uso inválido. O `status` mostra só identificadores, estados, posições e
contagens, nunca valores (design 005 §6).
"""

import argparse
import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo

from coordenador.adaptadores.logs import configurar_logging
from coordenador.adaptadores.postgres import abridor_de_transacao
from coordenador.aplicacao import (
    CancelarReconstrucao,
    ConsultarSituacao,
    IdentidadeRecusada,
    IniciarReconstrucao,
    Situacao,
    SituacaoGeracao,
)
from coordenador.dominio import ErroDominio

COMPONENTE = "coordenador"
REPUBLICAR = (
    "docker compose -f infra/compose.yaml run --rm relay-republicar --desde-posicao 1"
)

log = logging.getLogger("coordenador.cli")


def conninfo_do_ambiente(ambiente: Mapping[str, str]) -> str:
    senha = ambiente.get("PG_CONSOLIDADO_SENHA_OPERACAO")
    if not senha:
        raise SystemExit("defina PG_CONSOLIDADO_SENHA_OPERACAO (ver .env.example)")
    return make_conninfo(
        host=ambiente.get("PG_CONSOLIDADO_HOST", "pg-consolidado"),
        port=ambiente.get("PG_CONSOLIDADO_PORTA", "5432"),
        dbname=ambiente.get("PG_CONSOLIDADO_BANCO", "consolidado"),
        user=ambiente.get("PG_CONSOLIDADO_USUARIO_OPERACAO", "consolidado_operacao"),
        password=senha,
        connect_timeout=5,
        application_name=COMPONENTE,
    )


def _limite(texto: str) -> int:
    valor = int(texto)
    if valor < 0:
        raise argparse.ArgumentTypeError("o limite deve ser zero ou positivo")
    return valor


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coordenador",
        description="Reconstrução e gerações da projeção do consolidado.",
    )
    grupos = parser.add_subparsers(dest="grupo", required=True)
    reconstruir = grupos.add_parser(
        "reconstruir", help="iniciar, acompanhar ou cancelar a reconstrução"
    )
    acoes = reconstruir.add_subparsers(dest="acao", required=True)
    acoes.add_parser("iniciar", help="cria a geração candidata")
    status = acoes.add_parser(
        "status", help="checkpoint, lacunas e fase de cada geração"
    )
    status.add_argument("--json", action="store_true", help="saída em JSON")
    status.add_argument(
        "--lacunas",
        type=_limite,
        default=20,
        metavar="N",
        help="quantas posições em lacuna listar por geração (padrão: 20)",
    )
    acoes.add_parser("cancelar", help="descarta a geração candidata")
    return parser


def _instante(valor: datetime | None) -> str | None:
    return None if valor is None else valor.isoformat()


def _geracao_em_json(geracao: SituacaoGeracao) -> dict[str, Any]:
    return {
        "id": geracao.id,
        "estado": str(geracao.estado),
        "criada_em": _instante(geracao.criada_em),
        "ativada_em": _instante(geracao.ativada_em),
        "checkpoint": geracao.checkpoint,
        "checkpoint_atualizado_em": _instante(geracao.checkpoint_atualizado_em),
        "maior_posicao": geracao.maior_posicao,
        "lacunas": geracao.lacunas,
        "primeiras_lacunas": list(geracao.primeiras_lacunas),
        "fase": geracao.fase,
        "fase_atualizada_em": _instante(geracao.fase_atualizada_em),
    }


def situacao_em_json(situacao: Situacao) -> dict[str, Any]:
    pendencias = situacao.pendencias_de_troca()
    return {
        "geracao_ativa_id": situacao.geracao_ativa_id,
        "barreira": situacao.barreira,
        "versao": situacao.versao,
        "geracoes": [_geracao_em_json(g) for g in situacao.geracoes],
        "criterio_de_troca": None
        if pendencias is None
        else {"atendido": not pendencias, "pendencias": pendencias},
        "consumidores": [
            {
                "consumidor_id": c.consumidor_id,
                "pausado": c.pausado,
                "visto_em": _instante(c.visto_em),
            }
            for c in situacao.consumidores
        ],
    }


def situacao_em_texto(situacao: Situacao) -> str:
    linhas = [
        (
            f"geração ativa: {situacao.geracao_ativa_id}"
            f" (barreira {situacao.barreira}, versão {situacao.versao})"
        )
    ]
    for geracao in situacao.geracoes:
        linhas.append(f"geração {geracao.id}: {geracao.estado}")
        linhas.append(
            f"  checkpoint: {geracao.checkpoint}"
            f" (maior posição projetada: {geracao.maior_posicao})"
        )
        lacunas = f"  lacunas: {geracao.lacunas}"
        if geracao.primeiras_lacunas:
            posicoes = ", ".join(str(p) for p in geracao.primeiras_lacunas)
            lacunas += f" (menores posições: {posicoes})"
        linhas.append(lacunas)
        if geracao.fase is not None:
            linhas.append(
                f"  fase: {geracao.fase} desde {_instante(geracao.fase_atualizada_em)}"
            )
    pendencias = situacao.pendencias_de_troca()
    if pendencias is None:
        linhas.append("reconstrução: nenhuma em andamento")
    elif pendencias:
        linhas.append(
            "critério de troca (FR-009): não atendido; " + "; ".join(pendencias)
        )
    else:
        linhas.append("critério de troca (FR-009): atendido nesta leitura")
    for consumidor in situacao.consumidores:
        estado = "pausado" if consumidor.pausado else "consumindo"
        linhas.append(
            f"consumidor {consumidor.consumidor_id}: {estado}"
            f" (visto em {_instante(consumidor.visto_em)})"
        )
    return "\n".join(linhas)


def _executar(argumentos: argparse.Namespace, conninfo: str) -> str:
    if argumentos.acao == "iniciar":
        iniciada = IniciarReconstrucao(abridor_de_transacao(conninfo))()
        return (
            f"geração candidata {iniciada.geracao_id} criada (construindo).\n"
            f"Próximo passo, no lado do livro: {REPUBLICAR}\n"
            "Depois, acompanhar com: reconstruir status"
        )
    if argumentos.acao == "cancelar":
        cancelada = CancelarReconstrucao(abridor_de_transacao(conninfo))()
        return f"geração candidata {cancelada.geracao_id} cancelada (descartada)."
    situacao = ConsultarSituacao(abridor_de_transacao(conninfo, somente_leitura=True))(
        argumentos.lacunas
    )
    if argumentos.json:
        return json.dumps(situacao_em_json(situacao), ensure_ascii=False, indent=2)
    return situacao_em_texto(situacao)


def main(
    argv: Sequence[str] | None = None, ambiente: Mapping[str, str] | None = None
) -> int:
    ambiente = os.environ if ambiente is None else ambiente
    argumentos = _parser().parse_args(argv)
    configurar_logging(ambiente.get("LOG_NIVEL", "INFO"))
    conninfo = conninfo_do_ambiente(ambiente)
    comando = f"{argumentos.grupo} {argumentos.acao}"

    try:
        saida = _executar(argumentos, conninfo)
    except (ErroDominio, IdentidadeRecusada) as erro:
        print(f"{comando} recusado: {erro}", file=sys.stderr)
        log.warning(
            "operação recusada",
            extra={"comando": comando, "erro_tipo": type(erro).__name__},
        )
        return 1
    except psycopg.Error as erro:
        # Só o SQLSTATE e a mensagem principal, sem a conninfo.
        primaria = erro.diag.message_primary or type(erro).__name__
        print(f"{comando} recusado ({erro.sqlstate}): {primaria}", file=sys.stderr)
        log.error(
            "operação recusada pelo banco",
            extra={"comando": comando, "sqlstate": erro.sqlstate},
        )
        return 1

    print(saida)
    return 0
