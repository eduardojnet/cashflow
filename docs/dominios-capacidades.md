# Domínios funcionais e capacidades de negócio

Aqui apresento o mapeamento que DES exige em Requisitos obrigatórios
("Mapeamento de domínios funcionais e capacidades de negócio"). Transcrevi o
conteúdo do steering de produto do projeto, um artefato local que não
versiono (decisão 006 D-006).

Fontes:

- DES = [`Desafio_Arquiteto_Soluções.md`](../Desafio_Arquiteto_Solu%C3%A7%C3%B5es.md)
- INFRA = [`Requisitos_de_Infraestrutura.md`](../Requisitos_de_Infraestrutura.md)

Legenda de origem: "Inferência" = dedução direta de uma fonte; "decisão
minha" = decisão que tomei, registrada com data; `[NVI]` = não verificado.

## Problema

- O comerciante precisa registrar débitos e créditos e conhecer o saldo
  diário consolidado (DES).
- O controle de lançamentos não pode ficar indisponível se o consolidado
  diário cair (DES - Requisitos não funcionais).
- Em dias de pico, o consolidado recebe 50 requisições por segundo, com no
  máximo 5% de perda (DES - Requisitos não funcionais).

## Domínios e capacidades

Fonte: DES - Requisitos de negócio; INFRA §2. A classificação
Núcleo/Suporte/Genérico é inferência minha: chamei de núcleo os dois domínios
que DES descreve como o negócio, de suporte a operação que só existe para
mantê-los corretos, e de genérico a identidade, que qualquer provedor
padrão atende.

| Domínio | Classificação | Capacidades | Componentes (INFRA §2) |
|---------|---------------|-------------|------------------------|
| Lançamentos (livro de caixa) | Núcleo | Registrar lançamento; listar lançamentos por período; responder de forma idempotente; preservar lançamentos imutáveis; disponibilizar eventos para o consolidado | Serviço de lançamentos; Relay da outbox |
| Consolidado diário | Núcleo | Projetar saldo diário; consultar saldo; informar inclusão de lançamento no consolidado | Worker consumidor; Serviço de consolidado |
| Operação da projeção | Suporte | Reconciliar; reconstruir projeção; controlar gerações; trocar geração ativa | Coordenador operacional |
| Identidade e acesso | Genérico | Emitir tokens para clientes técnicos; validar tokens em cada API | Keycloak; validação JWT nas APIs |

Onde cada componente está no repositório:

| Componente | Pasta |
|------------|-------|
| Serviço de lançamentos | `services/lancamentos/` |
| Relay da outbox | `services/relay/` |
| Worker consumidor | `services/consolidado/worker/` (mesma imagem da API do consolidado) |
| Serviço de consolidado | `services/consolidado/` |
| Coordenador operacional | `services/coordenador/` |
| Validação JWT nas APIs | `packages/seguranca/` |
| Keycloak (realm versionado) | `infra/keycloak/` |

Os requisitos de cada domínio estão em [`requisitos.md`](requisitos.md):
lançamentos nas specs 002 e 003, consolidado diário na spec 004, operação da
projeção na spec 005, identidade e acesso na spec 001.

## Personas

| Persona | Quem é | Necessidades | Fonte |
|---------|--------|--------------|-------|
| Comerciante | Dono do fluxo de caixa | Registrar lançamentos; consultar saldo diário consolidado | DES |
| Cliente técnico | Sistema que consome as APIs, autenticado por OAuth 2.0 Client Credentials | Escopos mínimos `lancamentos:registrar` e `consolidado:consultar` (INFRA 5.4) e `lancamentos:consultar` | INFRA 5.2, 5.4; decisão minha (002/Q-001) |
| Operador | Responsável por reconciliação, reconstrução de projeções e controle de gerações | Interfaces de replay e reconstrução com acesso restrito | INFRA 2.5, 6.4 |
| Avaliador do desafio | Banca que avalia a entrega | Requisitos obrigatórios atendidos, README claro, repositório público e documentação no repositório | DES |

As fontes não descrevem as dores nem as alternativas atuais do comerciante,
e não as inventei `[NVI]`. Tomei Linux como o ambiente do avaliador; macOS e
Windows recebem aviso no README, sem garantia (decisão minha, 001/Q-001).

