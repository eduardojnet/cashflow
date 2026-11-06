#!/usr/bin/env bash
# Verifica a readiness e a persistência da infraestrutura local
# (001-T7; FR-009, FR-010; INFRA 7.1, 8.4, 8.5).
#
# Uso: infra/verificar-ambiente.sh [prontidao|persistencia]
#
#   prontidao     (padrão) aguarda o health check de pg-livro, pg-consolidado,
#                 rabbitmq e keycloak ficar `healthy` e confere, do host, o
#                 JWKS do Keycloak por TLS validado contra a AC local.
#   persistencia  aguarda a readiness, grava um marcador em cada componente,
#                 recria os contêineres (`down` sem `-v` e `up -d`), aguarda a
#                 readiness de novo e confere os marcadores. Só os volumes
#                 atravessam a recriação. Os marcadores são removidos ao final.
#
# Código de saída: 0 com tudo pronto e preservado; 1 em falha de readiness ou
# de persistência; 2 em uso incorreto; outro valor diferente de 0 se um
# comando falhar de forma inesperada. VERIFICAR_TIMEOUT define a espera máxima por readiness, em
# segundos (padrão 300).
#
# Marcadores:
#   - PostgreSQL: linha em verificacao_ambiente.marcador, esquema do
#     superusuário sem privilégio para os papéis da aplicação;
#   - RabbitMQ: mensagem persistente em fila durável de um vhost e de um
#     usuário temporários, criados só para a verificação;
#   - Keycloak: conjunto de `kid` do JWKS do realm cashflow. As chaves de
#     assinatura são geradas na importação e gravadas no volume; com o volume
#     perdido, a reimportação gera chaves novas e os tokens emitidos antes
#     deixam de validar.
set -euo pipefail

raiz="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
compose=(docker compose -f "$raiz/compose.yaml")
ac="$raiz/tls/gerados/ac/ac-cashflow.crt"
timeout_prontidao="${VERIFICAR_TIMEOUT:-300}"

servicos=(pg-livro pg-consolidado rabbitmq keycloak)
bancos=(pg-livro:livro pg-consolidado:consolidado)

rmq_vhost=verificacao-ambiente
rmq_usuario=verificacao-ambiente
rmq_fila=marcador

falhas=0

info() { echo "[verificar-ambiente] $*"; }
falha() {
	echo >&2 "[verificar-ambiente] FALHA: $*"
	falhas=$((falhas + 1))
}

encerrar() {
	if ((falhas > 0)); then
		info "resultado: $falhas falha(s)"
		exit 1
	fi
	info "resultado: tudo pronto"
	exit 0
}

# Comando inesperado que falha encerra o script com código diferente de 0
# (set -e); a mensagem indica onde.
trap 'echo >&2 "[verificar-ambiente] FALHA: comando inesperado falhou (linha $LINENO)"' ERR

uso() {
	echo >&2 "uso: $0 [prontidao|persistencia]"
	exit 2
}

for comando in docker curl jq; do
	if ! command -v "$comando" >/dev/null 2>&1; then
		echo >&2 "erro: $comando não encontrado no PATH"
		exit 1
	fi
done

