#!/bin/sh
# Papéis do banco do livro (001-T6; design 001 §4, TD-003; INFRA 3.3, 5.11).
#
# Roda pelo entrypoint da imagem do PostgreSQL somente na criação do volume.
# É idempotente: reaplicar em volume existente cria o que faltar, reafirma
# atributos e privilégios e troca as senhas pelas de infra/.env:
#
#   docker compose -f infra/compose.yaml exec pg-livro \
#     /docker-entrypoint-initdb.d/10-papeis.sh
#
# Privilégio mínimo nesta fase, sem tabelas de negócio:
#   - nenhum papel conecta a outro banco da instância nem cria tabela temporária;
#   - só livro_migracao cria objetos (DDL) no esquema public;
#   - livro_api, livro_relay e livro_replay só usam o esquema. Os privilégios
#     por tabela são concedidos pelas migrações que criam as tabelas
#     (002-T2, 003).
set -eu

: "${PG_LIVRO_SENHA_MIGRACAO:?defina PG_LIVRO_SENHA_MIGRACAO em infra/.env}"
: "${PG_LIVRO_SENHA_API:?defina PG_LIVRO_SENHA_API em infra/.env}"
: "${PG_LIVRO_SENHA_RELAY:?defina PG_LIVRO_SENHA_RELAY em infra/.env}"
: "${PG_LIVRO_SENHA_REPLAY:?defina PG_LIVRO_SENHA_REPLAY em infra/.env}"

# As senhas entram como variáveis do psql e são citadas por :'nome', sem passar
# pela linha de comando de outro processo nem pelo log.
psql -v ON_ERROR_STOP=1 --no-psqlrc \
  --username "${POSTGRES_USER:-postgres}" --dbname livro \
  -v senha_migracao="$PG_LIVRO_SENHA_MIGRACAO" \
  -v senha_api="$PG_LIVRO_SENHA_API" \
  -v senha_relay="$PG_LIVRO_SENHA_RELAY" \
  -v senha_replay="$PG_LIVRO_SENHA_REPLAY" <<'SQL'
\set QUIET on
BEGIN;

SELECT format('CREATE ROLE %I', papel)
FROM unnest(ARRAY['livro_migracao', 'livro_api', 'livro_relay', 'livro_replay']) AS papel
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = papel)
\gexec

ALTER ROLE livro_migracao WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'senha_migracao';
ALTER ROLE livro_api      WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'senha_api';
ALTER ROLE livro_relay    WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'senha_relay';
ALTER ROLE livro_replay   WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'senha_replay';

-- PUBLIC recebe CONNECT e TEMP em todo banco por padrão; aqui cada papel
-- recebe só o que é seu.
REVOKE ALL ON DATABASE livro, postgres, template1 FROM PUBLIC;
GRANT CONNECT ON DATABASE livro TO livro_migracao, livro_api, livro_relay, livro_replay;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO livro_migracao;
GRANT USAGE ON SCHEMA public TO livro_api, livro_relay, livro_replay;

COMMIT;
SQL
