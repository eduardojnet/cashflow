"""Processo do worker do consolidado (004-T3, T4; design 004 §3, §12).

    python -m consolidado.worker

Laço de consumo, health em `HEALTH_PORTA` (padrão 8080) e /metrics em
`METRICAS_PORTA` (padrão 9464), ambos em porta interna, sem publicação no
host (INFRA 6.1; 007 design §4). Encerra de forma limpa em SIGTERM e SIGINT
(`docker compose stop`): cancela o consumo, termina a mensagem em curso e
fecha broker e pool. O que foi recebido e não confirmado volta à fila.
"""

import asyncio
import os
import signal

from observabilidade import Metricas

from consolidado.adaptadores.logs import configurar_logging
from consolidado.adaptadores.postgres import UnidadeDeTrabalhoProjecaoPostgres
from consolidado.adaptadores.rabbitmq import ConsumidorRabbitMQ, ProcessadorMensagens
from consolidado.aplicacao import ProjetarLancamento
from consolidado.worker.metricas import MetricasWorker, medir_pool
from consolidado.worker.principal import (
    ConfiguracaoWorker,
    LacoWorker,
    banco_pronto_de,
    criar_pool,
    fabrica_de_conexao,
    prontidao_de,
)
from consolidado.worker.saude import ServidorSaude


async def executar(configuracao: ConfiguracaoWorker) -> None:
    metricas = Metricas()
    metricas_worker = MetricasWorker(metricas)
    servidor_metricas = metricas.iniciar_servidor(configuracao.metricas_porta)

    pool = criar_pool(configuracao)
    medir_pool(pool, metricas)
    projetar = ProjetarLancamento(
        UnidadeDeTrabalhoProjecaoPostgres(fabrica_de_conexao(pool)), metricas_worker
    )
    processador = ProcessadorMensagens(projetar, metricas_worker)
    consumidor = ConsumidorRabbitMQ(configuracao.url_rabbitmq, configuracao.prefetch)
    banco_pronto = banco_pronto_de(pool, configuracao.prontidao_timeout_segundos)
    laco = LacoWorker(
        configuracao,
        consumidor,
        processador.processar,
        banco_pronto,
        projetar.publicar_checkpoint,
    )
    saude = ServidorSaude(prontidao_de(banco_pronto, consumidor))

    parar = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sinal in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sinal, parar.set)

    # O pool abre sem esperar o banco: o worker sobe com o banco fora e
    # responde ready falso até conectar (INFRA 8.5).
    await pool.open(wait=False)
    await saude.iniciar(configuracao.health_porta)
    try:
        await laco.executar(parar)
    finally:
        await saude.parar()
        await consumidor.fechar()
        await pool.close()
        servidor_metricas.parar()


def main() -> None:
    configurar_logging(os.environ.get("LOG_NIVEL", "INFO"))
    try:
        configuracao = ConfiguracaoWorker.de_ambiente(os.environ)
    except ValueError as erro:
        raise SystemExit(str(erro)) from None
    asyncio.run(executar(configuracao))


if __name__ == "__main__":
    main()
