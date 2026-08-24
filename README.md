# Halcyon Docs Chatbot

A grounded question-answering bot over a local document corpus, built on Glean's
Indexing, Search and Chat APIs and exposed as a single MCP tool.

There is no web UI. The chat interface is an MCP client — Cursor, Claude
Desktop, or the CLI in this repo.

## The pipeline

```
data/Halcyon Shared Drive/     16 .docx / .pdf / .xlsx files
        |
        |  extraction/         one adapter per file type -> normalized records
        v
   Indexing API                bulk push, anonymous access, stable doc IDs
        |
        |  ...minutes...       indexing is asynchronous
        v
   Search API                  retrieve top-k passages, filtered to our datasource
        |
        |  relevance floor     below the floor -> stop here, never call Chat
        v
   Chat API                    generate from those passages only, no retrieval
        |
        |  resolve citations   map [n] markers back to retrieved passages
        v
   {answer, sources, diagnostics}
```

Two rules make the whole thing work:

**Search runs before Chat, and Chat never retrieves.** Passages are retrieved
explicitly and passed in. That gives a retrieval stage that can be logged,
inspected and tuned, and citations that map to document IDs we pushed ourselves.

**Zero results short-circuit.** If nothing clears the relevance floor, the
answer is an honest "no indexed content found" and Chat is never called. This is
what stops the model answering from world knowledge.

## Setup

