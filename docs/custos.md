# Estimativa de custos

Aqui estimo os custos com infraestrutura e licenças, um diferencial de DES
("Estimativa de custos com infraestrutura e licenças"). Defini o escopo na
decisão 006 D-003, em 2026-11-05: licenças dos componentes nas versões
fixadas e um cenário no Google Cloud com preços de página oficial, citando
fonte e data de cada número, para que qualquer valor possa ser conferido.
Mantenho as specs em `.ai/sdd/specs/`, pasta local que não versiono (decisão
006 D-006).

Fontes:

- DES = [`Desafio_Arquiteto_Soluções.md`](../Desafio_Arquiteto_Solu%C3%A7%C3%B5es.md)
- INFRA = [`Requisitos_de_Infraestrutura.md`](../Requisitos_de_Infraestrutura.md)

Legenda de origem, a mesma de [`requisitos.md`](requisitos.md): "Fonte" =
DES ou INFRA; "decisão minha" = decisão que tomei, com data; "Proposta" = não
consta nas fontes; `[NVI]` = não verificado.

Separo o que implementei do que só estimei:

- **Implementado:** execução local com Docker Compose, um nó, sem nuvem
  ([ADR-010](adr/ADR-010-docker-compose-local.md)). O custo de licença dessa
  execução é zero (seção [Licenças](#licenças)).
- **Estimativa, não implementada:** o cenário Google Cloud da seção
  [Cenário Google Cloud](#cenário-google-cloud). Infraestrutura em nuvem está
  fora do escopo desta entrega (INFRA - Fora do escopo); fiz o cenário só
  para responder ao diferencial de DES, e não o implantei nem testei.

## Licenças

Todos os componentes que executo ou uso no desenvolvimento são software
livre, sem custo de licença nas versões fixadas. Não uso nenhuma edição
comercial nem assinatura de suporte, e não estimei suporte comercial.

### Imagens de contêiner

Fixei as versões em [`infra/compose.yaml`](../infra/compose.yaml) e nos
`Dockerfile` de `services/`, cada uma com fonte e data em comentário
(001 TD-007).

| Componente | Versão fixada (imagem) | Licença | Custo de licença | Evidência da licença (conferida em 2026-11-09) |
|------------|------------------------|---------|------------------|-------------------------------------------------|
| PostgreSQL (livro e consolidado) | 18.6 (`postgres:18.6-alpine3.24`) | PostgreSQL License | Zero | [postgresql.org/about/licence](https://www.postgresql.org/about/licence/); `postgres --version` na imagem = 18.6 |
| RabbitMQ | 4.4.0 (`rabbitmq:4.4.0-management-alpine`) | MPL-2.0 (servidor); plugins e dependências com Apache-2.0, BSD, ISC e MIT | Zero | `/opt/rabbitmq/LICENSE` e `LICENSE-*` na imagem |
| Keycloak | 26.8.2 (`quay.io/keycloak/keycloak:26.8.2`) | Apache-2.0 | Zero | `/opt/keycloak/LICENSE.txt` e rótulo `org.opencontainers.image.licenses` da imagem |
| postgres_exporter | v0.20.1 (`quay.io/prometheuscommunity/postgres-exporter:v0.20.1`) | Apache-2.0 | Zero | [`LICENSE` na tag v0.20.1](https://github.com/prometheus-community/postgres_exporter/blob/v0.20.1/LICENSE) |
| Prometheus | v3.16.0 (`prom/prometheus:v3.16.0`) | Apache-2.0 | Zero | `/LICENSE` e rótulo `org.opencontainers.image.licenses` da imagem |
| Grafana OSS | 13.3.0 (`grafana/grafana:13.3.0`) | AGPL-3.0 | Zero | `/usr/share/grafana/LICENSE` na imagem |
| Python (base das imagens dos serviços) | 3.12.16 (`python:3.12.16-slim-trixie`) | PSF-2.0 | Zero | [`LICENSE` na tag v3.12.16 do CPython](https://github.com/python/cpython/blob/v3.12.16/LICENSE) |
| uv (estágio de build) | 0.13.7 (`ghcr.io/astral-sh/uv:0.13.7`) | MIT ou Apache-2.0 | Zero | [`LICENSE-MIT` e `LICENSE-APACHE` na tag 0.13.7](https://github.com/astral-sh/uv/tree/0.13.7) |

Os pacotes do sistema base das imagens (Alpine 3.24 e Debian trixie) têm
licenças livres diversas, e não os conferi um a um `[NVI]`.

### Bibliotecas Python

Versões resolvidas em [`uv.lock`](../uv.lock) e instaladas no ambiente do
workspace; li a licença dos metadados de cada pacote instalado
(`importlib.metadata`, 2026-11-09, recontado em 2026-11-10). Dependências diretas declaradas em
[`pyproject.toml`](../pyproject.toml) e nos `pyproject.toml` de `packages/`:

| Biblioteca | Versão | Licença declarada | Uso |
|------------|--------|-------------------|-----|
| FastAPI | 0.162.0 | MIT | APIs HTTP (ST-Q-002) |
| uvicorn | 0.54.0 | BSD-3-Clause | Servidor das APIs |
| psycopg | 3.3.8 | LGPL-3.0-only | Driver PostgreSQL (ST-Q-003) |
| psycopg-pool | 3.3.3 | LGPL-3.0-only | Pool de conexões |
| aio-pika | 10.2.2 | Apache-2.0 | Cliente RabbitMQ (ST-Q-004) |
| Alembic | 1.20.1 | MIT | Migrações (ST-Q-005) |
| PyJWT | 2.15.1 | MIT | Validação JWT |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | Assinatura RS256 do PyJWT |
| prometheus-client | 0.26.0 | Apache-2.0 AND BSD-2-Clause | Métricas `/metrics` |
| pytest | 9.2.0 | MIT | Testes (só desenvolvimento) |
| httpx2 | 2.16.0 | BSD-3-Clause | Cliente dos testes de API (só desenvolvimento) |
| Ruff | 0.17.4 | MIT | Lint e formatação (só desenvolvimento; ST-Q-011) |
| mypy | 2.4.1 | MIT | Verificação de tipos (só desenvolvimento; ST-Q-011) |

No ambiente há 48 pacotes: 46 de terceiros, todos com licença livre
declarada (MIT, MIT-0, BSD-2-Clause, BSD-3-Clause, Apache-2.0, LGPL-3.0-only,
MPL-2.0 ou PSF-2.0), e os 2 pacotes internos do repositório
(`observabilidade`, `seguranca`), que não declaram licença. Eram 42 até
2026-11-09; os 6 a mais entraram com Ruff e mypy no grupo `dev` em
2026-11-10 (`ruff`, `mypy`, `mypy_extensions`, `librt`, `ast_serialize`, MIT;
`pathspec`, MPL-2.0), e só rodam no desenvolvimento.

### Ferramentas de desenvolvimento e teste

| Ferramenta | Versão | Licença | Evidência (2026-11-09) |
|------------|--------|---------|------------------------|
| k6 (teste de carga) | v2.3.0 | AGPL-3.0 | [`LICENSE.md` na tag v2.3.0](https://github.com/grafana/k6/blob/v2.3.0/LICENSE.md); versão registrada em [`testes/carga-consolidado.md`](testes/carga-consolidado.md#recursos-da-máquina) |
| Ruff (lint e formatação) | 0.17.4, fixada no grupo `dev` desde 2026-11-10 ([ADR-009](adr/ADR-009-stack-python.md)) | MIT | Metadados do pacote instalado |
| mypy (tipos) | 2.4.1, fixada no grupo `dev` desde 2026-11-10 ([ADR-009](adr/ADR-009-stack-python.md)) | MIT | Metadados do pacote instalado |
| OpenSSL (AC local, `infra/tls/gerar-certificados.sh`) | A do host; 3.0.13 na máquina de desenvolvimento | Apache-2.0 | [`LICENSE.txt` na tag openssl-3.0.13](https://github.com/openssl/openssl/blob/openssl-3.0.13/LICENSE.txt) |

### Obrigações das licenças com copyleft

É a minha leitura sobre o uso que fiz nesta entrega; não é parecer jurídico.

| Licença | Componentes | Uso nesta entrega | Obrigação aplicável |
|---------|-------------|-------------------|---------------------|
| AGPL-3.0 | Grafana OSS, k6 | Imagem e binário sem modificação; k6 só roda em teste | Manter avisos de licença. Modificar o Grafana e oferecê-lo pela rede obrigaria a disponibilizar o código modificado |
| LGPL-3.0 | psycopg, psycopg-pool | Bibliotecas usadas sem modificação | Manter avisos. Modificar a biblioteca e distribuí-la obrigaria a publicar a modificação sob LGPL |
| MPL-2.0 | RabbitMQ; `pathspec` (dependência do mypy, só desenvolvimento) | Imagem e biblioteca sem modificação | Copyleft por arquivo: só arquivos modificados teriam de ser publicados |

As demais licenças (Apache-2.0, MIT, BSD, PostgreSQL License, PSF-2.0) são
permissivas e pedem a preservação dos avisos de direito autoral.

## Cenário Google Cloud

Estimo o custo mensal de executar a mesma solução no Google Cloud, sem mudar
a arquitetura: o [`infra/compose.yaml`](../infra/compose.yaml) inteiro numa
única VM do Compute Engine. Escolhi esse cenário porque é o equivalente
direto da implantação local
([05 - Implantação local](arquitetura/05-implantacao-local.md)): roda a
mesma arquitetura que testei e herda os mesmos limites, ou seja, um nó, sem
alta disponibilidade e sem backup gerenciado (INFRA - Fora do escopo;
[ADR-010](adr/ADR-010-docker-compose-local.md)).

### Premissas

| Premissa | Valor | Origem |
|----------|-------|--------|
| Região | São Paulo (`southamerica-east1`) | Proposta: o dia do lançamento é o do fuso America/Sao_Paulo ([002/D-005](requisitos.md#spec-002-registro-e-listagem-de-lançamentos), decisão minha, 2026-11-05), que tomei como indício de operação no Brasil. As fontes não definem a região |
| Modelo de cobrança | Sob demanda (preço "Default" das páginas), sem desconto por compromisso de uso nem VM spot | Proposta: não há prazo de uso definido |
| Horas por mês | 730 | As próprias páginas de preço exibem o valor mensal como o horário x 730 (conferido: em 43 regiões, o mensal da `e2-standard-4` é igual ao horário x 730) |
| Moeda | USD, sem impostos e sem conversão para BRL | Preço das páginas oficiais; câmbio não estimado |
| Data dos preços | 2026-11-09 | Consulta às páginas citadas em cada linha |

### Dimensionamento

| Insumo | Valor | Origem |
|--------|-------|--------|
| Baseline de capacidade | 4 a 8 vCPUs; 16 GB de RAM; 30 GB de armazenamento livre | INFRA, "Capacidade local": baseline provisória, marcada `[NVI]` pela própria fonte |
| Memória medida, ambiente ocioso | 1.077,2 MiB somando os 12 contêineres do projeto `cashflow` | `docker stats --no-stream` em 2026-11-09T13:43:46-03:00 |
| CPU medida sob carga | `consolidado-api` entre 25% e 59% de uma CPU a 50 consultas/s | [`testes/carga-consolidado.md`](testes/carga-consolidado.md#resultado) |

Escolhi a `e2-standard-4` (4 vCPUs, 16 GiB), o menor tipo E2 padrão que
atende à baseline de INFRA, porque a baseline é o único dimensionamento que
as fontes dão. A medição indica folga, mas não testei a solução numa máquina
de 4 vCPUs: a configuração que verifiquei tem 12 CPUs lógicas e 16 GB
(001/NFR-001) `[NVI]`. Disco: 30 GiB, a baseline de armazenamento de INFRA.

### Estimativa mensal

| Item | Quantidade | Preço unitário (São Paulo) | Mensal (USD) | Fonte (consulta em 2026-11-09) |
|------|------------|----------------------------|--------------|--------------------------------|
| VM `e2-standard-4` (4 vCPUs, 16 GiB), sob demanda | 1 VM, 730 h | USD 0,2127408 por hora | 155,30 | [Compute Engine - General-purpose machine family pricing](https://cloud.google.com/products/compute/pricing/general-purpose), tabela "E2 standard machine types"; a página informa USD 155,300784 por mês |
| Disco persistente balanceado (`pd-balanced`) | 30 GiB | USD 0,15 por GiB por mês ("Balanced provisioned space") | 4,50 | [Disk and image pricing](https://cloud.google.com/compute/disks-image-pricing) |
| Endereço IP externo em uso numa VM padrão | 1 endereço, 730 h | USD 0,005 por hora | 3,65 | [VPC network pricing](https://cloud.google.com/vpc/network-pricing), "External IP address pricing"; a página informa USD 3,65 por mês |
| Licença do sistema operacional | Imagem pública não premium (por exemplo, Debian) | Zero | 0,00 | [Disk and image pricing](https://cloud.google.com/compute/disks-image-pricing), "Premium images": a lista de imagens cobradas (RHEL, SLES, Ubuntu Pro, Windows Server, SQL Server) não inclui o Debian |
| Licenças dos componentes | Seção [Licenças](#licenças) | Zero | 0,00 | Este documento |
| **Total** | | | **163,45** | Soma das linhas: 155,300784 + 4,50 + 3,65 = 163,450784 |

### Sensibilidade

Uso as mesmas fontes e data da tabela acima; só muda o item indicado.

| Variação | VM (USD/mês) | Disco 30 GiB (USD/mês) | IP (USD/mês) | Total (USD/mês) | Observação |
|----------|--------------|------------------------|--------------|-----------------|------------|
| Cenário base, São Paulo, `e2-standard-4` | 155,300784 | 4,50 | 3,65 | 163,45 | Tabela acima |
| São Paulo, `e2-standard-2` (2 vCPUs, 8 GiB) | 77,650392 (USD 0,1063704 por hora) | 4,50 | 3,65 | 85,80 | Abaixo da baseline de INFRA; cabe na memória medida em repouso, mas não testei com carga `[NVI]` |
| Iowa (`us-central1`), `e2-standard-4` | 97,8366732 (USD 0,13402284 por hora) | 3,00 (USD 0,10 por GiB por mês) | 3,65 | 104,49 | Incluí para mostrar o peso da região; não medi a latência até o Brasil |

### Fora da estimativa

| Item | Motivo |
|------|--------|
| Tráfego de saída para a Internet | Depende do volume e do tamanho das respostas, que não medi |
| Snapshots e backup | Política numérica de backup, retenção, RPO e RTO pendente (INFRA 7.7). Referência para quando for definida: snapshot padrão em São Paulo custa USD 0,08 por GiB por mês ([Disk and image pricing](https://cloud.google.com/compute/disks-image-pricing), 2026-11-09) |
| Alta disponibilidade, zonas múltiplas, balanceador, PostgreSQL e RabbitMQ gerenciados ou em cluster | Fora do escopo (INFRA - Fora do escopo); caminhos em [`evolucoes.md`](evolucoes.md) |
| Certificado TLS reconhecido publicamente e gestão de segredos em cofre | A AC local atende só ao desenvolvimento (INFRA 5.8); evolução em [`evolucoes.md`](evolucoes.md) |
| Cloud Logging, Cloud Monitoring e plano de suporte | Não os uso: métricas e painéis rodam no próprio compose ([ADR-012](adr/ADR-012-prometheus-grafana.md)) |
| Descontos por compromisso de uso, nível gratuito e impostos | Fora da premissa de preço sob demanda |

Para recalcular em outra data ou região, selecione a região nas mesmas
páginas e substitua os preços unitários; as quantidades não mudam.
