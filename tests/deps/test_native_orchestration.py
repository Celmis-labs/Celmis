"""Оркестрація нативних аудиторів — кілька підпроєктів в одному репозиторії
та чесний облік того, що НЕ перевіряли.

Інструменти тут не запускаються: `audit_repo` приймає runner, тож тест
підставляє зафіксований вивід і перевіряє маршрутизацію — яку теку яким
інструментом обходимо, що робимо з відсутнім бінарником, і чи потрапляє
підпроєкт у знахідку.
"""

from __future__ import annotations

import json

from src.deps.native import CmdResult, audit_repo
from src.deps.scanner import DeclaredDep

PIP_AUDIT_OUT = json.dumps({
    "dependencies": [
        {"name": "urllib3", "version": "1.26.5",
         "vulns": [{"id": "PYSEC-2023-192", "fix_versions": ["1.26.18"],
                    "aliases": ["CVE-2023-45803"], "description": "leak"}]},
    ],
    "fixes": [],
})

NPM_AUDIT_OUT = json.dumps({
    "auditReportVersion": 2,
    "vulnerabilities": {
        "minimist": {
            "name": "minimist", "severity": "critical", "isDirect": False,
            "via": [{"source": 1179, "name": "minimist",
                     "title": "Prototype Pollution",
                     "url": "https://github.com/advisories/GHSA-xvch-5gv4-984h",
                     "severity": "critical", "range": "<0.2.1"}],
            "range": "<0.2.1", "nodes": ["node_modules/minimist"],
            "fixAvailable": {"name": "minimist", "version": "1.2.8"},
        },
    },
    "metadata": {"vulnerabilities": {"total": 1}},
})


def _tree(tmp_path):
    """Монорепо: Python-сервіс у ./api, Node-застосунок у ./web, і Go-модуль
    у корені без встановленого інструмента."""
    api = tmp_path / "api"
    api.mkdir()
    (api / "requirements.txt").write_text("urllib3>=1.26\n")

    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_text(json.dumps({
        "name": "web", "dependencies": {"mkdirp": "^0.5.1"}}))
    (web / "package-lock.json").write_text(json.dumps({
        "lockfileVersion": 3,
        "packages": {
            "": {"dependencies": {"mkdirp": "^0.5.1"}},
            "node_modules/mkdirp": {"version": "0.5.1"},
            "node_modules/minimist": {"version": "0.0.8"},
        },
    }))

    (tmp_path / "go.mod").write_text("module example.com/app\n\ngo 1.22\n")
    return tmp_path


def test_audit_repo_routes_each_subproject_to_its_tool(tmp_path) -> None:
    calls: list[tuple[str, str]] = []

    def runner(cmd, cwd, timeout):
        calls.append((cmd[0], cwd.name))
        if cmd[0] == "pip-audit":
            return CmdResult(True, 1, PIP_AUDIT_OUT, "")
        if cmd[0] == "npm":
            # Другий прогін (--omit=dev) не знаходить minimist → dev-only.
            if "--omit=dev" in cmd:
                return CmdResult(True, 0, json.dumps(
                    {"auditReportVersion": 2, "vulnerabilities": {}}), "")
            return CmdResult(True, 1, NPM_AUDIT_OUT, "")
        return CmdResult(False, -1, "", "", f"{cmd[0]} is not installed in this image")

    declared = [
        DeclaredDep("PyPI", "urllib3", "1.26", ">=1.26", False, "api/requirements.txt"),
        DeclaredDep("npm", "mkdirp", "0.5.1", "^0.5.1", False, "web/package.json"),
    ]
    result = audit_repo(_tree(tmp_path), declared=declared, runner=runner)

    assert ("pip-audit", "api") in calls
    assert ("npm", "web") in calls
    assert ("govulncheck", tmp_path.name) in calls

    packages = {f.package: f for f in result.findings}
    assert packages["urllib3"].subproject == "api"
    assert packages["urllib3"].source == "pip-audit"
    assert packages["minimist"].subproject == "web"
    assert packages["minimist"].transitive is True
    assert packages["minimist"].version == "0.0.8", "версія дочитана з package-lock"
    assert packages["minimist"].is_dev is True, "відсутній у --omit=dev прогоні"


