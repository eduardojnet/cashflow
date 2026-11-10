# Relatório do teste de carga

Aqui apresento a minha evidência para o requisito não funcional de DES sobre
o consolidado ("em dias de pico, recebe 50 requisições por segundo, com no
máximo 5% de perda") e para o protocolo de INFRA 9.4. Implementei o teste na
spec 004 (004-T6, NFR-001). Mantenho as specs em `.ai/sdd/specs/`, pasta
local que não versiono (decisão 006 D-006).

Fontes:

- DES = [`Desafio_Arquiteto_Soluções.md`](../../Desafio_Arquiteto_Solu%C3%A7%C3%B5es.md)
- INFRA = [`Requisitos_de_Infraestrutura.md`](../../Requisitos_de_Infraestrutura.md)

Protocolo completo, massa de dados e recursos da máquina da primeira
execução: [`carga-consolidado.md`](carga-consolidado.md). Script:
[`tests/carga/consolidado.js`](../../tests/carga/consolidado.js).

## Resumo

| Item | Valor |
|---|---|
| Meta | pelo menos 14.250 de 15.000 consultas com sucesso (até 5% de perda) |
| Execuções | 2 (2026-11-06 e 2026-11-09) |
| Resultado | **APROVADO nas duas**: 15.000 de 15.000 com sucesso, perda 0 |
| Pior latência observada | 68,82 ms (limite por consulta: 10 s) |
| Cache | nenhum ([ADR-011](../adr/ADR-011-sem-cache-na-entrega-inicial.md)) |

## Protocolo

Fonte: INFRA 9.4; 004 NFR-001. Usei chegada fixa, e não usuários
concorrentes, porque é o que INFRA 9.4 descreve: com chegada fixa, uma API
lenta acumula consultas em atraso em vez de reduzir a taxa enviada. Detalhe
em [`carga-consolidado.md`](carga-consolidado.md#protocolo).

| Item | Valor |
|---|---|
| Gerador | k6, executor `constant-arrival-rate` (chegada fixa) |
| Taxa e duração | 50 consultas/s por 300 s |
| Total programado | 15.000 consultas |
| Observação individual | até 10 s por consulta (`timeout` da requisição) |
| Sucesso | status 200, corpo com a data pedida e resposta em até 10 s |
| Perda | consulta programada sem sucesso: erro, timeout, token não obtido ou iteração descartada pelo gerador |
| Cenário | `GET /v1/consolidado/saldos/{data}`, 30 datas existentes em rodízio |
| Autenticação e TLS | token de `cliente-consultas` do Keycloak, renovado por VU; TLS validado só com a AC local |

Comando, a partir da raiz do repositório, com o ambiente `cashflow` no ar:

```bash
SSL_CERT_FILE=infra/tls/gerados/ac/ac-cashflow.crt \
  uv run --env-file infra/.env -- k6 run tests/carga/consolidado.js
```

Com `RESUMO_JSON=<arquivo>`, o script grava também o resumo em JSON. Pus o
limiar `consultas_sucesso >= 14250` no próprio script, para que o k6 termine
com código 0 quando a meta é atendida e com 99 quando não é, sem leitura
manual do resultado.

## Contagens

Copiei os valores do resumo impresso pelo próprio script (`handleSummary`).

| Contagem | 2026-11-06 | 2026-11-09 |
|---|---|---|
| Programadas | 15.000 | 15.000 |
| Enviadas | 15.000 | 15.000 |
| Sucesso | 15.000 | 15.000 |
| Perda | 0 (0%) | 0 (0%) |
| Não enviadas por falta de token | 0 | 0 |
| Descartadas pelo gerador | 0 | 0 |
| VUs máximos alocados | 50 | 50 |
| Código de saída do k6 | 0 | 0 |
| Veredito | APROVADO | APROVADO |

## Latências

Duração de cada consulta em milissegundos, medida pelo k6.

| Execução | mín | média | mediana | p90 | p95 | p99 | p99,9 | máx |
|---|---|---|---|---|---|---|---|---|
| 2026-11-06 | 1,79 | 5,00 | 5,37 | 6,20 | 6,47 | 8,05 | 9,26 | 25,31 |
| 2026-11-09 | 1,88 | 8,80 | 8,92 | 12,12 | 13,49 | 18,83 | 36,58 | 68,82 |

## Execuções

### 2026-11-06 (004-T6)

Encerrei às 16:27:25 (-03:00), sobre o commit `b2848ee` mais o script. O
registro completo, com corroboração pelo Prometheus, uso dos contêineres e
recursos da máquina, está em [`carga-consolidado.md`](carga-consolidado.md#resultado).

### 2026-11-09 (006-T6)

| Item | Valor |
|---|---|
| Início e fim | 13:50:33 a 13:55:34 (-03:00), setup incluído |
| Commit | `33feead`, que inclui a spec 005 (controle de gerações), posterior à primeira execução |
| Resumo bruto | [`resultados/carga-2026-11-09.json`](resultados/carga-2026-11-09.json) |
| Máquina | a mesma da primeira execução (Intel Core i7-1355U, 12 CPUs lógicas, 16 GB de RAM; Linux Mint 22.3, kernel 6.8.0-146-generic) |
| Memória disponível no início | 3.723.668 kB |
| Disco livre | 25 GiB de 468 GiB |
| Ferramentas | Docker Engine 29.9.2, Docker Compose 5.6.1, k6 v2.3.0, uv 0.13.7 |
| Outros contêineres na máquina | 3, sem relação com o projeto |

Corroborei pelo Prometheus, consultado logo depois na janela da carga, para
não depender só da contagem do gerador:
`sum by (status) (increase(cashflow_http_requisicoes_total{componente="consolidado-api"}[6m] @ <13:56:00>))`
retornou cerca de 15.745 respostas 200, 0 respostas 503 e 0 respostas 401. A
mesma consulta nos 6 minutos anteriores, sem carga, retornou cerca de 32
respostas 200. O valor inclui as consultas do setup e esse tráfego de fundo, e
`increase` extrapola nas bordas: serve de conferência de ordem de grandeza,
não de contagem exata.

Amostrei o uso com `docker stats --no-stream`, 14 amostras durante a carga:

| Contêiner | CPU | Memória |
|---|---|---|
| consolidado-api | 23% a 141% (mediana 42%) | 51 a 81 MiB |
| pg-consolidado | 2% a 11% | 19 a 24 MiB |
| keycloak | menos de 1%, com um pico de 12% | 496 a 507 MiB |

Valor acima de 100% significa mais de uma CPU ocupada pelo contêiner da API
naquele instante (2 de 14 amostras).

## Análise

- **Meta atingida com folga nas duas execuções.** Nenhuma perda; a pior
  consulta levou 68,82 ms, mais de 140 vezes abaixo do limite de 10 s.
- **Latência maior na segunda execução.** A mediana passou de 5,37 ms para
  8,92 ms e o p99, de 8,05 ms para 18,83 ms. Não isolei a causa. Entre
  as duas execuções entrou a spec 005; no processo da API, a única mudança é
  uma leitura da fase da reconstrução a cada 10 s, em segundo plano
  (`FASE_INTERVALO_SEGUNDOS`), e a consulta de saldo é a mesma. A outra
  candidata é a carga concorrente da
  máquina de desenvolvimento, que o teste não controla. As duas hipóteses são
  `[NVI]`; nenhuma das medições é de ambiente dedicado.
- **Mudança posterior, sem nova medição.** Em 2026-11-10 a API passou a ler
  também a geração ativa a cada 10 s, em segundo plano, e o relay passou a
  conferir o lease antes de cada evento. Não reexecutei a carga depois disso
  `[NVI]`; a consulta de saldo medida aqui não mudou.
- **Sem cache.** Atingi a meta sem cache, e por isso mantenho a decisão de
  não ter cache na entrega inicial
  ([ADR-011](../adr/ADR-011-sem-cache-na-entrega-inicial.md)).
- **Limites do teste.** O saldo acumulado é calculado na consulta, somando as
  datas anteriores da geração ativa (004 TD-007); com cerca de 30 datas no
  banco, o custo é pequeno. O teste não mede o custo com milhares de datas,
  nem consultas concorrentes ao registro de lançamentos, nem mais de um
  processo da API. Tratei esses pontos como gatilhos de revisão dos ADRs 009
  e 011
  ([`evolucoes.md`](../evolucoes.md)).
- **Reexecução segura.** Dei à massa chaves de idempotência fixas, porque o
  livro é imutável e um lançamento de teste nele não sai mais: a segunda
  execução devolveu os mesmos lançamentos, sem gravar novos. Conferi no
  `pg-livro` depois da carga: 0 lançamentos com
  `registrado_em` na janela de 13:50 a 13:56 e 60 lançamentos nas datas da
  massa (um crédito e um débito em cada uma das 30 datas).
