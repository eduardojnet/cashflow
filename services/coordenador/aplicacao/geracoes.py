"""Troca, rollback e descarte da geração ativa (005-T4; design 005 §3, §4).

Troca (FR-004, FR-005, FR-009) e rollback (FR-006) seguem o mesmo protocolo,
sob o bloqueio exclusivo de operação do coordenador:

1. ergue a barreira em `controle_projecao` (`solicitada`, versão nova) e
   grava a fase `barreira`, numa transação;
2. espera cada consumidor registrado em `estado_consumidor` confirmar a
   pausa: `pausado = true` com `visto_em` posterior ao pedido da barreira.
   O worker só grava a pausa com o consumo fechado, portanto nenhuma
   mensagem está em processamento (005-T2). Sem a confirmação de todos
   dentro do tempo limite (DQ-001), a operação é recusada. Um consumidor
   registrado que não roda mais também impede a troca: ele não confirma;
3. numa única transação, confere que a barreira e a versão são as do
   pedido, aplica o critério de aceite com os consumidores parados (FR-009,
   só na troca), muda os estados (a ativa sai antes da outra entrar) e
   aponta `geracao_ativa_id` para a nova ativa (TD-003). A API lê o ponteiro
   em cada consulta: a troca vale na consulta seguinte, sem reinício;
4. libera a barreira, com sucesso ou recusa. Se a troca não aconteceu, a
   fase volta à de antes da barreira.

Qualquer falha antes do COMMIT do passo 3 desfaz a transação inteira: a
ativa e o ponteiro ficam como estavam. Se o próprio coordenador cair com a
barreira erguida, os consumidores seguem pausados até a próxima operação de
troca ou rollback, que libera a barreira ao terminar.

O bloqueio de operação impede duas operações de geração ao mesmo tempo;
quem chega com ele ocupado é recusado sem esperar. A versão da barreira
detecta quem mexeu nela sem o bloqueio: com outra versão no passo 3, nada
muda.

O descarte (D-005; INFRA 7.5) leva a anterior a `descartada` e remove os
dados dela, sem barreira, como o cancelamento da reconstrução: a anterior
deixa de receber eventos no commit e uma mensagem já em curso que ainda
grave nela não é lida por ninguém.

Logs: cada transição com a geração e o operador técnico (design 005 §12),
sem valores financeiros.
"""

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from coordenador.aplicacao.reconstrucao import (
    OperacaoConcluida,
    SituacaoConsumidor,
    TransacaoGeracoes,
)
from coordenador.dominio import (
    EstadoGeracao,
    FaseReconstrucao,
    Geracao,
    Progresso,
    SemAnterior,
    SemReconstrucao,
    TrocaRecusada,
    pendencias_de_troca,
    trocar_ativa,
)

log = logging.getLogger("coordenador.geracoes")

BARREIRA_SOLICITADA = "solicitada"
_NOMES_DE_FASE = frozenset(str(fase) for fase in FaseReconstrucao)
TEMPO_LIMITE_PADRAO_SEGUNDOS = 30.0
INTERVALO_PADRAO_SEGUNDOS = 0.2


class OperacaoRecusada(Exception):
    """Operação de geração recusada sem mudar nenhuma geração."""


class OperacaoConcorrente(OperacaoRecusada):
    """Outra operação de geração em andamento ou barreira alterada por fora."""

    def __init__(self, motivo: str) -> None:
        super().__init__(f"operação concorrente: {motivo}")


class BarreiraNaoConfirmada(OperacaoRecusada):
    """Consumidores sem confirmação de pausa no tempo limite (design 005 §8)."""

    def __init__(self, pendentes: list[str], tempo_limite_segundos: float) -> None:
        super().__init__(
            f"barreira não confirmada em {tempo_limite_segundos:g} s pelos"
            f" consumidores: {', '.join(pendentes)}; troca cancelada"
        )
        self.pendentes = pendentes


@dataclass(frozen=True)
class Controle:
    """Linha única de `controle_projecao`."""

    geracao_ativa_id: int
    barreira: str
    versao: int


