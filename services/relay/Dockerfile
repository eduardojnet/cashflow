# Imagem do relay da outbox (003-T5; design 003 §12, Execução). A mesma
# imagem serve o laço (`relay`) e o comando de republicação
# (`relay-republicar`, perfil `operacao`).
#
# Contexto de build: raiz do repositório. A lista de arquivos que entram no
# contexto está em Dockerfile.dockerignore, ao lado deste arquivo.
#
# Versões (001 TD-007): as mesmas de services/lancamentos/Dockerfile,
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

# Dependências pelo lock: só o grupo do relay, com o pacote interno de
# métricas instalado como pacote comum, sem editável. O pyproject de
# seguranca entra só para o uv resolver o workspace; o pacote não é instalado.
COPY pyproject.toml uv.lock ./
COPY packages/observabilidade packages/observabilidade
COPY packages/seguranca/pyproject.toml packages/seguranca/
RUN uv sync --frozen --only-group relay --no-editable \
    && rm -rf packages

# Só o código de execução do relay, sem testes.
COPY services/relay/__init__.py services/relay/__main__.py services/relay/principal.py services/relay/
COPY services/relay/aplicacao services/relay/aplicacao
COPY services/relay/adaptadores services/relay/adaptadores
COPY services/relay/comandos services/relay/comandos

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/services \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Sem root: o relay só conecta ao banco e ao broker.
USER nobody

# Health e /metrics, ambos em porta interna (design 003 §12; 007 design §4).
EXPOSE 8080 9464

CMD ["python", "-m", "relay"]
