"""`outline`, `read_symbol`, `grep`, `refs` and `map` read the committed text."""

from __future__ import annotations

import re
import subprocess

from src.mcp_server.dev_contract import HIT_RE
from src.mcp_server.dev_profile import tools_grep, tools_read, tools_refs
from tests.mcp_server.dev.conftest import BILLING, SHOP

ORDERS = "app/services/orders.py"


def _git_show(world, slug: str, path: str) -> list[str]:
    root, shas = world
    out = subprocess.run(
        ["git", "-C", str(root / "repos" / slug) if (root / "repos").exists() else str(root / slug),
         "show", f"{shas[slug]}:{path}"], capture_output=True, text=True, check=True)
    return out.stdout.split("\n")


def _body(out: str) -> list[str]:
    parts = out.split("\n---\n")
    return parts[1].split("\n") if len(parts) > 1 else []


def test_read_symbol_prints_exactly_the_lines_of_the_symbol_at_the_indexed_commit(as_caller, world):
    out = tools_read.read_symbol(SHOP, "OrderService")
    m = re.search(rf"{SHOP} {ORDERS}:(\d+)-(\d+) class", out)
    assert m
    start, end = int(m[1]), int(m[2])
    expected = _git_show(world, SHOP, ORDERS)[start - 1:end]
    assert _body(out)[:len(expected)] == expected


def test_a_long_symbol_keeps_its_head_and_tail_and_reports_the_elided_count(as_caller, world):
    out = tools_read.read_symbol(SHOP, "long_report", max_lines=20)
    head = _git_show(world, SHOP, ORDERS)
    assert "def long_report(rows):" in out and "return out" in out
    m = re.search(r"… (\d+) lines elided", out)
    assert m
    shown = [ln for ln in _body(out) if ln and ln != "---" and not ln.startswith("…")]
    assert len(shown) + int(m[1]) == 123
    assert "Read app/services/orders.py:33-135" in out
    assert head[32] == "    out.append(rows[8])"  # the first elided line, per git show
    assert "out.append(rows[8])" not in out


def test_max_lines_widens_the_slice_up_to_the_hard_limit(as_caller, world):
    out = tools_read.read_symbol(SHOP, "long_report", max_lines=500)
    assert "elided" not in out
    assert len([ln for ln in _body(out) if ln.startswith("    out.append")]) == 120


def test_a_class_member_is_addressed_as_class_dot_method(as_caller, world):
    out = tools_read.read_symbol(SHOP, "OrderService.cancel_order")
    assert "def cancel_order(self, order_id)" in out
    assert "def create_order(self" not in out


def test_an_ambiguous_name_prints_the_best_match_and_lists_the_others(as_caller, world):
    out = tools_read.read_symbol(SHOP, "create_order")
    assert f"{SHOP} {ORDERS}:19-20 function create_order" in out
    assert "also in: app/services/orders.py:10, vendor/lib.py:1" in out


def test_a_path_argument_picks_one_of_several_same_named_symbols(as_caller, world):
    out = tools_read.read_symbol(SHOP, "create_order", path="vendor/lib.py")
    assert "def create_order(x)" in out


def test_read_symbol_lists_callers_and_callees_of_the_symbol_it_printed(as_caller, world):
    out = tools_read.read_symbol(SHOP, "create_order", path=ORDERS)
    assert "callers(2): post_order app/api/orders.py:4" in out
    assert "callees(1): cancel_order" in out


def test_include_none_leaves_the_neighbour_lines_out(as_caller, world):
    out = tools_read.read_symbol(SHOP, "create_order", path=ORDERS, include="")
    assert "callers(" not in out


def test_an_unknown_symbol_gets_a_next_step_not_a_stack_trace(as_caller, world):
    out = tools_read.read_symbol(SHOP, "does_not_exist")
    assert "Traceback" not in out and "find" in out


