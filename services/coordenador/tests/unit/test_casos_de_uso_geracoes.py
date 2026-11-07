"""Troca, rollback e descarte sem banco (005-T4; design 005 §3, §4).

O banco é um dublê em memória com transações: cada `abrir` trabalha numa
cópia do estado, que só substitui o original na saída sem exceção (o COMMIT
do adaptador real). Assim, "nada muda" quer dizer o mesmo que no PostgreSQL.
O gatilho, a exclusão adiável e os privilégios da migração 0002 são
conferidos nos testes de integração.
"""

import copy
import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from coordenador.aplicacao import (
    BarreiraNaoConfirmada,
    ConfiguracaoBarreira,
    Controle,
    DescartarAnterior,
    OperacaoConcorrente,
    PedidoBarreira,
    ReverterTroca,
    Situacao,
    SituacaoConsumidor,
    TrocarGeracao,
    pendentes_da_barreira,
)
from coordenador.dominio import (
    EstadoGeracao,
    FaseReconstrucao,
    Geracao,
    Progresso,
    SemAnterior,
    SemReconstrucao,
    TransicaoInvalida,
    TrocaRecusada,
    trocar_ativa,
)

E = EstadoGeracao
F = FaseReconstrucao
INICIO = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


@dataclass
class Estado:
    geracoes: dict[int, EstadoGeracao]
    ativa_id: int
    barreira: str = "liberada"
    versao: int = 0
    fases: dict[int, str] = field(default_factory=dict)
    progresso: dict[int, Progresso] = field(default_factory=dict)
    consumidores: dict[str, tuple[bool, datetime]] = field(default_factory=dict)
    removidas: list[int] = field(default_factory=list)


@dataclass
class Banco:
    estado: Estado
    agora: datetime = INICIO
    commits: int = 0
    # Chamado a cada leitura de consumidores: simula o worker no intervalo.
    ao_ler_consumidores: Callable[["Banco"], None] | None = None
    # Falha injetada no comando com este nome (falha durante a troca).
    falhar_em: str | None = None

    def abrir(self) -> AbstractContextManager["TransacaoFalsa"]:
        @contextmanager
        def abrir() -> Iterator[TransacaoFalsa]:
            transacao = TransacaoFalsa(self, copy.deepcopy(self.estado))
            yield transacao
            self.estado = transacao.estado
            self.commits += 1

        return abrir()

    def pausar(self, consumidor_id: str = "worker") -> None:
        """O worker confirma a pausa, depois do pedido da barreira."""
        self.agora += timedelta(seconds=1)
        self.estado.consumidores[consumidor_id] = (True, self.agora)


class TransacaoFalsa:
    def __init__(self, banco: Banco, estado: Estado) -> None:
        self._banco = banco
        self.estado = estado

    def _talvez_falhar(self, comando: str) -> None:
        if self._banco.falhar_em == comando:
            raise RuntimeError(f"falha simulada em {comando}")

    def operador(self) -> str:
        return "consolidado_operacao"

    def geracoes_vigentes(self) -> list[Geracao]:
        return [
            Geracao(id_, estado)
            for id_, estado in sorted(self.estado.geracoes.items())
            if estado is not E.DESCARTADA
        ]

    def geracoes_para_atualizar(self) -> list[Geracao]:
        return self.geracoes_vigentes()

    def criar_candidata(self) -> int:
        raise AssertionError("não usado")

    def candidata_para_atualizar(self) -> Geracao | None:
        raise AssertionError("não usado")

    def mudar_estado(self, geracao_id: int, estado: EstadoGeracao) -> None:
        self._talvez_falhar("mudar_estado")
        # Mesma regra do gatilho do banco.
        Geracao(geracao_id, self.estado.geracoes[geracao_id]).transitar(estado)
        self.estado.geracoes[geracao_id] = estado

    def registrar_fase(self, geracao_id: int, fase: FaseReconstrucao) -> None:
        self.estado.fases[geracao_id] = str(fase)

    def remover_dados(self, geracao_id: int) -> None:
        assert self.estado.geracoes[geracao_id] is E.DESCARTADA
        self.estado.removidas.append(geracao_id)

    def situacao(self, limite_lacunas: int) -> Situacao:
        raise AssertionError("não usado")

    def controle_para_atualizar(self) -> Controle:
        e = self.estado
        return Controle(e.ativa_id, e.barreira, e.versao)

    def erguer_barreira(self) -> PedidoBarreira:
        herdada = self.estado.barreira == "solicitada"
        self.estado.barreira = "solicitada"
        self.estado.versao += 1
        return PedidoBarreira(self.estado.versao, self._banco.agora, herdada)

    def liberar_barreira(self) -> bool:
        if self.estado.barreira != "solicitada":
            return False
        self.estado.barreira = "liberada"
        self.estado.versao += 1
        return True

    def consumidores(self) -> list[SituacaoConsumidor]:
        if self._banco.ao_ler_consumidores is not None:
            # O "worker" age noutra transação, já confirmada; esta leitura
            # (só leitura, na espera da pausa) passa a ver o que ele fez.
            self._banco.ao_ler_consumidores(self._banco)
            self.estado = copy.deepcopy(self._banco.estado)
        return [
            SituacaoConsumidor(id_, pausado, visto_em)
            for id_, (pausado, visto_em) in sorted(self.estado.consumidores.items())
        ]

    def progresso(self, geracao_id: int) -> Progresso:
        return self.estado.progresso.get(geracao_id, Progresso(0, 0))

    def fase_de(self, geracao_id: int) -> str | None:
        return self.estado.fases.get(geracao_id)

    def adiar_exclusao(self) -> None:
        pass

    def apontar_ativa(self, geracao_id: int) -> None:
        self._talvez_falhar("apontar_ativa")
        self.estado.ativa_id = geracao_id
        self.estado.versao += 1


