"""What one call to a workspace's own LiteLLM proxy cost — ONE answer.

A proxy alias ("team-flash-latest") is in no price table, and what it runs on
may be a fine-tune, a self-hosted model or a vendor model LiteLLM's table has
never heard of. So the price of an alias is resolved here, in this order, and
every cost path that bills a workspace-proxy call asks this module (the review
/ agent ``LLMClient``, the chat stream, embeddings, the non-streaming
``record_completion_spend`` callers — all through
:func:`workspace_proxy_cost`):

  1. ``manual_price``     — a price a workspace admin typed in Settings → LLM
                            for that alias (USD per 1M tokens, stored in the
                            workspace LLM config blob under ``model_prices``).
                            Read from the workspace that OWNS the proxy the
                            call went to: the shared embeddings profile uses
                            the default workspace's proxy, so its prices too.
  2. ``openrouter_actual``— an amount the response itself says was charged
                            (``usage.cost``). Not a price, a fact; it keeps
                            outranking every estimate exactly as before.
  3. ``proxy_price``      — the price the proxy declares in ``/model/info``
                            ``model_info.input_cost_per_token`` /
                            ``output_cost_per_token``.
  4. ``litellm_estimate`` — an estimate LiteLLM made off the response, then
                            LiteLLM's table price of the model behind the
                            alias (``litellm_params.model``), then the alias
                            name itself as a last resort.
  5. ``unknown``          — None. Never a made-up number.

Best effort throughout: pricing never fails a call. The installation gateway
(``Profile.via_gateway``) never comes through here.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Key in the workspace LLM config blob (provider "__llm_workspace__").
CONFIG_KEY = "model_prices"
#: The UI and the API speak USD per 1M tokens; the ledger multiplies per token.
PER_MTOK = 1_000_000
#: Upper bound for a manual price, USD per 1M tokens. Far above any hosted
#: model today; a value beyond it is a typo (per-token typed as per-1M, or
#: the other way round), not a price.
MAX_PER_MTOK = 1000.0
#: How many aliases one PUT may carry.
MAX_ALIASES = 200

SOURCE_MANUAL = "manual_price"
SOURCE_PROXY = "proxy_price"
SOURCE_TABLE = "litellm_estimate"
SOURCE_ACTUAL = "openrouter_actual"
SOURCE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class AliasPrice:
    """A per-token price and where it came from (one of the SOURCE_* values)."""

    input_per_token: float
    output_per_token: float
    source: str

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (int(tokens_in or 0) * self.input_per_token
                + int(tokens_out or 0) * self.output_per_token)


def clean_price(value: Any) -> float | None:
    """A usable price number, or None: a real int/float, finite, not negative.

    ``bool`` is an int in Python and is refused on purpose; so are strings — a
    proxy that answers ``"0.1"`` is not declaring a price we can trust the
    unit of.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    v = float(value)
    if not math.isfinite(v) or v < 0:
        return None
    return v


# ─── Manual prices (workspace config blob) ───────────────────────────


def price_workspace(surface: str | None, workspace_id: str) -> str:
    """The workspace whose proxy — and so whose manual prices — a call used.

    Embeddings are workspace-shared and resolved from the default workspace's
    profile and proxy (src/llm/profiles.resolve_profile), so their prices are
    the default workspace's as well. Everything else: the calling workspace.
    """
    return "default" if surface == "embeddings" else (workspace_id or "default")


def load_manual_prices(workspace_id: str) -> dict[str, dict[str, Any]]:
    """The raw ``model_prices`` map of a workspace ({} on any problem)."""
    try:
        from src.api.routers.llm import _load_workspace_config

        raw = (_load_workspace_config(workspace_id) or {}).get(CONFIG_KEY) or {}
    except Exception as exc:  # noqa: BLE001 — pricing never fails a call
        logger.debug("manual_prices_load_failed err=%s", type(exc).__name__)
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)} \
        if isinstance(raw, dict) else {}


def manual_price(entry: Any) -> AliasPrice | None:
    """A stored manual entry ({input_per_mtok, output_per_mtok}) → per token."""
    if not isinstance(entry, dict):
        return None
    i = clean_price(entry.get("input_per_mtok"))
    o = clean_price(entry.get("output_per_mtok"))
    if i is None or o is None:
        return None
    return AliasPrice(i / PER_MTOK, o / PER_MTOK, SOURCE_MANUAL)


