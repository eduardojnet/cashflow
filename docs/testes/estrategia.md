# Estratégia de testes

Aqui explico como testei a solução, onde ficam os testes e como executá-los.
É a minha evidência para DES ("Testes") e para INFRA §9. Os relatórios ficam
ao lado:

- [`relatorio-carga.md`](relatorio-carga.md): teste de carga do consolidado
  (INFRA 9.4), com as contagens de cada execução;
- [`relatorio-falhas.md`](relatorio-falhas.md): um cenário por item de
  INFRA 9.6;
- [`carga-consolidado.md`](carga-consolidado.md): relatório bruto da
  primeira execução de carga (004-T6), com protocolo e recursos da máquina.

Mantenho as specs em `.ai/sdd/specs/`, pasta local que não versiono
(decisão 006 D-006).

Fontes:

- DES = [`Desafio_Arquiteto_Soluções.md`](../../Desafio_Arquiteto_Solu%C3%A7%C3%B5es.md)
- INFRA = [`Requisitos_de_Infraestrutura.md`](../../Requisitos_de_Infraestrutura.md)

## Princípios

- **Dependências reais nos testes de integração** (INFRA 9.1 a 9.3): uso
  PostgreSQL, RabbitMQ e Keycloak de verdade, em contêineres descartáveis,
  com os papéis, privilégios, migrações e a topologia do ambiente. Fiz assim
  porque as regras que o banco garante por privilégio (livro imutável, papéis
  por serviço) só se provam contra o banco real.
- **Falha injetada de fora** (INFRA 9.5; ST-Q-010): injeto `stop`, `kill`,
  `pause`, `rm` e remoção de volume pelo Docker, sobre o `infra/compose.yaml`
  real, para que a falha seja a mesma que aconteceria em operação, e não uma
  simulação dentro do código.
- **Isolamento do ambiente:** os testes de integração e e2e sobem projetos e
  contêineres próprios e os removem ao fim; o ambiente `cashflow` não recebe
  lançamentos de teste, exceto a massa idempotente do teste de carga. Fiz
  assim porque o livro é imutável: um lançamento de teste nele não sai mais.
- **Pulo explícito:** sem Docker ou sem os segredos de `infra/.env`, o módulo
  é pulado com o motivo, nunca aprovado em silêncio, para que um teste que
  não rodou nunca conte como aprovado.

## Níveis

