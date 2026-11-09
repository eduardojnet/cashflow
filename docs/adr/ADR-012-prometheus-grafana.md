# ADR-012: Prometheus + Grafana

> Status: Aceito
> Data: 2026-11-05
> Relacionado: DES (monitoramento proativo; diferencial "Monitoramento e Observabilidade"); INFRA 8.1 a 8.6; ST-TD-008; 007 TD-001, TD-004; 007 D-001; P-011

## Contexto

DES pede monitoramento proativo e lista "Monitoramento e Observabilidade"
entre os diferenciais. INFRA 8.3 enumera as métricas que a solução precisa
expor (backlog e idade da outbox, mensagens prontas e não confirmadas,
reentregas, quarentena, pools, disco, geração ativa, fase da reconstrução,
checkpoint, lacunas, falhas de JWT e JWKS), INFRA 8.4 e 8.5 pedem health
checks com readiness real, e INFRA 8.6 deixa "backend específico de métricas e
visualização ainda não definido". A escolha foi feita por Eduardo em
2026-11-05 (ST-Q-008). Não há fonte que defina canal de notificação de
alertas.

## Decisão

- **Backend e visualização (007 TD-001):** Prometheus coleta por pull o
  `/metrics` de `lancamentos-api`, `consolidado-api`, `relay` e `worker`, o
  plugin Prometheus do RabbitMQ e dois `postgres-exporter` (um por banco),
  a cada 15 s. Grafana exibe o painel `CashFlow`, com fonte de dados e painel
  provisionados por arquivo no repositório.
- **Alertas no Prometheus, sem notificação externa (007 TD-004):** regras
  avaliadas pelo Prometheus e visíveis no Grafana, com limites declarados
  como séries de gravação (`cashflow:limite_*`) no próprio arquivo de regras:
  - `CashFlowOutboxEventoAntigo`: evento pendente na outbox acima do limite
    de idade (relay ou RabbitMQ com problema);
  - `CashFlowQuarentena`: evento enviado para a quarentena;
  - `CashFlowLacunas`: lacunas de posição numa geração por mais de 1 minuto.
- **Grafana com TLS e autenticação:** publicado só em `127.0.0.1:8446`, com
  certificado da AC local, administrador com senha de `infra/.env`, sem
  cadastro nem acesso anônimo. Prometheus não é publicado
  ([ADR-016](ADR-016-isolamento-de-rede-da-observabilidade.md)).
- **Fora desta entrega:** tracing distribuído e envio de alertas para canais
  externos (sem fonte; INFRA não lista).

## Alternativas consideradas

### Alternativa 1: OpenTelemetry com coletor, métricas e tracing

- **Origem:** análise do ADR.
- **Descrição:** instrumentação OpenTelemetry nos serviços, coletor e um
  backend de traces além do de métricas.
- **Prós:** padrão aberto; correlação entre métricas, logs e traces.
- **Contras:** mais componentes (coletor, backend de traces) sem requisito
  que peça tracing.
- **Por que foi rejeitada:** INFRA 8.3 pede métricas; tracing não consta das
  fontes (P-011). Fica como evolução.

### Alternativa 2: Alertmanager com notificação por e-mail ou chat

- **Origem:** análise do ADR (007 TD-004 registra "sem fonte para canal de
  notificação").
- **Descrição:** encaminhar os alertas do Prometheus a um canal externo.
- **Prós:** monitoramento proativo completo, sem alguém olhando o painel.
- **Contras:** exige escolher canal, credenciais e destinatários, que nenhuma
  fonte define; mais um componente.
- **Por que foi rejeitada:** sem fonte para o canal; decidir isso seria
  inventar requisito (P-010).

### Alternativa 3: Serviço de observabilidade gerenciado em nuvem

- **Origem:** análise do ADR.
- **Descrição:** enviar métricas a um provedor SaaS.
- **Prós:** sem operar backend; retenção e alertas prontos.
- **Contras:** depende de conta externa e de rede para o avaliador rodar a
  solução; métricas saem da máquina local.
- **Por que foi rejeitada:** infraestrutura em nuvem está em INFRA - Fora do
  escopo.

### Alternativa 4: Só logs estruturados, sem backend de métricas

- **Origem:** análise do ADR.
- **Descrição:** extrair indicadores dos logs quando preciso.
- **Prós:** nenhum componente novo.
- **Contras:** sem série temporal nem alerta; INFRA 8.3 pede métricas.
- **Por que foi rejeitada:** não atende INFRA 8.3 nem o diferencial de DES.

## Consequências

### Positivas

- Painel e alertas sobem com o compose, sem passo manual.
- Cobertura verificada por teste: alvos coletados, cada métrica do catálogo
  presente no Prometheus, consultas do painel com dados e regras de alerta
  disparando com falha injetada
  ([`tests/observabilidade/`](../../tests/observabilidade/)).
- Taxa de erro das APIs observável fora do teste de carga
  ([ADR-015](ADR-015-catalogo-fechado-de-metricas.md)).

### Negativas / trade-offs

- Alertas só são vistos por quem abre o Grafana ou o Prometheus; não há
  notificação ativa.
- Retenção local, num nó, sem alta disponibilidade do Prometheus.
- Dois contêineres e dois exporters a mais no ambiente.

### Riscos

- Alerta disparado sem ninguém ver. Mitigação: o painel destaca os alertas;
  canal de notificação é evolução que depende de decisão de negócio.

## Notas de implementação

- Configuração em [`infra/observabilidade/`](../../infra/observabilidade/):
  `prometheus.yml`, `regras.yml` e o provisionamento do Grafana.
- Imagens `prom/prometheus:v3.16.0`, `grafana/grafana:13.3.0` e
  `quay.io/prometheuscommunity/postgres-exporter:v0.20.1`, com fonte e data no
  compose.
- Gatilho de revisão: definição de canal de notificação, requisito de
  tracing ou ambiente com mais de um nó.

## Referências

- [02 - Contêineres](../arquitetura/02-conteineres.md)
- [05 - Implantação local](../arquitetura/05-implantacao-local.md)
