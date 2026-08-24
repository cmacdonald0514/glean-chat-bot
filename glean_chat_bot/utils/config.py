import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from dotenv import load_dotenv

# override=False so an already-exported shell variable beats the .env file.
load_dotenv(override=False)


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing or unusable."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


def _optional(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _number_env[T: (int, float)](name: str, default: T, cast: Callable[[str], T]) -> T:
    raw = _optional(name, str(default))
    try:
        return cast(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a valid {cast.__name__}, got {raw!r}") from exc


def _float_env(name: str, default: float) -> float:
    return _number_env(name, default, float)


def _int_env(name: str, default: int) -> int:
    return _number_env(name, default, int)


def _require_dir(name: str) -> Path:
    directory = Path(_require(name))
    if not directory.is_dir():
        raise ConfigError(f"{name} does not exist: {directory}")
    return directory


def _common() -> dict:
    return {
        "instance": _require("GLEAN_INSTANCE"),
        "datasource": _require("GLEAN_DATASOURCE"),
        "doc_id_prefix": _optional("GLEAN_DOC_ID_PREFIX", "halcyon"),
        "default_top_k": _int_env("GLEAN_TOP_K", 5),
        "max_snippet_size": _int_env("GLEAN_MAX_SNIPPET_SIZE", 2000),
        "min_term_overlap": _float_env("GLEAN_MIN_TERM_OVERLAP", 0.30),
        # Required when the client token is Global: Glean rejects the request
        # with "Required header missing" unless X-Glean-ActAs names a user.
        "act_as": _optional("GLEAN_ACT_AS", ""),
        # The SDK default is ~5s, too short for chat generation.
        "chat_timeout_ms": _int_env("GLEAN_CHAT_TIMEOUT_MS", 60000),
    }


@dataclass(frozen=True)
class Settings:
    instance: str
    datasource: str
    doc_id_prefix: str
    default_top_k: int
    max_snippet_size: int
    min_term_overlap: float
    act_as: str
    chat_timeout_ms: int
    # Exactly one of these is populated, depending on which constructor ran.
    indexing_token: str | None = None
    client_token: str | None = None
    docs_root: Path | None = None

    @classmethod
    def for_indexing(cls) -> Self:
        return cls(
            **_common(),
            indexing_token=_require("GLEAN_INDEXING_TOKEN"),
            docs_root=_require_dir("GLEAN_DOCS_ROOT"),
        )

    @classmethod
    def for_query(cls) -> Self:
        return cls(**_common(), client_token=_require("GLEAN_CLIENT_TOKEN"))

    def namespaced_doc_id(self, doc_id: str) -> str:
        """Apply the shared-sandbox prefix, idempotently: HR-004 and halcyon-HR-004 both work."""
        prefix = f"{self.doc_id_prefix}-"
        return doc_id if doc_id.startswith(prefix) else prefix + doc_id
