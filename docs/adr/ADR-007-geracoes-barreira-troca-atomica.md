# ADR-007: Gerações da projeção, barreira e troca atômica

> Status: Aceito
> Data: 2026-11-05
> Relacionado: INFRA 2.3, 2.5, 6.4, 6.7, 7.4 a 7.6, 9.6; ST-TD-004; 005 TD-001 a TD-003; 005 D-001 a D-006; 003 TD-004; P-002, P-011

## Contexto

O saldo consolidado é uma projeção derivada do livro (P-002) e precisa poder
ser refeito: após defeito na projeção, após perda de mensagens no broker
(INFRA 7.6) ou para conferir o resultado. INFRA 2.5 pede um coordenador
operacional com reconstrução de projeções, controle de gerações, barreira de
consumidores e troca atômica da geração ativa; INFRA 2.3, que a API leia
somente a geração ativa; INFRA 7.4 e 7.5, geração candidata separada da ativa
e capacidade para duas gerações. INFRA 9.6 exige testar reconstrução
concorrente (com lançamentos novos chegando), troca de geração e rollback
controlado. A reconstrução não pode ler o banco do livro (INFRA 6.7) nem usar
o RabbitMQ como histórico (INFRA 4.11).

## Decisão

- **Gerações:** todas as tabelas da projeção são chaveadas por `geracao_id`.
  Estados: `construindo` (candidata), `ativa`, `anterior` e `descartada`, com
  as transições impostas por gatilho no banco. No máximo duas gerações
  coexistem além das descartadas: a reconstrução só começa sem `construindo`
  e sem `anterior` (INFRA 7.5).
- **Fonte da reconstrução (005 D-001, D-002; 003 TD-004):** replay pela
  outbox. O operador cria a candidata no coordenador e reabre a outbox no lado
  do livro com `relay-republicar --desde-posicao 1`; o relay republica e o
  worker aplica. Disparo manual (005 D-003).
- **Worker aplica a todas as gerações receptoras (005 TD-001):** cada
  mensagem é aplicada, numa transação, à ativa, à candidata e à anterior, com
  deduplicação por geração ([ADR-005](ADR-005-inbox-deduplicacao-quarentena.md)).
  A candidata converge com o replay e com os lançamentos novos, e a ativa
  segue atendendo consultas.
- **Barreira por tabela de controle (005 TD-002):** para trocar, o
  coordenador marca `barreira = solicitada` em `controle_projecao`; cada worker
  fecha o consumo, termina a mensagem em curso e confirma a pausa em
  `estado_consumidor`. Sem confirmação de todos dentro do tempo limite
  configurável, a troca é cancelada e a barreira, liberada.
- **Critério de troca (005 D-006):** candidata sem lacunas e com checkpoint
  maior ou igual ao da ativa, avaliado com os consumidores em pausa.
- **Troca por ponteiro (005 TD-003):** numa transação, a ativa vira
  `anterior`, a candidata vira `ativa` e `geracao_ativa_id` passa a apontar
  para ela. A API lê o ponteiro em cada consulta, sem reiniciar.
- **Rollback e descarte (005 D-005):** a `anterior` continua recebendo eventos
  e pode voltar a ser ativa pelo mesmo protocolo; o operador a descarta
  quando não precisar mais dela.
- **Reconciliação (005 D-002, D-004):** o lado do livro exporta totais por
  data até uma posição (`livro-relatorio`), o relatório passa pelo operador e
  o coordenador compara com a ativa. Só reporta; a correção é a
  reconstrução.

## Alternativas consideradas

### Alternativa 1: Reconstruir e depois reaplicar um delta

- **Origem:** spec (005 TD-001).
- **Descrição:** congelar a posição, reconstruir a candidata até ela e, antes
  da troca, reaplicar os eventos novos chegados no meio tempo.
- **Prós:** sem escrita multiplicada durante a reconstrução.
- **Contras:** mais passos e uma janela em que eventos novos podem faltar na
  candidata.
- **Por que foi rejeitada:** a aplicação a todas as gerações receptoras
  mantém a candidata em dia sem passo extra, com o mesmo código de projeção.

### Alternativa 2: Reconstrução no lugar (truncar e reprojetar)

- **Origem:** análise do ADR.
- **Descrição:** apagar a projeção atual e reprojetar a partir do replay.
- **Prós:** uma única geração; menos espaço em disco.
- **Contras:** consultas erradas ou vazias durante a reconstrução; sem
  rollback.
- **Por que foi rejeitada:** descumpre INFRA 2.5 e 7.4 (candidata separada e
  troca atômica) e INFRA 9.6 (rollback controlado).

### Alternativa 3: Troca por renomeação de tabelas ou esquemas

- **Origem:** análise do ADR.
- **Descrição:** construir em tabelas paralelas e trocar com `ALTER TABLE ...
  RENAME` ou `search_path`.
- **Prós:** sem coluna de geração nas chaves.
- **Contras:** DDL em runtime exige privilégio de dono das tabelas para o
  operador; a candidata não recebe os eventos novos sem código separado.
- **Por que foi rejeitada:** contraria o mínimo privilégio (INFRA 5.11) e
  complica a reconstrução concorrente.

### Alternativa 4: Componente externo de coordenação para a barreira

- **Origem:** análise do ADR (005 TD-002 registra "sem novo componente").
- **Descrição:** serviço de locks distribuídos ou pausa dos consumidores pela
  API de administração do RabbitMQ.
- **Prós:** mecanismo genérico de coordenação.
- **Contras:** novo componente ou nova credencial administrativa no broker.
- **Por que foi rejeitada:** P-011; o próprio `pg-consolidado` já é o ponto de
  coordenação de worker e coordenador.

## Consequências

### Positivas

- Consultas continuam na ativa durante toda a reconstrução; a troca é
  instantânea para a API.
- Reconstrução concorrente, troca, rollback, troca recusada e recuperação
  após perda do RabbitMQ testados sobre o compose real
  ([`tests/e2e/test_reconstrucao.py`](../../tests/e2e/test_reconstrucao.py)).
- O livro é a única fonte necessária para recompor o consolidado.

### Negativas / trade-offs

- Escrita multiplicada durante a reconstrução (até três gerações
  receptoras).
- Espaço para duas gerações completas no `pg-consolidado` (INFRA 7.5).
- Consumo pausado enquanto a barreira está erguida; as mensagens esperam na
  fila e o atraso é absorvido quando os workers retomam.
- Operação manual em vários comandos; erro de ordem é mitigado pelas
  verificações do coordenador e pelo runbook do README.

### Riscos

- Replay completo lento com volume alto de lançamentos; não medido além do
  volume dos testes locais. Mitigação: medir antes de escalar o volume.

## Notas de implementação

- Coordenador em [`services/coordenador/`](../../services/coordenador/),
  CLI sem porta (INFRA 6.4), papel `consolidado_operacao`, perfil `operacao`
  do compose.
- Barreira do worker em
  [`services/consolidado/aplicacao/barreira.py`](../../services/consolidado/aplicacao/barreira.py).
- Métricas `cashflow_geracao_ativa` e `cashflow_reconstrucao_fase`.
- Gatilho de revisão: reconstrução automática, volume que torne o replay
  completo inviável (exigindo snapshot) ou mais de um consumidor de projeção.

## Referências

- [04 - Fluxos: reconstrução, troca, reconciliação e recuperação](../arquitetura/04-fluxos.md#6-reconstrução-da-projeção)
- [03 - Componentes: coordenador](../arquitetura/03-componentes.md#coordenador-operacional-servicescoordenador)
