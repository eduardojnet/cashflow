# ADR-006: Posição monotônica e checkpoint

> Status: Aceito
> Data: 2026-11-05
> Relacionado: INFRA 2.3, 3.1, 3.2, 8.3; 002 TD-002; 004 TD-006; 003 D-003; 004 D-004, D-005

## Contexto

A entrega não preserva ordem (003 D-003) e pode perder mensagens já
confirmadas pelo broker em caso de perda do seu volume
([ADR-004](ADR-004-rabbitmq-confirms-ack-apos-commit.md)). O consolidado
precisa saber, sem ler o banco do livro (INFRA 6.7), se recebeu tudo até um
ponto. O cliente precisa avaliar a defasagem do saldo, já que não fixei meta
numérica de atraso (004 D-004). INFRA 3.1 lista um "contador de posição
monotônica" no livro; INFRA 3.2, checkpoints no consolidado; INFRA 8.3, as
métricas de checkpoint e de lacunas.

## Decisão

**No livro (002 TD-002).** Dou a cada lançamento uma posição tirada de uma
linha única, `contador_posicao`, bloqueada e incrementada dentro da
transação de registro. Mantive a posição contínua, porque rollback não
consome número. Com isso, uma lacuna de posição no consolidado significa
evento faltante, nunca um número descartado.

**No consolidado (004 TD-006).** Por geração, o checkpoint é a maior posição
contínua recebida. Aplico normalmente o evento fora de ordem e registro na
tabela `lacuna` as posições ausentes abaixo da maior recebida, até que
cheguem. Toda consulta de saldo devolve o `posicao_checkpoint` da geração
ativa (004 D-005), e o sinal de inclusão responde se um lançamento específico
já entrou no saldo (INFRA 2.3). Ofereço os dois sinais porque o checkpoint dá
a visão do conjunto e o sinal de inclusão responde ao cliente que acabou de
registrar um lançamento.

## Alternativas consideradas

### Alternativa 1: Sequence do PostgreSQL

- **Origem:** spec (002 TD-002).
- **Descrição:** `posicao` gerada por `nextval`.
- **Prós:** sem bloqueio; maior vazão de escrita.
- **Contras:** a sequence consome números em rollback, criando lacunas que
  nunca serão preenchidas.
- **Por que rejeitei:** cada rollback viraria um falso positivo de lacuna no
  consolidado, e o checkpoint pararia.

### Alternativa 2: Ordenar por instante de registro

- **Origem:** análise do ADR.
- **Descrição:** usar `registrado_em` como referência de progresso.
- **Prós:** sem contador.
- **Contras:** instantes repetidos e relógio não são contínuos; não há como
  saber se falta algum evento entre dois instantes.
- **Por que rejeitei:** não permite detectar falta, que é o objetivo.

### Alternativa 3: Só o marcador de inclusão, sem checkpoint

- **Origem:** análise do ADR.
- **Descrição:** responder apenas se cada lançamento foi incluído.
- **Prós:** menos estado no consolidado.
- **Contras:** o cliente não tem visão do conjunto, e a reconstrução não tem
  critério objetivo de completude.
- **Por que rejeitei:** INFRA 3.2 e 8.3 pedem checkpoint e lacunas, e o
  critério de troca de geração depende deles (005 D-006).

## Consequências

### Positivas

- Lacuna é um sinal confiável de evento faltante, que uso no alerta
  `CashFlowLacunas`, na reconciliação e no critério de troca de geração
  ([ADR-007](ADR-007-geracoes-barreira-troca-atomica.md)).
- O cliente sabe até onde o saldo está atualizado sem consultar o livro.

### Negativas / trade-offs

- O bloqueio da linha do contador serializa os registros: a vazão de escrita
  fica limitada por essa linha. Aceitei esse limite porque as fontes não têm
  meta de vazão de escrita; também não a medi nesta entrega.
- Manter `lacuna` e `checkpoint` acrescenta escrita a cada evento no worker.

### Riscos

- Contenção no contador sob carga de escrita alta. Minha conduta prevista é
  medir antes de mudar; a alternativa seria particionar a posição, o que
  muda o significado de lacuna.

## Notas de implementação

- Livro: [`unidade_de_trabalho.py`](../../services/lancamentos/adaptadores/postgres/unidade_de_trabalho.py);
  teste de concorrência em
  [`test_unidade_de_trabalho_livro.py`](../../services/lancamentos/tests/integracao/test_unidade_de_trabalho_livro.py).
- Consolidado: tabelas `checkpoint` e `lacuna`; testes em
  [`test_checkpoint_lacunas.py`](../../services/consolidado/tests/integracao/test_checkpoint_lacunas.py).
- Métricas `cashflow_checkpoint_posicao{geracao}` e `cashflow_lacunas{geracao}`.
- Revejo esta decisão se surgir meta de vazão de escrita que o contador único
  não atenda.

## Referências

- [04 - Fluxos: registro e consolidação](../arquitetura/04-fluxos.md#2-registro-de-lançamento)
- [03 - Componentes](../arquitetura/03-componentes.md)
