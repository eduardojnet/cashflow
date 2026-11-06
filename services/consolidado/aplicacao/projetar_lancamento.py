"""Caso de uso do worker: projetar um evento no saldo do dia (004-T3; 005-T2).

Fluxo de design 004 §3, numa transação por mensagem (TD-001 a TD-003):

0. Barreira dos consumidores solicitada: a mensagem não é processada
   (`BarreiraSolicitada`; 005 TD-002, ver `consolidado.aplicacao.barreira`).
1. Mensagem ilegível (JSON inválido ou fora do contrato v1) vai para a
   quarentena com o payload bruto, sem tocar na projeção (FR-003).
2. A inbox registra o `evento_id` com o hash do evento. Mesmo hash é
   reentrega: sem efeito na geração. Hash diferente é conflito: quarentena
   (FR-001, FR-003).
3. A contribuição é única por lançamento e por posição na geração. Mesmo
   lançamento com o mesmo hash (republicação com outro `evento_id`; 003
   TD-004) não tem efeito; conteúdo diferente, ou outro lançamento na mesma
   posição, vai para a quarentena.
4. Lançamento novo: saldo do dia atualizado pelo domínio e marcador de
   inclusão gravado (FR-006).
5. Checkpoint e lacunas na mesma transação (FR-009; TD-006; 004-T4), ver
   `_avancar_checkpoint`.

Os passos 2 a 5 rodam em cada geração receptora (ativa, `construindo` e
`anterior`; design 005 §3), na mesma transação (005 TD-001): com uma
candidata em construção, cada mensagem altera as duas gerações ou nenhuma, e
os lançamentos registrados durante a reconstrução chegam à candidata (005
FR-007). Cada geração deduplica o seu: na republicação do replay, a ativa
reconhece a duplicata e a candidata aplica. As gerações são percorridas em
ordem de `id`, a mesma em todo worker, para os bloqueios de saldo e de
checkpoint não formarem ciclo entre transações concorrentes.

Desfecho da mensagem: o primeiro conflito, se houver, vai uma única vez para
a quarentena (que não é por geração; D-008) e é o desfecho, sem desfazer o
que as outras gerações aplicaram; sem conflito, `APLICADO` se alguma geração
aplicou, senão a duplicata. Só quando todas reconhecem o evento como
duplicado a transação é desfeita, porque nada foi gravado.

Quem confirma a mensagem ao RabbitMQ é o adaptador, e só depois de este caso
de uso retornar, isto é, depois do commit (FR-002; INFRA 4.8). Falha de banco
propaga sem confirmação: a mensagem volta à fila. O estado do checkpoint vai
ao observador (métricas) também só depois do commit.
"""

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Protocol
from uuid import UUID

from consolidado.aplicacao.barreira import BarreiraSolicitada
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

_MOTIVOS = {
    Desfecho.QUARENTENA_CONFLITO_EVENTO: MOTIVO_CONFLITO_EVENTO,
    Desfecho.QUARENTENA_CONFLITO_LANCAMENTO: MOTIVO_CONFLITO_LANCAMENTO,
    Desfecho.QUARENTENA_CONFLITO_POSICAO: MOTIVO_CONFLITO_POSICAO,
}


@dataclass(frozen=True)
class ContribuicaoExistente:
    """Contribuição que já ocupa o lançamento ou a posição na geração."""

    lancamento_id: UUID
    hash: str


@dataclass(frozen=True)
class EstadoCheckpoint:
    """Checkpoint e lacunas abertas de uma geração, para as métricas (§12)."""

    geracao_id: int
    posicao_continua: int
    lacunas: int


class ObservadorCheckpoint(Protocol):
    """Recebe o estado do checkpoint depois do commit (design 004 §12)."""

    def checkpoint(self, estado: EstadoCheckpoint) -> None: ...


