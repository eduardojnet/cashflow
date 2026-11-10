# ADR-003: Outbox transacional e relay

> Status: Aceito
> Data: 2026-11-05
> Relacionado: INFRA 2.1, 2.2, 3.1, 3.3, 6.4, 7.2, 7.6; ST-TD-002; 002 TD-001, TD-003, TD-004; 003 TD-001 a TD-004; 003 D-001 a D-003; P-001, P-002, P-006

## Contexto

O registro de um lançamento precisa ser confirmado ao cliente sem depender
do consolidado nem do RabbitMQ (INFRA 2.1; P-001), mas todo lançamento
confirmado precisa, cedo ou tarde, chegar ao consolidado. Gravar no banco e
publicar no broker são duas operações em sistemas diferentes, e INFRA 3.3
proíbe transação distribuída. INFRA 2.2 descreve o componente que resolve
isso: um relay da outbox com consulta periódica, lease por tentativa,
publisher confirms e retentativas controladas. INFRA 3.1 lista no banco do
livro: lançamentos imutáveis, respostas idempotentes, contador de posição,
outbox e metadados de publicação. INFRA 7.2 e 7.6 exigem preservar livro e
outbox para replay e para recuperação após perda do RabbitMQ.

## Decisão

**Registro (002 TD-001, TD-003, TD-004).** Uma única transação local em
`pg-livro` grava, tudo ou nada: a resposta idempotente, a nova posição, o
lançamento e o evento `LancamentoRegistrado` v1 na outbox. A API não fala com
o RabbitMQ.

- **Idempotência:** a chave do cliente (`Idempotency-Key`) é única por
  cliente; a tabela `resposta_idempotente` tem chave primária
  (`cliente_id`, `chave`) e guarda o hash do corpo. Repetição com o mesmo
  corpo devolve a resposta original; com corpo diferente, 409 (002 D-006).
  Duas requisições simultâneas com a mesma chave se resolvem pela chave
  primária.
- **Imutabilidade por privilégio:** `livro_api` tem só `SELECT` e `INSERT` em
  `lancamento` e só `INSERT` na outbox. Alterar ou apagar um lançamento é
  negado pelo banco, não só pelo código (INFRA 3.1; P-006).

**Publicação (003 TD-001 a TD-003).** O processo `relay`, com o papel
`livro_relay`, em laço:

1. Reserva um lote de eventos pendentes com `FOR UPDATE SKIP LOCKED`, grava
   `lease_dono` e `lease_ate` e confirma a reserva antes de publicar.
2. Publica cada evento com `mandatory`, mensagem persistente e `message_id`
   igual ao `evento_id`, e espera o publisher confirm
   ([ADR-004](ADR-004-rabbitmq-confirms-ack-apos-commit.md)).
3. Só com confirm positivo e sem devolução grava `publicado_em`. Devolução,
   recusa, falta de confirmação no prazo ou erro de conexão incrementam
   `tentativas` e agendam a próxima tentativa com espera exponencial
   (`base * 2^n`) até um teto, sem limite de tentativas (003 D-002).
4. Nenhum evento sai da outbox depois de publicado (003 D-001).

A ordem de entrega não é garantida (003 D-003); o consolidado soma
contribuições e detecta faltas pela posição
([ADR-006](ADR-006-posicao-monotonica-e-checkpoint.md)).

**Republicação (003 TD-004).** O comando restrito `relay-republicar
--desde-posicao N`, com o papel `livro_replay` e só na rede do livro, zera
`publicado_em` e `tentativas` dos eventos a partir da posição N; o relay os
publica de novo. É a base da reconstrução e da recuperação após perda do
RabbitMQ (INFRA 7.6; [ADR-007](ADR-007-geracoes-barreira-troca-atomica.md)).

## Alternativas consideradas

### Alternativa 1: Publicar direto no RabbitMQ na requisição

- **Origem:** spec (002 TD-001).
- **Descrição:** a API grava o lançamento e publica o evento antes de
  responder.
- **Prós:** menor latência até o consolidado; sem relay.
- **Contras:** se o broker estiver fora, o registro falha ou o evento se
  perde entre o commit e a publicação.
- **Por que foi rejeitada:** viola INFRA 2.1 (confirmação independente do
  RabbitMQ).

### Alternativa 2: Transação distribuída entre banco e broker

- **Origem:** spec (002 TD-001).
- **Descrição:** XA ou two-phase commit envolvendo PostgreSQL e o broker.
- **Prós:** atomicidade entre gravação e publicação.
- **Contras:** o registro passa a depender da disponibilidade do broker;
  complexidade operacional alta.
- **Por que foi rejeitada:** viola INFRA 3.3 (sem transação distribuída).

