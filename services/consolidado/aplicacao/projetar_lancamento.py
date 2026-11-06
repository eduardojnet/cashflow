"""Caso de uso do worker: projetar um evento no saldo do dia (004-T3).

Fluxo de design 004 §3, numa transação por mensagem (TD-001 a TD-003):

1. Mensagem ilegível (JSON inválido ou fora do contrato v1) vai para a
   quarentena com o payload bruto, sem tocar na projeção (FR-003).
2. A inbox registra o `evento_id` com o hash do evento. Mesmo hash é
   reentrega: a transação é desfeita, sem efeito. Hash diferente é conflito:
   quarentena (FR-001, FR-003).
3. A contribuição é única por lançamento e por posição na geração. Mesmo
   lançamento com o mesmo hash (republicação com outro `evento_id`; 003
   TD-004) não tem efeito; conteúdo diferente, ou outro lançamento na mesma
   posição, vai para a quarentena.
4. Lançamento novo: saldo do dia atualizado pelo domínio e marcador de
   inclusão gravado (FR-006). Checkpoint e lacunas: 004-T4.

Quem confirma a mensagem ao RabbitMQ é o adaptador, e só depois de este caso
de uso retornar, isto é, depois do commit (FR-002; INFRA 4.8). Falha de banco
propaga sem confirmação: a mensagem volta à fila.
"""

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Protocol
from uuid import UUID

from consolidado.dominio import (
    Contribuicao,
    ErroDominio,
    EventoLancamentoRegistrado,
    Recebimento,
    SaldoDiario,
    classificar,
)


@dataclass(frozen=True)
class MensagemEvento:
    """Mensagem como chegou do broker, já com a tentativa de ler o JSON.

    `dados` é `None` quando o corpo não é um objeto JSON em UTF-8; ler bytes
    e JSON é do adaptador (design 004 §4). `identificador` é o `message_id`
    da mensagem, que o relay preenche com o `evento_id` (003-T2); serve à
    quarentena quando o corpo não traz um `evento_id` legível.
    """

    corpo: bytes
    dados: Mapping[str, object] | None
    identificador: str | None = None


class Desfecho(Enum):
    """O que a transação fez com a mensagem; todos terminam em ACK."""

    APLICADO = "aplicado"
    EVENTO_DUPLICADO = "evento_duplicado"
    LANCAMENTO_DUPLICADO = "lancamento_duplicado"
    QUARENTENA_ILEGIVEL = "quarentena_ilegivel"
    QUARENTENA_CONFLITO_EVENTO = "quarentena_conflito_evento"
    QUARENTENA_CONFLITO_LANCAMENTO = "quarentena_conflito_lancamento"
    QUARENTENA_CONFLITO_POSICAO = "quarentena_conflito_posicao"

    @property
    def quarentena(self) -> bool:
        return self.name.startswith("QUARENTENA_")


# Motivos gravados na quarentena: texto fixo ou a regra violada, nunca o
# valor recebido (INFRA 8.2).
MOTIVO_JSON_INVALIDO = "mensagem ilegível: corpo não é objeto JSON em UTF-8"
MOTIVO_CONFLITO_EVENTO = "conflito: evento já recebido com outro conteúdo"
MOTIVO_CONFLITO_LANCAMENTO = "conflito: lançamento já projetado com outro conteúdo"
MOTIVO_CONFLITO_POSICAO = "conflito: posição já projetada para outro lançamento"


@dataclass(frozen=True)
class ContribuicaoExistente:
    """Contribuição que já ocupa o lançamento ou a posição na geração."""

    lancamento_id: UUID
    hash: str


class TransacaoProjecao(Protocol):
    """Operações da transação de design 004 §3 (adaptador PostgreSQL)."""

    async def geracao_ativa(self) -> int: ...

    async def registrar_inbox(
        self, geracao_id: int, evento_id: UUID, hash_evento: str
    ) -> str | None:
        """Insere na inbox; devolve o hash já registrado, ou `None` se novo."""
        ...

    async def registrar_contribuicao(
        self, geracao_id: int, contribuicao: Contribuicao
    ) -> ContribuicaoExistente | None:
        """Insere a contribuição; devolve a que já ocupa lançamento ou posição."""
        ...

    async def saldo_para_atualizar(self, geracao_id: int, data: date) -> SaldoDiario:
        """Saldo do dia bloqueado até o fim da transação (vazio se novo)."""
        ...

    async def gravar_saldo(self, geracao_id: int, saldo: SaldoDiario) -> None: ...

    async def marcar_inclusao(
        self, geracao_id: int, contribuicao: Contribuicao
    ) -> None: ...

    async def quarentenar(
        self, evento_id: UUID | None, motivo: str, payload_bruto: bytes
    ) -> None: ...

    def desfazer(self) -> None:
        """Pede ROLLBACK ao fim da transação em vez de COMMIT."""
        ...


