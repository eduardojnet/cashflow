# Teste de carga do consolidado (004-T6)

Relatório bruto da execução do teste de carga da API do consolidado. A spec
006 (006-T6) consolida este relatório, com as execuções seguintes, em
[`relatorio-carga.md`](relatorio-carga.md).

## Protocolo

Fonte: INFRA 9.4; 004 NFR-001.

| Item | Valor |
|---|---|
| Gerador | k6, executor `constant-arrival-rate` (chegada fixa) |
| Taxa | 50 consultas/s |
| Duração | 300 s |
| Total programado | 15.000 consultas |
| Limite por consulta | 10 s (`timeout` da requisição) |
| Critério de aprovação | pelo menos 14.250 respostas de sucesso (até 5% de perda) |
| Sucesso | status 200, corpo com a data pedida e resposta em até 10 s |
| Perda | consulta programada que não teve sucesso: erro, timeout, token não obtido ou iteração descartada pelo gerador |
| Cenário | `GET /v1/consolidado/saldos/{data}` (FR-004), datas existentes em rodízio |
| Massa | 30 datas (2026-10-01 a 2026-10-30), um crédito e um débito em cada, registrados no setup com chaves de idempotência fixas |
| Autenticação | token de `cliente-consultas` obtido no Keycloak, renovado por VU antes de expirar (token de 300 s) |
| TLS | k6 no host contra `https://localhost:8445`, confiando só na AC local (`SSL_CERT_FILE`); sem `insecureSkipTLSVerify` |

Script: `tests/carga/consolidado.js`. Comando, a partir da raiz do
repositório, com o ambiente no ar:

```bash
SSL_CERT_FILE=infra/tls/gerados/ac/ac-cashflow.crt \
  uv run --env-file infra/.env -- k6 run tests/carga/consolidado.js
```

O k6 termina com código 0 quando o limiar `consultas_sucesso >= 14250` é
atendido e com 99 quando não é.

## Resultado

Execução de 2026-11-06, encerrada às 16:27:25 (-03:00), sobre o commit
`b2848ee` mais o script deste relatório. Código de saída do k6: 0.

| Contagem | Valor |
|---|---|
| Programadas | 15.000 |
| Enviadas | 15.000 |
| Sucesso | 15.000 |
| Perda | 0 (0%) |
| Não enviadas por falta de token | 0 |
| Descartadas pelo gerador | 0 |
| VUs máximos alocados | 50 |
| Veredito | APROVADO (mínimo 14.250) |

Duração das consultas (ms), medida pelo k6:

| mín | média | mediana | p90 | p95 | p99 | p99,9 | máx |
|---|---|---|---|---|---|---|---|
| 1,79 | 5,00 | 5,37 | 6,20 | 6,47 | 8,05 | 9,26 | 25,31 |

Corroboração pelo Prometheus, logo após o teste:
`sum by (status) (increase(cashflow_http_requisicoes_total{componente="consolidado-api"}[7m]))`
retornou cerca de 15.610 respostas 200 e 0 respostas 503. O valor inclui as
consultas de inclusão do setup e as sondas de health da janela, e `increase`
extrapola nas bordas; serve de conferência de ordem de grandeza, não de
contagem exata.

Uso amostrado com `docker stats --no-stream` durante a carga:

| Contêiner | CPU | Memória |
|---|---|---|
| consolidado-api | 25% a 59% | 65 a 93 MiB |
| pg-consolidado | 3% a 7% | 28 MiB |
| keycloak | menos de 1% | 470 a 484 MiB |

## Recursos da máquina

| Item | Valor |
|---|---|
| CPU | 13th Gen Intel Core i7-1355U, 12 CPUs lógicas |
| RAM | 16.053.588 kB no total; 2.999.728 kB disponíveis no início da carga |
| Disco livre | 23,7 GiB de 467,9 GiB (mesmo sistema de arquivos do projeto e de `/var/lib/docker`) |
| Sistema | Linux Mint 22.3, kernel 6.8.0-146-generic |
| Docker | Engine 29.9.2 (cliente e servidor), driver overlay2 |
| Docker Compose | v5.6.1 |
| k6 | v2.3.0 |
| uv | 0.13.7 |

Configuração da API durante o teste: um processo uvicorn, pool psycopg de 1 a
10 conexões (`POOL_MIN`, `POOL_MAX` padrão), sem cache (004 TD-005). Rodavam
na máquina, além do ambiente `cashflow` completo, outros três contêineres sem
relação com o projeto.

## Observações

- A meta foi atingida sem cache, o que mantém a decisão 004 TD-005 (cache só
  com evidência de necessidade).
- O saldo acumulado é calculado na consulta (004 TD-007), somando as datas
  anteriores da geração ativa; com 31 datas no banco, o custo ficou abaixo de
  10 ms no p99,9. O teste não mede o custo com milhares de datas.
- O registro da massa grava lançamentos no livro, que é imutável (002
  TD-003). As chaves de idempotência fixas fazem a reexecução devolver os
  mesmos lançamentos sem criar novos.
- A atualização de 001/NFR-001 com os recursos acima é feita por Eduardo
  fora da execução não assistida (emenda da 004-T6, 2026-11-06).
