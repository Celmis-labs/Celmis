"""A webhook secret belongs to one workspace, and proves only that workspace.

Auto-review is driven by an endpoint anyone on the internet can POST to, so
the secret is the whole of its authentication. There was ONE secret for the
entire instance, read from the environment, which is fine on a single-tenant
box and wrong here: every workspace would configure its provider with the same
string, and any tenant holding it could sign a delivery naming somebody else's
repository.

Two things now stand between a forged delivery and a review:

  * the secret is per workspace, resolved from that workspace's own slot in
    the credential store — no cross-tenant fallback;
  * the workspace in the URL must MATCH the workspace the repository is bound
    to, which is the half a per-workspace secret alone does not cover. Tenant
    A can always sign for A's own URL; without the assertion, A signs a
    payload naming B's repo and the review runs on B's token and B's budget.

The tests below are mostly about the second one, and about the ways a fix like
this quietly stops working: the legacy route regressing, the tenant arriving
as a query parameter instead of a path segment, or the store's failure mode
turning from "refuse" into "fall back".
"""

from __future__ import annotations

import ast
import inspect
import io
import textwrap
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEBHOOK_SRC = (ROOT / "src" / "review" / "webhook.py").read_text()
SECRETS_SRC = (ROOT / "src" / "review" / "webhook_secrets.py").read_text()


def _code(obj) -> str:
    """Source with comments and docstrings blanked, positions preserved.

    Both modules explain their own security decisions at length, naming the
    very things they refuse to do, so a plain grep proves nothing.
    """
    source = textwrap.dedent(inspect.getsource(obj))
    lines = source.splitlines(keepends=True)
    spans = [(t.start, t.end)
             for t in tokenize.generate_tokens(io.StringIO(source).readline)
             if t.type == tokenize.COMMENT]
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            spans.append(((first.lineno, first.col_offset),
                          (first.end_lineno, first.end_col_offset)))
    for (srow, scol), (erow, ecol) in spans:
        for row in range(srow, erow + 1):
            line = lines[row - 1]
            start = scol if row == srow else 0
            end = ecol if row == erow else len(line.rstrip("\n"))
            lines[row - 1] = line[:start] + " " * (end - start) + line[end:]
    return "".join(lines)


class _Settings:
    """Stands in for ReviewSettings — the env half of the resolver."""

    class _S:
        def __init__(self, v): self._v = v
        def get_secret_value(self): return self._v

    def __init__(self, github=None, gitlab=None, bitbucket=None):
        self.webhook_secret = self._S(github) if github else None
        self.gitlab_token = self._S(gitlab) if gitlab else None
        self.bitbucket_secret = self._S(bitbucket) if bitbucket else None


# ─── the resolver ────────────────────────────────────────────────────


def test_a_workspace_never_inherits_the_instance_secret():
    """The single most important line. Inheriting is exactly how one tenant
    ends up able to sign for another."""
    from src.review import webhook_secrets as ws

    ws._cache.clear()
    got = ws.resolve_webhook_secret("github", "ws-abc", _Settings(github="env-secret"))
    assert got is None, "a workspace with no secret of its own read the env one"


def test_the_legacy_route_still_reads_the_environment():
    """`None` is the un-suffixed URL that existing deployments registered
    before any of this existed. It must behave exactly as it used to."""
    from src.review import webhook_secrets as ws

    ws._cache.clear()
    for provider, kwargs in (("github", {"github": "g"}),
                             ("gitlab", {"gitlab": "l"}),
                             ("bitbucket", {"bitbucket": "b"})):
        assert ws.resolve_webhook_secret(provider, None, _Settings(**kwargs))


def test_the_default_workspace_may_still_be_env_configured():
    """Same transition rule `_git_slot_chain` already applies to git tokens, so
    a single-tenant install keeps working while it migrates."""
    from src.review import webhook_secrets as ws

    ws._cache.clear()
    assert ws.resolve_webhook_secret(
        "github", "default", _Settings(github="env-secret")) == "env-secret"


def test_each_provider_reads_its_own_field():
    """They verify differently — GitHub and Bitbucket sign the body, GitLab
    compares plaintext — so a crossed field is a 401 that looks like a wrong
    secret rather than a wrong scheme."""
    from src.review import webhook_secrets as ws

    ws._cache.clear()
    s = _Settings(github="G", gitlab="L", bitbucket="B")
    assert ws.resolve_webhook_secret("github", None, s) == "G"
    assert ws.resolve_webhook_secret("gitlab", None, s) == "L"
    assert ws.resolve_webhook_secret("bitbucket", None, s) == "B"


