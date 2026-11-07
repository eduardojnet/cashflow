"""Casos de uso da reconstrução: iniciar, acompanhar e cancelar (005-T3).

Fluxo de design 005 §3: `iniciar` cria a geração candidata (`construindo`)
com checkpoint 0 e fase `construindo`; a partir do commit, o worker aplica
cada mensagem também nela (TD-001). O operador então reabre a outbox no lado
do livro (`relay-republicar --desde-posicao 1`; D-002), e a candidata
converge enquanto a ativa segue atendendo consultas (FR-003). `status`
mostra checkpoint e lacunas de cada geração e o critério de troca (FR-009);
`cancelar` descarta a candidata antes da troca e libera o espaço dela
(INFRA 7.5).

Cada operação é uma transação da porta `TransacaoGeracoes`, no papel
consolidado_operacao (FR-008; INFRA 5.11). As regras do ciclo de vida são
conferidas aqui, pelo domínio, e de novo no banco (gatilho e exclusão da
migração 0002), que é quem resolve operadores concorrentes.

Logs: cada transição de estado com a geração e o operador técnico (design
005 §12). Nenhum valor financeiro: só identificadores, estados e posições.
"""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from coordenador.dominio import (
    EstadoGeracao,
    FaseReconstrucao,
    Geracao,
    Progresso,
    SemReconstrucao,
    pendencias_de_troca,
    verificar_inicio_reconstrucao,
)

log = logging.getLogger("coordenador.reconstrucao")

# Identidade técnica dedicada da reconstrução (FR-008; INFRA 5.11).
PAPEL_OPERACAO = "consolidado_operacao"


class IdentidadeRecusada(Exception):
    """Conexão com outro papel que não `PAPEL_OPERACAO` (FR-008).

    Os privilégios da migração 0002 já barram os papéis do worker e da API;
    esta verificação barra também o dono das tabelas (consolidado_migracao),
    que pode tudo no banco, antes de qualquer comando.
    """

    def __init__(self, papel: str) -> None:
        super().__init__(
            f"identidade {papel} não autorizada; o coordenador opera só como"
            f" {PAPEL_OPERACAO}"
        )
        self.papel = papel


@dataclass(frozen=True)
class SituacaoGeracao:
    """Geração não descartada, com o progresso da projeção nela."""

    id: int
    estado: EstadoGeracao
    criada_em: datetime
    ativada_em: datetime | None
    checkpoint: int
    checkpoint_atualizado_em: datetime | None
    # Maior posição já projetada; 0 = nenhuma.
    maior_posicao: int
    lacunas: int
    # As menores posições em lacuna, em ordem; no máximo o limite pedido.
    primeiras_lacunas: tuple[int, ...]
    fase: str | None
    fase_atualizada_em: datetime | None

    @property
    def progresso(self) -> Progresso:
        return Progresso(checkpoint=self.checkpoint, lacunas=self.lacunas)


@dataclass(frozen=True)
class SituacaoConsumidor:
    consumidor_id: str
    pausado: bool
    visto_em: datetime


@dataclass(frozen=True)
class Situacao:
    """Leitura do controle da projeção para o `status` (design 005 §4)."""

    geracao_ativa_id: int
    barreira: str
    versao: int
    geracoes: tuple[SituacaoGeracao, ...]
    consumidores: tuple[SituacaoConsumidor, ...]

    def _no_estado(self, estado: EstadoGeracao) -> SituacaoGeracao | None:
        return next((g for g in self.geracoes if g.estado is estado), None)

    @property
    def ativa(self) -> SituacaoGeracao | None:
        return self._no_estado(EstadoGeracao.ATIVA)

    @property
    def candidata(self) -> SituacaoGeracao | None:
        return self._no_estado(EstadoGeracao.CONSTRUINDO)

    @property
    def anterior(self) -> SituacaoGeracao | None:
        return self._no_estado(EstadoGeracao.ANTERIOR)

    def pendencias_de_troca(self) -> list[str] | None:
        """FR-009 sobre esta leitura; None sem candidata. Só acompanhamento:
        a troca (005-T4) decide com os consumidores em pausa."""
        candidata, ativa = self.candidata, self.ativa
        if candidata is None or ativa is None:
            return None
        return pendencias_de_troca(candidata.progresso, ativa.progresso)


