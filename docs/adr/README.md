# Registros de decisão arquitetural (ADRs)

Justificativa das escolhas de tecnologia e do tipo de arquitetura, exigida em
DES - Requisitos obrigatórios ("Justificativa na decisão/escolha de
ferramentas/tecnologias e de tipo de arquitetura"). Cada ADR registra o
contexto, a decisão, as alternativas consideradas e as consequências, no
formato do template `adr.md` do SDD-KIT, traduzido para o português.

Os ADRs não criam decisões novas: cada um deriva de decisões técnicas (TD)
aprovadas nas specs 001 a 005 e 007 e no steering do projeto (decisão 006
D-001). As specs ficam em `.ai/sdd/specs/`, pasta local que não é versionada
(006 D-006); por isso o texto necessário foi trazido para cá. Visão geral da
arquitetura: [`docs/arquitetura/`](../arquitetura/README.md).

## Convenções

- **Status:** `Aceito` = aprovado por Eduardo na data indicada;
  `Proposto` = aplicado no código, mas ainda pendente de confirmação de
  Eduardo.
- **Identificadores de origem:** `002 TD-001` é a decisão técnica TD-001 do
  `design.md` da spec 002; `004 D-008` é a decisão D-008 do
  `requirements.md` da spec 004; `ST-TD-001` é a decisão do steering de
  tecnologia; `P-001` é um princípio do projeto; `INFRA 2.1` e `DES` são os
  documentos-fonte na raiz
  ([`Requisitos_de_Infraestrutura.md`](../../Requisitos_de_Infraestrutura.md),
  [`Desafio_Arquiteto_Soluções.md`](../../Desafio_Arquiteto_Solu%C3%A7%C3%B5es.md)).
- **Origem de cada alternativa:** "spec" quando a alternativa e o motivo da
  rejeição constam da decisão aprovada; "análise do ADR" quando a alternativa
  foi acrescentada nesta documentação (006-T3) para explicitar o trade-off,
  sem mudar a decisão.
- **Especificado e implementado:** quando a implementação difere do texto da
  decisão, a diferença aparece em "Notas de implementação".

## Índice

| ADR | Título | Status | Origem |
|-----|--------|--------|--------|
| [ADR-001](ADR-001-servicos-independentes-bancos-exclusivos.md) | Serviços independentes com bancos exclusivos e integração assíncrona | Aceito | INFRA 2.1, 3.3; ST-TD-001; 001 TD-003 |
| [ADR-002](ADR-002-arquitetura-hexagonal.md) | Arquitetura hexagonal | Aceito | INFRA 2.1; steering de convenções |
| [ADR-003](ADR-003-outbox-transacional-e-relay.md) | Outbox transacional e relay | Aceito | INFRA 2.2, 3.1; ST-TD-002; 002 TD-001, TD-003, TD-004; 003 TD-001 a TD-004 |
| [ADR-004](ADR-004-rabbitmq-confirms-ack-apos-commit.md) | RabbitMQ com confirms, `mandatory` e ACK após commit; não é fonte de verdade | Aceito | INFRA §4; 003 TD-002; 004 TD-002 |
| [ADR-005](ADR-005-inbox-deduplicacao-quarentena.md) | Inbox, deduplicação e quarentena | Aceito | INFRA 2.4; ST-TD-003; 004 TD-001, TD-003 |
| [ADR-006](ADR-006-posicao-monotonica-e-checkpoint.md) | Posição monotônica e checkpoint | Aceito | 002 TD-002; 004 TD-006 |
| [ADR-007](ADR-007-geracoes-barreira-troca-atomica.md) | Gerações da projeção, barreira e troca atômica | Aceito | INFRA 2.5; ST-TD-004; 005 TD-001 a TD-003 |
| [ADR-008](ADR-008-keycloak-client-credentials-jwt.md) | Keycloak, Client Credentials, JWT validado em cada API, sem gateway/RBAC/ABAC | Aceito | INFRA §5; ST-TD-005; 001 TD-004, TD-005; 002 TD-005 |
| [ADR-009](ADR-009-stack-python.md) | Python 3.12, uv, FastAPI, psycopg 3, aio-pika, Alembic, pytest, k6, Ruff, mypy | Aceito | INFRA 1.3; ST-TD-007 |
| [ADR-010](ADR-010-docker-compose-local.md) | Docker Compose local; Kubernetes fora do escopo | Aceito | INFRA 1.5; ST-TD-006; 001 TD-001, TD-002, TD-006, TD-007 |
| [ADR-011](ADR-011-sem-cache-na-entrega-inicial.md) | Sem cache na entrega inicial | Aceito | 004 TD-005, TD-004, TD-007, TD-008; 002 TD-006 |
| [ADR-012](ADR-012-prometheus-grafana.md) | Prometheus + Grafana | Aceito | ST-TD-008; 007 TD-001, TD-004 |
| [ADR-013](ADR-013-escopo-lancamentos-consultar.md) | Escopo adicional `lancamentos:consultar` | Aceito | 002 D-002 |
| [ADR-014](ADR-014-audiencia-derivada-do-escopo.md) | Audiência do token derivada do escopo | Aceito | 001 TD-008; 002 DQ-003 |
| [ADR-015](ADR-015-catalogo-fechado-de-metricas.md) | Catálogo fechado de métricas e métricas do projeto fora de INFRA 8.3 | Aceito | 007 TD-002, TD-003, TD-005, TD-006 |
| [ADR-016](ADR-016-isolamento-de-rede-da-observabilidade.md) | Isolamento de rede da observabilidade e bind do `/metrics` | Aceito | 007 TD-007; 001 TD-002 |

## Rastreio: decisão técnica aprovada para ADR

Toda decisão técnica aprovada nas specs 001 a 005 e 007 e no steering aparece
em pelo menos um ADR.

| Decisão | Assunto | ADR |
|---------|---------|-----|
| ST-TD-001 | Dois serviços com bancos exclusivos e integração assíncrona | ADR-001 |
| ST-TD-002 | Outbox no banco do livro e relay dedicado | ADR-003 |
| ST-TD-003 | Inbox, deduplicação e quarentena no consolidado | ADR-005 |
| ST-TD-004 | Gerações da projeção com troca atômica | ADR-007 |
| ST-TD-005 | Keycloak + Client Credentials + JWT validado em cada API | ADR-008 |
| ST-TD-006 | Docker Compose local | ADR-010 |
| ST-TD-007 | uv, FastAPI, psycopg 3, aio-pika, Alembic, pytest, k6, Ruff, mypy | ADR-009 |
| ST-TD-008 | Prometheus + Grafana no MVP | ADR-012 |
| 001 TD-001 | Docker Compose único | ADR-010 |
| 001 TD-002 | Redes segmentadas por fronteira | ADR-010 (também ADR-001 e ADR-016) |
| 001 TD-003 | Papel de banco por identidade técnica | ADR-001 |
| 001 TD-004 | AC local com OpenSSL | ADR-008 |
| 001 TD-005 | Realm Keycloak versionado + volume | ADR-008 |
| 001 TD-006 | Interface administrativa do RabbitMQ só em loopback | ADR-010 |
| 001 TD-007 | Versões fixadas | ADR-010 (também ADR-009) |
| 001 TD-008 | Audiência do token derivada do escopo | ADR-014 |
| 002 TD-001 | Outbox transacional no banco do livro | ADR-003 |
| 002 TD-002 | Contador de posição em linha única bloqueada | ADR-006 |
| 002 TD-003 | Imutabilidade por privilégio de banco | ADR-003 (também ADR-001) |
| 002 TD-004 | Idempotência por chave do cliente e hash do corpo | ADR-003 (também ADR-005) |
| 002 TD-005 | Validação JWT local com cache de JWKS | ADR-008 |
| 002 TD-006 | Paginação por cursor na listagem de lançamentos | ADR-011 |
| 003 TD-001 | Lease com `SKIP LOCKED` | ADR-003 |
| 003 TD-002 | Publisher confirms + `mandatory` | ADR-003, ADR-004 |
| 003 TD-003 | Retentativa com espera crescente | ADR-003 |
| 003 TD-004 | Republicação por posição no lado do livro | ADR-003 (também ADR-007) |
| 004 TD-001 | Inbox + contribuição única por geração e lançamento | ADR-005 |
| 004 TD-002 | ACK após commit | ADR-004 |
| 004 TD-003 | Quarentena no banco | ADR-005 |
| 004 TD-004 | Saldo materializado por data e geração | ADR-011 |
| 004 TD-005 | Sem cache na entrega inicial | ADR-011 |
| 004 TD-006 | Checkpoint como maior posição contínua | ADR-006 |
| 004 TD-007 | Saldo acumulado calculado na consulta | ADR-011 |
| 004 TD-008 | Paginação por cursor de data | ADR-011 |
| 005 TD-001 | Worker aplica eventos a todas as gerações receptoras | ADR-007 |
| 005 TD-002 | Barreira por tabela de controle | ADR-007 |
| 005 TD-003 | Troca por ponteiro de geração ativa | ADR-007 |
| 007 TD-001 | Prometheus + Grafana | ADR-012 |
| 007 TD-002 | Pacote compartilhado de métricas | ADR-015 |
| 007 TD-003 | Rótulos sem dados sensíveis | ADR-015 |
| 007 TD-004 | Regras de alerta no Prometheus, sem notificação externa | ADR-012 |
| 007 TD-005 | Contador de requisições HTTP por status | ADR-015 |
| 007 TD-006 | Contador de falhas de publicação do relay | ADR-015 |
| 007 TD-007 | Isolamento de rede do Prometheus, do Grafana e do `/metrics` | ADR-016 |

A spec 006 tem uma decisão técnica própria, 006 TD-001 ("documentação
derivada dos artefatos SDD aprovados"). Ela trata de como esta documentação é
produzida, não da arquitetura da solução, e fica fora do escopo dos ADRs
definido em 006 D-001 (ADRs derivados das decisões das specs de produto). Ela
se reflete na forma de `docs/`: cada documento cita a spec e a decisão de
origem.
