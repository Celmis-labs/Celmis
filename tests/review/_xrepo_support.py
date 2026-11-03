"""Shared fixtures for tests that run the breaking-change agent."""

from __future__ import annotations

import difflib
from types import SimpleNamespace

from src.config import get_settings
from src.groups import impact
from src.indexing.graph.xrepo import (
    CallFact,
    FactSet,
    ImportCall,
    ImportFact,
    PublishedPackage,
    RouteFact,
)
from src.review.diff import parse_unified_diff
from src.review.models import PullRequest

APP = '''from fastapi import FastAPI

app = FastAPI()


@app.post("/invoices/{id}/pay")
async def pay(id: str):
    return {"id": id}
'''

CLIENT = '''def pay_invoice(invoice_id, amount):
    return {"invoice": invoice_id, "amount": amount}
'''

BILLING = FactSet(
    routes=[RouteFact("POST", "/invoices/{}/pay", "app/main.py", 7, "pay", "fastapi")],
    published=[PublishedPackage("pypi", "acme-billing-client", "pyproject.toml",
                                ["acme_billing_client"])],
)
CONSUMER = FactSet(
    calls=[CallFact("POST", "/invoices/{}/pay", "shop/checkout.py", 11, "billing-service", "",
                    "httpx", "checkout")],
    imports=[ImportFact("acme_billing_client.client", ["pay_invoice"], "shop/checkout.py", 6, "pypi",
                        [ImportCall("pay_invoice", 14)])],
)


def serve_facts(monkeypatch, by_slug: dict[str, FactSet | None]) -> list[str]:
    """Make `load_facts` answer from `by_slug`; returns the slugs it was asked for."""
    settings = get_settings()
    by_path = {str(settings.repo_graph_path(s)): f for s, f in by_slug.items()}
    asked: list[str] = []

    def load(path):  # noqa: ANN001, ANN202
        for slug in by_slug:
            if str(path) == str(settings.repo_graph_path(slug)):
                asked.append(slug)
                return by_path[str(path)]
        raise AssertionError(f"facts of an unexpected repository were read: {path}")

    monkeypatch.setattr(impact, "load_facts", load)
    return asked


def pull_request(changes: dict[str, tuple[str, str]], repo: str = "acme/billing-service"):
    diff = ""
    for path, (old, new) in changes.items():
        diff += f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        diff += "".join(list(difflib.unified_diff(
            old.splitlines(True), new.splitlines(True), n=3))[2:])
    hunks, _ = parse_unified_diff(diff)
    pr = PullRequest(provider="bitbucket", repo=repo, number=7, title="Change billing",
                     description="", author="dev", base_ref="main", base_sha="base",
                     head_ref="feat", head_sha="head", state="open",
                     url="https://git.example.com/acme/billing-service/pull/7",
                     hunks=hunks, raw_diff=diff)
    files = {("base", p): o for p, (o, n) in changes.items()} | \
        {("head", p): n for p, (o, n) in changes.items()}
    provider = SimpleNamespace(read_file_at=lambda r, ref, path: files.get((ref, path)))
    return pr, provider
