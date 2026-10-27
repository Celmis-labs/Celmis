"""`grep` reads every repository the caller can read, one batch of `MAX_REPOS_PER_CALL` per page.

An agent once read "searched 10 of 39 repos" as "the index covers 10 of 39" and gave up
on Celmis. The contract (docs/MCP_DEV.md "Limits per token"):

  * up to 41 repositories per call; 39 readable ones are searched in one call, with no note;
  * more than that: page 1 searches repositories 1-41, says "(a cut of this call, not the index)" and that the
    cursor continues; the cursor goes through the hits left in the batch, then into the next
    41 repositories, so a walk meets every repository once and every hit once;
  * shared repositories come first; `repo=` is never batched;
  * a cursor is bound to its query; a repository that appeared, vanished or was re-indexed
    between two pages is said (page 1 again), never a crash, a gap or a repeat;
  * `ask` keeps its own, smaller limit (it is paid), and the tool description states the cap.
"""

from __future__ import annotations

import re

import pytest

from src.mcp_server.dev_profile import common, cursor, freshness, git_io, tools_ask, tools_grep
from tests.mcp_server.dev.conftest import SHOP

HITS_PER_REPO = 3
CURSOR_RE = re.compile(r"cursor=(\S+)")
SEARCHED_RE = re.compile(r"^searched repositories (\d+)-(\d+) of (\d+) \(a cut of this call, not the index\); "
                         r"cursor=<next> continues with the next (\d+); repo= targets one$")


@pytest.fixture
def fleet(as_caller, monkeypatch):
    """`fleet(n)`: n readable repositories with a clone each that only exist as names.
    Returns `(slugs, shas, searched)`: `shas` can be edited to re-index, `searched`
    records the repositories `git grep` was asked about."""
    def make(n: int, *, hits: int = HITS_PER_REPO):
        slugs = [f"github_acme-svc{i:03d}" for i in range(n)]
        shas = {s: f"{i + 1:040x}" for i, s in enumerate(slugs)}
        searched: list[str] = []
        as_caller({s: "code" for s in slugs})
        monkeypatch.setattr(common, "fresh_entries", lambda sl: [
            freshness.RepoFresh(s, "main", shas[s], "1h", "fresh") for s in sl if s in shas])
        monkeypatch.setattr(git_io, "has_commit", lambda slug, sha: True)

        def fake_grep(slug, sha, pat, **kw):
            searched.append(slug)
            return [git_io.GrepHit(f"src/m{j}.py", j + 1, f"x = {pat}  # {slug}")
                    for j in range(hits)]

        monkeypatch.setattr(git_io, "grep", fake_grep)

        def broken_store(_slug):
            raise RuntimeError("no graph in this test")

        monkeypatch.setattr(common, "open_store", broken_store)
        return slugs, shas, searched

    return make


def _hit_keys(out: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for ln in out.splitlines()
            if (m := re.match(r"^(\S+) (src/m\d+\.py:\d+)\s", ln))]


def _cursor(out: str) -> str:
    last = out.splitlines()[-1]
    m = CURSOR_RE.search(last)
    return m.group(1) if m else ""


def _walk(pattern: str = "needle", **kw) -> list[str]:
    pages, cur = [], ""
    for _ in range(60):
        out = tools_grep.run(pattern, cursor_=cur, **kw)
        pages.append(out)
        cur = _cursor(out)
        if not cur:
            return pages
    raise AssertionError("the cursor never ended")


def test_39_readable_repositories_are_all_searched_in_one_call_with_no_note(fleet):
    slugs, _shas, searched = fleet(39, hits=1)

    out = tools_grep.run("needle", limit=60)

    assert sorted(searched) == slugs
    assert "searched" not in out and "cursor=" not in out
    assert len({k[0] for k in _hit_keys(out)}) == 39


def test_100_repositories_page_1_searches_1_to_41_and_hands_out_a_cursor(fleet):
    slugs, _shas, searched = fleet(100, hits=1)

    out = tools_grep.run("needle", limit=60)

    assert sorted(searched) == slugs[:41]
    notes = [m for ln in out.splitlines() if (m := SEARCHED_RE.match(ln))]
    assert [m.groups() for m in notes] == [("1", "41", "100", "41")]
    assert _cursor(out), "the answer ends with the cursor of the next repositories"
    assert "repositories 42-82 of 100 not searched yet" in out.splitlines()[-1]