@dataclass
class Bloqueio:
    ocupado: bool = False
    usos: int = 0
    em_uso: bool = False

    def __call__(self) -> AbstractContextManager[None]:
        @contextmanager
        def bloquear() -> Iterator[None]:
            if self.ocupado:
                raise OperacaoConcorrente("outra operação de geração está em andamento")
            self.usos += 1
            self.em_uso = True
            try:
                yield
            finally:
                self.em_uso = False

        return bloquear()


class Relogio:
    """Relógio monotônico falso; `dormir` só avança o tempo."""

    def __init__(self) -> None:
        self.agora = 0.0
        self.esperas: list[float] = []

    def __call__(self) -> float:
        return self.agora

    def dormir(self, segundos: float) -> None:
        self.esperas.append(segundos)
        self.agora += segundos


def _com_candidata(
    checkpoint_candidata: int = 10, lacunas_candidata: int = 0
) -> Estado:
    return Estado(
        geracoes={1: E.ATIVA, 2: E.CONSTRUINDO},
        ativa_id=1,
        fases={2: str(F.CONSTRUINDO)},
        progresso={
            1: Progresso(10, 0),
            2: Progresso(checkpoint_candidata, lacunas_candidata),
        },
        consumidores={"worker": (False, INICIO - timedelta(seconds=5))},
    )


def _depois_da_troca() -> Estado:
    return Estado(
        geracoes={1: E.ANTERIOR, 2: E.ATIVA},
        ativa_id=2,
        versao=4,
        fases={2: str(F.CONCLUIDA)},
        consumidores={"worker": (False, INICIO - timedelta(seconds=5))},
    )


def _pausar_na_leitura(banco: Banco) -> None:
    if banco.estado.barreira == "solicitada":
        banco.pausar()


def _trocar(
    banco: Banco,
    bloqueio: Bloqueio | None = None,
    relogio: Relogio | None = None,
    tempo_limite: float = 5,
) -> TrocarGeracao:
    relogio = relogio or Relogio()
    return TrocarGeracao(
        banco.abrir,
        bloqueio or Bloqueio(),
        ConfiguracaoBarreira(tempo_limite_segundos=tempo_limite, intervalo_segundos=1),
        relogio=relogio,
        dormir=relogio.dormir,
    )


def _reverter(banco: Banco, bloqueio: Bloqueio | None = None) -> ReverterTroca:
    relogio = Relogio()
    return ReverterTroca(
        banco.abrir,
        bloqueio or Bloqueio(),
        ConfiguracaoBarreira(tempo_limite_segundos=5, intervalo_segundos=1),
        relogio=relogio,
        dormir=relogio.dormir,
    )


# --- domínio -----------------------------------------------------------------


def test_trocar_ativa_tira_a_ativa_antes_de_a_outra_entrar() -> None:
    saindo, entrando = trocar_ativa(Geracao(1, E.ATIVA), Geracao(2, E.CONSTRUINDO))

    assert (saindo, entrando) == (Geracao(1, E.ANTERIOR), Geracao(2, E.ATIVA))
    assert trocar_ativa(Geracao(2, E.ATIVA), Geracao(1, E.ANTERIOR)) == (
        Geracao(2, E.ANTERIOR),
        Geracao(1, E.ATIVA),
    )


