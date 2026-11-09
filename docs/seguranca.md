# Segurança para consumo de serviços

Critérios de segurança para consumo dos serviços, diferencial de DES
("critérios de segurança para consumo de serviços"), escritos como regras que
o sistema cliente do comerciante segue para consumir as APIs e que a solução
impõe. Cada regra parte de um item de INFRA §5. Conteúdo conferido contra o
realm do Keycloak, o pacote `packages/seguranca`, as APIs, o
[`infra/compose.yaml`](../infra/compose.yaml) e os testes. As specs ficam em
`.ai/sdd/specs/`, pasta local que não é versionada (decisão 006 D-006).

Fontes:

- DES = [`Desafio_Arquiteto_Soluções.md`](../Desafio_Arquiteto_Solu%C3%A7%C3%B5es.md)
- INFRA = [`Requisitos_de_Infraestrutura.md`](../Requisitos_de_Infraestrutura.md)

Legenda de origem, a mesma de [`requisitos.md`](requisitos.md): "Fonte" =
DES ou INFRA; "Eduardo" = decisão do autor com data; "Proposta" = não consta
nas fontes e foi aprovada junto com a spec; `[NVI]` = não verificado.

Decisões: [ADR-008](adr/ADR-008-keycloak-client-credentials-jwt.md)
(Keycloak, Client Credentials, JWT validado em cada API),
[ADR-013](adr/ADR-013-escopo-lancamentos-consultar.md) (escopo
`lancamentos:consultar`) e
[ADR-014](adr/ADR-014-audiencia-derivada-do-escopo.md) (audiência derivada do
escopo). Fluxo em
[04 - Fluxos: autenticação e autorização](arquitetura/04-fluxos.md#1-autenticação-e-autorização).

## Quem consome o quê

Os consumidores são sistemas, não pessoas: clientes técnicos do comerciante
(INFRA 5.2). Cada cliente tem só os escopos da sua função.

| Cliente técnico | Escopos | Operações permitidas | Audiência no token |
|-----------------|---------|----------------------|--------------------|
| `cliente-lancamentos` | `lancamentos:registrar` | `POST /v1/lancamentos` | `lancamentos-api` |
| `cliente-consultas` | `consolidado:consultar`, `lancamentos:consultar` | `GET /v1/consolidado/saldos/{data}`, `GET /v1/consolidado/saldos?...`, `GET /v1/consolidado/inclusoes/{lancamento_id}`, `GET /v1/lancamentos?...` | `consolidado-api`, `lancamentos-api` |

Endereços no ambiente local, todos em `127.0.0.1` e só com TLS (INFRA 6.5,
6.6):

| Serviço | Endereço |
|---------|----------|
| Keycloak, endpoint de token | `https://localhost:8443/realms/cashflow/protocol/openid-connect/token` |
| API de lançamentos | `https://localhost:8444` |
| API do consolidado | `https://localhost:8445` |

Bancos, RabbitMQ, Prometheus, `/metrics`, relay, worker e coordenador não são
serviços de consumo: não têm porta no host e ficam em redes internas (INFRA
6.1 a 6.4; [05 - Implantação local](arquitetura/05-implantacao-local.md#portas-publicadas)).

## Regras de consumo

Uma regra por item de INFRA §5. "Regra" é o que o consumidor faz; "Como é
imposta" é o que a solução faz, inclusive quando o consumidor erra;
"Evidência" é o teste que verifica.

### RC-01: Obter o token no provedor de identidade do realm (INFRA 5.1)

- **Regra:** o consumidor obtém o token no endpoint de token do realm
  `cashflow` e não depende de nada específico do Keycloak além do padrão
  OAuth 2.0 (endpoint de token, emissor e JWKS).
- **Como é imposta:** as APIs recebem emissor, URL do JWKS e AC pelo ambiente
  (`JWT_EMISSOR`, `JWT_JWKS_URL`, `JWT_AC_ARQUIVO`); trocar o provedor é trocar
  essas variáveis, sem mudar código. Realm versionado em
  [`realm-cashflow.json`](../infra/keycloak/realm-cashflow.json), sem segredos
  (001 TD-005).
- **Evidência:** [`tests/infra/test_keycloak.py`](../tests/infra/test_keycloak.py).

### RC-02: Autenticar como cliente técnico, só por Client Credentials (INFRA 5.2)

- **Regra:** cada sistema cliente se autentica com o seu `client_id` e o seu
  segredo, com `grant_type=client_credentials`. Não há login de usuário nem
  refresh token: quando o token expira, o cliente pede outro.
- **Como é imposta:** os dois clientes são confidenciais, com conta de
  serviço e os demais fluxos desligados (código de autorização, implícito,
  senha, dispositivo e CIBA); refresh token desligado para Client
  Credentials; o realm exige TLS em todas as conexões (`sslRequired: all`) e
  tem proteção contra força bruta.
- **Evidência:** `test_keycloak.py` (`test_clientes_tecnicos_nao_usam_outro_fluxo`,
  `test_segredo_errado_e_recusado`, `test_endpoint_de_token_nao_responde_sem_tls`).

### RC-03: Usar o JWT emitido, sem montá-lo nem alterá-lo (INFRA 5.3)

- **Regra:** o consumidor trata o `access_token` como opaco e o envia como
  recebeu. O token vale 300 s (`accessTokenLifespan`); pode ser reutilizado
  enquanto válido.
- **Como é imposta:** o Keycloak assina com RS256 e publica só chaves
  assimétricas no JWKS. As APIs aceitam só RS256: qualquer outro algoritmo
  no cabeçalho, inclusive `none` e os simétricos, é recusado antes de buscar
  a chave e verificar a assinatura; carga adulterada ou
  assinatura de outra chave com o mesmo `kid` é recusada.
- **Evidência:** `test_keycloak.py` (`test_token_e_assinado_com_rs256_e_confere_com_o_jwks`,
  `test_jwks_publica_somente_chaves_assimetricas`);
  [`test_validador_jwt.py`](../packages/seguranca/tests/test_validador_jwt.py)
  (`test_algoritmo_simetrico_e_recusado_antes_de_verificar`,
  `test_token_com_carga_adulterada_e_recusado`,
  `test_token_assinado_por_outra_chave_com_o_mesmo_kid_e_recusado`).

### RC-04: Pedir só o escopo da operação (INFRA 5.4)

- **Regra:** cada sistema usa o cliente da sua função e chama só as operações
  dos seus escopos (tabela [Quem consome o quê](#quem-consome-o-quê)). Um
  sistema que só consulta não recebe o cliente de registro.
- **Como é imposta:** escopos no formato `<recurso>:<ação>`; cada rota exige
  um escopo e a audiência da sua API (`aud`, derivada do escopo pelo
  Keycloak, 001 TD-008). Token sem o escopo da rota: 403 com
  `WWW-Authenticate: Bearer error="insufficient_scope"` e corpo
  `{"erro": "sem_escopo"}`. O escopo `lancamentos:consultar` não consta de
  INFRA 5.4: foi acrescentado por decisão de Eduardo (002 Q-001;
  [ADR-013](adr/ADR-013-escopo-lancamentos-consultar.md)).
- **Evidência:** `test_keycloak.py` (`test_token_de_cada_cliente_contem_somente_seu_escopo`,
  `test_token_traz_a_audiencia_das_apis_dos_seus_escopos`,
  `test_cliente_de_registro_nao_obtem_escopo_de_consulta`);
  `test_validador_jwt.py` (`test_token_sem_o_escopo_exigido_e_recusado_com_403`).

### RC-05: Enviar o token em cada chamada, direto à API (INFRA 5.5)

- **Regra:** toda requisição leva `Authorization: Bearer <access_token>`. Não
  há gateway: o consumidor chama cada API diretamente, e cada uma valida o
  token por conta própria.
- **Como é imposta:** cada API valida, nesta ordem, formato, algoritmo, chave
  pelo `kid`, assinatura, expiração, vigência, emissor
  (`https://localhost:8443/realms/cashflow`), audiência e escopo
  ([`validador.py`](../packages/seguranca/src/seguranca/validador.py)). A
  autenticação vem antes do corpo e dos parâmetros: sem token válido, nada da
  requisição é lido. Token ausente, malformado, expirado ou com emissor ou
  audiência errados: 401 com `WWW-Authenticate: Bearer error="invalid_token"`
  e corpo `{"erro": "nao_autenticado"}`. Cada falha soma em
  `cashflow_jwt_falhas_total{motivo}` ([observabilidade](observabilidade.md#métricas-de-infra-83)).
- **Evidência:** `test_validador_jwt.py` (`test_token_ausente_ou_malformado_e_recusado`,
  `test_token_expirado_e_recusado`,
  `test_emissor_audiencia_e_claims_obrigatorias_sao_conferidos`);
  [`test_validacao_jwt_keycloak.py`](../services/lancamentos/tests/integracao/test_validacao_jwt_keycloak.py),
  contra o Keycloak real.

### RC-06: Tratar a indisponibilidade da validação como temporária (INFRA 5.6)

- **Regra:** 503 `indisponivel` é falha temporária do serviço (banco ou JWKS
  indisponível): o consumidor repete depois, com espera crescente. 401 não se
  resolve repetindo o mesmo token: o consumidor obtém um novo.
- **Como é imposta:** o JWKS fica em cache por 300 s
  (`JWT_JWKS_TTL_SEGUNDOS`). Com o Keycloak fora, tokens válidos continuam
  aceitos até o cache vencer; depois, sem chave válida, a API responde 503,
  não 401, para não mandar o cliente descartar um token bom. `kid`
  desconhecido força nova busca do JWKS (rotação de chave), limitada a uma a
  cada 10 s, para que tokens com `kid` inventado não gerem uma consulta ao
  Keycloak por requisição. Falhas somam em `cashflow_jwks_falhas_total`.
- **Evidência:** `test_validador_jwt.py` (`test_keycloak_parado_token_valido_e_aceito_dentro_do_ttl`,
  `test_keycloak_parado_apos_o_ttl_sem_chave_valida_da_503`,
  `test_kid_novo_dentro_do_ttl_renova_o_jwks_rotacao`,
  `test_kid_inexistente_e_recusado_sem_renovar_a_cada_requisicao`);
  [`tests/e2e/test_falhas_consolidado.py`](../tests/e2e/test_falhas_consolidado.py)
  (`test_keycloak_indisponivel`).

### RC-07: Usar só HTTPS e validar o certificado (INFRA 5.7)

- **Regra:** o consumidor fala só HTTPS com Keycloak e APIs e valida o
  certificado do servidor; nunca desliga a verificação (`-k`,
  `verify=False`, `insecureSkipTLSVerify`).
- **Como é imposta:** o Keycloak roda em modo produção com HTTP desligado; as
  APIs e o Grafana só escutam TLS; as APIs buscam o JWKS só por HTTPS,
  validando o certificado do Keycloak. Não há porta HTTP publicada.
- **Evidência:** `test_keycloak.py` (`test_endpoint_de_token_nao_responde_sem_tls`);
  `test_validador_jwt.py` (`test_jwks_so_e_obtido_por_tls`);
  [`tests/infra/test_isolamento.py`](../tests/infra/test_isolamento.py)
  (`test_keycloak_atende_por_tls_em_loopback`).

### RC-08: Confiar explicitamente na AC local (INFRA 5.8)

- **Regra:** no ambiente local, o consumidor confia na AC gerada pelo projeto,
  `infra/tls/gerados/ac/ac-cashflow.crt`, de forma explícita (por exemplo,
  `curl --cacert` ou `SSL_CERT_FILE`), sem instalá-la no sistema. Os
  certificados valem para `localhost`, `127.0.0.1` e o nome do serviço na
  rede interna.
- **Como é imposta:** a AC e os certificados são gerados por
  [`infra/tls/gerar-certificados.sh`](../infra/tls/gerar-certificados.sh)
  com OpenSSL (001 TD-004); a chave privada da AC fica só legível pelo dono e
  fora do Git.
- **Evidência:** [`tests/infra/test_certificados.py`](../tests/infra/test_certificados.py)
  (`test_certificado_valida_contra_a_ac_local`,
  `test_certificado_nao_valida_contra_outra_ac`,
  `test_chave_privada_da_ac_so_legivel_pelo_dono`).

### RC-09: Guardar o segredo do cliente fora do código e dos logs (INFRA 5.9)

- **Regra:** o segredo do cliente técnico só trafega no corpo da requisição
  de token, por TLS. Não vai para código, URL, repositório nem log; o
  `access_token` também não vai para log.
- **Como é imposta:** os segredos dos clientes vêm de `infra/.env`
  (`KEYCLOAK_SEGREDO_CLIENTE_LANCAMENTOS`, `KEYCLOAK_SEGREDO_CLIENTE_CONSULTAS`),
  ignorado pelo Git, e entram no realm só na importação. Do lado das APIs, os
  logs redigem `Authorization`, padrões de JWT e de `Bearer`, e as mensagens
  de erro nunca repetem o token (INFRA 8.2;
  [observabilidade](observabilidade.md#logs-estruturados)).
- **Evidência:** [`tests/infra/test_segredos.py`](../tests/infra/test_segredos.py)
  (`test_env_local_e_ignorado_pelo_git`,
  `test_nenhum_segredo_local_aparece_no_repositorio`);
  `test_validador_jwt.py` (`test_mensagem_do_erro_nao_contem_o_token`).

### RC-10: Gerar segredos próprios a partir do modelo (INFRA 5.10)

- **Regra:** quem instala o ambiente copia [`.env.example`](../.env.example)
  para `infra/.env`, com permissão `600`, e preenche cada valor com um segredo
  próprio (o modelo sugere `openssl rand -hex 24`).
- **Como é imposta:** o modelo só tem nomes e valores vazios; o compose
  recusa subir com variável obrigatória vazia (`${VAR:?...}`).
- **Evidência:** `test_segredos.py` (`test_modelo_nao_traz_valor_real`,
  `test_modelo_declara_toda_variavel_obrigatoria_do_compose`).

### RC-11: Uma identidade por sistema e por função (INFRA 5.11)

- **Regra:** registro e consulta usam clientes técnicos diferentes; um
  cliente não é compartilhado entre funções. Do lado da solução, cada
  componente interno também tem identidade própria.
- **Como é imposta:** dois clientes técnicos com escopos disjuntos de
  operação; administração do Keycloak no realm `master` (`admin-cashflow`),
  separada dos clientes; um papel PostgreSQL e um usuário RabbitMQ por
  componente, com privilégio mínimo (tabela completa em
  [05 - Implantação local](arquitetura/05-implantacao-local.md#identidades-técnicas)).
- **Evidência:** `test_keycloak.py`
  (`test_administrador_fica_no_realm_master_separado_dos_clientes`);
  [`test_papeis_banco.py`](../tests/infra/test_papeis_banco.py);
  [`test_rabbitmq_permissoes.py`](../tests/infra/test_rabbitmq_permissoes.py).

### RC-12: Autorização por operação, sem gateway, RBAC ou ABAC (INFRA 5.12)

- **Regra:** o consumidor sabe que a autorização é por operação (escopo e
  audiência), não por comerciante nem por recurso: o detentor de
  `cliente-consultas` consulta todos os saldos e lançamentos. O segredo do
  cliente equivale ao acesso completo às operações do seu escopo e deve ser
  protegido como tal.
- **Como é imposta:** não há API Gateway, papéis ou atributos (001 D-001);
  multiempresa está fora do escopo (INFRA - Fora do escopo). Não há limite
  de taxa por cliente nas APIs nem lista de revogação: um token comprometido
  vale até expirar (300 s).
- **Evidência:** ausência verificada no código das APIs e no compose; não há
  teste, por ser ausência deliberada.

## Respostas e conduta do consumidor

Contrato de erros das duas APIs
([lançamentos](../services/lancamentos/adaptadores/http/app.py),
[consolidado](../services/consolidado/adaptadores/http/app.py)), no que toca
à segurança e ao consumo. O corpo de erro traz o campo `erro`.

| Status | `erro` | Significado | Conduta do consumidor |
|--------|--------|-------------|-----------------------|
| 401 | `nao_autenticado` | Token ausente, malformado, expirado, de outro emissor ou de outra audiência | Obter novo token; não repetir com o mesmo |
| 403 | `sem_escopo` | Token válido sem o escopo da rota | Não repetir; usar o cliente técnico da função |
| 400 | `requisicao_invalida` | No registro: corpo que não é objeto JSON ou `Idempotency-Key` ausente | Corrigir a requisição |
| 409 | `conflito_idempotencia` | `Idempotency-Key` já usada com outro conteúdo | Não repetir; usar chave nova para lançamento novo |
| 503 | `indisponivel` | Banco do serviço ou JWKS indisponível | Repetir depois, com espera crescente, mantendo a mesma `Idempotency-Key` no registro |

## Regras complementares

Não decorrem de INFRA §5, mas fazem parte do consumo seguro:

- **Idempotência no registro:** todo `POST /v1/lancamentos` leva
  `Idempotency-Key`. A repetição idêntica devolve a resposta original, sem
  duplicar o lançamento; a chave vale por cliente técnico (chave primária
  `cliente_id`, `chave`), então chaves de clientes diferentes não colidem
  (002 TD-004; INFRA 3.1).
- **Correlação:** o consumidor pode enviar `X-Correlation-ID` (até 64
  caracteres entre letras, dígitos, `.`, `_` e `-`) para rastrear a chamada
  nos logs; valor fora do formato é substituído por um UUID gerado pela API,
  para que o cliente não injete texto arbitrário nos logs.
- **Dados mínimos no pedido:** a API de lançamentos recusa campo não previsto
  no corpo (422), e a descrição e o valor nunca vão para log.

## Exemplo de consumo

A partir da raiz do repositório, com o ambiente no ar. O segredo vem de
`infra/.env` e não deve ficar no histórico do shell:

```bash
AC=infra/tls/gerados/ac/ac-cashflow.crt
SEGREDO=$(grep '^KEYCLOAK_SEGREDO_CLIENTE_CONSULTAS=' infra/.env | cut -d= -f2-)

# Token por Client Credentials, por TLS validado pela AC local.
TOKEN=$(curl -sS --cacert "$AC" \
  -d grant_type=client_credentials \
  -d client_id=cliente-consultas \
  --data-urlencode "client_secret=$SEGREDO" \
  https://localhost:8443/realms/cashflow/protocol/openid-connect/token \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

# Consulta do consolidado com o token no cabeçalho Authorization.
curl -sS --cacert "$AC" -H "Authorization: Bearer $TOKEN" \
  https://localhost:8445/v1/consolidado/saldos/2026-10-01
```

Sem o cabeçalho, a mesma consulta devolve 401; com esse token, um
`POST /v1/lancamentos` devolve 403, porque `cliente-consultas` não tem
`lancamentos:registrar`.

## Fora desta entrega

Pontos conhecidos, aceitos pelas fontes ou pelos ADRs, a tratar como
evolução:

- Revogação imediata de token (hoje vale até expirar, 300 s;
  [ADR-008](adr/ADR-008-keycloak-client-credentials-jwt.md)).
- Limite de taxa por cliente e API Gateway (INFRA 5.12).
- Autorização por comerciante ou por recurso (RBAC/ABAC; multiempresa fora
  do escopo).
- AC reconhecida fora do ambiente local e gestão de segredos em cofre: a AC
  local e o `infra/.env` atendem só ao desenvolvimento (INFRA 5.8, 5.9).
- Política numérica de retenção dos logs e das respostas idempotentes
  (INFRA 7.7, pendente).
