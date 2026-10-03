"""Every chat call carries the address its profile demands — or refuses.

A workspace's own LiteLLM proxy gives `p.litellm_model == "litellm_proxy/<alias>"`
and `p.api_key == <the tenant's virtual key>`. Handed to `litellm.completion`
with no `api_base`, the SDK reads LITELLM_PROXY_API_BASE — which
src/llm/gateway.py points at the INSTALLATION's gateway. So the tenant's key
and a prompt built from its repository went to the operator's proxy. Three
non-streaming callers did exactly that: the architecture summary and both
dependency reports. With the gateway off they failed instead, for every proxy
workspace.

`completion_route(p)` in src/llm/completion.py is the one place the rule lives;
these tests drive each caller with a proxy profile and read what reached
litellm.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.llm.profiles import Profile

BASE = "https://proxy.tenant.example"
SRC = Path(__file__).resolve().parents[2] / "src"


def _proxy_profile(api_base: str | None = BASE) -> Profile:
    return Profile(surface="review", provider="litellm", model="chat-a",
                   api_key="sk-tenant-virtual", api_base=api_base)


def _self_hosted(api_base: str | None) -> Profile:
    return Profile(surface="review", provider="openai_compatible", model="qwen",
                   api_key="local-no-key", api_base=api_base)


@pytest.fixture
def calls(monkeypatch):
    import litellm

    import src.llm.completion as completion
    import src.llm.litellm_proxy as litellm_proxy

    seen: list[dict] = []

    def _fake_completion(**kw):
        seen.append(kw)
        msg = SimpleNamespace(content="report")
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)

    monkeypatch.setattr(litellm, "completion", _fake_completion)
    # The address rules are tested in tests/api/test_litellm_proxy_setup.py;
    # here only WHICH address is handed over matters.
    monkeypatch.setattr(litellm_proxy, "ensure_call_target", lambda b: b)
    monkeypatch.setattr(completion, "record_completion_spend", lambda *a, **kw: None)
    monkeypatch.setenv("LITELLM_PROXY_API_BASE", "https://installation-gateway.internal")
    return seen


def _use(monkeypatch, profile: Profile) -> None:
    import src.llm.profiles as profiles

    monkeypatch.setattr(profiles, "resolve_profile", lambda surface, ws="default": profile)


def _architecture() -> str:
    import src.review.architecture as arch

    text, _model, _tokens = arch.generate_summary(
        repo_slug="acme", user_id="u", workspace_id="ws-a")
    return text


def _deps_router_report() -> str:
    from src.api.routers.deps import _api_report

    return _api_report("prompt", "ws-a", 0.2)


def _deps_report() -> str:
    from src.deps.report import run_api_report

    return run_api_report("prompt", "ws-a")


CALLERS = [_architecture, _deps_router_report, _deps_report]


@pytest.fixture(autouse=True)
def _context(monkeypatch):
    import src.review.architecture as arch

    monkeypatch.setattr(arch, "_collect_context", lambda *a, **kw: "tree + readme")


@pytest.mark.parametrize("caller", CALLERS, ids=lambda c: c.__name__)
def test_a_proxy_profile_goes_to_its_own_proxy(monkeypatch, calls, caller) -> None:
    _use(monkeypatch, _proxy_profile())
    assert caller() == "report"
    [kw] = calls
    assert kw["model"] == "litellm_proxy/chat-a"
    assert kw["api_base"] == BASE


@pytest.mark.parametrize("caller", CALLERS, ids=lambda c: c.__name__)
def test_a_proxy_profile_without_an_address_never_calls(monkeypatch, calls, caller) -> None:
    _use(monkeypatch, _proxy_profile(api_base=None))
    if caller is _architecture:
        assert caller() == "", "the summary's contract is: empty, never a raise"
    else:
        with pytest.raises(Exception, match="no base URL"):
            caller()
    assert calls == [], "the call went out with no address — to the installation's gateway"


@pytest.mark.parametrize("caller", CALLERS, ids=lambda c: c.__name__)
def test_a_self_hosted_profile_goes_to_its_server_or_nowhere(monkeypatch, calls, caller) -> None:
    _use(monkeypatch, _self_hosted("http://llm.lan:8000/v1"))
    caller()
    assert calls[-1]["api_base"] == "http://llm.lan:8000/v1"
    calls.clear()
    _use(monkeypatch, _self_hosted(None))
    if caller is _architecture:
        assert caller() == ""
    else:
        with pytest.raises(RuntimeError, match="api.openai.com"):
            caller()
    assert calls == []


def _completion_calls_without_a_route() -> list[str]:
    """`litellm.completion(` / `litellm.acompletion(` calls in src that pass
    neither `api_base=` nor a `**kwargs` splat the route can ride in."""
    bad: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Attribute) and f.attr in ("completion", "acompletion")
                    and isinstance(f.value, ast.Name) and f.value.id == "litellm"):
                continue
            names = {k.arg for k in node.keywords}
            if "api_base" not in names and None not in names:
                bad.append(f"{path.relative_to(SRC)}:{node.lineno}")
    return bad


def test_no_completion_call_site_drops_the_route() -> None:
    assert _completion_calls_without_a_route() == []
