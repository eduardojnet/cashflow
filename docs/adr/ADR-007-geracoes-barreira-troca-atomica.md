# ADR-007: Gerações da projeção, barreira e troca atômica

> Status: Aceito
> Data: 2026-11-05
> Relacionado: INFRA 2.3, 2.5, 6.4, 6.7, 7.4 a 7.6, 9.6; ST-TD-004; 005 TD-001 a TD-003; 005 D-001 a D-006; 003 TD-004; P-002, P-011

## Contexto

Tratei o saldo consolidado como uma projeção derivada do livro (P-002), que
precisa poder ser refeita: após defeito na projeção, após perda de mensagens
no broker (INFRA 7.6) ou para conferir o resultado. INFRA 2.5 pede um
coordenador operacional com reconstrução de projeções, controle de gerações,
barreira de consumidores e troca atômica da geração ativa; INFRA 2.3, que a
API leia somente a geração ativa; INFRA 7.4 e 7.5, geração candidata separada
da ativa e capacidade para duas gerações. INFRA 9.6 exige testar
reconstrução concorrente (com lançamentos novos chegando), troca de geração
e rollback controlado. A reconstrução não pode ler o banco do livro
(INFRA 6.7) nem usar o RabbitMQ como histórico (INFRA 4.11).

## Decisão

- **Gerações:** chaveei todas as tabelas da projeção por `geracao_id`. Os
  estados são `construindo` (candidata), `ativa`, `anterior` e `descartada`,
  e imponho as transições por gatilho no banco, para que nenhum caminho de
  código consiga pular um estado. No máximo duas gerações coexistem além das
  descartadas: a reconstrução só começa sem `construindo` e sem `anterior`
  (INFRA 7.5).
- **Fonte da reconstrução (005 D-001, D-002; 003 TD-004):** escolhi o replay
  pela outbox, porque é a única fonte que respeita INFRA 6.7 e 4.11 ao mesmo
  tempo. O operador cria a candidata no coordenador e reabre a outbox no lado
  do livro com `relay-republicar --desde-posicao 1`; o relay republica e o
  worker aplica. Decidi pelo disparo manual (005 D-003).
- **Worker aplica a todas as gerações receptoras (005 TD-001):** cada
  mensagem é aplicada, numa transação, à ativa, à candidata e à anterior, com
  deduplicação por geração ([ADR-005](ADR-005-inbox-deduplicacao-quarentena.md)).
  Assim a candidata converge com o replay e com os lançamentos novos, e a
  ativa segue atendendo consultas.
- **Barreira por tabela de controle (005 TD-002):** para trocar, o
  coordenador marca `barreira = solicitada` em `controle_projecao`; cada worker
  fecha o consumo, termina a mensagem em curso e confirma a pausa em
  `estado_consumidor`. Sem confirmação de todos dentro do tempo limite
  configurável, a troca é cancelada e a barreira, liberada. Fiz assim para
  que o critério de troca seja avaliado com nada mudando por baixo.
- **Critério de troca (005 D-006):** candidata sem lacunas e com checkpoint
  maior ou igual ao da ativa, avaliado com os consumidores em pausa.
- **Troca por ponteiro (005 TD-003):** numa transação, a ativa vira
  `anterior`, a candidata vira `ativa` e `geracao_ativa_id` passa a apontar
  para ela. A API lê o ponteiro em cada consulta, sem reiniciar.
- **Rollback e descarte (005 D-005):** a `anterior` continua recebendo eventos
  e pode voltar a ser ativa pelo mesmo protocolo; o operador a descarta
  quando não precisar mais dela. Mantive-a recebendo eventos para que o
  rollback não perca o que chegou depois da troca.
- **Reconciliação (005 D-002, D-004):** o lado do livro exporta totais por
  data até uma posição (`livro-relatorio`), o relatório passa pelo operador e
  o coordenador compara com a ativa. Ela só reporta; a correção é a
  reconstrução. Fiz o relatório passar pelo operador porque os dois lados não
  têm rede em comum (INFRA 6.7).

## Alternativas consideradas

### Alternativa 1: Reconstruir e depois reaplicar um delta

- **Origem:** spec (005 TD-001).
- **Descrição:** congelar a posição, reconstruir a candidata até ela e, antes
  da troca, reaplicar os eventos novos chegados no meio tempo.
