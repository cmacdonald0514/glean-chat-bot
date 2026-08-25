# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
poetry install                          # deps; venv lives at ./.venv (virtualenvs.in-project true)
docker compose up --build               # mcp + indexer + indexer-cron; MCP on http://127.0.0.1:8000/mcp
poetry run glean-mcp                    # the MCP server alone, uncontainerised (-v for debug logging)
poetry run glean-index --dry-run        # walk the corpus, print the extraction table, send nothing
poetry run glean-index                  # extract and bulk-push
poetry run glean-index --process-now    # ask Glean to process immediately (rate limited, 1 per 3h)
poetry run glean-indexd                 # the same push, served over HTTP for cron (-v for debug)
docker compose exec indexer glean-index-trigger        # index now, through the running service
docker compose run --rm indexer glean-index --dry-run  # the dry run, against the container's mount
INDEX_SCHEDULE='*/5 * * * *' docker compose up -d indexer-cron   # re-schedule without a rebuild
poetry run ruff check . && poetry run ruff format .   # before committing
poetry run pytest                       # the contract tests: no network, no tokens
poetry run pytest -m live               # the eval set, against the real Glean instance
```

`-v` on `glean-index` or `glean-mcp` turns on debug logging. `--dry-run` is the only way to
exercise the write path without touching Glean.

`tests/test_contract.py` holds one test per invariant this design rests on, and
nothing else — token separation, the floor gating generation, unresolved
citations staying visible, the MCP envelope. It runs in ~1s with no credentials,
so keep it that way: quality belongs in the eval set, not here. (The ~1s is
almost entirely `test_the_server_starts_over_http_and_serves_its_tool`, which
spawns a real server; every other test is under 5ms.)

`tests/eval_cases.py` is the read path's regression set. Every expected answer
in it is invented, so a correct answer proves retrieval worked. A case carrying
`known_gap` is xfailed with the reason: those are real gaps in retrieval today,
not flaky tests, and `strict=False` means fixing one reports XPASS rather than
going red.

Config comes from `.env` (`cp .env.example .env`); `load_dotenv(override=False)`
means an exported shell variable beats the file.

## Architecture

Two paths that share config and models and touch nothing else:

```
write:  data/  →  extraction.py  →  indexing.py  →  Glean Indexing API
          ↑ CLI: glean-index      ↑ HTTP POST /index: glean-indexd, fired by cron
