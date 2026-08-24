"""Verification: is a document uploaded, indexed, and actually retrievable?"""

from __future__ import annotations

import logging
import time

from glean.api_client import Glean

from config import Settings
from indexing.datasource import OBJECT_TYPE
from logs import log_call
from query.retrieval import search

log = logging.getLogger("glean_chat_bot.indexing")

# The status endpoint allows one request per second and 429s above that.
STATUS_RATE_LIMIT_SECONDS = 1.2


def document_count(client: Glean, settings: Settings) -> int:
    with log_call("indexing.documents.count", datasource=settings.datasource) as rec:
        response = client.indexing.documents.count(datasource=settings.datasource)
        count = response.document_count or 0
        rec["count"] = count
    return count


def document_status(client: Glean, settings: Settings, doc_id: str) -> dict:
    full_id = settings.namespaced_doc_id(doc_id)
    with log_call("indexing.documents.status", doc_id=full_id) as rec:
        response = client.indexing.documents.status(
            datasource=settings.datasource, object_type=OBJECT_TYPE, doc_id=full_id
        )
        rec["upload"] = response.upload_status
        rec["indexing"] = response.indexing_status
    return {
        "doc_id": full_id,
        "upload_status": response.upload_status,
        "indexing_status": response.indexing_status,
        "last_uploaded_at": response.last_uploaded_at,
        "last_indexed_at": response.last_indexed_at,
    }


def debug_document(client: Glean, settings: Settings, doc_id: str) -> dict:
    """Separates "the push failed" from "the push worked and permissions hide it".

    allow_anonymous_access is the field that answers it: if it is false, the
    document is indexed and correctly unreachable, and re-indexing will not help.
    """
    full_id = settings.namespaced_doc_id(doc_id)
    with log_call("indexing.documents.debug", doc_id=full_id):
        response = client.indexing.documents.debug(
            datasource=settings.datasource, object_type=OBJECT_TYPE, doc_id=full_id
        )
    permissions = response.uploaded_permissions
    return {
        "doc_id": full_id,
        "status": response.status.model_dump() if response.status else None,
        "uploaded_permissions": permissions.model_dump() if permissions else None,
        "allow_anonymous_access": bool(permissions and permissions.allow_anonymous_access),
    }


def indexing_coverage(client: Glean, settings: Settings, doc_ids: list[str]) -> dict[str, str]:
    """Per-document indexing status. Sequential and rate-limited, since the endpoint is 1/sec."""
    coverage: dict[str, str] = {}
    for position, doc_id in enumerate(doc_ids):
        if position:
            time.sleep(STATUS_RATE_LIMIT_SECONDS)
        # Keyed the same way on both paths, so one document cannot occupy two keys.
        full_id = settings.namespaced_doc_id(doc_id)
        try:
            status = document_status(client, settings, doc_id)
            coverage[full_id] = status["indexing_status"] or "UNKNOWN"
        except Exception as exc:
            coverage[full_id] = f"ERROR ({type(exc).__name__})"
    return coverage


def pending(coverage: dict[str, str]) -> list[str]:
    """Document IDs from a coverage map that have not reached INDEXED."""
    return [doc_id for doc_id, state in coverage.items() if state != "INDEXED"]


def wait_until_indexed(
    client: Glean,
    settings: Settings,
    doc_ids: list[str],
    *,
    timeout_seconds: int = 900,
    poll_seconds: float = 30.0,
) -> dict[str, str]:
    """Poll until every pushed document reports INDEXED, or time out.

    Coverage is per document because a probe query can succeed while a third of
    the corpus is still missing, and which documents are missing decides whether
    an answer is right.
    """
    deadline = time.monotonic() + timeout_seconds
    coverage: dict[str, str] = {}
    outstanding = list(doc_ids)

    while True:
        # Only ask about documents not yet settled: re-polling the whole corpus
        # costs a fixed sweep at 1 req/sec even for a single straggler.
        coverage.update(indexing_coverage(client, settings, outstanding))
        still_pending = pending(coverage)
        log.info(
            "indexed %d/%d; pending: %s",
            len(coverage) - len(still_pending),
            len(coverage),
            ", ".join(sorted(still_pending)) or "none",
        )
        if not still_pending:
            return coverage
        # Every call failing is a bad token, object type or datasource - not
        # latency. Polling the full timeout would report it as "not yet indexed".
        if all(coverage[doc_id].startswith("ERROR") for doc_id in still_pending):
            log.error("every status call failed; stopping rather than polling out")
            return coverage
        if time.monotonic() >= deadline:
            return coverage
        outstanding = [
            doc_id for doc_id in doc_ids if settings.namespaced_doc_id(doc_id) in still_pending
        ]
        time.sleep(poll_seconds)


def wait_until_searchable(
    query_settings: Settings,
    probe_query: str,
    probe_top_k: int,
    client: Glean,
    *,
    timeout_seconds: int = 900,
    initial_delay: float = 10.0,
    max_delay: float = 60.0,
) -> dict:
    """Poll until a search against the datasource returns results, or time out.

    Capped exponential backoff from 10s, because indexing routinely takes
    several minutes. The query-scoped Settings is passed in so a bad client
    token fails before the caller has polled for its full timeout.
    """
    deadline = time.monotonic() + timeout_seconds
    delay = initial_delay
    attempt = 0

    while True:
        attempt += 1
        passages, _ = search(query_settings, probe_query, top_k=probe_top_k, client=client)
        log.info("verify attempt %d: %d result(s) for %r", attempt, len(passages), probe_query)
        if passages or time.monotonic() >= deadline:
            return {
                "searchable": bool(passages),
                "attempts": attempt,
                "titles": [p.title for p in passages],
            }
        log.info("not searchable yet, sleeping %.0fs", delay)
        time.sleep(delay)
        delay = min(delay * 2, max_delay)
