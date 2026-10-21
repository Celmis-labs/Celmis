"""A name that is ordinary can be a second way to reach a secret or an example env file; none of them reads.

THE DEFECT. Two things decided how a reader treated a file by the name it was ASKED FOR:
  * the code reader masked an example env file (`.env.example`, shown with its values
    withheld) only when the requested name was an example name, so an in-repo symlink
    `docs/envlink.txt -> ../.env.example` came back with every real value in it;
  * `plain_file` refused a symlink but nothing refused a HARD link, and `safe_join` (the
    code reader, the exploration agent, citations) refused neither: `hl_notes.txt`, a hard
    link to `.env`, read fine, and the plain-text search walked it.

THE CONTRACT. `safe_join` refuses a file that IS a symlink and a regular file that has a
second name (git never makes either in a clone; an upload with a link is refused at the
door). A reader that masks classifies the file it RESOLVED to, whichever name it was given.
Everything reads the same: a refusal looks like a missing file.
"""

from __future__ import annotations

import os

import pytest

from src.security import secret_files as sf

VALUE = "hunter2-REAL-VALUE-a91c"
ENV_EXAMPLE = f"API_KEY={VALUE}\nHOST=localhost\n"


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "acme-app"
    (root / "docs").mkdir(parents=True)
    (root / "src").mkdir()
    (root / ".env.example").write_text(ENV_EXAMPLE)
    (root / ".env").write_text(f"API_KEY={VALUE}\n")
    (root / "src/app.py").write_text("def main():\n    return 1\n")
    return root


def _readers(repo, rel: str) -> dict[str, object]:
    from src.mcp_server.howto import engine
    from src.qa.exploration_agent import ExplorationAgent
    from src.retrieval.tier3_code import CodeReader

    agent = ExplorationAgent.__new__(ExplorationAgent)
    agent.repo_path = repo
    reader = CodeReader()
    full = reader.read_full_file(repo, rel, redact_content=False)
    part = reader.read_locations(repo, [(rel, 1, 2)], redact_content=False).snippets
    return {
        "code reader (whole file)": full.content if full else None,
        "code reader (locations)": part[0].content if part else None,
        "howto": engine._read(repo, rel),
        "exploration agent": agent._tool_grep_in_file({"file": rel, "pattern": "."}),
    }


@pytest.mark.parametrize("target", [".env.example", ".env"])
def test_a_symlink_to_an_example_env_or_a_secret_reveals_no_value_through_any_reader(repo, target):
    os.symlink(f"../{target}", repo / "docs/envlink.txt")
    os.symlink("docs/envlink.txt", repo / "chain.txt")

    for rel in ("docs/envlink.txt", "chain.txt"):
        for reader, shown in _readers(repo, rel).items():
            assert VALUE not in str(shown), (reader, rel)
        assert sf.plain_file(repo, rel) is None
        with pytest.raises(sf.SecretPathRefused):
            sf.safe_join(repo, rel)


@pytest.mark.parametrize("target", [".env.example", ".env"])
def test_a_hard_link_to_an_example_env_or_a_secret_is_refused_by_every_reader(repo, target):
    from src.indexing.graph.languages.factory import walk_repo_files
    from src.qa import text_search

    os.link(repo / target, repo / "hl_notes.txt")

    for reader, shown in _readers(repo, "hl_notes.txt").items():
        assert VALUE not in str(shown), reader
    assert sf.content_verdict(repo, "hl_notes.txt") == "deny"
    assert sf.classify_file(repo, "hl_notes.txt") == "deny"
    assert sf.plain_file(repo, "hl_notes.txt") is None
    with pytest.raises(sf.SecretPathRefused):
        sf.safe_join(repo, "hl_notes.txt")
    assert "hl_notes.txt" not in {rel for rel, _ in text_search.iter_text_files(repo)}
    assert "hl_notes.txt" not in {p.name for p in walk_repo_files(repo)}


def test_a_source_file_with_a_second_name_is_refused_too_whatever_its_extension_says(repo):
    """The extension decides what is READ for the sniff, never whether a second name is fine."""
    os.link(repo / ".env", repo / "innocent.py")

    assert sf.classify_file(repo, "innocent.py") == "deny"
    assert sf.plain_file(repo, "innocent.py") is None


def test_a_link_to_a_directory_is_followed_and_the_file_is_judged_by_where_it_lands(repo):
    """The only link that stays readable: a directory link, whose target is classified as itself."""
    (repo / "cfg").mkdir()
    (repo / "cfg/.env.example").write_text(ENV_EXAMPLE)
    (repo / "cfg/.env").write_text(f"X={VALUE}\n")
    os.symlink("cfg", repo / "settings")

    masked = _readers(repo, "settings/.env.example")
    refused = _readers(repo, "settings/.env")

    for reader, shown in {**masked, **refused}.items():
        assert VALUE not in str(shown), reader
    assert masked["code reader (whole file)"] is not None and "API_KEY=[value withheld]" in masked["code reader (whole file)"]
    assert refused["code reader (whole file)"] is None


def test_a_reader_masks_by_the_file_it_resolved_to_not_the_name_it_was_asked_for(repo, monkeypatch):
    """Defence in depth. Were a link ever to get past `safe_join`, the readers that mask are handed
    the REAL path of the example file under an ordinary name and must still withhold its values."""
    from src.mcp_server.howto import engine
    from src.retrieval import tier3_code

    real = (repo / ".env.example").resolve()
    monkeypatch.setattr(tier3_code, "_safe_path", lambda _r, _rel: real)
    monkeypatch.setattr(engine, "plain_file", lambda _r, _rel: real)

    shown = tier3_code.CodeReader().read_full_file(repo, "notes.txt", redact_content=False)
    located = tier3_code.CodeReader().read_locations(repo, [("notes.txt", 1, 2)], redact_content=False)

    assert shown is not None and VALUE not in shown.content and "API_KEY=[value withheld]" in shown.content
    assert VALUE not in located.snippets[0].content
    assert VALUE not in (engine._read(repo, "notes.txt") or "")
    assert sf.reading_verdict(repo, "notes.txt", real) == "keys_only"
    assert sf.reading_verdict(repo, "src/app.py", repo / "src/app.py") == "ok"
    assert sf.reading_verdict(repo, "notes.txt", repo / ".env") == "deny"