@dataclass(frozen=True)
class PedidoBarreira:
    versao: int
    # Relógio do banco no pedido; a confirmação de pausa tem de ser posterior.
    instante: datetime
    # A barreira já estava solicitada: sobra de uma operação interrompida.
    herdada: bool


class TransacaoTroca(TransacaoGeracoes, Protocol):
    """`TransacaoGeracoes` mais o que a troca, o rollback e o descarte usam."""

    def geracoes_para_atualizar(self) -> list[Geracao]:
        """Gerações fora de `descartada`, travadas até o fim da transação."""
        ...

    def controle_para_atualizar(self) -> Controle: ...

    def erguer_barreira(self) -> PedidoBarreira: ...

    def liberar_barreira(self) -> bool:
        """Libera a barreira solicitada; False se ela já estava liberada."""
        ...

    def consumidores(self) -> list[SituacaoConsumidor]: ...

    def progresso(self, geracao_id: int) -> Progresso: ...

    def fase_de(self, geracao_id: int) -> str | None: ...

    def adiar_exclusao(self) -> None:
        """Adia `ex_geracao_uma_alem_da_ativa` para o COMMIT (migração 0002)."""
        ...

    def apontar_ativa(self, geracao_id: int) -> None:
        """`controle_projecao.geracao_ativa_id` (TD-003), com versão nova."""
        ...


AbrirTransacaoTroca = Callable[[], AbstractContextManager[TransacaoTroca]]
# Bloqueio exclusivo de operação; recusa com `OperacaoConcorrente` se ocupado.
BloquearOperacao = Callable[[], AbstractContextManager[None]]


@dataclass(frozen=True)
class ConfiguracaoBarreira:
    tempo_limite_segundos: float = TEMPO_LIMITE_PADRAO_SEGUNDOS
    intervalo_segundos: float = INTERVALO_PADRAO_SEGUNDOS

    def __post_init__(self) -> None:
        if self.tempo_limite_segundos <= 0 or self.intervalo_segundos <= 0:
            raise ValueError("tempo limite e intervalo da barreira devem ser positivos")


@dataclass(frozen=True)
class TrocaConcluida:
    ativa_id: int
    anterior_id: int
    operador: str


def pendentes_da_barreira(
    consumidores: Iterable[SituacaoConsumidor], desde: datetime
) -> list[str]:
    """Consumidores sem pausa confirmada depois de `desde` (design 005 §4)."""
    return [
        c.consumidor_id for c in consumidores if not (c.pausado and c.visto_em > desde)
    ]


@dataclass(frozen=True)
class _Alvo:
    """O que a barreira protege: quem sai, quem entra e de quem é a fase."""

    pedido: PedidoBarreira
    ativa_id: int
    entrando_id: int
    fase_geracao_id: int
    fase_antes: str | None


