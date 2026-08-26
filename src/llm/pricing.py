"""Model pricing resolver (Stage 11).

Strategy:
    Base:    LiteLLM's ``model_cost`` table (ships with the package,
             ~2,900 entries covering all major providers as of pinned version).
    Overlay: OpenRouter ``GET /api/v1/models`` refreshed every 24 h.
             Provides fresher prices (intro-pricing, mid-cycle changes)
             and is the *only* provider with a programmatic price endpoint.
    Actual:  For OpenRouter path, ``response.usage.total_cost`` — this is
             the real amount debited from the user's balance.
             For direct paths, ``litellm.completion_cost(response)`` — an
             estimate based on the same table above.

Fallback semantics:
    Unknown model → ``None`` (not the LiteLLM default of $1). Callers must
    tolerate ``None`` and log/surface it — silent $1 estimates are worse
    than missing data.

Startup validation:
    ``assert_configured_models_known()`` walks ``ReviewSettings.*_model`` and
    fails startup if any of them are missing from the merged table — this is
    the guard-rail that prevents the "cost dashboard shows $1000/review"
    surprise.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)


# ─── Data ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelPrice:
    """Per-token USD price + metadata for one model."""

    model: str
    input_usd_per_token: float
    output_usd_per_token: float
    provider: str          # LiteLLM's litellm_provider (or "openrouter")
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    cached_input_usd_per_token: float | None = None  # anthropic cache read price

    @property
    def input_per_million(self) -> float:
        return self.input_usd_per_token * 1_000_000

    @property
    def output_per_million(self) -> float:
        return self.output_usd_per_token * 1_000_000


# ─── Resolver ────────────────────────────────────────────────────────


_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_OVERLAY_TTL = timedelta(hours=24)


class PricingResolver:
    """Base (LiteLLM) + overlay (OpenRouter). Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._base: dict[str, ModelPrice] = {}
        self._overlay: dict[str, ModelPrice] = {}
        self._last_overlay_refresh: datetime | None = None
        self._load_base()

    # ── Public ──

    def get(self, model: str) -> ModelPrice | None:
        """Return the merged price for `model`, or None if unknown.

        Match order: exact overlay → exact base → suffix match on base
        (handles LiteLLM's ``vendor/model`` prefixes, e.g.
        ``anthropic/claude-sonnet-4`` in overlay vs ``claude-sonnet-4`` in base).
        """
        with self._lock:
            if p := self._overlay.get(model):
                return p
            if p := self._base.get(model):
                return p
            # Try stripping provider prefix ("anthropic/claude-…" → "claude-…")
            if "/" in model:
                bare = model.split("/", 1)[1]
                if p := self._base.get(bare):
                    return p
        return None

    def known_models(self) -> list[str]:
        with self._lock:
            return sorted(set(self._base) | set(self._overlay))

    def is_overlay_stale(self) -> bool:
        return (
            self._last_overlay_refresh is None
            or datetime.now(UTC) - self._last_overlay_refresh > _OVERLAY_TTL
        )

    def refresh_overlay(self, *, timeout: float = 10.0) -> int:
        """Fetch OpenRouter's /models and merge into overlay. Returns count
        of entries added. Safe to call repeatedly — no-op on network failure.
        """
        try:
            from urllib.parse import urlsplit

            from src.http import build_client

            # openrouter.ai is not on the shipped public allowlist; the host
            # exception is derived from the module constant above, never from
            # anything a caller passed in.
            host = urlsplit(_OPENROUTER_MODELS_URL).hostname or ""
            with build_client(
                timeout=timeout, extra_allowed_hosts=(host,) if host else (),
            ) as client:
                r = client.get(_OPENROUTER_MODELS_URL)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("openrouter_pricing_refresh_failed err=%s", exc)
            return 0

        new: dict[str, ModelPrice] = {}
        for entry in data.get("data", []):
            model = entry.get("id")
            pricing = entry.get("pricing") or {}
            if not model or not pricing:
                continue
            try:
                inp = float(pricing.get("prompt", 0) or 0)
                out = float(pricing.get("completion", 0) or 0)
            except (TypeError, ValueError):
                continue
            if inp == 0 and out == 0:
                # Free / demo model — skip so we don't quote zero.
                continue
            ctx = entry.get("context_length") or None
            new[model] = ModelPrice(
                model=model,
                input_usd_per_token=inp,
                output_usd_per_token=out,
                provider="openrouter",
                max_input_tokens=int(ctx) if ctx else None,
                max_output_tokens=None,
            )
        with self._lock:
            self._overlay = new
            self._last_overlay_refresh = datetime.now(UTC)
        logger.info("openrouter_pricing_refreshed entries=%d", len(new))
        return len(new)

    # ── Base load ──

    def _load_base(self) -> None:
        try:
            import litellm
        except ImportError:
            logger.warning("litellm_not_installed — pricing disabled")
            return
        entries: dict[str, ModelPrice] = {}
        for name, meta in litellm.model_cost.items():
            if not isinstance(meta, dict):
                continue
            inp = meta.get("input_cost_per_token")
            out = meta.get("output_cost_per_token")
            if inp is None or out is None:
                continue
            entries[name] = ModelPrice(
                model=name,
                input_usd_per_token=float(inp),
                output_usd_per_token=float(out),
                provider=str(meta.get("litellm_provider", "")),
                max_input_tokens=meta.get("max_input_tokens"),
                max_output_tokens=meta.get("max_output_tokens"),
                cached_input_usd_per_token=meta.get(
                    "cache_read_input_token_cost"
                ),
            )
        with self._lock:
            self._base = entries
        logger.info("litellm_pricing_loaded entries=%d", len(entries))


