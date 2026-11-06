#!/usr/bin/env bash
# Gera a AC local de desenvolvimento e os certificados de servidor dos
# componentes publicados com TLS (001 FR-006, TD-004; INFRA 5.7, 5.8).
#
# Uso: infra/tls/gerar-certificados.sh [diretorio-de-saida]
# Padrão de saída: infra/tls/gerados/, ignorado pelo Git (INFRA 5.9).
#
# A AC é reaproveitada se já existir, para não invalidar a confiança já
# instalada no cliente; os certificados de servidor são reemitidos a cada
# execução. Para recriar tudo, apague o diretório de saída.
set -euo pipefail

raiz="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
saida="${1:-$raiz/gerados}"

# Nome do serviço no compose = nome DNS na rede interna (001 design §3).
servicos=(lancamentos-api consolidado-api keycloak grafana)

dias_ac=1825
# Limite aceito por navegadores e clientes para certificado de servidor.
dias_servidor=397

if ! command -v openssl >/dev/null 2>&1; then
	echo >&2 "erro: openssl não encontrado no PATH"
	exit 1
fi

umask 077
mkdir -p "$saida/ac"
chmod 0700 "$saida/ac"

ac_crt="$saida/ac/ac-cashflow.crt"
ac_key="$saida/ac/ac-cashflow.key"

if [[ -f "$ac_crt" && -f "$ac_key" ]]; then
	echo "AC local existente reaproveitada: $ac_crt"
else
	openssl genpkey -quiet -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$ac_key"
	# pathlen:0: a AC só emite certificados finais, nunca outra AC.
	openssl req -x509 -new -key "$ac_key" -sha256 -days "$dias_ac" \
		-subj "/O=CashFlow/CN=CashFlow AC local de desenvolvimento" \
		-addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
		-addext "keyUsage=critical,keyCertSign,cRLSign" \
		-addext "subjectKeyIdentifier=hash" \
		-out "$ac_crt"
	echo "AC local criada: $ac_crt"
fi
chmod 0600 "$ac_key"
chmod 0644 "$ac_crt"

extensoes="$(mktemp)"
trap 'rm -f "$extensoes" "$saida"/*.csr' EXIT

for servico in "${servicos[@]}"; do
	key="$saida/$servico.key"
	csr="$saida/$servico.csr"
	crt="$saida/$servico.crt"

	openssl genpkey -quiet -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$key"
	openssl req -new -key "$key" -subj "/O=CashFlow/CN=$servico" -out "$csr"

	# Nome interno para chamadas entre contêineres (JWKS) e loopback para o
	# cliente no host (INFRA 6.5, 6.6).
	cat > "$extensoes" <<-EOF
		basicConstraints=critical,CA:FALSE
		keyUsage=critical,digitalSignature
		extendedKeyUsage=serverAuth
		subjectAltName=DNS:$servico,DNS:localhost,IP:127.0.0.1
		subjectKeyIdentifier=hash
		authorityKeyIdentifier=keyid
	EOF

	openssl x509 -req -in "$csr" -CA "$ac_crt" -CAkey "$ac_key" -CAcreateserial \
		-CAserial "$saida/ac/ac-cashflow.srl" -sha256 -days "$dias_servidor" \
		-extfile "$extensoes" -out "$crt"

	# Chave legível pelo dono e pelo grupo; o ajuste para o usuário de cada
	# contêiner fica na task que monta o certificado.
	chmod 0640 "$key"
	chmod 0644 "$crt"

	# Falha a geração se a cadeia não fechar na AC local.
	openssl verify -CAfile "$ac_crt" -purpose sslserver "$crt" >/dev/null
	echo "Certificado emitido e validado: $crt"
done
