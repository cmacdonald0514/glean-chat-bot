"""Shared fixtures. Importable helpers live in `helpers.py`."""

import os

import pytest

from glean_chat_bot.utils.config import Settings
from tests.helpers import make_settings


@pytest.fixture
def clean_env(monkeypatch):
    """Wipe every GLEAN_* variable, then set the minimum for_query() needs.

    Deleting by prefix rather than by a hand-kept list, because
    `load_dotenv(override=False)` runs at import time: the developer's real
    .env is already in os.environ when tests collect, so a variable added to
    config.py and forgotten here would leave a config test asserting against
    real values.
    """
    for name in [name for name in os.environ if name.startswith("GLEAN_")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GLEAN_INSTANCE", "test-instance")
    monkeypatch.setenv("GLEAN_DATASOURCE", "testds")
    monkeypatch.setenv("GLEAN_CLIENT_TOKEN", "test-client-token")


@pytest.fixture
def query_settings() -> Settings:
    return make_settings()
