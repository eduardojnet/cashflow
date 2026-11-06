# Imagem da API de lançamentos (002-T5; design 002 §3, §4).
#
# Contexto de build: raiz do repositório. A lista de arquivos que entram no
# contexto está em Dockerfile.dockerignore, ao lado deste arquivo.
#
# Versões (001 TD-007): as mesmas de services/lancamentos/migracoes/Dockerfile,
# conferidas em 2026-11-06:
# - uv 0.13.7, release "Latest" de 2026-11-03 em
#   https://github.com/astral-sh/uv/releases; imagem em ghcr.io/astral-sh/uv.
# - Python 3.12.16, último 3.12 estável (2026-10-25) em
#   https://www.python.org/downloads/source/; tag em
#   https://hub.docker.com/_/python.
FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

FROM python:3.12.16-slim-trixie

COPY --from=uv /uv /usr/local/bin/uv

# Ambiente fora de /app, sem baixar Python e sem cache em tempo de execução.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /app

# Dependências pelo lock: só o grupo da API, com os pacotes internos
# (seguranca, observabilidade) instalados como pacotes comuns, sem editável,
# para que o código-fonte deles não precise ficar na imagem final.
COPY pyproject.toml uv.lock ./
COPY packages/observabilidade packages/observabilidade
COPY packages/seguranca packages/seguranca
RUN uv sync --frozen --only-group lancamentos --no-editable \
    && rm -rf packages

# Só o código de execução do serviço: sem testes e sem migrações, que têm
# imagem própria (livro-migracao).
COPY services/lancamentos/__init__.py services/lancamentos/
COPY services/lancamentos/dominio services/lancamentos/dominio
COPY services/lancamentos/aplicacao services/lancamentos/aplicacao
COPY services/lancamentos/adaptadores services/lancamentos/adaptadores

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/services \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Sem root. UID e GID 1000: a chave TLS montada do host tem modo 0640 e
# pertence a quem a gerou (infra/tls/gerar-certificados.sh), como no
# Keycloak (infra/compose.yaml).
USER 1000:1000

EXPOSE 8443 9464

CMD ["python", "-m", "lancamentos.adaptadores.http"]