@pytest.mark.parametrize(
    ("saindo", "entrando"),
    [
        (E.CONSTRUINDO, E.ANTERIOR),
        (E.ANTERIOR, E.CONSTRUINDO),
        (E.ATIVA, E.ATIVA),
        (E.ATIVA, E.DESCARTADA),
    ],
)
def test_trocar_ativa_recusa_fora_do_diagrama(
    saindo: EstadoGeracao, entrando: EstadoGeracao
) -> None:
    with pytest.raises(TransicaoInvalida):
        trocar_ativa(Geracao(1, saindo), Geracao(2, entrando))


def test_pendentes_da_barreira_exige_pausa_posterior_ao_pedido() -> None:
    consumidores = [
        SituacaoConsumidor("a", True, INICIO + timedelta(seconds=1)),
        SituacaoConsumidor("b", True, INICIO),
        SituacaoConsumidor("c", False, INICIO + timedelta(seconds=1)),
    ]

    assert pendentes_da_barreira(consumidores, INICIO) == ["b", "c"]
    assert pendentes_da_barreira([], INICIO) == []


def test_configuracao_recusa_tempo_limite_nao_positivo() -> None:
    with pytest.raises(ValueError, match="positivos"):
        ConfiguracaoBarreira(tempo_limite_segundos=0)


# --- troca ---------------------------------------------------------------------


def test_troca_ergue_a_barreira_espera_a_pausa_troca_e_libera(
    caplog: pytest.LogCaptureFixture,
) -> None:
    banco = Banco(_com_candidata(), ao_ler_consumidores=_pausar_na_leitura)
    bloqueio = Bloqueio()

    with caplog.at_level(logging.INFO, "coordenador.geracoes"):
        trocada = _trocar(banco, bloqueio)()

    assert (trocada.ativa_id, trocada.anterior_id) == (2, 1)
    assert trocada.operador == "consolidado_operacao"
    e = banco.estado
    assert e.geracoes == {1: E.ANTERIOR, 2: E.ATIVA}
    assert e.ativa_id == 2
    assert e.barreira == "liberada"
    # Erguer, apontar a ativa e liberar: três versões novas.
    assert e.versao == 3
    assert e.fases == {2: str(F.CONCLUIDA)}
    assert bloqueio.usos == 1
    assert not bloqueio.em_uso
    mensagens = [r.getMessage() for r in caplog.records]
    assert mensagens == [
        "barreira solicitada",
        "pausa confirmada pelos consumidores",
        "geração ativa trocada",
        "barreira liberada",
    ]
    trocou = caplog.records[2].__dict__
    assert (trocou["geracao_id"], trocou["geracao_anterior_id"]) == (2, 1)
    assert trocou["operador"] == "consolidado_operacao"


def test_troca_so_decide_depois_da_pausa_de_todos_os_consumidores() -> None:
    estado = _com_candidata()
    estado.consumidores["worker-2"] = (False, INICIO)
    leituras: list[tuple[str, int]] = []

    def worker_2_atrasa(banco: Banco) -> None:
        leituras.append((banco.estado.barreira, len(leituras)))
        if banco.estado.barreira == "solicitada":
            banco.pausar("worker")
            if len(leituras) >= 3:
                banco.pausar("worker-2")

    banco = Banco(estado, ao_ler_consumidores=worker_2_atrasa)
    relogio = Relogio()

    _trocar(banco, relogio=relogio)()

    assert len(leituras) == 3
    assert relogio.esperas == [1, 1]
    assert banco.estado.ativa_id == 2


def test_troca_recusada_com_lacuna_na_candidata_nao_muda_nada() -> None:
    banco = Banco(
        _com_candidata(lacunas_candidata=2), ao_ler_consumidores=_pausar_na_leitura
    )

    with pytest.raises(TrocaRecusada) as erro:
        _trocar(banco)()

    assert erro.value.pendencias == ["candidata com 2 lacunas"]
    assert "FR-009" in str(erro.value)
    e = banco.estado
    assert e.geracoes == {1: E.ATIVA, 2: E.CONSTRUINDO}
    assert e.ativa_id == 1
    assert e.barreira == "liberada"
    # A fase volta à de antes da barreira.
    assert e.fases == {2: str(F.CONSTRUINDO)}


def test_troca_recusada_com_checkpoint_da_candidata_abaixo_do_da_ativa() -> None:
    banco = Banco(
        _com_candidata(checkpoint_candidata=9), ao_ler_consumidores=_pausar_na_leitura
    )

    with pytest.raises(TrocaRecusada) as erro:
        _trocar(banco)()

    assert erro.value.pendencias == [
        "checkpoint da candidata (9) abaixo do da ativa (10)"
    ]
    assert banco.estado.ativa_id == 1


