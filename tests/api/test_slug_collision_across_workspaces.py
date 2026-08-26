"""Two repositories, two workspaces, one vault directory.

`ParsedRepo.slug` flattens the owner path with a dash, so
gitlab.com/acme/group/billing and gitlab.com/acme-group/billing are different
repositories that produce one identical slug. The 1:1 guard at registration
compared `(provider, full_name)`, and those full names genuinely differ — so it
never fired.

Everything downstream is slug-keyed and global: the clone, the graph, the
vault. `_vault_dir()` checks that the SLUG belongs to the caller's workspace,
which it does, and hands back the shared directory. Workspace B could read
workspace A's documentation through GET /api/docs/{slug}/note and rewrite it
through the regenerate endpoint added this session.

The fix is at registration, because that is the only place it can be closed
without a migration: refuse the second repository rather than let two tenants
share a directory.
"""

from __future__ import annotations

from pathlib import Path

from src.sync.git_providers import parse_repo_url

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"


def test_the_collision_is_real():
    """Pinned so the fix is never removed as unnecessary. If slug generation
    ever becomes injective this test says so and the guard can go."""
    a = parse_repo_url("https://gitlab.com/acme/group/billing")
    b = parse_repo_url("https://gitlab.com/acme-group/billing")
    assert f"{a.owner}/{a.name}" != f"{b.owner}/{b.name}", "different repos"
    assert a.slug == b.slug, (
        "slugs no longer collide — if that is deliberate, this guard and the "
        "registration check it protects can both be removed"
    )


def test_the_full_name_guard_cannot_see_it():
    """Why the existing check was not enough: it compares the names, and the
    names are different. Stated here so nobody removes the slug check as a
    duplicate of it."""
    a = parse_repo_url("https://gitlab.com/acme/group/billing")
    b = parse_repo_url("https://gitlab.com/acme-group/billing")
    assert f"{a.owner}/{a.name}" != f"{b.owner}/{b.name}"


def test_registration_refuses_a_slug_another_workspace_holds():
    repos = (SRC / "api" / "routers" / "repos.py").read_text(encoding="utf-8")
    assert "existing_slug_binding(" in repos, (
        "registration no longer checks the slug, so two tenants can share a "
        "vault directory again"
    )
    # 409, like the full_name guard beside it — a conflict, not a permission
    # error, because the caller has done nothing wrong.
    idx = repos.index("existing_slug_binding(")
    assert "409" in repos[idx:idx + 700] or "HTTP_409_CONFLICT" in repos[idx:idx + 700]


def test_the_store_can_answer_the_question():
    from src.api.auto_review import AutoReviewStore

    assert hasattr(AutoReviewStore, "existing_slug_binding")


def test_the_error_says_what_to_do():
    """"Conflict" alone leaves somebody staring at a URL that looks unique to
    them — the collision is invisible unless the message explains it."""
    repos = (SRC / "api" / "routers" / "repos.py").read_text(encoding="utf-8")
    idx = repos.index("existing_slug_binding(")
    message = repos[idx:idx + 900]
    assert "local name" in message
    assert "documentation" in message or "vault" in message
