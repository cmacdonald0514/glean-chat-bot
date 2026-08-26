import os
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from dotenv import load_dotenv

# override=False so an already-exported shell variable beats the .env file.
load_dotenv(override=False)


DEFAULT_MIN_BODY_CHARS = 200


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing or unusable."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


def _optional(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _int_env(name: str, default: int) -> int:
    raw = _optional(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


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
        "min_results": _int_env("GLEAN_MIN_RESULTS", 1),
        "min_body_chars": _int_env("GLEAN_MIN_BODY_CHARS", DEFAULT_MIN_BODY_CHARS),
        "act_as": _optional("GLEAN_ACT_AS", ""),
        "chat_timeout_ms": _int_env("GLEAN_CHAT_TIMEOUT_MS", 60000),
    }


@dataclass(frozen=True)
class Settings:
    instance: str
    datasource: str
    doc_id_prefix: str
    default_top_k: int
    max_snippet_size: int
    min_results: int
    min_body_chars: int
    act_as: str
    chat_timeout_ms: int
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


# The loopback set the SDK would apply on its own if we bound to 127.0.0.1. We
# name it explicitly because the container binds 0.0.0.0, where the SDK's
# default is no protection at all rather than this list.
DEFAULT_ALLOWED_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")


@dataclass(frozen=True)
class ServerOptions:
    """Where the MCP server listens. Not part of `Settings`: a bind address is
    not a Glean credential and must not travel through the token-separation seam."""

    host: str
    port: int
    allowed_hosts: tuple[str, ...]

    @classmethod
    def from_env(cls) -> Self:
        raw = _optional("MCP_ALLOWED_HOSTS", "")
        return cls(
            host=_optional("MCP_HOST", "127.0.0.1"),
            port=_int_env("MCP_PORT", 8000),
            allowed_hosts=tuple(h.strip() for h in raw.split(",") if h.strip())
            or DEFAULT_ALLOWED_HOSTS,
        )
