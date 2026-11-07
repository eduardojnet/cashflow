"""Comando de relatório do livro sem banco (005-T5; D-002).

Formato exportado, conexão pela identidade dedicada e uso inválido. A
leitura real do livro, as recusas de identidade e de posição e a ausência
de valores no log estão no teste de integração do coordenador
(services/coordenador/tests/integracao/test_reconciliacao_no_postgres.py),
que gera o relatório pelo comando real e o reconcilia.
"""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from lancamentos.comandos.relatorio import (
    FORMATO,
    RelatorioLivro,
    TotalDoDia,
    conninfo_do_ambiente,
    main,
    relatorio_em_json,
)
from psycopg.conninfo import conninfo_to_dict

SENHA = "senha-de-teste-nao-usada"


def test_relatorio_em_json_com_valores_em_texto_de_duas_casas() -> None:
    relatorio = RelatorioLivro(
        ate_posicao=3,
        gerado_em=datetime(2026, 11, 7, 9, 0, tzinfo=timezone(timedelta(hours=-3))),
        datas=(
            TotalDoDia(date(2026, 11, 3), 2, Decimal("10.5"), Decimal(0)),
            TotalDoDia(date(2026, 11, 4), 1, Decimal(0), Decimal("7.25")),
        ),
    )

    assert relatorio.lancamentos == 3
    assert relatorio_em_json(relatorio) == {
        "formato": FORMATO,
        "ate_posicao": 3,
        "gerado_em": datetime(2026, 11, 7, 12, 0, tzinfo=UTC).isoformat(),
        "datas": [
            {
                "data": "2026-11-03",
                "quantidade": 2,
                "total_creditos": "10.50",
                "total_debitos": "0.00",
            },
            {
                "data": "2026-11-04",
                "quantidade": 1,
                "total_creditos": "0.00",
                "total_debitos": "7.25",
            },
        ],
    }


def test_formato_igual_ao_lido_pelo_coordenador() -> None:
    from coordenador.adaptadores.relatorio_livro import FORMATO as LIDO

    assert FORMATO == LIDO


def test_conninfo_usa_livro_replay_por_padrao() -> None:
    campos = conninfo_to_dict(conninfo_do_ambiente({"PG_LIVRO_SENHA_REPLAY": SENHA}))

    assert campos["host"] == "pg-livro"
    assert campos["dbname"] == "livro"
    assert campos["user"] == "livro_replay"
    assert campos["application_name"] == "livro-relatorio"


def test_sem_senha_o_comando_termina_sem_conectar() -> None:
    with pytest.raises(SystemExit) as saida:
        main([], ambiente={})

    assert "PG_LIVRO_SENHA_REPLAY" in str(saida.value.code)


@pytest.mark.parametrize("posicao", ["0", "-1", "dez"])
def test_posicao_invalida_e_uso_invalido(posicao: str) -> None:
    with pytest.raises(SystemExit) as saida:
        main(["--ate-posicao", posicao], ambiente={"PG_LIVRO_SENHA_REPLAY": SENHA})

    assert saida.value.code == 2
