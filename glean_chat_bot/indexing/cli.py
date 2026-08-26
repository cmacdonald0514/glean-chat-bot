import argparse
import sys

from glean_chat_bot.client import indexing_client
from glean_chat_bot.indexing.run import collect_documents, index_documents
from glean_chat_bot.utils.config import ConfigError, Settings
from glean_chat_bot.utils.logging import configure_logging


def index_and_report(dry_run: bool, process_now: bool) -> int:
    settings = Settings.for_indexing()

    docs, skipped = collect_documents(settings)
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

    if dry_run:
        print("\n--dry-run: nothing sent to Glean.")
        return 0

    with indexing_client(settings) as client:
        result = index_documents(client, settings, docs, process_now=process_now)

    print(f"\nUploaded {len(docs)} document(s), upload_id={result['upload_id']}")
    if process_now:
        print(result["processing"])
    print(f"Glean reports {result['datasource_count']} document(s) in the datasource.")
    print(
        "\nIndexing is asynchronous and takes several minutes. Documents stay "
        "unsearchable until it finishes, so give it time before querying."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="glean-index",
        description="Extract the Halcyon corpus and bulk-push it into Glean.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--dry-run", action="store_true", help="extract and report, send nothing")
    parser.add_argument(
        "--process-now",
        action="store_true",
        help="ask Glean to process immediately (rate limited to once per 3h per datasource)",
    )
    args = parser.parse_args(argv)

    configure_logging(args.verbose)
    try:
        return index_and_report(args.dry_run, args.process_now)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
