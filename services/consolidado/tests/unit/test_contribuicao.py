"""Contribuição do lançamento, evento v1 e conflito por hash (004 FR-001, FR-003; TD-001)."""

import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from consolidado.dominio import (
    VALOR_MAXIMO,
    CampoInvalido,
    CampoObrigatorioAusente,
    Contribuicao,
    ErroDominio,
    EventoLancamentoRegistrado,
    Recebimento,
    TipoLancamento,
    VersaoNaoSuportada,
    classificar,
)

LANCAMENTO_ID = uuid.UUID("0b5f8f0e-3c1a-4d0e-9a51-6f3b2a1c0d01")
EVENTO_ID = uuid.UUID("7d2c1e4a-8b3f-4a6d-b9e2-1f0a3c5d7e02")


def _dados(**alteracoes: object) -> dict[str, object]:
    """Evento v1 como o livro publica (002 design §4)."""
    dados: dict[str, object] = {
        "evento_id": str(EVENTO_ID),
        "versao": 1,
        "lancamento_id": str(LANCAMENTO_ID),
        "posicao": 123,
        "tipo": "CREDITO",
        "valor": "100.00",
        "data_lancamento": "2026-11-05",
        "registrado_em": "2026-11-05T13:45:10.123456+00:00",
    }
    dados.update(alteracoes)
    return dados


def test_evento_v1_gera_a_contribuicao_do_lancamento() -> None:
    evento = EventoLancamentoRegistrado.de_dados(_dados())

    assert evento.evento_id == EVENTO_ID
    assert evento.registrado_em == datetime(2026, 11, 5, 13, 45, 10, 123456, UTC)
    assert evento.contribuicao == Contribuicao(
        LANCAMENTO_ID, 123, date(2026, 11, 5), TipoLancamento.CREDITO, Decimal("100")
    )


@pytest.mark.parametrize(
    ("tipo", "esperado"),
    [("CREDITO", Decimal("25.00")), ("DEBITO", Decimal("-25.00"))],
)
def test_tipo_define_o_sinal_da_contribuicao(tipo: str, esperado: Decimal) -> None:
    contribuicao = Contribuicao.de(LANCAMENTO_ID, 1, "2026-11-05", tipo, "25")

    assert contribuicao.efeito_no_saldo == esperado


def test_valor_e_normalizado_para_duas_casas() -> None:
    contribuicao = Contribuicao.de(LANCAMENTO_ID, 1, "2026-11-05", "DEBITO", "7.5")

    assert contribuicao.valor == Decimal("7.50")
    assert f"{contribuicao.valor:f}" == "7.50"


def test_contribuicao_e_imutavel() -> None:
    contribuicao = Contribuicao.de(LANCAMENTO_ID, 1, "2026-11-05", "DEBITO", "1")

    with pytest.raises(AttributeError):
        contribuicao.valor = Decimal(2)  # type: ignore[misc]


def test_campo_fora_do_contrato_e_ignorado() -> None:
    evento = EventoLancamentoRegistrado.de_dados(_dados(descricao="venda"))

    assert evento.hash == EventoLancamentoRegistrado.de_dados(_dados()).hash


@pytest.mark.parametrize(
    "campo",
    [
        "evento_id",
        "versao",
        "lancamento_id",
        "posicao",
        "tipo",
        "valor",
        "data_lancamento",
        "registrado_em",
    ],
)
def test_campo_obrigatorio_ausente_torna_o_evento_ilegivel(campo: str) -> None:
    dados = _dados()
    del dados[campo]

    with pytest.raises(CampoObrigatorioAusente) as erro:
        EventoLancamentoRegistrado.de_dados(dados)

    assert erro.value.campo == campo


@pytest.mark.parametrize(
    ("campo", "bruto"),
    [
        ("evento_id", "nao-e-uuid"),
        ("evento_id", 123),
        ("lancamento_id", ""),
        ("posicao", 0),
        ("posicao", -1),
        ("posicao", "123"),
        ("posicao", 1.0),
        ("posicao", True),
        ("tipo", "ESTORNO"),
        ("tipo", "credito"),
        ("valor", "0"),
        ("valor", "-10.00"),
        ("valor", "10.001"),
        ("valor", "abc"),
        ("valor", "NaN"),
        ("valor", "1e400"),
        ("valor", 10.5),
        ("valor", True),
        ("data_lancamento", "2026-02-30"),
        ("data_lancamento", "05/11/2026"),
        ("data_lancamento", "20261105"),
        ("data_lancamento", "2026-11-05T10:00:00"),
        ("registrado_em", "ontem"),
        ("registrado_em", "2026-11-05T13:45:10"),
        ("registrado_em", 1730814310),
    ],
)
def test_campo_invalido_torna_o_evento_ilegivel(campo: str, bruto: object) -> None:
    with pytest.raises(CampoInvalido) as erro:
        EventoLancamentoRegistrado.de_dados(_dados(**{campo: bruto}))

    assert erro.value.campo == campo


@pytest.mark.parametrize("versao", [2, 0, "1", 1.0, True])
def test_versao_diferente_de_1_nao_e_suportada(versao: object) -> None:
    with pytest.raises(VersaoNaoSuportada):
        EventoLancamentoRegistrado.de_dados(_dados(versao=versao))


