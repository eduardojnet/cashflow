# Relatório dos testes de falha

Aqui apresento a minha evidência para DES ("Testes") e para INFRA 9.5 e 9.6:
escrevi, para cada cenário de falha de INFRA 9.6, um teste executável que
injeta a falha no ambiente real e confere o resultado. Implementei-os nas
specs 002 (002-T6), 003 (003-T5), 004 (004-T7) e 005 (005-T6). Mantenho as
specs em `.ai/sdd/specs/`, pasta local que não versiono (decisão 006 D-006).

Fontes:

- DES = [`Desafio_Arquiteto_Soluções.md`](../../Desafio_Arquiteto_Solu%C3%A7%C3%B5es.md)
- INFRA = [`Requisitos_de_Infraestrutura.md`](../../Requisitos_de_Infraestrutura.md)

Estratégia geral dos testes: [`estrategia.md`](estrategia.md). Teste de
carga: [`relatorio-carga.md`](relatorio-carga.md).

## Como a falha é injetada

INFRA 9.5 pede "instrumentação para injeção controlada de falhas". Decidi não
pôr código de injeção dentro dos serviços e provocar a falha de fora, pelo
próprio Docker, sobre o `infra/compose.yaml` real (ST-Q-010), para que o
código testado seja o mesmo que roda em operação e a falha seja a de um
processo ou contêiner de verdade. Os testes ficam em [`tests/e2e/`](../../tests/e2e/).

| Operação | Falha que simula |
|---|---|
| `docker compose stop` | Serviço parado (queda ordenada) |
| `docker compose kill` | Queda abrupta (SIGKILL), sem desligamento ordenado |
| `docker compose pause` | Processo congelado: mantém as conexões abertas, mas não lê nem escreve |
| `docker compose rm` + `docker volume rm` | Perda dos dados do RabbitMQ (INFRA 7.6) |

Fiz cada módulo subir um projeto compose próprio, com volumes próprios e
portas efêmeras em `127.0.0.1`, e removê-lo ao fim com volumes e imagens
locais. Assim o ambiente `cashflow` do dia a dia não é tocado, e o livro,
imutável por privilégio ([ADR-003](../adr/ADR-003-outbox-transacional-e-relay.md)),
não recebe lançamentos de teste que não poderiam mais ser apagados.

| Módulo | Projeto compose | Spec |
|---|---|---|
| [`test_independencia_lancamentos.py`](../../tests/e2e/test_independencia_lancamentos.py) | `cashflow-e2e` | 002-T6 |
| [`test_queda_rabbitmq.py`](../../tests/e2e/test_queda_rabbitmq.py) | `cashflow-e2e-rabbitmq` | 003-T5 |
| [`test_falhas_consolidado.py`](../../tests/e2e/test_falhas_consolidado.py) | `cashflow-e2e-consolidado` | 004-T7 |
| [`test_reconstrucao.py`](../../tests/e2e/test_reconstrucao.py) | `cashflow-e2e-reconstrucao` | 005-T6 |

## O que todo cenário confere

Depois do retorno do componente, fiz os testes de 004-T7 e 005-T6
conferirem o mesmo conjunto, para que "passou" sempre signifique nada
perdido e nada duplicado:

- **Saldo correto:** o saldo de cada data é a soma dos lançamentos
  registrados pelo próprio teste, conferido em três lugares: no livro, no
  banco do consolidado (em cada geração que recebe eventos) e pela API de
  consulta.
- **Nenhum evento descartado:** todo `evento_id` da outbox está na inbox da
  geração ativa; todo lançamento tem contribuição e marcador de inclusão; a
  quarentena está vazia; o checkpoint é a última posição sem lacunas; a fila
  termina vazia.
- **Reconciliação** (005-T6): o relatório do livro e a projeção saem
  conciliados.

## Resultado por cenário de INFRA 9.6

