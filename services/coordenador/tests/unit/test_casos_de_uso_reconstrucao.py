"""Casos de uso da reconstrução sem banco (005-T3; design 005 §3, §4).

A transação é um dublê em memória que registra os comandos na ordem; as
regras do banco (gatilho e exclusão da migração 0002) são conferidas nos
testes de integração.
"""

import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from coordenador.aplicacao import (
    CancelarReconstrucao,
    ConsultarSituacao,
    IniciarReconstrucao,
    Situacao,
    SituacaoGeracao,
)
from coordenador.dominio import (
    EstadoGeracao,
    FaseReconstrucao,
    Geracao,
    ReconstrucaoRecusada,
    SemReconstrucao,
)

E = EstadoGeracao
INSTANTE = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


@dataclass
class TransacaoFalsa:
    geracoes: dict[int, EstadoGeracao] = field(default_factory=lambda: {1: E.ATIVA})
    comandos: list[tuple[object, ...]] = field(default_factory=list)
    confirmada: bool = False

    def operador(self) -> str:
        return "consolidado_operacao"

    def geracoes_vigentes(self) -> list[Geracao]:
        return [
            Geracao(id_, estado)
            for id_, estado in sorted(self.geracoes.items())
            if estado is not E.DESCARTADA
        ]

    def criar_candidata(self) -> int:
        geracao_id = max(self.geracoes) + 1
        self.geracoes[geracao_id] = E.CONSTRUINDO
        self.comandos.append(("criar_candidata", geracao_id))
        return geracao_id

    def candidata_para_atualizar(self) -> Geracao | None:
        return next(
            (g for g in self.geracoes_vigentes() if g.estado is E.CONSTRUINDO), None
        )

    def mudar_estado(self, geracao_id: int, estado: EstadoGeracao) -> None:
        self.geracoes[geracao_id] = estado
        self.comandos.append(("mudar_estado", geracao_id, estado))

    def registrar_fase(self, geracao_id: int, fase: FaseReconstrucao) -> None:
        self.comandos.append(("registrar_fase", geracao_id, fase))

    def remover_dados(self, geracao_id: int) -> None:
        assert self.geracoes[geracao_id] is E.DESCARTADA
        self.comandos.append(("remover_dados", geracao_id))

    def situacao(self, limite_lacunas: int) -> Situacao:
        self.comandos.append(("situacao", limite_lacunas))
        return Situacao(1, "liberada", 0, (), ())


def _abridor(
    transacao: TransacaoFalsa,
) -> Callable[[], AbstractContextManager[TransacaoFalsa]]:
    @contextmanager
    def abrir() -> Iterator[TransacaoFalsa]:
        yield transacao
        # Só chega aqui sem exceção: é o COMMIT do adaptador real.
        transacao.confirmada = True

    return abrir


def test_iniciar_cria_a_candidata_com_fase_construindo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transacao = TransacaoFalsa()

    with caplog.at_level(logging.INFO, "coordenador.reconstrucao"):
        iniciada = IniciarReconstrucao(_abridor(transacao))()

    assert iniciada.geracao_id == 2
    assert iniciada.operador == "consolidado_operacao"
    assert transacao.comandos == [
        ("criar_candidata", 2),
        ("registrar_fase", 2, FaseReconstrucao.CONSTRUINDO),
    ]
    assert transacao.confirmada
    (registro,) = caplog.records
    assert registro.__dict__["geracao_id"] == 2
    assert registro.__dict__["estado"] == "construindo"
    assert registro.__dict__["operador"] == "consolidado_operacao"


@pytest.mark.parametrize("existente", [E.CONSTRUINDO, E.ANTERIOR])
def test_iniciar_recusa_com_construindo_ou_anterior(existente: EstadoGeracao) -> None:
    transacao = TransacaoFalsa(geracoes={1: E.ATIVA, 2: existente})

    with pytest.raises(ReconstrucaoRecusada) as erro:
        IniciarReconstrucao(_abridor(transacao))()

    assert erro.value.impedimentos == [str(existente)]
    assert transacao.comandos == []
    assert not transacao.confirmada


