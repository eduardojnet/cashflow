# ADR-014: Audiência do token derivada do escopo

> Status: Aceito
> Data: 2026-11-06
> Relacionado: INFRA 5.4, 5.5; 001 TD-008 (emenda à 001-T4); 001 TD-005; 002 DQ-003; [ADR-008](ADR-008-keycloak-client-credentials-jwt.md), [ADR-013](ADR-013-escopo-lancamentos-consultar.md)

## Contexto

No design da API de lançamentos, decidi validar a claim `aud` do token (002
DQ-003, 2026-11-05). O realm que configurei na 001-T4 não emitia `aud`, o que
bloqueou a implementação da validação em 2026-11-06. Sem audiência, um token
emitido para uma API é aceito na outra sempre que o nome do escopo coincidir
ou for mal verificado: a autorização dependeria só do nome do escopo. Os dois
clientes técnicos têm perfis diferentes: `cliente-lancamentos` usa só a API
de lançamentos, e `cliente-consultas` usa as duas.

## Decisão

- Pus em cada *client scope* do realm um `oidc-audience-mapper` que acrescenta
  à claim `aud` a audiência da API dona do escopo:
  - `lancamentos:registrar` e `lancamentos:consultar` levam
    `aud=lancamentos-api`;
  - `consolidado:consultar` leva `aud=consolidado-api`.
- `lancamentos-api` aceita somente token com `aud` contendo `lancamentos-api`;
  `consolidado-api`, somente com `consolidado-api`. Deixei a audiência
  esperada como configuração do pacote `packages/seguranca`, que serve às
  duas APIs.
- Usei para a audiência os mesmos nomes dos certificados TLS das APIs.
- Resultado: `cliente-lancamentos` recebe `aud=lancamentos-api`;
  `cliente-consultas` recebe `aud=[consolidado-api, lancamentos-api]`.

Derivei a audiência do escopo, e não do cliente, porque assim ela acompanha
o que o token de fato autoriza.

## Alternativas consideradas

### Alternativa 1: Dispensar `aud` e validar só o escopo

- **Origem:** spec (001 TD-008).
- **Descrição:** manter o realm como estava.
- **Prós:** nenhuma mudança no realm.
- **Contras:** autorização dependente só do nome do escopo.
- **Por que rejeitei:** descumpre 002 DQ-003.

### Alternativa 2: Mapper por cliente (audiência fixa por cliente)

- **Origem:** spec (001 TD-008).
- **Descrição:** cada cliente técnico recebe uma audiência fixa.
- **Prós:** configuração simples por cliente.
- **Contras:** `cliente-consultas` usa as duas APIs, e a audiência deixaria de
  acompanhar o que o token de fato autoriza.
- **Por que rejeitei:** a audiência precisa seguir os escopos concedidos.

### Alternativa 3: Audiência igual ao `clientId` do chamador (`azp`)

- **Origem:** spec (001 TD-008).
- **Descrição:** usar o identificador do cliente como audiência.
- **Prós:** nenhum mapper extra.
- **Contras:** identifica quem pede o token, não a quem ele se destina.
- **Por que rejeitei:** semântica errada para `aud`.

### Alternativa 4: Clientes `bearer-only` por API com `included.client.audience`

- **Origem:** spec (001 TD-008).
- **Descrição:** criar um cliente para cada API só para nomear a audiência.
- **Prós:** forma comum de configurar audiência no Keycloak.
- **Contras:** clientes sem credencial nem uso além de nomear a audiência.
- **Por que rejeitei por ora:** acrescenta objetos ao realm sem ganho sobre
  o mapper por escopo.

## Consequências

### Positivas

- O token só é aceito pela API a que se destina, independentemente do escopo
  ([`tests/infra/test_keycloak.py`](../../tests/infra/test_keycloak.py),
  `test_token_traz_a_audiencia_das_apis_dos_seus_escopos`;
  [`test_validacao_jwt_keycloak.py`](../../services/lancamentos/tests/integracao/test_validacao_jwt_keycloak.py),
  `test_validador_de_outra_audiencia_recusa_o_token`).

### Negativas / trade-offs

- Um escopo novo exige declarar também a audiência.
- Mudar o realm exige recriar `vol-keycloak`, porque `--import-realm` não
  reimporta realm existente (001 TD-005); os segredos dos clientes vêm de
  `infra/.env` e não mudam na recriação.

### Riscos

- Esquecer o mapper num escopo novo: o token não teria a audiência e seria
  recusado. Aceito esse risco porque a falha é segura, e os testes de
  Keycloak acusam a ausência.

## Notas de implementação

- Os mappers estão em [`infra/keycloak/realm-cashflow.json`](../../infra/keycloak/realm-cashflow.json).
- Revejo esta decisão se surgir uma terceira API, um cliente que precise de
  token restrito a só uma das APIs que consulta, ou troca do provedor de
  identidade.

## Referências

- [04 - Fluxos: autenticação e autorização](../arquitetura/04-fluxos.md#1-autenticação-e-autorização)