## Casos de uso principais

| Caso de uso | Persona principal | Valor | Fonte |
|-------------|-------------------|-------|-------|
| Registrar lançamento de débito ou crédito | Cliente técnico (pelo comerciante) | Controle de caixa | DES; INFRA 2.1 |
| Listar lançamentos por intervalo de datas | Cliente técnico (pelo comerciante) | Controle de lançamentos | DES; decisão minha (002/Q-001) |
| Consultar saldo diário consolidado (data ou intervalo) | Cliente técnico (pelo comerciante) | Relatório diário | DES; INFRA 2.3; decisão minha (004/Q-003) |
| Consultar se um lançamento já está no consolidado | Cliente técnico | Transparência da consistência eventual | INFRA 2.3 ("sinal de inclusão") |
| Reconciliar e reconstruir o consolidado | Operador | Recuperação de falhas | INFRA 2.5, 7.6 |

## Proposta de valor

O que ofereço ao comerciante: registro de lançamentos que continua
disponível com o consolidado fora do ar, saldo consolidado reconstruível a
partir do livro e nenhum descarte silencioso de evento financeiro (DES - RNF;
INFRA 2.1, 4.10, 4.11, 7.2).

## Fronteiras do produto

O que pus dentro do escopo:

- Serviço de controle de lançamentos e serviço de consolidado diário (DES).
- Componentes executáveis de INFRA §2.
- Documentação, testes e README exigidos em DES - Requisitos obrigatórios.

O que deixei fora, porque INFRA assim define (INFRA - Fora do escopo atual;
INFRA 5.12):

- Kubernetes; alta disponibilidade produtiva; múltiplas zonas ou regiões;
  balanceador de carga; API Gateway; infraestrutura em nuvem; multiempresa;
  disaster recovery produtivo; autoscaling; SLA, RPO ou RTO numéricos;
  cluster RabbitMQ; cluster PostgreSQL.
- RBAC e ABAC.

## Métricas de sucesso

| Métrica | Meta / sinal | Fonte |
|---------|--------------|-------|
| Disponibilidade de lançamentos com consolidado fora | Serviço de lançamentos continua confirmando lançamentos válidos | DES - RNF |
| Perda no consolidado em pico | Até 5% a 50 req/s. Protocolo: 15.000 consultas em 300 s; aprovado se pelo menos 14.250 forem respondidas com sucesso em até 10 s cada | DES - RNF; INFRA 9.4 (14.250 = 15.000 - 5%; li "observação individual por até 10 segundos" como limite de sucesso, por inferência) |
| Eventos financeiros descartados silenciosamente | 0 | INFRA 4.10 |

## Glossário do domínio

- **Lançamento:** registro imutável de débito ou crédito (DES; INFRA 3.1).
- **Livro:** base de verdade dos lançamentos (INFRA 3.1, 4.11).
- **Outbox:** registro, na base do livro, dos eventos a publicar (INFRA 2.2,
  3.1).
- **Inbox:** registro, na base do consolidado, dos eventos recebidos, para
  deduplicação (INFRA 2.4, 3.2).
- **Contribuição:** efeito de um lançamento no saldo de um dia (INFRA 3.2;
  definição: inferência).
- **Geração:** versão completa da projeção do consolidado; apenas uma é ativa
  (INFRA 2.3, 2.5, 7.4).
- **Quarentena:** armazenamento durável de eventos em conflito, sem descarte
  (INFRA 2.4, 4.9).
- **Sinal / marcador de inclusão:** indicação de que um lançamento já foi
  refletido no consolidado (INFRA 2.3, 3.2; definição: inferência).
- **Posição monotônica:** número crescente atribuído a cada lançamento no
  livro (INFRA 3.1).
- **Checkpoint / lacuna:** ponto de avanço da projeção e posições ausentes
  (INFRA 3.2, 8.3).
- **Barreira:** pausa coordenada dos consumidores antes da troca de geração
  (INFRA 2.5).
- **Lease:** reserva temporária de um evento para uma tentativa de
  publicação (INFRA 2.2; definição: inferência).