- **Prós:** sem escrita multiplicada durante a reconstrução.
- **Contras:** mais passos e uma janela em que eventos novos podem faltar na
  candidata.
- **Por que rejeitei:** a aplicação a todas as gerações receptoras mantém a
  candidata em dia sem passo extra, com o mesmo código de projeção.

### Alternativa 2: Reconstrução no lugar (truncar e reprojetar)

- **Origem:** análise do ADR.
- **Descrição:** apagar a projeção atual e reprojetar a partir do replay.
- **Prós:** uma única geração; menos espaço em disco.
- **Contras:** consultas erradas ou vazias durante a reconstrução; sem
  rollback.
- **Por que rejeitei:** descumpre INFRA 2.5 e 7.4 (candidata separada e troca
  atômica) e INFRA 9.6 (rollback controlado).

### Alternativa 3: Troca por renomeação de tabelas ou esquemas

- **Origem:** análise do ADR.
- **Descrição:** construir em tabelas paralelas e trocar com `ALTER TABLE ...
  RENAME` ou `search_path`.
- **Prós:** sem coluna de geração nas chaves.
- **Contras:** DDL em runtime exige privilégio de dono das tabelas para o
  operador; a candidata não recebe os eventos novos sem código separado.
- **Por que rejeitei:** contraria o mínimo privilégio (INFRA 5.11) e complica
  a reconstrução concorrente.

### Alternativa 4: Componente externo de coordenação para a barreira

- **Origem:** análise do ADR (em 005 TD-002 registrei "sem novo componente").
- **Descrição:** serviço de locks distribuídos ou pausa dos consumidores pela
  API de administração do RabbitMQ.
- **Prós:** mecanismo genérico de coordenação.
- **Contras:** novo componente ou nova credencial administrativa no broker.
- **Por que rejeitei:** P-011; o próprio `pg-consolidado` já é o ponto de
  coordenação de worker e coordenador.

## Consequências

### Positivas

- As consultas continuam na ativa durante toda a reconstrução; a troca é
  instantânea para a API.
- Testei reconstrução concorrente, troca, rollback, troca recusada e
  recuperação após perda do RabbitMQ sobre o compose real
  ([`tests/e2e/test_reconstrucao.py`](../../tests/e2e/test_reconstrucao.py)).
- O livro é a única fonte necessária para recompor o consolidado.

### Negativas / trade-offs

- Aceitei escrita multiplicada durante a reconstrução (até três gerações
  receptoras).
- Preciso de espaço para duas gerações completas no `pg-consolidado`
  (INFRA 7.5); não medi esse espaço
  ([lacunas](../evolucoes.md#lacunas-conhecidas-da-implementação)).
- O consumo fica pausado enquanto a barreira está erguida; as mensagens
  esperam na fila e o atraso é absorvido quando os workers retomam.
- A operação é manual, em vários comandos; mitiguei o erro de ordem com as
  verificações do coordenador e com o runbook do README.

### Riscos

- Replay completo lento com volume alto de lançamentos; não medi além do
  volume dos testes locais. Minha conduta é medir antes de escalar o volume.
- Coordenador que cai com a barreira erguida deixa o consumo pausado. A
  próxima `geracao trocar` ou `geracao rollback` assume e libera a barreira;
  escrevi a conduta no runbook do README, mas não há alerta para esse caso.

## Notas de implementação

- O coordenador está em [`services/coordenador/`](../../services/coordenador/):
  é uma CLI sem porta (INFRA 6.4), com o papel `consolidado_operacao`, sob o
  perfil `operacao` do compose.
- A barreira do worker está em
  [`services/consolidado/aplicacao/barreira.py`](../../services/consolidado/aplicacao/barreira.py).
- Métricas `cashflow_geracao_ativa` e `cashflow_reconstrucao_fase`.
- Revejo esta decisão se surgir reconstrução automática, volume que torne o
  replay completo inviável (exigindo snapshot) ou mais de um consumidor de
  projeção.

## Referências

- [04 - Fluxos: reconstrução, troca, reconciliação e recuperação](../arquitetura/04-fluxos.md#6-reconstrução-da-projeção)
- [03 - Componentes: coordenador](../arquitetura/03-componentes.md#coordenador-operacional-servicescoordenador)
