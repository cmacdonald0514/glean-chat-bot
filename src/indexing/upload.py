"""The write path: collect documents, map them onto Glean's shapes, bulk push."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from glean.api_client import Glean, models

from config import Settings
from extraction.adapters import MIN_BODY_CHARS
from extraction.walker import slugify, walk
from indexing.datasource import CUSTOM_PROPERTIES, OBJECT_TYPE
from logs import log_call
from models.documents import ExtractedDoc

log = logging.getLogger("glean_chat_bot.indexing")

BATCH_SIZE = 50


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
        if len(doc.body.strip()) < MIN_BODY_CHARS:
            skipped.append(
                f"{doc.doc_id} ({doc.source_path}): body is only "
                f"{len(doc.body.strip())} chars, below the {MIN_BODY_CHARS} floor. "
                f"{' '.join(doc.warnings) or 'Probably an extraction failure.'}"
            )
            continue
        pushable.append(doc)
    return pushable, skipped


def push(client: Glean, settings: Settings, docs: list[ExtractedDoc]) -> str:
    """Upload every document in one bulk upload. Returns the upload_id.

    Bulk replaces the datasource contents as a unit - documents absent from the
    upload are deleted afterwards - which is what makes re-running idempotent.
    is_last_page is therefore only ever set on the final page.
    """
    upload_id = f"{settings.datasource}-{uuid.uuid4().hex[:12]}"
    definitions = [to_document_definition(d, settings) for d in docs]
    batches = [definitions[i : i + BATCH_SIZE] for i in range(0, len(definitions), BATCH_SIZE)]

    for page_number, batch in enumerate(batches):
        is_first = page_number == 0
        is_last = page_number == len(batches) - 1
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
