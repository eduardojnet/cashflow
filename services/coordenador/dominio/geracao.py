"""Ciclo de vida das gerações da projeção (005-T1; design 005 §3).

```
construindo --troca--> ativa --troca--> anterior --rollback--> ativa
construindo --cancelar--> descartada
anterior --descartar--> descartada
```

O banco impõe as mesmas transições (gatilho `geracao_transicao_valida`,
migração 0002 do consolidado); este módulo deixa o coordenador recusar antes
de abrir transação e com mensagem do domínio.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from coordenador.dominio.erros import ReconstrucaoRecusada, TransicaoInvalida


class EstadoGeracao(StrEnum):
    CONSTRUINDO = "construindo"
    ATIVA = "ativa"
    ANTERIOR = "anterior"
    DESCARTADA = "descartada"


TRANSICOES: frozenset[tuple[EstadoGeracao, EstadoGeracao]] = frozenset(
    {
        (EstadoGeracao.CONSTRUINDO, EstadoGeracao.ATIVA),
        (EstadoGeracao.ATIVA, EstadoGeracao.ANTERIOR),
        (EstadoGeracao.ANTERIOR, EstadoGeracao.ATIVA),
        (EstadoGeracao.CONSTRUINDO, EstadoGeracao.DESCARTADA),
        (EstadoGeracao.ANTERIOR, EstadoGeracao.DESCARTADA),
    }
)

# Gerações que recebem eventos do worker (design 005 §3; TD-001).
RECEPTORAS: frozenset[EstadoGeracao] = frozenset(
    {EstadoGeracao.ATIVA, EstadoGeracao.CONSTRUINDO, EstadoGeracao.ANTERIOR}
)

# Com uma delas, a reconstrução não inicia: no máximo duas gerações
# coexistem (INFRA 7.5) e a anterior fica até o descarte manual (D-005).
IMPEDEM_RECONSTRUCAO: tuple[EstadoGeracao, ...] = (
    EstadoGeracao.CONSTRUINDO,
    EstadoGeracao.ANTERIOR,
)


def transicao_valida(origem: EstadoGeracao, destino: EstadoGeracao) -> bool:
    return (origem, destino) in TRANSICOES


@dataclass(frozen=True)
class Geracao:
    id: int
    estado: EstadoGeracao

    @property
    def recebe_eventos(self) -> bool:
        return self.estado in RECEPTORAS

    def transitar(self, destino: EstadoGeracao) -> "Geracao":
        """Nova geração no estado `destino`; recusa transição fora do diagrama."""
        if not transicao_valida(self.estado, destino):
            raise TransicaoInvalida(self.estado, destino)
        return Geracao(id=self.id, estado=destino)


def trocar_ativa(saindo: Geracao, entrando: Geracao) -> tuple[Geracao, Geracao]:
    """Troca (FR-005) e rollback (FR-006): `saindo` vai de `ativa` a `anterior`
    e `entrando` (a candidata na troca, a anterior no rollback) vira a ativa.

    Devolve as duas gerações nos estados novos, na ordem em que o banco deve
    mudá-las: a ativa sai antes, porque `ux_geracao_uma_ativa` não admite
    duas ativas nem por um comando (migração 0002).
    """
    return saindo.transitar(EstadoGeracao.ANTERIOR), entrando.transitar(
        EstadoGeracao.ATIVA
    )


def verificar_inicio_reconstrucao(geracoes: Iterable[Geracao]) -> None:
    """Recusa a reconstrução se já houver geração `construindo` ou `anterior`."""
    presentes = {geracao.estado for geracao in geracoes}
    impedimentos = [
        str(estado) for estado in IMPEDEM_RECONSTRUCAO if estado in presentes
    ]
    if impedimentos:
        raise ReconstrucaoRecusada(impedimentos)
