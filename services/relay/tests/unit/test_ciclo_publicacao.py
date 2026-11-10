"""Ciclo de publicação sem infraestrutura (003-T2, 003-T3; FR-002, FR-003).

Portas falsas registram a ordem das chamadas: marcar vem sempre depois do
confirm do mesmo evento, só o confirm positivo sem return marca, e todo outro
desfecho reagenda o evento pela política de retentativa, sem removê-lo.

Um relógio falso mede o lease: nenhum evento é publicado sem que o restante
comporte uma publicação inteira (FR-004; review 003, Issue 1).
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from relay.aplicacao import (
    EventoReservado,
    PoliticaRetentativa,
    PrazoReserva,
    PublicarPendentes,
    ResultadoPublicacao,
)

POLITICA = PoliticaRetentativa(base=timedelta(seconds=1), teto=timedelta(minutes=1))
# Os padrões do compose: lease de 30 s e confirm em até 5 s, margem de 2 s.
PRAZO = PrazoReserva(lease=timedelta(seconds=30), publicacao=timedelta(seconds=5))


def _evento(posicao: int, tentativas: int = 0) -> EventoReservado:
    evento_id = uuid.uuid4()
    return EventoReservado(
        evento_id=evento_id,
        posicao=posicao,
        tipo_evento="LancamentoRegistrado",
        payload={"evento_id": str(evento_id), "posicao": posicao, "valor": "25.40"},
        tentativas=tentativas,
        lease_ate=datetime(2026, 11, 6, tzinfo=UTC),
    )


class _Portas:
    def __init__(
        self,
        eventos: list[EventoReservado],
        resultados: dict[uuid.UUID, ResultadoPublicacao | Exception],
        duracao_publicacao: float = 0.0,
    ) -> None:
        self._eventos = eventos
        self._resultados = resultados
        self._duracao_publicacao = duracao_publicacao
        self.agora = 0.0
        self.chamadas: list[tuple[str, uuid.UUID]] = []
        self.esperas: dict[uuid.UUID, timedelta] = {}

    def relogio(self) -> float:
        return self.agora

    async def reservar(self) -> list[EventoReservado]:
        return self._eventos

    async def publicar(self, evento: EventoReservado) -> ResultadoPublicacao:
        self.chamadas.append(("publicar", evento.evento_id))
        self.agora += self._duracao_publicacao
        resultado = self._resultados[evento.evento_id]
        if isinstance(resultado, Exception):
            raise resultado
        return resultado

    async def marcar_publicado(self, evento_id: uuid.UUID) -> None:
        self.chamadas.append(("marcar", evento_id))

    async def reagendar(self, evento: EventoReservado, espera: timedelta) -> None:
        self.chamadas.append(("reagendar", evento.evento_id))
        self.esperas[evento.evento_id] = espera

    def caso(self) -> PublicarPendentes:
        return PublicarPendentes(self, self, self, POLITICA, PRAZO, self.relogio)

    def _com(self, nome: str) -> list[uuid.UUID]:
        return [evento for chamada, evento in self.chamadas if chamada == nome]

    @property
    def marcados(self) -> list[uuid.UUID]:
        return self._com("marcar")

    @property
    def reagendados(self) -> list[uuid.UUID]:
        return self._com("reagendar")


def test_confirmado_e_marcado_logo_apos_o_proprio_confirm() -> None:
    eventos = [_evento(1), _evento(2)]
    portas = _Portas(
        eventos, {e.evento_id: ResultadoPublicacao.CONFIRMADO for e in eventos}
    )

    resumo = asyncio.run(portas.caso().executar())

    a, b = (e.evento_id for e in eventos)
    assert portas.chamadas == [
        ("publicar", a),
        ("marcar", a),
        ("publicar", b),
        ("marcar", b),
    ]
    assert resumo.reservados == 2
    assert resumo.resultados[ResultadoPublicacao.CONFIRMADO] == 2


@pytest.mark.parametrize(
    "resultado",
    [
        ResultadoPublicacao.DEVOLVIDO,
        ResultadoPublicacao.RECUSADO,
        ResultadoPublicacao.SEM_CONFIRMACAO,
    ],
    ids=lambda resultado: resultado.value,
)
def test_sem_confirm_positivo_sem_return_o_evento_e_reagendado_e_nao_marcado(
    resultado: ResultadoPublicacao,
) -> None:
    falho, confirmado = _evento(1), _evento(2)
    portas = _Portas(
        [falho, confirmado],
        {
            falho.evento_id: resultado,
            confirmado.evento_id: ResultadoPublicacao.CONFIRMADO,
        },
    )

    resumo = asyncio.run(portas.caso().executar())

    assert portas.marcados == [confirmado.evento_id]
    assert portas.reagendados == [falho.evento_id]
    assert resumo.resultados[resultado] == 1
    assert resumo.resultados[ResultadoPublicacao.CONFIRMADO] == 1


def test_falha_na_publicacao_reagenda_o_evento_e_o_ciclo_continua() -> None:
    confirmado, quebrado, seguinte = _evento(1), _evento(2), _evento(3)
    portas = _Portas(
        [confirmado, quebrado, seguinte],
        {
            confirmado.evento_id: ResultadoPublicacao.CONFIRMADO,
            quebrado.evento_id: ConnectionError("broker fora"),
            seguinte.evento_id: ResultadoPublicacao.CONFIRMADO,
        },
    )

    resumo = asyncio.run(portas.caso().executar())

    assert portas.marcados == [confirmado.evento_id, seguinte.evento_id]
    assert portas.reagendados == [quebrado.evento_id]
    assert resumo.resultados[ResultadoPublicacao.FALHA] == 1
    assert resumo.resultados[ResultadoPublicacao.CONFIRMADO] == 2


def test_espera_do_reagendamento_cresce_com_as_tentativas_do_evento() -> None:
    eventos = [_evento(1, tentativas=0), _evento(2, tentativas=3), _evento(3, 50)]
    portas = _Portas(
        eventos, {e.evento_id: ResultadoPublicacao.DEVOLVIDO for e in eventos}
    )

    asyncio.run(portas.caso().executar())

    assert [portas.esperas[e.evento_id] for e in eventos] == [
        timedelta(seconds=1),
        timedelta(seconds=8),
        timedelta(minutes=1),
    ]


def test_falha_no_registro_propaga_e_o_evento_segue_pendente() -> None:
    evento = _evento(1)

    class _RegistroFora(_Portas):
        async def reagendar(self, evento: EventoReservado, espera: timedelta) -> None:
            raise ConnectionError("banco fora")

    portas = _RegistroFora([evento], {evento.evento_id: ResultadoPublicacao.RECUSADO})

    # Sem reagendar, o lease vence e outra reserva pega o evento (NFR-003).
    with pytest.raises(ConnectionError):
        asyncio.run(portas.caso().executar())
    assert portas.marcados == []


def test_falha_vai_a_log_com_identidade_do_evento_sem_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    evento = _evento(7, tentativas=2)
    portas = _Portas([evento], {evento.evento_id: ConnectionError("senha=x")})

    with caplog.at_level(logging.WARNING, logger="relay.publicacao"):
        asyncio.run(portas.caso().executar())

    falha, reagendado = caplog.records
    assert falha.__dict__["erro_tipo"] == "ConnectionError"
    assert reagendado.__dict__["evento_id"] == str(evento.evento_id)
    assert reagendado.__dict__["posicao"] == 7
    assert reagendado.__dict__["resultado"] == "falha"
    assert reagendado.__dict__["tentativas"] == 3
    assert reagendado.__dict__["espera_s"] == 4.0
    for registro in caplog.records:
        assert "payload" not in registro.__dict__
        assert "25.40" not in registro.getMessage()
        assert "senha" not in str(registro.__dict__)


def test_lote_vazio_nao_publica_nem_marca() -> None:
    portas = _Portas([], {})

    resumo = asyncio.run(portas.caso().executar())

    assert portas.chamadas == []
    assert resumo.reservados == 0
    assert set(resumo.resultados.values()) == {0}


def test_evento_sem_lease_para_uma_publicacao_inteira_fica_sem_tentativa(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Cada publicação leva o prazo inteiro do confirm (5 s): com 30 s de
    # lease e 2 s de margem, cabem cinco; a sexta terminaria depois do lease.
    eventos = [_evento(posicao) for posicao in range(1, 8)]
    portas = _Portas(
        eventos,
        {e.evento_id: ResultadoPublicacao.CONFIRMADO for e in eventos},
        duracao_publicacao=5.0,
    )

    with caplog.at_level(logging.WARNING, logger="relay.publicacao"):
        resumo = asyncio.run(portas.caso().executar())

    assert portas.marcados == [e.evento_id for e in eventos[:5]]
    # Sem tentativa e sem reagendamento: o lease vence e outra reserva pega.
    assert portas.reagendados == []
    assert resumo.reservados == 7
    assert resumo.sem_tentativa == 2
    assert resumo.resultados[ResultadoPublicacao.CONFIRMADO] == 5
    (aviso,) = caplog.records
    assert aviso.__dict__["sem_tentativa"] == 2
    assert aviso.__dict__["posicao"] == 6


def test_publicacao_que_passaria_do_lease_e_interrompida_e_reagendada() -> None:
    evento = _evento(1)

    class _BrokerParado(_Portas):
        # Broker no ar sem confirmar (alarme de memória): a publicação não
        # volta, e o prazo do próprio adaptador não ajuda.
        async def publicar(self, evento: EventoReservado) -> ResultadoPublicacao:
            await asyncio.sleep(60)
            raise AssertionError("o ciclo deveria ter interrompido")

    portas = _BrokerParado([evento], {})
    prazo = PrazoReserva(
        lease=timedelta(milliseconds=500),
        publicacao=timedelta(milliseconds=100),
        margem=timedelta(milliseconds=100),
    )
    caso = PublicarPendentes(portas, portas, portas, POLITICA, prazo)

    resumo = asyncio.run(asyncio.wait_for(caso.executar(), timeout=5))

    # Interrompida no restante do lease, como confirm fora do prazo.
    assert resumo.resultados[ResultadoPublicacao.SEM_CONFIRMACAO] == 1
    assert portas.reagendados == [evento.evento_id]
    assert portas.marcados == []


@pytest.mark.parametrize(
    ("lease", "publicacao", "margem"),
    [(5, 5, 0), (6, 5, 1), (30, 0, 2), (30, 5, -1)],
    ids=["lease-igual-ao-confirm", "sem-folga", "confirm-zero", "margem-negativa"],
)
def test_prazo_que_nao_comporta_uma_publicacao_e_recusado(
    lease: int, publicacao: int, margem: int
) -> None:
    with pytest.raises(ValueError):
        PrazoReserva(
            timedelta(seconds=lease),
            timedelta(seconds=publicacao),
            timedelta(seconds=margem),
        )
