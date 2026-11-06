# Papel do Arquiteto de Soluções

- O Arquiteto de Soluções é responsável por compreender e transformar requisitos de negócios, sejam funcionais ou não-funcionais, em
capacidades e/ou competências para realizar atividades que gerem valor para a Organização;

- É de responsabilidade do Arquiteto de Soluções desenhar arquiteturas de contexto com as distribuições e responsabilidades dos processos e
etapas que devem ser realizadas de forma isolada ou não, habilitando a segregação das capacidades;

- É requerido do Arquiteto de Soluções uma capacidade analítica necessária para a definição de conceitos e o desenho de processos e
soluções que componham a cadeia de valor de cada negócio da organização;

- Da mesma forma, é de responsabilidade tornar essas soluções escaláveis, reutilizáveis e flexíveis necessários para suportar a
estratégia de negócios e a arquitetura de referência;

- O Arquiteto de Soluções deve possuir grande capacidade de comunicação e visão sistêmica, definindo estratégias de integração
entre áreas, atividades, serviços e/ou sistemas que juntos alcançam o resultado esperado no planejamento estratégico da Organização;

# Objetivo do Desafio

Desenvolver uma arquitetura que integre processos e sistemas de forma eficiente, garantindo a entrega de valor para a organização. Isso inclui a

definição de contextos, capacidades de negócio e domínios funcionais, escalabilidade das soluções para garantir alta disponibilidade, segurança e

desempenho, a comunicação eficaz entre áreas e serviços, a seleção adequada de padrões arquiteturais, integração de tecnologias e frameworks,

além de otimização de requisitos não-funcionais. Deve abranger a capacidade analítica, a visão sistêmica e a habilidade de criar soluções

flexíveis e reutilizáveis.

- • **Compreensão dos Requisitos de Negócios**: O Arquiteto de Soluçõesdeve entender profundamente os requisitos funcionais e não funcionais
da organização. Isso inclui compreender as capacidades necessárias para gerar valor.

- • **Arquitetura Corporativa**: O foco deve estar na criação dearquiteturas que se alinhem com o contexto de cada negócio. Isso
envolve definir processos, etapas e responsabilidades de forma isolada ou integrada.

- • **Escalabilidade**: Garanta que a arquitetura possa lidar com o aumentoda carga de trabalho sem degradação significativa do desempenho.
Considere dimensionamento horizontal, balanceamento de carga e estratégias de cache.

- • **Resiliência**: Projete para a recuperação de falhas. Isso incluiredundância, failover, monitoramento proativo e estratégias de
recuperação.

- • **Segurança**: Proteja os dados e sistemas contra ameaças. Implementeautenticação, autorização, criptografia e mecanismos de proteção
contra ataques.

- • **Padrões Arquiteturais**: Escolha padrões adequados, comomicrosserviços, monolitos, SOA ou serverless. Considere trade-offs
entre simplicidade e flexibilidade.

- • **Integração**: Defina como os componentes se comunicarão. Avalieprotocolos, formatos de mensagem e ferramentas de integração.
- • **Requisitos Não-Funcionais**: Otimize para desempenho,disponibilidade e confiabilidade. Defina métricas e metas claras.
- • **Documentação**: Registre decisões arquiteturais, diagramas e fluxos dedados. Isso facilita a comunicação e a manutenção.
Lembrando que não é necessário que todas essas premissas sejam apresentadas na codificação, mas nas decisões e representações

arquiteturais do projeto. A intenção do desafio é analisar o seu conhecimento empírico, capacidade de tomada de decisão, aplicação de

boas práticas, decomposição dos domínios e componentes, etc.

# Descritivo da Solução

Um comerciante precisa controlar o seu fluxo de caixa diário com os lançamentos (débitos e créditos), também precisa de um relatório que

disponibilize o saldo diário consolidado.

# Requisitos de negócio

- Serviço que faça o controle de lançamentos
- Serviço do consolidado diário
# Requisitos obrigatórios

- Mapeamento de domínios funcionais e capacidades de negócio
- Refinamento do Levantamento de requisitos funcionais e não funcionais
- Desenho da solução completo (Arquitetura Alvo)
- Justificativa na decisão/escolha de ferramentas/tecnologias e de tipo de arquitetura
- Pode ser feito na linguagem que você domina
- Testes
- Readme com instruções claras de como a aplicação funciona, e como rodar localmente
- Hospedar em repositório publico (GitHub)
- Todas as documentações de projeto devem estar no repositório

Caso os requisitos técnicos obrigatórios não sejam minimamente atendidos, o teste será descartado.

# Requisitos diferenciais

- Desenho da solução da Arquitetura de Transição (se necessária, considerando uma migração de legado)
- Estimativa de custos com infraestrutura e licenças
- Monitoramento e Observabilidade
- Critérios de segurança para consumo (integração) de serviços
# Requisitos não funcionais

O serviço de controle de lançamento não deve ficar indisponível se o sistema de consolidado diário cair. Em dias de picos, o serviço de consolidado diário

recebe 50 requisições por segundo, com no máximo 5% de perda de requisições.

# Observações

Leve em consideração todos os critérios técnicos mencionados, mas não se prenda somente a eles. Use o teste para demonstrar sua habilidade em

tomar decisões sobre o que é importante durante a definição de soluções para o problema de negócio.

Também são bem vindas descrições sobre o que você gostaria de ter implementado ou evoluções futuras para o sistema proposto. Entendemos

que o tempo para a execução do projeto é limitado, então aproveite a documentação do seu projeto para apresentar pontos que possam

demonstrar melhor suas capacidades, além do que é possível demonstrar através do desenolvimento desse desafio.

Desejamos boa sorte para você!
