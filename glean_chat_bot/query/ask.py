import logging
import time

from glean_chat_bot.client import query_client
from glean_chat_bot.models import Answer
from glean_chat_bot.query.chat import generate
from glean_chat_bot.query.search import passes_floor, search
from glean_chat_bot.utils.config import Settings

log = logging.getLogger("glean_chat_bot.ask")

MIN_TOP_K = 1
MAX_TOP_K = 20

FLOOR_REASONS = {
    "below_result_floor": (
        "Glean's search ranked nothing in datasource '{datasource}' as relevant "
        "enough to return for this question ({n} result(s), floor {floor}). "
        "Glean returns no results at all for a question its index does not "
        "cover, so this is its relevance judgement, not a threshold of ours."
    ),
    "no_passage_text": (
        "Search returned {n} result(s) from datasource '{datasource}', but Glean "
        "returned no readable text for them. That is a content or permissions "
        "problem rather than a relevance one, so lowering the result floor "
        "will not help."
    ),
}


def _no_results_answer(settings: Settings, diagnostics: dict) -> Answer:
    """The grounded refusal. Chat is never called on this path."""
    reason = FLOOR_REASONS[diagnostics["floor_reason"]].format(
        datasource=settings.datasource,
        n=diagnostics["results_returned"],
        floor=settings.min_results,
    )
    return Answer(
        answer=(
            f"No indexed content found on that. {reason} "
            f"I did not generate an answer, because anything I produced would "
            f"come from general knowledge rather than from Halcyon's documents."
        ),
        sources=[],
        diagnostics=diagnostics,
    )


def ask(
    question: str,
    top_k: int | None = None,
    include_citations: bool = True,
    settings: Settings | None = None,
) -> Answer:
    """Answer one question from indexed Halcyon documents."""
    settings = settings or Settings.for_query()
    if top_k is not None and not MIN_TOP_K <= top_k <= MAX_TOP_K:
        raise ValueError(f"top_k must be between {MIN_TOP_K} and {MAX_TOP_K}, got {top_k}")

    started = time.perf_counter()

    # One client for the whole question, so chat reuses the connection search
    # opened, and `with` closes it rather than leaving the SDK's reference cycle
    # to a GC pass in a long-lived server.
    with query_client(settings) as client:
        passages, search_diagnostics = search(settings, question, top_k=top_k, client=client)

        floor_passed, floor_reason = passes_floor(passages, settings.min_results)
        diagnostics = {
            **search_diagnostics,
            "floor_passed": floor_passed,
            "floor_reason": floor_reason,
            "chat_called": False,
            "include_citations": include_citations,
        }

        if not floor_passed:
            log.info("floor not cleared for %r (%s); skipping chat", question, floor_reason)
            diagnostics["total_ms"] = round((time.perf_counter() - started) * 1000)
            return _no_results_answer(settings, diagnostics)

        answer_text, sources, chat_diagnostics = generate(
            settings, question, passages, client=client
        )

    diagnostics.update(chat_diagnostics)
    diagnostics["chat_called"] = True
    diagnostics["total_ms"] = round((time.perf_counter() - started) * 1000)

    # include_citations=False suppresses the resolved source list, not the [n]
    # markers: stripping those would leave sourced claims looking unsourced.
    return Answer(
        answer=answer_text,
        sources=sources if include_citations else [],
        diagnostics=diagnostics,
    )
