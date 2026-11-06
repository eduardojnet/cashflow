"""Regras do lançamento: tipo, valor, data e descrição (002 FR-002, FR-010, D-001, D-004, D-005)."""

import uuid
from datetime import date, datetime
from decimal import Decimal

import pytest
from lancamentos.dominio import (
    VALOR_MAXIMO,
    CampoObrigatorioAusente,
    DataInvalida,
    DescricaoInvalida,
    ErroDominio,
    Lancamento,
    TipoInvalido,
    TipoLancamento,
    Valor,
    ValorInvalido,
)


def test_lancamento_valido_guarda_os_campos_de_d001() -> None:
    lancamento = Lancamento.novo("CREDITO", "100.5", "2026-11-05", "venda balcão")

    assert lancamento.tipo is TipoLancamento.CREDITO
    assert lancamento.valor.quantia == Decimal("100.50")
    assert str(lancamento.valor) == "100.50"
    assert lancamento.valor.moeda == "BRL"
    assert lancamento.data_lancamento == date(2026, 11, 5)
    assert lancamento.descricao == "venda balcão"
    assert isinstance(lancamento.id, uuid.UUID)


def test_descricao_e_opcional() -> None:
    lancamento = Lancamento.novo("DEBITO", Decimal(10), date(2026, 11, 5))

    assert lancamento.descricao is None


def test_cada_lancamento_recebe_identificador_proprio() -> None:
    primeiro = Lancamento.novo("DEBITO", "1", "2026-11-05")
    segundo = Lancamento.novo("DEBITO", "1", "2026-11-05")

    assert primeiro.id != segundo.id


def test_lancamento_e_imutavel() -> None:
    lancamento = Lancamento.novo("DEBITO", "1", "2026-11-05")

    with pytest.raises(AttributeError):
        lancamento.valor = Valor.de("2")  # type: ignore[misc]


@pytest.mark.parametrize(
    ("tipo", "esperado"),
    [("CREDITO", Decimal("25.00")), ("DEBITO", Decimal("-25.00"))],
)
def test_tipo_define_o_sinal_do_valor(tipo: str, esperado: Decimal) -> None:
    assert Lancamento.novo(tipo, "25", "2026-11-05").efeito_no_saldo == esperado


@pytest.mark.parametrize(
    "tipo", ["ESTORNO", "debito", "Credito", "", " DEBITO", 1, True]
)
def test_tipo_fora_de_debito_ou_credito_gera_erro_de_dominio(tipo: object) -> None:
    with pytest.raises(TipoInvalido) as erro:
        Lancamento.novo(tipo, "10", "2026-11-05")

    assert erro.value.campo == "tipo"


@pytest.mark.parametrize(
    "valor",
    [
        "0",
        "0.00",
        "-1",
        "-0.01",
        "10.001",
        "0.001",
        "abc",
        "",
        "NaN",
        "Infinity",
        "1e400",
        "1,50",
    ],
)
def test_valor_nao_positivo_ou_com_mais_de_duas_casas_gera_erro_de_dominio(
    valor: str,
) -> None:
    with pytest.raises(ValorInvalido) as erro:
        Lancamento.novo("CREDITO", valor, "2026-11-05")

    assert erro.value.campo == "valor"


@pytest.mark.parametrize("valor", [10.5, True, [], {"valor": "1"}])
def test_valor_que_nao_e_decimal_exato_gera_erro_de_dominio(valor: object) -> None:
    with pytest.raises(ValorInvalido):
        Lancamento.novo("CREDITO", valor, "2026-11-05")


def test_valor_acima_do_limite_da_coluna_gera_erro_de_dominio() -> None:
    assert Valor.de(VALOR_MAXIMO).quantia == VALOR_MAXIMO

    with pytest.raises(ValorInvalido):
        Valor.de(VALOR_MAXIMO + Decimal("0.01"))


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [("0.01", "0.01"), (7, "7.00"), ("1.10", "1.10"), ("1.100", "1.10")],
)
def test_valor_com_ate_duas_casas_e_aceito_e_normalizado(
    valor: object, esperado: str
) -> None:
    assert str(Valor.de(valor)) == esperado


def test_mensagem_de_erro_nao_repete_o_valor_recebido() -> None:
    with pytest.raises(ValorInvalido) as erro:
        Valor.de("-987654.32")

    assert "987654" not in str(erro.value)


@pytest.mark.parametrize(
    "data",
    [
        "2026-02-30",
        "2026-13-01",
        "05/11/2026",
        "20261105",
        "2026-W45-4",
        "",
        "hoje",
        20261105,
    ],
)
def test_data_invalida_gera_erro_de_dominio(data: object) -> None:
    with pytest.raises(DataInvalida) as erro:
        Lancamento.novo("DEBITO", "1", data)

    assert erro.value.campo == "data_lancamento"


def test_instante_com_hora_e_recusado_para_nao_escolher_o_dia_implicitamente() -> None:
    with pytest.raises(DataInvalida):
        Lancamento.novo("DEBITO", "1", datetime(2026, 11, 5, 23, 30))


def test_dia_do_lancamento_e_a_data_informada_sem_conversao_de_fuso() -> None:
    assert Lancamento.novo("DEBITO", "1", "2026-12-31").data_lancamento == date(
        2026, 12, 31
    )


def test_lancamento_retroativo_e_permitido() -> None:
    assert Lancamento.novo("DEBITO", "1", "2020-01-01").data_lancamento == date(
        2020, 1, 1
    )


@pytest.mark.parametrize(
    ("campos", "campo_ausente"),
    [
        ({"tipo": None, "valor": "1", "data_lancamento": "2026-11-05"}, "tipo"),
        ({"tipo": "DEBITO", "valor": None, "data_lancamento": "2026-11-05"}, "valor"),
        ({"tipo": "DEBITO", "valor": "1", "data_lancamento": None}, "data_lancamento"),
    ],
)
def test_campo_obrigatorio_ausente_gera_erro_de_dominio(
    campos: dict[str, object], campo_ausente: str
) -> None:
    with pytest.raises(CampoObrigatorioAusente) as erro:
        Lancamento.novo(**campos)

    assert erro.value.campo == campo_ausente


def test_descricao_que_nao_e_texto_gera_erro_de_dominio() -> None:
    with pytest.raises(DescricaoInvalida):
        Lancamento.novo("DEBITO", "1", "2026-11-05", 123)


def test_construcao_direta_tambem_valida() -> None:
    with pytest.raises(ValorInvalido):
        Lancamento(TipoLancamento.DEBITO, Valor(Decimal(-1)), date(2026, 11, 5))


def test_todo_erro_de_validacao_e_erro_de_dominio_com_campo_e_motivo() -> None:
    for classe in (
        CampoObrigatorioAusente,
        TipoInvalido,
        ValorInvalido,
        DataInvalida,
        DescricaoInvalida,
    ):
        assert issubclass(classe, ErroDominio)

    with pytest.raises(ErroDominio) as erro:
        Lancamento.novo("CREDITO", "0", "2026-11-05")

    assert erro.value.motivo == "deve ser positivo"
