"""Command line interface: index, verify, ask, debug-doc, serve."""

from __future__ import annotations

import argparse
import json
import sys
import time

from client import indexing_client, query_client
from config import ConfigError, Settings
from logs import configure_logging
from models.answers import Answer
from query.pipeline import ask as pipeline_ask


def cmd_index(args) -> int:
    """Extract the corpus and bulk-push it. The only command using the indexing token."""
    # Local import: the write path pulls in docx/pypdf/openpyxl, which `ask` and
    # `serve` would otherwise pay for on every invocation.
    from indexing import datasource, status, upload

    settings = Settings.for_indexing()
    client = indexing_client(settings)

    docs, skipped = upload.collect_documents(settings)
    print(f"Extracted {len(docs) + len(skipped)} document(s) from {settings.docs_root}")
    if skipped:
        print(f"\nSkipped {len(skipped)}, not pushed:")
        for reason in skipped:
            print(f"  !! {reason}")

    print(f"\nPushing {len(docs)} document(s) to datasource '{settings.datasource}':")
    print(f"{'DOC ID':<18} {'TYPE':<6} {'STATUS':<9} {'DEPARTMENT':<19} {'CHARS':>6}  TITLE")
    # 108 spans a full data row, not the header: titles run to 34 characters.
    print("-" * 108)
    for doc in docs:
        print(
            f"{settings.namespaced_doc_id(doc.doc_id):<18} {doc.file_type:<6} "
            f"{doc.status:<9} {doc.department:<19} {len(doc.body):>6}  {doc.title[:34]}"
        )
        for warning in doc.warnings:
            print(f"{'':<18} !! {warning}")

    if args.dry_run:
        print("\n--dry-run: nothing sent to Glean.")
        return 0

    datasource.ensure_datasource(client, settings)
    upload_id = upload.push(client, settings, docs)
    print(f"\nUploaded {len(docs)} document(s), upload_id={upload_id}")

    if args.process_now:
        print(upload.request_processing(client, settings))

    count = status.document_count(client, settings)
    print(f"Glean reports {count} document(s) in the datasource.")
    print("\nIndexing is asynchronous and takes several minutes. Run `glean-chat-bot verify` next.")
    return 0


def _report_coverage(client, settings, doc_ids: list[str], timeout_seconds: int) -> list[str]:
    """Poll until every document is indexed, print each result, return what is still pending."""
    from indexing.status import pending, wait_until_indexed

    coverage = wait_until_indexed(client, settings, doc_ids, timeout_seconds=timeout_seconds)
    still_pending = pending(coverage)
    for doc_id, state in sorted(coverage.items()):
        marker = " " if state == "INDEXED" else "!"
        print(f"  {marker} {doc_id:<22} {state}")

    if not still_pending:
        return still_pending
    failed = [d for d in still_pending if coverage[d].startswith("ERROR")]
    if failed:
        print(
            f"\n{len(failed)} status call(s) failed outright. That is a bad "
            f"token, object type or datasource name - not indexing latency."
        )
    else:
        print(
            f"\n{len(still_pending)} document(s) still not indexed after "
            f"{timeout_seconds}s. Answers that depend on them will be wrong or "
            f"missing, so treat this as not yet verified and re-run."
        )
    return still_pending


def _diagnose_unsearchable(client, settings, doc_id: str) -> None:
    """Tell indexing latency apart from a permissions problem, using one document."""
    from indexing.status import debug_document

    probe = debug_document(client, settings, doc_id)
    print(json.dumps(probe, indent=2, default=str))
    if not probe["allow_anonymous_access"]:
        print(
            "\nallowAnonymousAccess is not set. The document is indexed and "
            "correctly unreachable. Re-indexing will not fix this."
        )
    else:
        print(
            "\nPermissions look right, so this is indexing latency or the "
            "datasource is not enabled in the Glean admin console. Check the "
            "console, then re-run verify."
        )