def test_an_unreadable_store_refuses_rather_than_raising(monkeypatch):
    """A lost master key must fail closed on the delivery, not take the API
    process down — and must NOT silently fall through to the env secret."""
    from src.review import webhook_secrets as ws

    ws._cache.clear()

    class _Boom:
        def load(self, *a, **k):
            raise RuntimeError("master key is gone")

    # Patch the store the resolver reaches for, not the resolver's own helper —
    # patching the helper would test the stub rather than the handling.
    import src.credentials.store as store_mod
    monkeypatch.setattr(store_mod, "get_credential_store", lambda: _Boom())

    # It must SWALLOW the failure and return None, not propagate…
    assert ws._stored_secret("github", "ws-1") is None
    # …and None must mean refuse, never fall through to the instance secret.
    assert ws.resolve_webhook_secret(
        "github", "ws-1", _Settings(github="env-secret")) is None


def test_the_unauthenticated_path_does_not_write_to_the_store():
    """Every unsigned POST from the internet would otherwise be a database
    write, which is a free amplification primitive."""
    assert "update_last_used=False" in _code(
        __import__("src.review.webhook_secrets", fromlist=["x"])._stored_secret)


def test_rotation_takes_effect_without_waiting_for_the_cache():
    from src.review import webhook_secrets as ws

    ws._cache[("github", "ws-1")] = (10 ** 9, "old")
    ws.invalidate("github", "ws-1")
    assert ("github", "ws-1") not in ws._cache
    assert "invalidate(" in _code(ws.save_webhook_secret)


# ─── the tenant assertion ────────────────────────────────────────────


def test_the_url_workspace_must_match_the_repo_binding():
    """Tenant A can always sign for A's own URL. The remaining attack is a
    payload naming B's repository, signed with A's valid secret — and without
    this the review runs on B's provider token and B's budget."""
    from src.review.webhook import _dispatch_review

    body = _code(_dispatch_review)
    assert "expected_workspace_id" in body
    assert "workspace_id != expected_workspace_id" in body
    # …and it must return, not merely log.
    after = body[body.index("workspace_id != expected_workspace_id"):]
    assert "return" in after[:400]


def test_the_assertion_comes_after_the_binding_is_resolved():
    """Comparing before the binding has produced a value would compare against
    None and pass everything.

    Keyed on the ASSIGNMENT, not on the lookup's name. This test used to grep
    for `workspace_for_repo`, and broke the day the dispatcher started asking
    the store for the whole config row instead of just the workspace id — a
    change that strengthened the very property this file exists to protect.
    A test that fails when the code improves is keyed on the wrong thing."""
    from src.review.webhook import _dispatch_review

    body = _code(_dispatch_review)
    assert body.index("workspace_id = ") < body.index("!= expected_workspace_id")


def test_every_handler_passes_its_tenant_on():
    """A handler that verifies against a workspace's secret and then dispatches
    without saying which workspace is the same hole with extra steps.

    Keyed on the property, not on a count. This asserted
    `count(...) == 3` and failed the day a fourth dispatcher was added that
    passes the tenant correctly — the failure mode the docstring two functions
    above names in so many words. A count also passes for the wrong reason:
    add a handler that omits the tenant while deleting one that had it, and
    three is still three.

    Every call to any `_dispatch_*` inside the app factory must carry
    `expected_workspace_id`, whatever the number of them turns out to be.
    """
    import ast

    from src.review.webhook import build_webhook_app

    tree = ast.parse(_code(build_webhook_app).lstrip())
    dispatches = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and str(getattr(node.func, "id", "") or getattr(node.func, "attr", ""))
        .startswith("_dispatch_")
    ]
    assert len(dispatches) >= 3, (
        f"expected a dispatcher per provider, found {len(dispatches)}"
    )
    missing = [
        ast.unparse(d.func)
        for d in dispatches
        if not any(kw.arg == "expected_workspace_id" for kw in d.keywords)
    ]
    assert not missing, (
        f"these dispatch without naming the workspace they verified against: "
        f"{missing}"
    )


def test_the_repo_binding_is_still_the_backstop():
    """Unchanged, and load-bearing: an unknown repo, or one bound to more than
    one workspace, is refused outright."""
    from src.review.webhook import _dispatch_review

    body = _code(_dispatch_review)
    # The store is asked, and a None answer returns rather than proceeding.
    # Either lookup satisfies this — what matters is that one is consulted and
    # that its refusal is honoured.
    assert "config_for_repo" in body or "workspace_for_repo" in body
    assert "get_auto_review_store()" in body
    guard = body.index("is None:")
    assert "return" in body[guard:guard + 400]


