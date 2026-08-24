import contextlib
import logging
import sys
import time

log = logging.getLogger("glean_chat_bot.api")


def configure_logging(verbose: bool) -> None:
    """Logs go to stderr: the MCP stdio transport owns stdout for JSON-RPC."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


@contextlib.contextmanager
def log_call(endpoint: str, **context):
    """Time one Glean API call and log it, whether it succeeds or raises.

    Yields a mutable dict; callers add result counts to it so they land on the
    same log line as the latency.
    """
    record: dict = {}
    started = time.perf_counter()
    outcome = "ok"
    try:
        yield record
    except Exception as exc:
        outcome = f"error={type(exc).__name__}"
        raise
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        fields = " ".join(f"{k}={v!r}" for k, v in {**context, **record}.items())
        log.info("%s %s %.0fms %s", endpoint, outcome, elapsed_ms, fields)
