"""Ciclo de publicação sem infraestrutura (003-T2; FR-002).

Portas falsas registram a ordem das chamadas: marcar vem sempre depois do
confirm do mesmo evento, e só o confirm positivo sem return marca.
"""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from relay.aplicacao import EventoReservado, PublicarPendentes, ResultadoPublicacao


def _evento(posicao: int) -> EventoReservado:
    evento_id = uuid.uuid4()
    return EventoReservado(
        evento_id=evento_id,
        posicao=posicao,
        tipo_evento="LancamentoRegistrado",
        payload={"evento_id": str(evento_id), "posicao": posicao},
        tentativas=0,
        lease_ate=datetime(2026, 11, 6, tzinfo=UTC),
    )


class _Portas:
    def __init__(
        self,
        eventos: list[EventoReservado],
        resultados: dict[uuid.UUID, ResultadoPublicacao | Exception],
    ) -> None:
        self._eventos = eventos
        self._resultados = resultados
        self.chamadas: list[tuple[str, uuid.UUID]] = []

    async def reservar(self) -> list[EventoReservado]:
        return self._eventos

    async def publicar(self, evento: EventoReservado) -> ResultadoPublicacao:
        self.chamadas.append(("publicar", evento.evento_id))
        resultado = self._resultados[evento.evento_id]
        if isinstance(resultado, Exception):
            raise resultado
        return resultado

    async def marcar_publicado(self, evento_id: uuid.UUID) -> None:
        self.chamadas.append(("marcar", evento_id))

    def caso(self) -> PublicarPendentes:
        return PublicarPendentes(self, self, self)

    @property
    def marcados(self) -> list[uuid.UUID]:
        return [evento for chamada, evento in self.chamadas if chamada == "marcar"]


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
def test_sem_confirm_positivo_sem_return_o_evento_nao_e_marcado(
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
    assert resumo.resultados[resultado] == 1
    assert resumo.resultados[ResultadoPublicacao.CONFIRMADO] == 1


def test_falha_na_publicacao_interrompe_o_ciclo_sem_marcar() -> None:
    confirmado, quebrado, seguinte = _evento(1), _evento(2), _evento(3)
    portas = _Portas(
        [confirmado, quebrado, seguinte],
        {
            confirmado.evento_id: ResultadoPublicacao.CONFIRMADO,
            quebrado.evento_id: ConnectionError("broker fora"),
            seguinte.evento_id: ResultadoPublicacao.CONFIRMADO,
        },
    )

    with pytest.raises(ConnectionError):
        asyncio.run(portas.caso().executar())

    # O já confirmado fica marcado; os demais seguem pendentes na outbox.
    assert portas.marcados == [confirmado.evento_id]


def test_lote_vazio_nao_publica_nem_marca() -> None:
    portas = _Portas([], {})

    resumo = asyncio.run(portas.caso().executar())

    assert portas.chamadas == []
    assert resumo.reservados == 0
    assert set(resumo.resultados.values()) == {0}
