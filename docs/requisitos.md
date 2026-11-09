# Requisitos funcionais e não funcionais refinados

Refinamento exigido em DES - Requisitos obrigatórios ("Refinamento do
Levantamento de requisitos funcionais e não funcionais"). Conteúdo transcrito
dos `requirements.md` aprovados por Eduardo em 2026-11-05 nas specs 001 a 005
e 007. As specs ficam em `.ai/sdd/specs/`, pasta local que não é versionada
(decisão 006 D-006); por isso o texto foi trazido para cá, e cada seção abaixo
corresponde a uma spec.

Fontes:

- DES = [`Desafio_Arquiteto_Soluções.md`](../Desafio_Arquiteto_Solu%C3%A7%C3%B5es.md)
- INFRA = [`Requisitos_de_Infraestrutura.md`](../Requisitos_de_Infraestrutura.md)

Convenções:

- O identificador de cada requisito é `<spec>/<id>`, por exemplo `002/FR-004`
  é o FR-004 da spec 002. Decisões seguem o mesmo padrão (`004/D-006`).
- Prioridade MoSCoW: Must Have, Should Have, Could Have.
- Requisitos funcionais em EARS (WHEN / IF / THE SYSTEM SHALL), condensados
  em uma frase.
- Origem: "Fonte" = DES ou INFRA; "Eduardo" = decisão do autor com data;
  "Inferência" = dedução direta de uma fonte; "Proposta" = não consta nas
  fontes e foi aprovada junto com a spec; `[NVI]` = não verificado.

Domínios e capacidades que estes requisitos atendem:
[`dominios-capacidades.md`](dominios-capacidades.md).

## Índice das specs

| Spec | Tema | Requisitos | Origem local (não versionada) |
|------|------|------------|-------------------------------|
| [001](#spec-001-plataforma-local-de-execução) | Plataforma local de execução | FR-001 a FR-010; NFR-001 a NFR-003 | `.ai/sdd/specs/001-plataforma-local/requirements.md` |
| [002](#spec-002-registro-e-listagem-de-lançamentos) | Registro e listagem de lançamentos | FR-001 a FR-010; NFR-001 a NFR-004 | `.ai/sdd/specs/002-registro-lancamentos/requirements.md` |
| [003](#spec-003-publicação-de-eventos-relay-da-outbox) | Publicação de eventos (relay da outbox) | FR-001 a FR-006; NFR-001 a NFR-003 | `.ai/sdd/specs/003-publicacao-eventos/requirements.md` |
| [004](#spec-004-consolidado-diário) | Consolidado diário | FR-001 a FR-009; NFR-001 a NFR-004 | `.ai/sdd/specs/004-consolidado-diario/requirements.md` |
| [005](#spec-005-reconstrução-da-projeção-coordenador-operacional) | Reconstrução da projeção (coordenador operacional) | FR-001 a FR-009; NFR-001 a NFR-003 | `.ai/sdd/specs/005-reconstrucao-projecao/requirements.md` |
| [007](#spec-007-observabilidade-com-métricas) | Observabilidade com métricas | FR-001 a FR-006; NFR-001 a NFR-002 | `.ai/sdd/specs/007-observabilidade-metricas/requirements.md` |

A spec 006 trata desta própria documentação e não tem requisitos de produto.

## Do enunciado aos requisitos refinados

| Enunciado (DES) | Requisitos refinados |
|-----------------|----------------------|
| Serviço que faça o controle de lançamentos | 002/FR-001 a 002/FR-010; 003/FR-001 a 003/FR-006 |
| Serviço do consolidado diário | 004/FR-001 a 004/FR-009; 005/FR-001 a 005/FR-009 |
| Lançamentos não ficam indisponíveis se o consolidado cair | 002/FR-005, 002/NFR-001, 004/NFR-002 |
| Consolidado com 50 req/s em pico e no máximo 5% de perda | 004/NFR-001, 007/NFR-001 |
| Segurança: autenticação, autorização, criptografia | 001/FR-004 a 001/FR-008, 002/FR-008, 004/FR-008, 005/FR-008, 007/FR-003, 007/FR-004 |
| Resiliência: recuperação de falhas, monitoramento proativo | 003/FR-003, 003/FR-005, 004/FR-002, 004/FR-003, 005/FR-001 a 005/FR-009, 007/FR-001 a 007/FR-006 |
| Como rodar localmente | 001/FR-001 a 001/FR-010 |

## Spec 001: plataforma local de execução

Ambiente local conteinerizado que hospeda os bancos, o RabbitMQ, o Keycloak e
os componentes da solução, com isolamento, persistência e segurança definidos
em INFRA. Fonte: INFRA §1, §3.3, §4, §5, §6, §7.1, §8.4, §8.5, Capacidade
local, Fora do escopo; DES - Requisitos obrigatórios.

### Requisitos funcionais

| ID | Prioridade | Requisito | Fonte |
|----|------------|-----------|-------|
| 001/FR-001 | Must Have | Execução local conteinerizada: o sistema executa em Linux com Docker Engine e Docker Compose v2, e o README avisa que macOS e Windows podem funcionar via Docker Desktop, sem garantia, para que o avaliador rode a solução localmente. | INFRA 1.1 a 1.5; DES; 001/D-003 |
| 001/FR-002 | Must Have | Bancos exclusivos por serviço: um banco exclusivo para o livro e outro para o consolidado, com volumes, credenciais, pools e migrações separados, para que nenhum serviço dependa dos dados do outro. | INFRA 3.1 a 3.3, 7.1 |
| 001/FR-003 | Must Have | RabbitMQ persistente: uma instância RabbitMQ local com volume persistente. | INFRA 4.1, 4.2 |
| 001/FR-004 | Must Have | Emissão de token com escopo mínimo: quando um cliente técnico apresenta credenciais válidas, o sistema emite access token JWT assinado assimetricamente contendo apenas os escopos concedidos a esse cliente, para que cada API aceite somente o uso autorizado. | INFRA 5.1 a 5.4 |
| 001/FR-005 | Must Have | Identidades técnicas separadas: identidades distintas para runtime das APIs, migrações, relay, worker, reconstrução e reconciliação, e administração do Keycloak. | INFRA 5.11 |
| 001/FR-006 | Must Have | Tráfego cifrado: APIs e Keycloak servidos apenas por TLS, com certificados emitidos por autoridade certificadora local de desenvolvimento. | INFRA 5.7, 5.8 |
| 001/FR-007 | Must Have | Exposição mínima de rede: bancos, RabbitMQ e componentes operacionais em redes internas; publicadas apenas APIs e Keycloak, preferencialmente em loopback. | INFRA 6.1 a 6.6 |
| 001/FR-008 | Must Have | Segredos fora do repositório: segredos carregados fora do Git e `.env.example` sem credenciais reais. | INFRA 5.9, 5.10 |
| 001/FR-009 | Must Have | Health e readiness da infraestrutura: health check e readiness, distinta de processo em execução, para bancos, RabbitMQ e Keycloak. | INFRA 8.4, 8.5 |
| 001/FR-010 | Must Have | Persistência entre reinícios: quando os contêineres reiniciam, o sistema preserva dados do livro, do consolidado, do RabbitMQ e a configuração necessária do Keycloak. | INFRA 7.1 |

### Requisitos não funcionais

- **001/NFR-001 Capacidade local:** baseline provisória de 4 a 8 vCPUs,
  16 GB de RAM, 30 GB livres, virtualização, espaço para duas gerações do
  consolidado e relógio sincronizado. `[NVI]` a confirmar por medição
  (INFRA - Capacidade local). Os recursos da máquina usada no teste de carga
  estão em [`testes/carga-consolidado.md`](testes/carga-consolidado.md).
- **001/NFR-002 Segurança e privacidade:** coberto por 001/FR-004 a
  001/FR-008.
- **001/NFR-003 Usabilidade:** instruções de execução claras no README (DES).

### Restrições e decisões

- Sem Kubernetes, nuvem ou clusters (INFRA - Fora do escopo). Keycloak deve
  ser substituível (INFRA 5.1).

| ID | Decisão | Origem |
|----|---------|--------|
| 001/D-001 | Sem API Gateway, RBAC ou ABAC: autorização apenas por escopo validado em cada API. | INFRA 5.12 |
| 001/D-002 | Sem metas numéricas de RPO, RTO, retenção e backup: documentadas como pendentes, sem política numérica implementada. | INFRA 7.7; Fora do escopo |
| 001/D-003 | Linux suportado e testado; o README avisa que macOS e Windows podem funcionar via Docker Desktop, sem garantia. | Eduardo, 2026-11-05 (001/Q-001) |

Fora do escopo: Kubernetes, nuvem, alta disponibilidade, clusters,
balanceador, autoscaling, API Gateway (INFRA - Fora do escopo); RBAC e ABAC
(INFRA 5.12); RPO, RTO, SLA e política numérica de backup (INFRA 7.7).

## Spec 002: registro e listagem de lançamentos

Serviço que registra lançamentos de débito e crédito do comerciante, de forma
imutável e idempotente, permanece disponível mesmo com o consolidado ou o
RabbitMQ fora do ar, e lista os lançamentos por intervalo de datas. Fonte:
DES - Descritivo, Requisitos de negócio, RNF; INFRA 2.1, 3.1, 5.4, 5.5, 8.2,
8.3, 9.6; decisões de Eduardo em 2026-11-05.

### Requisitos funcionais

| ID | Prioridade | Requisito | Fonte |
|----|------------|-----------|-------|
| 002/FR-001 | Must Have | Registrar lançamento: quando um cliente técnico autorizado envia um lançamento válido com tipo, valor, data e descrição opcional, o sistema registra o lançamento e devolve confirmação com seu identificador. | DES; INFRA 2.1; 002/D-001 |
| 002/FR-002 | Must Have | Validar lançamento: se o tipo não for débito nem crédito, ou o valor não for positivo com até duas casas decimais, ou a data for inválida, ou faltar campo obrigatório, o sistema rejeita a requisição informando o motivo, sem registrar nada. | DES; 002/D-001, 002/D-004 |
| 002/FR-003 | Must Have | Imutabilidade: o sistema impede alteração e exclusão de lançamento registrado; correção é feita por novo lançamento de tipo oposto. | INFRA 3.1; 002/D-003 |
| 002/FR-004 | Must Have | Resposta idempotente: quando uma requisição repete a mesma chave de idempotência e o mesmo conteúdo, o sistema devolve a resposta original sem criar novo lançamento; se a chave se repete com conteúdo diferente, rejeita como conflito. | INFRA 3.1; 002/D-006 |
| 002/FR-005 | Must Have | Confirmação independente: o sistema confirma lançamentos sem depender da disponibilidade do consolidado nem do RabbitMQ. | DES - RNF; INFRA 2.1 |
| 002/FR-006 | Must Have | Disponibilizar lançamento ao consolidado sem perda: quando um lançamento é confirmado, o sistema garante que ele será disponibilizado ao consolidado, mesmo que a publicação ocorra depois. | INFRA 2.2, 3.1, 4.10 |
| 002/FR-007 | Must Have | Ordem verificável: o sistema atribui a cada lançamento uma posição crescente e única no livro, para que o consolidado detecte lacunas. | INFRA 3.1, 8.3 |
| 002/FR-008 | Must Have | Autorização por escopo: registro somente com `lancamentos:registrar` e listagem somente com `lancamentos:consultar`, em access token válido. | INFRA 5.4, 5.5; 002/D-002 |
| 002/FR-009 | Must Have | Listar lançamentos por intervalo: quando um cliente autorizado solicita lançamentos de um intervalo de datas, o sistema retorna, em páginas, os lançamentos cuja data esteja no intervalo. | DES ("controle de lançamentos"); 002/D-002 |
| 002/FR-010 | Must Have | Dia do lançamento: o sistema usa a data informada no lançamento, interpretada no fuso America/Sao_Paulo, como o dia do lançamento. | 002/D-005 |

### Requisitos não funcionais

- **002/NFR-001 Confiabilidade:** disponibilidade do registro independe do
  consolidado e do RabbitMQ (DES - RNF).
- **002/NFR-002 Desempenho:** meta numérica para registro e listagem não
  consta nas fontes. A meta de 50 req/s se aplica ao consolidado (DES).
- **002/NFR-003 Segurança e privacidade:** TLS (INFRA 5.7); validação JWT na
  API com cache controlado de JWKS (INFRA 5.5, 5.6); logs sem token, segredo
  e conteúdo financeiro (INFRA 8.2).
- **002/NFR-004 Observabilidade:** logs estruturados e correlacionados (INFRA
  8.1); métricas de pool de conexões e falhas de JWT/JWKS, expostas ao
  Prometheus (INFRA 8.3; spec 007); health e readiness (INFRA 8.4, 8.5).

### Restrições e decisões

- Banco exclusivo do livro; sem transação distribuída (INFRA 3.3). Domínio e
  aplicação hexagonais (INFRA 2.1).

| ID | Decisão | Origem |
|----|---------|--------|
| 002/D-001 | Campos do lançamento: tipo (débito ou crédito), valor, data do lançamento e descrição opcional; sem categoria. | Eduardo, 2026-11-05 (002/Q-002) |
| 002/D-002 | Listagem por intervalo de datas, paginada, com o escopo `lancamentos:consultar`, adicional aos escopos de INFRA 5.4. | Eduardo, 2026-11-05 (002/Q-001) |
| 002/D-003 | Sem estorno vinculado; o erro é corrigido com novo lançamento de tipo contrário. | Eduardo, 2026-11-05 (002/Q-003) |
| 002/D-004 | Moeda BRL, duas casas decimais, valor positivo; o tipo define o sinal. | Eduardo, 2026-11-05 (002/Q-004) |
| 002/D-005 | Dia = data informada no lançamento, fuso America/Sao_Paulo; lançamento retroativo é permitido e pode alterar o saldo de dias passados. | Eduardo, 2026-11-05 (002/Q-005) |
| 002/D-006 | Mesma chave de idempotência com conteúdo diferente é rejeitada como conflito. | Eduardo, 2026-11-05 (002/Q-006) |

Fora do escopo: alteração ou exclusão de lançamento (INFRA 3.1); estorno
vinculado (002/D-003); multimoeda e categoria (002/D-001, 002/D-004);
multiempresa (INFRA - Fora do escopo); interface de usuário (as fontes
definem apenas APIs).

## Spec 003: publicação de eventos (relay da outbox)

Componente que leva cada evento da outbox do livro ao RabbitMQ, com
confirmação do broker, retentativas e recuperação após perda do broker.
Fonte: INFRA 2.2, 3.1, 4.3 a 4.7, 4.10, 4.11, 7.2, 7.6, 8.3, 8.4, 9.2, 9.6.

### Requisitos funcionais

| ID | Prioridade | Requisito | Fonte |
|----|------------|-----------|-------|
| 003/FR-001 | Must Have | Publicar eventos pendentes: quando existir evento pendente na outbox, o sistema o publica no RabbitMQ como mensagem persistente em exchange durável. | INFRA 2.2, 4.3, 4.5 |
| 003/FR-002 | Must Have | Confirmar antes de marcar: o evento é marcado como publicado somente após publisher confirm positivo e sem retorno de mensagem não roteada (`mandatory`). | INFRA 2.2, 4.6, 4.7 |
| 003/FR-003 | Must Have | Retentativa controlada: se a publicação falhar, não for confirmada ou retornar como não roteada, o sistema mantém o evento pendente e tenta novamente conforme política controlada. | INFRA 2.2, 4.10; 003/D-002 |
| 003/FR-004 | Must Have | Exclusividade por tentativa: o sistema impede que duas execuções do relay publiquem o mesmo evento na mesma tentativa. | INFRA 2.2 ("controle de lease por tentativa") |
| 003/FR-005 | Must Have | Republicação a partir da outbox: quando o operador solicita republicação a partir de uma posição, o sistema republica os eventos da outbox a partir dessa posição. | INFRA 6.4, 7.2, 7.6 |
| 003/FR-006 | Must Have | Sem descarte silencioso: todo evento com falha persistente é registrado e exposto em métrica, sem ser removido da outbox. | INFRA 4.10 |

### Requisitos não funcionais

- **003/NFR-001 Observabilidade:** métricas de backlog da outbox, idade do
  evento mais antigo, mensagens prontas e não confirmadas (INFRA 8.3); health
  e readiness do relay (INFRA 8.4, 8.5).
- **003/NFR-002 Segurança:** identidade própria no banco e no RabbitMQ (INFRA
  5.11); interface de republicação com acesso restrito (INFRA 6.4).
- **003/NFR-003 Confiabilidade:** queda do relay não perde eventos, pois a
  outbox é preservada (INFRA 7.2).

### Restrições e decisões

- RabbitMQ não é fonte de verdade (INFRA 4.11).

| ID | Decisão | Origem |
|----|---------|--------|
| 003/D-001 | Eventos publicados não são apagados da outbox, para permitir replay e reconstrução. | INFRA 7.2, 7.3 |
| 003/D-002 | Retentativa sem limite de tentativas, com espera crescente até um teto configurável; alerta pela idade do evento mais antigo. | Eduardo, 2026-11-05 (003/Q-001) |
| 003/D-003 | O relay não garante ordem; o consolidado soma contribuições e detecta lacunas pela posição. | Eduardo, 2026-11-05 (003/Q-002) |

Fora do escopo: cluster RabbitMQ (INFRA - Fora do escopo); entrega
exatamente uma vez no broker, pois a deduplicação é do consumidor (INFRA
2.4).

## Spec 004: consolidado diário

Consumidor que projeta cada lançamento no saldo do dia correspondente, sem
duplicar nem descartar, e API que disponibiliza o saldo diário consolidado,
por data ou por intervalo, e o sinal de inclusão de um lançamento. Fonte:
DES - Descritivo, Requisitos de negócio, RNF; INFRA 2.3, 2.4, 3.2, 4.8 a
4.10, 5.4, 8.3, 9.4, 9.6; decisões de Eduardo em 2026-11-05.

### Requisitos funcionais

| ID | Prioridade | Requisito | Fonte |
|----|------------|-----------|-------|
| 004/FR-001 | Must Have | Projetar cada lançamento uma única vez: quando um evento de lançamento é recebido, se o evento e o lançamento ainda não foram processados, o sistema aplica o lançamento ao saldo do dia correspondente (data do lançamento, America/Sao_Paulo), para que o saldo não seja duplicado por reentrega. | INFRA 2.4; 002/D-005 |
| 004/FR-002 | Must Have | Confirmar recebimento após persistir: a mensagem só é confirmada ao RabbitMQ após a persistência do seu efeito. | INFRA 2.4, 4.8 |
| 004/FR-003 | Must Have | Quarentena de conflitos: se um evento ou lançamento já processado chega com conteúdo diferente, ou a mensagem é ilegível, o sistema o guarda em quarentena durável, sem aplicá-lo e sem descartá-lo. | INFRA 2.4, 4.9, 4.10 |
| 004/FR-004 | Must Have | Consultar saldo de uma data: para uma data com lançamentos, o sistema retorna total de créditos, total de débitos, saldo do dia, saldo acumulado até a data e o checkpoint; se a data não tem lançamentos consolidados, responde "não encontrado". | DES; INFRA 2.3; 004/D-001, 004/D-005, 004/D-006 |
| 004/FR-005 | Must Have | Consultar saldos de um intervalo: o sistema retorna, em páginas, um item por dia com lançamentos, com os mesmos campos de 004/FR-004, omitindo dias sem lançamentos e sem limite de tamanho do intervalo. | 004/D-002, 004/D-007 |
| 004/FR-006 | Must Have | Consultar sinal de inclusão: quando um cliente autorizado consulta um lançamento, o sistema informa se ele já foi incluído no consolidado. | INFRA 2.3, 3.2 |
| 004/FR-007 | Must Have | Ler apenas a geração ativa: as consultas são respondidas somente com dados da geração ativa. | INFRA 2.3 |
| 004/FR-008 | Must Have | Autorização por escopo: consultas aceitas somente com token válido contendo `consolidado:consultar`. | INFRA 5.4, 5.5 |
| 004/FR-009 | Must Have | Detectar lacunas: o sistema registra até que posição do livro a projeção está contínua e quais posições faltam. | INFRA 3.2, 8.3 |

### Requisitos não funcionais

- **004/NFR-001 Desempenho:** em pico, 50 req/s com no máximo 5% de perda
  (DES). Protocolo: chegada fixa de 50 consultas/s por 300 s, total 15.000;
  aprovação com pelo menos 14.250 respostas de sucesso em até 10 s cada
  (INFRA 9.4; critério de 10 s: inferência). Cenário: consulta de saldo por
  data (004/FR-004) com datas existentes (Proposta). Resultado medido:
  [`testes/carga-consolidado.md`](testes/carga-consolidado.md).
- **004/NFR-002 Confiabilidade:** queda do consolidado não afeta o registro
  de lançamentos (DES); tolera queda do consumidor, perda de ACK e
  duplicação (INFRA 9.6).
- **004/NFR-003 Segurança e privacidade:** TLS; JWT validado na API; logs sem
  conteúdo financeiro (INFRA 5.5, 5.7, 8.2).
- **004/NFR-004 Observabilidade:** métricas de reentregas, quarentena, pools,
  geração ativa, checkpoint, lacunas e falhas de JWT/JWKS, expostas ao
  Prometheus (INFRA 8.3; spec 007); idade do evento mais antigo visível como
  indicador de defasagem (004/D-004); health e readiness da API e do worker
  (INFRA 8.4, 8.5).

### Restrições e decisões

- Sem acesso ao banco do livro (INFRA 6.7).

| ID | Decisão | Origem |
|----|---------|--------|
| 004/D-001 | Conteúdo do saldo: total de créditos, total de débitos, saldo do dia e saldo acumulado até a data. | Eduardo, 2026-11-05 (004/Q-001) |
| 004/D-002 | Além da consulta por data, consulta por intervalo sem tamanho máximo, com paginação. | Eduardo, 2026-11-05 (004/Q-003) |
| 004/D-003 | Dia do lançamento segue 002/D-005: data informada, fuso America/Sao_Paulo. | 004/Q-002 |
| 004/D-004 | Sem SLA de defasagem; visibilidade pelo sinal de inclusão, pelo checkpoint e pela idade do evento mais antigo. | Eduardo, 2026-11-05 (004/Q-004) |
| 004/D-005 | A resposta de saldo informa o checkpoint da geração ativa. | Eduardo, 2026-11-05 (004/Q-005) |
| 004/D-006 | Consulta por data sem lançamentos consolidados responde "não encontrado". | Eduardo, 2026-11-05 (004/Q-006) |
| 004/D-007 | No intervalo, dias sem lançamentos não aparecem; intervalo vazio retorna lista vazia. | Eduardo, 2026-11-05 (004/Q-007) |
| 004/D-008 | Quarentena persistida no banco do consolidado. | INFRA 3.2, 4.9 |

Fora do escopo: reconstrução e troca de geração (spec 005); cache, se o
teste de carga não exigir; meta numérica de defasagem (004/D-004);
multiempresa (INFRA - Fora do escopo).

## Spec 005: reconstrução da projeção (coordenador operacional)

Capacidade operacional de reconciliar o consolidado com o livro e reconstruir
a projeção em geração separada, trocando a geração ativa de forma atômica e
reversível, sem interromper consultas. Fonte: DES - Resiliência; INFRA 2.5,
3.2, 3.3, 5.11, 6.4, 6.7, 7.2 a 7.6, 8.3, 9.6.

### Requisitos funcionais

| ID | Prioridade | Requisito | Fonte |
|----|------------|-----------|-------|
| 005/FR-001 | Must Have | Reconciliação: quando o operador solicita reconciliação, o sistema compara o consolidado com o livro e reporta divergências por data e lacunas de posição. | INFRA 2.5, 8.3; 005/D-002, 005/D-004 |
| 005/FR-002 | Must Have | Geração candidata separada: quando o operador inicia reconstrução, o sistema constrói a projeção em geração candidata separada da ativa. | INFRA 7.4 |
| 005/FR-003 | Must Have | Consultas inalteradas durante reconstrução: as consultas permanecem na geração ativa durante toda a reconstrução. | INFRA 2.3 |
| 005/FR-004 | Must Have | Barreira de consumidores: antes de trocar a geração ativa, o sistema pausa os consumidores e garante que nenhuma mensagem esteja em processamento. | INFRA 2.5 |
| 005/FR-005 | Must Have | Troca atômica: a geração candidata se torna ativa em uma única operação atômica. | INFRA 2.5 |
| 005/FR-006 | Must Have | Rollback controlado: quando o operador solicita rollback, o sistema restaura a geração anterior como ativa. | INFRA 9.6; 005/D-005 |
| 005/FR-007 | Must Have | Reconstrução concorrente sem perda: a geração candidata inclui os lançamentos registrados durante a reconstrução. | INFRA 9.6 ("reconstrução concorrente") |
| 005/FR-008 | Must Have | Acesso restrito: reconciliação, replay e reconstrução são permitidos somente à identidade técnica dedicada. | INFRA 5.11, 6.4 |
| 005/FR-009 | Must Have | Critério de aceite da candidata: a troca só ocorre se a candidata estiver sem lacunas e com checkpoint igual ou superior ao da ativa no momento da barreira. | INFRA 2.5, 3.2, 8.3; 005/D-006 |

### Requisitos não funcionais

- **005/NFR-001 Capacidade:** espaço temporário para duas gerações (INFRA
  7.5).
- **005/NFR-002 Observabilidade:** métricas de geração ativa, fase da
  reconstrução, checkpoint e lacunas (INFRA 8.3).
- **005/NFR-003 Confiabilidade:** livro e outbox como fonte para
  reconstrução; RabbitMQ não é histórico (INFRA 4.11, 7.2, 7.3).

### Restrições e decisões

- Sem SQL cruzado, sem acesso do consolidado ao livro, sem transação
  distribuída (INFRA 3.3, 6.7).

| ID | Decisão | Origem |
|----|---------|--------|
| 005/D-001 | A fonte da reconstrução é o livro via outbox, nunca o histórico do RabbitMQ. | INFRA 4.11, 7.2, 7.6 |
| 005/D-002 | Comando restrito no lado do livro reabre a outbox; o relay republica; o worker aplica na geração candidata. A reconciliação compara o relatório de totais exportado pelo lado do livro com o da projeção. | Eduardo, 2026-11-05 (005/Q-001) |
| 005/D-003 | Reconstrução disparada manualmente pelo operador. | Eduardo, 2026-11-05 (005/Q-002) |
| 005/D-004 | A reconciliação não corrige; a correção é feita por reconstrução. | Eduardo, 2026-11-05 (005/Q-003) |
| 005/D-005 | Após a troca, a geração anterior fica disponível para rollback até o operador descartá-la; nova reconstrução exige o descarte antes. | Eduardo, 2026-11-05 (005/Q-004) |
| 005/D-006 | Candidata aceita se estiver sem lacunas e com checkpoint maior ou igual ao da ativa no momento da barreira. | Eduardo, 2026-11-05 (005/Q-005) |
| 005/D-007 | O coordenador operacional faz parte do MVP. | Eduardo, 2026-11-05 (PD-001) |

Fora do escopo: disaster recovery produtivo; RPO e RTO numéricos (INFRA -
Fora do escopo); acesso direto do consolidado ao banco do livro (INFRA 6.7).

## Spec 007: observabilidade com métricas

Coleta das métricas de INFRA 8.3 de todos os componentes e exibição em
painéis, para o operador e o avaliador verem a saúde da solução. Fonte: DES -
Resiliência ("monitoramento proativo"), Diferenciais ("Monitoramento e
Observabilidade"); INFRA 8.2, 8.3, 8.4, 8.6; decisão de Eduardo em 2026-11-05
(ST-Q-008).

### Requisitos funcionais

| ID | Prioridade | Requisito | Fonte |
|----|------------|-----------|-------|
| 007/FR-001 | Must Have | Coletar métricas de todos os componentes: o sistema coleta as métricas de INFRA 8.3 de APIs, relay, worker, coordenador, RabbitMQ e PostgreSQL. Lista de INFRA 8.3: backlog da outbox; idade do evento mais antigo; mensagens prontas e não confirmadas; reentregas; quarentena; pools de conexão; espaço em disco; geração ativa; fase da reconstrução; checkpoint; lacunas; falhas de JWT e JWKS. | INFRA 8.3; ST-Q-008 |
| 007/FR-002 | Must Have | Painéis: painéis versionados que exibem cada métrica de INFRA 8.3, carregados automaticamente na subida. | ST-Q-008 |
| 007/FR-003 | Must Have | Acesso restrito: visualização publicada somente em loopback, com TLS; coleta mantida em redes internas, com credenciais fora do Git. | INFRA 5.7, 5.9, 6.1, 6.6 |
| 007/FR-004 | Must Have | Sem dados sensíveis em métricas: nomes e rótulos de métricas não incluem valores financeiros, tokens ou identificadores de cliente. | INFRA 8.2 |
| 007/FR-005 | Must Have | Health da observabilidade: health e readiness do coletor e da visualização. | INFRA 8.4, 8.5 (extensão aos novos componentes) |
| 007/FR-006 | Could Have | Regras de alerta visíveis: o sistema avalia regras de alerta para idade do evento mais antigo acima de um limite configurável, quarentena maior que zero e lacunas maiores que zero, exibindo-as no painel. | DES - "monitoramento proativo"; regras: Proposta |

### Requisitos não funcionais

- **007/NFR-001 Desempenho:** a coleta não pode afetar a meta de 50 req/s do
  consolidado; verificado no teste de carga com a coleta ativa (004-T6).
- **007/NFR-002 Segurança:** coberto por 007/FR-003 e 007/FR-004.

### Restrições e decisões

- Sem novos serviços expostos fora de loopback (INFRA 6.5, 6.6).

| ID | Decisão | Origem |
|----|---------|--------|
| 007/D-001 | Prometheus como backend de métricas e Grafana como visualização, na Phase 1. | Eduardo, 2026-11-05 (ST-Q-008) |

Fora do escopo: tracing distribuído (não consta nas fontes); notificação
externa de alertas (e-mail, chat); alta disponibilidade do Prometheus ou do
Grafana (INFRA - Fora do escopo).
