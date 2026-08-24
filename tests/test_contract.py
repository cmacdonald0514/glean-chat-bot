"""The invariants the design rests on. One test each, no more.

These are the properties that make this server defensible rather than merely
working: the two paths cannot share a token, generation cannot happen without
retrieval, a hallucinated citation cannot hide, and a failure still reaches the
caller as something it can read. Retrieval *quality* is not tested here -- that
is the live eval suite.
"""

import asyncio
import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import glean_chat_bot.__main__ as server
from glean_chat_bot.client import indexing_client, query_client
from glean_chat_bot.models import Answer, Source
from glean_chat_bot.query import ask as ask_module
from glean_chat_bot.query.ask import MAX_TOP_K, MIN_TOP_K, ask
from glean_chat_bot.query.chat import resolve_citations
from glean_chat_bot.query.search import passes_floor, term_overlap
from glean_chat_bot.utils.config import ConfigError, Settings
from tests.helpers import make_passages, make_settings

# --- the two paths cannot share a token --------------------------------------


def test_the_query_path_needs_no_indexing_token(clean_env):
    """The query-only MCP host runs with GLEAN_INDEXING_TOKEN absent entirely."""
    settings = Settings.for_query()

    assert settings.client_token == "test-client-token"
    assert settings.indexing_token is None
    assert settings.docs_root is None


def test_each_client_refuses_the_other_path_s_settings(query_settings):
    indexing_settings = make_settings(client_token=None, indexing_token="test-indexing-token")

    with pytest.raises(ConfigError):
        query_client(indexing_settings)
    with pytest.raises(ConfigError):
        indexing_client(query_settings)


# --- the relevance floor -----------------------------------------------------


def test_term_overlap_is_the_fraction_of_question_terms_found():
    """The floor is an overlap fraction, not a score: Glean's scores are not
    comparable across queries."""
    passages = make_passages("The deploy freeze runs from December 15.")

    assert term_overlap("deploy freeze dental insurance", passages) == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("passage_texts", "overlap", "expected"),
    [
        ((), 0.0, (False, "no_results")),
        # Matched documents with no usable text: sending them is sending nothing.
        (("   ",), 1.0, (False, "no_passage_text")),
        (("real text",), 0.29, (False, "below_overlap_floor")),
        # The check is `<`, so equality clears the floor.
        (("real text",), 0.30, (True, "passed")),
    ],
)
def test_the_floor_gate_truth_table(passage_texts, overlap, expected):
    assert passes_floor(make_passages(*passage_texts), overlap, 0.30) == expected


# --- retrieval happens before generation -------------------------------------


@pytest.fixture
def fake_client(monkeypatch):
    """Fake only the client, so ask() opens and closes something inert.

    Separate from `fake_glean` because the floor test supplies its own search
    and generate: it needs the client faked and nothing else.
    """
    calls = {"generate": []}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            calls["closed"] = True
            return False

    monkeypatch.setattr(ask_module, "query_client", lambda settings: FakeClient())
    return calls


@pytest.fixture
def fake_glean(monkeypatch, fake_client):
    """Fake the whole Glean boundary. Returns the call recorder."""

    def fake_search(settings, question, client, top_k=None):
        return make_passages("18 PTO days per year.", "23 days at Level 6."), {
            "query": question,
            "results_returned": 2,
            "term_overlap": 0.9,
            "retrieved_doc_ids": ["doc-a", "doc-b"],
        }

    def fake_generate(settings, question, passages, client):
        fake_client["generate"].append(question)
        return (
            "Full-time employees get 18 days [1].",
            [Source(marker=1, resolved=True, doc_id="doc-a", title="A", url="https://a")],
            {"passages_sent": 2, "citations_unresolved": []},
        )

    monkeypatch.setattr(ask_module, "search", fake_search)
    monkeypatch.setattr(ask_module, "generate", fake_generate)
    return fake_client


def test_generate_is_never_called_below_the_floor(monkeypatch, fake_client, query_settings):
    """The whole grounding guarantee. If this breaks, Chat answers from priors."""

    def fail_if_called(*args, **kwargs):
        raise AssertionError("generate() ran on a question that did not clear the floor")

    monkeypatch.setattr(
        ask_module,
        "search",
        lambda *a, **kw: ([], {"results_returned": 0, "term_overlap": 0.0}),
    )
    monkeypatch.setattr(ask_module, "generate", fail_if_called)

    answer = ask("What is our 401k match?", settings=query_settings)

    assert answer.diagnostics["chat_called"] is False
    assert answer.sources == []
    assert "No indexed content found" in answer.answer
    # The refusal names what was searched, so the caller can tell a retrieval
    # miss from an empty index.
    assert "testds" in answer.answer