class TransacaoProjecao(Protocol):
    """Operações da transação de design 004 §3 (adaptador PostgreSQL)."""

    async def barreira_solicitada(self) -> bool:
        """`True` com a barreira dos consumidores erguida (005 TD-002)."""
        ...

    async def geracoes_receptoras(self) -> Sequence[int]:
        """Gerações ativa, `construindo` e `anterior`, em ordem de `id`."""
        ...

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

    async def checkpoint_para_atualizar(self, geracao_id: int) -> int:
        """Posição contínua, bloqueada até o fim da transação (TD-006)."""
        ...

    async def remover_lacuna(self, geracao_id: int, posicao: int) -> bool:
        """Apaga a lacuna da posição; `True` se ela existia."""
        ...

    async def maior_posicao_abaixo(self, geracao_id: int, posicao: int) -> int:
        """Maior posição projetada abaixo da dada; 0 se nenhuma."""
        ...

    async def registrar_lacunas(self, geracao_id: int, de: int, ate: int) -> None:
        """Registra as posições de `de` a `ate`, inclusive, como lacunas."""
        ...

    async def fim_da_continuidade(self, geracao_id: int) -> int:
        """Posição antes da menor lacuna, ou a maior projetada sem lacunas."""
        ...

    async def gravar_checkpoint(self, geracao_id: int, posicao: int) -> None: ...

    async def estado_checkpoint(self, geracao_id: int) -> EstadoCheckpoint: ...

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


async def _avancar_checkpoint(
    transacao: TransacaoProjecao, geracao_id: int, posicao: int
) -> None:
    """Checkpoint como maior posição contínua; ausências viram lacunas (TD-006).

    A posição do livro não tem lacunas nem repetição (002 TD-002). Invariante
    por geração: toda posição acima do checkpoint e abaixo da maior projetada
    que ainda não chegou é uma lacuna. Com a nova posição `p` e o checkpoint
    `c`:

    - `p` era lacuna: a lacuna sai;
    - `p` acima de tudo o que já chegou: as posições entre a maior projetada
      (ou `c`) e `p` viram lacunas;
    - `p = c + 1`: o checkpoint vai até antes da menor lacuna restante, ou
      até a maior posição projetada, se não restar lacuna.

    A linha do checkpoint fica bloqueada até o COMMIT: projeções concorrentes
    da mesma geração serializam aqui e cada uma lê o que a anterior gravou.
    """
    continua = await transacao.checkpoint_para_atualizar(geracao_id)
    if posicao <= continua:
        # Não ocorre pelo invariante: a posição é única por geração e o
        # checkpoint só cobre posições projetadas. Nada a mover.
        return
    preenchida = await transacao.remover_lacuna(geracao_id, posicao)
    if not preenchida and posicao > continua + 1:
        maior = max(continua, await transacao.maior_posicao_abaixo(geracao_id, posicao))
        if maior + 1 <= posicao - 1:
            await transacao.registrar_lacunas(geracao_id, maior + 1, posicao - 1)
    if posicao == continua + 1:
        await transacao.gravar_checkpoint(
            geracao_id, await transacao.fim_da_continuidade(geracao_id)
        )


class _SemObservador:
    def checkpoint(self, estado: EstadoCheckpoint) -> None:
        pass


