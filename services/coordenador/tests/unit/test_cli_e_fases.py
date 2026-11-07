"""CLI do coordenador e códigos de fase da métrica, sem banco (005-T3).

O mapa de códigos de `cashflow_reconstrucao_fase` existe no coordenador
(quem grava a fase) e na API do consolidado (quem a expõe); a imagem da API
não leva o coordenador, então este teste é o que mantém os dois iguais.
"""

import json
from datetime import UTC, datetime

import pytest
from consolidado.adaptadores.http import fase as fase_api
from coordenador.aplicacao import Situacao, SituacaoConsumidor, SituacaoGeracao
from coordenador.cli import (
    conninfo_do_ambiente,
    main,
    situacao_em_json,
    situacao_em_texto,
)
from coordenador.dominio import (
    CODIGO_FASE,
    CODIGO_SEM_RECONSTRUCAO,
    EstadoGeracao,
    FaseReconstrucao,
)
from psycopg.conninfo import conninfo_to_dict

E = EstadoGeracao
INSTANTE = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
SENHA = "senha-de-teste-nao-usada"


def test_codigos_de_fase_iguais_aos_da_api_do_consolidado() -> None:
    assert {str(f): c for f, c in CODIGO_FASE.items()} == fase_api.CODIGO_FASE
    assert CODIGO_SEM_RECONSTRUCAO == fase_api.CODIGO_SEM_RECONSTRUCAO


def test_toda_fase_tem_codigo_unico_e_positivo() -> None:
    assert set(CODIGO_FASE) == set(FaseReconstrucao)
    assert len(set(CODIGO_FASE.values())) == len(CODIGO_FASE)
    assert all(codigo > CODIGO_SEM_RECONSTRUCAO for codigo in CODIGO_FASE.values())


def test_conninfo_usa_consolidado_operacao_por_padrao() -> None:
    campos = conninfo_to_dict(
        conninfo_do_ambiente({"PG_CONSOLIDADO_SENHA_OPERACAO": SENHA})
    )

    assert campos["host"] == "pg-consolidado"
    assert campos["dbname"] == "consolidado"
    assert campos["user"] == "consolidado_operacao"
    assert campos["application_name"] == "coordenador"


def test_sem_senha_o_comando_termina_sem_conectar() -> None:
    with pytest.raises(SystemExit) as saida:
        main(["reconstruir", "status"], ambiente={})

    assert "PG_CONSOLIDADO_SENHA_OPERACAO" in str(saida.value.code)


@pytest.mark.parametrize(
    "argumentos",
    [
        [],
        ["reconstruir"],
        ["reconstruir", "trocar"],
        ["reconstruir", "status", "--lacunas", "-1"],
    ],
    ids=["vazio", "sem-acao", "acao-desconhecida", "limite-negativo"],
)
def test_uso_invalido_termina_com_codigo_2(argumentos: list[str]) -> None:
    with pytest.raises(SystemExit) as saida:
        main(argumentos, ambiente={"PG_CONSOLIDADO_SENHA_OPERACAO": SENHA})

    assert saida.value.code == 2


def _situacao() -> Situacao:
    ativa = SituacaoGeracao(
        id=1,
        estado=E.ATIVA,
        criada_em=INSTANTE,
        ativada_em=INSTANTE,
        checkpoint=40,
        checkpoint_atualizado_em=INSTANTE,
        maior_posicao=40,
        lacunas=0,
        primeiras_lacunas=(),
        fase=None,
        fase_atualizada_em=None,
    )
    candidata = SituacaoGeracao(
        id=2,
        estado=E.CONSTRUINDO,
        criada_em=INSTANTE,
        ativada_em=None,
        checkpoint=12,
        checkpoint_atualizado_em=INSTANTE,
        maior_posicao=20,
        lacunas=3,
        primeiras_lacunas=(13, 15, 18),
        fase="construindo",
        fase_atualizada_em=INSTANTE,
    )
    return Situacao(
        1,
        "liberada",
        4,
        (ativa, candidata),
        (SituacaoConsumidor("worker", False, INSTANTE),),
    )


def test_status_em_texto_mostra_checkpoint_e_lacunas_da_candidata() -> None:
    texto = situacao_em_texto(_situacao())

    assert "geração ativa: 1 (barreira liberada, versão 4)" in texto
    assert "geração 2: construindo" in texto
    assert "checkpoint: 12 (maior posição projetada: 20)" in texto
    assert "lacunas: 3 (menores posições: 13, 15, 18)" in texto
    assert "fase: construindo desde 2026-11-06T12:00:00+00:00" in texto
    assert "critério de troca (FR-009): não atendido" in texto
    assert "candidata com 3 lacunas" in texto
    assert "consumidor worker: consumindo" in texto


def test_status_em_json_mostra_checkpoint_lacunas_e_criterio() -> None:
    documento = json.loads(json.dumps(situacao_em_json(_situacao())))

    candidata = documento["geracoes"][1]
    assert candidata["estado"] == "construindo"
    assert candidata["checkpoint"] == 12
    assert candidata["lacunas"] == 3
    assert candidata["primeiras_lacunas"] == [13, 15, 18]
    assert candidata["fase"] == "construindo"
    assert documento["criterio_de_troca"]["atendido"] is False
    assert len(documento["criterio_de_troca"]["pendencias"]) == 2


def test_status_sem_candidata_informa_que_nao_ha_reconstrucao() -> None:
    situacao = _situacao()
    so_ativa = Situacao(1, "liberada", 4, situacao.geracoes[:1], ())

    assert "reconstrução: nenhuma em andamento" in situacao_em_texto(so_ativa)
    assert situacao_em_json(so_ativa)["criterio_de_troca"] is None


@pytest.mark.parametrize(
    ("fase", "codigo"),
    [
        (None, 0),
        ("construindo", 1),
        ("cancelada", 5),
        ("inexistente", fase_api.CODIGO_FASE_DESCONHECIDA),
    ],
)
def test_api_traduz_a_fase_no_codigo_da_metrica(fase: str | None, codigo: int) -> None:
    assert fase_api.codigo_da_fase(fase) == codigo
