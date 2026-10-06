"""The fixtures and ``gold.yaml`` agree, and the fixtures carry nothing secret.

A scenario that fails because a fixture edit moved a line is a harness bug, not
a product bug; these tests make that fail here, in milliseconds, with the
offending fact named.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.e2e_local.world import FIXTURES, REPOS, SECRET_FILES
from tests.plugin import contract as C

GOLD = yaml.safe_load((FIXTURES / "gold.yaml").read_text(encoding="utf-8"))
SCENARIOS = GOLD["scenarios"]


def _fixture_lines(repo: str, path: str) -> list[str]:
    spec = GOLD["repos"][repo]
    return (FIXTURES / spec["dir"] / path).read_text(encoding="utf-8").splitlines()


def test_scenario_ids_are_unique_and_prefixed() -> None:
    ids = [s["id"] for s in SCENARIOS]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("S") and "-" in i for i in ids)


def test_every_step_names_a_dev_tool_and_passes_only_documented_arguments() -> None:
    allowed = {"repos": {}, "find": {"query", "repo", "kind", "mode", "limit", "cursor"},
               "outline": {"repo", "path", "depth"},
               "read_symbol": {"repo", "name", "path", "max_lines"},
               "refs": {"repo", "symbol", "direction", "depth", "limit"},
               "grep": {"pattern", "repo", "path_glob", "regex", "limit"},
               "map": {"repo", "path"}, "ask": {"question"},
               "howto": {"topic", "repo", "path"}}
    for s in SCENARIOS:
        for step in s["steps"]:
            assert step["tool"] in C.DEV_TOOLS, s["id"]
            assert set(step.get("args", {})) <= set(allowed[step["tool"]]), (s["id"], step)
            if step["tool"] == "howto":
                assert step["args"]["topic"] in C.HOWTO_TOPICS


def test_every_repository_a_scenario_names_is_declared_and_exists() -> None:
    for s in SCENARIOS:
        for step in s["steps"]:
            repo = step.get("args", {}).get("repo")
            if repo:
                assert repo in GOLD["repos"], (s["id"], repo)
    for logical, spec in GOLD["repos"].items():
        assert (FIXTURES / spec["dir"]).is_dir()
        assert REPOS[logical][0] == spec["dir"]


@pytest.mark.parametrize("scenario", [s for s in SCENARIOS if s.get("gold")],
                         ids=lambda s: s["id"])
def test_every_gold_fact_is_on_the_line_it_names(scenario) -> None:
    for g in scenario["gold"]:
        lines = _fixture_lines(g["repo"], g["path"])
        assert g["line"] <= len(lines), g
        assert g["text"] in lines[g["line"] - 1], (
            f"{g['path']}:{g['line']} no longer contains {g['text']!r}: "
            f"{lines[g['line'] - 1]!r}")


def test_names_and_sources_the_scenarios_expect_exist_in_the_fixtures() -> None:
    for s in SCENARIOS:
        repos = {st["args"]["repo"] for st in s["steps"] if st.get("args", {}).get("repo")}
        corpus = ""
        for r in repos:
            base = FIXTURES / GOLD["repos"][r]["dir"]
            corpus += "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                                for p in base.rglob("*") if p.is_file())
        for name in s.get("names", []):
            assert name in corpus, (s["id"], name)
        for src in s.get("sources", []):
            assert any((FIXTURES / GOLD["repos"][r]["dir"] / src).exists() for r in repos), (
                s["id"], src)


def test_the_fixture_tree_holds_no_secret_file_and_no_value() -> None:
    """Secret files are generated at run time; none may be committed."""
    bad_names = {".env", "id_rsa", "id_ed25519"}
    for path in FIXTURES.rglob("*"):
        if not path.is_file() or path.name == "gold.yaml":  # gold names the probes
            continue
        assert path.name not in bad_names and not path.name.startswith(".env.") or \
            path.name.endswith((".example", ".sample")), f"secret-shaped file committed: {path}"
        assert path.suffix not in {".pem", ".key", ".p12", ".tfvars"}, path
        assert "secrets" not in path.relative_to(FIXTURES).parts, path
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "PRIVATE KEY-----" not in text, path
        assert "AKIA" not in text and "sk_live_" not in text, path
        assert "CANARY_" not in text.replace("__CANARY_", ""), f"a literal canary in {path}"


def test_every_secret_file_the_world_plants_is_named_by_a_probe_scenario() -> None:
    """If a secret file is planted, a probe must be able to ask for it or list it."""
    probes = " ".join(str(s) for s in SCENARIOS if s["id"].startswith(("S11", "S12")))
    probes = probes.replace("\\\\", "").replace("\\", "")
    for files in SECRET_FILES.values():
        for rel in files:
            assert Path(rel).name in probes, f"no probe scenario names {rel}"


def test_nothing_in_the_fixtures_or_the_harness_names_the_company() -> None:
    root = Path(__file__).resolve().parents[2]
    assert C.forbidden_hits([FIXTURES, root / "tests" / "e2e_local",
                             root / "scripts" / "dev_mcp_e2e.py"]) == []
