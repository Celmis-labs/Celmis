"""Is the review model answering? Asked before a review starts, not after it failed.

A review used to find out the model was down the expensive way: every agent
failed, the run ended "REVIEW FAILED ... has NOT been reviewed" on the pull
request, and the queue marked the job completed, so nothing ever ran it again.

This module is the question and its answer:

  * `check_models` sends ONE tiny real request through the SAME `LLMClient`
    the review will use (same workspace key, same routing, same OpenRouter
    fallback), for each distinct model the review's agents would call. No
    litellm retries, a short timeout, a few output tokens.
  * The answer is cached per (workspace, model) — 60 s when it was yes, 15 s
    when it was no — and a burst of pull-request events costs one probe: the
    probe of a key is single-flight, so concurrent callers wait for it.
  * `ModelUnavailable` is how a review that must wait leaves the pipeline. The
    queue worker turns it into a deferral (`src.sync.queue.JobDeferred`);
    a caller that cannot queue gets it as a clear error.

Availability, by the dispositions of `src.llm.errors`:

  available   the call returned (an empty answer too), or the model REFUSED
              this one probe (it answered), or it failed in a way that says
              nothing about reachability (an unrecognised error, a context
              window the probe cannot exceed). Failing OPEN there is
              deliberate: a probe that blocks every review because the
              provider rejects some quirk of the probe itself is worse than
              the outage it was meant to catch; the review will report it.
  unavailable TRANSIENT (provider down, timeout), THROTTLED (429) and the
              TERMINAL codes that are about the workspace, not the request
              (missing/invalid key, unknown model, exhausted budget or quota).

Never raises; never puts an exception's own text anywhere — the reason is the
curated sentence of the failure code.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field

from src.sync.queue import JobDeferred

logger = logging.getLogger(__name__)

#: Codes that mean "the model cannot be used right now" — an outage or a
#: workspace-level block, as opposed to something wrong with one request.
#: `context_too_long` is TERMINAL too but belongs to the pull request: waiting
#: would never fix it.
AVAILABILITY_CODES: frozenset[str] = frozenset({
    "no_api_key", "invalid_key", "model_not_found", "budget_exceeded",
    "quota_exhausted", "rate_limited", "provider_unavailable", "local_timeout",
})

_OK_TTL = 60.0
_FAIL_TTL = 15.0
_PROBE_PROMPT = "Reply with the single word: ok"
#: Reachability only: one word back is enough; reasoning is asked for at the
#: lowest level the model takes (dropped silently when it takes none).
_PROBE_MAX_TOKENS = 16
_PROBE_REASONING = "low"

#: Availability codes that a person has to fix (key, model name, budget): the
#: review still waits, but the workspace admins are told once per job.
WORKSPACE_CODES: frozenset[str] = frozenset({
    "no_api_key", "invalid_key", "model_not_found", "budget_exceeded",
    "quota_exhausted",
})


def enabled() -> bool:
    """On unless CELMIS_REVIEW_MODEL_PREFLIGHT is off/0/false/no."""
    return os.environ.get("CELMIS_REVIEW_MODEL_PREFLIGHT", "on").strip().lower() \
        not in ("off", "0", "false", "no", "disabled")


def probe_timeout() -> float:
    try:
        return max(1.0, float(os.environ.get(
            "CELMIS_REVIEW_MODEL_PREFLIGHT_TIMEOUT_SECONDS", "20")))
    except ValueError:
        return 20.0


class ModelUnavailable(JobDeferred):
    """The review model cannot be reached; the review has to wait.

    `reason` is a curated sentence (safe for the run record, the jobs page and
    a pull request). `models` lists what was probed — for the log only."""

    def __init__(self, reason: str, *, code: str | None = None,
                 models: tuple[str, ...] = ()) -> None:
        super().__init__(reason, code=code)
        self.models = models


@dataclass(frozen=True)
class ModelCheck:
    available: bool
    code: str | None = None
    reason: str | None = None
    models: tuple[str, ...] = ()
    cached: bool = False

    def raise_if_unavailable(self) -> None:
        if not self.available:
            raise ModelUnavailable(self.reason or "the model is unavailable",
                                   code=self.code, models=self.models)


@dataclass
class ReviewModels:
    """What a review of one repository would call: the client and, per
    distinct model, the first agent that uses it (the probe goes out as it)."""

    client: object | None
    targets: list[tuple[str, str]] = field(default_factory=list)  # (agent, model)

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(m for _, m in self.targets)


# ─── Which models would this review call? ────────────────────────────


def review_models(provider: str, repo: str, *, workspace_id: str,
                  user_id: str) -> ReviewModels:
    """The distinct models the review's LLM agents would call, resolved the
    way the orchestrator resolves them (repo policy over workspace defaults
    over install). Empty targets mean there is nothing to probe — the
    workspace reviews with Claude Code, the roster is switched off, or no LLM
    client could be built (the agents then have their own legacy path)."""
    from src.review.orchestrator import ReviewOrchestrator
    from src.review.review_defaults import effective_disabled_agents
    from src.review.settings import REVIEW_AGENTS

    try:
        from src.api.routers.llm import _load_workspace_config

        if str(_load_workspace_config(workspace_id).get("review_engine") or "api") \
                == "claude_code":
            return ReviewModels(None)
    except Exception:  # noqa: BLE001
        pass
    orch = ReviewOrchestrator()
    try:
        from src.sync.git_providers import parse_repo_url

        slug = parse_repo_url(f"{provider}:{repo}").slug
    except Exception:  # noqa: BLE001
        slug = f"{provider}_{repo.replace('/', '-')}"
    try:
        policy = orch._resolved_policy(slug, workspace_id)
    except Exception:  # noqa: BLE001
        policy = None
    client, _by_agent = orch._build_llm_client(user_id, workspace_id, policy)
    if client is None:
        return ReviewModels(None)
    named_off = {str(a).strip().lower()
                 for a in ((policy or {}).get("disabled_agents") or [])}
    off = named_off | effective_disabled_agents(
        None, (policy or {}).get("enabled_agents"))
    with contextlib.suppress(Exception):
        off |= set(orch._dormant_agents(policy, off))
    seen: dict[str, str] = {}
    for agent in REVIEW_AGENTS:
        if agent in off:
            continue
        model = client.model_for(agent)
        if model and model not in seen:
            seen[model] = agent
    return ReviewModels(client, [(a, m) for m, a in seen.items()])


# ─── The probe, its cache, and the answer ────────────────────────────


_CACHE: dict[tuple[str, str], tuple[float, ModelCheck]] = {}
_KEY_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_GUARD = threading.Lock()


def reset_cache() -> None:
    with _GUARD:
        _CACHE.clear()
        _KEY_LOCKS.clear()


def _cached(key: tuple[str, str]) -> ModelCheck | None:
    hit = _CACHE.get(key)
    if hit is None:
        return None
    expires, check = hit
    if time.monotonic() >= expires:
        return None
    return ModelCheck(check.available, check.code, check.reason,
                      check.models, cached=True)


def probe_failure(exc: BaseException) -> tuple[bool, str | None]:
    """(available?, code) for an exception raised by the probe call."""
    from src.llm.errors import REFUSED, classify

    failure = classify(exc)
    if failure.disposition == REFUSED:
        return True, failure.code
    if failure.code == "rate_limited":
        # A 429 on the one-word probe says the gateway is busy, not down: the
        # review has its own retries and must get its chance.
        return True, failure.code
    return failure.code not in AVAILABILITY_CODES, failure.code


def _probe_one(client, agent: str, model: str) -> ModelCheck:
    from src.llm.errors import curated_reason

    try:
        client.generate(
            prompt=_PROBE_PROMPT, agent=agent, mode="review",
            operation="review_preflight", max_output_tokens=_PROBE_MAX_TOKENS,
            reasoning=_PROBE_REASONING, num_retries=0, timeout=probe_timeout(),
        )
    except Exception as exc:  # noqa: BLE001 — classified, never re-raised
        available, code = probe_failure(exc)
        if available:
            if code and code not in AVAILABILITY_CODES:
                logger.info("review_preflight_inconclusive model=%s code=%s "
                            "— treated as available", model, code)
            return ModelCheck(True, code, models=(model,))
        return ModelCheck(False, code, curated_reason(code)
                          or "the model is unavailable", (model,))
    return ModelCheck(True, models=(model,))


def check_models(workspace_id: str, models: ReviewModels) -> ModelCheck:
    """Probe each distinct model (cache first) and answer for the set: the
    first model that is unavailable decides, so the reason names a real cause."""
    if not enabled() or models.client is None or not models.targets:
        return ModelCheck(True, models=models.models)
    answers: list[ModelCheck] = []
    for agent, model in models.targets:
        key = (workspace_id, model)
        check = _cached(key)
        if check is None:
            with _GUARD:
                lock = _KEY_LOCKS.setdefault(key, threading.Lock())
            with lock:  # single flight: a burst waits for one probe
                check = _cached(key)
                if check is None:
                    check = _probe_one(models.client, agent, model)
                    ttl = _OK_TTL if check.available else _FAIL_TTL
                    _CACHE[key] = (time.monotonic() + ttl, check)
        answers.append(check)
        if not check.available:
            break
    bad = next((c for c in answers if not c.available), None)
    if bad is not None:
        return ModelCheck(False, bad.code, bad.reason, models.models,
                          cached=all(c.cached for c in answers))
    return ModelCheck(True, models=models.models,
                      cached=all(c.cached for c in answers))


def check_for_review(provider: str, repo: str, *, workspace_id: str,
                     user_id: str) -> ModelCheck:
    """The preflight of one review. Never raises: a question that cannot be
    asked is not an outage."""
    if not enabled():
        return ModelCheck(True)
    try:
        return check_models(workspace_id, review_models(
            provider, repo, workspace_id=workspace_id, user_id=user_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("review_preflight_failed_open workspace=%s err_type=%s",
                       workspace_id, type(exc).__name__)
        return ModelCheck(True)


# ─── A review that ended with nothing having answered ────────────────


def notify_blocked(payload: dict, code: str | None, reason: str) -> None:
    """Tell the workspace once that a review waits for something a person must
    fix (key, model name, budget). Never raises; the reason is the curated
    sentence, never an exception's text."""
    if code not in WORKSPACE_CODES:
        return
    try:
        from src.notifications import notify

        notify(
            workspace_id=str(payload.get("workspace_id") or "default"),
            event="review_model_blocked", repo_slug=payload.get("repo"),
            title="Reviews are waiting for the model",
            body_md=(f"The review of `{payload.get('repo')}` "
                     f"#{payload.get('pr_number')} is waiting: {reason}. "
                     "It runs by itself once this is fixed."),
            severity="warn",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("review_model_blocked_notify_failed err_type=%s",
                       type(exc).__name__)


def outage_of(failed_codes: list[str | None]) -> ModelUnavailable | None:
    """`ModelUnavailable` when EVERY failed agent failed for a model-
    availability code; None when any failure is something else (a reply that
    could not be read, an agent crash with no code) — that review fails the
    way it always did."""
    from src.llm.errors import curated_reason

    if not failed_codes or any(c not in AVAILABILITY_CODES for c in failed_codes):
        return None
    first = failed_codes[0]
    return ModelUnavailable(curated_reason(first) or "the model is unavailable",
                            code=first)