def test_clearing_the_floor_runs_chat_and_merges_both_diagnostics(fake_glean, query_settings):
    answer = ask("How many PTO days do I get?", settings=query_settings)

    assert fake_glean["generate"] == ["How many PTO days do I get?"]
    assert answer.diagnostics["chat_called"] is True
    assert answer.diagnostics["retrieved_doc_ids"] == ["doc-a", "doc-b"]  # from search
    assert answer.diagnostics["passages_sent"] == 2  # from chat
    assert fake_glean["closed"] is True


def test_suppressing_citations_drops_the_sources_but_keeps_the_markers(fake_glean, query_settings):
    """Stripping the [n] markers too would leave sourced claims looking unsourced."""
    answer = ask("a question", include_citations=False, settings=query_settings)

    assert answer.sources == []
    assert "[1]" in answer.answer


# --- a hallucinated citation cannot hide -------------------------------------


def test_a_citation_with_no_passage_behind_it_is_kept_not_dropped():
    sources = resolve_citations("Something [1] and something else [7].", make_passages("one"))

    assert [(s.marker, s.resolved) for s in sources] == [(1, True), (7, False)]
    assert sources[1].doc_id is None


# --- the MCP contract --------------------------------------------------------


@pytest.fixture(scope="module")
def tool():
    tools = asyncio.run(server.mcp.list_tools())
    assert [t.name for t in tools] == ["ask_company_docs"]
    return tools[0]


def test_the_advertised_top_k_bounds_match_the_enforced_ones(tool):
    """Drift guard: ask() rejects out-of-range top_k, the schema must say so too."""
    schema = tool.input_schema["properties"]["top_k"]
    bounds = next(b for b in schema["anyOf"] if b.get("type") == "integer")

    assert (bounds["minimum"], bounds["maximum"]) == (MIN_TOP_K, MAX_TOP_K)
    assert tool.input_schema["required"] == ["question"]


@pytest.mark.parametrize(
    ("exception", "expected_kind"),
    [(ConfigError("GLEAN_CLIENT_TOKEN is not set."), "config"), (RuntimeError("503"), "api")],
)
def test_failures_come_back_as_the_envelope_not_as_a_tool_error(
    monkeypatch, exception, expected_kind
):
    """An opaque tool error carries no diagnostics for the caller to act on."""

    def boom(*args, **kwargs):
        raise exception

    monkeypatch.setattr(server, "ask", boom)

    payload = server.ask_company_docs("How many PTO days?")

    assert set(payload) == {"answer", "sources", "diagnostics"}
    assert payload["diagnostics"]["error"] == expected_kind
    assert payload["diagnostics"]["chat_called"] is False
    assert "do not substitute your own knowledge" in payload["answer"].lower()


def test_the_server_starts_on_stdio_and_serves_its_tool():
    """The only test that catches "the server does not start". No Glean calls.

    env passed here is merged over the SDK's safe-inherit set, so the real
    GLEAN_* values are not picked up, and load_dotenv(override=False) leaves
    these in place if a .env is present.
    """
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "glean_chat_bot"],
        env={
            "GLEAN_INSTANCE": "test-instance",
            "GLEAN_DATASOURCE": "testds",
            "GLEAN_CLIENT_TOKEN": "test-client-token",
        },
    )

    async def handshake():
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return await session.list_tools()

    tools = asyncio.run(handshake())

    assert [t.name for t in tools.tools] == ["ask_company_docs"]


def test_the_envelope_reaches_the_client_as_json(monkeypatch):
    """The tool is annotated `-> dict`, so there is no output schema and the
    envelope arrives as JSON text rather than structured content."""
    monkeypatch.setattr(
        server,
        "ask",
        lambda *a, **kw: Answer(answer="grounded [1].", diagnostics={"chat_called": True}),
    )

    result = asyncio.run(server.mcp.call_tool("ask_company_docs", {"question": "q"}))
    payload = json.loads(result.content[0].text)

    assert set(payload) == {"answer", "sources", "diagnostics"}
