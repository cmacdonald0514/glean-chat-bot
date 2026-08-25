"""The invariants the design rests on. One test each, no more.

These are the properties that make this server defensible rather than merely
working: the two paths cannot share a token, generation cannot happen without
retrieval, a hallucinated citation cannot hide, and a failure still reaches the
caller as something it can read. Retrieval *quality* is not tested here -- that
is the live eval suite.
"""

import asyncio
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from glean.api_client import models
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.testclient import TestClient

import glean_chat_bot.__main__ as server
from glean_chat_bot import indexd, indexing
from glean_chat_bot.client import indexing_client, query_client
from glean_chat_bot.models import ACTIVE_STATUS, STATUS_PROPERTY, Answer, Source
from glean_chat_bot.query import ask as ask_module
from glean_chat_bot.query.ask import MAX_TOP_K, MIN_TOP_K, ask
from glean_chat_bot.query.chat import resolve_citations
from glean_chat_bot.query.search import active_only_filter, passes_floor, search, term_overlap
from glean_chat_bot.utils.config import ConfigError, Settings
from tests.helpers import (
    ENVELOPE_KEYS,
    QUERY_ENV,
    TEST_CLIENT_TOKEN,
    make_passages,
    make_settings,
)

# --- the two paths cannot share a token --------------------------------------


def test_the_query_path_needs_no_indexing_token(clean_env):
    """The query-only MCP host runs with GLEAN_INDEXING_TOKEN absent entirely."""
    settings = Settings.for_query()

    assert settings.client_token == TEST_CLIENT_TOKEN
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


# --- retrieval is scoped to active documents ---------------------------------


def test_the_status_filter_names_the_property_the_indexing_path_declares():
    """Drift guard. Glean answers an unrecognised facet name with zero results
    rather than an error, so a rename on one path would surface as an empty
    corpus rather than as a failure."""
    declared = {name for name, _label, _attr, _facet in indexing.CUSTOM_PROPERTIES}
    assert STATUS_PROPERTY in declared

    (facet_filter,) = active_only_filter()
    # Lowercased: Glean exposes custom properties as facet fields in lower case.
    assert facet_filter.field_name == STATUS_PROPERTY.lower()

    (value,) = facet_filter.values
    assert value.value == ACTIVE_STATUS
    # EQUALS Active, never NOT_EQUALS Archived: the negated form matched the
    # archived document against the live instance, the exact inverse of intent.
    assert value.relation_type == models.RelationType.EQUALS


def test_search_scopes_every_query_to_the_datasource_and_to_active_documents(query_settings):
    """The superseded per diem is kept out of Chat's context by retrieval, not
    by trusting Chat to disregard it."""
    sent = {}

    class FakeSearch:
        def query(self, **kwargs):
            sent.update(kwargs)
            return SimpleNamespace(results=[], request_id="r", backend_time_millis=1)

    fake_client = SimpleNamespace(client=SimpleNamespace(search=FakeSearch()))

    _passages, diagnostics = search(query_settings, "meal per diem", client=fake_client)

    options = sent["request_options"]
    assert options.datasources_filter == [query_settings.datasource]
    assert options.facet_filters == active_only_filter()
    # And the caller is told, so zero results is not misread as an empty corpus.
    assert diagnostics["status_filter"] == ACTIVE_STATUS


# --- retrieval happens before generation -------------------------------------


@pytest.fixture
def fake_client(monkeypatch):
    """Fake only the client, so ask() opens and closes something inert.

    Separate from `fake_glean` because the floor test supplies its own search
    and generate: it needs the client faked and nothing else.
    """
    calls = {}

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
    fake_client["generate"] = []

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
    assert query_settings.datasource in answer.answer


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

    assert set(payload) == ENVELOPE_KEYS
    assert payload["diagnostics"]["error"] == expected_kind
    assert payload["diagnostics"]["chat_called"] is False
    assert "do not substitute your own knowledge" in payload["answer"].lower()


