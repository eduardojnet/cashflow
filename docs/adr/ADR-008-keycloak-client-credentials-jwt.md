# ADR-008: Keycloak, Client Credentials, JWT validado em cada API, sem gateway/RBAC/ABAC

> Status: Aceito
> Data: 2026-11-05
> Relacionado: INFRA 5.1 a 5.12, 7.1; ST-TD-005; 001 TD-004, TD-005; 001 D-001; 002 TD-005, DQ-002; 004 DQ-002; P-007, P-008

## Contexto

As APIs são consumidas por clientes técnicos do comerciante, não por pessoas
(DES - critérios de segurança para consumo de serviços). INFRA §5 fixa o
modelo: Keycloak local e substituível, OAuth 2.0 Client Credentials, JWT com
assinatura assimétrica, escopos mínimos, validação do JWT diretamente em cada
API com cache controlado de JWKS, TLS em APIs e provedor de identidade com AC
local, segredos fora do Git e identidades técnicas separadas. INFRA 5.12
exclui API Gateway, RBAC e ABAC. Acrescentei uma restrição minha: a
validação do token não pode tornar o registro de lançamentos dependente da
disponibilidade do Keycloak a cada requisição (P-001).

## Decisão

- **Provedor:** uso o Keycloak 26.8.2, realm `cashflow`, com a administração
  separada dos clientes técnicos.
- **Fluxo:** Client Credentials. Dei a `cliente-lancamentos` o escopo
  `lancamentos:registrar` e a `cliente-consultas` os escopos
  `consolidado:consultar` e `lancamentos:consultar`
  ([ADR-013](ADR-013-escopo-lancamentos-consultar.md)), para que nenhum
  cliente tenha mais do que a sua operação exige. O token é um JWT assinado
  com RS256, com emissor, escopo e audiência
  ([ADR-014](ADR-014-audiencia-derivada-do-escopo.md)).
- **Validação local em cada API (002 TD-005):** o pacote
  `packages/seguranca` verifica algoritmo, assinatura, `exp`, `iat`, emissor,
  audiência e escopo. Guardo o JWKS em cache com TTL configurável (300 s no
  compose) e só o busco de novo quando o cache vence ou chega um `kid`
  desconhecido; é isso que mantém o registro funcionando com o Keycloak fora.
  Sem chave válida em cache e com o Keycloak fora, a API responde 503
  (002 DQ-002), porque a falha é do serviço, não do token do cliente. Erros: 401
  sem token ou com token inválido, 403 sem o escopo exigido.
- **Autorização só por escopo e audiência (001 D-001):** sem gateway, papéis
  ou atributos.
- **TLS com AC local por OpenSSL (001 TD-004):** escrevi um script que gera a
  AC e os certificados de Keycloak, APIs e Grafana; a chave privada da AC
  fica fora do Git.
- **Realm versionado sem segredos (001 TD-005):** exportei o realm para o
  repositório e o importo na subida; os segredos dos clientes vêm de
  `infra/.env` (ignorado pelo Git); o volume `vol-keycloak` guarda o estado.

## Alternativas consideradas

### Alternativa 1: API Gateway validando o token

- **Origem:** análise do ADR; excluída por INFRA 5.12.
- **Descrição:** um gateway na frente das APIs valida o JWT e repassa a
  identidade.
- **Prós:** validação e limites de uso num só ponto.
- **Contras:** novo componente no caminho crítico; as APIs confiariam no
  gateway em vez de validar.
- **Por que rejeitei:** INFRA 5.5 exige validação em cada API e INFRA 5.12
  exclui gateway nesta entrega.

### Alternativa 2: Introspecção do token no Keycloak a cada requisição

- **Origem:** análise do ADR.
- **Descrição:** a API pergunta ao Keycloak se o token é válido.
- **Prós:** revogação imediata.
- **Contras:** cada registro de lançamento passaria a depender do Keycloak no
  ar; latência extra por requisição.
- **Por que rejeitei:** contraria INFRA 5.5 e 5.6 (validação local com JWKS
  em cache) e P-001.

### Alternativa 3: Chave de API estática por cliente

- **Origem:** análise do ADR.
- **Descrição:** segredo compartilhado enviado em cabeçalho.
- **Prós:** sem provedor de identidade.
- **Contras:** sem expiração nem escopo no próprio token; rotação manual em
  cada API.
