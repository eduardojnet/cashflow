# ADR-001: Serviços independentes com bancos exclusivos e integração assíncrona

> Status: Aceito
> Data: 2026-11-05
> Relacionado: INFRA 2.1, 3.3, 5.11, 6.7; ST-TD-001; 001 TD-002, TD-003; 002 TD-003; P-001, P-004, P-007

## Contexto

DES descreve dois serviços de negócio, controle de lançamentos e consolidado
diário, e impõe um requisito não funcional que, na minha leitura, molda todo
o desenho: "o serviço de controle de lançamento não deve ficar indisponível
se o sistema de consolidado diário cair". O mesmo DES me pede escolher um
padrão arquitetural (microsserviços, monolitos, SOA ou serverless) e
justificar o trade-off entre simplicidade e flexibilidade.

INFRA fixa as restrições de isolamento que precisei respeitar: banco
PostgreSQL exclusivo para cada lado (3.1, 3.2); volumes, credenciais, pools e
migrações separados, sem SQL cruzado, sem chave estrangeira entre serviços e
sem transação distribuída (3.3); consolidado sem acesso direto ao banco do
livro (6.7); identidades técnicas separadas por função (5.11). O consolidado
ainda precisa suportar 50 req/s em pico com no máximo 5% de perda (DES), e
essa carga não pode afetar o registro de lançamentos.

## Decisão

Decompus a solução em serviços independentes por domínio, cada um com o seu
banco, integrados de forma assíncrona por eventos. Escolhi esse corte porque
ele atende ao mesmo tempo à independência que DES exige e às restrições de
INFRA §3, dentro da execução local que INFRA 1.1 a 1.5 pede:

- **Lado do livro:** `lancamentos-api` (registro e listagem) e `relay` (publica
  a outbox), sobre `pg-livro`.
- **Lado do consolidado:** `worker` (projeta os eventos), `consolidado-api`
  (consultas) e `coordenador` (reconstrução e reconciliação), sobre
  `pg-consolidado`.
- **Integração:** só pelo evento `LancamentoRegistrado` v1, do livro para o
  consolidado, via outbox, relay e RabbitMQ (detalhes em
  [ADR-003](ADR-003-outbox-transacional-e-relay.md) e
  [ADR-004](ADR-004-rabbitmq-confirms-ack-apos-commit.md)). Não deixei
  nenhuma chamada síncrona entre os lados, porque qualquer uma recriaria a
  dependência que DES proíbe.
- **Isolamento físico:** separei volumes, credenciais, pools e árvores de
  migração por banco, e não liguei por rede o lado do consolidado a
  `pg-livro` (001 TD-002). Assim o isolamento não depende de ninguém lembrar
  de não acessar o outro banco.
- **Identidades por função (001 TD-003):** criei um papel PostgreSQL por
  função, com privilégio mínimo: `livro_api`, `livro_migracao`,
  `livro_relay`, `livro_replay`, `livro_monitoramento`, `consolidado_api`
  (somente leitura), `consolidado_worker`, `consolidado_migracao`,
  `consolidado_operacao` e `consolidado_monitoramento`. Uso o privilégio
  também para garantir regra de negócio: `livro_api` só tem `SELECT` e
  `INSERT` em `lancamento`, o que torna o lançamento imutável
  independentemente do código (002 TD-003).

No vocabulário de DES, escolhi microsserviços na menor escala que atende aos
requisitos: dois domínios, cinco processos de aplicação, um nó. Fiquei nessa
escala porque não acrescento componente sem requisito que o exija (P-011).

## Alternativas consideradas

### Alternativa 1: Monolito com banco único

- **Origem:** análise do ADR (DES lista monolitos entre os padrões).
- **Descrição:** uma aplicação com os módulos de lançamentos e consolidado,
  um banco compartilhado e o saldo atualizado na mesma transação do
  lançamento.
- **Prós:** consistência imediata do saldo; um processo, um banco, menos
  infraestrutura.
- **Contras:** queda ou sobrecarga do consolidado derruba ou degrada o
  registro; a carga de 50 req/s de consulta disputa recursos com a escrita.
