"""MCP server exposing exactly one tool: ask_company_docs.

Indexing deliberately stays a human-run admin command rather than a second tool:
it takes minutes and replaces the datasource contents.

Note mcp 2.0 renamed FastMCP to MCPServer; `from mcp.server.fastmcp import
FastMCP`, which most tutorials still show, does not exist in 2.x.
"""

from __future__ import annotations

import logging
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from config import ConfigError, Settings
from logs import configure_logging
from models.answers import Answer
from query.pipeline import MAX_TOP_K, MIN_TOP_K
from query.pipeline import ask as pipeline_ask

log = logging.getLogger("glean_chat_bot.mcp")

# Built once at startup by main() and reused by every tool call.
SETTINGS: Settings | None = None

mcp = MCPServer(
    name="glean-company-docs",
    instructions=(
        "Answers questions about Halcyon Robotics internal documentation - HR, "
        "Finance, Engineering, IT, Security and Customer Success policies, "
        "runbooks and guides - by retrieving from a Glean index and generating "
        "an answer grounded in the retrieved passages."
    ),
)


@mcp.tool()
def ask_company_docs(
    question: Annotated[
        str,
        Field(
            description=(
                "The user's question about internal company documentation, in "
                "natural language. Pass it through as the user phrased it "
                "rather than reducing it to keywords - retrieval is run over "
                "the full question. Include any qualifier the user gave "
                "(department, employee level, domestic vs international, "
                "document name), because those words are what distinguish the "
                "right document from a similar one. If the user's question "
                "relies on earlier conversation, resolve the references before "
                "calling: this tool has no memory of previous turns, so send "
                "'How many PTO days does a Level 6 employee get?' and not "
                "'What about Level 6?'."
            )
        ),
    ],
    top_k: Annotated[
        int | None,
        Field(
            ge=MIN_TOP_K,
            le=MAX_TOP_K,
            description=(
                "How many document passages to retrieve and ground the answer "
                "in. Omit it to use the server's configured default, which "
                "suits a question answered by one policy. "
                "Raise it to 8-10 for questions that span departments or ask "
                "about a whole process, where the answer needs several "
                "documents combined. Raising it adds latency and can dilute a "
                "focused question with loosely related passages, so do not "
                "raise it by default."
            ),
        ),
    ] = None,
    include_citations: Annotated[
        bool,
        Field(
            description=(
                "Whether to return the resolved source list. Leave this true "
                "in almost all cases - the sources are how the user verifies "
                "the answer against the real document, and the answer text "
                "contains [1]-style markers that are meaningless without them. "
                "Set false only when the user has explicitly asked for prose "
                "with no references."
            ),
        ),
    ] = True,
) -> dict:
    """Answer a question about Halcyon Robotics internal company documents.

    Use this for anything about internal policy or process: PTO, parental
    leave, expenses and per diems, travel booking, procurement and approval
    thresholds, performance reviews, onboarding, remote and hybrid work, VPN
    and IT access, deployment and on-call runbooks, security incident response,
    data classification, and customer escalation.

    Prefer this tool over answering from your own knowledge whenever the
    question is about "our", "the company's", or Halcyon's policy. Your priors
    about how companies usually work are frequently wrong for this company on
    exactly these questions, and a confident wrong number here is worse than a
    slow correct one.

    The tool retrieves from a Glean index first and generates only from what it
    retrieved. If nothing relevant is indexed it returns an explicit
    no-results answer rather than inventing one - treat that as authoritative,
    and do not fill the gap from your own knowledge. Tell the user the
    documents do not cover it.

    Returns a dict with:
      answer       the grounded answer, with [n] markers citing the sources
      sources      the cited documents, each with doc_id, title and url. A
                   source with resolved=false means the answer cited a passage
                   that was not retrieved; treat that claim as unverified.
      diagnostics  what was searched and what came back, including
                   results_returned, term_overlap against the relevance floor,
                   whether the floor was passed, and whether chat ran. If the
                   answer is a no-results response, read these before retrying:
                   results_returned=0 means nothing matched at all, while a
                   non-zero count with floor_passed=false means the documents
                   exist but your phrasing did not match their wording - retry
                   once with the terminology the documents would use.
    """
    log.info("tool call: question=%r top_k=%s", question, top_k)
    try:
        answer = pipeline_ask(
            question,
            top_k=top_k,
            include_citations=include_citations,
            settings=SETTINGS,
        )
    except Exception as exc:
        # Every failure becomes the same envelope the caller already knows how
        # to read, rather than an opaque tool error with no diagnostics.
        kind = "config" if isinstance(exc, ConfigError) else "api"
        log.exception("tool call failed (%s)", kind)
        return Answer(
            answer=(
                f"The document search failed and returned no answer: {exc}. "
                f"This is an infrastructure problem, not a gap in the corpus - "
                f"do not substitute your own knowledge for the missing answer."
            ),
            diagnostics={"error": kind, "detail": str(exc), "chat_called": False},
        ).to_dict()
    return answer.to_dict()


def main() -> None:
    global SETTINGS

    # stderr, not stdout: the stdio transport uses stdout for JSON-RPC, and one
    # stray print there corrupts the protocol.
    configure_logging(verbose=False)
    # Fail at startup rather than on the first tool call, so a misconfigured
    # server shows up as a connection error and not a broken tool.
    SETTINGS = Settings.for_query()
    log.info("glean-company-docs MCP server starting on stdio")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