class _TrocaComBarreira(ABC):
    """Protocolo comum de troca e rollback (passos 1 a 4 do módulo)."""

    # Estado de quem entra: `construindo` na troca, `anterior` no rollback.
    _entrando: EstadoGeracao
    _operacao: str

    def __init__(
        self,
        abrir: AbrirTransacaoTroca,
        bloquear: BloquearOperacao,
        configuracao: ConfiguracaoBarreira | None = None,
        relogio: Callable[[], float] = time.monotonic,
        dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        self._abrir = abrir
        self._bloquear = bloquear
        self._configuracao = configuracao or ConfiguracaoBarreira()
        self._relogio = relogio
        self._dormir = dormir

    @abstractmethod
    def _sem_entrando(self) -> Exception:
        """Erro quando não há geração no estado de quem entra."""

    def _verificar(self, transacao: TransacaoTroca, ativa: int, entrando: int) -> None:
        """Critério de aceite com os consumidores pausados; nada por padrão."""

    @abstractmethod
    def _fase_geracao(self, ativa: Geracao, entrando: Geracao) -> int:
        """Geração cuja fase a operação grava."""

    @abstractmethod
    def _fase_final(self, alvo: _Alvo) -> FaseReconstrucao: ...

    def __call__(self) -> TrocaConcluida:
        with self._bloquear():
            alvo: _Alvo | None = None
            trocou = False
            try:
                # Dentro do `try`: mesmo recusada aqui, a operação libera uma
                # barreira que tenha sobrado de operação interrompida.
                alvo = self._erguer_barreira()
                self._aguardar_pausa(alvo.pedido)
                concluida = self._trocar(alvo)
                trocou = True
                log.info(
                    "geração ativa trocada",
                    extra={
                        "operacao": self._operacao,
                        "geracao_id": concluida.ativa_id,
                        "estado": str(EstadoGeracao.ATIVA),
                        "geracao_anterior_id": concluida.anterior_id,
                        "fase": str(self._fase_final(alvo)),
                        "operador": concluida.operador,
                    },
                )
            finally:
                # Sem a troca, a fase volta à de antes da barreira.
                self._liberar_barreira(None if trocou else alvo)
        return concluida

    def _erguer_barreira(self) -> _Alvo:
        with self._abrir() as transacao:
            geracoes = transacao.geracoes_para_atualizar()
            ativa = next((g for g in geracoes if g.estado is EstadoGeracao.ATIVA), None)
            if ativa is None:
                raise RuntimeError("nenhuma geração ativa (migração 0001)")
            entrando = next((g for g in geracoes if g.estado is self._entrando), None)
            if entrando is None:
                raise self._sem_entrando()
            fase_geracao_id = self._fase_geracao(ativa, entrando)
            fase_antes = transacao.fase_de(fase_geracao_id)
            pedido = transacao.erguer_barreira()
            transacao.registrar_fase(fase_geracao_id, FaseReconstrucao.BARREIRA)
            operador = transacao.operador()
        if pedido.herdada:
            log.warning(
                "barreira já solicitada por operação interrompida; assumida",
                extra={"operacao": self._operacao, "operador": operador},
            )
        log.info(
            "barreira solicitada",
            extra={
                "operacao": self._operacao,
                "geracao_id": entrando.id,
                "versao": pedido.versao,
                "fase": str(FaseReconstrucao.BARREIRA),
                "operador": operador,
            },
        )
        return _Alvo(pedido, ativa.id, entrando.id, fase_geracao_id, fase_antes)

    def _aguardar_pausa(self, pedido: PedidoBarreira) -> None:
        tempo_limite = self._configuracao.tempo_limite_segundos
        limite = self._relogio() + tempo_limite
        while True:
            with self._abrir() as transacao:
                consumidores = transacao.consumidores()
            pendentes = pendentes_da_barreira(consumidores, pedido.instante)
            if not pendentes:
                log.info(
                    "pausa confirmada pelos consumidores",
                    extra={
                        "operacao": self._operacao,
                        "consumidores": len(consumidores),
                    },
                )
                return
            if self._relogio() >= limite:
                raise BarreiraNaoConfirmada(pendentes, tempo_limite)
            self._dormir(self._configuracao.intervalo_segundos)

    def _trocar(self, alvo: _Alvo) -> TrocaConcluida:
        with self._abrir() as transacao:
            controle = transacao.controle_para_atualizar()
            if (
                controle.barreira != BARREIRA_SOLICITADA
                or controle.versao != alvo.pedido.versao
            ):
                raise OperacaoConcorrente(
                    "a barreira mudou durante a espera da pausa; nada foi trocado"
                )
            geracoes = {g.id: g for g in transacao.geracoes_para_atualizar()}
            ativa = geracoes.get(controle.geracao_ativa_id)
            entrando = geracoes.get(alvo.entrando_id)
            if (
                ativa is None
                or ativa.id != alvo.ativa_id
                or entrando is None
                or entrando.estado is not self._entrando
            ):
                raise OperacaoConcorrente(
                    "as gerações mudaram durante a espera da pausa; nada foi trocado"
                )
            self._verificar(transacao, ativa.id, entrando.id)
            saindo, nova = trocar_ativa(ativa, entrando)
            transacao.adiar_exclusao()
            transacao.mudar_estado(saindo.id, saindo.estado)
            transacao.mudar_estado(nova.id, nova.estado)
            transacao.apontar_ativa(nova.id)
            transacao.registrar_fase(alvo.fase_geracao_id, self._fase_final(alvo))
            operador = transacao.operador()
        return TrocaConcluida(
            ativa_id=nova.id, anterior_id=saindo.id, operador=operador
        )

    def _liberar_barreira(self, restaurar: _Alvo | None) -> None:
        with self._abrir() as transacao:
            liberada = transacao.liberar_barreira()
            fase_antes = None if restaurar is None else restaurar.fase_antes
            if restaurar is not None and fase_antes in _NOMES_DE_FASE:
                transacao.registrar_fase(
                    restaurar.fase_geracao_id, FaseReconstrucao(fase_antes)
                )
            operador = transacao.operador()
        if liberada:
            log.info(
                "barreira liberada",
                extra={
                    "operacao": self._operacao,
                    "troca_feita": restaurar is None,
                    "operador": operador,
                },
            )


class TrocarGeracao(_TrocaComBarreira):
    """A candidata vira a ativa; a ativa fica `anterior` para rollback (D-005)."""

    _entrando = EstadoGeracao.CONSTRUINDO
    _operacao = "trocar"

    def _sem_entrando(self) -> Exception:
        return SemReconstrucao()

    def _verificar(self, transacao: TransacaoTroca, ativa: int, entrando: int) -> None:
        pendencias = pendencias_de_troca(
            transacao.progresso(entrando), transacao.progresso(ativa)
        )
        if pendencias:
            raise TrocaRecusada(pendencias)

    def _fase_geracao(self, ativa: Geracao, entrando: Geracao) -> int:
        return entrando.id

    def _fase_final(self, alvo: _Alvo) -> FaseReconstrucao:
        return FaseReconstrucao.CONCLUIDA


class ReverterTroca(_TrocaComBarreira):
    """Rollback: a anterior volta a ser a ativa; a ativa fica `anterior`.

    A anterior seguiu recebendo eventos (TD-001), então está atualizada; o
    critério da troca (FR-009) não se aplica.
    """

    _entrando = EstadoGeracao.ANTERIOR
    _operacao = "rollback"

    def _sem_entrando(self) -> Exception:
        return SemAnterior()

    def _fase_geracao(self, ativa: Geracao, entrando: Geracao) -> int:
        # A reconstruída é a mais nova do par: a candidata nasce depois.
        return max(ativa.id, entrando.id)

    def _fase_final(self, alvo: _Alvo) -> FaseReconstrucao:
        if alvo.fase_geracao_id == alvo.ativa_id:
            return FaseReconstrucao.REVERTIDA
        return FaseReconstrucao.CONCLUIDA


class DescartarAnterior:
    """Descarta a anterior e libera o espaço dela (D-005; INFRA 7.5)."""

    def __init__(self, abrir: AbrirTransacaoTroca, bloquear: BloquearOperacao) -> None:
        self._abrir = abrir
        self._bloquear = bloquear

    def __call__(self) -> OperacaoConcluida:
        with self._bloquear(), self._abrir() as transacao:
            anterior = next(
                (
                    g
                    for g in transacao.geracoes_para_atualizar()
                    if g.estado is EstadoGeracao.ANTERIOR
                ),
                None,
            )
            if anterior is None:
                raise SemAnterior()
            descartada = anterior.transitar(EstadoGeracao.DESCARTADA)
            transacao.mudar_estado(descartada.id, descartada.estado)
            transacao.registrar_fase(descartada.id, FaseReconstrucao.DESCARTADA)
            transacao.remover_dados(descartada.id)
            operador = transacao.operador()
        log.info(
            "geração anterior descartada",
            extra={
                "geracao_id": descartada.id,
                "estado": str(descartada.estado),
                "fase": str(FaseReconstrucao.DESCARTADA),
                "operador": operador,
            },
        )
        return OperacaoConcluida(descartada.id, operador)
