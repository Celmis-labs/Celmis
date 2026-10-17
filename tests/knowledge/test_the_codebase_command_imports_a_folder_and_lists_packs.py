"""`analyzer codebase import|list` — a folder or a zip into a pack, from the shell.

The contract (docs/CODEBASE.md, "Command line"):

  * `import` reads the database like `mcp issue-token`; it creates the pack if
    the slug is new and otherwise adds the next version, and prints ONE line of
    counts — never a file's text;
  * the folder is checked exactly as a web upload is: a secret file or a secret
    inside a file is refused, naming file and line and never the value, and
    nothing is written;
  * without `--as` it acts as the operator; with `--as` the person's own standing
    decides — a member who may not create packs gets a refusal (the same sentence a
    hidden pack gets, so the refusal does not tell which slugs exist), an editor may;
  * `list` prints slug, audience, version and file count for every pack,
    restricted ones included, because whoever runs it can read the database.
"""

from __future__ import annotations

import secrets

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from src.cli import app
from tests.api.rbac_world import MASTER_EMAIL, _sqlite_booleans

runner = CliRunner()
A = "wsid-a"
TEAM = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def shell(tmp_path, monkeypatch):
    """A database file with one workspace, one team and two people, and the
    process settings the command reads."""
    import src.db.session as session_mod
    import src.users.store as users_mod
    from src.db.models import Base, Team, Workspace, WorkspaceMember
    from src.users import User

    db = tmp_path / "cli.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", MASTER_EMAIL)
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(session_mod, "_engine", None)
    monkeypatch.setattr(session_mod, "_session_factory", None)

    original = session_mod.init_engine

    def with_sqlite_booleans(url=None, *, echo=False):
        engine = original(url, echo=echo)
        event.listen(engine.sync_engine, "connect", _sqlite_booleans)
        return engine

    monkeypatch.setattr(session_mod, "init_engine", with_sqlite_booleans)

    store = users_mod.UserStore(tmp_path / "users.db")
    monkeypatch.setattr(users_mod, "_default_store", store)
    for name, email in (("editor", "editor@acme-corp.io"), ("member", "member@acme-corp.io")):
        store.create(User(id=f"u-{name}", email=email, name=name))

    engine = create_engine(f"sqlite:///{db}")
    event.listen(engine, "connect", _sqlite_booleans)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Workspace(id=A, name="Alpha", slug="ws-a", description=""))
        s.add(WorkspaceMember(workspace_id=A, user_id="u-editor", role="editor"))
        s.add(WorkspaceMember(workspace_id=A, user_id="u-member", role="member"))
        s.add(Team(id=TEAM, name="Platform", description="", workspace_id=A))
        s.commit()

    class Shell:
        def run(self, *args):
            return runner.invoke(app, ["codebase", *args])

        def rows(self, model, **where):
            with Session(engine) as s:
                q = select(model)
                for key, value in where.items():
                    q = q.where(getattr(model, key) == value)
                return list(s.scalars(q).all())

        def count(self, model):
            with Session(engine) as s:
                return s.scalar(select(func.count()).select_from(model))

    yield Shell()
    engine.dispose()


