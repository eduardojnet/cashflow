#!/usr/bin/env bash
# Gera as definições do RabbitMQ a partir do modelo versionado, injetando as
# senhas de infra/.env (001 TD-005 aplicado ao broker; INFRA 5.9). O arquivo
# gerado fica em /run, fora do volume e fora do Git, e é recriado a cada subida.
set -euo pipefail

modelo=/etc/rabbitmq/cashflow/definicoes.modelo.json
destino_dir=/run/cashflow
destino="$destino_dir/definicoes.json"

# As senhas entram em JSON por substituição textual: aceito só caracteres que
# não exigem escape (o que `openssl rand -hex` produz).
for nome in RABBITMQ_SENHA_RELAY RABBITMQ_SENHA_WORKER; do
	valor="${!nome:-}"
	if [[ ! "$valor" =~ ^[A-Za-z0-9._~-]{16,}$ ]]; then
		echo >&2 "erro: $nome ausente ou inválida (mínimo 16 caracteres em [A-Za-z0-9._~-])"
		exit 1
	fi
done

mkdir -p "$destino_dir"
sed \
	-e "s/__RABBITMQ_SENHA_RELAY__/$RABBITMQ_SENHA_RELAY/" \
	-e "s/__RABBITMQ_SENHA_WORKER__/$RABBITMQ_SENHA_WORKER/" \
	"$modelo" > "$destino"
chown -R rabbitmq:rabbitmq "$destino_dir"
chmod 0700 "$destino_dir"
chmod 0600 "$destino"

exec docker-entrypoint.sh "$@"
