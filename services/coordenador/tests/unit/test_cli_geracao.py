"""Comandos `geracao trocar|rollback|descartar` da CLI, sem banco (005-T4)."""

import pytest
from coordenador.cli import VARIAVEL_TEMPO_LIMITE, main, tempo_limite_do_ambiente

SENHA = "senha-de-teste-nao-usada"


def test_tempo_limite_padrao_e_do_ambiente() -> None:
    assert tempo_limite_do_ambiente({}) == 30
    assert tempo_limite_do_ambiente({VARIAVEL_TEMPO_LIMITE: "2.5"}) == 2.5


@pytest.mark.parametrize("texto", ["0", "-1", "nan", "dez"])
def test_tempo_limite_invalido_no_ambiente_encerra_com_o_nome_da_variavel(
    texto: str,
) -> None:
    with pytest.raises(SystemExit) as saida:
        tempo_limite_do_ambiente({VARIAVEL_TEMPO_LIMITE: texto})

    assert VARIAVEL_TEMPO_LIMITE in str(saida.value.code)


@pytest.mark.parametrize("acao", ["trocar", "rollback"])
@pytest.mark.parametrize("texto", ["0", "-3", "dez"])
def test_tempo_limite_invalido_na_linha_de_comando_e_uso_invalido(
    acao: str, texto: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as saida:
        main(
            ["geracao", acao, "--tempo-limite", texto],
            ambiente={"PG_CONSOLIDADO_SENHA_OPERACAO": SENHA},
        )

    assert saida.value.code == 2
    assert "--tempo-limite" in capsys.readouterr().err


def test_descartar_nao_aceita_tempo_limite(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as saida:
        main(
            ["geracao", "descartar", "--tempo-limite", "5"],
            ambiente={"PG_CONSOLIDADO_SENHA_OPERACAO": SENHA},
        )

    assert saida.value.code == 2


def test_geracao_exige_uma_acao(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as saida:
        main(["geracao"], ambiente={"PG_CONSOLIDADO_SENHA_OPERACAO": SENHA})

    assert saida.value.code == 2
    assert "trocar" in capsys.readouterr().err
