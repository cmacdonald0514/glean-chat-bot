"""Search -> Passage objects, plus the relevance floor."""

import logging
import re

from glean.api_client import Glean, models

from glean_chat_bot.client import act_as_headers
from glean_chat_bot.models import ACTIVE_STATUS, STATUS_PROPERTY, Passage
from glean_chat_bot.utils.config import Settings
from glean_chat_bot.utils.logging import log_call

log = logging.getLogger("glean_chat_bot.search")


def active_only_filter() -> list[models.FacetFilter]:
    """Restrict retrieval to documents whose status is Active.

    EQUALS Active, never a negation: the negated form matched exactly the
    archived document against this instance, and `is_negated` is deprecated for
    removal in October 2026. CLAUDE.md has the full rationale.
    """
    return [
        models.FacetFilter(
            # Glean lowercases custom-property names when exposing them as facet
            # fields, so `halcyonStatus` is filtered on as `halcyonstatus`. The
            # camelCase name matches zero documents and does not error, which is
            # why it is derived here rather than retyped.
            field_name=STATUS_PROPERTY.lower(),
            values=[
                models.FacetFilterValue(
                    value=ACTIVE_STATUS,
                    relation_type=models.RelationType.EQUALS,
                )
            ],
        )
    ]


# Question words are included because "what/how/many" appear in nearly every
# question and would inflate every overlap score identically.
STOP_WORDS = frozenset(
    """
    a an and are as at be been but by can do does for from get give had has have
    how i if in into is it its many me much my of on or our ours should so than
    that the their there these they this to was we what when where which who
    why will with would you your
    """.split()
)


def content_words(text: str) -> list[str]:
    # \w+ keeps "401k" as one token; splitting it would let "401" match a dollar
    # figure and give a question with no answer a non-zero score.
    tokens = re.findall(r"\w+", text.lower())
    return [t for t in tokens if t not in STOP_WORDS and len(t) > 1]


def term_overlap(question: str, passages: list[Passage]) -> float:
    terms = set(content_words(question))
    if not terms or not passages:
        return 0.0
    corpus = " ".join(p.text.lower() for p in passages)
    return sum(1 for term in terms if term in corpus) / len(terms)


def passes_floor(passages: list[Passage], overlap: float, min_overlap: float) -> tuple[bool, str]:
    """Decide whether these passages are worth sending to Chat, and say why not.

    Takes the overlap search() already computed, so the number reported in
    diagnostics is provably the number this gate used.
    """
    if not passages:
        return False, "no_results"
    if not any(p.text.strip() for p in passages):
        # Matched documents with no usable text. Sending those to Chat is the
        # same as sending nothing, and Chat would answer from priors.
        return False, "no_passage_text"
    if overlap < min_overlap:
        return False, "below_overlap_floor"
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
                # Never retrieve outside the corpus we pushed.
                datasources_filter=[settings.datasource],
                # ...and never retrieve a superseded document from inside it.
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

    overlap = term_overlap(question, passages)
    diagnostics = {
        "query": question,
        "datasource_searched": settings.datasource,
        "instance": settings.instance,
        "top_k": k,
        "results_returned": len(passages),
        "request_id": response.request_id,
        "backend_time_millis": response.backend_time_millis,
        "term_overlap": round(overlap, 3),
        "min_term_overlap": settings.min_term_overlap,
        "retrieved_doc_ids": [p.doc_id for p in passages],
        # Part of the contract: a caller seeing zero results needs to know
        # retrieval was scoped to active documents before concluding the
        # corpus does not cover the question.
        "status_filter": ACTIVE_STATUS,
    }
    log.info(
        "retrieved %d passage(s), term_overlap=%.2f (floor %.2f)",
        len(passages),
        overlap,
        settings.min_term_overlap,
    )
    return passages, diagnostics
