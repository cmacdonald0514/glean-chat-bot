"""Search -> Passage objects, plus the relevance floor.

Glean's Search API returns no relevance score, so the floor is computed here as
term overlap: the fraction of the question's content words present in the
retrieved passages. It is lexical, so a pure paraphrase scores lower than it
deserves - which is why the threshold is an environment variable.
"""

from __future__ import annotations

import logging
import re

from glean.api_client import Glean, models

from client import act_as_headers
from config import Settings
from logs import log_call
from models.documents import Passage

log = logging.getLogger("glean_chat_bot.retrieval")

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
    }
    log.info(
        "retrieved %d passage(s), term_overlap=%.2f (floor %.2f)",
        len(passages),
        overlap,
        settings.min_term_overlap,
    )
    return passages, diagnostics
