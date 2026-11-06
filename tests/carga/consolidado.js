// Teste de carga do consolidado: chegada fixa de 50 consultas/s por 300 s,
// 15.000 consultas, cada uma observada por até 10 s (004-T6; NFR-001;
// INFRA 9.4; ST-Q-007). Aprovado com pelo menos 14.250 respostas de sucesso.
//
// Cenário (004 NFR-001): consulta de saldo por data (FR-004) com datas
// existentes. O setup garante essas datas: registra, pelo cliente técnico de
// lançamentos, um crédito e um débito em cada um dos 30 dias de outubro de
// 2026 com chaves de idempotência fixas, de modo que reexecutar o teste não
// cria lançamentos novos no livro (002 D-006), e espera o worker incluí-los
// no consolidado antes de começar a carga.
//
// Sucesso = status 200, corpo com a data pedida e resposta em até 10 s. A
// consulta que não chega a ser enviada (iteração descartada pelo k6 ou token
// não obtido) conta como perda. O token de `cliente-consultas` dura 300 s
// (realm `accessTokenLifespan`), menos que o teste: cada VU renova o seu
// quando faltar menos de 60 s.
//
// Roda no host contra as APIs publicadas em loopback, confiando só na AC
// local, sem `insecureSkipTLSVerify`. A partir da raiz do repositório, com o
// ambiente no ar e `infra/.env` preenchido:
//
//     SSL_CERT_FILE=infra/tls/gerados/ac/ac-cashflow.crt \
//       uv run --env-file infra/.env -- k6 run tests/carga/consolidado.js
//
// Variáveis opcionais: CONSOLIDADO_URL, LANCAMENTOS_URL, KEYCLOAK_URL (portas
// padrão do compose), TAXA e DURACAO_S (só para ensaio; a verificação usa os
// padrões) e RESUMO_JSON (caminho para gravar o resumo em JSON).

import http from 'k6/http';
import { fail, sleep } from 'k6';
import exec from 'k6/execution';
import { Counter, Trend } from 'k6/metrics';

const CONSOLIDADO_URL = __ENV.CONSOLIDADO_URL || 'https://localhost:8445';
const LANCAMENTOS_URL = __ENV.LANCAMENTOS_URL || 'https://localhost:8444';
const KEYCLOAK_URL = __ENV.KEYCLOAK_URL || 'https://localhost:8443';
const TOKEN_URL = `${KEYCLOAK_URL}/realms/cashflow/protocol/openid-connect/token`;

const TAXA = parseInt(__ENV.TAXA || '50', 10);
const DURACAO_S = parseInt(__ENV.DURACAO_S || '300', 10);
// O executor dispara também no instante final (taxa x duração + 1); a
// iteração excedente sai sem consultar, para o total ser o de INFRA 9.4.
const TOTAL = TAXA * DURACAO_S;
const LIMITE_RESPOSTA_MS = 10000;
const MINIMO_SUCESSOS = 14250;

// Massa fixa: 30 datas, um crédito e um débito em cada.
const DATAS = Array.from({ length: 30 }, (_, i) => `2026-10-${String(i + 1).padStart(2, '0')}`);
const VERSAO_MASSA = 'v1';
const ESPERA_INCLUSAO_S = 120;
const RENOVAR_ANTES_MS = 60000;

const consultasEnviadas = new Counter('consultas_enviadas');
const consultasSucesso = new Counter('consultas_sucesso');
const consultasSemToken = new Counter('consultas_sem_token');
const consultaDuracao = new Trend('consulta_duracao', true);

export const options = {
  scenarios: {
    consultas: {
      executor: 'constant-arrival-rate',
      rate: TAXA,
      timeUnit: '1s',
      duration: `${DURACAO_S}s`,
      preAllocatedVUs: TAXA,
      // Com respostas no limite de 10 s, 50/s exigem até 500 VUs em voo.
      maxVUs: TAXA * 10,
      exec: 'consultar',
    },
  },
  setupTimeout: '300s',
  thresholds: {
    consultas_sucesso: [`count>=${MINIMO_SUCESSOS}`],
  },
  // O resumo calcula também a mediana e os percentis 99 e 99,9.
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'p(99.9)', 'max'],
};

function obterToken(cliente, segredo) {
  const resposta = http.post(
    TOKEN_URL,
    { grant_type: 'client_credentials', client_id: cliente, client_secret: segredo },
    { tags: { nome: 'token' }, timeout: '10s' },
  );
  if (resposta.status !== 200) {
    return null;
  }
  const corpo = resposta.json();
  return { valor: corpo.access_token, expiraEm: Date.now() + corpo.expires_in * 1000 };
}

function segredo(nome) {
  const valor = __ENV[nome];
  if (!valor) {
    fail(`${nome} ausente: executar com uv run --env-file infra/.env (ver cabeçalho)`);
  }
  return valor;
}

