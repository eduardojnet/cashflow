# ADR-015: Catálogo fechado de métricas e métricas do projeto fora de INFRA 8.3

> Status: Aceito
> Data: 2026-11-06
> Relacionado: INFRA 8.2, 8.3; 007 TD-002, TD-003, TD-005, TD-006 (emendas de 2026-11-06); 002 design §12; 003 design §12; [ADR-012](ADR-012-prometheus-grafana.md)

## Contexto

INFRA 8.3 lista o que medir, e INFRA 8.2 proíbe expor tokens, senhas,
segredos e conteúdo financeiro, inclusive em telemetria. Rótulo de métrica
com identificador de lançamento, cliente ou valor vaza dado e explode a
cardinalidade das séries. Quatro processos de aplicação expõem métricas (as
duas APIs, o relay e o worker) e precisam da mesma convenção de nomes e
rótulos.

Em 2026-11-06, duas atividades bloquearam porque o design pedia medidas que o
catálogo não tinha: "requisições por status" nas APIs (002 design §12) e
"falhas de publicação" no relay (003 design §12). A meta de DES de no máximo
5% de perda no consolidado só era medida pelo k6 durante o teste de carga, e
a causa de um evento parado na outbox só aparecia no log.

## Decisão

- **Pacote compartilhado (007 TD-002):** `packages/observabilidade` define o
  catálogo e serve o `/metrics` numa porta interna. Nenhum componente cria
  métrica fora dele.
- **Catálogo fechado com rótulos permitidos (007 TD-003):** cada métrica tem a
  lista fechada de rótulos aceitos; o pacote recusa métrica desconhecida,
  rótulo fora da lista e, por padrão de nome, rótulo que pareça dado sensível
  (`cliente`, `lancamento`, `valor`, `saldo`, `token`, `senha` e afins).
- **Métricas de INFRA 8.3:** `cashflow_outbox_pendentes`,
  `cashflow_outbox_idade_mais_antigo_segundos`,
  `cashflow_worker_reentregas_total`, `cashflow_quarentena_total`,
  `cashflow_pool_conexoes{componente,estado}`, `cashflow_geracao_ativa`,
  `cashflow_reconstrucao_fase`, `cashflow_checkpoint_posicao{geracao}`,
  `cashflow_lacunas{geracao}`, `cashflow_jwt_falhas_total{motivo}` e
  `cashflow_jwks_falhas_total`. Mensagens prontas e não confirmadas vêm das
  métricas nativas de fila do plugin Prometheus do RabbitMQ; espaço em disco,
  do tamanho dos bancos nos exporters PostgreSQL e do disco livre informado
  pelo plugin do RabbitMQ. A fase da reconstrução, gravada pelo coordenador
  (CLI) em tabela, é exposta pela API do consolidado.
- **Métricas do projeto, fora de INFRA 8.3 (decisões de Eduardo em
  2026-11-06):**
  - `cashflow_http_requisicoes_total{componente,status}` (007 TD-005): uma
    contagem por resposta das duas APIs, inclusive 401, 403 e 422 gerados
    antes do handler; `status` é o código HTTP numérico.
  - `cashflow_relay_falhas_publicacao_total{resultado}` (007 TD-006):
    `resultado` em `devolvido`, `recusado`, `sem_confirmacao` e `falha`; as
    quatro séries começam em zero na subida.

## Alternativas consideradas

### Alternativa 1: Instrumentação HTTP pronta de terceiros

- **Origem:** spec (007 TD-005).
- **Descrição:** biblioteca que instrumenta o FastAPI automaticamente.
- **Prós:** menos código; latência por rota de graça.
- **Contras:** nomes e rótulos fora do catálogo; rótulos por rota e método
  sem controle.
- **Por que foi rejeitada:** quebra a regra de rótulos permitidos (007 TD-002,
  TD-003).

### Alternativa 2: Retirar "requisições por status" e "falhas de publicação" do design

- **Origem:** spec (007 TD-005, TD-006).
- **Descrição:** cobrir só INFRA 8.3 e deixar o resto para os logs.
- **Prós:** catálogo igual a INFRA 8.3.
- **Contras:** a meta de perda de 5% ficaria sem medição fora do teste de
  carga; a causa da falha de publicação ficaria só no log, sem série
  temporal nem alerta.
- **Por que foi rejeitada:** perde a observabilidade que DES pede.

### Alternativa 3: Rótulo `rota` nas requisições ou rótulo com a mensagem de erro no relay

- **Origem:** spec (007 TD-005, TD-006).
- **Descrição:** mais granularidade nos contadores.
- **Prós:** diagnóstico mais fino.
- **Contras:** cardinalidade crescente (rota) ou ilimitada e com risco de dado
  sensível (mensagem de erro, `evento_id`).
- **Por que foi rejeitada:** nenhuma fonte pede essa granularidade, e INFRA
  8.2 proíbe o risco de vazamento.

### Alternativa 4: Catálogo aberto, por convenção

- **Origem:** análise do ADR.
- **Descrição:** cada componente cria suas métricas seguindo um guia.
- **Prós:** sem pacote compartilhado.
- **Contras:** a proteção contra rótulo sensível depende de disciplina; nomes
  divergem entre componentes.
- **Por que foi rejeitada:** controle que depende de ninguém errar não
  atende INFRA 8.2 de forma verificável.

## Consequências

### Positivas

- Rótulo sensível é recusado por código e por teste
  ([`packages/observabilidade/tests/test_catalogo_metricas.py`](../../packages/observabilidade/tests/test_catalogo_metricas.py)).
- Cada métrica do catálogo é conferida no Prometheus real
  ([`tests/observabilidade/test_cobertura.py`](../../tests/observabilidade/test_cobertura.py)).
- Taxa de erro das APIs e causa das falhas do relay visíveis no painel; no
  teste de carga, o contador do servidor corroborou o k6
  ([relatório](../testes/carga-consolidado.md)).

### Negativas / trade-offs

- Duas métricas mantidas pelo projeto além de INFRA 8.3.
- Toda métrica ou valor de rótulo novo exige mudar o catálogo; um novo valor
  de `resultado` exige emenda.
- `cashflow_quarentena_total` é contador do processo e volta a zero quando o
  worker reinicia; o registro permanente é a tabela `quarentena`.

### Riscos

- Lacuna de cobertura: o mapa de métricas de 007 design §4 associa
  `cashflow_pool_conexoes` às APIs, ao relay, ao worker e ao coordenador. Na
  implementação, só as duas APIs e o worker a expõem; o relay não a expõe, e
  o coordenador, por ser CLI sem `/metrics`, não tem como ser coletado.
  Registrada como pendência para a revisão da spec 007.

## Notas de implementação

- Catálogo em
  [`packages/observabilidade/src/observabilidade/catalogo.py`](../../packages/observabilidade/src/observabilidade/catalogo.py).
- Gatilho de revisão: meta de latência ou de erro por rota; cardinalidade de
  `status` acima de algumas dezenas de séries por componente; novo desfecho
  de publicação; alerta que precise de taxa de falha relativa ao total
  publicado.

## Referências

- [03 - Componentes: pacotes compartilhados](../arquitetura/03-componentes.md#pacotes-compartilhados-packages)
- [ADR-012](ADR-012-prometheus-grafana.md)
