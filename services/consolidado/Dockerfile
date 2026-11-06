# Imagem do consolidado (004-T3, T5; design 004 §4). A mesma imagem serve o
# worker (`python -m consolidado.worker`, comando padrão) e a API de consulta
# (`python -m consolidado.adaptadores.http`, comando do serviço
# consolidado-api no compose). As migrações têm imagem própria
# (services/consolidado/migracoes/Dockerfile).
#
# Contexto de build: raiz do repositório. A lista de arquivos que entram no
# contexto está em Dockerfile.dockerignore, ao lado deste arquivo.
#
# Versões (001 TD-007): as mesmas de services/relay/Dockerfile, conferidas em
# 2026-11-06:
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

# Dependências pelo lock: só o grupo do consolidado, com os pacotes internos
# (observabilidade, seguranca) instalados como pacotes comuns, sem editável,
# para que o código-fonte deles não precise ficar na imagem final.
COPY pyproject.toml uv.lock ./
COPY packages/observabilidade packages/observabilidade
COPY packages/seguranca packages/seguranca
RUN uv sync --frozen --only-group consolidado --no-editable \
    && rm -rf packages

# Só o código de execução do consolidado, sem testes nem migrações.
COPY services/consolidado/__init__.py services/consolidado/
COPY services/consolidado/dominio services/consolidado/dominio
COPY services/consolidado/aplicacao services/consolidado/aplicacao
COPY services/consolidado/adaptadores services/consolidado/adaptadores
COPY services/consolidado/worker services/consolidado/worker

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/services \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Sem root: o worker só conecta ao banco e ao broker. A API lê a chave TLS
# montada do host, de modo 0640 e dono UID 1000, e roda como 1000:1000 pelo
# `user` do serviço consolidado-api no compose, como lancamentos-api.
USER nobody

# Worker: health em 8080. API: HTTPS em 8443. Ambos: /metrics em 9464, porta
# interna (design 004 §12; 007 design §4).
EXPOSE 8080 8443 9464

CMD ["python", "-m", "consolidado.worker"]
