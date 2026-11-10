# ADR-011: Sem cache na entrega inicial

> Status: Aceito
> Data: 2026-11-05
> Relacionado: DES (escalabilidade; RNF de 50 req/s); INFRA 9.4; 004 TD-004, TD-005, TD-007, TD-008; 002 TD-006; 004 NFR-001; 004 D-002, D-007; 002 D-005; P-011

## Contexto

DES pede considerar "estratégias de cache" e fixa o pico do consolidado:
50 req/s com no máximo 5% de perda. INFRA 9.4 define o protocolo de medição:
chegada fixa de 50 consultas/s por 300 s (15.000 consultas), cada uma
observada por até 10 s; a meta que fixei na spec 004 é de pelo menos 14.250
respostas de sucesso (004 NFR-001). INFRA não lista cache entre os
componentes, e não acrescento componente sem requisito que o exija (P-011).

Nesta solução, o cache também tem custo de correção: o saldo muda a cada
evento projetado, lançamentos retroativos alteram dias passados (002 D-005)
e a troca de geração muda todo o conjunto de uma vez
([ADR-007](ADR-007-geracoes-barreira-troca-atomica.md)). Um cache precisaria
de invalidação por data e por geração.

## Decisão

**Não pus cache na entrega inicial (004 TD-005).** Decidi que o cache só
entra se o teste de carga mostrar que a meta não é atingida sem ele. Sustento
a leitura direta do banco com o desenho do modelo de leitura:

- **Saldo materializado por data e geração (004 TD-004):** uma linha por
  (`geracao_id`, `data`) em `saldo_diario`, atualizada a cada contribuição.
  A consulta do dia é uma leitura por chave primária.
- **Saldo acumulado calculado na consulta (004 TD-007):** soma de `saldo_dia`
  das datas até a consultada, na geração ativa. No intervalo, soma das datas
  anteriores ao primeiro item da página mais a soma corrente na página.
  Calculo na consulta porque materializar o acumulado faria cada lançamento
  retroativo reescrever todos os dias seguintes (alternativa 3).
- **Paginação por cursor de data no consolidado (004 TD-008):** cursor opaco
  com a última data retornada, `limite` com padrão 50 e máximo 500
  configuráveis; dias sem lançamento são omitidos (004 D-002, D-007).
- **Paginação por cursor na listagem de lançamentos (002 TD-006):** cursor
  opaco com (`data_lancamento`, `posicao`) do último item, ordenação por data
  e posição.

## Alternativas consideradas

### Alternativa 1: Cache distribuído (por exemplo, Redis) na frente do consolidado

- **Origem:** análise do ADR (DES cita cache; em 004 TD-005 decidi adiar).
- **Descrição:** guardar o saldo por data e geração num cache externo com
  expiração curta.
- **Prós:** absorve picos acima da capacidade do banco.
- **Contras:** novo componente, nova credencial e nova rede; invalidação por
  evento, por lançamento retroativo e por troca de geração; risco de servir
  saldo de geração já trocada.
- **Por que rejeitei:** não havia evidência de necessidade, e a medição
  atingiu a meta sem cache (ver Consequências).

### Alternativa 2: Cache em memória no processo da API

- **Origem:** análise do ADR.
- **Descrição:** guardar respostas recentes no próprio processo uvicorn.
- **Prós:** sem componente novo.
- **Contras:** inconsistência entre réplicas, se houver mais de uma; a mesma
  invalidação por geração e por data.
- **Por que rejeitei:** pelo mesmo motivo da alternativa 1, e porque
  acrescentaria estado a uma API que hoje não tem estado.

### Alternativa 3: Saldo acumulado materializado por dia

- **Origem:** spec (004 TD-007).
- **Descrição:** gravar o acumulado em cada linha de `saldo_diario`.
- **Prós:** consulta lê uma linha só.
- **Contras:** cada lançamento retroativo reescreveria o acumulado de todos os
  dias seguintes, em cascata, na transação do worker.
- **Por que rejeitei:** custo de escrita imprevisível e contenção no worker.

### Alternativa 4: Paginação por offset e limite

- **Origem:** spec (002 TD-006).
- **Descrição:** `?pagina=N&tamanho=M`.
- **Prós:** salto direto para uma página.
- **Contras:** páginas instáveis com inserções retroativas concorrentes
  (itens repetidos ou pulados) e custo crescente com o offset.
- **Por que rejeitei:** a listagem precisa ser estável com lançamentos
  retroativos chegando.

## Consequências

### Positivas

- No teste de carga de 2026-11-06, sem cache: 15.000 consultas programadas,
  15.000 com sucesso, 0 perda; duração p95 de 6,47 ms e p99 de 8,05 ms; a API
  usou de 25% a 59% de CPU
  ([relatório](../testes/carga-consolidado.md)). Atingi a meta (14.250), e
  por isso mantenho a decisão.
- Nenhuma resposta fica desatualizada por cache: o saldo devolvido é o da
  geração ativa no momento da consulta, com o checkpoint correspondente.

### Negativas / trade-offs

- Toda consulta vai ao `pg-consolidado`; a capacidade de leitura é a do banco
  e de um processo de API.
- O custo do acumulado cresce com o número de datas da geração. O teste
  rodou com 31 datas no banco; não medi o custo com milhares de datas.
- A escrita concorrente na mesma data serializa na linha de `saldo_diario`.
- O cursor não permite saltar para a página N.

### Riscos

- Volume de datas alto degradar o acumulado. Minha conduta prevista é medir
  com massa maior e, se necessário, materializar o acumulado por mês ou pôr
  cache, com nova decisão.

## Notas de implementação

- As consultas estão em
  [`services/consolidado/aplicacao/consultar_consolidado.py`](../../services/consolidado/aplicacao/consultar_consolidado.py);
  os limites, em `SALDOS_LIMITE_PADRAO` e `SALDOS_LIMITE_MAXIMO`.
- Script de carga: [`tests/carga/consolidado.js`](../../tests/carga/consolidado.js).
- Revejo esta decisão se o teste de carga ficar abaixo de 14.250 sucessos, se
  a meta de pico passar de 50 req/s, ou se a latência crescer com o número de
  datas.

## Referências

- [Relatório do teste de carga](../testes/carga-consolidado.md)
- [04 - Fluxos: consulta do consolidado](../arquitetura/04-fluxos.md#5-consulta-do-consolidado)
