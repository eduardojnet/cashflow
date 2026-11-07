"""Leitura do relatório de totais exportado no lado do livro (005-T5; D-002).

O relatório é o JSON de `lancamentos.comandos.relatorio`, formato
`cashflow.relatorio-livro.v1`: `ate_posicao` e `datas`, cada uma com
`data`, `quantidade`, `total_creditos` e `total_debitos` (valores em texto
decimal). Chega por arquivo ou pela entrada padrão, nunca por rede entre os
lados (INFRA 6.7). A imagem do coordenador não leva o código do livro: o
formato é repetido aqui, e um teste do coordenador lê o relatório gerado
pelo comando real do livro.

Valor em número JSON é recusado, para não passar por ponto flutuante. Toda
recusa é `RelatorioInvalido`, com o campo e a data, sem o valor (INFRA 8.2).
"""

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from coordenador.dominio import RelatorioInvalido, RelatorioLivro, TotalDoDia

FORMATO = "cashflow.relatorio-livro.v1"


def _inteiro(documento: dict[str, Any], campo: str, onde: str) -> int:
    valor = documento.get(campo)
    if not isinstance(valor, int) or isinstance(valor, bool):
        raise RelatorioInvalido(f"{campo} ausente ou não inteiro{onde}")
    return valor


def _decimal(documento: dict[str, Any], campo: str, onde: str) -> Decimal:
    texto = documento.get(campo)
    if not isinstance(texto, str):
        raise RelatorioInvalido(f"{campo} ausente ou fora de texto decimal{onde}")
    try:
        valor = Decimal(texto)
    except InvalidOperation:
        raise RelatorioInvalido(f"{campo} não é decimal{onde}") from None
    # Expoente em texto ('n', 'N', 'F') é NaN ou infinito.
    expoente = valor.as_tuple().exponent
    if not isinstance(expoente, int) or expoente < -2:
        raise RelatorioInvalido(f"{campo} não é decimal finito de até duas casas{onde}")
    return valor


def _dia(entrada: object, indice: int) -> TotalDoDia:
    if not isinstance(entrada, dict):
        raise RelatorioInvalido(f"entrada {indice} de datas não é objeto")
    texto = entrada.get("data")
    try:
        data = date.fromisoformat(texto) if isinstance(texto, str) else None
    except ValueError:
        data = None
    if data is None:
        raise RelatorioInvalido(f"data ausente ou fora de ISO 8601 na entrada {indice}")
    onde = f" em {data.isoformat()}"
    return TotalDoDia(
        data=data,
        quantidade=_inteiro(entrada, "quantidade", onde),
        total_creditos=_decimal(entrada, "total_creditos", onde),
        total_debitos=_decimal(entrada, "total_debitos", onde),
    )


def ler_relatorio(texto: str) -> RelatorioLivro:
    """Relatório do livro validado a partir do JSON exportado."""
    try:
        documento = json.loads(texto)
    except json.JSONDecodeError:
        raise RelatorioInvalido("não é JSON") from None
    if not isinstance(documento, dict):
        raise RelatorioInvalido("não é objeto JSON")
    if documento.get("formato") != FORMATO:
        raise RelatorioInvalido(f"formato diferente de {FORMATO}")
    datas = documento.get("datas")
    if not isinstance(datas, list):
        raise RelatorioInvalido("datas ausente ou não é lista")
    return RelatorioLivro(
        ate_posicao=_inteiro(documento, "ate_posicao", ""),
        datas=tuple(_dia(entrada, indice) for indice, entrada in enumerate(datas)),
    )
