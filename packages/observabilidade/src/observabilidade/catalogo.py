"""Catálogo fechado das métricas de aplicação (007 design §4, TD-002, TD-003).

Cada métrica de INFRA 8.3 coletada pelos componentes Python tem aqui nome,
tipo, descrição e a lista fechada de rótulos permitidos. Métricas do
RabbitMQ (plugin) e do PostgreSQL (exporters) não passam por este pacote.

Rótulos nunca carregam valores financeiros, tokens ou identificadores de
cliente ou de lançamento (INFRA 8.2, FR-004).
"""

import re
from dataclasses import dataclass
from enum import Enum


class TipoMetrica(Enum):
    CONTADOR = "contador"
    MEDIDOR = "medidor"


class ErroCatalogo(ValueError):
    """Métrica ou rótulo fora do catálogo fechado."""


class MetricaDesconhecida(ErroCatalogo):
    pass


class RotuloNaoPermitido(ErroCatalogo):
    pass


@dataclass(frozen=True)
class DefinicaoMetrica:
    nome: str
    tipo: TipoMetrica
    descricao: str
    rotulos: tuple[str, ...] = ()


# Lista fechada de rótulos por métrica (TD-003). Métrica sem rótulo tem
# conjunto vazio. Nova métrica ou novo rótulo entra aqui e no design.
ROTULOS_PERMITIDOS: dict[str, frozenset[str]] = {
    "cashflow_outbox_pendentes": frozenset(),
    "cashflow_outbox_idade_mais_antigo_segundos": frozenset(),
    "cashflow_worker_reentregas_total": frozenset(),
    "cashflow_quarentena_total": frozenset(),
    "cashflow_pool_conexoes": frozenset({"componente", "estado"}),
    "cashflow_geracao_ativa": frozenset(),
    "cashflow_reconstrucao_fase": frozenset(),
    "cashflow_checkpoint_posicao": frozenset({"geracao"}),
    "cashflow_lacunas": frozenset({"geracao"}),
    "cashflow_jwt_falhas_total": frozenset({"motivo"}),
    "cashflow_jwks_falhas_total": frozenset(),
    "cashflow_http_requisicoes_total": frozenset({"componente", "status"}),
}

# Defesa adicional: nenhum rótulo pode sugerir dado sensível, mesmo que
# alguém o acrescente à lista acima por engano (INFRA 8.2).
_PADRAO_ROTULO_SENSIVEL = re.compile(
    r"cliente|lancamento|valor|saldo|token|segredo|senha|secret|password|jwt$",
    re.IGNORECASE,
)


def validar_definicao(definicao: DefinicaoMetrica) -> None:
    """Recusa métrica fora do catálogo e rótulo fora da lista permitida."""
    permitidos = ROTULOS_PERMITIDOS.get(definicao.nome)
    if permitidos is None:
        raise MetricaDesconhecida(f"métrica fora do catálogo: {definicao.nome}")
    if len(set(definicao.rotulos)) != len(definicao.rotulos):
        raise RotuloNaoPermitido(f"rótulo repetido em {definicao.nome}")
    for rotulo in definicao.rotulos:
        if rotulo not in permitidos or _PADRAO_ROTULO_SENSIVEL.search(rotulo):
            raise RotuloNaoPermitido(
                f"rótulo não permitido em {definicao.nome}: {rotulo}"
            )


CATALOGO: dict[str, DefinicaoMetrica] = {
    d.nome: d
    for d in (
        DefinicaoMetrica(
            "cashflow_outbox_pendentes",
            TipoMetrica.MEDIDOR,
            "Eventos pendentes de publicação na outbox",
        ),
        DefinicaoMetrica(
            "cashflow_outbox_idade_mais_antigo_segundos",
            TipoMetrica.MEDIDOR,
            "Idade em segundos do evento pendente mais antigo da outbox",
        ),
        DefinicaoMetrica(
            "cashflow_worker_reentregas_total",
            TipoMetrica.CONTADOR,
            "Mensagens recebidas pelo worker marcadas como reentrega",
        ),
        DefinicaoMetrica(
            "cashflow_quarentena_total",
            TipoMetrica.CONTADOR,
            "Eventos enviados para quarentena",
        ),
        DefinicaoMetrica(
            "cashflow_pool_conexoes",
            TipoMetrica.MEDIDOR,
            "Conexões do pool por componente e estado",
            ("componente", "estado"),
        ),
        DefinicaoMetrica(
            "cashflow_geracao_ativa",
            TipoMetrica.MEDIDOR,
            "Geração ativa da projeção do consolidado",
        ),
        DefinicaoMetrica(
            "cashflow_reconstrucao_fase",
            TipoMetrica.MEDIDOR,
            "Fase corrente da reconstrução da projeção",
        ),
        DefinicaoMetrica(
            "cashflow_checkpoint_posicao",
            TipoMetrica.MEDIDOR,
            "Posição contínua do checkpoint por geração",
            ("geracao",),
        ),
        DefinicaoMetrica(
            "cashflow_lacunas",
            TipoMetrica.MEDIDOR,
            "Lacunas de posição abertas por geração",
            ("geracao",),
        ),
        DefinicaoMetrica(
            "cashflow_jwt_falhas_total",
            TipoMetrica.CONTADOR,
            "Falhas de validação de JWT por motivo",
            ("motivo",),
        ),
        DefinicaoMetrica(
            "cashflow_jwks_falhas_total",
            TipoMetrica.CONTADOR,
            "Falhas de obtenção ou renovação do JWKS",
        ),
        # Fora de INFRA 8.3 (007 TD-005): `status` é o código HTTP numérico.
        DefinicaoMetrica(
            "cashflow_http_requisicoes_total",
            TipoMetrica.CONTADOR,
            "Respostas HTTP das APIs por componente e status",
            ("componente", "status"),
        ),
    )
}

for _definicao in CATALOGO.values():
    validar_definicao(_definicao)
