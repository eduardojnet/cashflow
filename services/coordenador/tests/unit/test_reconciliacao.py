"""Reconciliação com o livro sem banco (005-T5; FR-001; D-002, D-004).

Comparação por data, coerência do saldo materializado, validação do
relatório do livro e saída da CLI, com a transação como dublê em memória.
A leitura real dos dois bancos está nos testes de integração.
"""

import io
import json
import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from coordenador import cli
from coordenador.adaptadores.logs import FormatadorJson
from coordenador.adaptadores.relatorio_livro import FORMATO, ler_relatorio
from coordenador.aplicacao import Reconciliar
from coordenador.dominio import (
    Divergencia,
    IntervaloFaltante,
    RelatorioInvalido,
    RelatorioLivro,
    SaldoMaterializado,
    TipoDivergencia,
    TotalDoDia,
    comparar_saldo_materializado,
    comparar_totais,
)

D1, D2, D3 = date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)
T = TipoDivergencia


def _dia(data: date, quantidade: int, creditos: str, debitos: str) -> TotalDoDia:
    return TotalDoDia(data, quantidade, Decimal(creditos), Decimal(debitos))


def _saldo(data: date, creditos: str, debitos: str) -> SaldoMaterializado:
    return SaldoMaterializado(data, Decimal(creditos), Decimal(debitos))


LIVRO = RelatorioLivro(
    ate_posicao=5,
    datas=(
        _dia(D1, 2, "1234.56", "789.01"),
        _dia(D2, 1, "4321.09", "0.00"),
        _dia(D3, 2, "555.37", "98.76"),
    ),
)


def _json_do_livro(**alteracoes: object) -> str:
    documento: dict[str, object] = {
        "formato": FORMATO,
        "ate_posicao": 3,
        "gerado_em": "2026-11-07T12:00:00+00:00",
        "datas": [
            {
                "data": "2026-11-03",
                "quantidade": 2,
                "total_creditos": "10.00",
                "total_debitos": "2.50",
            },
            {
                "data": "2026-11-04",
                "quantidade": 1,
                "total_creditos": "0.00",
                "total_debitos": "7.25",
            },
        ],
    }
    documento.update(alteracoes)
    return json.dumps(documento)


def test_datas_iguais_nao_divergem() -> None:
    assert comparar_totais(LIVRO.datas, LIVRO.datas) == []


def test_cada_tipo_de_divergencia_por_data_em_ordem() -> None:
    projecao = [
        _dia(D3, 1, "0.00", "98.76"),
        _dia(D1, 2, "1234.56", "789.01"),
        _dia(date(2026, 11, 6), 1, "1.00", "0.00"),
    ]

    divergencias = comparar_totais(LIVRO.datas, projecao)

    assert divergencias == [
        Divergencia(D2, T.AUSENTE_NA_PROJECAO, LIVRO.datas[1], None),
        Divergencia(D3, T.TOTAIS_DIFERENTES, LIVRO.datas[2], projecao[0]),
        Divergencia(date(2026, 11, 6), T.AUSENTE_NO_LIVRO, None, projecao[2]),
    ]


def test_quantidade_diferente_com_mesmos_totais_diverge() -> None:
    projecao = [_dia(D1, 3, "1234.56", "789.01")]

    divergencias = comparar_totais(LIVRO.datas[:1], projecao)

    assert [d.tipo for d in divergencias] == [T.TOTAIS_DIFERENTES]


def test_escala_diferente_do_mesmo_valor_nao_diverge() -> None:
    projecao = [_dia(D1, 2, "1234.560", "789.0100")]

    assert comparar_totais(LIVRO.datas[:1], projecao) == []


def test_saldo_materializado_diferente_das_contribuicoes() -> None:
    saldos = [_saldo(D1, "10.00", "2.00"), _saldo(D2, "0.00", "0.00")]
    contribuicoes = [_saldo(D1, "10.00", "3.00"), _saldo(D3, "1.00", "0.00")]

    inconsistentes = comparar_saldo_materializado(saldos, contribuicoes)

    # D2 zerado sem contribuição é coerente; D3 sem linha de saldo, não.
    assert [i.data for i in inconsistentes] == [D1, D3]
    assert inconsistentes[1].saldo == _saldo(D3, "0", "0")


@pytest.mark.parametrize(
    ("datas", "motivo"),
    [
        ((_dia(D1, 2, "1.00", "0.00"),), "soma das quantidades"),
        ((_dia(D1, 1, "1.00", "0.00"), _dia(D1, 1, "1.00", "0.00")), "repetida"),
        ((_dia(D1, 2, "1.00", "0.00"), _dia(D2, 0, "0.00", "0.00")), "sem lança"),
    ],
    ids=["soma-diferente-da-posicao", "data-repetida", "data-vazia"],
)
def test_relatorio_incoerente_e_recusado(
    datas: tuple[TotalDoDia, ...], motivo: str
) -> None:
    with pytest.raises(RelatorioInvalido, match=motivo):
        RelatorioLivro(ate_posicao=1 if len(datas) == 1 else 2, datas=datas)


