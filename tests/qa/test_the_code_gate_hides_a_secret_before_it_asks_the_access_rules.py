"""The retriever's code gate refuses a secret file FIRST, whatever the access rules say.

THE CONTRACT. `MultiRepoRetriever._read_code_for_hits` turns a path into prompt
text through one gate (`_allowed`). A secret path (`secrets.yaml`, anything under
`secrets/`) is refused before the access decision is looked at: for everyone, an
owner and a caller with no decision at all (`access=None`) included, and without
being recorded in `hidden_files`. That last part is what this file holds to
account: `hidden_files` becomes the boundary notice in the prompt ("some files
are hidden by the access policy") and is returned with the answer, so a secret
file that was merely *denied by a glob* (instead of refused by the floor) would
announce that it exists, and a repository with the floor alone would announce
nothing at all.

WHY THE TEST WAS MISSING. Deleting the check from `_allowed` passed the whole
ask suite: a later `safe_join` inside `_read_one` refuses the same file, so the
content never reached the prompt, and no test looked at what the gate reports.
Here a module walk visits a directory that holds `secrets.yaml`, and the result
must not name it in `files_read`, in `hidden_files` or in the prompt notice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.access.resolver import RepoAccessDecision, _RuleView
from src.config import Settings
from src.qa.multi_repo_retriever import MultiRepoRetriever, MultiRepoVaultHit

SLUG = "acme-billing"
NORMAL = "NORMAL_MARKER_5c1"
IN_YAML = "YAML_MARKER_a93f"
IN_DIR = "DIR_MARKER_27d0"
IN_INTERNAL = "INTERNAL_MARKER_8e44"

#: The deny globs the access rules put on the secret files, as an operator would.
DENY = ("**/secrets.yaml", "**/secrets/**")


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    repo = tmp_path / "repos" / SLUG
    files = {
        "billing/service.py": f"def charge():\n    return '{NORMAL}'\n",
        "billing/secrets.yaml": f"api_key: {IN_YAML}\n",
        "billing/secrets/ledger.py": f"TOKEN = '{IN_DIR}'\n",
        "billing/internal.py": f"def audit():\n    return '{IN_INTERNAL}'\n",
    }
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return Settings(workspace_dir=tmp_path)


def _module_hit() -> MultiRepoVaultHit:
    return MultiRepoVaultHit(
        note_path="modules/billing.md", score=1.0, type="module", module="billing",
        repo=SLUG, symbols=[], keywords=[], content="", path="billing")


def _decision(deny: tuple[str, ...]) -> RepoAccessDecision:
    return RepoAccessDecision(
        repo_slug=SLUG, visibility="code", open_default=False,
        rules=(_RuleView("code", (), deny, ()),), deny_globs=deny)


def _walk(settings: Settings, access):
    retriever = MultiRepoRetriever(settings=settings, vault_ret=None)
    files, bundle, hidden = retriever._read_code_for_hits([_module_hit()], access=access)
    notice = retriever._build_access_notice(
        denied_repos=[], related_repos=[], hidden_files=hidden,
        access=access or {}, no_accessible=False)
    return files, bundle, hidden, notice


@pytest.mark.parametrize("who", ["a rule that denies the secret by glob", "a caller with no access decision at all",
                                 "an unrestricted caller"])
def test_a_secret_file_is_never_read_listed_or_announced(settings, who):
    access = {
        "a rule that denies the secret by glob": {SLUG: _decision(DENY)},
        "a caller with no access decision at all": None,
        "an unrestricted caller": {SLUG: RepoAccessDecision.full(SLUG)},
    }[who]

    files, bundle, hidden, notice = _walk(settings, access)

    assert f"{SLUG}/billing/service.py" in files and NORMAL in bundle, "an ordinary file is still read"
    everything = "\n".join([*files, bundle, *hidden, notice])
    assert "secrets" not in everything, "neither the name of a secret file nor of its folder"
    assert IN_YAML not in everything and IN_DIR not in everything
    assert hidden == [] and notice == "", "a file the floor refused is not 'hidden by the access policy'"


def test_a_file_hidden_by_a_rule_is_still_announced_so_the_floor_is_the_only_silent_one(settings):
    """The control: a NON-secret file under a deny glob is recorded and announced, as before."""
    access = {SLUG: _decision((*DENY, "billing/internal.py"))}

    files, bundle, hidden, notice = _walk(settings, access)

    assert hidden == [f"{SLUG}/billing/internal.py"]
    assert "hidden by the access policy" in notice
    assert IN_INTERNAL not in bundle and f"{SLUG}/billing/internal.py" not in files
    assert "secrets" not in "\n".join([*hidden, notice])
