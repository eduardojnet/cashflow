"""CLI do coordenador da projeção (005-T3, 005-T4, 005-T5; design 005 §3, §4).

    python -m coordenador reconstruir iniciar
    python -m coordenador reconstruir status [--json] [--lacunas N]
    python -m coordenador reconstruir cancelar
    python -m coordenador geracao trocar [--tempo-limite S]
    python -m coordenador geracao rollback [--tempo-limite S]
    python -m coordenador geracao descartar
    python -m coordenador reconciliar [--relatorio-livro ARQUIVO] [--json]
                                      [--faltantes N]

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
4. `geracao trocar`: ergue a barreira, espera a pausa dos consumidores,
   confere o critério com eles parados e troca a ativa numa transação; a
   ativa de antes fica `anterior` (D-005). Recusada, nada muda.
5. Se a nova ativa estiver ruim, `geracao rollback` devolve a anterior,
   pelo mesmo protocolo de barreira (FR-006).
6. `geracao descartar` descarta a anterior e libera o espaço dela; só então
   uma nova reconstrução pode começar (INFRA 7.5).

`reconstruir cancelar` descarta a candidata antes da troca.

Reconciliação (FR-001; D-002, D-004): no lado do livro, o operador exporta
os totais por data até a última posição e entrega o relatório ao
coordenador pela entrada padrão (ou por arquivo, com `--relatorio-livro`),
sem rede entre os lados (INFRA 6.7):

    docker compose -f infra/compose.yaml run --rm -T livro-relatorio > relatorio.json
    docker compose -f infra/compose.yaml run --rm -T coordenador reconciliar < relatorio.json

O coordenador compara com a geração ativa e lista as datas divergentes, as
posições faltantes até a posição do relatório e as datas em que o saldo
materializado não bate com as contribuições. Só reporta: a correção é a
reconstrução (D-004). Os valores aparecem só no relatório, na saída padrão,
nunca no log (design 005 §6).

Barreira (DQ-001): a troca e o rollback esperam a pausa de cada consumidor
registrado até `--tempo-limite` segundos (padrão:
`COORDENADOR_BARREIRA_TEMPO_LIMITE_SEGUNDOS`, ou 30). Sem confirmação, a
operação é recusada e a barreira, liberada. Se o coordenador cair com a
barreira erguida, o consumo fica pausado até a próxima `geracao trocar` ou
`geracao rollback`, que libera a barreira ao terminar, aceita ou recusada.

Acesso (FR-008; INFRA 5.11, 6.4): conexão como consolidado_operacao, por
variáveis de ambiente: `PG_CONSOLIDADO_HOST`, `PG_CONSOLIDADO_PORTA`,
`PG_CONSOLIDADO_BANCO`, `PG_CONSOLIDADO_USUARIO_OPERACAO` (padrão
consolidado_operacao) e `PG_CONSOLIDADO_SENHA_OPERACAO`. Com outra
identidade, o comando termina com erro sem mudar nada: o banco nega os
privilégios da migração 0002 aos papéis do worker e da API, e o adaptador
recusa qualquer papel que não seja consolidado_operacao, inclusive o dono
das tabelas. A senha não vai a log nem à saída.

Saída: o resultado vai para a saída padrão; os logs JSON, para a de erro.
Código de saída 0 em sucesso, 1 em operação recusada ou falha do banco, 2
em uso inválido e 3 na reconciliação que encontra divergência ou lacuna. O `status` mostra só identificadores, estados, posições e
contagens, nunca valores (design 005 §6).
"""

import argparse
import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo

from coordenador.adaptadores.logs import configurar_logging
from coordenador.adaptadores.postgres import (
    abridor_de_transacao,
    bloqueio_de_operacao,
)
from coordenador.adaptadores.relatorio_livro import ler_relatorio
from coordenador.aplicacao import (
    CancelarReconstrucao,
    ConfiguracaoBarreira,
    ConsultarSituacao,
    DescartarAnterior,
    IdentidadeRecusada,
    IniciarReconstrucao,
    OperacaoRecusada,
    Reconciliar,
    ReverterTroca,
    Situacao,
    SituacaoGeracao,
    TrocarGeracao,
)
from coordenador.aplicacao.geracoes import TEMPO_LIMITE_PADRAO_SEGUNDOS
from coordenador.dominio import (
    Divergencia,
    ErroDominio,
    Reconciliacao,
    SaldoInconsistente,
    SaldoMaterializado,
    TotalDoDia,
)

COMPONENTE = "coordenador"
VARIAVEL_TEMPO_LIMITE = "COORDENADOR_BARREIRA_TEMPO_LIMITE_SEGUNDOS"
# Reconciliação concluída com divergência ou lacuna (FR-001).
CODIGO_DIVERGENCIA = 3
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