Contagens da execução de 2026-11-09 (ver [última execução completa](#última-execução-da-suíte)).
Em 2026-11-10 os testes de unidade e de pacotes passaram a 605, com os que
acrescentei no lote de correção (ver [execução de 2026-11-10](#execução-de-2026-11-10-depois-das-correções)).

| Nível | Onde | O que prova | Dependências | Testes |
|---|---|---|---|---|
| Unidade | `services/*/tests/unit`, `packages/*/tests` | Regras de domínio e de aplicação, validação de JWT, catálogo de métricas, política de retentativa | Nenhuma (sem rede, sem Docker) | 592 |
| Integração | `services/*/tests/integracao` | Adaptadores contra a dependência real: livro imutável, outbox, inbox e deduplicação, ACK depois do commit, quarentena, checkpoint e lacunas, troca de geração, API HTTP com JWT do Keycloak | PostgreSQL e RabbitMQ descartáveis, com a imagem, os papéis, as migrações e a topologia do compose (INFRA 9.1, 9.2); Keycloak do ambiente (INFRA 9.3) | 289 |
| Infraestrutura | `tests/infra` | Certificados e TLS, isolamento de rede, papéis de banco com privilégio mínimo, permissões do RabbitMQ, emissão de token, segredos fora do Git | Ambiente `cashflow` no ar, exceto certificados (OpenSSL) e segredos (Git); Keycloak e RabbitMQ conferidos de dentro da rede do compose | 151 |
| Observabilidade | `tests/observabilidade` | Alvos de coleta, cobertura de INFRA 8.3, painel do Grafana com dados, regras de alerta (com quarentena injetada) | Ambiente `cashflow` no ar; projeto `cashflow-alertas` próprio | 102 |
| Ponta a ponta e falha | `tests/e2e` | Os cenários de INFRA 9.6 sobre o compose real ([`relatorio-falhas.md`](relatorio-falhas.md)) | Projetos compose próprios, removidos ao fim | 15 |
| Carga | `tests/carga/consolidado.js` | 50 consultas/s por 300 s, 15.000 consultas, até 5% de perda (INFRA 9.4; [`relatorio-carga.md`](relatorio-carga.md)) | k6 no host; ambiente `cashflow` no ar | 1 cenário |

Total pytest: 1.149 testes.

## Como executar

Pré-requisitos: Docker com Compose, `uv`, `infra/.env` preenchido e os
certificados gerados (001-T3). A partir da raiz do repositório:

```bash
# Suíte completa (unidade, integração, infra, observabilidade e e2e)
uv run --env-file infra/.env pytest

# Só os testes de unidade de um serviço (sem Docker)
uv run pytest services/consolidado/tests/unit

# Testes de falha (INFRA 9.6)
uv run --env-file infra/.env pytest tests/e2e

# Teste de carga (INFRA 9.4), com o ambiente `cashflow` no ar
SSL_CERT_FILE=infra/tls/gerados/ac/ac-cashflow.crt \
  uv run --env-file infra/.env -- k6 run tests/carga/consolidado.js
```

Os testes de `tests/infra` e `tests/observabilidade` que conferem o ambiente
em execução exigem o projeto `cashflow` no ar (`docker compose -f
infra/compose.yaml up -d --wait`); sem ele, são pulados com o motivo.

Alguns módulos leem variáveis próprias e são pulados pelo comando acima, com
o motivo impresso, porque precisam rodar do host ou de dentro de uma rede do
compose. Cada um traz o comando na docstring:

| Módulo | Variável | Como executar |
|---|---|---|
| `services/consolidado/tests/integracao/test_api_consolidado.py`, `services/lancamentos/tests/integracao/test_api_http_livro.py`, `test_listagem_livro.py`, `test_validacao_jwt_keycloak.py` | `KEYCLOAK_URL` | No host, com o ambiente no ar: `KEYCLOAK_URL=https://localhost:8443 uv run --env-file infra/.env pytest <módulo>` |
| `tests/infra/test_keycloak.py` | `KEYCLOAK_URL` | Dentro da rede `cashflow_rede-identidade`, por contêiner `uv` avulso (comando na docstring) |
| `tests/infra/test_rabbitmq_permissoes.py` | `RABBITMQ_HOST` | Dentro da rede `cashflow_rede-mensageria`, por contêiner `uv` avulso (comando na docstring); o RabbitMQ não publica porta no host |

## Última execução da suíte

Rodei a suíte completa em 2026-11-09, sobre o commit `33feead`, na máquina
descrita em
[`relatorio-carga.md`](relatorio-carga.md#2026-11-09-006-t6), com o ambiente
`cashflow` no ar.

Suíte completa, das 13:55:38 às 14:23:01 (-03:00):

```bash
uv run --env-file infra/.env pytest -p no:cacheprovider -rs --durations=0 \
  --junitxml=<arquivo>.xml
```

Código de saída 0: **1.094 aprovados, 55 pulados, 0 falhas, 0 erros**, em
1.640,5 s (27 min 20 s). Copiei sem alteração o relatório JUnit dessa
execução: [`resultados/suite-2026-11-09.xml`](resultados/suite-2026-11-09.xml).

| Nível | Aprovados | Pulados | Tempo (s) |
|---|---|---|---|
| Unidade (`packages/observabilidade`) | 17 | 0 | 1,0 |
| Unidade (`packages/seguranca`) | 30 | 0 | 0,3 |
| Unidade (`services/consolidado`) | 222 | 0 | 0,7 |
| Unidade (`services/coordenador`) | 132 | 0 | 0,1 |
| Unidade (`services/lancamentos`) | 153 | 0 | 0,6 |
| Unidade (`services/relay`) | 38 | 0 | 0,1 |
| Integração (`services/consolidado`) | 93 | 14 | 59,4 |
| Integração (`services/coordenador`) | 88 | 0 | 34,3 |
| Integração (`services/lancamentos`) | 44 | 22 | 13,1 |
| Integração (`services/relay`) | 28 | 0 | 28,5 |
| Infraestrutura (`tests/infra`) | 132 | 19 | 71,4 |
| Observabilidade (`tests/observabilidade`) | 102 | 0 | 150,0 |
| Ponta a ponta e falha (`tests/e2e`) | 15 | 0 | 1.278,4 |
| **Total** | **1.094** | **55** | |

Os 55 pulados são exatamente os módulos da tabela de variáveis acima, todos
com o motivo `KEYCLOAK_URL ausente` ou `RABBITMQ_HOST ausente`. Executei-os
em seguida, cada um pelo seu comando, e todos passaram:

| Módulos | Resultado |
|---|---|
| `test_api_consolidado.py`, `test_api_http_livro.py`, `test_listagem_livro.py` | 29 passed in 52,51 s, código 0 |
| `test_validacao_jwt_keycloak.py` | 7 passed in 42,63 s, código 0 |
| `tests/infra/test_keycloak.py` (rede de identidade) | 12 passed in 4,14 s, código 0 |
| `tests/infra/test_rabbitmq_permissoes.py` (rede de mensageria) | 7 passed in 1,32 s, código 0 |

Com isso, os 1.149 testes pytest passaram nesta data (1.094 + 29 + 7 + 12 +
7). O teste de carga, executado antes da suíte, também passou
([`relatorio-carga.md`](relatorio-carga.md)).

Lint e tipos (Ruff e mypy) não fizeram parte desta execução.

## Execução de 2026-11-10, depois das correções

Depois de corrigir os achados de severidade média das revisões
([lacunas e correções](../evolucoes.md#lacunas-conhecidas-da-implementação)),
não repeti a suíte completa. Rodei só as partes que as correções tocam,
sobre o código do commit `5af2f87` (os commits seguintes mudam só
documentação), com as imagens reconstruídas. Registro aqui o que rodei, para
não apresentar esta bateria como a suíte completa:

| Etapa | Comando | Resultado |
|---|---|---|
| Lint e tipos | `uv run ruff check . && uv run ruff format --check . && uv run mypy` | Sem erro; mypy em 177 arquivos |
| Serviços e pacotes (unidade e integração) e certificados | `KEYCLOAK_URL=https://localhost:8443 uv run --env-file infra/.env pytest services packages tests/infra/test_certificados.py` | 909 passed em 207,7 s |
| Observabilidade | `uv run --env-file infra/.env pytest tests/observabilidade` | 103 passed em 150,9 s |
| Queda e perda do RabbitMQ | `uv run --env-file infra/.env pytest tests/e2e/test_queda_rabbitmq.py` | 3 passed em 143,8 s |
| Reconstrução, troca e rollback | `uv run --env-file infra/.env pytest tests/e2e/test_reconstrucao.py` | 4 passed em 442,0 s |
| Falhas do consolidado | `uv run --env-file infra/.env pytest tests/e2e/test_falhas_consolidado.py` | 7 passed em 374,0 s |

Ficaram fora desta bateria `tests/infra` (exceto certificados), o e2e de
independência dos lançamentos e o teste de carga. A única mudança no
caminho deles foi a medição de pool da API de lançamentos, coberta pelos
testes de serviços e de observabilidade acima; não reexecutei esses três. Desde essa data, Ruff e mypy estão fixados no grupo
`dev` e rodam sobre produção e testes, sem script único de verificação
([ADR-009](../adr/ADR-009-stack-python.md)):

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
```