def test_criterio_vale_no_momento_da_barreira() -> None:
    # Na leitura de antes da barreira a candidata ainda tinha lacuna; a
    # mensagem que faltava chegou antes da pausa.
    banco = Banco(_com_candidata(lacunas_candidata=1))

    def fecha_a_lacuna_e_pausa(banco: Banco) -> None:
        if banco.estado.barreira == "solicitada":
            banco.estado.progresso[2] = Progresso(10, 0)
            banco.pausar()

    banco.ao_ler_consumidores = fecha_a_lacuna_e_pausa

    assert _trocar(banco)().ativa_id == 2


@pytest.mark.parametrize("comando", ["mudar_estado", "apontar_ativa"])
def test_falha_durante_a_troca_nao_altera_a_ativa(comando: str) -> None:
    banco = Banco(_com_candidata(), ao_ler_consumidores=_pausar_na_leitura)
    banco.falhar_em = comando

    with pytest.raises(RuntimeError, match="falha simulada"):
        _trocar(banco)()

    e = banco.estado
    assert e.geracoes == {1: E.ATIVA, 2: E.CONSTRUINDO}
    assert e.ativa_id == 1
    assert e.barreira == "liberada"
    assert e.fases == {2: str(F.CONSTRUINDO)}


def test_sem_confirmacao_no_tempo_limite_a_troca_e_cancelada() -> None:
    estado = _com_candidata()
    estado.consumidores["worker-parado"] = (True, INICIO - timedelta(hours=1))
    banco = Banco(estado, ao_ler_consumidores=_pausar_na_leitura)
    relogio = Relogio()

    with pytest.raises(BarreiraNaoConfirmada) as erro:
        _trocar(banco, relogio=relogio, tempo_limite=3)()

    # Pausa antiga não vale: tem de ser posterior ao pedido.
    assert erro.value.pendentes == ["worker-parado"]
    assert "3 s" in str(erro.value)
    assert relogio.agora == 3
    e = banco.estado
    assert (e.ativa_id, e.barreira) == (1, "liberada")
    assert e.geracoes == {1: E.ATIVA, 2: E.CONSTRUINDO}
    assert e.fases == {2: str(F.CONSTRUINDO)}


def test_sem_consumidor_registrado_nao_ha_o_que_esperar() -> None:
    estado = _com_candidata()
    estado.consumidores.clear()
    banco = Banco(estado)

    assert _trocar(banco)().ativa_id == 2


def test_barreira_mexida_por_fora_durante_a_espera_cancela_a_troca() -> None:
    banco = Banco(_com_candidata())

    def libera_por_fora(banco: Banco) -> None:
        if banco.estado.barreira == "solicitada":
            banco.estado.barreira = "liberada"
            banco.estado.versao += 1
            banco.pausar()

    banco.ao_ler_consumidores = libera_por_fora

    with pytest.raises(OperacaoConcorrente, match="barreira mudou"):
        _trocar(banco)()

    assert banco.estado.ativa_id == 1
    assert banco.estado.geracoes == {1: E.ATIVA, 2: E.CONSTRUINDO}


def test_candidata_cancelada_durante_a_espera_cancela_a_troca() -> None:
    banco = Banco(_com_candidata())

    def cancela(banco: Banco) -> None:
        if banco.estado.barreira == "solicitada":
            banco.estado.geracoes[2] = E.DESCARTADA
            banco.pausar()

    banco.ao_ler_consumidores = cancela

    with pytest.raises(OperacaoConcorrente, match="gerações mudaram"):
        _trocar(banco)()

    assert banco.estado.ativa_id == 1
    assert banco.estado.barreira == "liberada"


def test_troca_sem_candidata_e_recusada_antes_da_barreira() -> None:
    banco = Banco(Estado(geracoes={1: E.ATIVA}, ativa_id=1))

    with pytest.raises(SemReconstrucao):
        _trocar(banco)()

    assert (banco.estado.barreira, banco.estado.versao) == ("liberada", 0)


def test_barreira_que_sobrou_de_operacao_interrompida_e_liberada() -> None:
    estado = Estado(geracoes={1: E.ATIVA}, ativa_id=1, barreira="solicitada", versao=7)
    banco = Banco(estado)

    with pytest.raises(SemReconstrucao):
        _trocar(banco)()

    assert (banco.estado.barreira, banco.estado.versao) == ("liberada", 8)


