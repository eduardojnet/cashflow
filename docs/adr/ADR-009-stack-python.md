# ADR-009: Python 3.12, uv, FastAPI, psycopg 3, aio-pika, Alembic, pytest, k6, Ruff, mypy

> Status: Aceito
> Data: 2026-11-05
> Relacionado: INFRA 1.3, 1.6, 3.3, 9.1 a 9.4; ST-TD-007; ST-Q-001 a ST-Q-011; 001 TD-007

## Contexto

DES permite a linguagem que o autor domina. INFRA 1.3 fixa Python 3.12, e
INFRA 1.6 deixa o gerenciador de dependências "a definir". A solução tem
duas APIs HTTP, dois processos de longa duração que falam com RabbitMQ e
PostgreSQL (relay e worker) e CLIs operacionais. Preciso de SQL explícito
para bloqueio de linha, `SKIP LOCKED`, privilégios por coluna e migrações
separadas por banco (INFRA 3.3), de testes contra infraestrutura real (INFRA
9.1 a 9.3) e de um gerador de carga com chegada fixa (INFRA 9.4). Fiz as
escolhas de ferramenta em 2026-11-05 (ST-Q-001 a ST-Q-011).

## Decisão

| Necessidade | Escolhi | Por quê / origem |
|-------------|---------|------------------|
| Linguagem | Python 3.12 | INFRA 1.3 fixa a versão |
| Dependências e workspace | uv, um workspace na raiz com lock único (`uv.lock`) e grupos por serviço | Um lock para todos os serviços e instalação por grupo na imagem (ST-Q-001) |
| HTTP | FastAPI com uvicorn | Assíncrono, valida a entrada por tipos e não impõe ORM (ST-Q-002) |
| PostgreSQL | psycopg 3 em modo assíncrono, com `psycopg-pool` por serviço | Modo assíncrono nativo, pool oficial e SQL explícito (ST-Q-003) |
| RabbitMQ | aio-pika (assíncrono) | Cliente assíncrono no mesmo laço do resto (ST-Q-004) |
| Migrações | Alembic, uma árvore por banco, com SQL explícito | Uma árvore por banco atende INFRA 3.3 (ST-Q-005) |
| Testes | pytest contra PostgreSQL, RabbitMQ e Keycloak reais do compose | INFRA 9.1 a 9.3 pedem infraestrutura real (ST-Q-006) |
| Carga | k6 com executor `constant-arrival-rate` | Chegada fixa nativa, que é o protocolo de INFRA 9.4 (ST-Q-007) |
| Qualidade | Ruff (lint e formatação) e mypy (tipos) | ST-Q-011 |
| JWT | PyJWT com `cryptography` (RS256), dentro de `packages/seguranca`, com cache de JWKS próprio | Implementação de 002 TD-005; pacote compartilhado pelas duas APIs (004 DQ-002) |
| Métricas | `prometheus-client`, dentro de `packages/observabilidade` | 007 TD-002 |

Mantive o modo assíncrono de ponta a ponta: FastAPI, psycopg 3 e aio-pika
compartilham o mesmo laço de eventos, sem threads para I/O.

## Alternativas consideradas

### Alternativa 1: Poetry ou pip com pip-tools

- **Origem:** análise do ADR (INFRA 1.6 deixava a escolha aberta).
- **Descrição:** gerenciador de dependências tradicional do ecossistema.
- **Prós:** maduros e conhecidos.
- **Contras:** sem workspace com lock único para vários pacotes (Poetry) ou
  com mais passos manuais (pip-tools); instalação mais lenta nas imagens.
- **Por que rejeitei:** escolhi o uv (ST-Q-001), que resolve workspace, lock
  e instalação por grupo na imagem (`uv sync --only-group <serviço>`).

### Alternativa 2: Django ou Flask

- **Origem:** análise do ADR.
- **Descrição:** framework web síncrono, com ORM (Django) ou mínimo (Flask).
- **Prós:** ecossistema amplo; Django traz admin e ORM.
- **Contras:** síncronos por padrão; o ORM do Django se choca com o SQL
  explícito e com o domínio sem dependências
  ([ADR-002](ADR-002-arquitetura-hexagonal.md)).
