# ADR-004: RabbitMQ com confirms, `mandatory` e ACK após commit; não é fonte de verdade

> Status: Aceito
> Data: 2026-11-05
> Relacionado: INFRA §4 (4.1 a 4.11), 2.4; 003 TD-002; 004 TD-002; 004 DQ-003; P-002, P-003, P-005

## Contexto

O evento sai do livro pelo relay ([ADR-003](ADR-003-outbox-transacional-e-relay.md))
e precisa chegar ao worker do consolidado sem perda silenciosa. INFRA §4 fixa
o broker e as garantias: RabbitMQ local com volume persistente, exchange e
filas duráveis, mensagens persistentes, publicação com `mandatory`, publisher
confirms, ACK manual somente após o commit da aplicação e nenhum descarte
silencioso. INFRA 4.11 acrescenta que o RabbitMQ não é fonte de verdade
financeira nem histórico obrigatório para reconstrução. Não escolhi o
broker: ele já vem de INFRA. Neste ADR registro como decidi usá-lo.

## Decisão

- **Topologia declarada por arquivo:** declarei a exchange durável
  `cashflow.lancamentos` ligada à fila durável `consolidado.lancamentos` pela
  routing key `lancamento.registrado.v1`. Carrego as definições na subida e
  não crio o usuário `guest`, para que nenhuma credencial padrão exista.
- **Usuários com permissão mínima:** `relay` só escreve na exchange; `worker`
  só lê a fila; nenhum dos dois tem permissão de configuração. Assim, uma
  credencial de runtime vazada não apaga nem recria a fila.
- **Lado do produtor (003 TD-002):** publico mensagem persistente com
  `mandatory` e publisher confirms. O relay só marca o evento como publicado
  com confirm positivo e sem `basic.return`; devolução (fila ausente),
  `nack`, falta de confirmação no prazo e erro de conexão mantêm o evento
  pendente. Escolhi as duas garantias juntas porque o confirm sozinho não
  avisa quando a exchange não tem fila ligada.
- **Lado do consumidor (004 TD-002):** envio o ACK manual só depois do commit
  da transação de projeção no `pg-consolidado`. Falha antes do commit deixa a
  mensagem sem ACK, e o broker a reentrega. Deixei o `prefetch` configurável
  (`WORKER_PREFETCH`, padrão 10; 004 DQ-003).
- **Sem papel de histórico:** uso o RabbitMQ só para transportar. Replay e
  reconstrução partem da outbox do livro ([ADR-003](ADR-003-outbox-transacional-e-relay.md),
  [ADR-007](ADR-007-geracoes-barreira-troca-atomica.md)); por isso a perda
  do volume do broker é recuperável (INFRA 7.6).

## Alternativas consideradas

### Alternativa 1: Publicação sem confirms nem `mandatory`

- **Origem:** análise do ADR (é o oposto de INFRA 4.6 e 4.7).
- **Descrição:** publicar e considerar entregue ao retornar do `basic.publish`.
- **Prós:** maior vazão do relay; código mais simples.
- **Contras:** mensagem para exchange sem fila ligada ou perdida pelo broker
  some sem aviso, e o relay a marca como publicada.
- **Por que rejeitei:** viola INFRA 4.6, 4.7 e 4.10.

### Alternativa 2: ACK automático ou antes do commit

- **Origem:** análise do ADR; listei como anti-padrão no steering de
  convenções.
- **Descrição:** confirmar a mensagem na entrega ou antes de gravar a
  projeção.
- **Prós:** menos reentregas.
- **Contras:** queda do worker entre o ACK e o commit perde o evento.
- **Por que rejeitei:** viola INFRA 4.8 e P-005.

### Alternativa 3: Log de eventos com retenção (por exemplo, Kafka) como histórico

- **Origem:** análise do ADR.
- **Descrição:** broker com retenção longa, usado também como fonte do replay.
- **Prós:** replay sem passar pela outbox; ordem por partição.
- **Contras:** duas fontes de verdade (livro e log); operação mais pesada num
  ambiente local de um nó.
- **Por que rejeitei:** INFRA 4.1 fixa RabbitMQ e INFRA 4.11 proíbe usar o
  broker como histórico obrigatório para reconstrução.

### Alternativa 4: Usuários com permissão de configuração declarando a topologia

- **Origem:** análise do ADR (decisão de 003 design §4, declaração por
  arquivo).
- **Descrição:** relay e worker declaram exchange e fila ao conectar.
- **Prós:** sem arquivo de definições.
- **Contras:** credencial de runtime capaz de apagar ou recriar a fila.
- **Por que rejeitei:** contraria o mínimo privilégio (INFRA 5.11; P-007).

## Consequências

### Positivas

- Nenhum evento é marcado como publicado sem confirmação do broker, e nenhuma
  mensagem é confirmada sem projeção gravada.
- Testei queda do consumidor, perda de ACK, duplicação e indisponibilidade do
  RabbitMQ sobre o compose real
  ([`tests/e2e/test_falhas_consolidado.py`](../../tests/e2e/test_falhas_consolidado.py),
  [`tests/e2e/test_queda_rabbitmq.py`](../../tests/e2e/test_queda_rabbitmq.py)).
- Verifico as permissões dos usuários em
  [`tests/infra/test_rabbitmq_permissoes.py`](../../tests/infra/test_rabbitmq_permissoes.py).

### Negativas / trade-offs

- A entrega é pelo menos uma vez: o consumidor precisa deduplicar
  ([ADR-005](ADR-005-inbox-deduplicacao-quarentena.md)).
- Confirms e ACK por mensagem reduzem a vazão máxima em relação a publicação
  e consumo sem garantia; aceitei isso porque as fontes não trazem meta de
  vazão de escrita.
- Uso instância única, sem cluster (INFRA - Fora do escopo): enquanto o
  broker está fora, o consolidado fica defasado, embora o registro continue.

### Riscos

- Mensagem confirmada pelo broker e perdida antes do consumo (por exemplo,
  perda do volume). Mitiguei com a reconciliação, que acusa as posições
  faltantes, e com a reconstrução, que republica a partir da outbox
  (INFRA 7.6).

## Notas de implementação

- Uso a imagem `rabbitmq:4.4.0-management-alpine`, com versão fixada, fonte e
  data no compose ([ADR-010](ADR-010-docker-compose-local.md)).
- As definições ficam em [`infra/rabbitmq/definicoes.modelo.json`](../../infra/rabbitmq/definicoes.modelo.json);
  o entrypoint substitui na subida as senhas vindas de `infra/.env`, para que
  nenhuma senha fique versionada.
- Uso fila clássica (`x-queue-type: classic`).
- Revejo esta decisão se surgir exigência de alta disponibilidade do broker
  (cluster ou filas quorum) ou de ordem de entrega.

## Referências

- [04 - Fluxos: publicação e consolidação](../arquitetura/04-fluxos.md#3-publicação-pelo-relay)
- [02 - Contêineres](../arquitetura/02-conteineres.md)
