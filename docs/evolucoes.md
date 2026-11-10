# Evoluções futuras

Aqui registro o que deixei fora desta entrega e como eu evoluiria a solução.
Respondo a DES - Observações ("descrições sobre o que você gostaria de ter
implementado ou evoluções futuras para o sistema proposto") e parto da lista
de INFRA - Fora do escopo atual. Mantenho as specs em `.ai/sdd/specs/`, pasta
local que não versiono (decisão 006 D-006).

Fontes:

- DES = [`Desafio_Arquiteto_Soluções.md`](../Desafio_Arquiteto_Solu%C3%A7%C3%B5es.md)
- INFRA = [`Requisitos_de_Infraestrutura.md`](../Requisitos_de_Infraestrutura.md)

Como ler:

- **Nada deste documento está implementado.** A coluna "Como evoluir" é
  Proposta: a direção que eu sugiro, não decisão aprovada nem requisito.
- **Gatilho** é o fato que justificaria a evolução. Quando o gatilho já
  consta de um ADR, cito o ADR; não começaria a evolução antes do gatilho,
  pelo princípio P-011 (não acrescentar componente sem requisito que o
  exija).
- Não proponho nenhum número novo aqui, porque metas de disponibilidade,
  RPO, RTO, retenção e escala dependem de decisão de negócio (INFRA 7.7).

## Fora do escopo de INFRA

Percorro os 12 itens de INFRA - Fora do escopo atual, na ordem da fonte.

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

São itens que deixei fora desta entrega por decisão registrada ou por
gatilho de ADR.

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
| Capacidade validada | Verificada numa máquina de desenvolvimento (12 CPUs lógicas, 16 GB de RAM, 23,7 GiB livres; [recursos](testes/carga-consolidado.md#recursos-da-máquina)), sem mínimo medido nem ambiente dedicado | Escolha do ambiente de produção | Teste de carga e de falha no ambiente-alvo, registrando os recursos medidos |

### Observabilidade

| Evolução | Situação atual | Gatilho | Como evoluir (Proposta) |
|----------|----------------|---------|-------------------------|
| Canal de notificação dos alertas | Alertas avaliados pelo Prometheus, sem envio ([ADR-012](adr/ADR-012-prometheus-grafana.md)) | Definição de canal pelo negócio (gatilho do ADR-012) | Alertmanager com o canal escolhido |
| Rastreamento distribuído (tracing) | Ausente; logs correlacionados ([`observabilidade.md`](observabilidade.md#logs-estruturados)) | Requisito de tracing (gatilho do ADR-012) | Instrumentação das APIs, relay e worker, propagando o identificador do evento já usado nos logs |

## Lacunas conhecidas da implementação

Aqui está o que especifiquei e não entreguei, ou que as revisões de cada
spec apontaram e segue aberto. Registro tudo para não apresentar como
implementado o que não está. Revisei cada spec contra requisitos, desenho e
código, com os testes executados de novo, em 2026-11-09; as sete terminaram
"aprovada com pendências". Corrigi os achados de severidade média em
2026-11-10 (lista no fim desta seção). Os que deixei abertos são todos de
baixa severidade, e para cada um digo o efeito e o que caberia fazer.

| Lacuna aberta | Efeito | Origem |
|---------------|--------|--------|
| Script único de verificação (`scripts/verificar.sh`), previsto no steering, não existe | Lint, tipos e testes rodam por comandos separados | [ADR-009](adr/ADR-009-stack-python.md); revisões 002 e 007 |
| Sem limite de tamanho do corpo da requisição nem de `descricao` no registro de lançamento | Um cliente com token válido pode enviar corpos de vários megabytes, lidos em memória e gravados para sempre no livro imutável; a autenticação vem antes da leitura do corpo. O tamanho máximo é decisão de negócio pendente; caberia um limite no adaptador HTTP sem mudar o contrato | Revisão 002 |
| Falha do banco que se repete a cada tentativa de aplicar uma mensagem (por exemplo, `posicao` acima do teto de `bigint`, que o domínio não valida) devolve a mensagem à fila sem fim | A projeção das mensagens seguintes para, sem perda; aparece nas reentregas e no checkpoint parado. Caberia validar o teto no domínio e mandar à quarentena. `[NVI]`: por leitura do código, não reproduzido | Revisão 004 |
| Reentrega de mensagem já quarentenada (ACK perdido depois do commit) grava outra linha na quarentena | A contagem e a triagem da quarentena ficam infladas; nada se perde e o saldo não muda. Caberia deduplicar pelo hash do conteúdo. `[NVI]`: por leitura do código | Revisão 004 |
| A idade do evento mais antigo é medida só na outbox, não na fila do consolidado | O atraso da fila aparece em mensagens prontas e em posições (checkpoint), não em segundos | Revisão 004 |
| Barreira deixada por um coordenador que caiu no meio da troca ou do rollback não tem alerta | O consumo fica pausado até a operação ser repetida; a conduta está no [runbook](../README.md#runbook-reconstrução-do-consolidado). `[NVI]`: a queda no meio do protocolo não foi reproduzida; a retomada da barreira herdada tem teste | Revisão 005 |
| O espaço em disco com duas gerações não foi medido, e o descarte (DELETE) não devolve espaço ao sistema de arquivos sem VACUUM | Com o volume do desafio o risco é desprezível; em volume real, a folga de disco para a reconstrução não está conferida (INFRA 7.5) | Revisão 005 |
| Os alertas dependem do próprio produtor da métrica (sem regra para alvo fora do ar), e o de quarentena se apaga com o reinício do worker | Com o relay ou o worker parado, a série some e o alerta não dispara; o alvo fora do ar aparece só no painel. A quarentena segue na tabela depois do reinício. `[NVI]`: análise das expressões | Revisão 007 |
| Publicação das APIs só em loopback é conferida à mão; o teste automatizado cobre só o Keycloak | Uma mudança de porta no compose pode expor uma API sem teste que falhe | Revisão 001 |
| O health check do PostgreSQL (`pg_isready` sem `-h`) pode dar saudável durante a inicialização do volume, pelo socket do servidor temporário | Na criação do volume, um dependente que usa TCP pode receber recusa de conexão. As APIs se recuperam pela readiness; os serviços de migração não têm retentativa. Não reproduzido | Revisão 001 |

O que corrigi em 2026-11-10, depois das revisões (severidade média):

| Achado | Correção |
|--------|----------|
| Lint não reproduzível: Ruff sem versão fixada (9 erros e 8 arquivos a formatar com a versão do dia) e mypy falhando nos testes, que importavam auxiliares por `sys.path` | Fixei Ruff e mypy no grupo `dev`, com `[tool.ruff]` e `[tool.mypy]`; os testes passaram a importar pelo caminho do pacote; `uv run ruff check .` e `uv run mypy` saem sem erro ([ADR-009](adr/ADR-009-stack-python.md)) |
| O relay seguia publicando o lote depois de vencer o próprio lease, e duas execuções podiam publicar o mesmo evento ao mesmo tempo | Fiz o ciclo conferir, antes de cada evento e pelo relógio monotônico, se o lease comporta uma publicação inteira com margem; a publicação em curso não passa do lease; lease menor que o prazo de confirmação impede a subida ([ADR-003](adr/ADR-003-outbox-transacional-e-relay.md)) |
| O relay não expunha `cashflow_pool_conexoes` | Registrei no relay, pela mesma função das APIs e do worker ([`observabilidade.md`](observabilidade.md#métricas-de-infra-83)) |
| `cashflow_geracao_ativa` valia 0 depois de cada subida até a primeira consulta | Fiz a API ler `controle_projecao` a cada 10 s; o teste de cobertura exige o valor igual ao do banco ([`observabilidade.md`](observabilidade.md#métricas-de-infra-83)) |

## Transição de um legado

Considerei a arquitetura de transição não aplicável, porque nenhum legado
foi informado ([`transicao.md`](transicao.md); decisão 006 D-002). Se um
legado for informado, desenharia a transição sobre ele. Deixo um roteiro
genérico, Proposta, só como ponto de partida:

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
