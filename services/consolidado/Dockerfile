# Imagem do consolidado (004-T3; design 004 §4). A mesma imagem serve o
# worker (`python -m consolidado.worker`) e, a partir da 004-T5, a API de
# consulta, que a reaproveita com outro comando. As migrações têm imagem
# própria (services/consolidado/migracoes/Dockerfile).
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

# Dependências pelo lock: só o grupo do consolidado, com o pacote interno de
# métricas instalado como pacote comum, sem editável. O pyproject de
# seguranca entra só para o uv resolver o workspace; o pacote não é
# instalado nesta task.
COPY pyproject.toml uv.lock ./
COPY packages/observabilidade packages/observabilidade
COPY packages/seguranca/pyproject.toml packages/seguranca/
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

# Sem root: o worker só conecta ao banco e ao broker.
USER nobody

# Health e /metrics, ambos em porta interna (design 004 §12; 007 design §4).
EXPOSE 8080 9464

CMD ["python", "-m", "consolidado.worker"]