Requires [Poetry](https://python-poetry.org/docs/#installation) and Python 3.12+.

```bash
poetry config virtualenvs.in-project true   # keeps the venv at ./.venv
poetry install
cp .env.example .env      # then fill in the two tokens
```

This installs the project and puts a `glean-chat-bot` command on the venv's
path. Prefix commands with `poetry run`, or activate `.venv` and drop the prefix.

`.env` is gitignored. Never commit real tokens.

| Variable | Used by | Notes |
|---|---|---|
| `GLEAN_INSTANCE` | both | SDK builds `https://{instance}-be.glean.com` |
| `GLEAN_INDEXING_TOKEN` | `index` only | never loaded on the query path |
| `GLEAN_CLIENT_TOKEN` | search + chat | scope Chat/Search, type Global |
| `GLEAN_DATASOURCE` | both | sandbox is shared, so this namespaces doc IDs |
| `GLEAN_DOCS_ROOT` | `index` only | corpus root (`data/Halcyon Shared Drive`) — genuinely not read on the query path |
| `GLEAN_ACT_AS` | search + chat | required for Global tokens — see below |

Optional, with defaults: `GLEAN_DOC_ID_PREFIX` (`halcyon`), `GLEAN_TOP_K` (`5`),
`GLEAN_MAX_SNIPPET_SIZE` (`2000`), `GLEAN_MIN_TERM_OVERLAP` (`0.30`),
`GLEAN_CHAT_TIMEOUT_MS` (`60000`).

A **Global** client token can act as any user, so Glean requires you to say
which: without an `X-Glean-ActAs` header it rejects the request with `Required
header missing: X-Glean-ActAs`. `GLEAN_ACT_AS` supplies it. A User-scoped token
carries its own identity and needs none of this, which is why the variable is
optional rather than required.

The header is passed per request rather than baked into the client, because
which user a request runs as decides which documents come back — it is the first
thing you want visible when search returns nothing for one person and results
for another. It appears on every search log line.

### Token separation

The two tokens are separated structurally, not by convention. `Settings` has two
constructors: `for_indexing()` reads `GLEAN_INDEXING_TOKEN` and leaves
`client_token` `None`; `for_query()` reads `GLEAN_CLIENT_TOKEN` and never reads
the indexing variable at all. The MCP server and the ask path call `for_query()`,
so they have no indexing token in memory to leak. `indexing_client()` raises if
handed query settings rather than sending `None` as a bearer token.

## Usage

```bash
poetry run glean-chat-bot index --dry-run   # extract and report, send nothing
poetry run glean-chat-bot index             # extract and bulk-push
poetry run glean-chat-bot verify            # poll until every document is indexed
poetry run glean-chat-bot ask "How many PTO days do I get?"
poetry run glean-chat-bot debug-doc HR-004  # one document's state and permissions
poetry run glean-chat-bot serve             # run the MCP server on stdio
```

Add `-v` for debug logging, `--json` on `ask` for the raw MCP-shaped dict.

`index` is an admin operation. The query path never indexes: Glean indexing
takes minutes, so a re-index triggered by a query could not help the query that
triggered it.

### MCP client configuration

```json
{
  "mcpServers": {
    "glean-company-docs": {
      "command": "/absolute/path/to/glean-chat-bot/.venv/bin/glean-chat-bot",
      "args": ["serve"],
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

Only the client token appears here — and no `GLEAN_DOCS_ROOT`. `Settings.for_query()`
does not read it, so a query-only host needs no corpus on disk. `GLEAN_INDEXING_TOKEN`
never appears on this path at all.

One tool, `ask_company_docs(question, top_k=5, include_citations=True) -> dict`,
returning `{answer, sources, diagnostics}`. `diagnostics` reports what was
searched and how many results came back, so the calling model can tell "nothing
matched" from "my phrasing missed" and retry accordingly.

## Design notes

### The relevance floor is term overlap, not a score

Glean's Search API returns no relevance score. `SearchResult` carries `url`,
`title`, `document`, `snippets`, `fullText` and `trackingToken`; there is no
score field anywhere in the response models. So the floor is computed locally:
the fraction of the question's content words appearing in the retrieved
passages, against `GLEAN_MIN_TERM_OVERLAP`.

"What is our 401k employer match?" scores 0.00 and short-circuits.
"How many PTO days do I get?" clears the floor and proceeds.

The alternative — trusting the top-ranked result — is not a floor at all, since
Glean returns its best match for any query including queries with no good match.
That is exactly the hallucination path.

Known limitation: this is lexical, so a pure paraphrase scores lower than it
deserves. The threshold is an environment variable because it is a tunable
heuristic, not a principled score.

### Chat retrieval is off, so citations are our own markers

`agent_config.agent = GPT` talks straight to the model with no company
retrieval, and both tool sets are disabled. With retrieval off Glean has nothing
of its own to cite, so `ChatMessageFragment.citation` is empty by construction.

The citations we resolve are therefore the `[n]` markers the model emits against
the passages we numbered — a stronger guarantee, since every resolved marker
maps to a document ID we pushed. Markers with no matching passage are reported
as `resolved: false` rather than dropped, because a dropped bad citation hides
exactly what the caller needs to see.

`ChatMessage.citations` — the top-level field — is deprecated as of 2026-02-06,
removal scheduled 2026-10-15, in favour of the inline fragment citation. This
code does not read it, but does read the inline field defensively and reports
anything found in `diagnostics.glean_inline_citations`.

### Bulk indexing and stable IDs

`bulkIndexDocuments` replaces the datasource contents as a unit — documents
absent from an upload are deleted afterwards. That is what makes re-running
`index` idempotent rather than accumulating orphans. Document IDs come from
`extraction/` (embedded properties, else derived from file path) plus the
`halcyon-` prefix, so the same file gets the same ID on every run and re-runs
upsert.

`force_restart_upload` defaults on: ctrl-C'ing the script leaves an open upload
that blocks every subsequent one, and during development that happens often.

### Indexing gotchas, all found the hard way

Every one of these is a 400 that stops the entire batch, and none of them is
obvious from the document payload:

- **View URLs must be percent-encoded.** Every folder in this corpus has a space
  in its name, and one malformed `viewURL` rejects all sixteen documents:
  `Error parsing view URL ... Illegal character in path at index 40`.
- **Object types must be declared before use.** A document with an
  `objectType` not present in the datasource's `objectDefinitions` fails with
  `Object definitions not found for object types: Document (16)`.
- **Custom property names collide with Glean's operators.** `department` is
  reserved. Rather than discovering the reserved list one 400 at a time, every
  custom property here is prefixed (`halcyonDepartment`, `halcyonStatus`, ...).
- **An author needs an identity, not just a name.** `Email or Datasource Id must
  be specified for document user`. The extractor only has display names, so a
  slug of the name goes in `datasourceUserId` — an identifier claimed to be
  meaningful only inside this datasource, which is exactly what it is.
  Synthesising `firstname.lastname@company.com` was rejected: if such an address
  matched a real Glean user, documents would be attributed to the wrong person.
- **The status endpoint allows one request per second.** Checking sixteen
  documents is a sequential, rate-limited sweep, not a concurrent one.
- **Chat needs an explicit timeout.** The SDK default is around five seconds and
  generation routinely exceeds it, producing a `ReadTimeout` that looks like a
  network fault rather than a timeout that was always going to fire.

### Verified means every document, not any document

`verify` polls each pushed document's indexing status until all report
`INDEXED`, and only then probes search. An earlier version declared success on
the first non-zero search result and reported "searchable after 1 attempt" while
3 of 16 documents were still missing — including FIN-011, the *active* expense
policy, while FIN-007, the *archived* one, was already indexed. Every per-diem
question in that window answered from the superseded document.

Glean indexes documents individually and at its own pace, so "a search returned
something" is far too weak a gate. `verify` exits non-zero on partial coverage.

### Debugging in a customer environment

Every Glean call goes through `log_call` in `logs.py`, which logs
endpoint, latency and result count on one line whether the call succeeds or
raises. `log_call` lives in the file every call already imports, so there is no
way to make a call that bypasses it.

When documents do not appear in search, `debug-doc` distinguishes the two
causes. `uploaded_permissions.allowAnonymousAccess` false means the document is
indexed and correctly unreachable — re-indexing will not fix it. Permissions
correct plus no search results means indexing latency, or the datasource is not
enabled in the Glean admin console.

### Bulk upload can leave documents queued indefinitely

On the first real run, 13 of 16 documents reached `INDEXED` within a few minutes
and 3 — ENG-018, ENG-032, FIN-011 — sat at `NOT_INDEXED` for over 40 minutes.
`debug-doc` showed them `UPLOADED` with `allowAnonymousAccess: true`, so neither
the push nor permissions were at fault, and nothing distinguished them from the
13 that worked (no unusual characters, no correlation with embedded email
addresses, comparable body lengths).

Re-pushing the same three through the incremental `/indexdocuments` endpoint
indexed all three within seconds.

So: `/bulkindexdocuments` is right for defining the corpus, because it makes
re-runs idempotent and deletes what is no longer present. But its processing is
queued and that queue can stall. `/indexdocuments` is the lever for a targeted
re-push when it does. Worth knowing before demoing anything on a deadline.

Note the risk this creates for the obvious workaround: each bulk run re-queues
processing for the whole datasource, so re-running `index` to fix three stuck
documents can reset the clock on the thirteen that were already fine.

### Known finding: search is conjunctive, so long questions are fragile

The multi-document procurement question from EVAL_QUESTIONS returns nothing:

```
"We want to buy a $60k analytics tool that stores customer data. What approvals?"  -> 0 results
"analytics tool approvals"                                                          -> 2 results
"procurement approval"                                                              -> 1 result
"$60k"                                                                              -> 0 results
"analytics tool"                                                                    -> 0 results
```

Individual terms that match nothing (`$60k`, `analytics tool`) do not merely
fail to contribute — they drop the whole query to zero. Search behaves
conjunctively enough that a long natural-language question containing specific
figures retrieves worse than a three-word keyword query.

This is not worked around in Milestone 1. The mitigation already present is
diagnostic: `results_returned: 0` with `floor_passed: false` is returned to the
calling model, and the tool docstring instructs it to retry once using the
terminology the documents would use. Query rewriting and adaptive retry are a
later milestone.

### Known finding: table rows are detached from their headings

The docx adapter in `extraction/adapters.py` emits all paragraphs, then all
tables. In FIN-011 that puts `Domestic travel | $75 per day` at the end of the
body, away from the "Meal per diem" heading it belongs under. Retrieval still
finds the document, but the passage containing the number carries no
surrounding context.

Not fixed in Milestone 1. Interleaving tables in document order is the fix.

## Eval results

Against EVAL_QUESTIONS.md on the full 16-document corpus, 13 of 14 sampled
questions answer correctly with the right citation:

| Question | Expected | Result |
|---|---|---|
| PTO days | 18, 23 at L6+ | correct, cites HR-004 |
| Deploy freeze | Dec 15 – Jan 2 | correct, cites ENG-018 |
| Parental leave | 16 weeks | correct, cites HR-013 |
| VPN | Tailscale, not AnyConnect | correct, cites IT-014 |
| Home office stipend | $1,200 / 24 months | correct, cites HR-009 |
| Expense deadline | 30 days | correct, cites FIN-011 |
| On-call rotation | 1 week, Mon 10am MT | correct, cites ENG-032 |
| Merit increases | March, effective Apr 1 | correct, cites HR-017 |
| Meal per diem | $75, FIN-011 only | **correct** — retrieved both FIN-007 and FIN-011, cited FIN-011 |
| Corporate card | Ramp, not Brex | correct, cites FIN-011 |
| $60k tool approvals | FIN-019 + SEC-003 | **fails** — 0 results, see conjunctive search above |
| 401k match | refuse | refuses, `chat_called: false` |
| CEO name | refuse | refuses |
| Dental deductible | refuse | refuses, `chat_called: false` |

Two of these are worth more than the rest.

**The archived-document trap passes.** Retrieval returned both the archived
FIN-007 ($50/day, Brex) and the active FIN-011 ($75/day, Ramp), and the answer
cited FIN-011 alone with the right figure. Nothing in the prompt or the
retrieval stage instructs it to prefer active documents — the model resolved it
from the "ARCHIVED ... do not rely on these figures" banner in the body text.
That is worth demonstrating precisely *because* it is luck rather than design:
filtering on `halcyonStatus` in the retrieval stage makes it deterministic, and
that is a two-line change.

**The CEO question refuses despite clearing the floor.** Term overlap scored
0.667 (`halcyon` and `robotics` both appear in the corpus), so the relevance
floor passed it through to Chat — and the grounding prompt caught it anyway:
"The provided passages do not identify the CEO". The floor and the prompt are
independent defences, and here the second one carried it. The other three
out-of-scope questions never reached Chat at all.

## Layout

```
src/
  config.py          env loading, one Settings, two constructors, fail fast
  logs.py            log format, and the timing wrapper every Glean call goes through
  cli.py             index / verify / ask / debug-doc / serve
  mcp_server.py      one tool, thin wrapper over query.pipeline.ask()
  client.py          indexing and query client factories, the ActAs header
  models/            Passage, Source, Answer, ExtractedDoc
  extraction/        adapters.py (one per file type), walker.py (path signals, walk)
  indexing/          datasource.py (object + property definitions)
                     upload.py (collect, map, bulk upsert)
                     status.py (count, debug, poll until indexed and searchable)
  query/             retrieval.py (search -> Passage, the relevance floor)
                     generation.py (chat -> answer + resolved citations)
                     pipeline.py (ask() — the single orchestration function)
data/                the corpus
docs/                eval question set, ingestion notes
```

Tooling: Poetry for dependencies and the `glean-chat-bot` console script, Ruff
for linting and formatting. `poetry run ruff check .` and
`poetry run ruff format .` before committing.

## Not built yet

Group and user permissions, department filtering, freshness annotations, a
content-hash manifest, adaptive top-k retry, streaming, conversation memory, a
test suite, retry and backoff, Docker, CI.

`mcp` 2.0 renamed `FastMCP` to `MCPServer` (`mcp.server.mcpserver.MCPServer`).
`from mcp.server.fastmcp import FastMCP`, which most tutorials still show, does
not exist in 2.x and has no compatibility alias.
