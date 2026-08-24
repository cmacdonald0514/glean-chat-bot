"""Numbered passages in, grounded answer plus resolved citations out."""

import logging
import re

from glean.api_client import Glean, models

from glean_chat_bot.client import act_as_headers
from glean_chat_bot.models import Passage, Source
from glean_chat_bot.utils.config import Settings
from glean_chat_bot.utils.logging import log_call

log = logging.getLogger("glean_chat_bot.chat")

CITATION_PATTERN = re.compile(r"\[(\d+)\]")

SYSTEM_PROMPT = """\
You answer questions about Halcyon Robotics internal documents.

Answer using ONLY the numbered passages provided in the user message. Do not use
prior knowledge, and do not infer facts that the passages do not state.

Cite your sources inline using the passage numbers in square brackets, like [1]
or [2][3]. Cite every factual claim. Use only numbers that appear in the
passages given to you.

If the passages do not contain enough information to answer, say exactly what is
missing instead of guessing. It is correct and useful to say you cannot answer.

Be concise. Lead with the direct answer, then any necessary qualification."""


def build_context_message(passages: list[Passage]) -> str:
    blocks = [
        f"[{passage.marker}] {passage.title} (document {passage.doc_id})\n{passage.text}"
        for passage in passages
    ]
    return f"{SYSTEM_PROMPT}\n\nPassages:\n\n" + "\n\n".join(blocks)


def resolve_citations(answer_text: str, passages: list[Passage]) -> list[Source]:
    """Map every [n] in the answer back to a retrieved passage.

    A marker with no matching passage comes back resolved=False rather than
    being dropped: silently dropping it would hide a hallucinated citation.
    """
    by_marker = {p.marker: p for p in passages}
    sources: list[Source] = []

    # dict.fromkeys de-duplicates while preserving first-mention order.
    for raw in dict.fromkeys(CITATION_PATTERN.findall(answer_text)):
        marker = int(raw)
        passage = by_marker.get(marker)
        if passage is None:
            sources.append(Source(marker=marker, resolved=False))
            continue
        sources.append(
            Source(
                marker=marker,
                resolved=True,
                doc_id=passage.doc_id,
                title=passage.title,
                url=passage.url,
            )
        )
    return sources


def _extract_reply(response: models.ChatResponse) -> tuple[str, list[dict], list[str]]:
    """Concatenate the assistant's text fragments and collect any inline citations."""
    content_parts: list[str] = []
    other_parts: list[str] = []
    inline_citations: list[dict] = []
    errors: list[str] = []

    for message in response.messages or []:
        if message.author == models.Author.USER:
            continue
        if message.message_type == models.MessageType.ERROR:
            errors.extend(f.text for f in message.fragments or [] if f.text)
            continue

        # CONTENT (or unset) is the answer. UPDATE and CONTROL_* are progress
        # and stream framing, kept only as a fallback so a relabelled reply
        # never comes back as an empty answer.
        target = (
            content_parts
            if message.message_type in (None, models.MessageType.CONTENT)
            else other_parts
        )
        for fragment in message.fragments or []:
            if fragment.text:
                target.append(fragment.text)
            if fragment.citation is not None:
                source_doc = fragment.citation.source_document
                inline_citations.append(
                    {
                        "doc_id": source_doc.id if source_doc else None,
                        "title": source_doc.title if source_doc else None,
                        "url": source_doc.url if source_doc else None,
                    }
                )

    answer_text = "".join(content_parts).strip()
    if not answer_text and other_parts:
        answer_text = "".join(other_parts).strip()
        log.warning(
            "no CONTENT message in chat response; fell back to %d other fragment(s). "
            "Check what message_type Glean returned.",
            len(other_parts),
        )
    return answer_text, inline_citations, errors


def generate(
    settings: Settings,
    question: str,
    passages: list[Passage],
    client: Glean,
) -> tuple[str, list[Source], dict]:
    """Call Chat with the retrieved passages. Returns (text, sources, diagnostics)."""
    context_message = build_context_message(passages)

    with log_call(
        "client.chat.create", passages=len(passages), prompt_chars=len(context_message)
    ) as rec:
        response = client.client.chat.create(
            http_headers=act_as_headers(settings),
            # Instructions and passages go in a CONTEXT message, the question in
            # a CONTENT one, so the question is not buried under thousands of
            # characters of passage text.
            messages=[
                models.ChatMessage(
                    author=models.Author.USER,
                    fragments=[models.ChatMessageFragment(text=context_message)],
                    message_type=models.MessageType.CONTEXT,
                ),
                models.ChatMessage(
                    author=models.Author.USER,
                    fragments=[models.ChatMessageFragment(text=question)],
                    message_type=models.MessageType.CONTENT,
                ),
            ],
            agent_config=models.AgentConfig(
                # GPT talks straight to the model, and both tool sets off, is
                # what disables Glean's own retrieval. Chat therefore has
                # nothing of its own to cite, so the citations we resolve are
                # the [n] markers against passages we numbered ourselves.
                agent=models.AgentEnum.GPT,
                tool_sets=models.ToolSets(
                    enable_web_search=False,
                    enable_company_tools=False,
                ),
            ),
            save_chat=False,
            # Server-side budget, so Glean returns a 408 rather than holding the
            # connection open...
            timeout_millis=settings.chat_timeout_ms,
            # ...and a slightly longer client read timeout, so that 408 arrives
            # instead of the socket dying first.
            timeout_ms=settings.chat_timeout_ms + 5000,
        )
        answer_text, inline_citations, errors = _extract_reply(response)
        rec["answer_chars"] = len(answer_text)
        rec["inline_citations"] = len(inline_citations)

    sources = resolve_citations(answer_text, passages)
    unresolved = [source.marker for source in sources if not source.resolved]
    diagnostics = {
        "chat_backend_time_millis": response.backend_time_millis,
        "passages_sent": len(passages),
        "prompt_chars": len(context_message),
        "chat_errors": errors,
        "citations_resolved": [source.marker for source in sources if source.resolved],
        "citations_unresolved": unresolved,
        # Expected to be 0 while Chat retrieval is disabled; non-zero means
        # Glean started citing on its own and this code needs revisiting.
        "glean_inline_citations": inline_citations,
    }
    if unresolved:
        log.warning(
            "answer cited %s but only %d passage(s) were supplied", unresolved, len(passages)
        )
    return answer_text, sources, diagnostics
