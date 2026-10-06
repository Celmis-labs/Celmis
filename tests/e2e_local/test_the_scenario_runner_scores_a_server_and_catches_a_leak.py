"""``scripts/dev_mcp_e2e.py`` against the reference ``/mcp/dev/`` server.

The reference server speaks the frozen contract with git grep and regexes. What
is under test is the HARNESS: scoring, budgets, the idx-line check, the leak
scan, the baseline arm, path hygiene and the report. The real tools are tested
against the same runner in ``test_the_dev_profile_passes_the_scenarios.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import scripts.dev_mcp_e2e as R
from tests.e2e_local.client import McpClient
from tests.e2e_local.reference_dev import ReferenceDev
from tests.e2e_local.world import FIXTURES, ROOT, materialize

TOKEN = "reference-token"


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return materialize(tmp_path_factory.mktemp("acme"))


@pytest.fixture(scope="module")
def secrets(world):
    return [R.Secret(label, value) for label, value in world.canaries.items()]


@pytest.fixture(scope="module")
def data():
    return R.load_scenarios(R.BUNDLED_GOLD)


@pytest.fixture(scope="module")
def good(world):
    with ReferenceDev(world.repos, TOKEN) as ref:
        yield ref


@pytest.fixture(scope="module")
def report(good, world, secrets, data):
    return R.run_scenarios(McpClient(good.url, "/mcp/dev/", TOKEN), data, clones=world.repos,
                           secrets_=secrets)


def _by_id(report, prefix: str):
    return next(r for r in report.results if r.id.startswith(prefix))


# ─── a correct server passes ─────────────────────────────────────────


def test_a_server_that_follows_the_contract_passes_every_scenario(report) -> None:
    failed = {r.id: (r.celmis.errors, r.celmis.names_missing, r.celmis.sources_missing,
                     r.celmis.text_missing, r.celmis.idx_missing_on)
              for r in report.results if r.celmis and not r.celmis.passed}
    assert failed == {} and report.ok, R.render(report)


def test_the_howto_scenarios_find_names_sources_and_the_guidance(report) -> None:
    for prefix in ("S1", "S2", "S3"):
        arm = _by_id(report, prefix).celmis
        assert arm.hit_rate == 1.0 and not arm.names_missing and not arm.sources_missing
        assert not arm.text_missing, prefix


def test_every_call_is_counted_and_costed(report) -> None:
    s2 = _by_id(report, "S2").celmis
    assert s2.calls == 2 and s2.chars > 0 and s2.tokens == round(s2.chars / 3.7)


def test_the_baseline_arm_is_deterministic_and_costs_more_on_the_search_scenarios(report, world,
                                                                                  secrets, data) -> None:
    for prefix in ("S4", "S5", "S6", "S7", "S8"):
        r = _by_id(report, prefix)
        assert r.baseline is not None and r.baseline.hit_rate == 1.0, prefix
        assert r.celmis.tokens < r.baseline.tokens, prefix
    again = R.run_baseline_arm(next(s for s in data["scenarios"] if s["id"].startswith("S4")),
                               world.repos)[0]
    assert again.chars == _by_id(report, "S4").baseline.chars


def test_the_baseline_shows_what_a_naive_grep_would_have_exposed(report) -> None:
    assert _by_id(report, "S1").baseline_exposes, "the baseline should surface the planted secrets"
    assert not _by_id(report, "S1").celmis.leaks


def test_the_tools_list_size_is_reported_separately(report) -> None:
    assert report.tools_list_count == 9 and 0 < report.tools_list_chars < 6000


def test_the_report_renders_without_any_value(report, world) -> None:
    text = R.render(report) + json.dumps(R.report_json(report))
    assert world.leaks_in(text) == []
    assert "RESULT PASS" in R.render(report)


# ─── a leaking server fails, naming the tool and the label ──────────


def test_a_server_that_returns_secret_files_fails_the_run_and_names_the_leak(world, secrets,
                                                                              data) -> None:
    with ReferenceDev(world.repos, TOKEN, leaky=True) as ref:
        rep = R.run_scenarios(McpClient(ref.url, "/mcp/dev/", TOKEN), data, clones=world.repos,
                              secrets_=secrets, baseline=False)
    assert not rep.ok
    assert any(line.startswith("leak: grep, ") for line in rep.leaks)
    text = R.render(rep)
    assert world.leaks_in(text) == [], "the report must name labels, never values"
    assert _by_id(rep, "S12").celmis.errors, "a secret file listed in map is a failure"


def test_a_missing_idx_line_is_a_contract_failure(world, secrets, data, monkeypatch) -> None:
    class NoIdx(ReferenceDev):
        def _idx(self, slugs):  # noqa: ANN001
            return "no idx here"

    with NoIdx(world.repos, TOKEN) as ref:
        rep = R.run_scenarios(McpClient(ref.url, "/mcp/dev/", TOKEN), data, clones=world.repos,
                              secrets_=secrets, only={"S4"}, baseline=False)
    assert _by_id(rep, "S4").celmis.idx_missing_on == ["find"] and not rep.ok


def test_an_answer_over_the_budget_is_a_failure(good, world, secrets, data) -> None:
    rep = R.run_scenarios(McpClient(good.url, "/mcp/dev/", TOKEN), data, clones=world.repos,
                          secrets_=secrets, only={"S2"}, baseline=False, max_chars=100)
    assert _by_id(rep, "S2").celmis.over_budget and not rep.ok


def test_a_wrong_token_is_a_failure_not_a_crash(good, world, secrets, data) -> None:
    rep = R.run_scenarios(McpClient(good.url, "/mcp/dev/", "wrong"), data, clones=world.repos,
                          secrets_=secrets, only={"S4"}, baseline=False)
    assert not rep.ok


def test_a_repository_the_token_cannot_see_skips_the_scenario_instead_of_failing_it(
        world, secrets, data) -> None:
    only_shop = {k: v for k, v in world.repos.items() if k == "acme/shop"}
    with ReferenceDev(only_shop, TOKEN) as ref:
        rep = R.run_scenarios(McpClient(ref.url, "/mcp/dev/", TOKEN), data, clones=world.repos,
                              secrets_=secrets, baseline=False)
    skipped = {r.id.split("-")[0] for r in rep.results if r.skipped}
    assert {"S1", "S3", "S7"} <= skipped and "S4" not in skipped
    assert _by_id(rep, "S1").skipped.startswith("not visible to this token")


def test_a_run_without_known_secret_values_says_the_leak_scan_was_not_armed(good, world, data) -> None:
    rep = R.run_scenarios(McpClient(good.url, "/mcp/dev/", TOKEN), data, clones=world.repos,
                          secrets_=[], only={"S4"}, baseline=False)
    text = R.render(rep)
    assert not rep.leak_scan_armed and "leak scan: NOT armed" in text
    assert R.report_json(rep)["leak_scan_armed"] is False
    assert rep.ok  # still a pass: arming it is the caller's choice ...
    strict = R.run_scenarios(McpClient(good.url, "/mcp/dev/", TOKEN), data, clones=world.repos,
                             secrets_=[], only={"S4"}, baseline=False, require_leak_scan=True)
    assert not strict.ok and "--require-leak-scan" in R.render(strict)  # ... and it can be forced


def test_a_run_that_skipped_most_scenarios_fails_when_a_minimum_is_asked_for(world, secrets,
                                                                             data) -> None:
    only_shop = {k: v for k, v in world.repos.items() if k == "acme/shop"}
    with ReferenceDev(only_shop, TOKEN) as ref:
        client = McpClient(ref.url, "/mcp/dev/", TOKEN)
        lenient = R.run_scenarios(client, data, clones=world.repos, secrets_=secrets, baseline=False)
        strict = R.run_scenarios(client, data, clones=world.repos, secrets_=secrets, baseline=False,
                                 min_scenarios=len(data["scenarios"]))
    assert lenient.ran and len(lenient.ran) < len(data["scenarios"])
    assert f"scenarios: {len(lenient.ran)} ran" in R.render(lenient)
    assert not strict.ok and "--min-scenarios" in R.render(strict)


def test_a_secret_that_json_escapes_is_found_in_the_text_and_in_the_raw_answer() -> None:
    quoted = R.Secret("API_SECRET", 'pa"ss\\word')
    escaped = json.dumps({"text": quoted.value})
    assert R.scan(quoted.value, [quoted]) == ["API_SECRET"]
    assert R.scan(escaped, [quoted]) == []  # why the text must be scanned as well as the raw JSON


def test_a_name_quoted_in_a_code_slice_is_not_a_listed_name() -> None:
    answer = ("idx: acme/shop develop@abc1234 1m fresh\n"
              "1) app/db.py:3-8 connect\n   url = os.environ['DATABASE_URL']\n"
              "inputs (names only; values are never available via Celmis):\n"
              "  DB_POOL_SIZE  config  read app/db.py\n"
              "next: ask the user or ops")
    assert R.names_section(answer).strip().startswith("DB_POOL_SIZE")
    arm = R.Arm()
    R.score(arm, {"names": ["DATABASE_URL", "DB_POOL_SIZE"]}, answer)
    assert arm.names_missing == ["DATABASE_URL"]


# ─── scoring rules ───────────────────────────────────────────────────


@pytest.mark.parametrize("text,found", [
    ("acme/x src/a.py:10-20 function f", True),
    ("src/a.py:20", False),                    # 20 is not 15
    ("src/a.py:15", True),
    ("src/a.py:16-30", False),
    ("  15-30 class Order  (src/a.py)", False),  # no text on a line carrying the number
    ("src/a.py L15 def create_order()", False),  # needs the path pinned
    ("whatever mentions create_order", False),
])
def test_a_gold_fact_needs_the_path_and_the_line(text, found) -> None:
    gold = {"path": "src/a.py", "line": 15, "text": "create_order"}
    assert R.gold_found(text, gold) is found


def test_a_numbered_body_line_counts_when_it_carries_the_text_and_the_number() -> None:
    gold = {"path": "src/a.py", "line": 15, "text": "create_order"}
    assert R.gold_found("    15| def create_order(self):", gold)
    assert not R.gold_found("    16| def create_order(self):", gold)
    assert not R.gold_found("    115| def create_order(self):", gold)


def test_names_are_matched_as_whole_words() -> None:
    assert R._has_name("uses DB_PASSWORD here", "DB_PASSWORD")
    assert not R._has_name("uses DB_PASSWORD_FILE here", "DB_PASSWORD")


# ─── hygiene: where output and scenarios may live ───────────────────


def test_an_output_directory_inside_the_repository_is_refused() -> None:
    with pytest.raises(SystemExit, match="inside the repository"):
        R.check_paths(ROOT / "tests" / "out", None)


def test_a_scenario_file_inside_the_repository_is_refused_except_the_bundled_one() -> None:
    R.check_paths(Path("/tmp/celmis-e2e-out"), R.BUNDLED_GOLD)
    with pytest.raises(SystemExit, match="outside the repository"):
        R.check_paths(Path("/tmp/celmis-e2e-out"), ROOT / "docs" / "mine.yaml")


def test_the_default_output_directory_is_under_the_cache() -> None:
    assert not R.inside_a_checkout_of_this_tool(Path("~/.cache/celmis-e2e/x").expanduser())


# ─── the leak scan over real .env files ──────────────────────────────


@pytest.fixture
def source_tree(tmp_path):
    repo = tmp_path / "svc"
    repo.mkdir()
    (repo / ".env").write_text(
        "DATABASE_URL=postgresql://app:Zq9fW2xK7pLm@db.internal/app\n"
        "STRIPE_KEY='sk_test_abcdef123456'\nDEBUG=true\nHOST=localhost\nSHORT=abc\n"
        "PUBLIC_URL=https://app.example.com/base\n", encoding="utf-8")
    (repo / ".env.example").write_text("PUBLIC_URL=https://app.example.com/base\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / ".env").write_text("IGNORED=ignored-value-123\n", encoding="utf-8")
    return tmp_path


def test_env_secrets_load_into_memory_with_names_and_skip_public_and_trivial(source_tree) -> None:
    found = R.load_env_secrets(source_tree, ["svc"])
    by_name = {s.name for s in found}
    assert by_name == {"DATABASE_URL", "STRIPE_KEY"}
    assert any(s.value == "Zq9fW2xK7pLm" for s in found), "the DSN password is scanned alone"


def test_a_leak_of_a_dsn_password_is_reported_by_variable_name_only(source_tree) -> None:
    secrets = R.load_env_secrets(source_tree, ["svc"])
    assert R.scan("connect postgresql://app:[REDACTED]@db", secrets) == []
    assert R.scan("password is Zq9fW2xK7pLm ok", secrets) == ["DATABASE_URL"]
    line = f"leak: tool, {R.scan('x sk_test_abcdef123456 y', secrets)[0]}"
    assert "sk_test" not in line and line == "leak: tool, STRIPE_KEY"


# ─── the command line, end to end, against a server we did not spawn ─


def test_the_cli_runs_against_an_external_url_and_writes_only_to_the_chosen_directory(
        good, world, tmp_path, monkeypatch, capsys) -> None:
    scenarios = tmp_path / "scenarios.yaml"
    scenarios.write_text((FIXTURES / "gold.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    out = tmp_path / "out"
    monkeypatch.setenv("E2E_TEST_TOKEN", TOKEN)
    code = R.main(["--url", good.url, "--token-env", "E2E_TEST_TOKEN", "--src-root",
                   str(world.root), "--scenarios", str(scenarios), "--out", str(out)])
    printed = capsys.readouterr().out
    assert code == 0 and "RESULT PASS" in printed
    assert sorted(p.name for p in out.iterdir()) == ["report.json", "report.txt"]
    blob = (out / "report.json").read_text() + (out / "report.txt").read_text() + printed
    assert world.leaks_in(blob) == []
    assert json.loads((out / "report.json").read_text())["ok"] is True
    assert yaml.safe_load(scenarios.read_text())["version"] == 1


def test_the_cli_refuses_an_output_inside_the_repository(good, monkeypatch) -> None:
    monkeypatch.setenv("E2E_TEST_TOKEN", TOKEN)
    with pytest.raises(SystemExit, match="inside the repository"):
        R.main(["--url", good.url, "--token-env", "E2E_TEST_TOKEN",
                "--out", str(ROOT / "tests" / "e2e_out")])
    assert not (ROOT / "tests" / "e2e_out").exists()


def test_the_cli_fails_when_a_scenario_fails(world, tmp_path, monkeypatch, capsys) -> None:
    with ReferenceDev(world.repos, TOKEN, leaky=True) as ref:
        monkeypatch.setenv("E2E_TEST_TOKEN", TOKEN)
        code = R.main(["--url", ref.url, "--token-env", "E2E_TEST_TOKEN", "--src-root",
                       str(world.root), "--no-baseline", "--out", str(tmp_path / "o")])
    assert code == 1 and "RESULT FAIL" in capsys.readouterr().out


def test_the_cli_needs_a_token_for_an_external_server(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("E2E_TEST_TOKEN", raising=False)
    assert R.main(["--url", "http://127.0.0.1:9", "--token-env", "E2E_TEST_TOKEN",
                   "--out", str(tmp_path / "o")]) == 2
    assert "is empty" in capsys.readouterr().err


def test_running_the_cli_leaves_the_loggers_as_it_found_them(tmp_path, monkeypatch) -> None:
    import logging

    before = {n: logging.getLogger(n).level for n in ("mcp", "httpx", "uvicorn", "src", "alembic")}
    monkeypatch.delenv("E2E_TEST_TOKEN", raising=False)
    R.main(["--url", "http://127.0.0.1:9", "--token-env", "E2E_TEST_TOKEN",
            "--out", str(tmp_path / "o")])
    assert {n: logging.getLogger(n).level for n in before} == before
