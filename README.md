# Halcyon Docs Chatbot

Grounded question answering over a local document corpus, built on Glean's
Indexing, Search and Chat APIs and exposed as a single MCP tool.

There is no web UI. The chat interface is an MCP client — Cursor, Claude
Desktop, or any other.

## How it works

```
data/Halcyon Shared Drive/  ->  Indexing API  ->  Search API  ->  Chat API  ->  {answer, sources, diagnostics}
```

Search runs before Chat, and Chat never retrieves: passages are retrieved
explicitly and passed in. If nothing clears the relevance floor (term overlap
against the question), the answer is an honest "no indexed content found" and
Chat is never called.

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
| `GLEAN_DOCS_ROOT` | indexing only | corpus root (`data/Halcyon Shared Drive`) |
| `GLEAN_ACT_AS` | search + chat | email to act as; required for Global tokens |

Optional, with defaults: `GLEAN_DOC_ID_PREFIX` (`halcyon`), `GLEAN_TOP_K` (`5`),
`GLEAN_MAX_SNIPPET_SIZE` (`2000`), `GLEAN_MIN_TERM_OVERLAP` (`0.30`),
`GLEAN_CHAT_TIMEOUT_MS` (`60000`).

The two tokens are separated structurally: `Settings.for_indexing()` reads
`GLEAN_INDEXING_TOKEN`, `Settings.for_query()` reads `GLEAN_CLIENT_TOKEN` and
never touches the indexing variable.

## Usage

```bash
poetry run python -m glean_chat_bot     # the MCP server, on stdio
poetry run glean-index --dry-run        # extract and report, send nothing
poetry run glean-index                  # extract and bulk-push
poetry run glean-index --process-now    # ask Glean to process immediately (1 per 3h)
poetry run pytest                       # contract tests: no network, no tokens
poetry run pytest -m live               # the eval set, against real Glean
```

Add `-v` for debug logging.

Indexing is asynchronous: `glean-index` returns once Glean has accepted the
documents, minutes before they become searchable. Confirm full coverage in the
Glean admin console before relying on the answers.

### MCP client configuration

```json
{
  "mcpServers": {
    "glean-company-docs": {
      "command": "/absolute/path/to/glean-chat-bot/.venv/bin/python",
      "args": ["-m", "glean_chat_bot"],
      "env": {
        "GLEAN_INSTANCE": "support-lab",
        "GLEAN_CLIENT_TOKEN": "...",
        "GLEAN_ACT_AS": "you@example.com",
        "GLEAN_DATASOURCE": "interviewds3"
      }
    }
  }
}
```

One tool, `ask_company_docs(question, top_k=None, include_citations=True) -> dict`,
returning `{answer, sources, diagnostics}`. `diagnostics` reports what was
searched and what came back, so the calling model can tell "nothing matched"
from "my phrasing missed" and retry accordingly.

## Layout

```
glean_chat_bot/
  __main__.py      `python -m glean_chat_bot`, the MCP server: one tool over query.ask.ask()
  client.py        indexing and query client factories, the ActAs header
  extraction.py    one adapter per file type, path signals, walk
  indexing.py      the whole write path, behind the `glean-index` command
  models.py        Passage, Source, Answer, ExtractedDoc (pydantic)
  query/           search.py (search -> Passage, the relevance floor)
                   chat.py (chat -> answer + resolved citations)
                   ask.py (ask() — the single orchestration function)
  utils/           config.py (env loading, one Settings, two constructors)
                   logging.py (log format, timing wrapper on every Glean call)
data/              the corpus
docs/              extraction notes
tests/             test_contract.py (the invariants, offline)
                   eval_cases.py + test_eval_live.py (the eval set, `-m live`)
```

Poetry for dependencies and packaging, Ruff for lint and format. Run
`poetry run ruff check .` and `poetry run ruff format .` before committing.

## Not built yet

Group and user permissions, department filtering, freshness annotations, a
content-hash manifest, query rewriting and adaptive retry, streaming,
conversation memory, retry and backoff, Docker, CI.