Execução de 2026-11-09 (detalhe em [Execução](#execução)). Os nove cenários
de INFRA 9.6 passaram. Em 2026-11-10, depois das correções, reexecutei os
módulos de RabbitMQ, falhas do consolidado e reconstrução, e todos passaram
de novo ([estratégia](estrategia.md#execução-de-2026-11-10-depois-das-correções)).

| # | Cenário de INFRA 9.6 | Teste e2e principal | Resultado |
|---|---|---|---|
| 1 | Queda do consolidado | `test_queda_do_consolidado`; `test_registro_confirma_com_consolidado_worker_e_rabbitmq_parados` | Aprovado |
| 2 | Queda do consumidor | `test_queda_do_consumidor_com_mensagens_sem_ack` | Aprovado |
| 3 | Perda de ACK | `test_perda_de_ack_depois_do_commit` | Aprovado |
| 4 | Duplicação de eventos | `test_duplicacao_de_eventos` | Aprovado |
| 5 | Indisponibilidade do RabbitMQ | `test_rabbitmq_indisponivel`; `test_backlog_zera_apos_retorno_do_rabbitmq`; `test_perda_do_volume_do_rabbitmq_e_republicacao`; `test_recuperacao_apos_perda_do_rabbitmq` | Aprovado |
| 6 | Reconstrução concorrente | `test_reconstrucao_concorrente_e_troca` | Aprovado |
| 7 | Troca de geração | `test_reconstrucao_concorrente_e_troca`; `test_troca_recusada_sem_mudar_a_ativa` | Aprovado |
| 8 | Rollback controlado | `test_rollback_controlado` | Aprovado |
| 9 | Indisponibilidade do Keycloak | `test_keycloak_indisponivel` | Aprovado |

### 1. Queda do consolidado

| Item | Conteúdo |
|---|---|
| Teste | `test_falhas_consolidado.py::test_queda_do_consolidado`; `test_independencia_lancamentos.py::test_registro_confirma_com_consolidado_worker_e_rabbitmq_parados` |
| Falha injetada | (a) `stop pg-consolidado`; (b) `stop consolidado-api worker pg-consolidado`; (c) no módulo 002-T6, `stop` de tudo fora do caminho do registro (RabbitMQ, banco, worker e API do consolidado, relay) |
| Comportamento esperado | O registro de lançamentos não depende do consolidado (DES; INFRA 2.1). Com só o banco fora, o worker sai da fila, fica não pronto e não confirma nada; os eventos esperam na fila. Com o retorno, o worker retoma sozinho |
| Conferido | (a) e (b): 15 lançamentos registrados em cada queda, 15 mensagens prontas na fila e nenhum consumidor durante a queda; em (a), ready do worker 503 e retomada sozinha com o retorno do banco; fila zerada após o retorno; saldo e inclusão conferidos. (c): 60 requisições em 6 linhas paralelas, todas 201; 60 eventos pendentes na outbox, nenhum publicado |

### 2. Queda do consumidor

| Item | Conteúdo |
|---|---|
| Teste | `test_falhas_consolidado.py::test_queda_do_consumidor_com_mensagens_sem_ack` |
| Falha injetada | `pause worker` (com `prefetch` 10), 30 lançamentos, depois `kill worker` |
| Comportamento esperado | Mensagens entregues e sem ACK voltam à fila quando a conexão cai; o novo worker as recebe como reentrega e as aplica uma vez (INFRA 4.8, 4.10) |
| Conferido | Antes do `kill`: 20 prontas e 10 não confirmadas. Depois: 30 prontas, nenhuma contribuição gravada. Após o retorno: métrica `cashflow_worker_reentregas_total` = 10 e cada evento com desfecho `aplicado` exatamente uma vez |

### 3. Perda de ACK

| Item | Conteúdo |
|---|---|
| Teste | `test_falhas_consolidado.py::test_perda_de_ack_depois_do_commit`; na integração, `services/consolidado/tests/integracao/test_worker_projecao.py::test_queda_entre_commit_e_ack_gera_reentrega_sem_duplicar_o_saldo` |
| Falha injetada | 5 mensagens entregues ao worker congelado; `pause rabbitmq`, `unpause worker` (o worker grava o efeito e envia o ACK, que o broker não lê); `kill rabbitmq` |
| Comportamento esperado | ACK só depois do commit ([ADR-004](../adr/ADR-004-rabbitmq-confirms-ack-apos-commit.md)); a mensagem com ACK perdido volta e a inbox a reconhece como duplicata ([ADR-005](../adr/ADR-005-inbox-deduplicacao-quarentena.md)) |
| Conferido | Efeito gravado e as 5 mensagens de volta à fila depois do retorno do broker; cada evento com um desfecho `aplicado` e um `evento_duplicado`; saldo sem duplicação |

### 4. Duplicação de eventos

| Item | Conteúdo |
|---|---|
| Teste | `test_falhas_consolidado.py::test_duplicacao_de_eventos`; na integração, `test_worker_projecao.py::test_mensagem_duplicada_nao_altera_o_saldo` e `test_republicacao_do_mesmo_lancamento_com_outro_evento_nao_altera_o_saldo` |
| Falha injetada | Worker parado e duas republicações completas da outbox desde a posição 1 (serviço `relay-republicar`) |
| Comportamento esperado | Pelo menos duas cópias de cada evento na fila, todas confirmadas sem efeito no saldo e sem quarentena (INFRA 2.4) |
| Conferido | Fila com pelo menos 2 cópias por lançamento registrado no módulo; todos os novos desfechos são `evento_duplicado`, um por cópia, cobrindo todos os eventos; saldo inalterado |

### 5. Indisponibilidade do RabbitMQ

| Item | Conteúdo |
|---|---|
| Teste | `test_falhas_consolidado.py::test_rabbitmq_indisponivel`; `test_queda_rabbitmq.py::test_backlog_zera_apos_retorno_do_rabbitmq` e `test_perda_do_volume_do_rabbitmq_e_republicacao`; `test_reconstrucao.py::test_recuperacao_apos_perda_do_rabbitmq` |
| Falha injetada | `stop rabbitmq`; e, para a perda de dados (INFRA 7.6), `stop` + `rm` do contêiner e `docker volume rm` do volume do broker |
| Comportamento esperado | O registro continua; os eventos ficam na outbox ([ADR-003](../adr/ADR-003-outbox-transacional-e-relay.md)); relay e worker ficam não prontos e reconectam sozinhos. O RabbitMQ não é fonte de verdade: depois da perda do volume, a republicação da outbox recupera tudo |
| Conferido | 15 lançamentos (004-T7) e 20 (003-T5) registrados com o broker parado, todos pendentes na outbox; métricas de backlog e de falha de publicação sobem e o ready do relay fica 503; com o retorno, backlog zero e cada evento na fila. Perda do volume: o broker volta vazio, só com a topologia; a republicação reabre os 30 eventos e todos chegam à fila. Em 005-T6, a reconciliação acusa as 15 posições perdidas (código 3) e, após reconstrução e troca, sai conciliada |

### 6. Reconstrução concorrente

| Item | Conteúdo |
|---|---|
| Teste | `test_reconstrucao.py::test_reconstrucao_concorrente_e_troca` |
| Falha injetada | Não é falha: operação concorrente. Lançamentos registrados sem parar e consulta contínua em segundo plano durante toda a reconstrução |
| Comportamento esperado | A candidata recebe os eventos republicados e os novos; a ativa segue atendendo consultas ([ADR-007](../adr/ADR-007-geracoes-barreira-troca-atomica.md)) |
| Conferido | Pelo menos 30 registros concorrentes no cenário e pelo menos 20 entre a criação da candidata e a troca; nenhuma consulta falhou; saldo conferido nas duas gerações; fase da candidata `concluida` |

### 7. Troca de geração

| Item | Conteúdo |
|---|---|
| Teste | `test_reconstrucao.py::test_reconstrucao_concorrente_e_troca` (troca aceita) e `test_troca_recusada_sem_mudar_a_ativa`; na integração, `services/coordenador/tests/integracao/test_troca_no_postgres.py` (falha simulada durante a troca, operação concorrente, identidade sem permissão) |
| Falha injetada | Candidata com lacunas (sem republicação); `pause worker` para que a barreira não seja confirmada (tempo limite de 3 s) |
| Comportamento esperado | Troca em uma transação, só com o critério de aceite atendido e a barreira confirmada; recusa sem mudar a ativa |
| Conferido | Troca aceita: controle com a candidata como ativa e barreira liberada. Troca recusada por lacunas e por barreira não confirmada: código 1, mensagem com o motivo, ativa e barreira inalteradas; candidata cancelada e dados removidos ao fim |

### 8. Rollback controlado

| Item | Conteúdo |
|---|---|
| Teste | `test_reconstrucao.py::test_rollback_controlado` |
| Falha injetada | Não é falha: operação de recuperação, com consulta contínua |
| Comportamento esperado | A geração anterior, que seguiu recebendo eventos, volta a ser a ativa; nova reconstrução é recusada até o descarte da anterior |
| Conferido | 10 lançamentos antes do rollback conferidos na anterior; nova reconstrução recusada (código 1); rollback sem falha de consulta; 5 lançamentos depois conferidos na reconstruída; descarte remove os dados das quatro tabelas da geração |

### 9. Indisponibilidade do Keycloak

| Item | Conteúdo |
|---|---|
| Teste | `test_falhas_consolidado.py::test_keycloak_indisponivel`; na integração, `services/lancamentos/tests/integracao/test_validacao_jwt_keycloak.py::test_keycloak_parado_token_valido_e_aceito_dentro_do_ttl` |
| Falha injetada | `stop keycloak` depois de as APIs lerem o JWKS e de os tokens serem emitidos |
| Comportamento esperado | Com o JWKS em cache (300 s), tokens já emitidos seguem aceitos nas duas APIs; a projeção não depende do Keycloak ([ADR-008](../adr/ADR-008-keycloak-client-credentials-jwt.md)) |
| Conferido | Controle positivo: nenhum token novo emitido. 10 lançamentos registrados e saldo consultado com os tokens anteriores; com o retorno, tokens novos funcionam |

## Execução

Rodei os testes de falha dentro da suíte completa de 2026-11-09, sobre o
commit `33feead`, das 13:55:38 às 14:23:01 (-03:00). Código de saída 0, sem
falha nem erro. Comando, máquina e contagens da suíte em
[`estrategia.md`](estrategia.md#última-execução-da-suíte); relatório JUnit em
[`resultados/suite-2026-11-09.xml`](resultados/suite-2026-11-09.xml).

Para rodar só os testes de falha, a partir da raiz do repositório, com
`infra/.env` preenchido e os certificados gerados:

```bash
uv run --env-file infra/.env pytest tests/e2e
```

Copiei do relatório JUnit o tempo de cada teste. O primeiro teste de cada
módulo inclui a subida do projeto compose próprio, e o último, a remoção.

| Módulo | Teste | Cenário | Resultado | Tempo (s) |
|---|---|---|---|---|
| `test_falhas_consolidado` | `test_cadeia_projeta_lancamentos_antes_das_falhas` | Controle: cadeia íntegra antes das falhas | Aprovado | 62,2 |
| `test_falhas_consolidado` | `test_queda_do_consolidado` | 1 | Aprovado | 32,7 |
| `test_falhas_consolidado` | `test_queda_do_consumidor_com_mensagens_sem_ack` | 2 | Aprovado | 15,1 |
| `test_falhas_consolidado` | `test_perda_de_ack_depois_do_commit` | 3 | Aprovado | 21,4 |
| `test_falhas_consolidado` | `test_duplicacao_de_eventos` | 4 | Aprovado | 66,8 |
| `test_falhas_consolidado` | `test_rabbitmq_indisponivel` | 5 | Aprovado | 34,9 |
| `test_falhas_consolidado` | `test_keycloak_indisponivel` | 9 | Aprovado | 75,9 |
| `test_independencia_lancamentos` | `test_registro_confirma_com_consolidado_worker_e_rabbitmq_parados` | 1 | Aprovado | 173,7 |
| `test_queda_rabbitmq` | `test_relay_fica_healthy_e_nao_alcanca_pg_consolidado` | Controle: relay pronto e isolado do consolidado | Aprovado | 138,2 |
| `test_queda_rabbitmq` | `test_backlog_zera_apos_retorno_do_rabbitmq` | 5 | Aprovado | 36,8 |
| `test_queda_rabbitmq` | `test_perda_do_volume_do_rabbitmq_e_republicacao` | 5 (INFRA 7.6) | Aprovado | 40,5 |
| `test_reconstrucao` | `test_reconstrucao_concorrente_e_troca` | 6 e 7 | Aprovado | 270,5 |
| `test_reconstrucao` | `test_rollback_controlado` | 8 | Aprovado | 91,4 |
| `test_reconstrucao` | `test_troca_recusada_sem_mudar_a_ativa` | 7 | Aprovado | 69,7 |
| `test_reconstrucao` | `test_recuperacao_apos_perda_do_rabbitmq` | 5 (INFRA 7.6) | Aprovado | 148,8 |

Total: 15 testes aprovados em 1.278,4 s. Ao fim, nenhum projeto, volume ou
imagem dos testes e2e restou na máquina, e o projeto `cashflow` seguiu com os
12 contêineres no ar.

## Limites

- Usei volumes pequenos (dezenas de lançamentos por falha). Eles provam a
  correção do protocolo (nada perdido, nada duplicado), não o comportamento
  com backlog grande.
- A queda é de processo ou contêiner na mesma máquina. Não injetei partição
  de rede, disco cheio nem relógio fora de sincronia.
- Não injetei a queda do coordenador no meio da troca: a retomada da
  barreira herdada tem teste de integração, mas o cenário inteiro é `[NVI]`
  ([lacunas](../evolucoes.md#lacunas-conhecidas-da-implementação)).
- O teste e2e cobre a indisponibilidade do Keycloak dentro do cache do JWKS
  e do prazo do token (300 s cada). Depois disso, nenhum cliente obtém token
  novo, e a API sem chave válida em cache responde 503, não 401
  ([ADR-008](../adr/ADR-008-keycloak-client-credentials-jwt.md)); não exercito
  esse caso longo no e2e. Cobri-o sem Keycloak, com JWKS em memória e relógio
  controlado, em `packages/seguranca/tests/test_validador_jwt.py`
  (`test_keycloak_parado_apos_o_ttl_sem_chave_valida_da_503`).
