"""Files the review pipeline reads BY NAME from a clone are judged like every other reader's.

THE DEFECT. Four places opened a file of a repository by its well-known name with a plain
`read_text` or `exists()`, which follows a link: the review's style guide (`CLAUDE.md`,
`.cursorrules`, `CONTRIBUTING.md`) and the vault overview, the rule importer's convention
files, the source sample the rule generator hands the model, the entry files of the
architecture summary, and the vault excerpt (`rglob("*.md")`). A repository anybody can
push to only has to commit `CLAUDE.md -> /etc/hosts`, or `README.md -> ../../other-clone/.env`,
and the server's own file is in the prompt the model reads and quotes. The same readers
listed secret-named files in the file tree they show.

THE CONTRACT. None of them reads or shows

  * a symlink (even one that stays inside the clone, or points at `.env`),
  * a file with a second hard link,
  * a secret-named file's content, or its name in a file tree,
  * a vault note that is a link out of the vault or has a secret name;

an ordinary file still is read, and when the first candidate is refused the next candidate
is tried (a linked `CLAUDE.md` does not hide a real `CONTRIBUTING.md`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config import Settings

REPO = "acme-app"
OUTSIDE = "OUTSIDE_SERVER_FILE_MARKER_77e"
SECRET = "NOT_A_REAL_TOKEN_MARKER_8f3a"
FOREIGN = "FOREIGN_TENANT_NOTE_MARKER_d02"
OWN = "OWN_NOTE_MARKER_61c"
GUIDE = "Use tabs. Prefer small functions. GUIDE_MARKER_5a1"


@pytest.fixture()
def world(tmp_path: Path, monkeypatch):
    settings = Settings(workspace_dir=tmp_path / "ws", vault_dir=tmp_path / "vault")
    monkeypatch.setattr("src.config.get_settings", lambda: settings)
    repo = settings.repo_path(REPO)
    repo.mkdir(parents=True)
    outside = tmp_path / "outside.md"                        # a file of the server, not of the clone
    outside.write_text(f"## Testing\nEvery change must come with a test. {OUTSIDE}\n" + "x" * 400,
                       encoding="utf-8")
    (repo / ".env").write_text(f"API_TOKEN={SECRET}\n", encoding="utf-8")
    return settings, repo, outside


def _link(target: Path | str, at: Path) -> None:
    try:
        at.symlink_to(target)
    except OSError:  # pragma: no cover - filesystem without symlinks
        pytest.skip("symlinks unavailable")


def _style_guide() -> str:
    from src.review.orchestrator import ReviewOrchestrator

    pr = SimpleNamespace(local_slug=REPO)
    return ReviewOrchestrator._load_style_guide(object.__new__(ReviewOrchestrator), pr)


def _overview() -> str:
    from src.review.orchestrator import ReviewOrchestrator

    pr = SimpleNamespace(local_slug=REPO)
    return ReviewOrchestrator._load_repo_overview(object.__new__(ReviewOrchestrator), pr)


# ─── the review's style guide ────────────────────────────────────────


def test_a_style_guide_of_the_repository_is_read_and_cut_at_three_thousand_characters(world):
    _settings, repo, _outside = world
    (repo / "CLAUDE.md").write_text(GUIDE + "\n" + "y" * 5000, encoding="utf-8")

    got = _style_guide()

    assert got.startswith(GUIDE) and len(got) == 3000


def test_a_style_guide_that_is_a_link_to_a_file_of_the_server_is_not_read(world):
    _settings, repo, outside = world
    _link(outside, repo / "CLAUDE.md")

    assert _style_guide() == ""


def test_a_style_guide_that_is_a_link_to_the_env_file_is_not_read(world):
    _settings, repo, _outside = world
    _link(".env", repo / "CLAUDE.md")

    got = _style_guide()

    assert got == "" and SECRET not in got


def test_a_refused_style_guide_does_not_hide_the_next_one(world):
    _settings, repo, outside = world
    _link(outside, repo / "CLAUDE.md")
    (repo / "CONTRIBUTING.md").write_text(GUIDE, encoding="utf-8")

    assert _style_guide() == GUIDE


def test_a_style_guide_that_is_a_second_name_of_the_env_file_is_not_read(world):
    _settings, repo, _outside = world
    try:
        os.link(repo / ".env", repo / ".cursorrules")
    except OSError:  # pragma: no cover - filesystem without hard links
        pytest.skip("hard links unavailable")

    assert _style_guide() == ""


def test_no_style_guide_at_all_is_an_empty_string(world):
    assert _style_guide() == ""


# ─── the vault overview ──────────────────────────────────────────────


def test_the_overview_of_the_repos_own_vault_is_read(world):
    settings, _repo, _outside = world
    vault = settings.repo_vault_path(REPO)
    vault.mkdir(parents=True)
    (vault / "_overview.md").write_text(f"# Overview\n{OWN}\n", encoding="utf-8")

    assert OWN in _overview()


def test_an_overview_that_is_a_link_to_another_repos_note_or_a_server_file_is_not_read(world):
    settings, _repo, outside = world
    vault = settings.repo_vault_path(REPO)
    other = settings.repo_vault_path("other-tenant-repo")
    vault.mkdir(parents=True)
    other.mkdir(parents=True)
    (other / "secret_note.md").write_text(f"{FOREIGN}\n", encoding="utf-8")

    _link(other / "secret_note.md", vault / "_overview.md")
    assert _overview() == ""

    (vault / "_overview.md").unlink()
    _link(outside, vault / "_overview.md")
    assert _overview() == ""


# ─── the rule importer and the rule generator ────────────────────────


def test_a_convention_file_that_is_a_link_proposes_nothing_and_a_real_one_still_does(world):
    from src.review.rules_generate import scan_convention_files

    _settings, repo, outside = world
    _link(outside, repo / "CONTRIBUTING.md")
    (repo / "CLAUDE.md").write_text("## Testing\n- Run pytest before pushing; always use the fixtures.\n",
                                    encoding="utf-8")

    found = scan_convention_files(repo)

    assert [r["source_ref"] for r in found] == ["CLAUDE.md#testing"]
    assert OUTSIDE not in str(found)


def test_a_convention_file_that_is_a_link_to_a_folder_of_the_server_proposes_nothing(world, tmp_path):
    from src.review.rules_generate import scan_convention_files

    _settings, repo, _outside = world
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "rules").mkdir(parents=True)
    (elsewhere / "rules" / "api.mdc").write_text(
        f"---\ndescription: Handlers validate input\n---\nEvery handler must validate. {OUTSIDE}\n",
        encoding="utf-8")
    (repo / ".cursor").mkdir()
    _link(elsewhere / "rules", repo / ".cursor" / "rules")

    assert scan_convention_files(repo) == []


def _source(marker: str) -> str:
    return f"# {marker}\n" + "def total(lines):\n    return sum(l.amount for l in lines)\n" * 12


def test_the_source_sample_for_the_rule_generator_leaves_out_links_and_secret_names(world, tmp_path):
    from src.review.rules_generate import build_repo_digest

    _settings, repo, _outside = world
    (repo / "app" / "billing").mkdir(parents=True)
    (repo / "app" / "billing" / "invoice.py").write_text(_source("INVOICE_MARKER"), encoding="utf-8")
    server_file = tmp_path / "server_module.py"
    server_file.write_text(_source(OUTSIDE), encoding="utf-8")
    _link(server_file, repo / "app" / "peek.py")
    _link(repo / ".env", repo / "app" / "settings_link.py")

    digest, stats = build_repo_digest(REPO, root=repo)

    assert "INVOICE_MARKER" in digest, "an ordinary source file is still sampled"
    assert OUTSIDE not in digest and SECRET not in digest
    assert "app/peek.py" not in stats["sampled"] and "app/settings_link.py" not in stats["sampled"]
    assert "- .env" not in digest, "the secret floor hides the name from the file tree too"


def test_the_vault_excerpt_for_the_rule_generator_reads_only_the_repos_own_notes(world):
    from src.review.rules_generate import _vault_excerpt

    settings, _repo, outside = world
    vault = settings.repo_vault_path(REPO)
    other = settings.repo_vault_path("other-tenant-repo")
    (vault / "modules").mkdir(parents=True)
    (vault / "secrets").mkdir()
    other.mkdir(parents=True)
    (vault / "overview.md").write_text(f"# Overview\n{OWN}\n", encoding="utf-8")
    (vault / ".env.md").write_text(f"{SECRET}\n", encoding="utf-8")
    (vault / "secrets" / "db.md").write_text(f"{SECRET}\n", encoding="utf-8")
    (other / "private.md").write_text(f"{FOREIGN}\n", encoding="utf-8")
    _link(other / "private.md", vault / "modules" / "to_other.md")
    _link(outside, vault / "modules" / "to_server.md")

    excerpt, count = _vault_excerpt(REPO)

    assert OWN in excerpt and count == 1
    for marker in (FOREIGN, OUTSIDE, SECRET):
        assert marker not in excerpt


# ─── the architecture summary ────────────────────────────────────────


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_the_architecture_summary_reads_no_entry_file_that_is_a_link_or_a_secret(world):
    from src.review.architecture import _collect_context

    _settings, repo, outside = world
    (repo / "main.py").write_text("print('ENTRY_MARKER')\n", encoding="utf-8")
    _link(outside, repo / "README.md")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)

    context = _collect_context(REPO, max_files=8, max_bytes=20_000)

    assert "ENTRY_MARKER" in context, "an ordinary entry file is still excerpted"
    assert OUTSIDE not in context and SECRET not in context
    assert "- .env" not in context, "the secret floor hides the name from the file tree too"
