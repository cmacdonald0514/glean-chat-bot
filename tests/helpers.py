"""Plain helpers shared across the suite.

The split with `conftest.py` is by kind, not by topic: fixtures live there,
importable functions live here. `Settings` is a frozen dataclass, so the
builders below construct it directly rather than routing through the
environment -- only the config test goes through `Settings.for_query()`,
because that test is specifically about env loading.
"""

import re

from glean_chat_bot.extraction import DOC_ID_PATTERN
from glean_chat_bot.models import Answer, Passage
from glean_chat_bot.query.chat import CITATION_PATTERN
from glean_chat_bot.utils.config import ConfigError, Settings

# The fake identity the offline suite runs under. Named because the fixtures,
# the server subprocess env and several assertions all have to agree on it.
TEST_INSTANCE = "test-instance"
TEST_DATASOURCE = "testds"
TEST_CLIENT_TOKEN = "test-client-token"

# The minimum the read path needs; conftest sets it, the startup test passes it
# to the subprocess.
QUERY_ENV = {
    "GLEAN_INSTANCE": TEST_INSTANCE,
    "GLEAN_DATASOURCE": TEST_DATASOURCE,
    "GLEAN_CLIENT_TOKEN": TEST_CLIENT_TOKEN,
}

# The envelope ask() returns. Read off the model rather than transcribed, so a
# field added to Answer cannot leave these assertions quietly checking the old
# shape.
ENVELOPE_KEYS = frozenset(Answer.model_fields)


def make_settings(**overrides) -> Settings:
    """A query-scoped Settings with no environment involved."""
    base = {
        "instance": TEST_INSTANCE,
        "datasource": TEST_DATASOURCE,
        "doc_id_prefix": "halcyon",
        "default_top_k": 5,
        "max_snippet_size": 2000,
        "min_term_overlap": 0.30,
        "act_as": "",
        "chat_timeout_ms": 60000,
        "client_token": TEST_CLIENT_TOKEN,
    }
    return Settings(**{**base, **overrides})


def make_passages(*texts: str) -> list[Passage]:
    """Passages numbered from 1, matching what search() produces."""
    return [
        Passage(
            marker=index + 1,
            doc_id=f"CUSTOM_TESTDS_Document_halcyon-DOC-{index + 1:03d}",
            title=f"Document {index + 1}",
            url=f"https://drive.example.com/doc-{index + 1}",
            text=text,
        )
        for index, text in enumerate(texts)
    ]


def env_has_live_credentials() -> bool:
    """Whether the read path can be constructed at all.

    Goes through `Settings.for_query()` rather than checking a variable name,
    because that constructor is the single source of what the read path needs:
    a missing GLEAN_INSTANCE should skip the live suite too, not blow up in
    fixture setup.
    """
    try:
        Settings.for_query()
    except ConfigError:
        return False
    return True


# Glean prefixes doc ids with the datasource and object type; the eval set uses
# the short form, so assertions compare on the trailing DEPT-NNN.
SHORT_DOC_ID = re.compile(DOC_ID_PATTERN + "$")


def short_ids(full_doc_ids) -> set[str]:
    """`CUSTOM_INTERVIEWDS3_Document_halcyon-FIN-011` -> `FIN-011`."""
    matches = (SHORT_DOC_ID.search(doc_id) for doc_id in full_doc_ids)
    return {match.group() for match in matches if match}


# --- the structural half of the eval contract, asserted on every case --------

# Keys ask() promises in every response, whether or not Chat ran. The tool
# docstring tells the calling model to read these, so a missing key is a broken
# contract rather than a cosmetic change.
BASE_KEYS = frozenset(
    {
        "query",
        "datasource_searched",
        "results_returned",
        "term_overlap",
        "min_term_overlap",
        "retrieved_doc_ids",
        "status_filter",
        "floor_passed",
        "floor_reason",
        "chat_called",
        "total_ms",
    }
)
CHAT_KEYS = frozenset({"passages_sent", "citations_unresolved", "glean_inline_citations"})


def assert_answer_envelope(payload: dict) -> None:
    assert set(payload) == ENVELOPE_KEYS
    assert payload["answer"].strip(), "answer must be non-empty text"

    diagnostics = payload["diagnostics"]
    missing = BASE_KEYS - set(diagnostics)
    assert not missing, f"diagnostics missing contract keys: {sorted(missing)}"

    for source in payload["sources"]:
        # An unresolved marker is a citation we could not tie to a passage. It
        # stays in the list precisely so it is visible, carrying no metadata.
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
    # Chat runs with both tool sets off, so Glean has nothing of its own to
    # cite. Non-empty means Glean started retrieving and chat.py needs revisiting.
    assert diagnostics["glean_inline_citations"] == []

    for marker in CITATION_PATTERN.findall(payload["answer"]):
        assert 1 <= int(marker) <= diagnostics["passages_sent"], (
            f"answer cites [{marker}] but only {diagnostics['passages_sent']} passage(s) were sent"
        )
