Requisitos de Infraestrutura
Lista derivada da especificação, plano e ADRs aprovados até o commit 89130c6.

Legenda:

- Obrigatório: exigência do desafio ou necessária ao funcionamento.
- Aprovado: decisão arquitetural interna.
- Pendente: ainda exige medição ou escolha operacional.

1. Ambiente de desenvolvimento
1.1. Sistema Linux compatível com contêineres.

1.2. Git instalado.

1.3. Python 3.12.

1.4. Docker Engine.

1.5. Docker Compose v2.

1.6. Gerenciador de dependências Python, ainda a definir.

1.7. Portas locais disponíveis para APIs, Keycloak e interfaces administrativas autorizadas.
2. Componentes executáveis
2.1. Serviço de lançamentos:

- API HTTP/HTTPS;
- domínio e aplicação hexagonais;
- adaptadores PostgreSQL;
- confirmação independente do consolidado e do RabbitMQ.
2.2. Relay da outbox:
- consulta periódica da outbox;
- controle de lease por tentativa;
- publicação no RabbitMQ;
- publisher confirms;
- retentativas controladas.
2.3. Serviço de consolidado:
- API de consulta de saldo;
- API de consulta do sinal de inclusão;
- leitura somente da geração ativa.
2.4. Worker consumidor:
- consumo do RabbitMQ;
- ACK manual após commit;
- deduplicação por evento e lançamento;
- quarentena durável para conflitos.
2.5. Coordenador operacional:
- reconciliação;
- reconstrução de projeções;
- controle das gerações;
- barreira de consumidores;
- troca atômica da geração ativa.

3. Bancos PostgreSQL
3.1. Uma instância PostgreSQL exclusiva para o livro:

- lançamentos imutáveis;
- respostas idempotentes;
- contador de posição monotônica;
- outbox;
- metadados de publicação.
3.2. Uma instância PostgreSQL exclusiva para o consolidado:
- inbox;
- contribuições;
- saldos;
- marcadores de inclusão;
- gerações;
- checkpoints;
- controle da projeção;
- quarentena.
3.3. Requisitos de isolamento:
- volumes separados;
- credenciais separadas;
- pools separados;
- migrações separadas;
- ausência de SQL cruzado;
- ausência de chaves estrangeiras entre serviços;
- ausência de transação distribuída.

4. RabbitMQ
4.1. Instância RabbitMQ local em contêiner.

4.2. Volume persistente.

4.3. Exchange durável.

4.4. Filas duráveis.

4.5. Mensagens persistentes.

4.6. Publicação com mandatory.

4.7. Publisher confirms.

4.8. ACK manual somente após commit da aplicação.

4.9. Fila ou armazenamento durável para quarentena.

4.10. Nenhum descarte silencioso de eventos financeiros.

4.11. RabbitMQ não será fonte de verdade financeira nem histórico obrigatório para reconstrução.
5. Identidade e segurança
5.1. Keycloak local e substituível.

5.2. OAuth 2.0 Client Credentials para clientes técnicos.

5.3. Access tokens JWT assinados assimetricamente.

5.4. Escopos mínimos:

- lancamentos:registrar;
- consolidado:consultar.
5.5. Validação JWT diretamente em cada API.

5.6. Cache controlado de JWKS.

5.7. TLS para APIs e provedor de identidade.

5.8. Autoridade certificadora local para desenvolvimento.

5.9. Segredos fora do Git.

5.10. .env.example sem credenciais reais.

5.11. Identidades técnicas separadas para:

- runtime das APIs;
- migrações;
- relay;
- worker;
- reconstrução e reconciliação;
- administração do Keycloak.
5.12. Não haverá API Gateway, RBAC ou ABAC nesta entrega.

6. Rede e exposição
6.1. Redes Docker internas para bancos, RabbitMQ e componentes operacionais.

6.2. Bancos não expostos publicamente.

6.3. RabbitMQ não exposto a clientes externos.

6.4. Interfaces de replay e reconstrução com acesso restrito.

6.5. APIs e Keycloak publicados somente nas interfaces necessárias.

6.6. Em ambiente local, preferência por bind em loopback.

6.7. Sem acesso direto do consolidado ao banco do livro.
7. Persistência e recuperação
7.1. Volumes persistentes separados para:

- PostgreSQL do livro;
- PostgreSQL do consolidado;
- RabbitMQ;
- configuração ou persistência necessária do Keycloak.
7.2. Preservação do livro e da outbox para replay.

7.3. Retenção suficiente para reconstrução completa da projeção.

7.4. Geração candidata armazenada separadamente da geração ativa.

7.5. Capacidade temporária para manter duas gerações do consolidado.

7.6. Procedimento de recuperação após perda do RabbitMQ usando livro e outbox.

7.7. Política numérica de retenção, backup, RPO e RTO permanece pendente.
8. Observabilidade
8.1. Logs estruturados e correlacionados.

8.2. Redação de tokens, senhas, segredos e conteúdo financeiro.

8.3. Métricas para:

- backlog da outbox;
- idade do evento mais antigo;
- mensagens prontas e não confirmadas;
- reentregas;
- quarentena;
- pools de conexão;
- espaço em disco;
- geração ativa;
- fase da reconstrução;
- checkpoint;
- lacunas;
- falhas de JWT e JWKS.
8.4. Health checks para APIs, bancos, RabbitMQ, Keycloak, relay e worker.

8.5. Readiness distinta de simples processo em execução.

8.6. Backend específico de métricas e visualização ainda não definido.
9. Infraestrutura para testes
9.1. PostgreSQL real para integração e concorrência.

9.2. RabbitMQ real para testes de entrega, ACK e reentrega.

9.3. Keycloak real para autenticação integrada.

9.4. Gerador de carga com chegada fixa:

- 50 consultas por segundo;
- duração de 300 segundos;
- total de 15.000 consultas;
- observação individual por até 10 segundos.
9.5. Instrumentação para injeção controlada de falhas.

9.6. Capacidade para testar:

- queda do consolidado;
- queda do consumidor;
- perda de ACK;
- duplicação de eventos;
- indisponibilidade do RabbitMQ;
- reconstrução concorrente;
- troca de geração;
- rollback controlado;
- indisponibilidade do Keycloak.
 1. Capacidade local
[NVI] Não existe dimensionamento mínimo validado por testes.

Baseline provisória recomendada:

- 4 a 8 vCPUs;
- 16 GB de RAM disponível;
- 30 GB de armazenamento livre;
- suporte a virtualização;
- espaço adicional para duas gerações do consolidado;
- relógio do sistema sincronizado.
Essa baseline precisa ser confirmada por medição.
 1. Fora do escopo atual
Não são requisitos desta entrega:

- Kubernetes;
- alta disponibilidade produtiva;
- múltiplas zonas ou regiões;
- balanceador de carga;
- API Gateway;
- infraestrutura em nuvem;
- multiempresa;
- disaster recovery produtivo;
- autoscaling;
- SLA, RPO ou RTO numéricos;
- cluster RabbitMQ;
- cluster PostgreSQL.