function semear() {
  const token = obterToken('cliente-lancamentos', segredo('KEYCLOAK_SEGREDO_CLIENTE_LANCAMENTOS'));
  if (token === null) {
    fail('token de cliente-lancamentos não obtido');
  }
  const ids = [];
  for (const data of DATAS) {
    for (const [tipo, valor] of [['CREDITO', 150.25], ['DEBITO', 40.1]]) {
      const resposta = http.post(
        `${LANCAMENTOS_URL}/v1/lancamentos`,
        JSON.stringify({ tipo, valor, data_lancamento: data, descricao: 'massa do teste de carga' }),
        {
          headers: {
            Authorization: `Bearer ${token.valor}`,
            'Content-Type': 'application/json',
            'Idempotency-Key': `carga-consolidado-${VERSAO_MASSA}-${data}-${tipo}`,
          },
          tags: { nome: 'semear' },
          timeout: '10s',
        },
      );
      // 201 na primeira execução; 200 na repetição com o mesmo conteúdo.
      if (resposta.status !== 201 && resposta.status !== 200) {
        fail(`registro de ${data} ${tipo} falhou com status ${resposta.status}`);
      }
      ids.push(resposta.json('id'));
    }
  }
  return ids;
}

function esperarInclusao(ids, token) {
  const pendentes = new Set(ids);
  const limite = Date.now() + ESPERA_INCLUSAO_S * 1000;
  while (pendentes.size > 0 && Date.now() < limite) {
    for (const id of [...pendentes]) {
      const resposta = http.get(`${CONSOLIDADO_URL}/v1/consolidado/inclusoes/${id}`, {
        headers: { Authorization: `Bearer ${token.valor}` },
        tags: { nome: 'inclusao' },
        timeout: '10s',
      });
      if (resposta.status === 200 && resposta.json('incluido') === true) {
        pendentes.delete(id);
      }
    }
    if (pendentes.size > 0) {
      sleep(1);
    }
  }
  if (pendentes.size > 0) {
    fail(`${pendentes.size} lançamentos da massa não incluídos em ${ESPERA_INCLUSAO_S} s`);
  }
}

export function setup() {
  const ids = semear();
  const token = obterToken('cliente-consultas', segredo('KEYCLOAK_SEGREDO_CLIENTE_CONSULTAS'));
  if (token === null) {
    fail('token de cliente-consultas não obtido');
  }
  esperarInclusao(ids, token);
}

// Token por VU, renovado antes de expirar.
let tokenDoVu = null;

function tokenValido() {
  if (tokenDoVu === null || tokenDoVu.expiraEm - Date.now() < RENOVAR_ANTES_MS) {
    tokenDoVu = obterToken('cliente-consultas', segredo('KEYCLOAK_SEGREDO_CLIENTE_CONSULTAS'));
  }
  return tokenDoVu;
}

export function consultar() {
  if (exec.scenario.iterationInTest >= TOTAL) {
    return;
  }
  const data = DATAS[exec.scenario.iterationInTest % DATAS.length];
  const token = tokenValido();
  if (token === null) {
    consultasSemToken.add(1);
    return;
  }
  consultasEnviadas.add(1);
  const resposta = http.get(`${CONSOLIDADO_URL}/v1/consolidado/saldos/${data}`, {
    headers: { Authorization: `Bearer ${token.valor}` },
    tags: { nome: 'saldo_da_data' },
    timeout: `${LIMITE_RESPOSTA_MS}ms`,
  });
  consultaDuracao.add(resposta.timings.duration);
  const sucesso =
    resposta.status === 200 &&
    resposta.timings.duration <= LIMITE_RESPOSTA_MS &&
    resposta.json('data') === data;
  if (sucesso) {
    consultasSucesso.add(1);
  }
}

function contagem(dados, nome) {
  const metrica = dados.metrics[nome];
  return metrica ? metrica.values.count : 0;
}

export function handleSummary(dados) {
  const enviadas = contagem(dados, 'consultas_enviadas');
  const sucesso = contagem(dados, 'consultas_sucesso');
  const semToken = contagem(dados, 'consultas_sem_token');
  const descartadas = contagem(dados, 'dropped_iterations');
  // Programadas = iterações executadas mais as que o gerador descartou.
  const total = enviadas + semToken + descartadas;
  const duracao = dados.metrics.consulta_duracao ? dados.metrics.consulta_duracao.values : {};
  const resumo = {
    programadas: total,
    enviadas,
    sucesso,
    perda: total - sucesso,
    perda_percentual: total > 0 ? Number((((total - sucesso) / total) * 100).toFixed(3)) : 0,
    nao_enviadas_sem_token: semToken,
    descartadas_pelo_gerador: descartadas,
    minimo_sucessos: MINIMO_SUCESSOS,
    aprovado: sucesso >= MINIMO_SUCESSOS,
    duracao_ms: duracao,
    vus_max: dados.metrics.vus_max ? dados.metrics.vus_max.values.max : null,
  };
  const linhas = [
    '',
    'Teste de carga do consolidado (004-T6; INFRA 9.4)',
    `  programadas: ${resumo.programadas}`,
    `  enviadas:    ${resumo.enviadas}`,
    `  sucesso:     ${resumo.sucesso} (mínimo ${MINIMO_SUCESSOS})`,
    `  perda:       ${resumo.perda} (${resumo.perda_percentual}%)`,
    `  sem token:   ${resumo.nao_enviadas_sem_token}`,
    `  descartadas: ${resumo.descartadas_pelo_gerador}`,
    `  duração (ms): ${Object.entries(duracao)
      .map(([k, v]) => `${k}=${v.toFixed(2)}`)
      .join(' ')}`,
    `  resultado:   ${resumo.aprovado ? 'APROVADO' : 'REPROVADO'}`,
    '',
  ];
  const saida = { stdout: linhas.join('\n') };
  if (__ENV.RESUMO_JSON) {
    saida[__ENV.RESUMO_JSON] = JSON.stringify(resumo, null, 2);
  }
  return saida;
}
