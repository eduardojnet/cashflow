"""Processo do relay da outbox (003-T5; design 003 §12, Execução).

    python -m relay

Laço de publicação, health em `HEALTH_PORTA` (padrão 8080) e /metrics em
`METRICAS_PORTA` (padrão 9464), ambos em porta interna, sem publicação no
host (INFRA 6.1; 007 design §4). Encerra de forma limpa em SIGTERM e SIGINT
(`docker compose stop`): termina o ciclo em curso, fecha broker e pool. O
evento com lease ativo de um ciclo interrompido volta quando o lease vence
(NFR-003).
"""

import asyncio
import os
import signal

from observabilidade import Metricas

from relay.adaptadores.http import ServidorSaude
from relay.adaptadores.logs import configurar_logging
from relay.adaptadores.metricas import MetricasRelay
from relay.adaptadores.postgres import (
    MedicaoOutboxPostgres,
    RegistroPublicacaoPostgres,
    ReservaOutboxPostgres,
)
from relay.adaptadores.rabbitmq import PublicadorRabbitMQ
from relay.aplicacao import PublicarPendentes
from relay.principal import (
    ConfiguracaoRelay,
    LacoRelay,
    criar_pool,
    fabrica_de_conexao,
    prontidao_de,
)


async def executar(configuracao: ConfiguracaoRelay) -> None:
    metricas = Metricas()
    metricas_relay = MetricasRelay(metricas)
    servidor_metricas = metricas.iniciar_servidor(configuracao.metricas_porta)

    pool = criar_pool(configuracao)
    conexao = fabrica_de_conexao(pool)
    publicador = PublicadorRabbitMQ(
        configuracao.url_rabbitmq, configuracao.timeout_confirmacao_segundos
    )
    ciclo = PublicarPendentes(
        ReservaOutboxPostgres(
            conexao, configuracao.dono, configuracao.lote, configuracao.lease
        ),
        publicador,
        RegistroPublicacaoPostgres(conexao),
        configuracao.politica,
        configuracao.prazo_reserva(),
    )
    laco = LacoRelay(
        configuracao, ciclo, MedicaoOutboxPostgres(conexao), publicador, metricas_relay
    )
    saude = ServidorSaude(
        prontidao_de(pool, publicador, configuracao.prontidao_timeout_segundos)
    )

    parar = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sinal in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sinal, parar.set)

    # O pool abre sem esperar o banco: o relay sobe com o banco fora e
    # responde ready falso até conectar (INFRA 8.5).
    await pool.open(wait=False)
    await saude.iniciar(configuracao.health_porta)
    try:
        await laco.executar(parar)
    finally:
        await saude.parar()
        await publicador.fechar()
        await pool.close()
        servidor_metricas.parar()


def main() -> None:
    configurar_logging(os.environ.get("LOG_NIVEL", "INFO"))
    try:
        configuracao = ConfiguracaoRelay.de_ambiente(os.environ)
    except ValueError as erro:
        raise SystemExit(str(erro)) from None
    asyncio.run(executar(configuracao))


if __name__ == "__main__":
    main()