def test_outline_of_a_file_nests_methods_under_their_class_with_line_ranges(as_caller, world):
    out = tools_read.outline(SHOP, ORDERS)
    assert re.search(r"^\d+-\d+ class OrderService", out, re.M)
    assert re.search(r"^  \d+-\d+ method create_order", out, re.M)


def test_outline_of_a_directory_counts_files_and_symbols_per_subdirectory(as_caller, world):
    out = tools_read.outline(SHOP, "app")
    assert "app/services/" in out and "app/api/" in out


def test_map_orders_directories_by_how_much_code_they_hold_and_names_top_symbols(as_caller, world):
    out = tools_read.repo_map(SHOP)
    lines = out.split("\n")
    assert lines[2].startswith("app/services/") and "top: create_order" in lines[2]


def test_map_of_a_tiny_budget_is_still_a_valid_answer(as_caller, world):
    out = tools_read.repo_map(SHOP, budget_tokens=60)
    assert out.startswith("idx: ")


# ─── grep ────────────────────────────────────────────────────────────

def test_grep_hits_have_the_contract_shape_and_the_matching_text(as_caller, world):
    out = tools_grep.run("RETRY_LIMIT", repo=SHOP)
    rows = [ln for ln in out.split("\n")[1:] if re.match(HIT_RE, ln)]
    assert any("config/settings.yaml:2" in r and "RETRY_LIMIT: 3" in r for r in rows)


def test_grep_ranks_source_above_docs_and_names_the_enclosing_symbol(as_caller, world):
    out = tools_grep.run("order_id", repo=SHOP)
    assert "cancel_order" in out


def test_grep_regex_mode_treats_the_pattern_as_a_regular_expression(as_caller, world):
    out = tools_grep.run(r"RETRY_L[A-Z]+: [0-9]", repo=SHOP, regex=True)
    assert "config/settings.yaml:2" in out


def test_grep_path_glob_restricts_the_files_searched(as_caller, world):
    out = tools_grep.run("RETRY_LIMIT", repo=SHOP, path_glob="*.md")
    assert "README.md" in out and "settings.yaml" not in out


def test_grep_without_a_repo_searches_every_readable_repo(as_caller, world):
    out = tools_grep.run("create_order_remote")
    assert BILLING in out


def test_grep_sees_only_committed_text_not_the_working_tree(as_caller, world):
    root, _ = world
    clone = root / "repos" / SHOP if (root / "repos").exists() else root / SHOP
    scratch = clone / "scratch_uncommitted.py"
    scratch.write_text("UNCOMMITTED_MARKER = 1\n")
    try:
        assert "scratch_uncommitted" not in tools_grep.run("UNCOMMITTED_MARKER", repo=SHOP)
    finally:
        scratch.unlink()


# ─── refs ────────────────────────────────────────────────────────────

def test_refs_lists_callers_with_the_file_line_and_enclosing_function(as_caller, world):
    out = tools_refs.run(SHOP, "create_order")
    assert re.search(r"app/api/orders\.py:\d+ function post_order", out)
    assert "callers of create_order: 2 in" in out


def test_refs_callees_lists_what_a_symbol_calls(as_caller, world):
    out = tools_refs.run(SHOP, "create_order", direction="callees")
    assert "cancel_order" in out


def test_refs_across_repos_finds_a_text_mention_in_a_sibling_repository(as_caller, world, monkeypatch):
    from src.mcp_server.dev_profile import tools_refs as r

    monkeypatch.setattr(r, "_siblings", lambda scope, slug: [BILLING])
    out = r.run(SHOP, "create_order")
    assert BILLING in out and "src/client.py" in out


def test_refs_with_cross_repo_off_never_leaves_the_repository(as_caller, world, monkeypatch):
    from src.mcp_server.dev_profile import tools_refs as r

    monkeypatch.setattr(r, "_siblings", lambda scope, slug: [BILLING])
    assert BILLING not in r.run(SHOP, "create_order", cross_repo=False)


def test_refs_with_a_bad_direction_is_an_error_line(as_caller, world):
    assert "direction must be" in tools_refs.run(SHOP, "create_order", direction="up")
