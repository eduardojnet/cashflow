"""Barreira dos consumidores, do lado do worker (005-T2; design 005 §4; TD-002).

Antes da troca e do rollback da geração ativa, o coordenador ergue a barreira
em `controle_projecao` e espera cada consumidor registrado em
`estado_consumidor` confirmar a pausa com `pausado = true` e `visto_em`
recente (FR-004). O worker:

- lê a barreira no início da transação de cada mensagem; com `solicitada`, a
  mensagem não é processada (`BarreiraSolicitada`, transação desfeita, sem
  ACK: ela volta à fila e chega de novo como reentrega depois da liberação);
- lê a barreira também a cada intervalo, com a fila parada, e grava
  `pausado = false` com `visto_em` enquanto consome;
- com a barreira solicitada, termina a mensagem em curso, fecha o consumo e
  só então grava `pausado = true`; repete a confirmação a cada intervalo até
  a barreira ser liberada e retoma o consumo.

`pausado = true` só é gravado com o consumo fechado, portanto a confirmação
garante que nenhuma mensagem está em processamento. A leitura da barreira em
cada transação cobre a corrida em que o worker retoma por ter visto a
barreira liberada pouco antes de ela ser erguida de novo: a transação da
mensagem começa depois do `pausado = false` confirmado e, por isso, enxerga
a barreira erguida antes da leitura do coordenador.
"""

from typing import Protocol


class BarreiraSolicitada(Exception):
    """Barreira erguida: a mensagem não é processada (005 TD-002)."""

    def __init__(self) -> None:
        super().__init__("barreira dos consumidores solicitada")


class ControleBarreira(Protocol):
    """Leitura da barreira e registro do consumidor (adaptador PostgreSQL)."""

    async def barreira_solicitada(self) -> bool: ...

    async def registrar_consumidor(self, consumidor_id: str, pausado: bool) -> None:
        """Grava `pausado` e `visto_em = now()` do consumidor, criando-o se novo."""
        ...


class BarreiraConsumidor:
    """Sinais do worker para o coordenador (design 005 §4, Barreira)."""

    def __init__(self, controle: ControleBarreira, consumidor_id: str) -> None:
        if not consumidor_id:
            raise ValueError("consumidor_id não pode ser vazio")
        self._controle = controle
        self.consumidor_id = consumidor_id

    async def sinalizar_consumo(self) -> bool:
        """Antes e durante o consumo: `True` se a barreira está solicitada.

        Com a barreira liberada, grava `pausado = false` e `visto_em`. Com ela
        solicitada, não grava nada: quem chama encerra o consumo e confirma a
        pausa com `confirmar_pausa`.
        """
        if await self._controle.barreira_solicitada():
            return True
        await self._controle.registrar_consumidor(self.consumidor_id, pausado=False)
        return False

    async def confirmar_pausa(self) -> bool:
        """Com o consumo fechado: grava `pausado = true` e `visto_em`.

        Devolve `True` enquanto a barreira segue solicitada; `False` quando
        foi liberada e o consumo pode ser retomado.
        """
        await self._controle.registrar_consumidor(self.consumidor_id, pausado=True)
        return await self._controle.barreira_solicitada()
