"""SDK client factories, one per token, so the wrong one cannot be asked for."""

from __future__ import annotations

from glean.api_client import Glean

from config import ConfigError, Settings


def indexing_client(settings: Settings) -> Glean:
    if not settings.indexing_token:
        raise ConfigError(
            "indexing_client() needs Settings.for_indexing(); this Settings has "
            "no indexing token. The read path must not construct one."
        )
    return Glean(api_token=settings.indexing_token, instance=settings.instance)


def query_client(settings: Settings) -> Glean:
    if not settings.client_token:
        raise ConfigError(
            "query_client() needs Settings.for_query(); this Settings has no client token."
        )
    return Glean(api_token=settings.client_token, instance=settings.instance)


def act_as_headers(settings: Settings) -> dict[str, str]:
    """Per-request, not baked into the client: this header decides which documents come back."""
    return {"X-Glean-ActAs": settings.act_as} if settings.act_as else {}
