"""`glean-index` extracts the corpus and bulk-pushes it to Glean."""

import argparse
import logging
import re
import sys
import time
import uuid
from datetime import datetime
from itertools import batched

from glean.api_client import Glean, models

from glean_chat_bot.client import indexing_client
from glean_chat_bot.extraction import BASE_URL, MIN_BODY_CHARS, slugify, walk
from glean_chat_bot.models import STATUS_PROPERTY, ExtractedDoc
from glean_chat_bot.utils.config import ConfigError, Settings
from glean_chat_bot.utils.logging import configure_logging, log_call

log = logging.getLogger("glean_chat_bot.indexing")

OBJECT_TYPE = "Document"

# (property name, display label, ExtractedDoc attribute, show as facet).
# Names are prefixed because Glean reserves a set of operator names and rejects
# any custom property that collides - "department" does.
CUSTOM_PROPERTIES = [
    ("halcyonDepartment", "Department", "department", True),
    ("halcyonDocType", "Document type", "doc_type", True),
    ("halcyonClassification", "Classification", "classification", True),
    (STATUS_PROPERTY, "Status", "status", True),
    ("halcyonFileType", "File type", "file_type", True),
    ("halcyonSourcePath", "Source path", "source_path", False),
]

BATCH_SIZE = 50

# Quoted by `glean-index-trigger` too, which offers the same flag.
PROCESS_NOW_HELP = "ask Glean to process immediately (rate limited to once per 3h per datasource)"


# --- datasource definition ---------------------------------------------------


def ensure_datasource(client: Glean, settings: Settings) -> None:
    """Create or update the datasource definition. /adddatasource is an upsert, so this re-runs."""
    with log_call("indexing.datasources.add", datasource=settings.datasource):
        client.indexing.datasources.add(
            name=settings.datasource,
            display_name="Halcyon Shared Drive",
            # Must not be UNCATEGORIZED - Glean treats category as a relevance signal.
            datasource_category=models.DatasourceCategory.PUBLISHED_CONTENT,
            # How Glean recognises URLs as belonging to this datasource. Derived
            # from the extractor's BASE_URL; if the two drift, documents index
            # fine but attribution silently breaks.
            url_regex=re.escape(BASE_URL) + "/.*",
            # Every object_type a document uses must be declared here first, or
            # the whole batch fails with "Object definitions not found".
            object_definitions=[
                models.ObjectDefinition(
                    name=OBJECT_TYPE,
                    display_label="Document",
                    doc_category=models.DocCategory.PUBLISHED_CONTENT,
                    property_definitions=[
                        models.PropertyDefinition(
                            name=name,
                            display_label=label,
                            property_type=models.PropertyDefinitionPropertyType.TEXT,
                            ui_options=models.UIOptions.SEARCH_RESULT,
                            hide_ui_facet=not facet,
                        )
                        for name, label, _attr, facet in CUSTOM_PROPERTIES
                    ],
                    summarizable=True,
                )
            ],
            is_test_datasource=False,
        )
    log.info("datasource %s ensured", settings.datasource)


# --- collect and map ---------------------------------------------------------


def _author_reference(name: str | None) -> models.UserReferenceDefinition | None:
    """Glean rejects a user reference carrying only a name, and these documents hold no emails.

    A slug of the name goes in as datasourceUserId: an identifier claimed to be
    meaningful only inside this datasource, which is what it is. Synthesising an
    email address would risk attributing a document to a real, wrong person.
    """
    if not name:
        return None
    return models.UserReferenceDefinition(name=name, datasource_user_id=slugify(name))


def _epoch_seconds(iso_timestamp: str | None) -> int | None:
    if not iso_timestamp:
        return None
    try:
        return int(datetime.fromisoformat(iso_timestamp).timestamp())
    except ValueError:
        log.warning("unparseable timestamp %r, omitting", iso_timestamp)
        return None


def to_document_definition(doc: ExtractedDoc, settings: Settings) -> models.DocumentDefinition:
    return models.DocumentDefinition(
        datasource=settings.datasource,
        object_type=OBJECT_TYPE,
        id=settings.namespaced_doc_id(doc.doc_id),
        title=doc.title,
        view_url=doc.view_url,
        body=models.ContentDefinition(
            # text/plain, not text/html: claiming HTML would make Glean strip
            # pipe-delimited table rows looking for tags that are not there.
            mime_type="text/plain",
            text_content=doc.body,
        ),
        author=_author_reference(doc.author),
        created_at=_epoch_seconds(doc.created),
        updated_at=_epoch_seconds(doc.modified),
        permissions=models.DocumentPermissionsDefinition(allow_anonymous_access=True),
        custom_properties=[
            models.CustomProperty(name=name, value=getattr(doc, attr))
            for name, _label, attr, _facet in CUSTOM_PROPERTIES
        ],
    )


def collect_documents(settings: Settings) -> tuple[list[ExtractedDoc], list[str]]:
    """Walk the corpus and split it into pushable documents and skip reasons."""
    pushable, skipped = [], []
    for doc in walk(str(settings.docs_root)):
        body_chars = len(doc.body.strip())
        if body_chars < MIN_BODY_CHARS:
            skipped.append(
                f"{doc.doc_id} ({doc.source_path}): body is only "
                f"{body_chars} chars, below the {MIN_BODY_CHARS} floor. "
                f"{' '.join(doc.warnings) or 'Probably an extraction failure.'}"
            )
            continue
        pushable.append(doc)
    return pushable, skipped


