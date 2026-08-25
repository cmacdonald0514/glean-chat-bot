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
# venv built above, and only the project itself still has to be installed.
COPY glean_chat_bot ./glean_chat_bot
RUN poetry install --only-root


FROM python:3.13-slim AS base

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_HOST=0.0.0.0 \
    INDEXER_HOST=0.0.0.0

WORKDIR /app
COPY --from=builder /app/.venv ./.venv
COPY glean_chat_bot ./glean_chat_bot

RUN useradd --create-home --uid 10001 glean
USER glean


# The scheduler. Ordered before `runtime` on purpose: docker builds the last
# stage by default, and the server is what a bare `docker build .` should
# produce -- getting a scheduler there instead would be a trap no comment can
# reliably head off.
#
# supercronic rather than the system cron: it runs in the foreground as PID 1,
# logs each job to stdout, and needs no root, which matters because this
# container stays on the unprivileged user.
FROM base AS cron

# Set by BuildKit, so one Dockerfile serves amd64 and arm64. It also means the
# download URL varies by host, which is why there is no ADD --checksum here: a
# single digest cannot cover both architectures, and carrying a table of them
# buys little for a pinned release tag off a GitHub release page.
ARG TARGETARCH
ARG SUPERCRONIC_VERSION=v0.2.33

USER root
# --chmod on the ADD itself: a separate `RUN chmod` would rewrite the whole
# 13 MB binary into a second layer.
ADD --chmod=0755 \
    https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH} \
    /usr/local/bin/supercronic
COPY --chmod=0755 docker/cron-entrypoint.sh /usr/local/bin/cron-entrypoint.sh
USER glean

CMD ["cron-entrypoint.sh"]


# Last, and therefore the default build target.
FROM base AS runtime

# 8000 is the MCP server; the indexer runs glean-indexd on 8001 from this same
# stage. Metadata only either way.
EXPOSE 8000 8001
CMD ["glean-mcp"]