read:   HTTP /mcp  →  MCP tool  →  query/ask.py  →  query/search.py  →  query/chat.py
```

**The two paths are separated by construction, not convention.** `Settings.for_indexing()`
reads `GLEAN_INDEXING_TOKEN` + `GLEAN_DOCS_ROOT`; `Settings.for_query()` reads
`GLEAN_CLIENT_TOKEN` and never touches the indexing variable. `client.py` raises
if handed the wrong one. Keep it that way — the query-only MCP host must be able
to run with no indexing token in its environment at all. `docker-compose.yml` is
where that holds at the container boundary, and it holds *per service* rather
than per file: `mcp` gets the query variables, `indexer` gets the indexing ones,
and `indexer-cron` gets neither, because all it does is make an HTTP call. Each
block lists its variables one by one rather than using `env_file: .env`, since
the repo's `.env` holds both tokens — `env_file` anywhere in that file would
hand some container both. Nothing tests this any more (the whole-file text scan
that used to could not coexist with an indexer service and was removed), so it
is on you: adding `GLEAN_INDEXING_TOKEN` to the `mcp` block would now go
unnoticed. `docker compose exec mcp printenv GLEAN_INDEXING_TOKEN` printing
nothing is the check.

**Retrieval happens before generation, and Chat never retrieves.** `chat.py` sets
`AgentEnum.GPT` with both tool sets off, which disables Glean's own retrieval.
Passages are numbered by us in `search.py`, embedded in a CONTEXT message, and the
`[n]` markers in the answer are resolved back against those passages in
`resolve_citations`. A marker with no matching passage comes back `resolved=False`
rather than being dropped — that's how a hallucinated citation stays visible.
`diagnostics["glean_inline_citations"]` should stay empty; non-zero means Glean
started citing on its own and this design needs revisiting.

**Retrieval is scoped to active documents.** `search.py` sends a facet filter of
`halcyonstatus = Active` on every query, so a superseded policy is never in Chat's
context to be cited. Before this, the archived FIN-007 came back alongside the
active FIN-011 and only Chat's judgement kept the retired $50 per diem out of the
answer — grounding resting on generation. The property name is single-sourced as
`models.STATUS_PROPERTY` because a facet name Glean does not recognise returns
zero results rather than an error, so drift between the two paths would look like
an empty corpus; `test_contract.py` guards it.

**The relevance floor is term overlap, not a score.** `search.py` computes the
fraction of the question's content words present in the retrieved text;
`ask.py` gates on it. If it fails, Chat is never called and the answer is an
explicit "no indexed content found" with the reason. Glean's own relevance scores
aren't comparable across queries, which is why this is an overlap fraction.

**`diagnostics` is part of the tool contract, not debug output.** It accumulates
through `search()` → `ask()` → the MCP response so the calling model can tell
"nothing is indexed" (`results_returned=0`) from "my phrasing missed"
(`results_returned>0, floor_passed=false`) and retry accordingly. Errors in
`__main__.py` are caught and returned in the same `Answer` envelope for the same
reason — an opaque tool error carries no diagnostics.

**The transport is a launcher concern; the tool is not.** `main()` in `__main__.py`
is the only thing that knows about HTTP — the `MCPServer`, the tool and the
`Answer` envelope are transport-agnostic. One gotcha, and it fails silently: the SDK
auto-enables DNS-rebinding protection only when the bind address is loopback
(`mcp/server/lowlevel/server.py`), so on `0.0.0.0` a `transport_security=None`
turns the `Host` check *off* rather than rejecting anything. `_transport_security()`
states the allowlist explicitly so the bind address and the allowlist stay
independent — that is what makes "bind 0.0.0.0 in the container, publish to
loopback outside" safe. `/healthz` deliberately makes no Glean call: an
unreachable Glean is a tool-call failure carrying diagnostics, not a reason for
the orchestrator to restart a healthy process.

**The MCP tool's docstring and `Field` descriptions are prompt, not documentation.**
They're what steers the calling model on when to use the tool, how to phrase the
question, and how to read a no-results answer. Edit them as deliberately as code.

**Logging goes to stderr** (`utils/logging.py`), where uvicorn's access log on
stdout cannot interleave with it. Never `print()` on any path reachable from
`__main__.py`; the `print()` calls in `indexing.py` are fine because that's a CLI.
`configure_logging()` passes `force=True` because `MCPServer.__init__` calls
`basicConfig` itself and `__main__.py` builds the server at import time, so by
the time an entrypoint configures logging the root logger already has the SDK's
handler and `basicConfig` would be a silent no-op — dropping both the format and
`-v`. Building the server inside `main()` instead would remove the need for it;
that is a real option, not an SDK limitation. `configure_logging()` is therefore
entrypoint-only: calling it from library code would tear down a caller's
handlers.
`log_call()` wraps every Glean API call and yields a mutable dict callers fill
with result counts, so latency and outcome land on one line.

### Extraction

`extraction.py` is an adapter registry: `ADAPTERS` maps extension → function
returning a raw dict, and `extract()` normalizes it into `ExtractedDoc`. Adding a
format is one function plus one dict entry.

Metadata precedence, in descending order of trust: embedded document properties →
folder path → filename → filesystem stat. **One deliberate inversion: path and
filename override embedded status** — a file in `Archive/` or named `(SUPERSEDED)`
is Archived whatever its properties claim, because people move files and forget to
update properties more often than the reverse. `data/EXTRACTION_NOTES.md` has the
full rationale and the per-format caveats.

Status is drawn from a closed vocabulary (`models.STATUSES`) because the read
path matches it exactly: a document whose embedded properties say `Current` or
`active` would be indexed fine and then never retrieved, with no error on either
path. `extract()` warns on a value outside the set rather than guessing at what
was meant, so `glean-index --dry-run` shows it before the push.

Bodies under `MIN_BODY_CHARS` (200) are skipped as extraction failures rather than
indexed empty; a PDF hitting that is usually scanned and needs OCR.

### Glean API constraints worth knowing before editing `indexing.py`

- Custom property names are prefixed `halcyon*` because Glean reserves operator
  names and rejects collisions — `department` is one.
- Glean **lowercases** custom property names when exposing them as facet fields:
  a property declared `halcyonStatus` is filtered on as `halcyonstatus`. The
  camelCase name matches zero documents and does not error. Prefer a positive
  `EQUALS` filter over a negation — `is_negated` is deprecated (removal Oct 2026)
  and matched the inverse of its intent against this instance.
- Every `object_type` a document uses must be declared in `object_definitions`
  first, or the whole batch fails with "Object definitions not found".
- Bulk upload replaces the datasource contents as a unit (absent documents are
  deleted), which is what makes re-running idempotent. `is_last_page` only on the
  final page; `force_restart_upload` only with `is_first_page`.
- `BASE_URL` in `extraction.py` and `url_regex` in `ensure_datasource()` must stay
  in sync — if they drift, documents index fine but attribution silently breaks.
- Document bodies are sent as `text/plain`; claiming HTML makes Glean strip the
  pipe-delimited table rows the extractors produce.
- Doc IDs are namespaced by `Settings.namespaced_doc_id()` because the sandbox
  datasource is shared with other candidates.
- Indexing is asynchronous — `glean-index` returns minutes before documents become
  searchable.

`X-Glean-ActAs` is a per-request header (`client.act_as_headers`), not baked into
the client, because it decides which documents come back. It's required when the
client token is Global.