### Alternativa 3: Captura de mudanças (CDC) pelo log do PostgreSQL

- **Origem:** análise do ADR.
- **Descrição:** uma ferramenta lê o WAL do `pg-livro` e publica as
  inserções no broker.
- **Prós:** sem polling; sem tabela de outbox.
- **Contras:** novo componente com acesso de replicação ao banco do livro;
  lease, confirms e retentativas ficam fora do controle da aplicação.
- **Por que foi rejeitada:** INFRA 2.2 descreve um relay com consulta
  periódica e lease; e P-011 pede não acrescentar componente sem requisito.

### Alternativa 4: Consolidado ler o livro para republicar ou reconstruir

- **Origem:** spec (003 TD-004).
- **Descrição:** o coordenador do consolidado lê `pg-livro` para refazer a
  projeção.
- **Prós:** reconstrução sem passar pelo broker.
- **Contras:** cria dependência de rede e de credencial entre os lados.
- **Por que foi rejeitada:** viola INFRA 6.7.

### Alternativa 5: Apagar o evento da outbox após publicar

- **Origem:** spec (003 D-001).
- **Descrição:** manter na outbox só os pendentes.
- **Prós:** tabela pequena.
- **Contras:** perde a fonte de replay.
- **Por que foi rejeitada:** INFRA 7.2 e 7.3 exigem preservar a outbox para
  replay e reconstrução.

## Consequências

### Positivas

- Lançamento confirmado com relay, RabbitMQ e consolidado parados
  ([`tests/e2e/test_independencia_lancamentos.py`](../../tests/e2e/test_independencia_lancamentos.py)).
- Com o RabbitMQ fora, os eventos acumulam na outbox e são publicados quando
  ele volta ([`tests/e2e/test_queda_rabbitmq.py`](../../tests/e2e/test_queda_rabbitmq.py)).
- O livro e a outbox bastam para recompor o consolidado (P-002).

### Negativas / trade-offs

- Consistência eventual com o consolidado; o atraso depende do intervalo do
  relay e do backlog.
- Entrega pelo menos uma vez: lease expirado ou confirm perdido geram
  duplicata, tratada no consumidor ([ADR-005](ADR-005-inbox-deduplicacao-quarentena.md)).
- A outbox e as respostas idempotentes crescem sem limite; a política de
  retenção é pendente (INFRA 7.7).
- Polling gera consultas periódicas no livro, mitigadas por índice parcial nos
  pendentes e intervalo configurável.

### Riscos

- Evento retido por falha persistente de publicação. Mitigação: retentativa
  ilimitada, métricas de backlog e de idade do evento mais antigo, contador de
  falhas por resultado e alerta `CashFlowOutboxEventoAntigo`
  ([ADR-012](ADR-012-prometheus-grafana.md),
  [ADR-015](ADR-015-catalogo-fechado-de-metricas.md)).

## Notas de implementação

- Parâmetros por variável de ambiente, com padrão no compose: intervalo 1 s,
  lote 100, lease 30 s, prazo do confirm 5 s, espera base 1 s e teto 60 s
  (`RELAY_*`).
- Orçamento do lease (003 TD-005, emenda de 2026-11-10 pela review da spec
  003): o lease cobre o lote, mas os eventos saem um a um. Antes de cada
  evento, o ciclo mede pelo relógio monotônico, a partir de antes da
  reserva, quanto resta do lease menos 2 s de margem. Se não cabe uma
  publicação inteira (o prazo do confirm), os restantes ficam sem tentativa
  e voltam à reserva quando o lease vence; a publicação em curso é
  interrompida no restante e tratada como confirm fora do prazo. Lease que
  não comporta o prazo do confirm mais a margem impede a subida.
  Alternativas rejeitadas: validar `lote × prazo do confirm < lease` na
  subida, que obrigaria lease de 500 s ou lote de 5 com os padrões; e
  renovar o lease durante o lote, que acrescenta uma escrita por evento e
  ainda depende de o relay continuar vivo para renovar.
- O `relay-republicar` roda sob o perfil `operacao` do compose.
- Testes: [`services/relay/tests/`](../../services/relay/tests/) (reserva,
  publicação e republicação contra PostgreSQL e RabbitMQ reais) e
  [`services/lancamentos/tests/integracao/test_unidade_de_trabalho_livro.py`](../../services/lancamentos/tests/integracao/test_unidade_de_trabalho_livro.py).
- Gatilho de revisão: vazão de escrita que torne o polling o gargalo, ou
  exigência de ordem de entrega.

## Referências

- [04 - Fluxos: registro e publicação](../arquitetura/04-fluxos.md#2-registro-de-lançamento)
- [03 - Componentes: relay](../arquitetura/03-componentes.md#relay-da-outbox-servicesrelay)