def test_total_negativo_e_recusado() -> None:
    with pytest.raises(RelatorioInvalido, match="negativo"):
        _dia(D1, 1, "-1.00", "0.00")


def test_relatorio_vazio_do_livro_vazio_e_valido() -> None:
    assert ler_relatorio(_json_do_livro(ate_posicao=0, datas=[])).datas == ()


def test_le_o_relatorio_exportado_pelo_livro() -> None:
    relatorio = ler_relatorio(_json_do_livro())

    assert relatorio == RelatorioLivro(
        ate_posicao=3,
        datas=(_dia(D1, 2, "10.00", "2.50"), _dia(D2, 1, "0.00", "7.25")),
    )


@pytest.mark.parametrize(
    ("texto", "motivo"),
    [
        ("{", "não é JSON"),
        ("[]", "não é objeto"),
        (_json_do_livro(formato="outro"), "formato"),
        (_json_do_livro(ate_posicao="3"), "ate_posicao"),
        (_json_do_livro(ate_posicao=True), "ate_posicao"),
        (_json_do_livro(datas={}), "datas"),
        (_json_do_livro(datas=[1]), "entrada 0"),
    ],
    ids=[
        "json-invalido",
        "nao-objeto",
        "formato",
        "posicao-texto",
        "posicao-booleana",
        "datas-nao-lista",
        "entrada-nao-objeto",
    ],
)
def test_documento_fora_do_formato_e_recusado(texto: str, motivo: str) -> None:
    with pytest.raises(RelatorioInvalido, match=motivo):
        ler_relatorio(texto)


@pytest.mark.parametrize(
    ("campo", "conteudo", "motivo"),
    [
        ("data", "03/11/2026", "ISO 8601"),
        ("quantidade", "2", "quantidade"),
        ("total_creditos", 10.0, "texto decimal"),
        ("total_creditos", "dez", "não é decimal"),
        ("total_creditos", "10.001", "até duas casas"),
        ("total_debitos", "NaN", "finito"),
        ("total_debitos", "-2.50", "negativo"),
    ],
)
def test_entrada_invalida_e_recusada_sem_o_valor(
    campo: str, conteudo: object, motivo: str
) -> None:
    documento = json.loads(_json_do_livro())
    documento["datas"][0][campo] = conteudo

    with pytest.raises(RelatorioInvalido, match=motivo) as erro:
        ler_relatorio(json.dumps(documento))

    if campo.startswith("total_"):
        assert str(conteudo) not in str(erro.value)


@dataclass
class TransacaoFalsa:
    """Ativa 7 com checkpoint 3: posições 1 a 3 e 5 projetadas, falta a 4."""

    projecao: list[TotalDoDia] = field(
        default_factory=lambda: [
            _dia(D1, 2, "1234.56", "789.01"),
            _dia(D2, 1, "4321.09", "0.00"),
            _dia(D3, 1, "0.00", "98.76"),
        ]
    )
    saldos_diarios: list[SaldoMaterializado] = field(
        default_factory=lambda: [
            _saldo(D1, "1234.56", "789.01"),
            _saldo(D2, "4321.09", "0.00"),
            _saldo(D3, "0.00", "98.76"),
        ]
    )
    faltantes: tuple[int, list[IntervaloFaltante]] = (1, [IntervaloFaltante(4, 4)])
    chamadas: list[tuple[object, ...]] = field(default_factory=list)

    def operador(self) -> str:
        return "consolidado_operacao"

    def geracao_ativa(self) -> tuple[int, int]:
        return 7, 3

    def totais_ate(self, geracao_id: int, ate_posicao: int) -> list[TotalDoDia]:
        self.chamadas.append(("totais_ate", geracao_id, ate_posicao))
        return self.projecao

    def faltantes_ate(
        self, geracao_id: int, ate_posicao: int, limite: int
    ) -> tuple[int, list[IntervaloFaltante]]:
        self.chamadas.append(("faltantes_ate", geracao_id, ate_posicao, limite))
        return self.faltantes

    def saldos(self, geracao_id: int) -> list[SaldoMaterializado]:
        return self.saldos_diarios

    def totais_das_contribuicoes(self, geracao_id: int) -> list[SaldoMaterializado]:
        return [
            SaldoMaterializado(d.data, d.total_creditos, d.total_debitos)
            for d in self.projecao
        ]


def _abridor(
    transacao: TransacaoFalsa,
) -> Callable[[], AbstractContextManager[TransacaoFalsa]]:
    @contextmanager
    def abrir() -> Iterator[TransacaoFalsa]:
        yield transacao

    return abrir