def _segundos(texto: str) -> float:
    try:
        valor = float(texto)
    except ValueError:
        raise argparse.ArgumentTypeError("informe os segundos em número") from None
    if not valor > 0:
        raise argparse.ArgumentTypeError("o tempo limite deve ser positivo")
    return valor


def tempo_limite_do_ambiente(ambiente: Mapping[str, str]) -> float:
    texto = ambiente.get(VARIAVEL_TEMPO_LIMITE)
    if not texto:
        return TEMPO_LIMITE_PADRAO_SEGUNDOS
    try:
        return _segundos(texto)
    except argparse.ArgumentTypeError as erro:
        raise SystemExit(f"{VARIAVEL_TEMPO_LIMITE} inválido: {erro}") from None


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

    geracao = grupos.add_parser(
        "geracao", help="trocar a geração ativa, desfazer a troca ou descartar"
    )
    operacoes = geracao.add_subparsers(dest="acao", required=True)
    for acao, ajuda in (
        ("trocar", "torna a candidata a ativa, com barreira e critério FR-009"),
        ("rollback", "devolve a geração anterior a ativa, com barreira"),
    ):
        operacao = operacoes.add_parser(acao, help=ajuda)
        operacao.add_argument(
            "--tempo-limite",
            type=_segundos,
            default=None,
            metavar="S",
            help=(
                "segundos de espera pela pausa dos consumidores (padrão:"
                f" {VARIAVEL_TEMPO_LIMITE} ou {TEMPO_LIMITE_PADRAO_SEGUNDOS:g})"
            ),
        )
    operacoes.add_parser(
        "descartar", help="descarta a geração anterior e libera o espaço dela"
    )

    reconciliar = grupos.add_parser(
        "reconciliar",
        help="compara a geração ativa com o relatório de totais do livro",
    )
    reconciliar.add_argument(
        "--relatorio-livro",
        default="-",
        metavar="ARQUIVO",
        help="relatório de livro-relatorio; '-' lê da entrada padrão (padrão)",
    )
    reconciliar.add_argument("--json", action="store_true", help="saída em JSON")
    reconciliar.add_argument(
        "--faltantes",
        type=_limite,
        default=20,
        metavar="N",
        help="quantos intervalos de posições faltantes listar (padrão: 20)",
    )
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


def _valor(valor: Decimal) -> str:
    return str(valor.quantize(Decimal("0.01")))


def _total_em_json(total: TotalDoDia | None) -> dict[str, Any] | None:
    if total is None:
        return None
    return {
        "quantidade": total.quantidade,
        "total_creditos": _valor(total.total_creditos),
        "total_debitos": _valor(total.total_debitos),
    }


def _saldo_em_json(saldo: SaldoMaterializado) -> dict[str, str]:
    return {
        "total_creditos": _valor(saldo.total_creditos),
        "total_debitos": _valor(saldo.total_debitos),
    }


def reconciliacao_em_json(reconciliacao: Reconciliacao) -> dict[str, Any]:
    return {
        "geracao_id": reconciliacao.geracao_id,
        "ate_posicao": reconciliacao.ate_posicao,
        "checkpoint": reconciliacao.checkpoint,
        "conciliada": reconciliacao.conciliada,
        "datas_no_livro": reconciliacao.datas_no_livro,
        "divergencias": [
            {
                "data": d.data.isoformat(),
                "tipo": str(d.tipo),
                "livro": _total_em_json(d.livro),
                "projecao": _total_em_json(d.projecao),
            }
            for d in reconciliacao.divergencias
        ],
        "posicoes_faltantes": reconciliacao.posicoes_faltantes,
        "primeiros_faltantes": [
            {"inicio": f.inicio, "fim": f.fim}
            for f in reconciliacao.primeiros_faltantes
        ],
        "saldo_inconsistente": [
            {
                "data": s.data.isoformat(),
                "saldo": _saldo_em_json(s.saldo),
                "contribuicoes": _saldo_em_json(s.contribuicoes),
            }
            for s in reconciliacao.saldo_inconsistente
        ],
    }


def _total_em_texto(total: TotalDoDia | None) -> str:
    if total is None:
        return "nenhum lançamento"
    return (
        f"{total.quantidade} lançamentos, créditos {_valor(total.total_creditos)},"
        f" débitos {_valor(total.total_debitos)}"
    )


_TIPO_EM_TEXTO = {
    "ausente_na_projecao": "data ausente da projeção",
    "ausente_no_livro": "data ausente do livro",
    "totais_diferentes": "totais diferentes",
}


def _divergencia_em_texto(divergencia: Divergencia) -> list[str]:
    tipo = _TIPO_EM_TEXTO[str(divergencia.tipo)]
    return [
        f"divergência em {divergencia.data.isoformat()}: {tipo}",
        f"  livro:    {_total_em_texto(divergencia.livro)}",
        f"  projeção: {_total_em_texto(divergencia.projecao)}",
    ]


def _saldo_em_texto(saldo: SaldoMaterializado) -> str:
    return (
        f"créditos {_valor(saldo.total_creditos)},"
        f" débitos {_valor(saldo.total_debitos)}"
    )


