# ADR-009: Python 3.12, uv, FastAPI, psycopg 3, aio-pika, Alembic, pytest, k6, Ruff, mypy

> Status: Aceito
> Data: 2026-11-05
> Relacionado: INFRA 1.3, 1.6, 3.3, 9.1 a 9.4; ST-TD-007; ST-Q-001 a ST-Q-011; 001 TD-007

## Contexto

DES permite a linguagem que o autor domina. INFRA 1.3 fixa Python 3.12 e
INFRA 1.6 deixa o gerenciador de dependências "a definir". A solução tem duas
APIs HTTP, dois processos de longa duração que falam com RabbitMQ e
PostgreSQL (relay e worker) e CLIs operacionais. Precisa de SQL explícito
para bloqueio de linha, `SKIP LOCKED`, privilégios por coluna e migrações
separadas por banco (INFRA 3.3), de testes contra infraestrutura real (INFRA
9.1 a 9.3) e de um gerador de carga com chegada fixa (INFRA 9.4). As escolhas
de ferramenta foram feitas por Eduardo em 2026-11-05 (ST-Q-001 a ST-Q-011).

## Decisão

| Necessidade | Escolha | Origem |
|-------------|---------|--------|
| Linguagem | Python 3.12 | INFRA 1.3 |
| Dependências e workspace | uv, um workspace na raiz com lock único (`uv.lock`) e grupos por serviço | ST-Q-001 |
| HTTP | FastAPI com uvicorn | ST-Q-002 |
| PostgreSQL | psycopg 3 em modo assíncrono, com `psycopg-pool` por serviço | ST-Q-003 |
| RabbitMQ | aio-pika (assíncrono) | ST-Q-004 |
| Migrações | Alembic, uma árvore por banco, com SQL explícito | ST-Q-005; INFRA 3.3 |
| Testes | pytest contra PostgreSQL, RabbitMQ e Keycloak reais do compose | ST-Q-006; INFRA 9.1 a 9.3 |
| Carga | k6 com executor `constant-arrival-rate` | ST-Q-007; INFRA 9.4 |
| Qualidade | Ruff (lint e formatação) e mypy (tipos) | ST-Q-011 |
| JWT | PyJWT com `cryptography` (RS256), dentro de `packages/seguranca`, com cache de JWKS próprio | Implementação de 002 TD-005; pacote: 004 DQ-002 |
| Métricas | `prometheus-client`, dentro de `packages/observabilidade` | 007 TD-002 |

Modo assíncrono de ponta a ponta: FastAPI, psycopg 3 e aio-pika compartilham
o mesmo laço de eventos, sem threads para I/O.

## Alternativas consideradas

### Alternativa 1: Poetry ou pip com pip-tools

- **Origem:** análise do ADR (INFRA 1.6 deixava a escolha aberta).
- **Descrição:** gerenciador de dependências tradicional do ecossistema.
- **Prós:** maduros e conhecidos.
- **Contras:** sem workspace com lock único para vários pacotes (Poetry) ou
  com mais passos manuais (pip-tools); instalação mais lenta nas imagens.
- **Por que foi rejeitada:** escolha de Eduardo por uv (ST-Q-001), que
  resolve workspace, lock e instalação por grupo na imagem
  (`uv sync --only-group <serviço>`).

### Alternativa 2: Django ou Flask

- **Origem:** análise do ADR.
- **Descrição:** framework web síncrono, com ORM (Django) ou mínimo (Flask).
- **Prós:** ecossistema amplo; Django traz admin e ORM.
- **Contras:** síncronos por padrão; o ORM do Django se choca com o SQL
  explícito e com o domínio sem dependências
  ([ADR-002](ADR-002-arquitetura-hexagonal.md)).
- **Por que foi rejeitada:** FastAPI é assíncrono, valida a entrada por
  tipos e não impõe ORM (ST-Q-002).

### Alternativa 3: SQLAlchemy ORM ou asyncpg

- **Origem:** análise do ADR.
- **Descrição:** ORM completo, ou driver assíncrono de baixo nível.
- **Prós:** ORM reduz código de mapeamento; asyncpg tem alto desempenho.
- **Contras:** ORM esconde o SQL de bloqueio e privilégio; asyncpg tem API
  própria, fora do padrão DB-API.
- **Por que foi rejeitada:** psycopg 3 tem modo assíncrono nativo, pool
  oficial e SQL explícito (ST-Q-003). Alembic é usado só como executor de
  migrações em SQL, sem modelos ORM.

### Alternativa 4: Locust ou JMeter para carga

- **Origem:** análise do ADR.
- **Descrição:** geradores de carga em Python (Locust) ou Java (JMeter).
- **Prós:** Locust na mesma linguagem do projeto.
- **Contras:** o modelo padrão do Locust é de usuários concorrentes, não de
  taxa de chegada fixa; JMeter exige JVM e planos em XML.
- **Por que foi rejeitada:** k6 tem executor de chegada fixa nativo, que é
  o protocolo de INFRA 9.4, e limiar com código de saída (ST-Q-007).

## Consequências

### Positivas

- Um lock único reproduz as mesmas versões em testes e imagens.
- O teste de carga segue exatamente o protocolo de INFRA 9.4: 15.000 de
  15.000 consultas com sucesso na execução de 2026-11-06
  ([relatório](../testes/carga-consolidado.md)).
- Tipos verificados pelo mypy e contratos HTTP validados por tipos no
  FastAPI.

### Negativas / trade-offs

- Python tem vazão por processo menor que linguagens compiladas; cada API
  roda um processo uvicorn. A meta de 50 req/s foi atendida assim, sem
  medição de limite superior.
- Código assíncrono exige cuidado com chamadas bloqueantes no laço.
- Mapeamento entre SQL e domínio escrito à mão.

### Riscos

- Biblioteca abandonada ou incompatível com Python 3.12. Mitigação: versões
  fixadas no `uv.lock`; atualização como item próprio, com verificação.

## Notas de implementação

- Versões resolvidas no `uv.lock` na data desta documentação: FastAPI
  0.162.0, uvicorn 0.54.0, psycopg 3.3.8, psycopg-pool 3.3.3, aio-pika
  10.2.2, Alembic 1.20.1, PyJWT 2.15.1, prometheus-client 0.26.0, pytest
  9.2.0. Ferramentas da máquina usadas nos testes: uv 0.13.7 e k6 v2.3.0
  ([relatório de carga](../testes/carga-consolidado.md#recursos-da-máquina)).
- Especificado e implementado: Ruff e mypy são executados como ferramentas
  (`uvx ruff`, `uv run --with mypy mypy`), sem entrar no `uv.lock`; o script
  único de verificação previsto no steering (`scripts/verificar.sh`) não foi
  criado.
- As versões das imagens de contêiner seguem 001 TD-007
  ([ADR-010](ADR-010-docker-compose-local.md)).
- Gatilho de revisão: meta de vazão que um processo Python por API não
  atenda, ou necessidade de mais de um processo por contêiner.

## Referências

- [`pyproject.toml`](../../pyproject.toml)
- [03 - Componentes](../arquitetura/03-componentes.md)