def _folder(root, **files):
    for path, text in files.items():
        target = root / path.replace("__", "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return root


def _good(tmp_path):
    return _folder(tmp_path / "kit", **{
        "README.md": "# Kit\n",
        "billing__SKILL.md": "---\nname: billing\ndescription: Raise an invoice\n---\n# Billing\n\nSteps.\n",
        "notes__why.md": "# Why\n\nBecause.\n"})


def test_a_folder_becomes_a_new_pack_with_one_line_of_counts(shell, tmp_path):
    from src.db.models import KnowledgeItem, KnowledgePack

    r = shell.run("import", str(_good(tmp_path)), "-w", "ws-a", "-p", "kit", "--title", "Kit",
                  "--audience", "workspace", "--note", "first")
    assert r.exit_code == 0, r.output
    assert "pack=kit created version=1 files=3 skills=1 ignored=0" in r.output
    [pack] = shell.rows(KnowledgePack)
    assert (pack.slug, pack.title, pack.audience, pack.created_by_id) == ("kit", "Kit", "workspace", "cli")
    assert shell.count(KnowledgeItem) == 3
    assert "Because" not in r.output and "Steps" not in r.output


def test_importing_again_adds_the_next_version_to_the_same_pack(shell, tmp_path):
    from src.db.models import KnowledgePack, KnowledgePackVersion

    kit = _good(tmp_path)
    assert shell.run("import", str(kit), "-w", "ws-a", "-p", "kit").exit_code == 0
    (kit / "notes" / "more.md").write_text("# More\n\nAnd more.\n")
    r = shell.run("import", str(kit), "-w", "wsid-a", "-p", "kit")
    assert r.exit_code == 0 and "pack=kit updated version=2 files=4" in r.output, r.output
    assert shell.count(KnowledgePack) == 1 and shell.count(KnowledgePackVersion) == 2


def test_a_zip_is_taken_like_a_folder(shell, tmp_path):
    import zipfile

    archive = tmp_path / "kit.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("README.md", "# Kit\n")
        zf.writestr("notes/n.md", "# N\n\nbody\n")
    r = shell.run("import", str(archive), "-w", "ws-a", "-p", "zipped")
    assert r.exit_code == 0 and "version=1 files=2" in r.output, r.output


def test_teams_are_granted_by_name_or_id_with_a_permission(shell, tmp_path):
    from src.db.models import KnowledgePackTeam

    r = shell.run("import", str(_good(tmp_path)), "-w", "ws-a", "-p", "kit", "--audience", "restricted",
                  "--team", "platform:maintain")
    assert r.exit_code == 0, r.output
    [grant] = shell.rows(KnowledgePackTeam)
    assert (grant.team_id, grant.permission) == (TEAM, "maintain")
    bad = shell.run("import", str(_good(tmp_path)), "-w", "ws-a", "-p", "other", "--team", "nonesuch")
    assert bad.exit_code == 1 and "no such team" in bad.output


def test_a_secret_is_refused_by_place_and_nothing_is_written(shell, tmp_path):
    from src.db.models import KnowledgePack

    canary = secrets.token_urlsafe(14)
    kit = _folder(tmp_path / "kit", **{
        "README.md": "# Kit\n",
        "api__setup.md": f"# Setup\n\nCall it like this:\n\npassword = '{canary}'\n"})
    r = shell.run("import", str(kit), "-w", "ws-a", "-p", "kit")
    assert r.exit_code == 1
    assert "secrets_found" in r.output and "api/setup.md:5" in r.output
    assert canary not in r.output
    assert shell.count(KnowledgePack) == 0


def test_a_secret_file_and_a_missing_path_are_refused(shell, tmp_path):
    kit = _folder(tmp_path / "kit", **{"README.md": "# Kit\n", ".env": "A=1\n", "id_rsa": "x\n"})
    r = shell.run("import", str(kit), "-w", "ws-a", "-p", "kit")
    assert r.exit_code == 1 and "secret_file" in r.output
    gone = shell.run("import", str(tmp_path / "nope"), "-w", "ws-a", "-p", "kit")
    assert gone.exit_code == 1 and "Traceback" not in gone.output


def test_an_unknown_workspace_and_a_bad_slug_are_one_line_refusals(shell, tmp_path):
    kit = _good(tmp_path)
    r = shell.run("import", str(kit), "-w", "nowhere", "-p", "kit")
    assert r.exit_code == 1 and "no such workspace" in r.output
    r = shell.run("import", str(kit), "-w", "ws-a", "-p", "Not A Slug!")
    assert r.exit_code == 1 and "slug" in r.output and "Traceback" not in r.output


def test_as_a_person_their_own_standing_decides(shell, tmp_path):
    from src.db.models import KnowledgePack

    kit = _good(tmp_path)
    r = shell.run("import", str(kit), "-w", "ws-a", "-p", "kit", "--as", "member@acme-corp.io")
    assert r.exit_code == 1 and "pack not found or not accessible" in r.output   # one answer for "may not create"
    assert shell.count(KnowledgePack) == 0

    r = shell.run("import", str(kit), "-w", "ws-a", "-p", "kit", "--as", "editor@acme-corp.io")
    assert r.exit_code == 0, r.output
    [pack] = shell.rows(KnowledgePack)
    assert pack.created_by_id == "u-editor"

    # the member can see the pack's audience (teams, no grant: they cannot) so cannot add to it
    again = shell.run("import", str(kit), "-w", "ws-a", "-p", "kit", "--as", "member@acme-corp.io")
    assert again.exit_code == 1 and "Traceback" not in again.output
    nobody = shell.run("import", str(kit), "-w", "ws-a", "-p", "kit", "--as", "ghost@acme-corp.io")
    assert nobody.exit_code == 1 and "no such user" in nobody.output


def test_list_prints_every_pack_with_audience_version_and_files(shell, tmp_path):
    assert "no packs" in shell.run("list", "-w", "ws-a").output
    assert shell.run("import", str(_good(tmp_path)), "-w", "ws-a", "-p", "kit",
                     "--audience", "restricted").exit_code == 0
    r = shell.run("list", "-w", "ws-a")
    assert r.exit_code == 0, r.output
    assert "kit\trestricted\tv1\t3" in r.output
    assert shell.run("list", "-w", "nowhere").exit_code == 1


def test_the_command_is_documented_in_the_help():
    r = runner.invoke(app, ["codebase", "--help"])
    assert r.exit_code == 0 and "import" in r.output and "list" in r.output
    r = runner.invoke(app, ["codebase", "import", "--help"])
    assert r.exit_code == 0
    for flag in ("--workspace", "--pack", "--audience", "--team", "--as", "--note"):
        assert flag in r.output, flag
