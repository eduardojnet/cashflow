# ADR-010: Docker Compose local; Kubernetes fora do escopo

> Status: Aceito
> Data: 2026-11-05
> Relacionado: INFRA 1.1 a 1.7, 6.1 a 6.7, 7.1; INFRA - Fora do escopo; ST-TD-006; 001 TD-001, TD-002, TD-006, TD-007; 001 D-003

## Contexto

A solução precisa rodar na máquina do avaliador a partir do README (DES).
INFRA 1.1 a 1.5 pedem Linux com Docker Engine e Docker Compose v2; INFRA - Fora
do escopo exclui Kubernetes, alta disponibilidade, balanceador, nuvem e
clusters. INFRA §6 exige redes internas para bancos, RabbitMQ e componentes
operacionais, nenhum banco ou broker exposto, bind em loopback e nenhuma rota
do consolidado ao banco do livro. INFRA 7.1 pede volumes persistentes
separados. As imagens de terceiros precisam ser reprodutíveis.

## Decisão

- **Um arquivo Compose (001 TD-001):** [`infra/compose.yaml`](../../infra/compose.yaml),
  projeto `cashflow`, um nó, com todos os componentes: bancos, RabbitMQ,
  Keycloak, APIs, relay, worker, Prometheus, Grafana e exporters.
  Migrações (`livro-migracao`, `consolidado-migracao`) rodam sob demanda no
  perfil `migracao`; coordenador e comandos do lado do livro
  (`relay-republicar`, `livro-relatorio`), no perfil `operacao`.
- **Redes por fronteira (001 TD-002):** `rede-livro`, `rede-consolidado` e
  `rede-mensageria` internas; `rede-identidade` para Keycloak e APIs; e, pela
  emenda da spec 007, `rede-observabilidade` interna e
  `rede-publicacao-grafana` ([ADR-016](ADR-016-isolamento-de-rede-da-observabilidade.md)).
  Nenhuma rede liga o lado do consolidado a `pg-livro`.
- **Publicação só em `127.0.0.1`:** Keycloak (8443), `lancamentos-api`
  (8444), `consolidado-api` (8445) e Grafana (8446), todas com TLS e com a
  porta do host configurável por variável.
- **Volumes separados:** `vol-pg-livro`, `vol-pg-consolidado`,
  `vol-rabbitmq`, `vol-keycloak`, mais os de Prometheus e Grafana.
- **Health checks com readiness real** em todos os serviços contínuos
  (INFRA 8.4, 8.5), e dependências de subida por `depends_on` com condição de
  saúde.
- **Versões fixadas (001 TD-007):** cada imagem de terceiros com a versão
  estável vigente na data da implementação, conferida na página oficial e
  registrada com fonte e data em comentário no compose. Nenhuma tag
  `latest`.
- **Interface administrativa do RabbitMQ (001 TD-006):** a decisão previa
  publicá-la só em `127.0.0.1`. Ver "Notas de implementação".
- **Plataforma suportada (001 D-003):** só Linux é suportado e testado; macOS
  e Windows podem funcionar com Docker Desktop, sem garantia.

## Alternativas consideradas

### Alternativa 1: Kubernetes local (kind, minikube ou k3d)

- **Origem:** spec (001 TD-001); excluída por INFRA - Fora do escopo.
- **Descrição:** manifests ou charts num cluster local.
- **Prós:** mais próximo de um ambiente produtivo; políticas de rede nativas.
- **Contras:** mais pré-requisitos e consumo de recursos na máquina do
  avaliador; manifestos e operação mais longos que a própria aplicação.
- **Por que foi rejeitada:** INFRA 1.5 pede Docker Compose e Kubernetes está
  fora do escopo.

### Alternativa 2: Rede única para todos os contêineres

- **Origem:** spec (001 TD-002).
- **Descrição:** uma rede padrão do Compose.
- **Prós:** configuração mínima.
- **Contras:** qualquer contêiner alcança qualquer banco.
- **Por que foi rejeitada:** não impede o acesso do consolidado ao livro
  (INFRA 6.7) nem isola bancos e broker (INFRA 6.1).

### Alternativa 3: Um Compose por serviço

- **Origem:** análise do ADR.
- **Descrição:** arquivos separados para livro, consolidado, identidade e
  observabilidade, combinados com `-f`.
- **Prós:** arquivos menores.
- **Contras:** redes e volumes compartilhados declarados em vários lugares;
  comando de subida mais longo e fácil de errar.
- **Por que foi rejeitada:** um arquivo único é a menor solução que atende
  (P-011) e simplifica o README.

### Alternativa 4: Tags móveis (`latest` ou só a versão maior)

- **Origem:** análise do ADR (001 TD-007 escolheu versão exata).
- **Descrição:** usar a última imagem publicada a cada subida.
- **Prós:** atualizações automáticas.
- **Contras:** a mesma subida pode resultar em ambientes diferentes em datas
  diferentes.
- **Por que foi rejeitada:** reprodutibilidade para o avaliador.

## Consequências

### Positivas

- `docker compose up` sobe a solução inteira numa máquina Linux.
- Isolamento verificado: o host não alcança bancos nem RabbitMQ, o Keycloak
  só atende em loopback, e o lado do consolidado não alcança `pg-livro`
  ([`tests/infra/test_isolamento.py`](../../tests/infra/test_isolamento.py));
  o host também não alcança Prometheus, exporters nem `/metrics`
  ([`tests/observabilidade/test_alvos.py`](../../tests/observabilidade/test_alvos.py)).
- Dados preservados entre reinícios pelos volumes.

### Negativas / trade-offs

- Sem alta disponibilidade, balanceamento ou escala horizontal: um nó, uma
  instância de cada componente.
- Seis redes e várias identidades tornam o compose longo.
- Mudança de esquema ou de realm exige recriar o volume correspondente.

### Riscos

- Capacidade da máquina do avaliador. A solução foi verificada numa máquina
  com 12 CPUs lógicas, 16 GB de RAM e 23,7 GiB de disco livre
  ([relatório de carga](../testes/carga-consolidado.md#recursos-da-máquina));
  máquina menor não foi testada (001/NFR-001).

## Notas de implementação

- Especificado e implementado (001 TD-006): a imagem é a variante
  `management` do RabbitMQ, mas nenhuma porta do RabbitMQ é publicada no
  host, nem em `127.0.0.1`. A interface administrativa fica acessível só de
  dentro da `rede-mensageria`, o que é mais restrito que a decisão e atende
  INFRA 6.3 e 6.6; `test_isolamento.py` confirma que o host não conecta.
- Imagens e fontes de versão: PostgreSQL `18.6-alpine3.24`, RabbitMQ
  `4.4.0-management-alpine`, Keycloak `26.8.2`, Prometheus `v3.16.0`, Grafana
  `13.3.0`, postgres-exporter `v0.20.1`; fonte e data em comentário junto de
  cada `image:` no compose.
- Detalhes de redes, portas, volumes e identidades:
  [05 - Implantação local](../arquitetura/05-implantacao-local.md).
- Gatilho de revisão: requisito de alta disponibilidade, de implantação em
  nuvem ou de mais de um nó.

## Referências

- [`infra/compose.yaml`](../../infra/compose.yaml)
- [05 - Implantação local](../arquitetura/05-implantacao-local.md)