def test_following_the_cursors_meets_every_repository_once_and_every_hit_once(fleet):
    slugs, _shas, searched = fleet(100)          # 41 * 3 hits per batch, 20 per page: many pages

    pages = _walk(limit=20)

    keys = [k for p in pages for k in _hit_keys(p)]
    assert len(keys) == len(set(keys)) == 100 * HITS_PER_REPO, "no hit twice, none missing"
    assert {k[0] for k in keys} == set(slugs)
    assert set(searched) == set(slugs), "every repository was searched (a page re-reads its batch)"
    ranges = [m.groups()[:3] for p in pages for ln in p.splitlines()
              if (m := SEARCHED_RE.match(ln) or re.match(
                  r"^searched repositories (\d+)-(\d+) of (\d+) \(a cut of this call, not the index\); "
                  r"this is the last part", ln))]
    assert ranges[0] == ("1", "41", "100") and ranges[-1] == ("83", "100", "100")
    assert {r[0] for r in ranges} == {"1", "42", "83"}


def test_page_2_of_a_batch_carries_the_batch_so_the_note_names_the_right_range(fleet):
    fleet(100)

    first = tools_grep.run("needle", limit=20)
    second = tools_grep.run("needle", limit=20, cursor_=_cursor(first))

    assert "searched repositories 1-41 of 100" in first
    assert "searched repositories 1-41 of 100" in second
    assert _hit_keys(first)[0] not in _hit_keys(second)


def test_shared_repositories_are_read_first(fleet, monkeypatch):
    slugs, _shas, searched = fleet(60, hits=1)
    last = slugs[-1]
    from src.mcp_server.dev_profile import access

    monkeypatch.setattr(access.DevScope, "is_shared", lambda self, s: s == last)

    tools_grep.run("needle")

    assert last in searched, "the shared one is in batch 1 although it sorts last"


def test_repo_names_one_repository_and_is_never_batched(fleet):
    slugs, _shas, searched = fleet(100, hits=1)

    out = tools_grep.run("needle", repo=slugs[77])

    assert searched == [slugs[77]]
    assert "searched" not in out and "cursor=" not in out


def test_a_cursor_for_another_pattern_is_rejected_and_page_1_is_shown(fleet):
    fleet(100, hits=1)
    cur = _cursor(tools_grep.run("needle", limit=60))
    assert cursor.decode(cur)["b"] == 41

    out = tools_grep.run("other", cursor_=cur, limit=60)

    assert cursor.MISMATCH_NOTE in out
    assert "searched repositories 1-41 of 100" in out, "batch 1 again, not the 42nd repository"


def test_a_tampered_batch_start_is_refused(fleet):
    fleet(100, hits=1)
    cur = _cursor(tools_grep.run("needle", limit=60))
    head, off, s, _b, r = cur.split(".")

    out = tools_grep.run("needle", cursor_=f"{head}.{off}.{s}.7.{r}", limit=60)

    assert cursor.REPOS_MOVED_NOTE in out
    assert "searched repositories 1-41 of 100" in out


def test_a_repository_that_vanished_before_the_batch_is_said_not_skipped(fleet, as_caller):
    slugs, _shas, searched = fleet(100, hits=1)
    cur = _cursor(tools_grep.run("needle", limit=60))
    as_caller({s: "code" for s in slugs[1:]})             # the first repository disappears
    searched.clear()

    out = tools_grep.run("needle", cursor_=cur, limit=60)

    assert cursor.REPOS_MOVED_NOTE in out
    assert "searched repositories 1-41 of 99" in out and slugs[1] in searched


def test_a_repository_re_indexed_between_pages_of_a_batch_is_said_and_page_1_is_shown(fleet):
    slugs, shas, _searched = fleet(100)
    first = tools_grep.run("needle", limit=20)
    cur = _cursor(first)
    shas[slugs[5]] = "f" * 40                                # pushed and re-indexed

    out = tools_grep.run("needle", cursor_=cur, limit=20)

    assert cursor.STALE_NOTE in out
    assert _hit_keys(out) == _hit_keys(first), "page 1 again, not a skewed page 2"


