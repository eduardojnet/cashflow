# ADR-016: Isolamento de rede da observabilidade e bind do `/metrics`

> Status: Proposto (aplicado no código; pendente de confirmação de Eduardo)
> Data: 2026-11-06
> Relacionado: INFRA 6.1, 6.5, 6.6, 5.11; 007 TD-007 (emenda); 001 TD-002; [ADR-010](ADR-010-docker-compose-local.md), [ADR-012](ADR-012-prometheus-grafana.md)

## Contexto

INFRA 6.1 pede redes internas para bancos, RabbitMQ e componentes
operacionais, e o design da spec 007 exige `/metrics` e Prometheus
inacessíveis a partir do host. As APIs estão em duas redes: a interna do seu
lado (livro ou consolidado) e a `rede-identidade`, que não é interna porque
precisa publicar a porta HTTPS em `127.0.0.1` e alcançar o JWKS do Keycloak
(001 TD-002).

Em 2026-11-06, a verificação da atividade de coleta (007-T1) mostrou, com
saída real, que o `/metrics` das APIs respondia 200 ao host pelo IP da
`rede-identidade`: o servidor de métricas escutava em todas as interfaces. A
mesma verificação mostrou que porta de contêiner ligada só a rede interna não
é publicada no host, o que impedia publicar o Grafana a partir da rede da
observabilidade.

## Decisão

1. `rede-observabilidade` interna, com `inhibit_ipv4` na ponte do host, para
   Prometheus e Grafana.
2. Grafana também numa rede própria não interna, `rede-publicacao-grafana`,
   usada só para publicar `127.0.0.1:8446` com TLS.
3. O `/metrics` das APIs escuta só no alias da API na rede interna do seu
   lado, definido por `METRICAS_ENDERECO` e repassado ao servidor de
   métricas; a `rede-identidade` não alcança o `/metrics`.
4. Papéis de monitoramento do PostgreSQL (`livro_monitoramento`,
   `consolidado_monitoramento`) criados pelos scripts idempotentes de papéis,
   reaplicáveis aos volumes existentes, sem `down -v`.

## Alternativas consideradas

### Alternativa 1: Aceitar `/metrics` alcançável pelo host

- **Origem:** spec (007 TD-007).
- **Descrição:** afrouxar o critério de isolamento.
- **Prós:** nenhuma mudança.
- **Contras:** expõe dados operacionais ao host.
- **Por que foi rejeitada:** contraria o design da spec 007 (§6) e INFRA 6.1.

### Alternativa 2: IP fixo por IPAM para o bind do `/metrics`

- **Origem:** spec (007 TD-007).
- **Descrição:** fixar o IP de cada API na rede interna e escutar nele.
- **Prós:** bind explícito por endereço.
- **Contras:** acopla o código à faixa de endereços e quebra ao recriar redes.
- **Por que foi rejeitada:** o alias de rede tem o mesmo efeito sem fixar
  endereços.

### Alternativa 3: Tirar as APIs da `rede-identidade`

- **Origem:** spec (007 TD-007).
- **Descrição:** deixar as APIs só nas redes internas.
- **Prós:** nenhuma interface alcançável pelo host além da publicada.
- **Contras:** as APIs deixariam de ser publicadas em loopback e de buscar o
  JWKS.
- **Por que foi rejeitada:** quebra 001 TD-002 e a validação de token.

### Alternativa 4: Autenticação ou TLS no `/metrics`

- **Origem:** spec (007 TD-007).
- **Descrição:** proteger o endpoint em vez de escondê-lo.
- **Prós:** funciona mesmo se o endpoint ficar alcançável.
- **Contras:** mais segredos e configuração no Prometheus para um endpoint
  que basta não expor.
- **Por que foi rejeitada por ora:** custo maior que o benefício no ambiente
  local.

### Alternativa 5: Grafana na `rede-identidade`

- **Origem:** spec (007 TD-007).
- **Descrição:** reaproveitar a rede já publicada.
- **Prós:** uma rede a menos.
- **Contras:** daria ao Grafana rota até Keycloak e APIs sem necessidade.
- **Por que foi rejeitada:** contraria o mínimo privilégio de rede.

### Alternativa 6: `down -v` para criar os papéis de monitoramento

- **Origem:** spec (007 TD-007).
- **Descrição:** recriar os volumes para que o entrypoint do PostgreSQL rode
  de novo.
- **Prós:** nenhum script reaplicável.
- **Contras:** apaga os dados locais.
- **Por que foi rejeitada:** os scripts de papéis já são idempotentes.

## Consequências

### Positivas

- Host sem acesso a `/metrics`, Prometheus e exporters; Prometheus só em
  redes internas; `rede-publicacao-grafana` só com o Grafana; Grafana só em
  loopback e só com TLS; e todos os alvos coletados pelas redes internas
  ([`tests/observabilidade/test_alvos.py`](../../tests/observabilidade/test_alvos.py)).

### Negativas / trade-offs

- O host alcança o Grafana também pelo IP da `rede-publicacao-grafana`, com o
  mesmo TLS e a mesma autenticação da publicação em loopback, como já ocorre
  com Keycloak e APIs na `rede-identidade`.
- Cada API precisa de alias na sua rede interna, e o endereço de bind do
  `/metrics` vira configuração obrigatória.
- Seis redes no compose em vez das quatro de 001 TD-002.

### Riscos

- Mudança na forma como o Docker publica portas de redes internas pode
  alterar o comportamento. Mitigação: os testes de isolamento rodam contra o
  ambiente real.

## Notas de implementação

- Status: a emenda 007 TD-007 foi proposta pelo executor na atividade 6.1 e
  aplicada em 2026-11-06 sob a instrução de Eduardo de resolver bloqueios em
  lote; a confirmação explícita de Eduardo ainda está pendente. Até lá, este
  ADR fica como `Proposto`.
- Configuração em [`infra/compose.yaml`](../../infra/compose.yaml) (redes,
  aliases e `METRICAS_ENDERECO`) e em `infra/postgres/*/10-papeis.sh`.
- Gatilho de revisão: Grafana ou Prometheus precisarem de acesso externo, ou
  mudança na forma como o Docker publica portas de redes internas.

## Referências

- [05 - Implantação local: redes](../arquitetura/05-implantacao-local.md#redes)
- [ADR-012](ADR-012-prometheus-grafana.md)