def test_the_http_app_mounts_the_protocol_and_the_healthcheck():
    """Cheap guard on the route table: compose polls /healthz, clients POST /mcp."""
    routes = {r.path for r in server.mcp.streamable_http_app().routes}

    assert {server.MCP_PATH, server.HEALTH_PATH} <= routes


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_serving(proc: subprocess.Popen, url: str, timeout: float = 15.0) -> None:
    """Poll until the server answers, failing fast if it exited instead."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"server exited {proc.returncode}: {proc.stderr.read().decode()}")
        # URLError, TimeoutError and ConnectionError are all OSError.
        with contextlib.suppress(OSError):
            urllib.request.urlopen(url, timeout=1).read()
            return
        time.sleep(0.1)
    pytest.fail(f"server never answered {url} within {timeout:.0f}s")


def test_the_server_starts_over_http_and_serves_its_tool():
    """The only test that catches "the server does not start". No Glean calls.

    env is QUERY_ENV over a stripped os.environ, so the real GLEAN_* values are
    not picked up and load_dotenv(override=False) leaves these in place if a
    .env is present.
    """
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GLEAN_", "MCP_"))}
    env.update(QUERY_ENV)

    proc = subprocess.Popen(
        [sys.executable, "-m", "glean_chat_bot", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stderr=subprocess.PIPE,
    )

    async def handshake():
        url = f"http://127.0.0.1:{port}{server.MCP_PATH}"
        async with (
            streamable_http_client(url) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return await session.list_tools()

    try:
        _wait_until_serving(proc, f"http://127.0.0.1:{port}{server.HEALTH_PATH}")
        tools = asyncio.run(handshake())
    finally:
        proc.terminate()
        proc.wait(timeout=10)

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

    assert set(payload) == ENVELOPE_KEYS


# --- indexing is a separate process, and only one run at a time --------------


def test_a_second_indexing_run_is_rejected_while_the_first_is_in_flight(monkeypatch):
    """The reason the service holds a lock at all.

    The second caller is refused rather than queued, because its result would be
    the first one's -- and a bulk upload replaces the datasource contents as a
    unit, so the two would race over what ends up in it.
    """
    started, release, calls = threading.Event(), threading.Event(), []

    def fake_index_once(settings, *, process_now=False):
        calls.append(process_now)
        started.set()
        release.wait(timeout=5)
        return {"documents": 1, "upload_id": "upload-1"}

    monkeypatch.setattr(indexd, "index_once", fake_index_once)

    first: list = []
    with TestClient(indexd.app) as client:
        runner = threading.Thread(target=lambda: first.append(client.post(indexd.INDEX_PATH)))
        runner.start()
        try:
            assert started.wait(timeout=5), "the first run never entered index_once"
            rejected = TestClient(indexd.app).post(indexd.INDEX_PATH)
        finally:
            release.set()
            runner.join(timeout=5)

    assert rejected.status_code == 409
    assert "already in progress" in rejected.json()["error"]
    assert first[0].status_code == 200
    assert calls == [False], "the rejected request must not have started a second run"


def test_a_failed_indexing_run_reaches_the_caller_as_a_5xx_carrying_the_reason(monkeypatch):
    """Same reasoning as the MCP tool's error envelope, one protocol down.

    Cron learns nothing from a process that swallowed the error, so the detail
    goes in the body and the status is 5xx -- which is what `glean-index-trigger`
    turns into a non-zero exit.
    """

    def boom(settings, *, process_now=False):
        raise RuntimeError("glean said no")

    monkeypatch.setattr(indexd, "index_once", boom)
    with TestClient(indexd.app, raise_server_exceptions=False) as client:
        response = client.post(indexd.INDEX_PATH)

    assert response.status_code == 500
    assert "glean said no" in response.json()["error"]
    # And the lock is not left held, or every later run answers a false 409.
    assert indexd._RUNNING.acquire(blocking=False), "a failed run leaked the lock"
    indexd._RUNNING.release()


def test_the_indexing_service_serves_the_run_over_http_and_nothing_else(monkeypatch):
    monkeypatch.setattr(
        indexd,
        "index_once",
        lambda settings, *, process_now=False: {"documents": 3, "asked_to_process": process_now},
    )
    with TestClient(indexd.app) as client:
        assert client.get(indexd.HEALTH_PATH).text == "ok"
        # POST-only: this endpoint rewrites the datasource.
        assert client.get(indexd.INDEX_PATH).status_code == 405
        response = client.post(f"{indexd.INDEX_PATH}?process_now=true")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "documents": 3, "asked_to_process": True}


def test_the_trigger_cli_exits_non_zero_when_the_run_did_not_happen(monkeypatch, capsys):
    """The cron sidecar's only channel: supercronic logs the job's exit status.

    A trigger that exited 0 on a 409 or a 500 would turn a datasource that has
    silently stopped updating into a schedule that looks like it is working.
    """

    def refuse(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 409, "Conflict", {}, io.BytesIO(b'{"error": "already in progress"}')
        )

    monkeypatch.setattr(urllib.request, "urlopen", refuse)

    assert indexd.trigger(["--url", "http://indexer:8001/index"]) == 1
    assert "already in progress" in capsys.readouterr().err
