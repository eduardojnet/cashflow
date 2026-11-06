"""Validador JWT configurado para a API de lançamentos (002-T4)."""

import os
from collections.abc import Callable, Mapping

from observabilidade import Metricas
from seguranca import ConfiguracaoJwt, ValidadorJwt, criar_validador

AUDIENCIA = "lancamentos-api"
ESCOPO_REGISTRAR = "lancamentos:registrar"
ESCOPO_CONSULTAR = "lancamentos:consultar"


def configuracao_de_ambiente(
    ambiente: Mapping[str, str] | None = None,
) -> ConfiguracaoJwt:
    """`JWT_AUDIENCIA` é opcional aqui: o padrão é a audiência desta API."""
    return ConfiguracaoJwt.de_ambiente(
        os.environ if ambiente is None else ambiente, audiencia_padrao=AUDIENCIA
    )


def criar_validador_lancamentos(
    metricas: Metricas,
    ambiente: Mapping[str, str] | None = None,
    relogio: Callable[[], float] | None = None,
) -> ValidadorJwt:
    return criar_validador(configuracao_de_ambiente(ambiente), metricas, relogio)