class UnidadeDeTrabalhoProjecao(Protocol):
    def transacao(self) -> AbstractAsyncContextManager[TransacaoProjecao]:
        """Transação única: COMMIT na saída normal, ROLLBACK em exceção."""
        ...


def _evento_id_legivel(mensagem: MensagemEvento) -> UUID | None:
    candidatos: list[object] = [mensagem.identificador]
    if mensagem.dados is not None:
        candidatos.insert(0, mensagem.dados.get("evento_id"))
    for candidato in candidatos:
        if isinstance(candidato, str):
            try:
                return UUID(candidato)
            except ValueError:
                continue
    return None


class ProjetarLancamento:
    def __init__(self, unidade: UnidadeDeTrabalhoProjecao) -> None:
        self._unidade = unidade

    async def executar(self, mensagem: MensagemEvento) -> Desfecho:
        try:
            evento = self._ler(mensagem)
        except ErroDominio as erro:
            return await self._quarentenar_ilegivel(
                mensagem, f"evento inválido: {erro}"
            )
        if evento is None:
            return await self._quarentenar_ilegivel(mensagem, MOTIVO_JSON_INVALIDO)

        async with self._unidade.transacao() as transacao:
            return await self._projetar(transacao, evento, mensagem.corpo)

    @staticmethod
    def _ler(mensagem: MensagemEvento) -> EventoLancamentoRegistrado | None:
        if mensagem.dados is None:
            return None
        return EventoLancamentoRegistrado.de_dados(mensagem.dados)

    async def _quarentenar_ilegivel(
        self, mensagem: MensagemEvento, motivo: str
    ) -> Desfecho:
        async with self._unidade.transacao() as transacao:
            await transacao.quarentenar(
                _evento_id_legivel(mensagem), motivo, mensagem.corpo
            )
        return Desfecho.QUARENTENA_ILEGIVEL

    async def _projetar(
        self,
        transacao: TransacaoProjecao,
        evento: EventoLancamentoRegistrado,
        corpo: bytes,
    ) -> Desfecho:
        geracao_id = await transacao.geracao_ativa()

        registrado = await transacao.registrar_inbox(
            geracao_id, evento.evento_id, evento.hash
        )
        recebimento = classificar(registrado, evento.hash)
        if recebimento is Recebimento.DUPLICATA:
            transacao.desfazer()
            return Desfecho.EVENTO_DUPLICADO
        if recebimento is Recebimento.CONFLITO:
            await transacao.quarentenar(evento.evento_id, MOTIVO_CONFLITO_EVENTO, corpo)
            return Desfecho.QUARENTENA_CONFLITO_EVENTO

        contribuicao = evento.contribuicao
        existente = await transacao.registrar_contribuicao(geracao_id, contribuicao)
        if existente is not None:
            # O evento fica na inbox: a próxima reentrega dele é duplicata.
            if existente.lancamento_id != contribuicao.lancamento_id:
                await transacao.quarentenar(
                    evento.evento_id, MOTIVO_CONFLITO_POSICAO, corpo
                )
                return Desfecho.QUARENTENA_CONFLITO_POSICAO
            if classificar(existente.hash, contribuicao.hash) is Recebimento.CONFLITO:
                await transacao.quarentenar(
                    evento.evento_id, MOTIVO_CONFLITO_LANCAMENTO, corpo
                )
                return Desfecho.QUARENTENA_CONFLITO_LANCAMENTO
            return Desfecho.LANCAMENTO_DUPLICADO

        saldo = await transacao.saldo_para_atualizar(
            geracao_id, contribuicao.data_lancamento
        )
        await transacao.gravar_saldo(geracao_id, saldo.aplicar(contribuicao))
        await transacao.marcar_inclusao(geracao_id, contribuicao)
        return Desfecho.APLICADO