def test_valor_acima_do_limite_da_coluna_e_recusado() -> None:
    assert Contribuicao.de(
        LANCAMENTO_ID, 1, "2026-11-05", "CREDITO", VALOR_MAXIMO
    ).valor == (VALOR_MAXIMO)

    with pytest.raises(CampoInvalido):
        Contribuicao.de(
            LANCAMENTO_ID, 1, "2026-11-05", "CREDITO", VALOR_MAXIMO + Decimal("0.01")
        )


def test_construcao_direta_tambem_valida() -> None:
    with pytest.raises(CampoInvalido):
        Contribuicao(
            LANCAMENTO_ID, 1, date(2026, 11, 5), TipoLancamento.DEBITO, Decimal(-1)
        )


def test_mensagem_de_erro_nao_repete_o_valor_recebido() -> None:
    with pytest.raises(CampoInvalido) as erro:
        EventoLancamentoRegistrado.de_dados(_dados(valor="-987654.32"))

    assert "987654" not in str(erro.value)


def test_todo_erro_de_validacao_e_erro_de_dominio() -> None:
    for classe in (CampoObrigatorioAusente, CampoInvalido, VersaoNaoSuportada):
        assert issubclass(classe, ErroDominio)


# Conflito por hash (TD-001, FR-003).


def test_mesmo_evento_reentregue_tem_o_mesmo_hash_e_e_duplicata() -> None:
    registrado = EventoLancamentoRegistrado.de_dados(_dados())
    reentregue = EventoLancamentoRegistrado.de_dados(_dados())

    assert classificar(registrado.hash, reentregue.hash) is Recebimento.DUPLICATA


def test_hash_usa_valores_normalizados() -> None:
    original = EventoLancamentoRegistrado.de_dados(_dados())
    # Mesmo valor e mesmo instante, escritos de outra forma.
    equivalente = EventoLancamentoRegistrado.de_dados(
        _dados(valor="100", registrado_em="2026-11-05T10:45:10.123456-03:00")
    )

    assert equivalente.hash == original.hash
    assert equivalente.contribuicao.hash == original.contribuicao.hash


@pytest.mark.parametrize(
    ("campo", "outro"),
    [
        ("lancamento_id", str(uuid.uuid4())),
        ("posicao", 124),
        ("tipo", "DEBITO"),
        ("valor", "100.01"),
        ("data_lancamento", "2026-11-06"),
        ("registrado_em", "2026-11-05T13:45:11+00:00"),
    ],
)
def test_mesmo_evento_com_conteudo_diferente_e_conflito(
    campo: str, outro: object
) -> None:
    registrado = EventoLancamentoRegistrado.de_dados(_dados())
    divergente = EventoLancamentoRegistrado.de_dados(_dados(**{campo: outro}))

    assert divergente.evento_id == registrado.evento_id
    assert classificar(registrado.hash, divergente.hash) is Recebimento.CONFLITO


def test_mesmo_lancamento_republicado_em_outro_evento_e_duplicata() -> None:
    # Republicação (003 TD-004) pode trazer o lançamento em outro evento: o
    # hash da contribuição ignora o evento e o instante de registro.
    original = EventoLancamentoRegistrado.de_dados(_dados())
    republicado = EventoLancamentoRegistrado.de_dados(
        _dados(evento_id=str(uuid.uuid4()), registrado_em="2026-11-06T09:00:00+00:00")
    )

    assert republicado.hash != original.hash
    assert (
        classificar(original.contribuicao.hash, republicado.contribuicao.hash)
        is Recebimento.DUPLICATA
    )


@pytest.mark.parametrize(
    ("campo", "outro"),
    [
        ("posicao", 124),
        ("tipo", "DEBITO"),
        ("valor", "99.99"),
        ("data_lancamento", "2026-11-04"),
    ],
)
def test_mesmo_lancamento_com_conteudo_diferente_e_conflito(
    campo: str, outro: object
) -> None:
    original = EventoLancamentoRegistrado.de_dados(_dados())
    divergente = EventoLancamentoRegistrado.de_dados(
        _dados(evento_id=str(uuid.uuid4()), **{campo: outro})
    )

    assert divergente.contribuicao.lancamento_id == original.contribuicao.lancamento_id
    assert (
        classificar(original.contribuicao.hash, divergente.contribuicao.hash)
        is Recebimento.CONFLITO
    )


def test_sem_registro_anterior_e_novo() -> None:
    evento = EventoLancamentoRegistrado.de_dados(_dados())

    assert classificar(None, evento.hash) is Recebimento.NOVO
    assert classificar(None, evento.contribuicao.hash) is Recebimento.NOVO


def test_hash_e_sha256_em_hexadecimal() -> None:
    evento = EventoLancamentoRegistrado.de_dados(_dados())

    for valor in (evento.hash, evento.contribuicao.hash):
        assert len(valor) == 64
        assert int(valor, 16) >= 0


def test_hash_recebido_vazio_e_recusado() -> None:
    with pytest.raises(CampoInvalido):
        classificar(None, "")


def test_instante_com_outro_fuso_e_normalizado_para_utc() -> None:
    evento = EventoLancamentoRegistrado.de_dados(
        _dados(registrado_em="2026-11-05T10:45:10-03:00")
    )

    assert evento.registrado_em.utcoffset() == timedelta(0)
    assert evento.registrado_em == datetime(
        2026, 11, 5, 10, 45, 10, tzinfo=timezone(timedelta(hours=-3))
    )