def _inconsistencia_em_texto(inconsistente: SaldoInconsistente) -> list[str]:
    data = inconsistente.data.isoformat()
    return [
        f"saldo materializado diferente das contribuições em {data}:",
        f"  saldo_diario:  {_saldo_em_texto(inconsistente.saldo)}",
        f"  contribuições: {_saldo_em_texto(inconsistente.contribuicoes)}",
    ]


def reconciliacao_em_texto(reconciliacao: Reconciliacao) -> str:
    linhas = [
        (
            f"reconciliação da geração ativa {reconciliacao.geracao_id} até a"
            f" posição {reconciliacao.ate_posicao}"
            f" (checkpoint da ativa: {reconciliacao.checkpoint})"
        ),
        (
            f"datas no livro: {reconciliacao.datas_no_livro};"
            f" datas divergentes: {len(reconciliacao.divergencias)};"
            f" posições faltantes: {reconciliacao.posicoes_faltantes}"
        ),
    ]
    for divergencia in reconciliacao.divergencias:
        linhas.extend(_divergencia_em_texto(divergencia))
    if reconciliacao.primeiros_faltantes:
        intervalos = ", ".join(
            str(f.inicio) if f.inicio == f.fim else f"{f.inicio}-{f.fim}"
            for f in reconciliacao.primeiros_faltantes
        )
        linhas.append(f"posições faltantes na ativa (primeiras): {intervalos}")
    for inconsistente in reconciliacao.saldo_inconsistente:
        linhas.extend(_inconsistencia_em_texto(inconsistente))
    if reconciliacao.conciliada:
        linhas.append("resultado: conciliada")
    else:
        linhas.append(
            "resultado: divergente; corrigir por reconstrução (reconstruir iniciar)"
        )
    return "\n".join(linhas)


def _ler_relatorio_livro(caminho: str) -> str:
    if caminho == "-":
        return sys.stdin.read()
    return Path(caminho).read_text(encoding="utf-8")


def _executar_reconciliacao(
    argumentos: argparse.Namespace, conninfo: str
) -> tuple[str, int]:
    try:
        texto = _ler_relatorio_livro(argumentos.relatorio_livro)
    except (OSError, UnicodeDecodeError) as erro:
        raise OperacaoRecusada(
            f"relatório do livro ilegível ({type(erro).__name__})"
        ) from None
    relatorio = ler_relatorio(texto)
    reconciliacao = Reconciliar(abridor_de_transacao(conninfo, somente_leitura=True))(
        relatorio, argumentos.faltantes
    )
    codigo = 0 if reconciliacao.conciliada else CODIGO_DIVERGENCIA
    if argumentos.json:
        documento = reconciliacao_em_json(reconciliacao)
        return json.dumps(documento, ensure_ascii=False, indent=2), codigo
    return reconciliacao_em_texto(reconciliacao), codigo


def _executar_geracao(
    argumentos: argparse.Namespace, conninfo: str, ambiente: Mapping[str, str]
) -> str:
    abrir = abridor_de_transacao(conninfo)
    bloquear = bloqueio_de_operacao(conninfo)
    if argumentos.acao == "descartar":
        descartada = DescartarAnterior(abrir, bloquear)()
        return (
            f"geração {descartada.geracao_id} descartada; dados removidos."
            " Uma nova reconstrução já pode começar."
        )
    tempo_limite = argumentos.tempo_limite or tempo_limite_do_ambiente(ambiente)
    configuracao = ConfiguracaoBarreira(tempo_limite_segundos=tempo_limite)
    if argumentos.acao == "trocar":
        trocada = TrocarGeracao(abrir, bloquear, configuracao)()
        return (
            f"geração {trocada.ativa_id} ativa; geração {trocada.anterior_id}"
            " anterior, retida para rollback até: geracao descartar"
        )
    revertida = ReverterTroca(abrir, bloquear, configuracao)()
    return (
        f"rollback concluído: geração {revertida.ativa_id} ativa; geração"
        f" {revertida.anterior_id} anterior"
    )


def _executar(
    argumentos: argparse.Namespace, conninfo: str, ambiente: Mapping[str, str]
) -> str | tuple[str, int]:
    if argumentos.grupo == "reconciliar":
        return _executar_reconciliacao(argumentos, conninfo)
    if argumentos.grupo == "geracao":
        return _executar_geracao(argumentos, conninfo, ambiente)
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
    comando = " ".join(
        parte
        for parte in (argumentos.grupo, getattr(argumentos, "acao", None))
        if parte
    )

    try:
        resultado = _executar(argumentos, conninfo, ambiente)
    except (ErroDominio, IdentidadeRecusada, OperacaoRecusada) as erro:
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

    saida, codigo = resultado if isinstance(resultado, tuple) else (resultado, 0)
    print(saida)
    return codigo
