# Halcyon Docs Chatbot

Grounded question answering over a local document corpus, built on Glean's
Indexing, Search and Chat APIs and exposed as a single MCP tool.

There is no web UI. The chat interface is an MCP client — Cursor, Claude
Desktop, or any other.

## How it works

```
data/Halcyon Shared Drive/  ->  Indexing API  ->  Search API  ->  Chat API  ->  {answer, sources, diagnostics}
```

[ARCHITECTURE.md](ARCHITECTURE.md) has a flow diagram for each path — one for what
happens when a question arrives, one for extraction and indexing.

Search runs before Chat, and Chat never retrieves: passages are retrieved
explicitly and passed in. If nothing clears the relevance floor (term overlap
against the question), the answer is an honest "no indexed content found" and
Chat is never called.

Retrieval is filtered to documents whose status is `Active`, so a superseded
policy never reaches Chat's context. Archived documents stay indexed and stay
findable in Glean itself — they are excluded from this bot, not from the index.

## Setup

Requires [Poetry](https://python-poetry.org/docs/#installation) and Python 3.12+.

```bash
poetry config virtualenvs.in-project true   # keeps the venv at ./.venv
poetry install
cp .env.example .env      # then fill in the tokens
```

`.env` is gitignored. Never commit real tokens.

| Variable | Used by | Notes |
|---|---|---|
| `GLEAN_INSTANCE` | both | SDK builds `https://{instance}-be.glean.com` |
| `GLEAN_INDEXING_TOKEN` | indexing only | never loaded on the query path |
| `GLEAN_CLIENT_TOKEN` | search + chat | scope Chat/Search, type Global |
| `GLEAN_DATASOURCE` | both | shared sandbox, so this namespaces doc IDs |
| `GLEAN_DOCS_ROOT` | indexing only | corpus root (`data/Halcyon Shared Drive`); also the host side of the indexer's bind mount |
| `GLEAN_ACT_AS` | search + chat | email to act as; required for Global tokens |

Optional, with defaults: `GLEAN_DOC_ID_PREFIX` (`halcyon`), `GLEAN_TOP_K` (`5`),
`GLEAN_MAX_SNIPPET_SIZE` (`2000`), `GLEAN_MIN_TERM_OVERLAP` (`0.30`),
`GLEAN_CHAT_TIMEOUT_MS` (`60000`), `INDEXER_PORT` (`8001`), `INDEX_SCHEDULE`
(`0 3 * * *`).

The two tokens are separated structurally: `Settings.for_indexing()` reads
`GLEAN_INDEXING_TOKEN`, `Settings.for_query()` reads `GLEAN_CLIENT_TOKEN` and
never touches the indexing variable.

## Usage

```bash
docker compose up --build               # mcp + indexer + indexer-cron
poetry run glean-mcp                    # the MCP server alone, without the container
poetry run glean-index --dry-run        # extract and report, send nothing
poetry run glean-index                  # extract and bulk-push
poetry run glean-index --process-now    # ask Glean to process immediately (1 per 3h)
poetry run pytest                       # contract tests: no network, no tokens
poetry run pytest -m live               # the eval set, against real Glean

docker compose exec indexer glean-index-trigger        # index now, on the running stack
docker compose run --rm indexer glean-index --dry-run  # dry run against the container's mount
```

Add `-v` for debug logging.

Indexing is asynchronous: `glean-index` returns once Glean has accepted the
documents, minutes before they become searchable. Confirm full coverage in the
Glean admin console before relying on the answers.

### Running in Docker

`docker compose up --build` is the intended way to run the server. It builds the
image, reads the same `.env` you filled in during setup, and publishes the port
on `127.0.0.1` only — the transport has no authentication in front of it, so it
is not meant to be reachable from the network.

Compose runs three services. `mcp` serves the MCP tool and holds only
`GLEAN_CLIENT_TOKEN`. `indexer` runs `glean-indexd` and holds only
`GLEAN_INDEXING_TOKEN`. `indexer-cron` holds neither — it is a scheduler whose
entire job is one HTTP call. Each block lists its variables one by one instead
of using `env_file: .env`, because `.env` holds both tokens and no container
should receive the one it has no business with:

```console
$ docker compose exec mcp printenv GLEAN_INDEXING_TOKEN     # prints nothing
$ docker compose exec indexer printenv GLEAN_CLIENT_TOKEN   # prints nothing
```

The corpus is bind-mounted at `/app/data` rather than baked into the image
(`data/` is in `.dockerignore`), so editing a document on the host and
re-triggering picks it up with no rebuild.

### Scheduled and on-demand indexing

Indexing runs in its own container, never in the MCP server: the read path must
be able to run with no indexing token in its environment at all, and a server
that could rewrite the corpus it answers from would give that up.

`indexer` serves `POST /index`, which does exactly what `glean-index` does and
answers with the outcome:

```console
$ docker compose exec indexer glean-index-trigger
{"ok":true,"documents":16,"skipped":[],"upload_id":"interviewds3-23f03bc7ec75",
 "processing":"not requested","datasource_count":16,"duration_ms":1443}
```

That endpoint is **not published to the host**. It is unauthenticated and it
rewrites the whole datasource, so the only ways to reach it are the cron sidecar
on the compose network and `docker compose exec`. Do not add a `ports:` entry to
the `indexer` service.

The service refuses a second request while a run is in flight, rather than
queueing it — a bulk upload replaces the datasource contents as a unit, so
overlapping runs race over what ends up in it. The guard is the endpoint's: a
`glean-index` push from the host or from `docker compose run` bypasses it, and
there `force_restart_upload` decides the winner instead.

```console
$ docker compose exec indexer glean-index-trigger
HTTP 409: {"ok":false,"error":"an indexing run is already in progress"}
$ echo $?
1
```

The non-zero exit is the point: `indexer-cron` runs supercronic, which logs each
job's status, so a failed or refused run shows up in `docker compose logs
indexer-cron` instead of a datasource that has quietly stopped updating.

`INDEX_SCHEDULE` sets when it fires, in ordinary five-field cron syntax,
defaulting to `0 3 * * *`. It is wall-clock, not an interval — a container
restart does not shift it. Changing it needs no rebuild:

```bash
INDEX_SCHEDULE='*/30 * * * *' docker compose up -d indexer-cron
```

Indexing stays asynchronous either way: a run returns once Glean has accepted
the documents, minutes before they are searchable.

`curl http://127.0.0.1:8000/healthz` answers `ok` once the server is up; that is
what the compose healthcheck polls. It reports process liveness only and makes no
Glean call — an unreachable Glean is a tool-call failure that comes back in the
answer envelope, not a reason to restart a healthy process.

`MCP_PORT` and `MCP_ALLOWED_HOSTS` override the port and the `Host`/`Origin`
allowlist under compose as well as on the host — compose forwards both, and the
published port follows `MCP_PORT` so the healthcheck cannot end up polling a
dead one. `MCP_HOST` is host-only: the container always binds `0.0.0.0` and is
confined by the published port instead. `--host` and `--port` override the
environment again on the command line.

### MCP client configuration

```json
{
  "mcpServers": {
    "glean-company-docs": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

The credentials live with the server, not the client: the container holds the
Glean token and the client only needs the URL.

One tool, `ask_company_docs(question, top_k=None, include_citations=True) -> dict`,
returning `{answer, sources, diagnostics}`. `diagnostics` reports what was
searched and what came back, so the calling model can tell "nothing matched"
from "my phrasing missed" and retry accordingly.

## Layout

```
glean_chat_bot/
  __main__.py      `glean-mcp`, the MCP server over streamable HTTP: one tool over query.ask.ask()
  client.py        indexing and query client factories, the ActAs header
  extraction.py    one adapter per file type, path signals, walk
  indexing.py      the whole write path, behind the `glean-index` command
  indexd.py        `glean-indexd`, the same write path over HTTP, plus the
                   `glean-index-trigger` CLI the cron sidecar runs
  models.py        Passage, Source, Answer, ExtractedDoc (pydantic)
  query/           search.py (search -> Passage, the relevance floor)
                   chat.py (chat -> answer + resolved citations)
                   ask.py (ask() — the single orchestration function)
  utils/           config.py (env loading, one Settings, two constructors)
                   logging.py (log format, timing wrapper on every Glean call)
Dockerfile         `runtime` (both servers) and `cron` (adds supercronic) stages
docker/            cron-entrypoint.sh, which renders the crontab from the env
data/              the corpus, plus EXTRACTION_NOTES.md
tests/             test_contract.py (the invariants, offline)
                   eval_cases.py + test_eval_live.py (the eval set, `-m live`)
```

Poetry for dependencies and packaging, Ruff for lint and format. Run
`poetry run ruff check .` and `poetry run ruff format .` before committing.

## Not built yet

Group and user permissions, department filtering, freshness annotations, a
content-hash manifest, query rewriting and adaptive retry, streaming,
conversation memory, retry and backoff, authentication on the HTTP transport, CI.
