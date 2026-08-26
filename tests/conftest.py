"""Shared fixtures and builders for the offline suite."""

import os

import pytest

from glean_chat_bot.models import Answer, Passage
from glean_chat_bot.utils.config import Settings

TEST_INSTANCE = "test-instance"
TEST_DATASOURCE = "testds"
TEST_CLIENT_TOKEN = "test-client-token"

# The minimum the read path needs. The startup test passes it to a subprocess.
QUERY_ENV = {
    "GLEAN_INSTANCE": TEST_INSTANCE,
    "GLEAN_DATASOURCE": TEST_DATASOURCE,
    "GLEAN_CLIENT_TOKEN": TEST_CLIENT_TOKEN,
}

# Read off the model rather than transcribed, so a field added to Answer cannot
# leave assertions quietly checking the old shape.
ENVELOPE_KEYS = frozenset(Answer.model_fields)


def make_settings(**overrides) -> Settings:
    """A query-scoped Settings with no environment involved."""
    base = {
        "instance": TEST_INSTANCE,
        "datasource": TEST_DATASOURCE,
        "doc_id_prefix": "halcyon",
        "default_top_k": 5,
        "max_snippet_size": 2000,
        "min_results": 1,
        "min_body_chars": 200,
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


@pytest.fixture
def clean_env(monkeypatch):
    """Wipe every GLEAN_* variable, then set the minimum for_query() needs.

    By prefix rather than a hand-kept list: `load_dotenv(override=False)` runs at
    import time, so the developer's real .env is already in os.environ when tests
    collect.
    """
    for name in [name for name in os.environ if name.startswith("GLEAN_")]:
        monkeypatch.delenv(name, raising=False)
    for name, value in QUERY_ENV.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def query_settings() -> Settings:
    return make_settings()
