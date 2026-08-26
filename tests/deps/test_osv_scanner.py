"""osv-scanner — універсальний рушій аудиту: розбір виводу, деградація та
дедуплікація з рештою джерел.

Фікстура нижче — обрізаний, але СПРАВЖНІЙ вивід `osv-scanner scan source
--format json` (v2.5.0) на репозиторії з Maven-, NuGet-, PyPI- та
npm-маніфестами. Саме тому тест ловить регресію парсера, а не наявність
бінарника на машині: сам бінарник тут не запускається жодного разу.

Три речі, заради яких цей файл існує:

* мультимовність не декларативна — Maven і NuGet розбираються нарівні з npm;
* ненульовий код виходу osv-scanner означає «знайшов уразливості», і сплутати
  це з помилкою — значить тихо втратити всі знахідки;
* відсутній/зламаний бінарник деградує до порожнього результату з причиною,
  а не до винятку: аудит не має падати через один недоступний рушій.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.deps.auditor import merge_vuln
from src.deps.native import CmdResult
from src.deps.osv_scanner import (
    REASON_BAD_JSON,
    REASON_MISSING,
    REASON_NO_SOURCES,
    REASON_TIMEOUT,
    audit_repo,
    canonical_ecosystem,
    parse_osv_scanner,
    scanned_ecosystems,
)
from src.deps.scanner import DeclaredDep

# ─── Фікстура: реальний вивід osv-scanner 2.5.0 ──────────────────────

LOG4J_RCE = {
    "id": "GHSA-jfh8-c2jp-5v3q",
    "aliases": ["CVE-2021-44228"],
    "summary": "Remote code injection in Log4j",
    "severity": [{"score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H/E:H",
                  "type": "CVSS_V3"}],
    "database_specific": {"severity": "CRITICAL"},
    # Пропатчено на трьох гілках одночасно — найменший fix тут є ЗНИЖЕННЯМ
    # версії для того, хто сидить на 2.14.1.
    "affected": [
        {"package": {"ecosystem": "Maven",
                     "name": "org.apache.logging.log4j:log4j-core"},
         "ranges": [{"type": "ECOSYSTEM",
                     "events": [{"introduced": "2.13.0"}, {"fixed": "2.15.0"}]}]},
        {"package": {"ecosystem": "Maven",
                     "name": "org.apache.logging.log4j:log4j-core"},
         "ranges": [{"type": "ECOSYSTEM",
                     "events": [{"introduced": "2.0-beta9"}, {"fixed": "2.3.1"}]}]},
        {"package": {"ecosystem": "Maven",
                     "name": "org.apache.logging.log4j:log4j-core"},
         "ranges": [{"type": "ECOSYSTEM",
                     "events": [{"introduced": "2.4"}, {"fixed": "2.12.2"}]}]},
    ],
}

LOG4J_XML = {
    "id": "GHSA-3pxv-7cmr-fjr4",
    "aliases": ["CVE-2026-34480"],
    "summary": "Apache Log4j Core: Silent log event loss in XmlLayout",
    # Лише CVSS 4.0 — формулу v4 severity.py не рахує, тож єдиний придатний
    # сигнал тут словесний.
    "severity": [{"score": "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:N/VI:N/VA:N/"
                           "SC:N/SI:L/SA:N", "type": "CVSS_V4"}],
    "database_specific": {"severity": "MODERATE"},
    "affected": [
        {"package": {"ecosystem": "Maven",
                     "name": "org.apache.logging.log4j:log4j-core"},
         "ranges": [{"type": "ECOSYSTEM",
                     "events": [{"introduced": "2.0-alpha1"}, {"fixed": "2.25.4"}]}]},
    ],
}

NEWTONSOFT = {
    "id": "GHSA-5crp-9r3c-p9vr",
    "aliases": ["CVE-2024-21907"],
    "summary": "Improper Handling of Exceptional Conditions in Newtonsoft.Json",
    "severity": [{"score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H",
                  "type": "CVSS_V3"}],
    "database_specific": {"severity": "HIGH"},
    "affected": [
        {"package": {"ecosystem": "NuGet", "name": "Newtonsoft.Json"},
         "ranges": [{"type": "ECOSYSTEM",
                     "events": [{"introduced": "0"}, {"fixed": "13.0.1"}]}]},
    ],
}

# PYSEC-записи не мають ані severity-слова, ані CVSS-вектора — рівень відомий
# ЛИШЕ з max_severity групи. Без цього запасного шляху кожен такий запис
# отримав би дефолтне "medium".
DJANGO_PYSEC = {
    "id": "PYSEC-2021-109",
    "aliases": ["BIT-django-2021-35042", "CVE-2021-35042", "GHSA-xpfp-f569-q3p2"],
    "affected": [
        {"package": {"ecosystem": "PyPI", "name": "django"},
         "ranges": [{"type": "ECOSYSTEM",
                     "events": [{"introduced": "3.1"}, {"fixed": "3.1.13"},
                                {"introduced": "3.2"}, {"fixed": "3.2.5"}]}]},
    ],
}

LODASH = {
    "id": "GHSA-29mw-wpgm-hmr9",
    "aliases": ["CVE-2020-28500"],
    "summary": "Regular Expression Denial of Service (ReDoS) in lodash",
    "severity": [{"score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:L",
                  "type": "CVSS_V3"}],
    "database_specific": {"severity": "MODERATE"},
    "affected": [
        {"package": {"ecosystem": "npm", "name": "lodash"},
         "ranges": [{"type": "ECOSYSTEM",
                     "events": [{"introduced": "0"}, {"fixed": "4.17.21"}]}]},
    ],
}


def _report(root: Path) -> dict:
    """Монорепо на чотирьох екосистемах: Java, .NET, Python, Node."""
    return {
        "results": [
            {"source": {"path": str(root / "java" / "pom.xml"), "type": "lockfile"},
             "packages": [{
                 "package": {"name": "org.apache.logging.log4j:log4j-core",
                             "version": "2.14.1", "ecosystem": "Maven"},
                 "groups": [
                     {"ids": ["GHSA-jfh8-c2jp-5v3q"],
                      "aliases": ["CVE-2021-44228", "GHSA-jfh8-c2jp-5v3q"],
                      "max_severity": "10.0"},
                     {"ids": ["GHSA-3pxv-7cmr-fjr4"],
                      "aliases": ["CVE-2026-34480", "GHSA-3pxv-7cmr-fjr4"],
                      "max_severity": "6.9"},
                 ],
                 "vulnerabilities": [LOG4J_XML, LOG4J_RCE],
             }]},
            {"source": {"path": str(root / "dotnet" / "packages.lock.json"),
                        "type": "lockfile"},
             "packages": [{
                 "package": {"name": "Newtonsoft.Json", "version": "11.0.1",
                             "ecosystem": "NuGet"},
                 "groups": [{"ids": ["GHSA-5crp-9r3c-p9vr"],
                             "aliases": ["CVE-2024-21907", "GHSA-5crp-9r3c-p9vr"],
                             "max_severity": "7.5"}],
                 "vulnerabilities": [NEWTONSOFT],
             }]},
            {"source": {"path": str(root / "api" / "requirements.txt"),
                        "type": "lockfile"},
             "packages": [{
                 "package": {"name": "django", "version": "3.2.0",
                             "ecosystem": "PyPI"},
                 "groups": [{"ids": ["PYSEC-2021-109", "GHSA-xpfp-f569-q3p2"],
                             "aliases": ["BIT-django-2021-35042", "CVE-2021-35042",
                                         "GHSA-xpfp-f569-q3p2", "PYSEC-2021-109"],
                             "max_severity": "9.8"}],
                 "vulnerabilities": [DJANGO_PYSEC],
             }]},
            {"source": {"path": str(root / "web" / "package-lock.json"),
                        "type": "lockfile"},
             "packages": [
                 {"package": {"name": "lodash", "version": "4.17.11",
                              "ecosystem": "npm"},
                  "dependency_groups": ["dev"],
                  "groups": [{"ids": ["GHSA-29mw-wpgm-hmr9"],
                              "aliases": ["CVE-2020-28500", "GHSA-29mw-wpgm-hmr9"],
                              "max_severity": "5.3"}],
                  "vulnerabilities": [LODASH]},
                 # Чистий пакет: без --all-packages його б тут не було, і
                 # «перевірено й чисто» стало б невідрізненним від «не дивились».
                 {"package": {"name": "left-pad", "version": "1.3.0",
                              "ecosystem": "npm"}},
             ]},
        ],
        "experimental_config": {"licenses": {"summary": False, "allowlist": None}},
    }


def _by_pkg(findings, name):
    return next(f for f in findings if f.package == name)


# ─── Розбір ──────────────────────────────────────────────────────────


def test_parses_every_ecosystem_into_the_shared_finding_shape(tmp_path) -> None:
    """Maven і NuGet — саме ті екосистеми, яких не бачить жоден нативний
    аудитор у цьому образі."""
    findings = parse_osv_scanner(_report(tmp_path), repo_path=tmp_path)

    ecosystems = {f.ecosystem for f in findings}
    assert ecosystems == {"Maven", "NuGet", "PyPI", "npm"}

    log4j = _by_pkg(findings, "org.apache.logging.log4j:log4j-core")
    assert log4j.ecosystem == "Maven"
    assert log4j.version == "2.14.1"
    assert log4j.source == "osv-scanner"
    assert log4j.subproject == "java"
    assert log4j.url == "https://osv.dev/vulnerability/GHSA-jfh8-c2jp-5v3q"
    assert log4j.cve == "CVE-2021-44228"

    nuget = _by_pkg(findings, "Newtonsoft.Json")
    assert nuget.ecosystem == "NuGet"
    assert nuget.cve == "CVE-2024-21907"
    assert nuget.fixed_in == "13.0.1"
    assert nuget.subproject == "dotnet"

    vuln = nuget.to_vuln()
    assert vuln["source"] == "osv-scanner"
    assert vuln["id"] == "GHSA-5crp-9r3c-p9vr"
    assert vuln["severity"] == "high"


def test_severity_comes_from_the_vector_never_from_float_of_it(tmp_path) -> None:
    """Три різні шляхи оцінки, і в жодному не можна отримати дефолтне
    "medium" там, де дані є."""
    findings = parse_osv_scanner(_report(tmp_path), repo_path=tmp_path)
    by_id = {f.vuln_id: f for f in findings}

    # 1) словесний рівень бази + CVSS 3.1 вектор
    assert by_id["GHSA-jfh8-c2jp-5v3q"].severity == "critical"
    # 2) вектор CVSS 4.0 порахувати нічим — залишається слово бази
    assert by_id["GHSA-3pxv-7cmr-fjr4"].severity == "medium"
    # 3) PYSEC не має НІЧОГО, крім max_severity групи ("9.8" — голе число, і
    #    саме через це воно йде через CVSS-хелпер, а не через float()).
    assert by_id["PYSEC-2021-109"].severity == "critical"


def test_fixed_in_is_an_upgrade_not_a_downgrade(tmp_path) -> None:
    """Адвізорі, пропатчене на кількох гілках, перелічує fix для кожної.
    Найменший із них — знижка версії, а не оновлення."""
    findings = parse_osv_scanner(_report(tmp_path), repo_path=tmp_path)
    by_id = {f.vuln_id: f for f in findings}

    assert by_id["GHSA-jfh8-c2jp-5v3q"].fixed_in == "2.15.0", "не 2.3.1"
    assert by_id["PYSEC-2021-109"].fixed_in == "3.2.5", "не 3.1.13"


def test_dev_group_and_declared_names_decide_is_dev_and_transitive(tmp_path) -> None:
    declared = [DeclaredDep("PyPI", "django", "3.2.0", "==3.2.0", False,
                            "api/requirements.txt")]
    findings = parse_osv_scanner(
        _report(tmp_path), repo_path=tmp_path,
        direct={"PyPI": {"django"}, "npm": {"left-pad"}},
        dev={d.ecosystem: set() for d in declared},
    )
    by_pkg = {f.package: f for f in findings}

    assert by_pkg["lodash"].is_dev is True, "dependency_groups: ['dev']"
    assert by_pkg["lodash"].transitive is True, "немає серед оголошених npm-пакетів"
    assert by_pkg["django"].transitive is False
    # Maven маніфести сканер декларацій не читає взагалі — чесна відповідь
    # «невідомо», а не вигадане «транзитивний».
    assert by_pkg["org.apache.logging.log4j:log4j-core"].transitive is False


def test_aliases_and_raw_ecosystem_survive_into_the_stored_vuln(tmp_path) -> None:
    findings = parse_osv_scanner(_report(tmp_path), repo_path=tmp_path)
    pysec = next(f for f in findings if f.vuln_id == "PYSEC-2021-109")

    assert "GHSA-xpfp-f569-q3p2" in pysec.to_vuln()["aliases"], (
        "без аліасів той самий дефект від pip-audit не склеїться з цим"
    )
    assert "ecosystem_raw" not in pysec.to_vuln(), "суфікса релізу тут немає"

    debian = parse_osv_scanner({"results": [{"source": {"path": "/repo/x"}, "packages": [
        {"package": {"name": "curl", "version": "7.88.1", "ecosystem": "Debian:12"},
         "vulnerabilities": [{"id": "CVE-2023-38545",
                              "database_specific": {"severity": "HIGH"}}]},
    ]}]})
    assert debian[0].ecosystem == "Debian", "рядки не мають дробитись по релізах"
    assert debian[0].to_vuln()["ecosystem_raw"] == "Debian:12"


def test_pypi_names_are_lowercased_to_match_pip_audit() -> None:
    """poetry.lock зберігає написання з маніфеста; pip-audit і сканер
    маніфестів обидва нормалізують — інакше це два рядки на один пакет."""
    findings = parse_osv_scanner({"results": [{"source": {"path": "/repo/poetry.lock"},
        "packages": [{"package": {"name": "Django", "version": "3.2.0",
                                  "ecosystem": "PyPI"},
                      "vulnerabilities": [DJANGO_PYSEC]}]}]})
    assert findings[0].package == "django"


def test_unknown_ecosystem_is_reported_not_dropped() -> None:
    assert canonical_ecosystem("Alpine:v3.18") == "Alpine"
    assert canonical_ecosystem("Red Hat:rhel_aus:8.4::appstream") == "Red Hat"
    assert canonical_ecosystem("crates.io") == "crates.io"
    assert canonical_ecosystem("SomethingNewIn2027") == "SomethingNewIn2027"


def test_scanned_ecosystems_counts_clean_packages_too(tmp_path) -> None:
    assert scanned_ecosystems(_report(tmp_path)) == {
        "Maven": 1, "NuGet": 1, "PyPI": 1, "npm": 2,
    }


# ─── Запуск бінарника ────────────────────────────────────────────────


def test_nonzero_exit_means_findings_not_failure(tmp_path) -> None:
    """osv-scanner виходить з кодом 1 САМЕ ТОДІ, коли щось знайшов. Вважати
    це помилкою — тихо втратити весь результат."""
    def runner(cmd, cwd, timeout):
        return CmdResult(True, 1, json.dumps(_report(tmp_path)), "")

    result = audit_repo(tmp_path, runner=runner)

    assert len(result.findings) == 5
    assert result.covered() == {"Maven", "NuGet", "PyPI", "npm"}
    assert all(c.status == "checked" for c in result.checks)
    assert {c.ecosystem: c.findings for c in result.checks} == {
        "Maven": 2, "NuGet": 1, "PyPI": 1, "npm": 1,
    }


def test_clean_repo_is_checked_not_unknown(tmp_path) -> None:
    """Код 0 і жодної уразливості — це «перевірено», а не «не перевіряли»:
    інакше аудит переміряє ту саму блокування через OSV і все одно назве
    її невідомою."""
    payload = {"results": [{"source": {"path": str(tmp_path / "go.sum")},
                            "packages": [{"package": {"name": "golang.org/x/net",
                                                      "version": "0.38.0",
                                                      "ecosystem": "Go"}}]}]}

    result = audit_repo(tmp_path, runner=lambda *_: CmdResult(True, 0, json.dumps(payload), ""))

    assert result.findings == []
    assert result.covered() == {"Go"}


def test_missing_binary_degrades_with_a_machine_readable_reason(tmp_path) -> None:
    def runner(cmd, cwd, timeout):
        return CmdResult(False, -1, "", "",
                         "osv-scanner is not installed in this image", "binary_missing")

    result = audit_repo(tmp_path, runner=runner)

    assert result.findings == []
    assert result.covered() == set(), "нічого не перевірено"
    assert len(result.checks) == 1
    check = result.checks[0]
    assert check.status == "not_checked"
    assert check.reason_code == REASON_MISSING
    assert "not installed" in check.reason
    assert check.as_dict()["reason_code"] == REASON_MISSING, "причина йде у summary"


def test_timeout_degrades_without_raising(tmp_path) -> None:
    result = audit_repo(tmp_path, runner=lambda *_: CmdResult(
        False, -1, "", "", "osv-scanner timed out after 600s", "timeout"))

    assert result.findings == []
    assert result.checks[0].reason_code == REASON_TIMEOUT


def test_malformed_json_is_failed_with_the_tool_error(tmp_path) -> None:
    """Бінарник є, відпрацював, але вивів сміття — окремий стан від
    «бінарника немає»."""
    result = audit_repo(tmp_path, runner=lambda *_: CmdResult(
        True, 1, "<not json at all>", "panic: runtime error: index out of range"))

    assert result.findings == []
    assert result.checks[0].status == "failed"
    assert result.checks[0].reason_code == REASON_BAD_JSON
    assert "panic" in result.checks[0].reason


def test_no_manifests_is_an_honest_empty_not_a_crash(tmp_path) -> None:
    """`results: null` (не []) — саме так виглядає репозиторій без жодного
    файлу залежностей."""
    result = audit_repo(tmp_path, runner=lambda *_: CmdResult(
        True, 0, json.dumps({"results": None}), ""))

    assert result.findings == []
    assert result.checks[0].reason_code == REASON_NO_SOURCES
    assert result.checks[0].status == "not_checked"


def test_v1_binary_falls_back_to_the_older_command_form(tmp_path) -> None:
    """`scan source` з'явилось у v2; старший бінарник валиться на usage-помилці
    ще до будь-якого JSON."""
    seen: list[list[str]] = []

    def runner(cmd, cwd, timeout):
        seen.append(cmd)
        if cmd[1] == "scan":
            return CmdResult(True, 127, "", "flag provided but not defined: -all-vulns")
        return CmdResult(True, 1, json.dumps(_report(tmp_path)), "")

    result = audit_repo(tmp_path, runner=runner)

    assert len(seen) == 2
    assert seen[1][:3] == ["osv-scanner", "--format", "json"]
    assert len(result.findings) == 5


def test_on_progress_can_abort_the_scan(tmp_path) -> None:
    class Cancelled(Exception):
        pass

    def boom(tool, sub):
        raise Cancelled(tool)

    try:
        audit_repo(tmp_path, on_progress=boom,
                   runner=lambda *_: CmdResult(True, 0, "{}", ""))
    except Cancelled:
        return
    raise AssertionError("виняток із on_progress не має проковтуватись")


# ─── Дедуплікація з рештою джерел ────────────────────────────────────


def _osv_api_vuln(**over) -> dict:
    """Форма, яку віддає registries._summarise (шлях через OSV API)."""
    return {"id": "GHSA-5crp-9r3c-p9vr", "cve": "CVE-2024-21907",
            "severity": "high", "summary": "", "fixed_in": None,
            "url": "https://osv.dev/vulnerability/GHSA-5crp-9r3c-p9vr",
            "source": "osv", "aliases": ["CVE-2024-21907"], **over}


def test_same_advisory_from_osv_api_and_osv_scanner_collapses(tmp_path) -> None:
    findings = parse_osv_scanner(_report(tmp_path), repo_path=tmp_path)
    from_scanner = _by_pkg(findings, "Newtonsoft.Json").to_vuln()

    vulns: list[dict] = []
    merge_vuln(vulns, _osv_api_vuln())
    merge_vuln(vulns, from_scanner)

    assert len(vulns) == 1, "один дефект — один рядок"
    assert vulns[0]["source"] == "osv", "перший запис лишається базовим"
    # Але порожні поля добираються з багатшого запису.
    assert vulns[0]["fixed_in"] == "13.0.1"
    assert vulns[0]["summary"].startswith("Improper Handling")


def test_dedup_works_through_aliases_without_a_shared_cve() -> None:
    """pip-audit називає це PYSEC-…, osv-scanner — GHSA-…, і CVE може не бути
    в жодного з них. Склеює саме список аліасів."""
    vulns: list[dict] = []
    merge_vuln(vulns, {"id": "PYSEC-2021-109", "cve": None, "severity": "critical",
                       "source": "pip-audit"})
    merge_vuln(vulns, {"id": "GHSA-xpfp-f569-q3p2", "cve": None, "severity": "critical",
                       "aliases": ["PYSEC-2021-109"], "source": "osv-scanner"})

    assert len(vulns) == 1
    assert "GHSA-xpfp-f569-q3p2" in vulns[0]["aliases"], (
        "об'єднаний запис має лишитись упізнаваним для третього джерела"
    )


def test_different_advisories_are_not_merged() -> None:
    vulns: list[dict] = []
    merge_vuln(vulns, _osv_api_vuln())
    merge_vuln(vulns, _osv_api_vuln(id="GHSA-jfh8-c2jp-5v3q", cve="CVE-2021-44228",
                                    aliases=["CVE-2021-44228"]))

    assert len(vulns) == 2