- **Por que rejeitei:** descumpre DES (lançamentos independentes do
  consolidado) e INFRA 3.1, 3.2 e 3.3 (bancos exclusivos e isolados).

### Alternativa 2: Serviços separados com chamada síncrona

- **Origem:** análise do ADR; listei como anti-padrão no steering de
  convenções.
- **Descrição:** `lancamentos-api` chama a API do consolidado por HTTP a cada
  registro.
- **Prós:** saldo atualizado no fim da requisição; sem broker.
- **Contras:** acoplamento temporal: com o consolidado fora, o registro falha
  ou precisa de fila local, que é o problema que a outbox já resolve.
- **Por que rejeitei:** viola INFRA 2.1 e P-001 (confirmação independente do
  consolidado e do RabbitMQ).

### Alternativa 3: Banco único com esquemas separados por serviço

- **Origem:** análise do ADR.
- **Descrição:** uma instância PostgreSQL com um esquema por serviço e
  usuários distintos.
- **Prós:** menos contêineres e volumes; backup único.
- **Contras:** falha, manutenção ou saturação da instância atinge os dois
  lados; o isolamento depende só de permissões.
- **Por que rejeitei:** INFRA 3.1 e 3.2 pedem instâncias exclusivas e INFRA
  3.3, volumes separados.

### Alternativa 4: Serverless

- **Origem:** análise do ADR (DES lista serverless).
- **Descrição:** funções gerenciadas por evento e banco gerenciado em nuvem.
- **Prós:** escala automática; sem servidor para operar.
- **Contras:** depende de provedor de nuvem e não roda localmente como pede
  INFRA 1.1 a 1.5.
- **Por que rejeitei:** infraestrutura em nuvem e autoscaling estão em
  INFRA - Fora do escopo.

## Consequências

### Positivas

- O registro de lançamento depende só de `pg-livro` e do JWKS em cache:
  consolidado, worker, relay e RabbitMQ podem estar parados. Comprovei isso
  em [`tests/e2e/test_independencia_lancamentos.py`](../../tests/e2e/test_independencia_lancamentos.py).
- Cada lado escala, falha e é recuperado separadamente; o consolidado é
  reconstruível a partir do livro ([ADR-007](ADR-007-geracoes-barreira-troca-atomica.md)).
- Consigo verificar o isolamento: o host não alcança bancos nem RabbitMQ, e o
  lado do consolidado não alcança `pg-livro`
  ([`tests/infra/test_isolamento.py`](../../tests/infra/test_isolamento.py));
  testo os privilégios de cada papel em
  [`tests/infra/test_papeis_banco.py`](../../tests/infra/test_papeis_banco.py).

### Negativas / trade-offs

- Aceitei consistência eventual: o saldo consolidado fica atrás do livro. O
  cliente vê a defasagem pelo sinal de inclusão e pelo `posicao_checkpoint`
  (INFRA 2.3; 004 D-005); não fixei meta numérica de defasagem (004 D-004).
- Há mais componentes, credenciais e redes para operar do que num monolito;
  a reconciliação entre os lados passa pelo operador, porque não deixei rede
  comum (005 D-002).

### Riscos

- Divergência entre livro e consolidado por evento perdido. Mitiguei com
  posição sem lacunas e checkpoint ([ADR-006](ADR-006-posicao-monotonica-e-checkpoint.md)),
  reconciliação e reconstrução (spec 005).

## Notas de implementação

- Coloquei o código em `services/lancamentos`, `services/relay`,
  `services/consolidado` (worker e API na mesma imagem, com comandos
  diferentes) e `services/coordenador`.
- Crio os papéis em `infra/postgres/*/10-papeis.sh` e concedo os privilégios
  por tabela nas migrações Alembic de cada serviço.
- Revejo esta decisão se surgir necessidade de consistência imediata do
  saldo, ou um terceiro domínio que precise dos mesmos eventos.

## Referências

- [01 - Contexto](../arquitetura/01-contexto.md#tipo-de-arquitetura)
- [02 - Contêineres](../arquitetura/02-conteineres.md)
- [05 - Implantação local](../arquitetura/05-implantacao-local.md)
- [Requisitos 002 e 004](../requisitos.md)