def test_barreira_herdada_e_assumida_com_aviso(
    caplog: pytest.LogCaptureFixture,
) -> None:
    estado = _com_candidata()
    estado.barreira = "solicitada"
    banco = Banco(estado, ao_ler_consumidores=_pausar_na_leitura)

    with caplog.at_level(logging.INFO, "coordenador.geracoes"):
        assert _trocar(banco)().ativa_id == 2

    assert banco.estado.barreira == "liberada"
    assert any("operação interrompida" in r.getMessage() for r in caplog.records)


def test_com_o_bloqueio_ocupado_nada_e_feito() -> None:
    banco = Banco(_com_candidata(), ao_ler_consumidores=_pausar_na_leitura)

    with pytest.raises(OperacaoConcorrente, match="em andamento"):
        _trocar(banco, Bloqueio(ocupado=True))()

    assert banco.commits == 0


# --- rollback ------------------------------------------------------------------


def test_rollback_restaura_a_anterior_como_ativa() -> None:
    banco = Banco(_depois_da_troca(), ao_ler_consumidores=_pausar_na_leitura)

    revertida = _reverter(banco)()

    assert (revertida.ativa_id, revertida.anterior_id) == (1, 2)
    e = banco.estado
    assert e.geracoes == {1: E.ATIVA, 2: E.ANTERIOR}
    assert (e.ativa_id, e.barreira) == (1, "liberada")
    # A fase é a da reconstruída (a mais nova do par).
    assert e.fases == {2: str(F.REVERTIDA)}


def test_rollback_do_rollback_volta_a_reconstruida() -> None:
    banco = Banco(_depois_da_troca(), ao_ler_consumidores=_pausar_na_leitura)
    _reverter(banco)()

    _reverter(banco)()

    assert banco.estado.geracoes == {1: E.ANTERIOR, 2: E.ATIVA}
    assert banco.estado.fases == {2: str(F.CONCLUIDA)}


def test_rollback_nao_aplica_o_criterio_da_troca() -> None:
    estado = _depois_da_troca()
    estado.progresso = {1: Progresso(3, 2), 2: Progresso(10, 0)}
    banco = Banco(estado, ao_ler_consumidores=_pausar_na_leitura)

    assert _reverter(banco)().ativa_id == 1


def test_rollback_sem_anterior_e_recusado() -> None:
    banco = Banco(_com_candidata())

    with pytest.raises(SemAnterior):
        _reverter(banco)()

    assert banco.estado.geracoes == {1: E.ATIVA, 2: E.CONSTRUINDO}


def test_falha_durante_o_rollback_nao_altera_a_ativa() -> None:
    banco = Banco(_depois_da_troca(), ao_ler_consumidores=_pausar_na_leitura)
    banco.falhar_em = "apontar_ativa"

    with pytest.raises(RuntimeError):
        _reverter(banco)()

    e = banco.estado
    assert (e.geracoes, e.ativa_id, e.barreira) == (
        {1: E.ANTERIOR, 2: E.ATIVA},
        2,
        "liberada",
    )
    assert e.fases == {2: str(F.CONCLUIDA)}


# --- descarte ------------------------------------------------------------------


def test_descartar_remove_a_anterior_e_libera_nova_reconstrucao(
    caplog: pytest.LogCaptureFixture,
) -> None:
    banco = Banco(_depois_da_troca())
    bloqueio = Bloqueio()

    with caplog.at_level(logging.INFO, "coordenador.geracoes"):
        descartada = DescartarAnterior(banco.abrir, bloqueio)()

    assert descartada.geracao_id == 1
    e = banco.estado
    assert e.geracoes == {1: E.DESCARTADA, 2: E.ATIVA}
    assert e.removidas == [1]
    assert e.fases[1] == str(F.DESCARTADA)
    assert (e.barreira, e.versao) == ("liberada", 4)
    assert bloqueio.usos == 1
    (registro,) = caplog.records
    assert registro.__dict__["geracao_id"] == 1
    assert registro.__dict__["operador"] == "consolidado_operacao"


def test_descartar_sem_anterior_e_recusado() -> None:
    banco = Banco(_com_candidata())

    with pytest.raises(SemAnterior):
        DescartarAnterior(banco.abrir, Bloqueio())()

    assert banco.commits == 0


def test_descartar_com_o_bloqueio_ocupado_nada_faz() -> None:
    banco = Banco(_depois_da_troca())

    with pytest.raises(OperacaoConcorrente):
        DescartarAnterior(banco.abrir, Bloqueio(ocupado=True))()

    assert banco.estado.geracoes == {1: E.ANTERIOR, 2: E.ATIVA}
