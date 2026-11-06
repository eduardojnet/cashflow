#!/bin/sh
# Papéis do banco do consolidado (001-T6; design 001 §4, TD-003; INFRA 3.3,
# 5.11).
#
# Roda pelo entrypoint da imagem do PostgreSQL somente na criação do volume.
# É idempotente: reaplicar em volume existente cria o que faltar, reafirma
# atributos e privilégios e troca as senhas pelas de infra/.env:
#
#   docker compose -f infra/compose.yaml exec pg-consolidado \
#     /docker-entrypoint-initdb.d/10-papeis.sh
#
# Privilégio mínimo nesta fase, sem tabelas de negócio:
#   - nenhum papel conecta a outro banco da instância nem cria tabela temporária;
#   - só consolidado_migracao cria objetos (DDL) no esquema public;
#   - consolidado_worker, consolidado_api e consolidado_operacao só usam o
#     esquema. Os privilégios por tabela, inclusive a leitura exclusiva de
#     consolidado_api, são concedidos pelas migrações que criam as tabelas
#     (004-T1, 005);
#   - consolidado_monitoramento, do exporter de métricas, só lê estatísticas
#     pelo papel predefinido pg_monitor, sem acesso ao esquema nem às tabelas
#     (007-T1; 007 design §6; INFRA 5.11).
set -eu

: "${PG_CONSOLIDADO_SENHA_MIGRACAO:?defina PG_CONSOLIDADO_SENHA_MIGRACAO em infra/.env}"
: "${PG_CONSOLIDADO_SENHA_WORKER:?defina PG_CONSOLIDADO_SENHA_WORKER em infra/.env}"
: "${PG_CONSOLIDADO_SENHA_API:?defina PG_CONSOLIDADO_SENHA_API em infra/.env}"
: "${PG_CONSOLIDADO_SENHA_OPERACAO:?defina PG_CONSOLIDADO_SENHA_OPERACAO em infra/.env}"
: "${PG_CONSOLIDADO_SENHA_MONITORAMENTO:?defina PG_CONSOLIDADO_SENHA_MONITORAMENTO em infra/.env}"

# As senhas entram como variáveis do psql e são citadas por :'nome', sem passar
# pela linha de comando de outro processo nem pelo log.
psql -v ON_ERROR_STOP=1 --no-psqlrc \
  --username "${POSTGRES_USER:-postgres}" --dbname consolidado \
  -v senha_migracao="$PG_CONSOLIDADO_SENHA_MIGRACAO" \
  -v senha_worker="$PG_CONSOLIDADO_SENHA_WORKER" \
  -v senha_api="$PG_CONSOLIDADO_SENHA_API" \
  -v senha_operacao="$PG_CONSOLIDADO_SENHA_OPERACAO" \
  -v senha_monitoramento="$PG_CONSOLIDADO_SENHA_MONITORAMENTO" <<'SQL'
\set QUIET on
BEGIN;

SELECT format('CREATE ROLE %I', papel)
FROM unnest(ARRAY['consolidado_migracao', 'consolidado_worker', 'consolidado_api', 'consolidado_operacao', 'consolidado_monitoramento']) AS papel
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = papel)
\gexec

ALTER ROLE consolidado_migracao      WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'senha_migracao';
ALTER ROLE consolidado_worker        WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'senha_worker';
ALTER ROLE consolidado_api           WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'senha_api';
ALTER ROLE consolidado_operacao      WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'senha_operacao';
ALTER ROLE consolidado_monitoramento WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'senha_monitoramento';

-- Leitura de estatísticas e tamanho dos bancos para o exporter (007-T1).
GRANT pg_monitor TO consolidado_monitoramento;

-- PUBLIC recebe CONNECT e TEMP em todo banco por padrão; aqui cada papel
-- recebe só o que é seu.
REVOKE ALL ON DATABASE consolidado, postgres, template1 FROM PUBLIC;
GRANT CONNECT ON DATABASE consolidado TO consolidado_migracao, consolidado_worker, consolidado_api, consolidado_operacao, consolidado_monitoramento;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO consolidado_migracao;
GRANT USAGE ON SCHEMA public TO consolidado_worker, consolidado_api, consolidado_operacao;

COMMIT;
SQL
