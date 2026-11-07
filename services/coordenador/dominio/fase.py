"""Fases da reconstrução e o código numérico da métrica (005-T3; INFRA 8.3).

A fase de cada geração reconstruída fica em `fase_reconstrucao` (migração
0002 do consolidado). O coordenador roda como CLI e não é alvo do
Prometheus: a API do consolidado lê a fase mais recente e a expõe em
`cashflow_reconstrucao_fase`, medidor sem rótulos (007 design §4). Por isso
cada fase tem um código fixo; o painel traduz o código no nome.

| Código | Fase | Quem grava |
|---|---|---|
| 0 | nenhuma reconstrução registrada | (ausência de linha) |
| 1 | `construindo`: candidata criada, recebendo o replay | `reconstruir iniciar` |
| 2 | `barreira`: consumidores em pausa para a troca | troca e rollback (005-T4) |
| 3 | `concluida`: candidata virou a ativa | troca (005-T4) |
| 4 | `revertida`: rollback devolveu a ativa à anterior | rollback (005-T4) |
| 5 | `cancelada`: candidata descartada antes da troca | `reconstruir cancelar` |
| 6 | `descartada`: geração reconstruída descartada depois da troca | descarte (005-T4) |

Os códigos não mudam de significado: fase nova recebe código novo. A API
do consolidado guarda o mesmo mapa (sua imagem não leva o coordenador) e um
teste do coordenador confere que os dois são iguais.
"""

from enum import StrEnum


class FaseReconstrucao(StrEnum):
    CONSTRUINDO = "construindo"
    BARREIRA = "barreira"
    CONCLUIDA = "concluida"
    REVERTIDA = "revertida"
    CANCELADA = "cancelada"
    DESCARTADA = "descartada"


CODIGO_SEM_RECONSTRUCAO = 0

CODIGO_FASE: dict[FaseReconstrucao, int] = {
    FaseReconstrucao.CONSTRUINDO: 1,
    FaseReconstrucao.BARREIRA: 2,
    FaseReconstrucao.CONCLUIDA: 3,
    FaseReconstrucao.REVERTIDA: 4,
    FaseReconstrucao.CANCELADA: 5,
    FaseReconstrucao.DESCARTADA: 6,
}