# ─── Singleton ───────────────────────────────────────────────────────


_INSTANCE: PricingResolver | None = None


def get_pricing_resolver() -> PricingResolver:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = PricingResolver()
    return _INSTANCE


# ─── Cost calculation helpers ────────────────────────────────────────


def cost_for(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Estimate USD cost of a completion with `input_tokens` in / `output_tokens` out.
    Returns None if the model is not in the resolver — caller must handle."""
    price = get_pricing_resolver().get(model)
    if price is None:
        return None
    return (
        input_tokens * price.input_usd_per_token
        + output_tokens * price.output_usd_per_token
    )


def extract_actual_cost_usd(response) -> tuple[float | None, str]:
    """Extract the truest cost signal from a LiteLLM completion response.

    Returns (usd, source):
        source = "openrouter_actual" — response came from OpenRouter and
                 included usage.total_cost (real charged amount).
        source = "litellm_estimate"  — used LiteLLM's completion_cost() based
                 on the static/overlay table.
        source = "unknown"           — model missing from tables; cost is None.
    """
    # OpenRouter includes total_cost in usage. Only path that gives us the
    # real number instead of an estimate.
    usage = getattr(response, "usage", None)
    if usage is not None:
        actual = getattr(usage, "total_cost", None) or getattr(usage, "cost", None)
        if actual is not None:
            try:
                return float(actual), "openrouter_actual"
            except (TypeError, ValueError):
                pass

    # Fallback: LiteLLM's own estimate (uses model_cost table).
    try:
        import litellm

        est = litellm.completion_cost(completion_response=response)
        if est is None:
            return None, "unknown"
        return float(est), "litellm_estimate"
    except Exception as exc:  # noqa: BLE001
        logger.debug("completion_cost_failed err=%s", exc)
        return None, "unknown"


# ─── Startup validation ──────────────────────────────────────────────


def assert_configured_models_known(
    models: list[str],
    *,
    strict: bool = False,
) -> list[str]:
    """Called at API startup with the list of models from `ReviewSettings`.

    Returns list of unknown models. In `strict=True` also raises. Otherwise
    just logs a warning — so a dev machine with an unusual model spec still
    boots but the operator knows the cost dashboard will show blanks.
    """
    resolver = get_pricing_resolver()
    unknown = [m for m in models if resolver.get(m) is None]
    if unknown:
        msg = (
            f"models missing from pricing tables: {unknown}. "
            f"Cost tracking will report None for calls to these models. "
            f"Update litellm or add an override."
        )
        if strict:
            raise RuntimeError(msg)
        logger.warning(msg)
    return unknown


__all__ = [
    "ModelPrice",
    "PricingResolver",
    "get_pricing_resolver",
    "cost_for",
    "extract_actual_cost_usd",
    "assert_configured_models_known",
]
