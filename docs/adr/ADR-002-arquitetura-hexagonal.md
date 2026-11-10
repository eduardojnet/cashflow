# ADR-002: Arquitetura hexagonal

> Status: Aceito
> Data: 2026-11-05
> Relacionado: INFRA 2.1; steering de convenções ("Architecture Patterns"); [ADR-001](ADR-001-servicos-independentes-bancos-exclusivos.md)

## Contexto

INFRA 2.1 exige "domínio e aplicação hexagonais" no serviço de lançamentos.
Os demais componentes (relay, worker, API do consolidado e coordenador)
também combinam regra de negócio com infraestrutura: PostgreSQL, RabbitMQ,
HTTP e validação de token. As regras que considero mais sensíveis na solução
(valor positivo com duas casas, efeito de débito e crédito no saldo,
detecção de conflito, critério de troca de geração) precisam ser testáveis
sem subir banco nem broker. Também quero poder trocar os adaptadores sem
tocar nessas regras, porque DES pede soluções "flexíveis e reutilizáveis".

## Decisão

Fiz todos os serviços seguirem portas e adaptadores:

- `dominio/`: entidades e regras só com a biblioteca padrão do Python, sem
  framework, banco ou broker.
- `aplicacao/`: casos de uso e as portas de que precisam, declaradas como
  `typing.Protocol`.
- `adaptadores/`: implementações das portas (HTTP com FastAPI, PostgreSQL com
  psycopg 3, RabbitMQ com aio-pika, validação JWT) e a montagem do processo.
- Mantenho a dependência sempre apontando para dentro: adaptadores conhecem
  a aplicação; a aplicação conhece o domínio e as portas; o domínio não
  conhece ninguém.
- Deixei o relay só com `aplicacao/` e `adaptadores/`, sem `dominio/`,
  porque ele não tem regra de negócio própria: só move eventos da outbox
  para o broker.

O padrão é obrigatório no serviço de lançamentos (INFRA 2.1). Estendi-o aos
demais componentes como Proposta registrada no steering de convenções, que
aprovei com as specs em 2026-11-05.

## Alternativas consideradas

### Alternativa 1: Camadas tradicionais com ORM

- **Origem:** análise do ADR.
- **Descrição:** controlador, serviço e repositório, com entidades mapeadas
  por um ORM.
- **Prós:** padrão difundido; menos código de mapeamento.
- **Contras:** a entidade passa a depender do ORM; o SQL explícito de que
  preciso para bloqueio de linha, `SKIP LOCKED` e privilégios por coluna
  fica escondido ou contornado.
- **Por que rejeitei:** não atende INFRA 2.1 no serviço de lançamentos, e o
  controle fino de transação e bloqueio é central nesta solução
  ([ADR-003](ADR-003-outbox-transacional-e-relay.md),
  [ADR-006](ADR-006-posicao-monotonica-e-checkpoint.md)).

### Alternativa 2: Script de transação na rota HTTP

- **Origem:** análise do ADR.
- **Descrição:** cada rota abre a transação e executa as regras e o SQL
  diretamente.
- **Prós:** menor quantidade de arquivos; adequado a CRUD simples.
- **Contras:** regra de negócio só testável com HTTP e banco; eu teria de
  repetir a mesma regra no worker e no coordenador, que não têm rota HTTP.
- **Por que rejeitei:** contraria INFRA 2.1 e dificulta testar as regras sem
  infraestrutura.

### Alternativa 3: Hexagonal só no serviço de lançamentos

- **Origem:** análise do ADR (é o mínimo literal de INFRA 2.1).
- **Descrição:** aplicar o padrão só onde INFRA exige.
- **Prós:** menos estrutura nos componentes menores, como o relay.
- **Contras:** dois estilos no mesmo repositório; regras do consolidado e do
  coordenador acopladas ao banco.
- **Por que rejeitei:** o consolidado e o coordenador concentram as regras
  de projeção e de gerações, as mais complexas da solução; com um estilo
  único, reduzo o custo de leitura do código.

## Consequências

### Positivas

- Testo as regras de domínio sem I/O, e verifico por teste, em cada serviço,
  que o domínio não tem dependência externa:
  [`test_dominio_sem_dependencias.py`](../../services/lancamentos/tests/unit/test_dominio_sem_dependencias.py),
  [`test_dominio_consolidado_sem_dependencias.py`](../../services/consolidado/tests/unit/test_dominio_consolidado_sem_dependencias.py),
  [`test_dominio_coordenador_sem_dependencias.py`](../../services/coordenador/tests/unit/test_dominio_coordenador_sem_dependencias.py).
- Testo os adaptadores de infraestrutura contra PostgreSQL, RabbitMQ e
  Keycloak reais (P-009), separados dos testes de domínio.
- Trocar o provedor de identidade, o broker ou o framework HTTP afeta só
  adaptadores.

### Negativas / trade-offs

- Aceitei mais arquivos e indireção do que um script de transação, e portas
  com uma única implementação em produção.
- Escrevo à mão o mapeamento entre domínio e SQL.

### Riscos

- Vazamento de detalhe de infraestrutura para o domínio com o tempo.
  Mitiguei com os testes de ausência de dependência acima, que falham se o
  domínio importar framework, banco ou broker.

## Notas de implementação

- Detalhei a estrutura de cada serviço em
  [03 - Componentes](../arquitetura/03-componentes.md#padrão-comum-portas-e-adaptadores).
- Deixei as migrações fora do hexágono, em `migracoes/` com Alembic e SQL
  explícito ([ADR-009](ADR-009-stack-python.md)).
- Revejo esta decisão se surgir componente sem regra de negócio própria (por
  exemplo, um exportador de dados), onde as camadas não trariam ganho.

## Referências

- [03 - Componentes](../arquitetura/03-componentes.md)
- [01 - Contexto: tipo de arquitetura](../arquitetura/01-contexto.md#tipo-de-arquitetura)