# --- push --------------------------------------------------------------------


def push(client: Glean, settings: Settings, docs: list[ExtractedDoc]) -> str:
    """Upload every document in one bulk upload. Returns the upload_id.

    Bulk replaces the datasource contents as a unit - documents absent from the
    upload are deleted afterwards - which is what makes re-running idempotent.
    is_last_page is therefore only ever set on the final page.
    """
    upload_id = f"{settings.datasource}-{uuid.uuid4().hex[:12]}"
    # Batch the documents, not their definitions: only one page's worth of
    # DocumentDefinition (each carrying a copy of the body) is alive at a time.
    pages = list(batched(docs, BATCH_SIZE))

    for page_number, page in enumerate(pages):
        batch = [to_document_definition(d, settings) for d in page]
        is_first = page_number == 0
        is_last = page_number == len(pages) - 1
        with log_call(
            "indexing.documents.bulk_index",
            upload_id=upload_id,
            page=page_number,
            docs=len(batch),
            first=is_first,
            last=is_last,
        ):
            client.indexing.documents.bulk_index(
                upload_id=upload_id,
                datasource=settings.datasource,
                documents=batch,
                is_first_page=is_first,
                is_last_page=is_last,
                # An interrupted upload leaves the datasource with one open, and
                # every later bulk_index fails until it is discarded. Only valid
                # together with is_first_page.
                force_restart_upload=True if is_first else None,
            )
    return upload_id


def request_processing(client: Glean, settings: Settings) -> str:
    """Ask Glean to process now. Rate limited to once per 3h per datasource, hence opt-in."""
    try:
        with log_call("indexing.documents.process_all", datasource=settings.datasource):
            client.indexing.documents.process_all(request={"datasource": settings.datasource})
        return "processing requested"
    except Exception as exc:
        # A 429 here is harmless - documents still process on the normal
        # schedule. Never fail the ingest over it.
        return (
            f"processing not requested ({type(exc).__name__}: {exc}); "
            f"indexing continues on the normal schedule"
        )


def document_count(client: Glean, settings: Settings) -> int:
    with log_call("indexing.documents.count", datasource=settings.datasource) as rec:
        response = client.indexing.documents.count(datasource=settings.datasource)
        count = response.document_count or 0
        rec["count"] = count
    return count


# --- one run ------------------------------------------------------------------


def push_documents(
    client: Glean, settings: Settings, docs: list[ExtractedDoc], *, process_now: bool
) -> dict:
    """The network half of a run, shared by both entrypoints.

    `index_once()` and `run()` differ only in how they report - a log record
    versus a printed table - so the sequence of API calls lives here rather than
    once in each. A step added to the write path (a pre-push validation, a
    second count after processing) then reaches the CLI and the scheduled
    service together, instead of one of them silently.
    """
    ensure_datasource(client, settings)
    upload_id = push(client, settings, docs)
    return {
        "upload_id": upload_id,
        # request_processing() reports its own failures rather than raising: a
        # 429 must not fail a run whose documents are already uploaded.
        "processing": request_processing(client, settings) if process_now else "not requested",
        "datasource_count": document_count(client, settings),
    }


def index_once(settings: Settings, *, process_now: bool = False) -> dict:
    """One indexing run, logged rather than printed. The service path's core.

    The split from `run()` is the point: `run()` is a CLI and may print, this
    runs inside the glean-indexd server process where stdout belongs to
    uvicorn's access log and everything the operator needs has to reach stderr
    as a log record instead.

    Returns the summary the HTTP caller gets back as JSON.
    """
    started = time.perf_counter()

    docs, skipped = collect_documents(settings)
    log.info(
        "extracted %d document(s) from %s, %d skipped",
        len(docs) + len(skipped),
        settings.docs_root,
        len(skipped),
    )
    for reason in skipped:
        log.warning("skipped: %s", reason)
    for doc in docs:
        for warning in doc.warnings:
            log.warning("%s: %s", settings.namespaced_doc_id(doc.doc_id), warning)

    # `with` closes the SDK's connection pools deterministically instead of
    # leaving its reference cycle to a GC pass -- the same reason ask.py holds
    # one, and it matters more here: this process is long-lived and allocates
    # almost nothing between runs, so a collection may be a long way off.
    with indexing_client(settings) as client:
        result = push_documents(client, settings, docs, process_now=process_now)

    duration_ms = round((time.perf_counter() - started) * 1000)
    log.info(
        "indexing run complete: %d document(s), upload_id=%s, %d in datasource, %dms",
        len(docs),
        result["upload_id"],
        result["datasource_count"],
        duration_ms,
    )
    return {"documents": len(docs), "skipped": skipped, **result, "duration_ms": duration_ms}


# --- entry point -------------------------------------------------------------


def run(dry_run: bool, process_now: bool) -> int:
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
        result = push_documents(client, settings, docs, process_now=process_now)

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
        help=PROCESS_NOW_HELP,
    )
    args = parser.parse_args(argv)

    configure_logging(args.verbose)
    try:
        return run(args.dry_run, args.process_now)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
