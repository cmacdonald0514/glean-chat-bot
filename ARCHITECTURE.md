# Architecture

Two paths that share `models.py` and `utils/config.py` and touch nothing else.
`CLAUDE.md` has the reasoning behind the decisions these show.

## Read path — a question arrives

```mermaid
flowchart TD
    Z["MCP client — POST /mcp, streamable HTTP"] --> A["MCP tool: ask_company_docs"]
    A --> B["ask()"]
    B --> C["search() — Active documents only"]
    C --> D{"clears the relevance floor?"}
    D -->|no| E["grounded refusal — Chat is never called"]
    D -->|yes| F["generate() — Chat, with retrieval disabled"]
    F --> G["resolve_citations()"]
    E --> H["Answer: answer, sources, diagnostics"]
    G --> H
```

## Write path — extraction and indexing

```mermaid
flowchart TD
    S["indexer-cron — supercronic, no Glean token"] -->|"POST /index"| T["glean-indexd"]
    T --> A
    U["glean-index (CLI)"] --> A
    A["index run"] --> B["walk(data/)"]
    B --> C["extract() — one adapter per format"]
    C --> D{"body over 200 chars?"}
    D -->|no| E["skipped as an extraction failure"]
    D -->|yes| F["ensure_datasource()"]
    F --> G["bulk_index() in pages of 50"]
    G --> H["Glean — processed asynchronously"]
```

The two entrypoints share everything below `index run`: `glean-indexd` calls
`indexing.index_once()`, and `glean-index` calls `indexing.run()`, which is the
same sequence plus the per-document table a CLI can print and a server cannot.
Only one run happens at a time — a bulk upload replaces the datasource contents
as a unit, so the service refuses a concurrent request with 409 rather than
letting two runs race over the result.
