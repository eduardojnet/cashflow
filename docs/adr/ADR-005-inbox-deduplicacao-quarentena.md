# ADR-005: Inbox, deduplicação e quarentena

> Status: Aceito
> Data: 2026-11-05
> Relacionado: INFRA 2.4, 3.2, 4.9, 4.10; ST-TD-003; 004 TD-001, TD-003; 004 D-008; 002 TD-004; P-003

## Contexto

A entrega do relay ao worker é pelo menos uma vez
([ADR-003](ADR-003-outbox-transacional-e-relay.md),
[ADR-004](ADR-004-rabbitmq-confirms-ack-apos-commit.md)): lease expirado,
confirm perdido, ACK perdido e republicação para reconstrução geram a mesma
mensagem mais de uma vez. Somar duas vezes o mesmo lançamento corrompe o
saldo. INFRA 2.4 exige "deduplicação por evento e lançamento" e "quarentena
durável para conflitos"; INFRA 4.10, nenhum descarte silencioso de evento
financeiro; INFRA 3.2 lista inbox, contribuições e quarentena no banco do
consolidado. Também é preciso tratar mensagem ilegível e o caso de um mesmo
identificador chegar com conteúdo diferente, que indica defeito ou
adulteração e não pode ser resolvido em silêncio.

## Decisão

Na mesma transação da projeção, para cada geração que recebe eventos
([ADR-007](ADR-007-geracoes-barreira-troca-atomica.md)), o worker aplica duas
chaves de deduplicação (004 TD-001):

1. **Inbox por evento:** chave primária (`geracao_id`, `evento_id`) com o
   hash do conteúdo. Mesmo evento com o mesmo hash: sem efeito. Mesmo evento
   com hash diferente: quarentena.
2. **Contribuição por lançamento:** chave primária (`geracao_id`,
   `lancamento_id`) com posição, data, tipo, valor e hash. Mesmo lançamento
   com outro conteúdo, ou outro lançamento na mesma posição: quarentena.

Só uma contribuição nova altera o saldo do dia, grava o marcador de inclusão e
avança o checkpoint.

**Quarentena no banco do consolidado (004 TD-003, D-008).** Conflitos e
mensagens ilegíveis são gravados na tabela `quarentena` com o motivo e o
conteúdo bruto, na mesma transação, uma vez por mensagem. Só depois do commit
o worker envia o ACK. Nada é descartado sem registro.

A deduplicação do lado do produtor é a idempotência do registro (002 TD-004,
em [ADR-003](ADR-003-outbox-transacional-e-relay.md)): a mesma requisição
repetida pelo cliente não gera um segundo lançamento nem um segundo evento.

## Alternativas consideradas

### Alternativa 1: Fila de mensagens mortas (dead-letter) no RabbitMQ

- **Origem:** análise do ADR; INFRA 4.9 aceita "fila ou armazenamento
  durável", e 004 D-008 escolheu o banco.
- **Descrição:** rejeitar a mensagem em conflito e deixá-la numa fila de
  dead-letter.
- **Prós:** recurso nativo do broker; sem tabela extra.
- **Contras:** a quarentena ficaria fora da transação da projeção e no
  componente que não é fonte de verdade (INFRA 4.11); consulta e auditoria
  exigiriam ler o broker.
- **Por que foi rejeitada:** INFRA 3.2 lista a quarentena no banco do
  consolidado; gravar na mesma transação garante que a mensagem só é
  confirmada depois de registrada.

### Alternativa 2: Deduplicar só por `evento_id`

- **Origem:** análise do ADR (INFRA 2.4 pede as duas chaves).
- **Descrição:** inbox simples, sem a chave por lançamento.
- **Prós:** um único controle; menos escrita.
- **Contras:** um mesmo lançamento publicado com outro `evento_id` seria
  somado duas vezes.
- **Por que foi rejeitada:** descumpre INFRA 2.4 ("por evento e lançamento").

### Alternativa 3: Saldo idempotente por recálculo, sem inbox

- **Origem:** análise do ADR.
- **Descrição:** recalcular o saldo da data a partir das contribuições a cada
  mensagem, em vez de incrementar.
- **Prós:** duplicata não altera o resultado.
- **Contras:** custo de escrita proporcional ao número de lançamentos da
  data; não distingue conflito de duplicata, então o conflito não iria para a
  quarentena.
- **Por que foi rejeitada:** não atende INFRA 2.4 (quarentena para
  conflitos) e piora a escrita no pico.

### Alternativa 4: Descartar a mensagem ilegível com `nack` sem requeue

- **Origem:** análise do ADR (004 design §16 permite só após gravar a
  quarentena).
- **Descrição:** recusar a mensagem que não passa na leitura do evento.
- **Prós:** simples.
- **Contras:** perda silenciosa de um possível evento financeiro.
- **Por que foi rejeitada:** viola INFRA 4.10 e P-003.

## Consequências

### Positivas

- Duplicação, perda de ACK e republicação não alteram o saldo
  ([`tests/e2e/test_falhas_consolidado.py`](../../tests/e2e/test_falhas_consolidado.py);
  [`services/consolidado/tests/integracao/test_worker_projecao.py`](../../services/consolidado/tests/integracao/test_worker_projecao.py)).
- Conflitos ficam auditáveis no banco, com o conteúdo original.
- Como as chaves incluem a geração, a mesma mensagem é aplicada uma vez em
  cada geração, o que permite reconstruir por replay
  ([ADR-007](ADR-007-geracoes-barreira-troca-atomica.md)).

### Negativas / trade-offs

- Inbox e contribuições crescem com o volume de lançamentos e são
  duplicadas por geração durante a reconstrução; a retenção é pendente
  (INFRA 7.7).
- A quarentena não tem fluxo de reprocessamento automático: o tratamento é
  manual.

### Riscos

- Quarentena crescendo sem ninguém ver. Mitigação: métrica
  `cashflow_quarentena_total` e alerta `CashFlowQuarentena`
  ([ADR-012](ADR-012-prometheus-grafana.md)).

## Notas de implementação

- Caso de uso `ProjetarLancamento` em
  [`services/consolidado/aplicacao/projetar_lancamento.py`](../../services/consolidado/aplicacao/projetar_lancamento.py);
  tabelas na migração 0001 do consolidado.
- Gatilho de revisão: volume de quarentena que exija reprocessamento
  automatizado, ou retenção da inbox definida (INFRA 7.7).

## Referências

- [04 - Fluxos: consolidação pelo worker](../arquitetura/04-fluxos.md#4-consolidação-pelo-worker)
- [03 - Componentes: modelo de dados do consolidado](../arquitetura/03-componentes.md#modelo-de-dados-do-consolidado)
