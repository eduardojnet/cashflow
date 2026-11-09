# Arquitetura alvo

Desenho da solução exigido em DES - Requisitos obrigatórios ("Desenho da
solução completo (Arquitetura alvo)"; "diagramas e fluxos de dados").
Conteúdo transcrito dos `design.md` aprovados por Eduardo em 2026-11-05 nas
specs 001 a 005 e 007, com as emendas de 2026-11-06, e conferido contra o
código e o `infra/compose.yaml` do repositório. As specs ficam em
`.ai/sdd/specs/`, pasta local que não é versionada (decisão 006 D-006).

Fontes:

- DES = [`Desafio_Arquiteto_Soluções.md`](../../Desafio_Arquiteto_Solu%C3%A7%C3%B5es.md)
- INFRA = [`Requisitos_de_Infraestrutura.md`](../../Requisitos_de_Infraestrutura.md)

Legenda de origem, a mesma de [`requisitos.md`](../requisitos.md): "Fonte" =
DES ou INFRA; "Eduardo" = decisão do autor com data; "Proposta" = não consta
nas fontes e foi aprovada junto com a spec; `[NVI]` = não verificado.

Diagramas em Mermaid dentro do Markdown, renderizados pelo GitHub (decisão
006 D-004), nos níveis de contexto, contêineres e componentes. Cada diagrama
vem precedido de um texto alternativo que descreve o mesmo conteúdo.

## Documentos

| Documento | Conteúdo |
|-----------|----------|
| [01 - Contexto](01-contexto.md) | Atores, fronteira do sistema, tipo de arquitetura e o que fica fora |
| [02 - Contêineres](02-conteineres.md) | Processos executáveis, bancos, broker, identidade e observabilidade; quem fala com quem |
| [03 - Componentes](03-componentes.md) | Estrutura hexagonal de cada serviço, modelos de dados e pacotes compartilhados |
| [04 - Fluxos](04-fluxos.md) | Registro, publicação, consolidação, consulta, autenticação, reconstrução, troca, rollback e reconciliação |
| [05 - Implantação local](05-implantacao-local.md) | Docker Compose, redes, portas, volumes, identidades técnicas, TLS, segredos, health checks e versões |

## Visão em uma página

- Dois serviços de negócio independentes, cada um com banco PostgreSQL
  exclusivo: **lançamentos** (livro de caixa, fonte de verdade) e
  **consolidado** (projeção do saldo diário) (INFRA 2.1, 2.3, 3.3).
- O registro grava lançamento e evento na mesma transação local (outbox); um
  **relay** publica os eventos no RabbitMQ com confirmação do broker (INFRA
  2.2, §4). O serviço de lançamentos não chama o RabbitMQ nem o consolidado,
  por isso continua confirmando lançamentos com eles fora do ar (DES - RNF;
  INFRA 2.1).
- Um **worker** consome a fila, deduplica (inbox e contribuição), projeta o
  saldo, guarda conflitos em quarentena e só confirma a mensagem após o
  commit (INFRA 2.4, 4.8).
- A projeção é versionada em **gerações**; o **coordenador** reconstrói uma
  geração candidata a partir do replay da outbox e troca a geração ativa de
  forma atômica, com barreira dos consumidores e rollback (INFRA 2.5, 7.4 a
  7.6).
- **Keycloak** emite tokens OAuth 2.0 Client Credentials; cada API valida o
  JWT localmente, com cache de JWKS, escopo e audiência (INFRA 5.1 a 5.6).
- **Prometheus** e **Grafana** coletam e exibem as métricas de INFRA 8.3
  (Eduardo, ST-Q-008; spec 007).
- Tudo roda localmente em um único Docker Compose, com redes internas por
  fronteira e só APIs, Keycloak e Grafana publicados em `127.0.0.1` com TLS
  (INFRA 1.5, 6.1 a 6.7).

As justificativas de cada escolha, com alternativas e trade-offs, ficam nos
ADRs em [`docs/adr/`](../adr/README.md); a tabela de origem desses ADRs está em
[01 - Contexto](01-contexto.md#decisões-que-moldam-a-arquitetura).
