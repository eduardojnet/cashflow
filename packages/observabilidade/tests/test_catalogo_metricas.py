"""Catálogo fechado de métricas e rótulos (007 TD-002, TD-003, FR-004)."""

import pytest
from observabilidade import (
    CATALOGO,
    ROTULOS_PERMITIDOS,
    DefinicaoMetrica,
    ErroCatalogo,
    MetricaDesconhecida,
    Metricas,
    RotuloNaoPermitido,
    TipoMetrica,
)

# Mapa de métricas de 007 design §4 que saem dos componentes Python.
NOMES_DO_MAPA = {
    "cashflow_outbox_pendentes",
    "cashflow_outbox_idade_mais_antigo_segundos",
    "cashflow_worker_reentregas_total",
    "cashflow_quarentena_total",
    "cashflow_pool_conexoes",
    "cashflow_geracao_ativa",
    "cashflow_reconstrucao_fase",
    "cashflow_checkpoint_posicao",
    "cashflow_lacunas",
    "cashflow_jwt_falhas_total",
    "cashflow_jwks_falhas_total",
}


def test_catalogo_contem_exatamente_as_metricas_do_mapa() -> None:
    assert set(CATALOGO) == NOMES_DO_MAPA
    assert set(ROTULOS_PERMITIDOS) == NOMES_DO_MAPA


def test_rotulos_do_catalogo_seguem_o_design() -> None:
    assert CATALOGO["cashflow_pool_conexoes"].rotulos == ("componente", "estado")
    assert CATALOGO["cashflow_checkpoint_posicao"].rotulos == ("geracao",)
    assert CATALOGO["cashflow_lacunas"].rotulos == ("geracao",)
    assert CATALOGO["cashflow_jwt_falhas_total"].rotulos == ("motivo",)


@pytest.mark.parametrize(
    "rotulo", ["cliente_id", "lancamento_id", "valor", "token", "status"]
)
def test_rotulo_fora_da_lista_permitida_gera_erro(rotulo: str) -> None:
    definicao = DefinicaoMetrica(
        "cashflow_jwt_falhas_total",
        TipoMetrica.CONTADOR,
        "Falhas de validação de JWT por motivo",
        ("motivo", rotulo),
    )
    with pytest.raises(RotuloNaoPermitido):
        Metricas().registrar(definicao)


def test_rotulo_permitido_em_outra_metrica_gera_erro() -> None:
    definicao = DefinicaoMetrica(
        "cashflow_outbox_pendentes",
        TipoMetrica.MEDIDOR,
        "Eventos pendentes de publicação na outbox",
        ("geracao",),
    )
    with pytest.raises(RotuloNaoPermitido):
        Metricas().registrar(definicao)


def test_rotulo_repetido_gera_erro() -> None:
    definicao = DefinicaoMetrica(
        "cashflow_lacunas",
        TipoMetrica.MEDIDOR,
        "Lacunas de posição abertas por geração",
        ("geracao", "geracao"),
    )
    with pytest.raises(RotuloNaoPermitido):
        Metricas().registrar(definicao)


def test_metrica_fora_do_catalogo_gera_erro() -> None:
    with pytest.raises(MetricaDesconhecida):
        Metricas().contador("cashflow_saldo_total")
    with pytest.raises(MetricaDesconhecida):
        Metricas().registrar(
            DefinicaoMetrica("cashflow_saldo", TipoMetrica.MEDIDOR, "Saldo")
        )


def test_definicao_divergente_do_catalogo_gera_erro() -> None:
    definicao = DefinicaoMetrica(
        "cashflow_lacunas", TipoMetrica.CONTADOR, "Outra descrição", ("geracao",)
    )
    with pytest.raises(ErroCatalogo):
        Metricas().registrar(definicao)


def test_tipo_errado_gera_erro() -> None:
    with pytest.raises(ErroCatalogo):
        Metricas().medidor("cashflow_quarentena_total")
    with pytest.raises(ErroCatalogo):
        Metricas().contador("cashflow_outbox_pendentes")


def test_metrica_nao_registrada_nao_aparece_na_exposicao() -> None:
    metricas = Metricas()
    metricas.contador("cashflow_quarentena_total")

    texto = metricas.exposicao().decode()

    assert "cashflow_quarentena_total" in texto
    assert "cashflow_outbox_pendentes" not in texto


def test_registrar_duas_vezes_devolve_a_mesma_metrica() -> None:
    metricas = Metricas()
    assert metricas.medidor("cashflow_geracao_ativa") is metricas.medidor(
        "cashflow_geracao_ativa"
    )


def test_uso_de_rotulo_nao_declarado_gera_erro() -> None:
    falhas = Metricas().contador("cashflow_jwt_falhas_total")
    with pytest.raises(ValueError):
        falhas.labels(motivo="expirado", cliente_id="c-1")
