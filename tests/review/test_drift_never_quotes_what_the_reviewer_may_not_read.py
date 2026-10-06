"""Cross-repo drift greps sibling repositories and quotes a line of each into a
review comment. A quote is a read: it must come from files the reviewer may see,
in repositories the reviewer may read, with credentials masked."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.review.cross_repo_drift import _grep_repo, _readable_siblings

VALUE = "payments-gateway-v2"
SECRET = "Zx9Qm4Tp7Wv2Lk8Rb5Nc3Hd6Fj1Ys0A"  # made up


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "sibling"
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def test_a_credential_file_in_a_sibling_is_never_quoted(tmp_path):
    root = _repo(tmp_path, {
        "config/.env.production": f"GATEWAY={VALUE}\nTOKEN={SECRET}\n",
        "deploy/vault-password.txt": f"{VALUE} {SECRET}\n",
        "keys/server.pem": f"{VALUE}\n",
        "app/settings.py": f'GATEWAY = "{VALUE}"\n',
    })
    got = _grep_repo(VALUE, root, "acme/shop")
    assert [m.file for m in got] == ["app/settings.py"]


def test_a_secret_on_the_line_that_matches_is_masked_in_the_quote(tmp_path):
    root = _repo(tmp_path, {
        "app/settings.py": f'GATEWAY = "{VALUE}"; DB_PASSWORD = "{SECRET}"\n'})
    got = _grep_repo(VALUE, root, "acme/shop")
    assert got and SECRET not in got[0].excerpt and VALUE in got[0].excerpt


def test_a_person_with_no_grant_gets_no_siblings_to_grep(monkeypatch):
    import src.access.effective as eff
    import src.users.store as store

    user = SimpleNamespace(id="u1", is_admin=False)
    monkeypatch.setattr(store, "get_user_store",
                        lambda: SimpleNamespace(get_by_id=lambda _id: user))
    decisions = {
        "github_acme-open": SimpleNamespace(code_visible=True),
        "github_acme-closed": SimpleNamespace(code_visible=False),
    }
    monkeypatch.setattr(eff, "effective_access", lambda *a, **k: decisions)
    got = _readable_siblings(["github_acme-open", "github_acme-closed", "github_acme-gone"],
                             "ws1", "u1")
    assert got == ["github_acme-open"]


def test_a_run_with_no_person_keeps_the_members_of_the_group():
    slugs = ["github_acme-a", "github_acme-b"]
    assert _readable_siblings(slugs, "ws1", None) == slugs
    assert _readable_siblings(slugs, "ws1", "default") == slugs


def test_when_access_cannot_be_decided_nothing_is_greped(monkeypatch):
    import src.access.effective as eff
    import src.users.store as store

    monkeypatch.setattr(store, "get_user_store",
                        lambda: SimpleNamespace(get_by_id=lambda _id: SimpleNamespace(
                            id="u1", is_admin=False)))

    def boom(*a, **k):
        raise RuntimeError("database down")

    monkeypatch.setattr(eff, "effective_access", boom)
    assert _readable_siblings(["github_acme-a"], "ws1", "u1") == []


def test_a_reviewer_who_no_longer_exists_reads_nothing(monkeypatch):
    import src.users.store as store

    monkeypatch.setattr(store, "get_user_store",
                        lambda: SimpleNamespace(get_by_id=lambda _id: None))
    assert _readable_siblings(["github_acme-a"], "ws1", "gone") == []
