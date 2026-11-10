# Arquitetura alvo

Aqui apresento o desenho da solução que DES exige em Requisitos obrigatórios
("Desenho da solução completo (Arquitetura alvo)"; "diagramas e fluxos de
dados"). Transcrevi o conteúdo dos `design.md` que aprovei em 2026-11-05 nas
specs 001 a 005 e 007, com as emendas de 2026-11-06, e o conferi contra o
código e o `infra/compose.yaml` do repositório. Mantenho as specs em
`.ai/sdd/specs/`, pasta local que não versiono (decisão 006 D-006).

Fontes:

- DES = [`Desafio_Arquiteto_Soluções.md`](../../Desafio_Arquiteto_Solu%C3%A7%C3%B5es.md)
- INFRA = [`Requisitos_de_Infraestrutura.md`](../../Requisitos_de_Infraestrutura.md)

Legenda de origem, a mesma de [`requisitos.md`](../requisitos.md): "Fonte" =
DES ou INFRA; "decisão minha" = decisão que tomei, com data; "Proposta" = não
consta nas fontes e aprovei junto com a spec; `[NVI]` = não verificado.

Desenhei os diagramas em Mermaid dentro do Markdown, porque o GitHub os
renderiza sem ferramenta extra (decisão 006 D-004), nos níveis de contexto,
contêineres e componentes. Antes de cada diagrama escrevi um texto
alternativo com o mesmo conteúdo, para quem não enxerga o desenho.

## Documentos

| Documento | Conteúdo |
|-----------|----------|
| [01 - Contexto](01-contexto.md) | Atores, fronteira do sistema, tipo de arquitetura e o que fica fora |
| [02 - Contêineres](02-conteineres.md) | Processos executáveis, bancos, broker, identidade e observabilidade; quem fala com quem |
| [03 - Componentes](03-componentes.md) | Estrutura hexagonal de cada serviço, modelos de dados e pacotes compartilhados |
| [04 - Fluxos](04-fluxos.md) | Registro, publicação, consolidação, consulta, autenticação, reconstrução, troca, rollback e reconciliação |
| [05 - Implantação local](05-implantacao-local.md) | Docker Compose, redes, portas, volumes, identidades técnicas, TLS, segredos, health checks e versões |

## Visão em uma página

- Separei dois serviços de negócio independentes, cada um com banco
  PostgreSQL exclusivo: **lançamentos** (livro de caixa, fonte de verdade) e
  **consolidado** (projeção do saldo diário) (INFRA 2.1, 2.3, 3.3). Fiz o
  corte assim para que a queda do consolidado não atinja o registro.
- O registro grava lançamento e evento na mesma transação local (outbox); um
  **relay** publica os eventos no RabbitMQ com confirmação do broker (INFRA
  2.2, §4). Não deixei o serviço de lançamentos chamar o RabbitMQ nem o
  consolidado, e por isso ele continua confirmando lançamentos com eles fora
  do ar (DES - RNF; INFRA 2.1).
- Um **worker** consome a fila, deduplica (inbox e contribuição), projeta o
  saldo, guarda conflitos em quarentena e só confirma a mensagem após o
  commit (INFRA 2.4, 4.8), para que nenhum evento se perca nem seja somado
  duas vezes.
- Versionei a projeção em **gerações**; o **coordenador** reconstrói uma
  geração candidata a partir do replay da outbox e troca a geração ativa de
  forma atômica, com barreira dos consumidores e rollback (INFRA 2.5, 7.4 a
  7.6), sem interromper as consultas.
- O **Keycloak** emite tokens OAuth 2.0 Client Credentials; cada API valida o
  JWT localmente, com cache de JWKS, escopo e audiência (INFRA 5.1 a 5.6),
  para que o registro não dependa do Keycloak a cada requisição.
- **Prometheus** e **Grafana** coletam e exibem as métricas de INFRA 8.3
  (decisão minha, ST-Q-008; spec 007).
- Tudo roda localmente em um único Docker Compose, com redes internas por
  fronteira; publiquei só APIs, Keycloak e Grafana, em `127.0.0.1` com TLS
  (INFRA 1.5, 6.1 a 6.7).

As justificativas de cada escolha, com alternativas e trade-offs, estão nos
ADRs em [`docs/adr/`](../adr/README.md); a tabela de origem desses ADRs está em
[01 - Contexto](01-contexto.md#decisões-que-moldam-a-arquitetura).