class ProjetarLancamento:
    def __init__(
        self,
        unidade: UnidadeDeTrabalhoProjecao,
        observador: ObservadorCheckpoint | None = None,
    ) -> None:
        self._unidade = unidade
        self._observador = observador or _SemObservador()

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
            await _respeitar_barreira(transacao)
            desfecho, estados = await self._projetar(transacao, evento, mensagem.corpo)
        # Fora do bloco: a transação terminou em COMMIT.
        for estado in estados:
            self._observador.checkpoint(estado)
        return desfecho

    async def publicar_checkpoint(self) -> list[EstadoCheckpoint]:
        """Lê o checkpoint das gerações receptoras e o entrega ao observador.

        Usado na subida da sessão do worker, para as métricas existirem antes
        da primeira mensagem (design 004 §12). Só leitura: ROLLBACK ao fim.
        """
        async with self._unidade.transacao() as transacao:
            estados = [
                await transacao.estado_checkpoint(geracao_id)
                for geracao_id in await transacao.geracoes_receptoras()
            ]
            transacao.desfazer()
        for estado in estados:
            self._observador.checkpoint(estado)
        return estados

    @staticmethod
    def _ler(mensagem: MensagemEvento) -> EventoLancamentoRegistrado | None:
        if mensagem.dados is None:
            return None
        return EventoLancamentoRegistrado.de_dados(mensagem.dados)

    async def _quarentenar_ilegivel(
        self, mensagem: MensagemEvento, motivo: str
    ) -> Desfecho:
        async with self._unidade.transacao() as transacao:
            await _respeitar_barreira(transacao)
            await transacao.quarentenar(
                _evento_id_legivel(mensagem), motivo, mensagem.corpo
            )
        return Desfecho.QUARENTENA_ILEGIVEL

    async def _projetar(
        self,
        transacao: TransacaoProjecao,
        evento: EventoLancamentoRegistrado,
        corpo: bytes,
    ) -> tuple[Desfecho, list[EstadoCheckpoint]]:
        """Desfecho e o checkpoint de cada geração em que o lançamento entrou."""
        geracoes = await transacao.geracoes_receptoras()
        if not geracoes:
            raise RuntimeError("nenhuma geração receptora (migração 0001)")
        desfechos: list[Desfecho] = []
        estados: list[EstadoCheckpoint] = []
        for geracao_id in geracoes:
            desfecho = await _projetar_na_geracao(transacao, geracao_id, evento)
            desfechos.append(desfecho)
            if desfecho is Desfecho.APLICADO:
                estados.append(await transacao.estado_checkpoint(geracao_id))

        conflito = next((d for d in desfechos if d.quarentena), None)
        if conflito is not None:
            await transacao.quarentenar(evento.evento_id, _MOTIVOS[conflito], corpo)
            return conflito, estados
        if Desfecho.APLICADO in desfechos:
            return Desfecho.APLICADO, estados
        if Desfecho.LANCAMENTO_DUPLICADO in desfechos:
            # A inbox de ao menos uma geração ganhou o evento: COMMIT.
            return Desfecho.LANCAMENTO_DUPLICADO, estados
        # Duplicata em todas: nada foi gravado.
        transacao.desfazer()
        return Desfecho.EVENTO_DUPLICADO, estados


async def _respeitar_barreira(transacao: TransacaoProjecao) -> None:
    """Primeiro comando da transação: com a barreira erguida, nada é gravado."""
    if await transacao.barreira_solicitada():
        raise BarreiraSolicitada()


async def _projetar_na_geracao(
    transacao: TransacaoProjecao,
    geracao_id: int,
    evento: EventoLancamentoRegistrado,
) -> Desfecho:
    """Passos 2 a 5 numa geração; a quarentena fica com quem chama."""
    registrado = await transacao.registrar_inbox(
        geracao_id, evento.evento_id, evento.hash
    )
    recebimento = classificar(registrado, evento.hash)
    if recebimento is Recebimento.DUPLICATA:
        return Desfecho.EVENTO_DUPLICADO
    if recebimento is Recebimento.CONFLITO:
        return Desfecho.QUARENTENA_CONFLITO_EVENTO

    contribuicao = evento.contribuicao
    existente = await transacao.registrar_contribuicao(geracao_id, contribuicao)
    if existente is not None:
        # O evento fica na inbox: a próxima reentrega dele é duplicata. A
        # posição já estava projetada: o checkpoint não muda.
        if existente.lancamento_id != contribuicao.lancamento_id:
            return Desfecho.QUARENTENA_CONFLITO_POSICAO
        if classificar(existente.hash, contribuicao.hash) is Recebimento.CONFLITO:
            return Desfecho.QUARENTENA_CONFLITO_LANCAMENTO
        return Desfecho.LANCAMENTO_DUPLICADO

    saldo = await transacao.saldo_para_atualizar(
        geracao_id, contribuicao.data_lancamento
    )
    await transacao.gravar_saldo(geracao_id, saldo.aplicar(contribuicao))
    await transacao.marcar_inclusao(geracao_id, contribuicao)
    await _avancar_checkpoint(transacao, geracao_id, contribuicao.posicao)
    return Desfecho.APLICADO
