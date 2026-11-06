# Imagem de migração do banco do consolidado (004-T1; design 004 §13).
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

# Só o necessário para resolver o workspace pelo lock e instalar o grupo de
# migrações, sem os membros nem as dependências de desenvolvimento. Os
# pyproject dos membros entram só para o uv resolver o workspace.
COPY pyproject.toml uv.lock ./
COPY packages/observabilidade/pyproject.toml packages/observabilidade/
COPY packages/seguranca/pyproject.toml packages/seguranca/
RUN uv sync --frozen --only-group migracoes

COPY services/consolidado/migracoes services/consolidado/migracoes

WORKDIR /app/services/consolidado/migracoes

# Sem root: a migração só precisa ler os arquivos e conectar ao banco.
USER nobody

# A rede do consolidado é interna e sem saída (INFRA 6.1): nada é
# sincronizado em tempo de execução.
CMD ["uv", "run", "--frozen", "--no-sync", "alembic", "upgrade", "head"]
