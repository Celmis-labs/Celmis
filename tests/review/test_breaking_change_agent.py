"""The breaking-change agent end to end: contract change -> consumers -> findings."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.mcp_server import tenancy, tools
from src.review import breaking_change
from src.review.models import FindingSeverity

from ._xrepo_support import APP, BILLING, CLIENT, CONSUMER, pull_request, serve_facts

SLUG = "acme-billing-service"  # bitbucket: no provider prefix


@pytest.fixture(autouse=True)
def workspace(monkeypatch):
    from src.deployment import reset_mode_cache

    monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", "multi_tenant")
    reset_mode_cache()
    monkeypatch.setattr(tools, "list_repo_slugs",
                        lambda: [SLUG, "acme-web-shop", "rival-app"])
    monkeypatch.setattr(tenancy, "workspace_slugs",
                        lambda ws: {SLUG, "acme-web-shop"} if ws == "ws-acme" else {"rival-app"})
    monkeypatch.setattr("src.notifications.notify", lambda **kw: None)
    yield
    reset_mode_cache()


def _run(changes, monkeypatch, facts=None):
    asked = serve_facts(monkeypatch, facts or {
        SLUG: BILLING, "acme-web-shop": CONSUMER, "rival-app": CONSUMER})
    pr, provider = pull_request(changes)
    ctx = SimpleNamespace(pull_request=pr, workspace_id="ws-acme", file_provider=provider)
    return breaking_change.run_breaking_change(ctx), asked


def test_a_renamed_parameter_and_a_moved_route_name_their_consumers(monkeypatch) -> None:
    out, _ = _run({
        "acme_billing_client/client.py": (CLIENT, CLIENT.replace("invoice_id", "invoice_ref")),
        "app/main.py": (APP, APP.replace('"/invoices/{id}/pay"', '"/v2/invoices/{id}/settle"')),
    }, monkeypatch)
    assert len(out.findings) == 2
    assert all(f.severity == FindingSeverity.WARNING for f in out.findings)
    bodies = "\n".join(f.body for f in out.findings)
    assert "acme-web-shop" in bodies and "shop/checkout.py:11" in bodies
    assert "shop/checkout.py:14" in bodies
    assert "http" in bodies and "import" in bodies
    assert all(f.rule_id.startswith("bc.strong.") for f in out.findings)
    assert breaking_change.has_strong_consumers(out.findings)


def test_a_body_only_change_has_no_findings(monkeypatch) -> None:
    out, asked = _run({
        "acme_billing_client/client.py": (CLIENT, CLIENT.replace("return {", "total = 1\n    return {")),
        "app/main.py": (APP, APP.replace('return {"id": id}', 'return {"id": id, "ok": True}')),
    }, monkeypatch)
    assert out.findings == [] and out.error is None
    assert asked == []  # nothing changed in the contract, nobody was asked


def test_a_new_optional_parameter_is_not_breaking(monkeypatch) -> None:
    out, _ = _run({"acme_billing_client/client.py": (
        CLIENT, CLIENT.replace("amount):", "amount, memo=None):"))}, monkeypatch)
    assert out.findings == []


def test_a_newly_required_parameter_is_breaking(monkeypatch) -> None:
    out, _ = _run({"acme_billing_client/client.py": (
        CLIENT, CLIENT.replace("amount):", "amount, currency):"))}, monkeypatch)
    (f,) = out.findings
    assert "currency" in f.body.lower() or "required" in f.body.lower()


def test_a_name_shared_with_unrelated_code_is_not_a_consumer(monkeypatch) -> None:
    """The old agent matched on the bare name; a repository that merely has a
    function called `pay_invoice` of its own is not a consumer."""
    other = CONSUMER.__class__(imports=[], calls=[])
    out, _ = _run({"acme_billing_client/client.py": (
        CLIENT, CLIENT.replace("invoice_id", "invoice_ref"))}, monkeypatch,
        facts={SLUG: BILLING, "acme-web-shop": other, "rival-app": other})
    assert out.findings == []


def test_a_weak_link_never_raises_severity(monkeypatch) -> None:
    weak = CONSUMER.__class__(calls=[CONSUMER.calls[0].__class__(
        "POST", "/invoices/{}/pay", "shop/checkout.py", 11, "", "", "httpx", "")])
    out, _ = _run({"app/main.py": (APP, APP.replace('"/invoices/{id}/pay"', '"/v2/pay/{id}"'))},
                  monkeypatch, facts={SLUG: BILLING, "acme-web-shop": weak, "rival-app": weak})
    (f,) = out.findings
    assert f.severity == FindingSeverity.INFO
    assert f.rule_id.startswith("bc.weak.")
    assert not breaking_change.has_strong_consumers(out.findings)


def test_unindexed_repositories_are_reported_not_treated_as_clean(monkeypatch) -> None:
    out, _ = _run({"acme_billing_client/client.py": (
        CLIENT, CLIENT.replace("invoice_id", "invoice_ref"))}, monkeypatch,
        facts={SLUG: BILLING, "acme-web-shop": None, "rival-app": CONSUMER})
    # nothing known, nothing claimed - and the gap is said, not hidden
    assert [f.rule_id for f in out.findings] == ["bc.unchecked"]


def test_severity_scales_with_strong_consumers() -> None:
    assert breaking_change.severity_for(1) == FindingSeverity.WARNING
    assert breaking_change.severity_for(2) == FindingSeverity.ERROR
    assert breaking_change.severity_for(5) == FindingSeverity.CRITICAL


# ─── review findings: missing facts must not read as "no consumers" ───


def test_a_removed_public_function_in_an_unindexed_workspace_says_so(monkeypatch) -> None:
    out, _ = _run({"acme_billing_client/client.py": (CLIENT, "")}, monkeypatch,
                  facts={SLUG: None, "acme-web-shop": None, "rival-app": None})
    (f,) = out.findings
    assert f.rule_id == "bc.unchecked" and f.severity == FindingSeverity.INFO
    assert "1 repository not checked" in f.title and "no recorded facts" in f.title
    assert out.coverage["changes"] == 1 and out.coverage["unchecked"] == 1
    assert not breaking_change.has_strong_consumers(out.findings)


def test_an_unchecked_repository_is_noted_even_when_nothing_else_was_found(monkeypatch) -> None:
    out, _ = _run({"acme_billing_client/client.py": (CLIENT, CLIENT.replace("invoice_id", "invoice_ref"))},
                  monkeypatch, facts={SLUG: BILLING, "acme-web-shop": None, "rival-app": None})
    assert [f.rule_id for f in out.findings] == ["bc.unchecked"]


def test_a_failed_lookup_is_counted_not_swallowed(monkeypatch) -> None:
    from src.groups import impact

    serve_facts(monkeypatch, {SLUG: BILLING, "acme-web-shop": CONSUMER, "rival-app": CONSUMER})

    def boom(**kw):  # noqa: ANN003, ANN202
        raise RuntimeError("graph busy")

    monkeypatch.setattr(impact, "consumers_of_symbols", boom)
    pr, provider = pull_request(
        {"acme_billing_client/client.py": (CLIENT, CLIENT.replace("invoice_id", "invoice_ref"))})
    out = breaking_change.run_breaking_change(
        SimpleNamespace(pull_request=pr, workspace_id="ws-acme", file_provider=provider))
    assert out.coverage["failures"] == 1
    assert [f.rule_id for f in out.findings] == ["bc.unchecked"]
    assert "lookup(s) failed" in out.findings[0].title


def test_no_contract_change_means_no_coverage_note(monkeypatch) -> None:
    out, _ = _run({"acme_billing_client/client.py": (CLIENT, CLIENT.replace("return {", "x = 1\n    return {"))},
                  monkeypatch, facts={SLUG: None, "acme-web-shop": None, "rival-app": None})
    assert out.findings == []


def test_the_workspace_facts_are_loaded_once_per_review(monkeypatch) -> None:
    out, asked = _run({
        "acme_billing_client/client.py": (CLIENT, CLIENT.replace("invoice_id", "invoice_ref")),
        "app/main.py": (APP, APP.replace('"/invoices/{id}/pay"', '"/v2/invoices/{id}/settle"')),
    }, monkeypatch)
    assert len(out.findings) == 2
    assert sorted(asked) == sorted(set(asked))  # no repository was read twice


def test_the_status_says_what_was_not_checked_instead_of_none_found() -> None:
    from src.review.models import PullRequest, ReviewBatch
    from src.review.orchestrator import ReviewOrchestrator

    sent: list[dict] = []

    class Poster:
        def post_commit_status(self, pr, **kw):  # noqa: ANN001, ANN003, ANN201
            sent.append(kw)
            from src.review.providers.base import StatusResult

            return StatusResult(True, kw["state"])

    class Stages:
        def begin(self, *a, **k): ...  # noqa: ANN002, ANN003, ANN202
        def end(self, *a, **k): ...  # noqa: ANN002, ANN003, ANN202

    pr = PullRequest(provider="github", repo="a/b", number=1, title="t", description="",
                     author="u", base_ref="m", base_sha="b", head_ref="f", head_sha="h", state="open")
    batch = ReviewBatch(pull_request=pr)
    batch.breaking_change_checked = True
    batch.breaking_change_incomplete = "2 repositories not checked"
    ReviewOrchestrator._post_breaking_change_status(Poster(), batch, checked=True, stages=Stages())
    assert sent[0]["state"] == "success"
    assert "2 repositories not checked" in sent[0]["description"]
    assert "No breaking change" not in sent[0]["description"]
