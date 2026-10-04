"""Generating and importing rules writes PROPOSALS — nothing a review applies
until a person approves it.

Generate is driven with a stand-in model and a temporary repository: what the
model is shown (the repository's files, its documentation), how it is booked
(operation "rules_generate"), the second chance an unreadable reply gets and
no more, and what lands in the store (pending, origin "generated", rationale
and examples kept, an existing title not proposed again).

Import reads real convention files from a temporary repository, with no
model at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.review import rules_generate, rules_store
from src.review.rules_generate import (
    RulesJobError,
    generate_rules,
    import_rules,
    parse_rules_reply,
    scan_convention_files,
)
from tests.review.rules_db import rules_db


class _Model:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    def generate(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(text=self.replies.pop(0))


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "app" / "billing").mkdir(parents=True)
    (root / "app" / "billing" / "invoice.py").write_text(
        "def total(lines, discounts=[]):\n" + "    return sum(l.amount for l in lines)\n" * 20)
    (root / "app" / "api.py").write_text("import requests\n" * 40)
    (root / "README.md").write_text("# Billing\n")
    return root


_REPLY = json.dumps({"rules": [
    {"title": "Money is Decimal, never float", "instructions": "Flag float amounts.",
     "severity": "error", "path_glob": "app/billing/**/*.py",
     "rationale": "invoice.py sums amounts", "examples_good": "Decimal('1.10')",
     "examples_bad": "1.1"},
    {"title": "Existing one", "instructions": "dup", "severity": "warning"},
    {"title": "", "instructions": "no title, dropped"},
    {"title": "Bad severity becomes warning", "instructions": "x", "severity": "huge"},
]})


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setattr(rules_generate, "repo_root", lambda slug: root)
    monkeypatch.setattr(rules_generate, "_vault_excerpt",
                        lambda slug, budget=0: ("### overview.md\nThe billing service.", 1))
    return root


async def test_generate_writes_pending_proposals(tmp_path, monkeypatch, repo):
    async with rules_db(tmp_path, monkeypatch):
        await rules_store.create_rule("ws", repo_slug=None, title="Existing one",
                                      instructions="already here")
        model = _Model(_REPLY)
        out = await generate_rules("ws", "acme_billing", "ed@x.io", llm=model, count=5)

        call = model.calls[0]
        assert call["operation"] == "rules_generate"
        assert "invoice.py" in call["code_context"]
        assert "The billing service." in call["code_context"]
        assert "Existing one" in call["prompt"], "the model is told what exists"

        # The empty title is refused; the workspace's own title is not proposed
        # again for the repository.
        assert len(out["created"]) == 2 and out["skipped"] == 2
        rows = await rules_store.list_rules("ws", "acme_billing")
        assert {r["status"] for r in rows} == {"pending"}
        assert {r["origin"] for r in rows} == {"generated"}
        money = next(r for r in rows if r["title"].startswith("Money"))
        assert money["rationale"] == "invoice.py sums amounts"
        assert money["examples_bad"] == "1.1" and money["severity"] == "error"
        assert money["path_glob"] == "app/billing/**/*.py"
        assert next(r for r in rows if r["title"].startswith("Bad"))["severity"] == "warning"
        # Nothing generated reaches a review before approval…
        applied = rules_store.load_active_rules_sync("ws", "acme_billing")
        assert [r["title"] for r in applied] == ["Existing one"]
        # …and an approved proposal does.
        await rules_store.set_status("ws", [money["id"]], "active", "ed@x.io")
        applied = rules_store.load_active_rules_sync("ws", "acme_billing")
        assert [r["title"] for r in applied] == ["Existing one", money["title"]]


async def test_an_unreadable_reply_gets_one_more_call_and_no_more(tmp_path, monkeypatch, repo):
    async with rules_db(tmp_path, monkeypatch):
        model = _Model("Sure! Here are some rules:", _REPLY)
        out = await generate_rules("ws", "acme_billing", None, llm=model)
        assert len(model.calls) == 2 and out["created"]
        assert "could not be parsed" in model.calls[1]["prompt"]

        model = _Model("no", "still no")
        with pytest.raises(RulesJobError, match="twice"):
            await generate_rules("ws", "acme_billing", None, llm=model)
        assert len(model.calls) == 2


async def test_no_clone_and_no_docs_is_said(tmp_path, monkeypatch):
    monkeypatch.setattr(rules_generate, "repo_root", lambda slug: None)
    monkeypatch.setattr(rules_generate, "_vault_excerpt", lambda slug, budget=0: ("", 0))
    async with rules_db(tmp_path, monkeypatch):
        with pytest.raises(RulesJobError, match="index it"):
            await generate_rules("ws", "acme_none", None, llm=_Model(_REPLY))
        with pytest.raises(RulesJobError, match="index it"):
            await import_rules("ws", "acme_none", None)


def test_the_reply_parser_takes_the_shapes_models_write():
    rules = [{"title": "a", "instructions": "b"}]
    assert parse_rules_reply(json.dumps(rules)) == rules
    assert parse_rules_reply("```json\n" + json.dumps({"rules": rules}) + "\n```") == rules
    assert parse_rules_reply("prose " + json.dumps({"rules": rules}) + " trailing") == rules
    assert parse_rules_reply("nothing here") is None


# ─── import ──────────────────────────────────────────────────────────


def _conventions(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "CONTRIBUTING.md").write_text(
        "# Contributing\n\n## License\nMIT, you must keep it.\n\n"
        "## Testing\nEvery change must come with a test. Never mock the database.\n\n"
        "## Contact\nWrite to us.\n")
    (root / "CLAUDE.md").write_text(
        "## Testing\n- Run pytest before pushing; always use the fixtures.\n")
    rules_dir = root / ".cursor" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "api.mdc").write_text(
        "---\ndescription: API handlers validate input\nglobs: src/api/**/*.py\n---\n"
        "Every handler must validate its payload with a pydantic model.\n")
    (root / ".editorconfig").write_text(
        "root = true\n\n[*]\nend_of_line = lf\n\n[*.py]\nindent_size = 4\n")
    (root / ".eslintrc.json").write_text(json.dumps(
        {"extends": ["eslint:recommended"], "rules": {"eqeqeq": "error", "no-var": "off"}}))
    (root / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 100\n[tool.ruff.lint]\nselect = ['E', 'B']\n")
    (root / "phpcs.xml").write_text('<ruleset><rule ref="PSR12"/></ruleset>')


def test_the_convention_files_are_read_into_rules(tmp_path):
    root = tmp_path / "repo"
    _conventions(root)
    found = scan_convention_files(root)
    by_ref = {r["source_ref"]: r for r in found}

    assert "CONTRIBUTING.md#testing" in by_ref
    assert "Never mock the database" in by_ref["CONTRIBUTING.md#testing"]["instructions"]
    assert not any(ref.startswith("CONTRIBUTING.md#license") for ref in by_ref)
    assert not any(ref.startswith("CONTRIBUTING.md#contact") for ref in by_ref)
    # Two "Testing" sections: the second is told apart by its file.
    assert by_ref["CLAUDE.md#testing"]["title"] == "Testing (CLAUDE.md)"

    cursor = by_ref[".cursor/rules/api.mdc"]
    assert cursor["title"] == "API handlers validate input"
    assert cursor["path_glob"] == "src/api/**/*.py"

    assert by_ref[".editorconfig#all"]["path_glob"] is None
    assert "end_of_line = lf" in by_ref[".editorconfig#all"]["instructions"]
    assert by_ref[".editorconfig#py"]["path_glob"] == "*.py"

    eslint = by_ref[".eslintrc.json"]["instructions"]
    assert "eqeqeq (error)" in eslint and "no-var" not in eslint
    assert "line-length = 100" in by_ref["pyproject.toml"]["instructions"]
    assert "PSR12" in by_ref["phpcs.xml"]["instructions"]
    for rule in found:
        rules_store.validate_rule({**rule, "agents": []})


async def test_import_is_pending_and_a_rerun_adds_nothing(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    _conventions(root)
    async with rules_db(tmp_path, monkeypatch):
        first = await import_rules("ws", "acme_app", "ed@x.io", root=root)
        assert first["created"] and ".editorconfig" in first["files"]
        rows = await rules_store.list_rules("ws", "acme_app")
        assert {(r["status"], r["origin"]) for r in rows} == {("pending", "imported")}
        assert all(r["source_ref"] for r in rows)

        # A rejected proposal is not proposed again either.
        await rules_store.set_status("ws", [rows[0]["id"]], "rejected", "ed@x.io")
        again = await import_rules("ws", "acme_app", "ed@x.io", root=root)
        assert again["created"] == [] and again["skipped"] == len(first["created"])


async def test_a_job_records_its_outcome(tmp_path, monkeypatch, repo):
    async with rules_db(tmp_path, monkeypatch):
        job, created = await rules_generate.start_job("ws", "acme_billing", "generate",
                                                      "ed@x.io")
        assert created and job["status"] == "queued"
        same, created_again = await rules_generate.start_job(
            "ws", "acme_billing", "generate", "ed@x.io")
        assert same["id"] == job["id"] and not created_again, "one live job per repo"

        await rules_generate.run_job(job["id"], llm=_Model(_REPLY))
        done = await rules_generate.get_job("ws", job["id"])
        assert done["status"] == "completed" and len(done["result"]["created"]) == 3
        assert await rules_generate.get_job("other-ws", job["id"]) is None

        failing, _ = await rules_generate.start_job("ws", "acme_billing", "generate", None)
        await rules_generate.run_job(failing["id"], llm=_Model("x", "y"))
        failed = await rules_generate.get_job("ws", failing["id"])
        assert failed["status"] == "failed" and "twice" in failed["error"]