def test_reconciliar_compara_a_ativa_ate_a_posicao_do_relatorio(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transacao = TransacaoFalsa()
    caplog.set_level(logging.INFO, logger="coordenador.reconciliacao")

    resultado = Reconciliar(_abridor(transacao))(LIVRO, limite_faltantes=10)

    assert transacao.chamadas == [("totais_ate", 7, 5), ("faltantes_ate", 7, 5, 10)]
    assert (resultado.geracao_id, resultado.ate_posicao, resultado.checkpoint) == (
        7,
        5,
        3,
    )
    assert [d.data for d in resultado.divergencias] == [D3]
    assert resultado.posicoes_faltantes == 1
    assert resultado.saldo_inconsistente == ()
    assert not resultado.conciliada

    # O log, no formato de produção, leva contagens e nenhum valor.
    linhas = [FormatadorJson().format(r) for r in caplog.records]
    assert len(linhas) == 1
    registro = json.loads(linhas[0])
    assert registro["mensagem"] == "reconciliação concluída"
    assert registro["datas_divergentes"] == 1
    for dia in LIVRO.datas:
        for valor in (dia.total_creditos, dia.total_debitos):
            if valor:
                assert str(valor) not in linhas[0]


def test_ativa_igual_ao_livro_concilia() -> None:
    transacao = TransacaoFalsa(projecao=list(LIVRO.datas), faltantes=(0, []))
    transacao.saldos_diarios = transacao.totais_das_contribuicoes(7)

    assert Reconciliar(_abridor(transacao))(LIVRO).conciliada


def test_limite_negativo_e_recusado() -> None:
    with pytest.raises(ValueError, match="negativo"):
        Reconciliar(_abridor(TransacaoFalsa()))(LIVRO, limite_faltantes=-1)


def _executar_cli(
    monkeypatch: pytest.MonkeyPatch,
    transacao: TransacaoFalsa,
    argumentos: list[str],
    entrada: str = "",
) -> int:
    monkeypatch.setattr(
        cli,
        "abridor_de_transacao",
        lambda conninfo, somente_leitura=False: _abridor(transacao),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(entrada))
    raiz = logging.getLogger()
    handlers, nivel = raiz.handlers[:], raiz.level
    try:
        return cli.main(
            argumentos, ambiente={"PG_CONSOLIDADO_SENHA_OPERACAO": "nao-usada"}
        )
    finally:
        raiz.handlers[:] = handlers
        raiz.setLevel(nivel)


def _relatorio_em_json(relatorio: RelatorioLivro) -> str:
    return json.dumps(
        {
            "formato": FORMATO,
            "ate_posicao": relatorio.ate_posicao,
            "datas": [
                {
                    "data": d.data.isoformat(),
                    "quantidade": d.quantidade,
                    "total_creditos": str(d.total_creditos),
                    "total_debitos": str(d.total_debitos),
                }
                for d in relatorio.datas
            ],
        }
    )


def test_cli_le_o_relatorio_da_entrada_padrao_e_sai_com_3_na_divergencia(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    codigo = _executar_cli(
        monkeypatch, TransacaoFalsa(), ["reconciliar"], _relatorio_em_json(LIVRO)
    )

    saida = capsys.readouterr()
    assert codigo == cli.CODIGO_DIVERGENCIA == 3
    assert saida.out.splitlines() == [
        "reconciliação da geração ativa 7 até a posição 5 (checkpoint da ativa: 3)",
        "datas no livro: 3; datas divergentes: 1; posições faltantes: 1",
        "divergência em 2026-11-05: totais diferentes",
        "  livro:    2 lançamentos, créditos 555.37, débitos 98.76",
        "  projeção: 1 lançamentos, créditos 0.00, débitos 98.76",
        "posições faltantes na ativa (primeiras): 4",
        "resultado: divergente; corrigir por reconstrução (reconstruir iniciar)",
    ]
    for valor in ("555.37", "98.76", "1234.56"):
        assert valor not in saida.err


def test_cli_le_o_relatorio_de_arquivo_em_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    arquivo = tmp_path / "relatorio.json"
    arquivo.write_text(_relatorio_em_json(LIVRO), encoding="utf-8")
    transacao = TransacaoFalsa(projecao=list(LIVRO.datas), faltantes=(0, []))
    transacao.saldos_diarios = transacao.totais_das_contribuicoes(7)

    codigo = _executar_cli(
        monkeypatch,
        transacao,
        ["reconciliar", "--relatorio-livro", str(arquivo), "--json"],
    )

    documento = json.loads(capsys.readouterr().out)
    assert codigo == 0
    assert documento["conciliada"] is True
    assert documento["divergencias"] == documento["saldo_inconsistente"] == []


def test_cli_recusa_relatorio_invalido_sem_consultar_o_banco(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transacao = TransacaoFalsa()

    codigo = _executar_cli(monkeypatch, transacao, ["reconciliar"], "não é json")

    assert codigo == 1
    assert "reconciliar recusado: relatório do livro inválido" in (
        capsys.readouterr().err
    )
    assert transacao.chamadas == []


def test_cli_recusa_arquivo_inexistente(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    codigo = _executar_cli(
        monkeypatch,
        TransacaoFalsa(),
        ["reconciliar", "--relatorio-livro", str(tmp_path / "ausente.json")],
    )

    assert codigo == 1
    assert "relatório do livro ilegível (FileNotFoundError)" in capsys.readouterr().err
