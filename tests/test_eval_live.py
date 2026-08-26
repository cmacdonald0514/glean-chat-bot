"""The eval set, run against the real Glean instance. One test per question.

Marker-gated: `pytest` skips these, `pytest -m live` runs them. Needs
GLEAN_CLIENT_TOKEN and a corpus that has finished indexing.

Calls the MCP tool function itself rather than ask(), so the tool layer is in
the path. Answers are LLM-generated, so assertions are on document ids, figures
and structure - never on phrasing.
"""

import re

import pytest

import glean_chat_bot.__main__ as server
from glean_chat_bot.indexing.extraction import DOC_ID_PATTERN
from glean_chat_bot.query.chat import CITATION_PATTERN
from glean_chat_bot.utils.config import ConfigError, Settings
from tests.conftest import ENVELOPE_KEYS
from tests.eval_cases import CASES


def env_has_live_credentials() -> bool:
    """Goes through the constructor rather than checking a variable name, so a
    missing GLEAN_INSTANCE skips the suite too instead of failing in setup."""
    try:
        Settings.for_query()
    except ConfigError:
        return False
    return True


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not env_has_live_credentials(),
        reason="GLEAN_CLIENT_TOKEN is not set; the live eval set needs real credentials",
    ),
]

# Glean prefixes doc ids with the datasource and object type; the eval set uses
# the short form, so assertions compare on the trailing DEPT-NNN.
SHORT_DOC_ID = re.compile(DOC_ID_PATTERN + "$")

# Keys ask() promises in every response, whether or not Chat ran. The tool
# docstring tells the calling model to read these, so a missing one is a broken
# contract rather than a cosmetic change.
BASE_KEYS = frozenset(
    {
        "query",
        "datasource_searched",
        "results_returned",
        "min_results",
        "results_truncated_by_glean",
        "retrieved_doc_ids",
        "status_filter",
        "floor_passed",
        "floor_reason",
        "chat_called",
        "total_ms",
    }
)
CHAT_KEYS = frozenset({"passages_sent", "citations_unresolved", "glean_inline_citations"})


def short_ids(full_doc_ids) -> set[str]:
    """`CUSTOM_INTERVIEWDS3_Document_halcyon-FIN-011` -> `FIN-011`."""
    matches = (SHORT_DOC_ID.search(doc_id) for doc_id in full_doc_ids)
    return {match.group() for match in matches if match}


def assert_answer_envelope(payload: dict) -> None:
    """The structural half of the eval contract, asserted on every case."""
    assert set(payload) == ENVELOPE_KEYS
    assert payload["answer"].strip(), "answer must be non-empty text"

    diagnostics = payload["diagnostics"]
    missing = BASE_KEYS - set(diagnostics)
    assert not missing, f"diagnostics missing contract keys: {sorted(missing)}"

    for source in payload["sources"]:
        # An unresolved marker stays in the list precisely so it is visible,
        # carrying no metadata.
        expected_present = source["resolved"]
        for field in ("doc_id", "title", "url"):
            assert bool(source[field]) is expected_present, (
                f"source [{source['marker']}] resolved={source['resolved']} but {field}="
                f"{source[field]!r}"
            )

    if not diagnostics["chat_called"]:
        return

    missing = CHAT_KEYS - set(diagnostics)
    assert not missing, f"diagnostics missing chat keys: {sorted(missing)}"
    assert diagnostics["citations_unresolved"] == [], (
        f"answer cited passages that were never retrieved: {diagnostics['citations_unresolved']}"
    )
    # Chat runs with both tool sets off, so Glean has nothing of its own to cite.
    assert diagnostics["glean_inline_citations"] == []

    for marker in CITATION_PATTERN.findall(payload["answer"]):
        assert 1 <= int(marker) <= diagnostics["passages_sent"], (
            f"answer cites [{marker}] but only {diagnostics['passages_sent']} passage(s) were sent"
        )


@pytest.fixture(scope="module")
def live_settings():
    """One Settings for the module, installed the way main() installs it."""
    settings = Settings.for_query()
    original = server.SETTINGS
    server.SETTINGS = settings
    yield settings
    server.SETTINGS = original


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).lower()


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            case,
            id=case.id,
            # strict=False, so a case that starts passing reports XPASS rather
            # than failing: that is the signal to drop its known_gap entry.
            marks=(
                [pytest.mark.xfail(reason=case.known_gap, strict=False)] if case.known_gap else []
            ),
        )
        for case in CASES
    ],
)
def test_eval_case(case, live_settings):
    payload = server.ask_company_docs(case.question, top_k=case.top_k)
    diagnostics = payload["diagnostics"]
    answer = normalize(payload["answer"])

    assert_answer_envelope(payload)

    if not case.grounded:
        # The grounded refusal: the floor rejects and Chat never runs.
        assert diagnostics["floor_passed"] is False, (
            f"cleared the floor on a question the corpus does not cover; "
            f"retrieved {diagnostics['retrieved_doc_ids']}"
        )
        assert diagnostics["chat_called"] is False
        assert payload["sources"] == []
        assert "no indexed content found" in answer
        assert live_settings.datasource in payload["answer"]
        return

    assert diagnostics["floor_passed"] is True, (
        f"did not clear the floor ({diagnostics['results_returned']} result(s), "
        f"floor {diagnostics['min_results']})"
    )
    assert diagnostics["chat_called"] is True
    assert payload["sources"], "a grounded answer must cite something"

    cited = short_ids(s["doc_id"] for s in payload["sources"] if s["resolved"])
    retrieved = short_ids(diagnostics["retrieved_doc_ids"])

    assert set(case.expect_cited) <= cited, f"expected {case.expect_cited} cited, got {cited}"
    assert set(case.expect_retrieved) <= retrieved, (
        f"expected {case.expect_retrieved} retrieved, got {retrieved}"
    )
    # Retirement is enforced at retrieval: a superseded document must never have
    # reached Chat at all, so there is nothing for Chat to have ignored.
    reached_chat = set(case.forbid_retrieved) & retrieved
    assert not reached_chat, (
        f"retrieved superseded document(s) {sorted(reached_chat)}: the active-only "
        f"facet filter in search.py is not matching. {case.note}"
    )
    missing = [phrase for phrase in case.must_contain if normalize(phrase) not in answer]
    assert not missing, f"answer is missing {missing}: {payload['answer']!r}"

    present = [phrase for phrase in case.must_not_contain if normalize(phrase) in answer]
    assert not present, f"answer states {present}, the wrong value: {payload['answer']!r}"
