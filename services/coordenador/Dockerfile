# Imagem do coordenador da projeção (005-T3; design 005 §3, §4): CLI de
# reconstrução e gerações, executada sob demanda pelo serviço `coordenador`
# do compose (perfil `operacao`), no padrão de livro-migracao.
#
#   docker compose -f infra/compose.yaml run --rm coordenador reconstruir status
#
# Contexto de build: raiz do repositório. A lista de arquivos que entram no
# contexto está em Dockerfile.dockerignore, ao lado deste arquivo.
#
# Versões (001 TD-007): as mesmas de services/consolidado/Dockerfile,
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

# Dependências pelo lock: só o grupo do coordenador (psycopg). Os pyproject
# dos membros entram só para o uv resolver o workspace; nenhum pacote interno
# é instalado.
COPY pyproject.toml uv.lock ./
COPY packages/observabilidade/pyproject.toml packages/observabilidade/
COPY packages/seguranca/pyproject.toml packages/seguranca/
RUN uv sync --frozen --only-group coordenador \
    && rm -rf packages

# Só o código de execução do coordenador, sem testes.
COPY services/coordenador/__init__.py services/coordenador/__main__.py services/coordenador/cli.py services/coordenador/
COPY services/coordenador/dominio services/coordenador/dominio
COPY services/coordenador/aplicacao services/coordenador/aplicacao
COPY services/coordenador/adaptadores services/coordenador/adaptadores

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/services \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Sem root: o coordenador só conecta ao banco do consolidado.
USER nobody

# Sem porta: só CLI (design 005 §4; INFRA 6.4). Sem argumentos, mostra o
# status, que não altera nada.
ENTRYPOINT ["python", "-m", "coordenador"]
CMD ["reconstruir", "status"]