def test_missing_tool_is_not_checked_with_a_reason(tmp_path) -> None:
    """Відсутній інструмент НІКОЛИ не має виглядати як «нуль уразливостей»."""
    def runner(cmd, cwd, timeout):
        return CmdResult(False, -1, "", "", f"{cmd[0]} is not installed in this image")

    result = audit_repo(_tree(tmp_path), declared=[], runner=runner)

    assert result.findings == []
    assert result.covered() == set(), "жодна екосистема не перевірена"
    statuses = {(c.ecosystem, c.status) for c in result.checks}
    assert ("PyPI", "not_checked") in statuses
    assert ("npm", "not_checked") in statuses
    assert ("Go", "not_checked") in statuses
    assert all(c.reason for c in result.checks if c.status == "not_checked"), (
        "кожен not_checked зобов'язаний нести причину"
    )


def test_missing_lockfile_is_reported_not_silently_skipped(tmp_path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"name": "app"}))

    def runner(cmd, cwd, timeout):  # не має викликатись узагалі
        raise AssertionError(f"npm audit без lock-файлу не повинен запускатись: {cmd}")

    result = audit_repo(tmp_path, declared=[], runner=runner)
    npm_checks = [c for c in result.checks if c.ecosystem == "npm"]
    assert len(npm_checks) == 1
    assert npm_checks[0].status == "not_checked"
    assert "lock file" in npm_checks[0].reason


def test_pnpm_and_yarn_lockfiles_pick_their_own_tool(tmp_path) -> None:
    pnpm_dir = tmp_path / "a"
    pnpm_dir.mkdir()
    (pnpm_dir / "package.json").write_text("{}")
    (pnpm_dir / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    yarn_dir = tmp_path / "b"
    yarn_dir.mkdir()
    (yarn_dir / "package.json").write_text("{}")
    (yarn_dir / "yarn.lock").write_text("# yarn lockfile v1\n")

    seen: list[list[str]] = []

    def runner(cmd, cwd, timeout):
        seen.append(cmd)
        return CmdResult(False, -1, "", "", f"{cmd[0]} is not installed in this image")

    audit_repo(tmp_path, declared=[], runner=runner)
    assert ["pnpm", "audit", "--json"] in seen
    assert ["yarn", "npm", "audit", "--all", "--json"] in seen


def test_on_progress_beats_before_every_invocation(tmp_path) -> None:
    """Без биття пульсу довгий `pip-audit -r` виглядає для UI як зависання."""
    beats: list[tuple[str, str]] = []

    def runner(cmd, cwd, timeout):
        return CmdResult(False, -1, "", "", "missing")

    audit_repo(_tree(tmp_path), declared=[], runner=runner,
               on_progress=lambda tool, sub: beats.append((tool, sub)))
    assert ("pip-audit", "api") in beats
    assert ("npm", "web") in beats
    assert ("govulncheck", "") in beats, "корінь репозиторію — порожній підпроєкт"


def test_on_progress_can_abort_the_sweep(tmp_path) -> None:
    """Той самий колбек — точка кооперативного скасування: якщо він кидає,
    обхід має зупинитись, а не проковтнути виняток."""
    class Cancelled(Exception):
        pass

    def boom(tool, sub):
        raise Cancelled(tool)

    try:
        audit_repo(_tree(tmp_path), declared=[], on_progress=boom,
                   runner=lambda *a: CmdResult(False, -1, "", "", "missing"))
    except Cancelled:
        return
    raise AssertionError("виняток із on_progress не має проковтуватись")


def test_broken_output_is_failed_not_checked(tmp_path) -> None:
    """Інструмент є, відпрацював, але вивів сміття — це `failed`, окремий стан
    від «інструмента немає»."""
    api = tmp_path / "api"
    api.mkdir()
    (api / "requirements.txt").write_text("urllib3\n")

    def runner(cmd, cwd, timeout):
        return CmdResult(True, 2, "", "ERROR: could not resolve dependencies")

    result = audit_repo(tmp_path, declared=[], runner=runner)
    pypi = [c for c in result.checks if c.ecosystem == "PyPI"]
    assert pypi and pypi[0].status == "failed"
    assert "could not resolve" in pypi[0].reason
