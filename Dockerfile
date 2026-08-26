FROM python:3.13-slim AS builder

ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN pip install --no-cache-dir "poetry>=2.0"

COPY pyproject.toml poetry.lock ./
RUN poetry install --only main --no-root

# Then the package itself, which is what registers the console scripts.
# --only-root skips re-resolving the lock: the dependencies are already in the
# venv built above.
COPY glean_chat_bot ./glean_chat_bot
RUN poetry install --only-root


FROM python:3.13-slim AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_HOST=0.0.0.0

WORKDIR /app
COPY --from=builder /app/.venv ./.venv
COPY glean_chat_bot ./glean_chat_bot

RUN useradd --create-home --uid 10001 glean
USER glean

EXPOSE 8000
CMD ["glean-mcp"]
