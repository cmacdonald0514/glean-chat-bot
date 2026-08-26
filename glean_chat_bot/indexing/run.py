import logging
import re
import uuid
from datetime import datetime
from itertools import batched

from glean.api_client import Glean, models

from glean_chat_bot.indexing.extraction import BASE_URL, slugify, walk
from glean_chat_bot.models import STATUS_PROPERTY, ExtractedDoc
from glean_chat_bot.utils.config import Settings
from glean_chat_bot.utils.logging import log_call

log = logging.getLogger("glean_chat_bot.indexing.run")

OBJECT_TYPE = "Document"

# (property name, display label, ExtractedDoc attribute, show as facet). Names
# are prefixed because Glean reserves a set of operator names and rejects any
# custom property that collides - "department" is one.
CUSTOM_PROPERTIES = [
    ("halcyonDepartment", "Department", "department", True),
    ("halcyonDocType", "Document type", "doc_type", True),
    ("halcyonClassification", "Classification", "classification", True),
    (STATUS_PROPERTY, "Status", "status", True),
    ("halcyonFileType", "File type", "file_type", True),
    ("halcyonSourcePath", "Source path", "source_path", False),
]

BATCH_SIZE = 50


def ensure_datasource(client: Glean, settings: Settings) -> None:
    """Create or update the datasource definition. /adddatasource is an upsert, so this re-runs."""
    with log_call("indexing.datasources.add", datasource=settings.datasource):
        client.indexing.datasources.add(
            name=settings.datasource,
            display_name="Halcyon Shared Drive",
            # Must not be UNCATEGORIZED - Glean treats category as a relevance signal.
            datasource_category=models.DatasourceCategory.PUBLISHED_CONTENT,
            # Must stay in sync with the extractor's BASE_URL; if the two drift,
            # documents index fine but attribution silently breaks.
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


def _author_reference(name: str | None) -> models.UserReferenceDefinition | None:
    """Glean rejects a user reference carrying only a name, and these documents hold no emails.

    A slug of the name goes in as datasourceUserId - an identifier meaningful
    only inside this datasource, which is what it is. Synthesising an email
    would risk attributing a document to a real, wrong person.
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
            # text/plain, not text/html: claiming HTML makes Glean strip the
            # pipe-delimited table rows the extractors produce.
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
        if body_chars < settings.min_body_chars:
            skipped.append(
                f"{doc.doc_id} ({doc.source_path}): body is only "
                f"{body_chars} chars, below the {settings.min_body_chars} floor. "
                f"{' '.join(doc.warnings) or 'Probably an extraction failure.'}"
            )
            continue
        pushable.append(doc)
    return pushable, skipped


def push(client: Glean, settings: Settings, docs: list[ExtractedDoc]) -> str:
    """Upload every document in one bulk upload. Returns the upload_id.

    Bulk replaces the datasource contents as a unit - documents absent from the
    upload are deleted afterwards - which is what makes re-running idempotent.
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
                # Only ever on the final page, or the upload stays open.
                is_last_page=is_last,
                # Discards an interrupted upload, which would otherwise fail
                # every later bulk_index. Only valid with is_first_page.
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


def index_documents(
    client: Glean, settings: Settings, docs: list[ExtractedDoc], *, process_now: bool
) -> dict:
    """The network half of a run: declare the datasource, push, report."""
    ensure_datasource(client, settings)
    upload_id = push(client, settings, docs)
    return {
        "upload_id": upload_id,
        # request_processing() reports its own failures rather than raising: a
        # 429 must not fail a run whose documents are already uploaded.
        "processing": request_processing(client, settings) if process_now else "not requested",
        "datasource_count": document_count(client, settings),
    }
