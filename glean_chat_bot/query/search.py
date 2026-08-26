import logging

from glean.api_client import Glean, models

from glean_chat_bot.client import act_as_headers
from glean_chat_bot.models import ACTIVE_STATUS, STATUS_PROPERTY, Passage
from glean_chat_bot.utils.config import Settings
from glean_chat_bot.utils.logging import log_call

log = logging.getLogger("glean_chat_bot.search")


def active_only_filter() -> list[models.FacetFilter]:
    """Restrict retrieval to documents whose status is Active."""
    return [
        models.FacetFilter(
            field_name=STATUS_PROPERTY.lower(),
            values=[
                models.FacetFilterValue(
                    value=ACTIVE_STATUS,
                    relation_type=models.RelationType.EQUALS,
                )
            ],
        )
    ]


def passes_floor(passages: list[Passage], min_results: int) -> tuple[bool, str]:
    """Decide whether these passages are worth sending to Chat, and say why not.

    The relevance judgement is Glean's, not ours. Glean's Search API returns no
    per-result score to threshold, because ranking is expressed in what comes
    back: a query the corpus does not cover returns zero results, and a query it
    does cover returns fewer than `page_size` rather than padding to fill it.
    Scoring the passages again here would only second-guess that ranking with a
    weaker signal, so the floor reads Glean's answer instead of recomputing one.
    """
    if len(passages) < min_results:
        return False, "below_result_floor"
    if not any(p.text.strip() for p in passages):
        # Matched documents with no usable text: Chat would answer from priors.
        return False, "no_passage_text"
    return True, "passed"


def _passage_text(result: models.SearchResult) -> str:
    if result.snippets:
        parts = [s.text or s.snippet or "" for s in result.snippets]
        joined = "\n".join(p for p in parts if p.strip())
        if joined.strip():
            return joined.strip()
    return (result.full_text or "").strip()


def search(
    settings: Settings,
    question: str,
    client: Glean,
    top_k: int | None = None,
) -> tuple[list[Passage], dict]:
    """Run one Search query restricted to our datasource. Returns passages and diagnostics."""
    # `is None` so an explicit top_k=0 is a caller error, not the default.
    k = settings.default_top_k if top_k is None else top_k

    with log_call(
        "client.search.query",
        datasource=settings.datasource,
        q=question,
        top_k=k,
        act_as=settings.act_as or "(token identity)",
    ) as rec:
        response = client.client.search.query(
            http_headers=act_as_headers(settings),
            query=question,
            page_size=k,
            max_snippet_size=settings.max_snippet_size,
            request_options=models.SearchRequestOptions(
                # Required by the model even though nothing here reads facets.
                facet_bucket_size=10,
                datasources_filter=[settings.datasource],
                facet_filters=active_only_filter(),
                # Makes "snippets" RAG-sized chunks rather than ~200 chars of
                # keyword context, which is too little for Chat to answer from.
                return_llm_content_over_snippets=True,
            ),
        )
        results = response.results or []
        rec["results"] = len(results)
        rec["request_id"] = response.request_id

    passages = []
    for index, result in enumerate(results):
        document = result.document
        passages.append(
            Passage(
                marker=index + 1,
                doc_id=(document.id if document else "") or "",
                title=result.title or (document.title if document else "") or "(untitled)",
                url=result.url,
                text=_passage_text(result),
            )
        )

    diagnostics = {
        "query": question,
        "datasource_searched": settings.datasource,
        "instance": settings.instance,
        "top_k": k,
        "results_returned": len(passages),
        "request_id": response.request_id,
        "backend_time_millis": response.backend_time_millis,
        "min_results": settings.min_results,
        # Glean truncates at its own relevance boundary rather than padding to
        # page_size, so a count below top_k is a ranking signal, not a shortfall.
        "results_truncated_by_glean": len(passages) < k,
        "retrieved_doc_ids": [p.doc_id for p in passages],
        # Reported so a caller seeing zero results knows retrieval was scoped to
        # active documents before concluding the corpus does not cover it.
        "status_filter": ACTIVE_STATUS,
    }
    log.info(
        "retrieved %d passage(s) of %d requested (floor %d)",
        len(passages),
        k,
        settings.min_results,
    )
    return passages, diagnostics