def test_iniciar_ignora_geracoes_descartadas() -> None:
    transacao = TransacaoFalsa(geracoes={1: E.ATIVA, 2: E.DESCARTADA})

    iniciada = IniciarReconstrucao(_abridor(transacao))()

    assert iniciada.geracao_id == 3


def test_cancelar_descarta_registra_a_fase_e_libera_os_dados(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transacao = TransacaoFalsa(geracoes={1: E.ATIVA, 2: E.CONSTRUINDO})

    with caplog.at_level(logging.INFO, "coordenador.reconstrucao"):
        cancelada = CancelarReconstrucao(_abridor(transacao))()

    assert cancelada.geracao_id == 2
    assert transacao.comandos == [
        ("mudar_estado", 2, E.DESCARTADA),
        ("registrar_fase", 2, FaseReconstrucao.CANCELADA),
        ("remover_dados", 2),
    ]
    assert transacao.confirmada
    (registro,) = caplog.records
    assert registro.__dict__["estado"] == "descartada"
    assert registro.__dict__["fase"] == "cancelada"


@pytest.mark.parametrize(
    "geracoes",
    [{1: E.ATIVA}, {1: E.ATIVA, 2: E.ANTERIOR}],
    ids=["so-ativa", "com-anterior"],
)
def test_cancelar_sem_candidata_e_recusado(geracoes: dict[int, EstadoGeracao]) -> None:
    transacao = TransacaoFalsa(geracoes=dict(geracoes))

    with pytest.raises(SemReconstrucao):
        CancelarReconstrucao(_abridor(transacao))()

    assert transacao.comandos == []
    assert transacao.geracoes == geracoes


def test_status_le_sem_alterar_e_repassa_o_limite_de_lacunas() -> None:
    transacao = TransacaoFalsa()

    situacao = ConsultarSituacao(_abridor(transacao))(limite_lacunas=5)

    assert situacao.geracao_ativa_id == 1
    assert transacao.comandos == [("situacao", 5)]


def test_status_recusa_limite_negativo() -> None:
    with pytest.raises(ValueError):
        ConsultarSituacao(_abridor(TransacaoFalsa()))(limite_lacunas=-1)


def _geracao(
    id_: int, estado: EstadoGeracao, checkpoint: int, lacunas: int = 0
) -> SituacaoGeracao:
    return SituacaoGeracao(
        id=id_,
        estado=estado,
        criada_em=INSTANTE,
        ativada_em=None,
        checkpoint=checkpoint,
        checkpoint_atualizado_em=INSTANTE,
        maior_posicao=checkpoint + lacunas,
        lacunas=lacunas,
        primeiras_lacunas=tuple(range(checkpoint + 1, checkpoint + 1 + lacunas)),
        fase=None,
        fase_atualizada_em=None,
    )


@pytest.mark.parametrize(
    ("candidata", "ativa", "pendencias"),
    [
        ((10, 0), 10, []),
        ((12, 0), 10, []),
        ((7, 0), 10, ["checkpoint da candidata (7) abaixo do da ativa (10)"]),
        ((10, 2), 10, ["candidata com 2 lacunas"]),
        (
            (4, 1),
            10,
            [
                "candidata com 1 lacunas",
                "checkpoint da candidata (4) abaixo do da ativa (10)",
            ],
        ),
    ],
    ids=["igual", "acima", "abaixo", "com-lacuna", "lacuna-e-abaixo"],
)
def test_criterio_de_troca_da_fr_009(
    candidata: tuple[int, int], ativa: int, pendencias: list[str]
) -> None:
    situacao = Situacao(
        1,
        "liberada",
        0,
        (_geracao(1, E.ATIVA, ativa), _geracao(2, E.CONSTRUINDO, *candidata)),
        (),
    )

    assert situacao.pendencias_de_troca() == pendencias


def test_criterio_de_troca_sem_candidata_e_none() -> None:
    situacao = Situacao(1, "liberada", 0, (_geracao(1, E.ATIVA, 3),), ())

    assert situacao.pendencias_de_troca() is None
    assert situacao.candidata is None
    assert situacao.ativa is not None
