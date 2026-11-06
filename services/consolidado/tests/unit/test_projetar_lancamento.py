"""Caso de uso do worker sem infraestrutura (004-T3; design 004 §3).

A unidade de trabalho falsa guarda o estado como o banco guardaria (inbox,
contribuição, saldo, marcador e quarentena) e só o publica no "commit", para
os testes verem o que cada desfecho grava e o que desfaz.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import date
from decimal import Decimal
from uuid import UUID

from consolidado.aplicacao import (
    MOTIVO_CONFLITO_EVENTO,
    MOTIVO_CONFLITO_LANCAMENTO,
    MOTIVO_CONFLITO_POSICAO,
    MOTIVO_JSON_INVALIDO,
    ContribuicaoExistente,
    Desfecho,
    MensagemEvento,
    ProjetarLancamento,
    TransacaoProjecao,
)
from consolidado.dominio import Contribuicao, SaldoDiario

GERACAO = 7
DATA = date(2026, 11, 6)


class _Estado:
    def __init__(self) -> None:
        self.inbox: dict[tuple[int, UUID], str] = {}
        self.contribuicoes: dict[tuple[int, UUID], Contribuicao] = {}
        self.saldos: dict[tuple[int, date], SaldoDiario] = {}
        self.marcadores: dict[tuple[int, UUID], int] = {}
        self.quarentena: list[tuple[UUID | None, str, bytes]] = []

    def copia(self) -> "_Estado":
        outro = _Estado()
        outro.inbox = dict(self.inbox)
        outro.contribuicoes = dict(self.contribuicoes)
        outro.saldos = dict(self.saldos)
        outro.marcadores = dict(self.marcadores)
        outro.quarentena = list(self.quarentena)
        return outro


class _Transacao:
    def __init__(self, estado: _Estado) -> None:
        self.estado = estado
        self.desfeita = False

    def desfazer(self) -> None:
        self.desfeita = True

    async def geracao_ativa(self) -> int:
        return GERACAO

    async def registrar_inbox(
        self, geracao_id: int, evento_id: UUID, hash_evento: str
    ) -> str | None:
        chave = (geracao_id, evento_id)
        if chave in self.estado.inbox:
            return self.estado.inbox[chave]
        self.estado.inbox[chave] = hash_evento
        return None

    async def registrar_contribuicao(
        self, geracao_id: int, contribuicao: Contribuicao
    ) -> ContribuicaoExistente | None:
        mesma = self.estado.contribuicoes.get((geracao_id, contribuicao.lancamento_id))
        if mesma is not None:
            return ContribuicaoExistente(mesma.lancamento_id, mesma.hash)
        for (geracao, _), outra in self.estado.contribuicoes.items():
            if geracao == geracao_id and outra.posicao == contribuicao.posicao:
                return ContribuicaoExistente(outra.lancamento_id, outra.hash)
        self.estado.contribuicoes[(geracao_id, contribuicao.lancamento_id)] = (
            contribuicao
        )
        return None

    async def saldo_para_atualizar(self, geracao_id: int, data: date) -> SaldoDiario:
        return self.estado.saldos.get((geracao_id, data), SaldoDiario.vazio(data))

    async def gravar_saldo(self, geracao_id: int, saldo: SaldoDiario) -> None:
        self.estado.saldos[(geracao_id, saldo.data)] = saldo

    async def marcar_inclusao(
        self, geracao_id: int, contribuicao: Contribuicao
    ) -> None:
        chave = (geracao_id, contribuicao.lancamento_id)
        assert chave not in self.estado.marcadores
        self.estado.marcadores[chave] = contribuicao.posicao

    async def quarentenar(
        self, evento_id: UUID | None, motivo: str, payload_bruto: bytes
    ) -> None:
        self.estado.quarentena.append((evento_id, motivo, payload_bruto))


class _Unidade:
    def __init__(self) -> None:
        self.estado = _Estado()
        self.commits = 0
        self.rollbacks = 0

    @asynccontextmanager
    async def _transacao(self) -> AsyncIterator[TransacaoProjecao]:
        transacao = _Transacao(self.estado.copia())
        try:
            yield transacao
        except BaseException:
            self.rollbacks += 1
            raise
        if transacao.desfeita:
            self.rollbacks += 1
        else:
            self.estado = transacao.estado
            self.commits += 1

    def transacao(self) -> AbstractAsyncContextManager[TransacaoProjecao]:
        return self._transacao()


def _evento(**campos: object) -> dict[str, object]:
    evento: dict[str, object] = {
        "evento_id": str(uuid.uuid4()),
        "versao": 1,
        "lancamento_id": str(uuid.uuid4()),
        "posicao": 1,
        "tipo": "CREDITO",
        "valor": "100.00",
        "data_lancamento": DATA.isoformat(),
        "registrado_em": "2026-11-06T12:00:00+00:00",
    }
    evento.update(campos)
    return evento


def _mensagem(evento: dict[str, object]) -> MensagemEvento:
    return MensagemEvento(
        corpo=json.dumps(evento).encode(),
        dados=evento,
        identificador=str(evento.get("evento_id")),
    )


def _projetar(unidade: _Unidade, *eventos: dict[str, object]) -> list[Desfecho]:
    caso = ProjetarLancamento(unidade)

    async def executar() -> list[Desfecho]:
        return [await caso.executar(_mensagem(e)) for e in eventos]

    return asyncio.run(executar())


def _saldo(unidade: _Unidade) -> SaldoDiario:
    return unidade.estado.saldos[(GERACAO, DATA)]


def test_lancamento_novo_atualiza_saldo_e_marca_inclusao_na_geracao_ativa() -> None:
    unidade = _Unidade()
    credito = _evento(posicao=1, tipo="CREDITO", valor="100.00")
    debito = _evento(posicao=2, tipo="DEBITO", valor="30.25")

    desfechos = _projetar(unidade, credito, debito)

    assert desfechos == [Desfecho.APLICADO, Desfecho.APLICADO]
    saldo = _saldo(unidade)
    assert (saldo.total_creditos, saldo.total_debitos, saldo.saldo_dia) == (
        Decimal("100.00"),
        Decimal("30.25"),
        Decimal("69.75"),
    )
    assert unidade.estado.marcadores == {
        (GERACAO, UUID(str(credito["lancamento_id"]))): 1,
        (GERACAO, UUID(str(debito["lancamento_id"]))): 2,
    }
    assert unidade.commits == 2


def test_evento_duplicado_desfaz_a_transacao_sem_efeito() -> None:
    unidade = _Unidade()
    evento = _evento()

    desfechos = _projetar(unidade, evento, evento)

    assert desfechos == [Desfecho.APLICADO, Desfecho.EVENTO_DUPLICADO]
    assert _saldo(unidade).total_creditos == Decimal("100.00")
    assert (unidade.commits, unidade.rollbacks) == (1, 1)
    assert unidade.estado.quarentena == []


def test_evento_repetido_com_outro_conteudo_vai_para_quarentena() -> None:
    unidade = _Unidade()
    original = _evento()
    adulterado = {**original, "valor": "99.00"}

    desfechos = _projetar(unidade, original, adulterado)

    assert desfechos[1] is Desfecho.QUARENTENA_CONFLITO_EVENTO
    assert _saldo(unidade).total_creditos == Decimal("100.00")
    [(evento_id, motivo, corpo)] = unidade.estado.quarentena
    assert evento_id == UUID(str(original["evento_id"]))
    assert motivo == MOTIVO_CONFLITO_EVENTO
    assert corpo == json.dumps(adulterado).encode()


def test_mesmo_lancamento_em_outro_evento_com_mesmo_conteudo_nao_tem_efeito() -> None:
    unidade = _Unidade()
    original = _evento()
    republicado = {**original, "evento_id": str(uuid.uuid4())}

    desfechos = _projetar(unidade, original, republicado)

    assert desfechos == [Desfecho.APLICADO, Desfecho.LANCAMENTO_DUPLICADO]
    assert _saldo(unidade).total_creditos == Decimal("100.00")
    # O evento republicado fica na inbox, sem quarentena.
    assert (GERACAO, UUID(str(republicado["evento_id"]))) in unidade.estado.inbox
    assert unidade.estado.quarentena == []


def test_mesmo_lancamento_com_outro_conteudo_vai_para_quarentena() -> None:
    unidade = _Unidade()
    original = _evento()
    divergente = {**original, "evento_id": str(uuid.uuid4()), "valor": "1.00"}

    desfechos = _projetar(unidade, original, divergente)

    assert desfechos[1] is Desfecho.QUARENTENA_CONFLITO_LANCAMENTO
    assert _saldo(unidade).total_creditos == Decimal("100.00")
    assert [motivo for _, motivo, _ in unidade.estado.quarentena] == [
        MOTIVO_CONFLITO_LANCAMENTO
    ]


def test_outro_lancamento_na_mesma_posicao_vai_para_quarentena() -> None:
    unidade = _Unidade()
    primeiro = _evento(posicao=5)
    intruso = _evento(posicao=5)

    desfechos = _projetar(unidade, primeiro, intruso)

    assert desfechos[1] is Desfecho.QUARENTENA_CONFLITO_POSICAO
    assert _saldo(unidade).total_creditos == Decimal("100.00")
    assert [motivo for _, motivo, _ in unidade.estado.quarentena] == [
        MOTIVO_CONFLITO_POSICAO
    ]
    assert (GERACAO, UUID(str(intruso["lancamento_id"]))) not in (
        unidade.estado.marcadores
    )


def test_corpo_ilegivel_vai_para_quarentena_com_o_identificador_da_mensagem() -> None:
    unidade = _Unidade()
    identificador = str(uuid.uuid4())
    mensagem = MensagemEvento(
        corpo=b"{quebrado", dados=None, identificador=identificador
    )

    desfecho = asyncio.run(ProjetarLancamento(unidade).executar(mensagem))

    assert desfecho is Desfecho.QUARENTENA_ILEGIVEL
    assert unidade.estado.quarentena == [
        (UUID(identificador), MOTIVO_JSON_INVALIDO, b"{quebrado")
    ]
    assert unidade.estado.inbox == {}


def test_evento_fora_do_contrato_vai_para_quarentena_sem_repetir_o_valor() -> None:
    unidade = _Unidade()
    evento = _evento(valor="123.456")

    [desfecho] = _projetar(unidade, evento)

    assert desfecho is Desfecho.QUARENTENA_ILEGIVEL
    [(evento_id, motivo, _)] = unidade.estado.quarentena
    assert evento_id == UUID(str(evento["evento_id"]))
    assert motivo == "evento inválido: valor: deve ter no máximo 2 casas decimais"
    assert "123.456" not in motivo
    assert unidade.estado.inbox == {} and unidade.estado.saldos == {}


def test_quarentena_sem_evento_id_legivel_fica_sem_identificacao() -> None:
    unidade = _Unidade()
    evento = _evento(evento_id="nao-e-uuid")
    mensagem = MensagemEvento(
        corpo=json.dumps(evento).encode(), dados=evento, identificador=None
    )

    desfecho = asyncio.run(ProjetarLancamento(unidade).executar(mensagem))

    assert desfecho is Desfecho.QUARENTENA_ILEGIVEL
    [(evento_id, motivo, _)] = unidade.estado.quarentena
    assert evento_id is None
    assert motivo == "evento inválido: evento_id: deve ser UUID"


def test_falha_na_transacao_propaga_sem_gravar_nada() -> None:
    class _Falha(_Unidade):
        @asynccontextmanager
        async def _transacao(self) -> AsyncIterator[TransacaoProjecao]:
            async with super()._transacao() as transacao:
                yield transacao
                raise ConnectionError("banco fora")

    unidade = _Falha()
    caso = ProjetarLancamento(unidade)

    try:
        asyncio.run(caso.executar(_mensagem(_evento())))
    except ConnectionError:
        pass
    else:
        raise AssertionError("a falha de banco deveria propagar")
    assert (unidade.commits, unidade.rollbacks) == (0, 1)
    assert unidade.estado.saldos == {} and unidade.estado.inbox == {}


def test_somente_desfechos_de_quarentena_contam_como_quarentena() -> None:
    assert {d for d in Desfecho if d.quarentena} == {
        Desfecho.QUARENTENA_ILEGIVEL,
        Desfecho.QUARENTENA_CONFLITO_EVENTO,
        Desfecho.QUARENTENA_CONFLITO_LANCAMENTO,
        Desfecho.QUARENTENA_CONFLITO_POSICAO,
    }