- **Por que rejeitei:** descumpre INFRA 5.1 a 5.4.

### Alternativa 4: mkcert para a AC local

- **Origem:** spec (001 TD-004).
- **Descrição:** usar mkcert para gerar e instalar a AC de desenvolvimento.
- **Prós:** instala a AC no repositório de confiança do sistema
  automaticamente.
- **Contras:** dependência extra na máquina do avaliador e alteração do
  repositório de confiança do sistema.
- **Por que rejeitei:** OpenSSL já está disponível nas distribuições Linux, e
  a confiança fica restrita a quem usa a AC explicitamente (por exemplo,
  `SSL_CERT_FILE` no teste de carga).

### Alternativa 5: Realm configurado manualmente pelo console

- **Origem:** análise do ADR (em 001 TD-005 escolhi o realm versionado).
- **Descrição:** criar realm, clientes e escopos pela interface do Keycloak.
- **Prós:** nenhum arquivo para manter.
- **Contras:** configuração não reproduzível e fora da revisão de código.
- **Por que rejeitei:** INFRA 5.9 e 7.1 pedem segredos fora do Git e
  persistência; o realm versionado sem segredos atende às duas coisas.

## Consequências

### Positivas

- Com o Keycloak fora, as APIs seguem validando tokens enquanto houver chave
  em cache
  ([`tests/e2e/test_falhas_consolidado.py`](../../tests/e2e/test_falhas_consolidado.py),
  `test_keycloak_indisponivel`), e respondem 503, não 401, quando não há
  chave válida ou chega um `kid` desconhecido
  ([`packages/seguranca/tests/test_validador_jwt.py`](../../packages/seguranca/tests/test_validador_jwt.py),
  com JWKS em memória e relógio controlado, para cobrir o TTL sem esperar).
- O token de um cliente não autoriza a operação de outro; verifiquei os
  escopos contra o Keycloak real
  ([`tests/infra/test_keycloak.py`](../../tests/infra/test_keycloak.py),
  [`test_validacao_jwt_keycloak.py`](../../services/lancamentos/tests/integracao/test_validacao_jwt_keycloak.py))
  e, sem Keycloak, em
  [`packages/seguranca/tests/test_validador_jwt.py`](../../packages/seguranca/tests/test_validador_jwt.py).
- Verifico a cadeia TLS e a ausência de segredos versionados em
  [`tests/infra/test_certificados.py`](../../tests/infra/test_certificados.py)
  e [`tests/infra/test_segredos.py`](../../tests/infra/test_segredos.py).

### Negativas / trade-offs

- A revogação de token só tem efeito na expiração (300 s); não criei lista de
  revogação.
- Sem RBAC ou ABAC, a autorização é por operação, não por comerciante ou por
  recurso; aceitei porque multiempresa está fora do escopo.
- O cliente precisa confiar explicitamente na AC local.
- Mudar o realm exige recriar `vol-keycloak`, porque `--import-realm` não
  reimporta realm existente.

### Riscos

- Rotação de chave do Keycloak: tratei com a nova busca do JWKS quando chega
  um `kid` desconhecido.
- Deixei as falhas de JWT e JWKS visíveis pelas métricas
  `cashflow_jwt_falhas_total` e `cashflow_jwks_falhas_total`
  ([ADR-015](ADR-015-catalogo-fechado-de-metricas.md)).

## Notas de implementação

- O realm está em [`infra/keycloak/realm-cashflow.json`](../../infra/keycloak/realm-cashflow.json);
  os certificados saem de [`infra/tls/gerar-certificados.sh`](../../infra/tls/gerar-certificados.sh).
- O emissor publicado é `https://localhost:8443/realms/cashflow`; as APIs
  buscam o JWKS pelo nome interno `keycloak`, que incluí no certificado.
- Revejo esta decisão se as APIs forem expostas a clientes externos à
  organização (limites de uso e gateway), ou se surgir necessidade de
  revogação imediata ou de autorização por recurso.

## Referências

- [04 - Fluxos: autenticação e autorização](../arquitetura/04-fluxos.md#1-autenticação-e-autorização)
- [05 - Implantação local](../arquitetura/05-implantacao-local.md)
