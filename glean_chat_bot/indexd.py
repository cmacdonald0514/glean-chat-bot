"""`glean-indexd` serves the indexing run over HTTP; `glean-index-trigger` calls it.

Why a service at all: indexing has to happen on a schedule and on demand, and
neither is a reason for the MCP server to gain the ability to write. This is a
separate process, built from the same image but launched with a different
command, holding `GLEAN_INDEXING_TOKEN` where the MCP server holds only
`GLEAN_CLIENT_TOKEN`. The cron sidecar that calls it holds neither.

`POST /index` runs synchronously and answers with the outcome rather than
accepting the job and returning 202. That is what lets the caller - cron, in
practice - exit non-zero on a failed run, so a broken index shows up in
`docker compose logs indexer-cron` instead of needing a status endpoint polled
by somebody who already went home.
"""

import argparse
import logging
import sys
import threading
import urllib.error
import urllib.request

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from glean_chat_bot.indexing import PROCESS_NOW_HELP, index_once
from glean_chat_bot.utils.config import (
    ConfigError,
    IndexerOptions,
    Settings,
    listener_parser,
)
from glean_chat_bot.utils.logging import configure_logging

log = logging.getLogger("glean_chat_bot.indexd")

INDEX_PATH = "/index"
HEALTH_PATH = "/healthz"

# Built once at startup by main() and reused by every request, mirroring the
# MCP server: a misconfigured process must fail to start, not fail per request.
SETTINGS: Settings | None = None

# A bulk upload replaces the datasource contents as a unit, so two overlapping
# runs are not "slow" - they fight over what the datasource ends up containing.
# Non-blocking acquire: the second caller is told no, rather than queued behind
# a run whose result it would only duplicate.
#
# This guards the endpoint, not the datasource: a `glean-index` push from a
# shell bypasses it entirely, and there push()'s force_restart_upload is what
# arbitrates - last writer wins. Closing that would need a lock shared across
# containers, which is not worth a volume for a command documented as a dry run.
_RUNNING = threading.Lock()

TRIGGER_TIMEOUT_SECONDS = 900


def index(request: Request) -> JSONResponse:
    """Run the indexer once. Sync `def`, so Starlette runs it in a threadpool.

    That matters: the Glean SDK is blocking, and an `async def` here would stall
    the event loop for the whole run - including /healthz.
    """
    if not _RUNNING.acquire(blocking=False):
        log.warning("rejected: an indexing run is already in progress")
        return JSONResponse(
            {"ok": False, "error": "an indexing run is already in progress"},
            status_code=409,
        )
    try:
        process_now = request.query_params.get("process_now", "").lower() in {"1", "true", "yes"}
        summary = index_once(SETTINGS, process_now=process_now)
    except Exception as exc:
        # The detail goes in the body, not only the log: an opaque 500 tells the
        # caller nothing, and the log is inside a container it may not read.
        log.exception("indexing run failed")
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            status_code=500,
        )
    finally:
        _RUNNING.release()
    return JSONResponse({"ok": True, **summary})


async def healthz(request: Request) -> PlainTextResponse:
    """Process liveness only.

    Same reasoning as the MCP server's: this makes no Glean call, because an
    unreachable Glean is a failed run reported in the response body, not a
    reason for the orchestrator to restart a healthy process. It stays `async`
    so it answers even while a run is occupying a threadpool worker.
    """
    return PlainTextResponse("ok")


app = Starlette(
    routes=[
        Route(INDEX_PATH, index, methods=["POST"]),
        Route(HEALTH_PATH, healthz, methods=["GET"]),
    ]
)


def main(argv: list[str] | None = None) -> int:
    global SETTINGS

    defaults = IndexerOptions.from_env()
    parser = listener_parser(
        "glean-indexd", "Serve the Halcyon corpus indexing run over HTTP.", defaults
    )
    args = parser.parse_args(argv)

    configure_logging(args.verbose)
    try:
        # Fail at startup rather than on the first request, so a missing token
        # or docs root is a container that will not come up healthy.
        SETTINGS = Settings.for_indexing()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    log.info("indexing service on http://%s:%s%s", args.host, args.port, INDEX_PATH)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        # log_config=None or uvicorn runs dictConfig and tears down the handlers
        # configure_logging() just installed, taking the format and -v with it.
        # With no config of its own, uvicorn's loggers propagate to the root
        # logger and land on stderr alongside everything else.
        log_config=None,
    )
    return 0


# --- the trigger CLI ---------------------------------------------------------


def trigger(argv: list[str] | None = None) -> int:
    """POST to the indexing service and report what came back.

    This is what the cron sidecar runs, which is why it holds no Glean
    credential: the token lives in the service, and this only makes an HTTP
    call. It prints, because unlike the service it is a CLI.
    """
    defaults = IndexerOptions.from_env()
    parser = argparse.ArgumentParser(
        prog="glean-index-trigger",
        description="Ask the indexing service to run once.",
    )
    parser.add_argument(
        "--url",
        default=defaults.trigger_url or f"http://127.0.0.1:{defaults.port}{INDEX_PATH}",
        help="indexing endpoint [$INDEXER_URL]",
    )
    parser.add_argument(
        "--process-now",
        action="store_true",
        help=PROCESS_NOW_HELP,
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=TRIGGER_TIMEOUT_SECONDS,
        help=f"seconds to wait for the run [{TRIGGER_TIMEOUT_SECONDS}]",
    )
    args = parser.parse_args(argv)

    url = f"{args.url}?process_now=true" if args.process_now else args.url
    request = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            print(response.read().decode())
        return 0
    except urllib.error.HTTPError as exc:
        # 409 (already running) and 500 (the run failed) both arrive here, and
        # both carry a JSON body worth showing.
        print(f"HTTP {exc.code}: {exc.read().decode(errors='replace')}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"could not reach {url}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
