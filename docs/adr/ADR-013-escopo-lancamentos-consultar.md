# ADR-013: Escopo adicional `lancamentos:consultar`

> Status: Aceito
> Data: 2026-11-05
> Relacionado: INFRA 5.4, 5.11; 002 D-002; 002 FR-008, FR-009; 001 design §4; P-007

## Contexto

DES pede um "serviço que faça o controle de lançamentos". Registrar não
basta para controlar: o comerciante precisa consultar o que foi lançado num
período. Eduardo decidiu incluir a listagem por intervalo de datas, paginada
(002 Q-001, D-002). INFRA 5.4 define os escopos mínimos
`lancamentos:registrar` e `consolidado:consultar`, nenhum dos quais descreve
leitura de lançamentos, e INFRA 5.4 e 5.11 pedem escopos mínimos por função
(P-007).

## Decisão

- Novo escopo `lancamentos:consultar`, adicional aos de INFRA 5.4, exigido por
  `GET /v1/lancamentos` (listagem por intervalo, paginada por cursor).
- `POST /v1/lancamentos` continua exigindo `lancamentos:registrar`, e só ele.
- No Keycloak: `cliente-lancamentos` recebe só `lancamentos:registrar`;
  `cliente-consultas` recebe `consolidado:consultar` e `lancamentos:consultar`.
- O escopo carrega a audiência `lancamentos-api`
  ([ADR-014](ADR-014-audiencia-derivada-do-escopo.md)).

## Alternativas consideradas

### Alternativa 1: Reaproveitar `lancamentos:registrar` para a leitura

- **Origem:** análise do ADR.
- **Descrição:** quem registra também lista.
- **Prós:** nenhum escopo novo.
- **Contras:** um cliente que só precisa ler receberia permissão de escrita.
- **Por que foi rejeitada:** viola o mínimo privilégio (INFRA 5.4; P-007).

### Alternativa 2: Reaproveitar `consolidado:consultar`

- **Origem:** análise do ADR.
- **Descrição:** um escopo único de leitura para as duas APIs.
- **Prós:** menos escopos.
- **Contras:** o escopo nomeia um recurso (`consolidado`) e passaria a valer
  para outro; um cliente só de saldo ganharia acesso ao detalhe dos
  lançamentos, que contém valores e descrições.
- **Por que foi rejeitada:** mistura recursos com sensibilidade diferente e
  quebra a convenção `<recurso>:<ação>` do steering.

### Alternativa 3: Não oferecer listagem de lançamentos

- **Origem:** análise do ADR (é o mínimo literal de INFRA 5.4).
- **Descrição:** a API de lançamentos só registra.
- **Prós:** superfície menor.
- **Contras:** o comerciante não consegue conferir o que lançou; o "controle
  de lançamentos" de DES fica incompleto.
- **Por que foi rejeitada:** decisão de Eduardo (002 D-002).

## Consequências

### Positivas

- Leitura e escrita de lançamentos autorizadas separadamente.
- Token de `cliente-lancamentos` é recusado com 403 na listagem, e token de
  `cliente-consultas` é recusado no registro (testes da API em
  [`services/lancamentos/tests/`](../../services/lancamentos/tests/) e
  [`tests/infra/test_keycloak.py`](../../tests/infra/test_keycloak.py)).

### Negativas / trade-offs

- Um escopo fora de INFRA 5.4 para manter no realm e na documentação.
- `cliente-consultas` acumula leitura das duas APIs; um cliente só de saldo
  exigiria um terceiro cliente técnico.

### Riscos

- Exposição de dados de lançamento a um cliente de leitura comprometido.
  Mitigação: o escopo só dá leitura; a autorização por comerciante (dados de
  um cliente técnico por vez) é evolução, porque multiempresa está fora do
  escopo.

## Notas de implementação

- Escopo e clientes em [`infra/keycloak/realm-cashflow.json`](../../infra/keycloak/realm-cashflow.json).
- Gatilho de revisão: cliente que precise só de uma das leituras, ou
  autorização por recurso (RBAC/ABAC, hoje fora do escopo por INFRA 5.12).

## Referências

- [04 - Fluxos: autenticação e autorização](../arquitetura/04-fluxos.md#1-autenticação-e-autorização)
- [Requisitos da spec 002](../requisitos.md)