def test_a_re_indexed_repository_in_an_earlier_batch_does_not_matter_to_the_next_one(fleet):
    slugs, shas, searched = fleet(100, hits=1)
    cur = _cursor(tools_grep.run("needle", limit=60))
    shas[slugs[5]] = "f" * 40
    searched.clear()

    out = tools_grep.run("needle", cursor_=cur, limit=60)

    assert cursor.STALE_NOTE not in out and cursor.REPOS_MOVED_NOTE not in out
    assert "searched repositories 42-82 of 100" in out and sorted(searched) == slugs[41:82]


def test_a_garbage_cursor_is_page_1(fleet):
    fleet(100, hits=1)

    out = tools_grep.run("needle", cursor_="nonsense.1.2.3.4.5.6", limit=60)

    assert cursor.MISMATCH_NOTE in out and "searched repositories 1-41 of 100" in out


def test_ask_keeps_its_small_paid_limit_and_the_cap_is_41():
    assert tools_ask.MAX_REPOS == 8
    assert common.MAX_REPOS_PER_CALL == 41
    assert not hasattr(tools_grep, "MAX_REPOS")


def test_the_tool_description_states_the_cap_the_cursor_and_repo(monkeypatch):
    import asyncio

    monkeypatch.setenv("MCP_JWT_SECRET", "x" * 48)

    from src.mcp_server.dev_profile import build_dev_mcp

    tools = asyncio.run(build_dev_mcp().list_tools())
    text = next(t.description for t in tools if t.name == "grep")
    assert "41 repos per call" in text and len(text) <= 200
    assert "cursor pages hits, then the next repos" in text
    assert "Unindexed ones are named" in text
    assert "repo= targets one" in text


def test_a_shop_repository_with_a_real_clone_is_still_searched(as_caller, world):
    """No fakes: the batch machinery works on the real fixture repositories too."""
    out = tools_grep.run("create_order")

    assert SHOP in out and "searched" not in out


def test_a_repository_without_an_index_is_named_not_called_indexed(fleet, monkeypatch):
    slugs, shas, _searched = fleet(5)
    del shas[slugs[2]]

    out = tools_grep.run("needle")

    assert "1 of these have no index yet, nothing searched: " + slugs[2] in out
    assert "all are indexed" not in out


def test_a_git_grep_that_timed_out_is_named(fleet, monkeypatch):
    slugs, _shas, _searched = fleet(3)

    def slow(slug, sha, pat, **kw):
        if slug == slugs[1]:
            kw["timed_out"].append(slug)
        return []

    monkeypatch.setattr(git_io, "grep", slow)

    out = tools_grep.run("needle")

    assert f"1 repos timed out or were skipped" in out and slugs[1] in out


def test_the_call_deadline_skips_and_names_the_rest(fleet, monkeypatch):
    slugs, _shas, searched = fleet(3)
    monkeypatch.setattr(tools_grep, "CALL_DEADLINE", -1.0)

    out = tools_grep.run("needle")

    assert not searched and "3 repos timed out or were skipped" in out


def test_a_cursor_of_another_caller_is_not_accepted(fleet, monkeypatch):
    fleet(60)
    page1 = tools_grep.run("needle", limit=5)
    cur = _cursor(page1)
    same = tools_grep.run("needle", limit=5, cursor_=cur)
    assert "does not belong" not in same

    class Other:
        user_id, workspace_id = "someone-else", "other-ws"

    real = tools_grep.common.begin()
    other = real.__class__(**{**real.__dict__, "user_id": "someone-else", "workspace_id": "other-ws"})
    monkeypatch.setattr(tools_grep.common, "begin", lambda: other)
    assert "does not belong to this query" in tools_grep.run("needle", limit=5, cursor_=cur)


def test_a_stale_cursor_in_a_later_part_says_it_shows_the_first_page_of_that_part(fleet):
    slugs, shas, _s = fleet(100, hits=2)
    out = tools_grep.run("needle", limit=60)
    for _ in range(2):  # the rest of part 1, then the first page of part 2
        out = tools_grep.run("needle", limit=60, cursor_=_cursor(out))
    cur = _cursor(out)
    shas[slugs[50]] = "f" * 40
    out = tools_grep.run("needle", limit=60, cursor_=cur)
    assert "showing the first page of this part" in out


def test_no_hits_over_a_full_part_keeps_the_idx_header_short(fleet):
    fleet(41, hits=0)

    out = tools_grep.run("needle")

    assert len(out.splitlines()[0]) < 1200
