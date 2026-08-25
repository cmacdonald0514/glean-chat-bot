import argparse
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


# The loopback set the SDK would apply on its own if we bound to 127.0.0.1. We
# name it explicitly because the container binds 0.0.0.0, where the SDK's
# default is no protection at all rather than this list.
DEFAULT_ALLOWED_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")

# Loopback unless something says otherwise. Both images override it to 0.0.0.0,
# which is safe only because each server states its own exposure rules -- the
# MCP server an explicit Host allowlist, the indexer an unpublished port. One
# home for the default so that policy cannot drift between the two.
DEFAULT_BIND_HOST = "127.0.0.1"


def _listen_env(prefix: str, default_port: int) -> dict:
    """`MCP_HOST`/`MCP_PORT`, `INDEXER_HOST`/`INDEXER_PORT` - one convention, read once."""
    return {
        "host": _optional(f"{prefix}_HOST", DEFAULT_BIND_HOST),
        "port": _int_env(f"{prefix}_PORT", default_port),
    }


def listener_parser(prog: str, description: str, defaults) -> argparse.ArgumentParser:
    """The flags every server entrypoint takes, so the two cannot drift apart.

    Both servers are launched the same way and should stay launchable the same
    way; `--host`/`--port` override what `*_HOST`/`*_PORT` supplied.
    """
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--host", default=defaults.host, help=f"bind address [{defaults.host}]")
    parser.add_argument("--port", type=int, default=defaults.port, help=f"port [{defaults.port}]")
    return parser


@dataclass(frozen=True)
class ServerOptions:
    """Where the MCP server listens. Deliberately not part of `Settings`.

    `Settings.for_query()` / `for_indexing()` are the token-separation seam; a
    bind address is not a Glean credential and must not travel through them.
    """

    host: str
    port: int
    allowed_hosts: tuple[str, ...]

    @classmethod
    def from_env(cls) -> Self:
        # Split only what the environment actually set: the default is ours and
        # is already in its parsed shape.
        raw = _optional("MCP_ALLOWED_HOSTS", "")
        return cls(
            **_listen_env("MCP", 8000),
            allowed_hosts=tuple(h.strip() for h in raw.split(",") if h.strip())
            or DEFAULT_ALLOWED_HOSTS,
        )


@dataclass(frozen=True)
class IndexerOptions:
    """Where the indexing service listens, and where its trigger CLI points.

    Separate from `Settings` for the same reason `ServerOptions` is: a bind
    address is not a Glean credential and must not travel through the
    token-separation seam. Separate from `ServerOptions` because the two
    processes are deployed apart - the cron sidecar sets `INDEXER_URL` and holds
    no Glean token at all.
    """

    host: str
    port: int
    # None because the default is a URL built from the route path, which is
    # defined by the service module rather than here. Typed so that is visible.
    trigger_url: str | None

    @classmethod
    def from_env(cls) -> Self:
        return cls(**_listen_env("INDEXER", 8001), trigger_url=_optional("INDEXER_URL", "") or None)