# ─── the routes ──────────────────────────────────────────────────────


@pytest.mark.parametrize("provider", ["github", "gitlab", "bitbucket"])
def test_both_route_shapes_exist(provider: str):
    """The tenanted one, and the un-suffixed one an existing deployment has
    already registered with its provider. Dropping the latter breaks
    auto-review on upgrade, silently, from the provider's side."""
    assert f'@app.post("/webhook/{provider}/{{workspace_id}}")' in WEBHOOK_SRC
    assert f'@app.post("/webhook/{provider}")' in WEBHOOK_SRC


@pytest.mark.parametrize("provider", ["github", "gitlab", "bitbucket"])
def test_the_legacy_route_is_its_own_function(provider: str):
    """Two decorators on one function would make FastAPI expose `workspace_id`
    as a QUERY parameter on the un-suffixed path — `?workspace_id=other`
    becomes a tenant selector on the URL everyone already has."""
    assert f"async def webhook_{provider}_legacy(" in WEBHOOK_SRC


@pytest.mark.parametrize("provider", ["github", "gitlab", "bitbucket"])
def test_the_legacy_delegate_passes_no_workspace(provider: str):
    """It must resolve to the environment secret, which is what `None` means.

    Checked as a keyword, because that is how it is written now and for a
    reason: the Bitbucket delegate originally passed headers positionally and
    had `x_event_key` and `x_request_uuid` the wrong way round, against a
    signature five hundred lines further up. Nothing about the call site
    looked wrong.
    """
    i = WEBHOOK_SRC.index(f"async def webhook_{provider}_legacy(")
    body = WEBHOOK_SRC[i:i + 900]
    assert f"await webhook_{provider}(" in body
    assert "workspace_id=None," in body
    # Every header forwarded by name, so a reordered signature cannot silently
    # swap two of them.
    call = body[body.index(f"await webhook_{provider}("):]
    call = call[:call.index(")")]
    for line in call.splitlines()[2:]:
        line = line.strip().rstrip(",")
        if line and line != "request":
            assert "=" in line, f"positional argument in the delegate: {line}"


def test_no_handler_reads_the_environment_secret_directly():
    """Each one has to go through the resolver, or a provider quietly keeps
    the single instance-wide secret while the others are tenanted."""
    for field in ("settings.webhook_secret.get_secret_value",
                  "settings.gitlab_token.get_secret_value",
                  "settings.bitbucket_secret.get_secret_value"):
        assert field not in WEBHOOK_SRC, field
    assert WEBHOOK_SRC.count("resolve_webhook_secret(") == 3


# ─── the setup endpoint ──────────────────────────────────────────────


def test_the_url_is_built_from_the_request_not_a_setting():
    """PUBLIC_BASE_URL is consumed only by the mailer and is routinely left at
    its default on a self-hosted box. A wrong URL here is handed to somebody
    as a copy-paste instruction and fails as a red delivery nobody sees."""
    from src.api.routers import webhooks

    body = _code(webhooks._public_base)
    assert "x-forwarded-proto" in body
    assert "x-forwarded-host" in body
    assert "PUBLIC_BASE_URL" not in body


def test_the_url_carries_the_proxy_prefix():
    """Caddy strips /backend before forwarding, so the route does not have it
    and the pasted URL must."""
    from src.api.routers import webhooks

    assert webhooks._PROXY_PREFIX == "/backend"
    assert "_PROXY_PREFIX" in _code(webhooks._webhook_url)


def test_the_secret_is_generated_not_typed():
    """Machine-to-machine, so a human-chosen one only adds the chance of a
    weak one."""
    from src.api.routers import webhooks

    assert "secrets.token_urlsafe" in _code(webhooks.rotate_secret)


def test_the_setup_endpoint_reports_resolved_not_merely_stored():
    """A `default` workspace backed by the environment is genuinely
    configured; telling it otherwise sends somebody to fix a working thing."""
    from src.api.routers import webhooks

    assert "resolve_webhook_secret(" in _code(webhooks.list_webhook_setup)


def test_the_setup_endpoint_is_authenticated_and_workspace_scoped():
    from src.api.routers import webhooks

    for fn in (webhooks.list_webhook_setup, webhooks.rotate_secret):
        body = _code(fn)
        assert "current_workspace_id" in body
        assert "get_current_user" in body