class TransacaoGeracoes(Protocol):
    """Uma transação no banco do consolidado, papel consolidado_operacao."""

    def operador(self) -> str:
        """Identidade técnica da conexão, para o log (design 005 §12)."""
        ...

    def geracoes_vigentes(self) -> list[Geracao]:
        """Gerações fora de `descartada`."""
        ...

    def criar_candidata(self) -> int:
        """Geração `construindo` com checkpoint 0; recusa com
        `ReconstrucaoRecusada` se outra transação já criou uma (INFRA 7.5)."""
        ...

    def candidata_para_atualizar(self) -> Geracao | None:
        """Geração `construindo`, travada até o fim da transação."""
        ...

    def mudar_estado(self, geracao_id: int, estado: EstadoGeracao) -> None: ...

    def registrar_fase(self, geracao_id: int, fase: FaseReconstrucao) -> None: ...

    def remover_dados(self, geracao_id: int) -> None:
        """Apaga os dados de uma geração já `descartada` (INFRA 7.5)."""
        ...

    def situacao(self, limite_lacunas: int) -> Situacao: ...


AbrirTransacao = Callable[[], AbstractContextManager[TransacaoGeracoes]]


@dataclass(frozen=True)
class OperacaoConcluida:
    geracao_id: int
    operador: str


class IniciarReconstrucao:
    """Cria a candidata; recusa com `construindo` ou `anterior` existente."""

    def __init__(self, abrir: AbrirTransacao) -> None:
        self._abrir = abrir

    def __call__(self) -> OperacaoConcluida:
        with self._abrir() as transacao:
            verificar_inicio_reconstrucao(transacao.geracoes_vigentes())
            geracao_id = transacao.criar_candidata()
            transacao.registrar_fase(geracao_id, FaseReconstrucao.CONSTRUINDO)
            operador = transacao.operador()
        log.info(
            "geração candidata criada",
            extra={
                "geracao_id": geracao_id,
                "estado": str(EstadoGeracao.CONSTRUINDO),
                "fase": str(FaseReconstrucao.CONSTRUINDO),
                "operador": operador,
            },
        )
        return OperacaoConcluida(geracao_id, operador)


class CancelarReconstrucao:
    """Descarta a candidata antes da troca e libera os dados dela.

    A candidata deixa de receber eventos no commit. Uma mensagem que o worker
    já tinha começado a aplicar antes disso ainda pode gravar na geração
    descartada; essas linhas não são lidas por ninguém (só a ativa responde
    e só as receptoras recebem eventos) e a reconstrução seguinte usa outra
    geração.
    """

    def __init__(self, abrir: AbrirTransacao) -> None:
        self._abrir = abrir

    def __call__(self) -> OperacaoConcluida:
        with self._abrir() as transacao:
            candidata = transacao.candidata_para_atualizar()
            if candidata is None:
                raise SemReconstrucao()
            descartada = candidata.transitar(EstadoGeracao.DESCARTADA)
            transacao.mudar_estado(descartada.id, descartada.estado)
            transacao.registrar_fase(descartada.id, FaseReconstrucao.CANCELADA)
            transacao.remover_dados(descartada.id)
            operador = transacao.operador()
        log.info(
            "geração candidata cancelada",
            extra={
                "geracao_id": descartada.id,
                "estado": str(descartada.estado),
                "fase": str(FaseReconstrucao.CANCELADA),
                "operador": operador,
            },
        )
        return OperacaoConcluida(descartada.id, operador)


class ConsultarSituacao:
    """Leitura do `status`, sem alterar nada."""

    def __init__(self, abrir: AbrirTransacao) -> None:
        self._abrir = abrir

    def __call__(self, limite_lacunas: int = 20) -> Situacao:
        if limite_lacunas < 0:
            raise ValueError("o limite de lacunas não pode ser negativo")
        with self._abrir() as transacao:
            return transacao.situacao(limite_lacunas)