# Aguarda o health check nativo de cada imagem (design 001 §12): pronto é
# `healthy`, não só processo em execução (INFRA 8.5).
aguardar_prontidao() {
	local limite=$((SECONDS + timeout_prontidao))
	local pendentes=("${servicos[@]}")

	while ((${#pendentes[@]} > 0)); do
		local restantes=()
		local servico id estado saude
		for servico in "${pendentes[@]}"; do
			id="$("${compose[@]}" ps -q "$servico" 2>/dev/null || true)"
			if [[ -z "$id" ]]; then
				falha "$servico não está em execução (suba com: docker compose -f infra/compose.yaml up -d)"
				continue
			fi
			read -r estado saude < <(docker inspect -f \
				'{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}sem-healthcheck{{end}}' \
				"$id" 2>/dev/null || echo "removido -")
			case "$estado:$saude" in
				running:healthy) info "$servico pronto" ;;
				running:sem-healthcheck) falha "$servico sem health check declarado" ;;
				running:*) restantes+=("$servico") ;;
				*) falha "$servico em estado $estado" ;;
			esac
		done
		pendentes=("${restantes[@]}")
		if ((${#pendentes[@]} > 0)); then
			if ((SECONDS >= limite)); then
				falha "sem readiness após ${timeout_prontidao}s: ${pendentes[*]}"
				break
			fi
			sleep 3
		fi
	done
	((falhas == 0)) || encerrar
}

# Conjunto ordenado de `kid` do JWKS, obtido do host por TLS validado contra
# a AC local (FR-006).
kids_do_jwks() {
	local endereco
	endereco="$("${compose[@]}" port keycloak 8443)"
	curl --fail --silent --show-error --max-time 10 --cacert "$ac" \
		"https://localhost:${endereco##*:}/realms/cashflow/protocol/openid-connect/certs" |
		jq -r '[.keys[].kid] | sort | join(",")'
}

conferir_jwks() {
	local kids
	if ! kids="$(kids_do_jwks)" || [[ -z "$kids" ]]; then
		falha "JWKS do realm cashflow indisponível pelo host"
		encerrar
	fi
	info "keycloak publica o JWKS por TLS (kid: $kids)"
}

psql_super() {
	local servico="$1" banco="$2"
	shift 2
	"${compose[@]}" exec -T "$servico" psql -v ON_ERROR_STOP=1 --no-psqlrc -qtA \
		--username postgres --dbname "$banco" "$@"
}

# rabbitmqadmin pela API HTTP local do contêiner, com o usuário temporário.
# A senha entra por variável de ambiente, fora da linha de comando.
rmqadmin() {
	"${compose[@]}" exec -T \
		-e RABBITMQADMIN_USERNAME="$rmq_usuario" \
		-e RABBITMQADMIN_PASSWORD="$rmq_senha" \
		rabbitmq rabbitmqadmin --non-interactive --vhost "$rmq_vhost" "$@"
}

gravar_marcadores() {
	local par servico banco
	for par in "${bancos[@]}"; do
		servico="${par%%:*}"
		banco="${par#*:}"
		psql_super "$servico" "$banco" -v marcador="$marcador" <<-'SQL'
			CREATE SCHEMA IF NOT EXISTS verificacao_ambiente;
			CREATE TABLE IF NOT EXISTS verificacao_ambiente.marcador (
			    valor text PRIMARY KEY,
			    gravado_em timestamptz NOT NULL DEFAULT now()
			);
			INSERT INTO verificacao_ambiente.marcador (valor) VALUES (:'marcador');
		SQL
		info "$servico: marcador gravado"
	done

	"${compose[@]}" exec -T rabbitmq rabbitmqctl -q add_vhost "$rmq_vhost"
	# Sem senha no argumento: rabbitmqctl lê a senha da entrada padrão.
	printf '%s\n' "$rmq_senha" | "${compose[@]}" exec -T rabbitmq rabbitmqctl -q add_user "$rmq_usuario"
	"${compose[@]}" exec -T rabbitmq rabbitmqctl -q set_user_tags "$rmq_usuario" management
	"${compose[@]}" exec -T rabbitmq rabbitmqctl -q set_permissions -p "$rmq_vhost" "$rmq_usuario" '.*' '.*' '.*'
	rmqadmin declare queue --name "$rmq_fila" --type classic --durable true >/dev/null
	rmqadmin publish message --routing-key "$rmq_fila" --payload "$marcador" \
		--properties '{"delivery_mode": 2}' >/dev/null
	info "rabbitmq: mensagem persistente publicada"

	kids_antes="$(kids_do_jwks)"
	info "keycloak: kid anotados ($kids_antes)"
}

conferir_marcadores() {
	local par servico banco achados mensagens kids_depois
	for par in "${bancos[@]}"; do
		servico="${par%%:*}"
		banco="${par#*:}"
		achados="$(psql_super "$servico" "$banco" -v marcador="$marcador" <<-'SQL' || true
			SELECT count(*) FROM verificacao_ambiente.marcador WHERE valor = :'marcador';
		SQL
		)"
		if [[ "$achados" == "1" ]]; then
			info "$servico: marcador preservado"
		else
			falha "$servico: marcador perdido após reinício"
		fi
	done

	# ack_requeue_true devolve a mensagem à fila: a leitura não a consome.
	mensagens="$(rmqadmin get messages --queue "$rmq_fila" --count 1 --ack-mode ack_requeue_true 2>&1 || true)"
	if grep -qF "$marcador" <<<"$mensagens"; then
		info "rabbitmq: mensagem persistente preservada"
	else
		falha "rabbitmq: mensagem perdida após reinício"
	fi

	if ! kids_depois="$(kids_do_jwks)"; then
		falha "keycloak: JWKS indisponível após reinício"
	elif [[ "$kids_depois" == "$kids_antes" ]]; then
		info "keycloak: chaves de assinatura preservadas"
	else
		falha "keycloak: chaves de assinatura mudaram após reinício ($kids_antes -> $kids_depois)"
	fi
}

# Remove os marcadores mesmo após falha; o que já não existe é ignorado.
remover_marcadores() {
	local par servico banco
	for par in "${bancos[@]}"; do
		servico="${par%%:*}"
		banco="${par#*:}"
		psql_super "$servico" "$banco" -c 'DROP SCHEMA IF EXISTS verificacao_ambiente CASCADE' \
			>/dev/null 2>&1 || true
	done
	"${compose[@]}" exec -T rabbitmq rabbitmqctl -q delete_vhost "$rmq_vhost" >/dev/null 2>&1 || true
	"${compose[@]}" exec -T rabbitmq rabbitmqctl -q delete_user "$rmq_usuario" >/dev/null 2>&1 || true
	info "marcadores removidos"
}

verificar_prontidao() {
	aguardar_prontidao
	conferir_jwks
}

verificar_persistencia() {
	verificar_prontidao

	marcador="verificacao-$(cat /proc/sys/kernel/random/uuid)"
	rmq_senha="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
	trap remover_marcadores EXIT
	gravar_marcadores

	info "recriando os contêineres; os volumes são mantidos"
	"${compose[@]}" down
	"${compose[@]}" up -d

	verificar_prontidao
	conferir_marcadores
	remover_marcadores
	trap - EXIT
}

case "${1:-prontidao}" in
	prontidao) verificar_prontidao ;;
	persistencia) verificar_persistencia ;;
	*) uso ;;
esac
encerrar
