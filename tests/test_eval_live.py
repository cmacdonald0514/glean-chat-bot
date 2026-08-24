"""The eval set, run against the real Glean instance. One test per question.

Marker-gated: `pytest` skips these, `pytest -m live` runs them. Needs
GLEAN_CLIENT_TOKEN and a corpus that has finished indexing.

Calls the MCP tool function itself rather than ask(), so the tool layer is in
the path. Answers are LLM-generated, so assertions are on document ids, figures
and structure -- never on phrasing.
"""

import re

import pytest

import glean_chat_bot.__main__ as server
from glean_chat_bot.utils.config import Settings
from tests.eval_cases import CASES
from tests.helpers import (
    assert_answer_envelope,
    env_has_live_credentials,
    short_ids,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not env_has_live_credentials(),
        reason="GLEAN_CLIENT_TOKEN is not set; the live eval set needs real credentials",
    ),
]


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
            # than failing: that is the signal the gap is fixed and the
            # known_gap entry should come out of eval_cases.py.
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
        # The grounded refusal: the floor rejects and Chat never runs. This is
        # the path that proves answers come from the corpus, not the model.
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
        f"did not clear the floor (overlap {diagnostics['term_overlap']}, "
        f"{diagnostics['results_returned']} result(s))"
    )
    assert diagnostics["chat_called"] is True
    assert payload["sources"], "a grounded answer must cite something"

    cited = short_ids(s["doc_id"] for s in payload["sources"] if s["resolved"])
    retrieved = short_ids(diagnostics["retrieved_doc_ids"])

    assert set(case.expect_cited) <= cited, f"expected {case.expect_cited} cited, got {cited}"
    assert set(case.expect_retrieved) <= retrieved, (
        f"expected {case.expect_retrieved} retrieved, got {retrieved}"
    )
    # The archived document may come back from search; it must not be cited.
    leaked = set(case.forbid_cited) & cited
    assert not leaked, (
        f"cited superseded document(s) {sorted(leaked)}. Filtering on "
        f"`status: active` in search.py is the fix. {case.note}"
    )

    missing = [phrase for phrase in case.must_contain if normalize(phrase) not in answer]
    assert not missing, f"answer is missing {missing}: {payload['answer']!r}"

    present = [phrase for phrase in case.must_not_contain if normalize(phrase) in answer]
    assert not present, f"answer states {present}, the wrong value: {payload['answer']!r}"
