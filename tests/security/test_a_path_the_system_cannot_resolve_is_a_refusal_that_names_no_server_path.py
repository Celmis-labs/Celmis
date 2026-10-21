"""An over-long name or a link loop is "not found", never an exception that carries the clone's path.

THE DEFECT. `safe_join` let the operating system's error through (`ENAMETOOLONG` for a segment longer
than 255 bytes, `ELOOP` for a link loop). Its callers caught only `SecretPathRefused`, so the exception
climbed out of `CodeReader.read_full_file` / `read_locations` and the exploration agent's tool, whose
text is `[Errno 36] File name too long: '/srv/celmis/workspace/repos/acme-app/aaaa...'` - the absolute
path of the clone on the server - and the model chooses the file name.

THE CONTRACT. Any failure to resolve a path is a `SecretPathRefused` with the one generic message, chained
to nothing, and every reader answers as for a missing file.
"""

from __future__ import annotations

import os

import pytest

from src.security import secret_files as sf

LONG = "a" * 300


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "srv-clone-path" / "acme-app"
    root.mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src/app.py").write_text("def main():\n    return 1\n")
    return root


def _os_rejects_it(root) -> bool:
    try:
        os.lstat(root / LONG)
    except OSError as exc:
        return exc.errno in {36, 63}  # ENAMETOOLONG (Linux, macOS)
    return False


@pytest.mark.parametrize("rel", [LONG, f"src/{LONG}", f"{LONG}/x.py", f"src/{LONG}/../app.py", "src/" + "b" * 5000])
def test_safe_join_refuses_a_name_the_system_cannot_open_with_the_generic_message(repo, rel):
    if not _os_rejects_it(repo):
        pytest.skip("this filesystem accepts a 300-byte name")

    with pytest.raises(sf.SecretPathRefused) as exc:
        sf.safe_join(repo, rel)

    assert str(exc.value) == sf.REFUSED
    assert str(repo.parent) not in repr(exc.value) and exc.value.__cause__ is None
    assert exc.value.__suppress_context__, "the OSError (and its path) is not chained to the refusal"
    assert sf.plain_file(repo, rel) is None


def test_every_reader_answers_a_missing_file_for_an_over_long_name(repo):
    from src.mcp_server.howto import engine
    from src.qa.exploration_agent import ExplorationAgent
    from src.retrieval.tier3_code import CodeReader

    if not _os_rejects_it(repo):
        pytest.skip("this filesystem accepts a 300-byte name")
    agent = ExplorationAgent.__new__(ExplorationAgent)
    agent.repo_path = repo
    reader = CodeReader()

    assert reader.read_full_file(repo, f"src/{LONG}") is None
    assert reader.read_locations(repo, [(f"src/{LONG}", 1, 3), ("src/app.py", 1, 2)]).snippets[0].file == "src/app.py"
    assert engine._read(repo, LONG) is None
    answer = agent._tool_grep_in_file({"file": LONG, "pattern": "x"})
    assert answer == {"error": f"not_found: {LONG}"} and str(repo.parent) not in str(answer)


def test_a_link_loop_and_a_nul_byte_are_refusals_too(repo):
    os.symlink("loop_b", repo / "loop_a")
    os.symlink("loop_a", repo / "loop_b")

    for rel in ("loop_a", "loop_b/x.py", "src/a\x00b.py"):
        with pytest.raises(sf.SecretPathRefused) as exc:
            sf.safe_join(repo, rel)
        assert str(exc.value) == sf.REFUSED
    assert sf.plain_file(repo, "loop_a") is None