- **Por que rejeitei:** FastAPI é assíncrono, valida a entrada por tipos e
  não impõe ORM (ST-Q-002).

### Alternativa 3: SQLAlchemy ORM ou asyncpg

- **Origem:** análise do ADR.
- **Descrição:** ORM completo, ou driver assíncrono de baixo nível.
- **Prós:** ORM reduz código de mapeamento; asyncpg tem alto desempenho.
- **Contras:** ORM esconde o SQL de bloqueio e privilégio; asyncpg tem API
  própria, fora do padrão DB-API.
- **Por que rejeitei:** psycopg 3 tem modo assíncrono nativo, pool oficial e
  SQL explícito (ST-Q-003). Uso o Alembic só como executor de migrações em
  SQL, sem modelos ORM.

### Alternativa 4: Locust ou JMeter para carga

- **Origem:** análise do ADR.
- **Descrição:** geradores de carga em Python (Locust) ou Java (JMeter).
- **Prós:** Locust na mesma linguagem do projeto.
- **Contras:** o modelo padrão do Locust é de usuários concorrentes, não de
  taxa de chegada fixa; JMeter exige JVM e planos em XML.
- **Por que rejeitei:** k6 tem executor de chegada fixa nativo, que é o
  protocolo de INFRA 9.4, e limiar com código de saída (ST-Q-007).

## Consequências

### Positivas

- Um lock único reproduz as mesmas versões em testes e imagens.
- O teste de carga segue exatamente o protocolo de INFRA 9.4: 15.000 de
  15.000 consultas com sucesso na execução de 2026-11-06
  ([relatório](../testes/carga-consolidado.md)).
- O mypy verifica os tipos, e o FastAPI valida os contratos HTTP por tipos.

### Negativas / trade-offs

- Python tem vazão por processo menor que linguagens compiladas; cada API
  roda um processo uvicorn. Atendi a meta de 50 req/s assim, sem medir o
  limite superior.
- Código assíncrono exige cuidado com chamadas bloqueantes no laço.
- Escrevo à mão o mapeamento entre SQL e domínio.

### Riscos

- Biblioteca abandonada ou incompatível com Python 3.12. Mitiguei com as
  versões fixadas no `uv.lock`; trato atualização como item próprio, com
  verificação.

## Notas de implementação

- Versões resolvidas no `uv.lock` na data desta documentação: FastAPI
  0.162.0, uvicorn 0.54.0, psycopg 3.3.8, psycopg-pool 3.3.3, aio-pika
  10.2.2, Alembic 1.20.1, PyJWT 2.15.1, prometheus-client 0.26.0, pytest
  9.2.0. Ferramentas da máquina que usei nos testes: uv 0.13.7 e k6 v2.3.0
  ([relatório de carga](../testes/carga-consolidado.md#recursos-da-máquina)).
- Implementado: fixei Ruff 0.17.4 e mypy 2.4.1 no grupo `dev` e no `uv.lock`,
  com `[tool.ruff]` (`required-version`) e `[tool.mypy]` (`strict`, produção
  e testes) no `pyproject.toml` da raiz. Até 2026-11-10 eles rodavam como
  ferramentas avulsas (`uvx ruff`), e o mesmo commit passava ou falhava
  conforme a versão do dia (review da spec 001); por isso os fixei.
  Especificado e não implementado: o script único de verificação previsto no
  steering (`scripts/verificar.sh`); lint, tipos e testes rodam por comandos
  separados.
- As versões das imagens de contêiner seguem 001 TD-007
  ([ADR-010](ADR-010-docker-compose-local.md)).
- Revejo esta decisão se surgir meta de vazão que um processo Python por API
  não atenda, ou necessidade de mais de um processo por contêiner.

## Referências

- [`pyproject.toml`](../../pyproject.toml)
- [03 - Componentes](../arquitetura/03-componentes.md)
