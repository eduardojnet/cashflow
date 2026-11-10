# Evoluções futuras

O que ficou fora desta entrega e como evoluir. Atende a DES - Observações
("descrições sobre o que você gostaria de ter implementado ou evoluções
futuras para o sistema proposto") e parte da lista de INFRA - Fora do escopo
atual. As specs ficam em `.ai/sdd/specs/`, pasta local que não é versionada
(decisão 006 D-006).

Fontes:

- DES = [`Desafio_Arquiteto_Soluções.md`](../Desafio_Arquiteto_Solu%C3%A7%C3%B5es.md)
- INFRA = [`Requisitos_de_Infraestrutura.md`](../Requisitos_de_Infraestrutura.md)

Como ler:

- **Nada deste documento está implementado.** A coluna "Como evoluir" é
  Proposta: direção sugerida pelo autor, não decisão aprovada nem requisito.
- **Gatilho** é o fato que justificaria a evolução. Quando o gatilho já
  consta de um ADR, o ADR é citado; a evolução não deve começar antes dele,
  pelo princípio P-011 (não acrescentar componente sem requisito que o
  exija).
- Nenhum número novo é proposto aqui: metas de disponibilidade, RPO, RTO,
  retenção e escala dependem de decisão de negócio (INFRA 7.7).

## Fora do escopo de INFRA

Os 12 itens de INFRA - Fora do escopo atual, na ordem da fonte.

| # | Item de INFRA | Situação nesta entrega | Gatilho | Como evoluir (Proposta) |
|---|---------------|------------------------|---------|-------------------------|
| 1 | Kubernetes | Docker Compose local, um nó ([ADR-010](adr/ADR-010-docker-compose-local.md)) | Requisito de alta disponibilidade, de implantação em nuvem ou de mais de um nó (gatilho do ADR-010) | Manter as mesmas imagens e variáveis de ambiente; traduzir cada serviço do compose em um objeto do orquestrador, as redes internas em políticas de rede e os health checks live e ready já existentes (INFRA 8.4, 8.5) em sondas |
| 2 | Alta disponibilidade produtiva | Uma instância de cada componente | Meta de disponibilidade definida pelo negócio | Mais de uma réplica das APIs atrás de balanceador (item 4); bancos e broker redundantes (itens 11 e 12). Antes de duplicar relay e worker, revisar os ADRs que assumem uma instância: o relay reserva lotes com `FOR UPDATE SKIP LOCKED` e lease ([ADR-003](adr/ADR-003-outbox-transacional-e-relay.md)), e a projeção assume um consumidor (gatilho do [ADR-007](adr/ADR-007-geracoes-barreira-troca-atomica.md): "mais de um consumidor de projeção") |
| 3 | Múltiplas zonas ou regiões | Um nó local | Requisito de tolerância à perda de uma zona ou região | Depois dos itens 2, 11 e 12: réplicas distribuídas entre zonas; regiões múltiplas exigem antes decidir onde fica o livro, que é a única fonte de verdade (P-002) |
| 4 | Balanceador de carga | APIs publicadas direto em loopback (INFRA 6.5, 6.6) | Mais de uma réplica de API (item 2) | Balanceador com TLS na frente das réplicas; cada API continua validando o JWT (INFRA 5.5), sem delegar a validação ao balanceador |
| 5 | API Gateway | Cada API valida o próprio token (INFRA 5.5, 5.12; [ADR-008](adr/ADR-008-keycloak-client-credentials-jwt.md)) | Exposição a clientes externos à organização, com limites de uso (gatilho do ADR-008) | Gateway para limite de taxa por cliente e roteamento, mantendo a validação em cada API como defesa em profundidade |
| 6 | Infraestrutura em nuvem | Execução local | Decisão de hospedagem | Primeiro passo sem mudar a arquitetura: o compose numa VM, estimado em [`custos.md`](custos.md#cenário-google-cloud). Depois, trocar componentes por serviços gerenciados um a um, cada troca com ADR próprio |
| 7 | Multiempresa | Um comerciante; escopos por operação, sem autorização por recurso ([ADR-013](adr/ADR-013-escopo-lancamentos-consultar.md)) | Segundo comerciante na mesma instalação | Identificador do comerciante no lançamento, no evento e na projeção; autorização por recurso (ver RBAC e ABAC abaixo); decidir entre isolamento por linha, por esquema ou por banco, em ADR |
| 8 | Disaster recovery produtivo | Recuperação documentada e testada (testes e2e) só para a perda do RabbitMQ, a partir do livro e da outbox (INFRA 7.6; [04 - Fluxos](arquitetura/04-fluxos.md#9-recuperação-após-perda-do-rabbitmq)); perda do banco do livro sem procedimento | Definição de RPO e RTO (item 10) | Backup do livro (a projeção é reconstruível a partir dele, [ADR-007](adr/ADR-007-geracoes-barreira-troca-atomica.md)); restauração ensaiada; réplica fora do local principal |
| 9 | Autoscaling | Um processo uvicorn por API ([ADR-009](adr/ADR-009-stack-python.md); [`testes/carga-consolidado.md`](testes/carga-consolidado.md#recursos-da-máquina)) | Meta de vazão que um processo Python por API não atenda (gatilho do ADR-009) | Primeiro mais processos por contêiner ou mais réplicas fixas; escala automática só depois do item 1, guiada pelas métricas de `/metrics` já expostas ([`observabilidade.md`](observabilidade.md)) |
| 10 | SLA, RPO ou RTO numéricos | Pendentes (INFRA 7.7; 001/D-002 em [`requisitos.md`](requisitos.md#spec-001-plataforma-local-de-execução)) | Decisão de negócio | Definir os números com o negócio; derivar deles a política de backup (item 8) e os alertas ([`observabilidade.md`](observabilidade.md#alertas)) |
| 11 | Cluster RabbitMQ | Instância única com volume persistente; o RabbitMQ não é fonte de verdade (INFRA 4.11; [ADR-004](adr/ADR-004-rabbitmq-confirms-ack-apos-commit.md)) | Exigência de alta disponibilidade do broker (gatilho do ADR-004) | Cluster com filas replicadas. A garantia financeira não depende disso: perda do broker já se recupera pelo livro e pela outbox |
| 12 | Cluster PostgreSQL | Duas instâncias exclusivas, livro e consolidado (INFRA 3.1 a 3.3; [ADR-001](adr/ADR-001-servicos-independentes-bancos-exclusivos.md)) | Meta de disponibilidade ou de RPO para o livro | Réplica com failover para o livro primeiro, pois é a fonte de verdade; o consolidado pode ser reconstruído e tolera recuperação mais lenta |

## Outras evoluções conhecidas

Itens fora desta entrega por decisão registrada ou por gatilho de ADR.

### Segurança

| Evolução | Situação atual | Gatilho | Como evoluir (Proposta) |
|----------|----------------|---------|-------------------------|
| RBAC e ABAC | Fora do escopo (INFRA 5.12); autorização por escopo de operação | Autorização por recurso ou multiempresa (gatilhos do [ADR-008](adr/ADR-008-keycloak-client-credentials-jwt.md) e do [ADR-013](adr/ADR-013-escopo-lancamentos-consultar.md)) | Claim de comerciante no token e verificação de propriedade do recurso em cada API |
| Revogação imediata de token | O token vale até expirar, 300 s ([ADR-008](adr/ADR-008-keycloak-client-credentials-jwt.md); [`seguranca.md`](seguranca.md#fora-desta-entrega)) | Necessidade de revogação imediata (gatilho do ADR-008) | Introspecção de token ou lista de revogação consultada pelas APIs |
| Limite de taxa por cliente | Ausente ([`seguranca.md`](seguranca.md#fora-desta-entrega)) | Exposição a clientes externos | No API Gateway (item 5) |
| AC reconhecida e cofre de segredos | AC local e `infra/.env` atendem só ao desenvolvimento (INFRA 5.8, 5.9) | Qualquer implantação fora do ambiente local | Certificado de AC pública ou corporativa; segredos num cofre, injetados em tempo de execução |

### Dados e domínio

| Evolução | Situação atual | Gatilho | Como evoluir (Proposta) |
|----------|----------------|---------|-------------------------|
| Política numérica de retenção e backup | Pendente (INFRA 7.7); outbox, respostas idempotentes e inbox crescem sem limite ([ADR-003](adr/ADR-003-outbox-transacional-e-relay.md), [ADR-005](adr/ADR-005-inbox-deduplicacao-quarentena.md)) | Definição de retenção pelo negócio | Expurgo do que já foi publicado e consolidado, sem tocar no livro, que precisa ficar íntegro para replay (INFRA 7.2, 7.3) |
| Estorno vinculado | Erro corrigido com novo lançamento de tipo contrário (002/D-003) | Necessidade de rastrear o lançamento estornado | Referência opcional ao lançamento original, mantendo o livro imutável (INFRA 3.1) |
| Multimoeda e categoria | Só BRL, sem categoria (002/D-001, 002/D-004) | Operação em outra moeda ou relatório por categoria | Moeda no lançamento e saldo por moeda; categoria como atributo do lançamento e do evento |
| Reprocessamento automatizado da quarentena | Tratamento manual dos eventos em quarentena ([ADR-005](adr/ADR-005-inbox-deduplicacao-quarentena.md)) | Volume de quarentena que exija automação (gatilho do ADR-005) | Comando operacional de reprocessamento, com o mesmo controle de acesso do coordenador |
| Reconstrução automática ou a partir de snapshot | Reconstrução acionada pelo operador, por replay completo ([ADR-007](adr/ADR-007-geracoes-barreira-troca-atomica.md)) | Volume que torne o replay completo inviável (gatilho do ADR-007) | Snapshot periódico da projeção com a posição de checkpoint; replay só do que vem depois |

### Desempenho e escala

| Evolução | Situação atual | Gatilho | Como evoluir (Proposta) |
|----------|----------------|---------|-------------------------|
| Cache de consulta do consolidado | Sem cache; meta de carga atingida sem ele ([ADR-011](adr/ADR-011-sem-cache-na-entrega-inicial.md); [`testes/carga-consolidado.md`](testes/carga-consolidado.md)) | Teste de carga abaixo da meta, pico maior que o de DES ou latência crescendo com o número de datas (gatilho do ADR-011) | Cache por data com invalidação na consolidação, ou saldo acumulado materializado |
| Contador de posição único | Um contador monotônico no livro ([ADR-006](adr/ADR-006-posicao-monotonica-e-checkpoint.md)) | Meta de vazão de escrita que o contador não atenda (gatilho do ADR-006) | Medir primeiro a contenção; só então avaliar alternativa, em ADR próprio |
| Consulta periódica da outbox | Relay por polling ([ADR-003](adr/ADR-003-outbox-transacional-e-relay.md)) | Polling como gargalo de vazão (gatilho do ADR-003) | Notificação do banco para acordar o relay, mantendo o polling como garantia |
| Capacidade validada | Baseline provisória `[NVI]` (INFRA, "Capacidade local") | Escolha do ambiente de produção | Teste de carga e de falha no ambiente-alvo, registrando os recursos medidos |

### Observabilidade

| Evolução | Situação atual | Gatilho | Como evoluir (Proposta) |
|----------|----------------|---------|-------------------------|
| Canal de notificação dos alertas | Alertas avaliados pelo Prometheus, sem envio ([ADR-012](adr/ADR-012-prometheus-grafana.md)) | Definição de canal pelo negócio (gatilho do ADR-012) | Alertmanager com o canal escolhido |
| Rastreamento distribuído (tracing) | Ausente; logs correlacionados ([`observabilidade.md`](observabilidade.md#logs-estruturados)) | Requisito de tracing (gatilho do ADR-012) | Instrumentação das APIs, relay e worker, propagando o identificador do evento já usado nos logs |

## Lacunas conhecidas da implementação

O que foi especificado e não foi entregue, registrado para não ser
apresentado como implementado:

| Lacuna | Onde está registrada |
|--------|----------------------|
| Script único de verificação (`scripts/verificar.sh`), previsto no steering, não existe; lint, tipos e testes rodam por comandos separados | [ADR-009](adr/ADR-009-stack-python.md) |
| O relay não expõe `cashflow_pool_conexoes`, embora o mapa de métricas da spec 007 o liste | [`observabilidade.md`](observabilidade.md#métricas-de-infra-83) |

## Transição de um legado

A arquitetura de transição é "não aplicável: sem legado informado"
([`transicao.md`](transicao.md); decisão 006 D-002). Se um legado for
informado, a transição deve ser desenhada sobre ele. Roteiro genérico,
Proposta, só como ponto de partida:

1. **Inventário:** dados do legado (lançamentos, saldos), formato, volume e
   quem os consome hoje.
2. **Carga inicial pela API de lançamentos:** cada lançamento do legado
   entra com a data original e uma chave de idempotência derivada do seu
   identificador no legado, o que torna a carga reexecutável sem duplicar
   (002/FR-004, 002/D-005).
3. **Reconciliação:** comparar os saldos diários do consolidado com os do
   legado, além da reconciliação livro x projeção que o coordenador já faz
   ([04 - Fluxos](arquitetura/04-fluxos.md#8-reconciliação)).
4. **Operação em paralelo:** período com os dois sistemas recebendo os
   mesmos lançamentos, até as reconciliações fecharem sem diferença.
5. **Corte:** consumidores migram para as APIs novas; o legado fica só para
   consulta até a decisão de desligá-lo.
