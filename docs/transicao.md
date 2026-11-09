# Arquitetura de transição

Diferencial de DES: "Desenho da solução da Arquitetura de Transição (se
necessária, considerando uma migração de legado)".

## Decisão

**Não aplicável: sem legado informado.**

Decisão 006 D-002 (Eduardo, 2026-11-05, resposta à questão 006 Q-001). As
specs ficam em `.ai/sdd/specs/`, pasta local que não é versionada (decisão
006 D-006).

## Motivo

- DES condiciona o diferencial à existência de um legado ("se necessária,
  considerando uma migração de legado").
- Nenhuma das fontes informa sistema legado, dados a migrar ou processo
  atual a substituir: DES descreve só a necessidade do comerciante
  (lançamentos e saldo diário consolidado), e INFRA descreve a
  infraestrutura da solução nova
  ([`Desafio_Arquiteto_Soluções.md`](../Desafio_Arquiteto_Solu%C3%A7%C3%B5es.md),
  [`Requisitos_de_Infraestrutura.md`](../Requisitos_de_Infraestrutura.md)).
- Desenhar a transição de um legado inventado seria apresentar suposição
  como requisito, o que o projeto proíbe (princípio P-010: o que não consta
  nas fontes vira questão ou proposta explícita, nunca fato).

## O que a solução já oferece a uma transição futura

Fatos da implementação atual que reduzem o custo de uma migração, caso um
legado venha a ser informado:

| Mecanismo | Por que ajuda | Onde |
|-----------|---------------|------|
| Registro idempotente por chave do cliente | Uma carga inicial pode ser reexecutada sem duplicar lançamentos | 002/FR-004 em [`requisitos.md`](requisitos.md#spec-002-registro-e-listagem-de-lançamentos); [ADR-003](adr/ADR-003-outbox-transacional-e-relay.md) |
| Lançamento retroativo permitido | Lançamentos antigos entram com a data original e recompõem o saldo daqueles dias | 002/D-005 em [`requisitos.md`](requisitos.md#spec-002-registro-e-listagem-de-lançamentos) |
| Livro e outbox como fonte para replay | A projeção do consolidado pode ser reconstruída depois de uma carga, sem depender do RabbitMQ | [ADR-007](adr/ADR-007-geracoes-barreira-troca-atomica.md); [04 - Fluxos](arquitetura/04-fluxos.md#6-reconstrução-da-projeção); INFRA 7.2, 7.6 |
| Reconciliação pelo coordenador operacional | Confere a projeção contra o livro após a carga | [ADR-007](adr/ADR-007-geracoes-barreira-troca-atomica.md); [04 - Fluxos](arquitetura/04-fluxos.md#8-reconciliação) |

A transição genérica (o que seria preciso desenhar se surgir um legado) é
tratada como evolução, conforme 006 D-002: ver
[`evolucoes.md`](evolucoes.md#transição-de-um-legado).