def proxy_price(info: dict | None) -> AliasPrice | None:
    """The price ``/model/info`` declares for one alias, or None.

    Both halves must be explicitly present (0 included — a free alias is a
    price). An embedding alias has no output side, so a missing output there
    means 0.
    """
    if not isinstance(info, dict):
        return None
    i = clean_price(info.get("input_cost_per_token"))
    o = clean_price(info.get("output_cost_per_token"))
    if o is None and i is not None and info.get("mode") == "embedding":
        o = 0.0
    if i is None or o is None:
        return None
    return AliasPrice(i, o, SOURCE_PROXY)


def table_price(model: str | None) -> AliasPrice | None:
    """LiteLLM's table (+ OpenRouter overlay) price for a model name."""
    if not model:
        return None
    try:
        from src.llm.pricing import get_pricing_resolver

        p = get_pricing_resolver().get(model)
    except Exception:  # noqa: BLE001
        return None
    if p is None:
        return None
    return AliasPrice(float(p.input_usd_per_token), float(p.output_usd_per_token),
                      SOURCE_TABLE)


def _model_info(endpoint) -> dict[str, dict]:
    if endpoint is None:
        return {}
    try:
        from src.llm import litellm_proxy

        return litellm_proxy.cached_model_info(endpoint) or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("proxy_model_info_failed err=%s", type(exc).__name__)
        return {}


def resolve_alias_price(
    alias: str, *, workspace_id: str, endpoint=None,
    manual: dict[str, dict] | None = None, info: dict[str, dict] | None = None,
) -> AliasPrice | None:
    """manual → proxy → table(underlying). The order the settings page shows.

    `manual` / `info` may be passed in when the caller already holds them
    (the price list asks for every alias at once).
    """
    if not alias:
        return None
    if manual is None:
        manual = load_manual_prices(workspace_id)
    found = manual_price(manual.get(alias))
    if found is not None:
        return found
    if info is None:
        info = _model_info(endpoint)
    entry = info.get(alias) or {}
    found = proxy_price(entry)
    if found is not None:
        return found
    return table_price(entry.get("underlying"))


def workspace_proxy_cost(
    alias: str, *, workspace_id: str, endpoint, tokens_in: int, tokens_out: int,
    response_cost: float | None = None, response_source: str | None = None,
) -> tuple[float | None, str]:
    """(USD, cost_source) for one workspace-proxy call. Never raises.

    `response_cost` / `response_source` are what the response said about
    itself (``pricing.extract_actual_cost_usd``), when the caller had one.
    The order is the module docstring's.
    """
    try:
        manual = manual_price(load_manual_prices(workspace_id).get(alias))
        if manual is not None:
            return manual.cost(tokens_in, tokens_out), SOURCE_MANUAL
        if response_cost is not None and response_source == SOURCE_ACTUAL:
            return float(response_cost), SOURCE_ACTUAL
        info = _model_info(endpoint)
        entry = info.get(alias) or {}
        declared = proxy_price(entry)
        if declared is not None:
            return declared.cost(tokens_in, tokens_out), SOURCE_PROXY
        if response_cost is not None:
            return float(response_cost), response_source or SOURCE_TABLE
        for name in (entry.get("underlying"), alias):
            table = table_price(name)
            if table is not None:
                return table.cost(tokens_in, tokens_out), SOURCE_TABLE
    except Exception as exc:  # noqa: BLE001 — pricing never fails a call
        logger.debug("workspace_proxy_cost_failed err=%s", type(exc).__name__)
        if response_cost is not None:
            return float(response_cost), response_source or SOURCE_TABLE
    return None, SOURCE_UNKNOWN


def profile_endpoint(p):
    """The proxy endpoint a workspace-proxy profile calls, or None."""
    if p is None or not (getattr(p, "api_base", None) and getattr(p, "api_key", None)):
        return None
    try:
        from src.llm.litellm_proxy import Endpoint

        return Endpoint(base_url=p.api_base, api_key=p.api_key, source="profile")
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "AliasPrice",
    "CONFIG_KEY",
    "MAX_ALIASES",
    "MAX_PER_MTOK",
    "PER_MTOK",
    "SOURCE_MANUAL",
    "SOURCE_PROXY",
    "SOURCE_TABLE",
    "SOURCE_UNKNOWN",
    "clean_price",
    "load_manual_prices",
    "manual_price",
    "price_workspace",
    "profile_endpoint",
    "proxy_price",
    "resolve_alias_price",
    "table_price",
    "workspace_proxy_cost",
]