def cmd_verify(args) -> int:
    """Poll until documents are actually retrievable, not merely accepted."""
    from indexing import status, upload

    indexing_settings = Settings.for_indexing()
    # Built up front so a missing client token fails now, not after fifteen
    # minutes of successful status polling. Two Settings, so the boundary holds.
    query_settings = Settings.for_query()
    client = indexing_client(indexing_settings)

    count = status.document_count(client, indexing_settings)
    print(f"Datasource '{indexing_settings.datasource}' reports {count} document(s).")
    if count == 0:
        print("Nothing uploaded. Run `glean-chat-bot index` first.")
        return 1

    # One budget shared by both waits.
    deadline = time.monotonic() + args.timeout

    docs, _ = upload.collect_documents(indexing_settings)
    print(
        f"\nPolling indexing status for {len(docs)} document(s) "
        f"(timeout {args.timeout}s, this normally takes a few minutes)..."
    )
    still_pending = _report_coverage(
        client, indexing_settings, [d.doc_id for d in docs], args.timeout
    )

    print(f"\nProbing search for {args.query!r}...")
    # One query client for every probe attempt, rather than a fresh SDK client
    # (and its httpx pool) per poll.
    with query_client(query_settings) as probe_client:
        result = status.wait_until_searchable(
            query_settings,
            probe_query=args.query,
            probe_top_k=args.probe_top_k,
            timeout_seconds=max(1, int(deadline - time.monotonic())),
            client=probe_client,
        )

    if result["searchable"]:
        print(f"\nSearchable after {result['attempts']} attempt(s). Top results:")
        for title in result["titles"]:
            print(f"  - {title}")
        # Searchable but incomplete is not verified.
        return 1 if still_pending else 0

    print(f"\nStill not searchable after {result['attempts']} attempt(s).")
    print("Checking one document directly to tell indexing from permissions...")
    if docs:
        _diagnose_unsearchable(client, indexing_settings, docs[0].doc_id)
    return 1


def cmd_ask(args) -> int:
    answer = pipeline_ask(
        args.question,
        top_k=args.top_k,
        include_citations=not args.no_citations,
    )
    if args.json:
        print(json.dumps(answer.to_dict(), indent=2, default=str))
        return 0
    _print_answer(answer)
    return 0


def cmd_debug_doc(args) -> int:
    """Fetch one document's indexed state. Accepts HR-004 or halcyon-HR-004."""
    from indexing.status import debug_document

    settings = Settings.for_indexing()
    client = indexing_client(settings)
    probe = debug_document(client, settings, args.doc_id)
    print(json.dumps(probe, indent=2, default=str))
    return 0


def cmd_serve(_args) -> int:
    """Run the MCP server on stdio. Still exactly one tool, ask_company_docs.

    mcp.run() blocks until the process exits, so this never returns.
    """
    from mcp_server import main as serve_main

    serve_main()


def _print_answer(answer: Answer) -> None:
    print()
    print(answer.answer)
    if answer.sources:
        print("\nSources:")
        for source in answer.sources:
            if source.resolved:
                print(f"  [{source.marker}] {source.title} ({source.doc_id})")
                print(f"      {source.url}")
            else:
                # Loud on purpose: the model pointed at a passage it never got.
                print(f"  [{source.marker}] UNRESOLVED - not among the retrieved passages")
    print("\nDiagnostics:")
    for key, value in answer.diagnostics.items():
        print(f"  {key}: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glean-chat-bot",
        description="Index Halcyon documents into Glean and ask grounded questions.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_index = subparsers.add_parser("index", help="extract and bulk-push the corpus")
    p_index.add_argument("--dry-run", action="store_true", help="extract and report, send nothing")
    p_index.add_argument(
        "--process-now",
        action="store_true",
        help="ask Glean to process immediately (rate limited to once per 3h per datasource)",
    )
    p_index.set_defaults(func=cmd_index)

    p_verify = subparsers.add_parser("verify", help="poll until documents are searchable")
    p_verify.add_argument("--query", default="PTO policy", help="probe query")
    p_verify.add_argument(
        "--probe-top-k", type=int, default=5, help="results to request for the probe query"
    )
    p_verify.add_argument("--timeout", type=int, default=900, help="seconds before giving up")
    p_verify.set_defaults(func=cmd_verify)

    p_ask = subparsers.add_parser("ask", help="ask a question")
    p_ask.add_argument("question")
    p_ask.add_argument("--top-k", type=int, default=None, help="passages to retrieve")
    p_ask.add_argument("--no-citations", action="store_true", help="suppress the source list")
    p_ask.add_argument("--json", action="store_true", help="emit the raw MCP-shaped dict")
    p_ask.set_defaults(func=cmd_ask)

    p_serve = subparsers.add_parser("serve", help="run the MCP server on stdio")
    p_serve.set_defaults(func=cmd_serve)

    p_debug = subparsers.add_parser("debug-doc", help="fetch one document's indexed state")
    p_debug.add_argument("doc_id", help="e.g. HR-004 or halcyon-HR-004")
    p_debug.set_defaults(func=cmd_debug_doc)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        # Bad argument values are user error, not a crash.
        print(f"Invalid argument: {exc}", file=sys.stderr)
        return 2
