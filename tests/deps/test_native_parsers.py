"""Парсери нативних аудиторів — на ЗАФІКСОВАНИХ прикладах реального виводу.

Жоден тест не запускає сам інструмент: кожен фікстур — це справжня форма
JSON/тексту, яку друкує відповідний аудитор. Саме тому тест ловить регресію
парсера, а не наявність npm/pip-audit на машині.

Головне, що перевіряється всюди: `transitive` та `is_dev` — дві речі, яких
сканер маніфестів не бачив узагалі, і заради яких нативні аудитори й додані.
"""

from __future__ import annotations

import json

from src.deps.native import (
    _json_stream,
    _loads,
    go_direct_modules,
    parse_bundler_audit,
    parse_cargo_audit,
    parse_composer_audit,
    parse_govulncheck,
    parse_npm_audit,
    parse_npm_v1_audit,
    parse_pip_audit,
    parse_yarn_classic_audit,
    run_tool,
)
from src.deps.severity import cvss_base_score, normalize_severity


def _by_pkg(findings, name):
    return next(f for f in findings if f.package == name)


# ─── pip-audit ───────────────────────────────────────────────────────

PIP_AUDIT_JSON = {
    "dependencies": [
        {
            "name": "jinja2",
            "version": "3.1.2",
            "vulns": [
                {
                    "id": "GHSA-h5c8-rqwp-cp95",
                    "fix_versions": ["3.1.3"],
                    "aliases": ["CVE-2024-22195"],
                    "description": "Jinja2 XSS in xmlattr filter",
                }
            ],
        },
        {
            "name": "urllib3",
            "version": "1.26.5",
            "vulns": [
                {
                    "id": "PYSEC-2023-192",
                    "fix_versions": ["1.26.18", "2.0.7"],
                    "aliases": ["CVE-2023-45803", "GHSA-g4mx-q9vg-27p4"],
                    "description": "urllib3 leaks request body on redirect",
                }
            ],
        },
        {"name": "click", "version": "8.1.7", "vulns": []},
    ],
    "fixes": [],
}


def test_pip_audit_marks_transitive_and_picks_lowest_fix() -> None:
    # Маніфест оголошує лише jinja2 — urllib3 приїхав як залежність залежності.
    findings = parse_pip_audit(PIP_AUDIT_JSON, direct={"jinja2"}, subproject="api")

    assert len(findings) == 2, "чисті пакети не повинні створювати знахідок"
    jinja = _by_pkg(findings, "jinja2")
    assert jinja.transitive is False
    assert jinja.version == "3.1.2"
    assert jinja.fixed_in == "3.1.3"
    assert jinja.cve == "CVE-2024-22195"
    assert jinja.source == "pip-audit"
    assert jinja.subproject == "api"

    urllib = _by_pkg(findings, "urllib3")
    assert urllib.transitive is True, "urllib3 немає в маніфесті → транзитивна"
    # Кілька fix_versions → найменша безпечна, а не перша-ліпша.
    assert urllib.fixed_in == "1.26.18"
    assert urllib.cve == "CVE-2023-45803"


def test_pip_audit_dev_flags() -> None:
    by_name = parse_pip_audit(PIP_AUDIT_JSON, direct={"jinja2", "urllib3"},
                              dev={"urllib3"})
    assert _by_pkg(by_name, "urllib3").is_dev is True
    assert _by_pkg(by_name, "jinja2").is_dev is False
    # requirements-dev.txt: увесь результат прогону — dev.
    forced = parse_pip_audit(PIP_AUDIT_JSON, direct={"jinja2"}, force_dev=True)
    assert all(f.is_dev for f in forced)


def test_pip_audit_legacy_bare_list() -> None:
    """pip-audit < 2.0 друкував голий список без обгортки."""
    legacy = PIP_AUDIT_JSON["dependencies"]
    assert len(parse_pip_audit(legacy, direct={"jinja2"})) == 2


def test_pip_audit_without_direct_set_does_not_guess() -> None:
    """Без списку прямих залежностей чесна відповідь — «не транзитивна»,
    а не вигадана."""
    assert all(not f.transitive for f in parse_pip_audit(PIP_AUDIT_JSON))


# ─── npm audit (report v2, npm 7+) ───────────────────────────────────

NPM_AUDIT_V2 = {
    "auditReportVersion": 2,
    "vulnerabilities": {
        "minimist": {
            "name": "minimist",
            "severity": "critical",
            "isDirect": False,
            "via": [
                {
                    "source": 1179,
                    "name": "minimist",
                    "dependency": "minimist",
                    "title": "Prototype Pollution in minimist",
                    "url": "https://github.com/advisories/GHSA-xvch-5gv4-984h",
                    "severity": "critical",
                    "cwe": ["CWE-1321"],
                    "cvss": {"score": 9.8, "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"},
                    "range": "<0.2.1",
                }
            ],
            "effects": ["mkdirp"],
            "range": "<0.2.1",
            "nodes": ["node_modules/mkdirp/node_modules/minimist"],
            "fixAvailable": {"name": "minimist", "version": "1.2.8", "isSemVerMajor": False},
        },
        "mkdirp": {
            "name": "mkdirp",
            "severity": "critical",
            "isDirect": True,
            "via": ["minimist"],
            "effects": [],
            "range": "0.4.1 - 0.5.1",
            "nodes": ["node_modules/mkdirp"],
            "fixAvailable": {"name": "mkdirp", "version": "1.0.4", "isSemVerMajor": True},
        },
        "eslint-utils": {
            "name": "eslint-utils",
            "severity": "moderate",
            "isDirect": False,
            "via": [
                {
                    "source": 1089735,
                    "name": "eslint-utils",
                    "dependency": "eslint-utils",
                    "title": "Prototype pollution in eslint-utils",
                    "url": "https://github.com/advisories/GHSA-3gv2-v4qc-9v7h",
                    "severity": "moderate",
                    "range": "<1.4.1",
                }
            ],
            "effects": ["eslint"],
            "range": "<1.4.1",
            "nodes": ["node_modules/eslint-utils"],
            "fixAvailable": True,
        },
    },
    "metadata": {
        "vulnerabilities": {"info": 0, "low": 0, "moderate": 1, "high": 0,
                            "critical": 2, "total": 3},
        "dependencies": {"prod": 120, "dev": 430, "total": 550},
    },
}


def test_npm_audit_v2_transitive_dev_and_versions() -> None:
    findings = parse_npm_audit(
        NPM_AUDIT_V2,
        installed={"minimist": "0.0.8", "mkdirp": "0.5.1", "eslint-utils": "1.4.0"},
        # Другий прогін (--omit=dev) знайшов лише ці два → eslint-utils dev-only.
        prod_names={"minimist", "mkdirp"},
    )
    assert len(findings) == 3

    minimist = _by_pkg(findings, "minimist")
    assert minimist.transitive is True, "isDirect=false → транзитивна"
    assert minimist.is_dev is False
    assert minimist.severity == "critical"
    assert minimist.vuln_id == "GHSA-xvch-5gv4-984h"
    assert minimist.version == "0.0.8", "версію бере з lock-файлу (npm її не друкує)"
    assert minimist.fixed_in == "1.2.8"

    mkdirp = _by_pkg(findings, "mkdirp")
    assert mkdirp.transitive is False
    # fixAvailable називає САМ пакет → це валідна ціль оновлення.
    assert mkdirp.fixed_in == "1.0.4"
    assert "minimist" in mkdirp.summary, "ланцюг уразливості має бути видимим"

    eslint_utils = _by_pkg(findings, "eslint-utils")
    assert eslint_utils.is_dev is True, "відсутній у --omit=dev прогоні → dev-only"
    assert eslint_utils.severity == "medium", "moderate → medium"
    # fixAvailable=true (без версії) — цілі немає, і вигадувати її не можна.
    assert eslint_utils.fixed_in is None


def test_npm_audit_v2_without_prod_run_does_not_claim_dev() -> None:
    findings = parse_npm_audit(NPM_AUDIT_V2, prod_names=None)
    assert all(f.is_dev is False for f in findings)


def test_npm_audit_never_infers_fix_from_vulnerable_range() -> None:
    """`range: "<0.2.1"` — це діапазон УРАЗЛИВИХ версій. Колишня спокуса взяти
    звідти 0.2.1 як «виправлену» дала б впевнену брехню."""
    payload = json.loads(json.dumps(NPM_AUDIT_V2))
    payload["vulnerabilities"]["minimist"]["fixAvailable"] = False
    minimist = _by_pkg(parse_npm_audit(payload), "minimist")
    assert minimist.fixed_in is None


# ─── npm/pnpm/yarn-berry report v1 ───────────────────────────────────

NPM_AUDIT_V1 = {
    "actions": [],
    "advisories": {
        "1088820": {
            "id": 1088820,
            "github_advisory_id": "GHSA-35jh-r3h4-6jhm",
            "module_name": "lodash",
            "severity": "high",
            "title": "Command Injection in lodash",
            "url": "https://github.com/advisories/GHSA-35jh-r3h4-6jhm",
            "vulnerable_versions": "<4.17.21",
            "patched_versions": ">=4.17.21",
            "cves": ["CVE-2021-23337"],
            "findings": [
                {"version": "4.17.15", "paths": ["lodash"]},
                {"version": "4.17.15", "paths": ["webpack>lodash"], "dev": True},
            ],
        }
    },
    "metadata": {
        "vulnerabilities": {"info": 0, "low": 0, "moderate": 0, "high": 1, "critical": 0},
        "dependencies": 812, "devDependencies": 640, "totalDependencies": 1452,
    },
}


def test_npm_v1_path_determines_transitivity() -> None:
    findings = parse_npm_v1_audit(NPM_AUDIT_V1, source="pnpm-audit")
    assert len(findings) == 2
    direct, via_webpack = findings

    assert direct.transitive is False, 'шлях "lodash" — пряма залежність'
    assert direct.is_dev is False
    assert via_webpack.transitive is True, 'шлях "webpack>lodash" — транзитивна'
    assert via_webpack.is_dev is True

    assert direct.vuln_id == "GHSA-35jh-r3h4-6jhm", "GHSA перемагає числовий id"
    assert direct.cve == "CVE-2021-23337"
    assert direct.fixed_in == "4.17.21"
    assert direct.version == "4.17.15"
    assert direct.severity == "high"
    assert direct.source == "pnpm-audit"


def test_npm_audit_dispatches_v1_payload() -> None:
    """pnpm і yarn-berry друкують v1-форму в ту саму команду — парсер має
    впізнати її за структурою, а не за назвою інструмента."""
    findings = parse_npm_audit(NPM_AUDIT_V1, source="pnpm-audit")
    assert {f.vuln_id for f in findings} == {"GHSA-35jh-r3h4-6jhm"}


# ─── yarn 1 (NDJSON) ─────────────────────────────────────────────────

YARN_CLASSIC_NDJSON = "\n".join([
    json.dumps({"type": "info", "data": "Fetching the latest registry index."}),
    json.dumps({
        "type": "auditAdvisory",
        "data": {
            "resolution": {"id": 1179, "path": "gulp>minimist", "dev": True,
                           "optional": False, "bundled": False},
            "advisory": {
                "id": 1179,
                "github_advisory_id": "GHSA-vh95-rmgr-6w4m",
                "module_name": "minimist",
                "severity": "moderate",
                "title": "Prototype Pollution",
                "url": "https://github.com/advisories/GHSA-vh95-rmgr-6w4m",
                "patched_versions": ">=0.2.1 <1.0.0 || >=1.2.3",
                "cves": ["CVE-2020-7598"],
                "findings": [{"version": "0.0.10", "paths": ["gulp>minimist"]}],
            },
        },
    }),
    json.dumps({"type": "auditSummary", "data": {"vulnerabilities": {"moderate": 1}}}),
])


def test_yarn_classic_ndjson_resolution_flags() -> None:
    findings = parse_yarn_classic_audit(YARN_CLASSIC_NDJSON, subproject="web")
    assert len(findings) == 1
    f = findings[0]
    assert f.package == "minimist"
    assert f.version == "0.0.10"
    assert f.is_dev is True, "resolution.dev — найточніший сигнал серед усіх тулів"
    assert f.transitive is True, 'resolution.path "gulp>minimist"'
    assert f.vuln_id == "GHSA-vh95-rmgr-6w4m"
    assert f.cve == "CVE-2020-7598"
    assert f.fixed_in == "0.2.1"
    assert f.severity == "medium"
    assert f.subproject == "web"
    assert f.source == "yarn-audit"


# ─── govulncheck ─────────────────────────────────────────────────────

GOVULNCHECK_STREAM = "".join(json.dumps(obj) for obj in [
    {"config": {"protocol_version": "v1.0.0", "scanner_name": "govulncheck"}},
    {"progress": {"message": "Scanning your code and 245 packages..."}},
    {"osv": {
        "id": "GO-2022-0969",
        "aliases": ["CVE-2022-41717", "GHSA-xrjj-mj9h-534m"],
        "summary": "Excessive memory growth in net/http and golang.org/x/net/http2",
        "affected": [{
            "package": {"name": "golang.org/x/net", "ecosystem": "Go"},
            "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"},
                                                     {"fixed": "0.4.0"}]}],
        }],
    }},
    {"osv": {
        "id": "GO-2023-1571",
        "aliases": ["CVE-2023-29403"],
        "summary": "Privilege escalation in runtime",
        "affected": [{"package": {"name": "stdlib", "ecosystem": "Go"}}],
    }},
    {"finding": {
        "osv": "GO-2022-0969",
        "fixed_version": "v0.4.0",
        "trace": [{"module": "golang.org/x/net", "version": "v0.1.0",
                   "package": "golang.org/x/net/http2"}],
    }},
    # Той самий advisory, але вже із символом — це «функцію реально викликають».
    {"finding": {
        "osv": "GO-2022-0969",
        "fixed_version": "v0.4.0",
        "trace": [
            {"module": "golang.org/x/net", "version": "v0.1.0",
             "package": "golang.org/x/net/http2", "function": "readContinuationFrame"},
            {"module": "example.com/app", "package": "example.com/app",
             "function": "main"},
        ],
    }},
    {"finding": {
        "osv": "GO-2023-1571",
        "fixed_version": "go1.20.5",
        "trace": [{"module": "stdlib", "version": "go1.20.1", "package": "os"}],
    }},
])


def test_govulncheck_stream_dedupes_and_prefers_called_symbol() -> None:
    findings = parse_govulncheck(
        GOVULNCHECK_STREAM, direct={"golang.org/x/net"}, subproject="")
    assert len(findings) == 2, "два finding-и на один advisory+модуль → один рядок"

    net = _by_pkg(findings, "golang.org/x/net")
    assert net.version == "0.1.0", "версію беремо з нульового кадру трасування"
    assert net.fixed_in == "0.4.0"
    assert net.cve == "CVE-2022-41717"
    assert net.transitive is False, "модуль є прямим require у go.mod"
    assert "imported, not called" not in net.summary, "символ викликається"

    stdlib = _by_pkg(findings, "stdlib")
    assert stdlib.transitive is True
    assert "imported, not called" in stdlib.summary, (
        "різниця між «імпортовано» і «викликається» — головна цінність govulncheck"
    )


def test_go_direct_modules_respects_indirect_marker() -> None:
    go_mod = """module example.com/app

go 1.22

require (
\tgolang.org/x/net v0.1.0
\tgithub.com/gin-gonic/gin v1.9.1
\tgolang.org/x/text v0.3.7 // indirect
)

require github.com/stretchr/testify v1.8.4
"""
    direct = go_direct_modules(go_mod)
    assert "golang.org/x/net" in direct
    assert "github.com/gin-gonic/gin" in direct
    assert "github.com/stretchr/testify" in direct, "однорядковий require теж прямий"
    assert "golang.org/x/text" not in direct, "// indirect — це і є транзитивність"


# ─── cargo audit ─────────────────────────────────────────────────────

CARGO_AUDIT_JSON = {
    "database": {"advisory-count": 610},
    "lockfile": {"dependency-count": 243},
    "vulnerabilities": {
        "found": True,
        "count": 2,
        "list": [
            {
                "advisory": {
                    "id": "RUSTSEC-2020-0071",
                    "package": "time",
                    "title": "Potential segfault in the time crate",
                    "url": "https://rustsec.org/advisories/RUSTSEC-2020-0071",
                    "aliases": ["CVE-2020-26235", "GHSA-wcg3-cvx6-7396"],
                    "cvss": "CVSS:3.1/AV:L/AC:H/PR:N/UI:R/S:C/C:H/I:H/A:H",
                    "categories": ["memory-corruption"],
                },
                "versions": {"patched": [">=0.2.23"], "unaffected": ["<0.2.7"]},
                "package": {"name": "time", "version": "0.1.44",
                            "source": "registry+https://github.com/rust-lang/crates.io-index"},
            },
            {
                "advisory": {
                    "id": "RUSTSEC-2021-0079",
                    "package": "hyper",
                    "title": "Integer overflow in hyper's parsing of Transfer-Encoding",
                    "url": "https://rustsec.org/advisories/RUSTSEC-2021-0079",
                    "aliases": ["CVE-2021-32714"],
                    "cvss": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
                },
                "versions": {"patched": [">=0.14.10"]},
                "package": {"name": "hyper", "version": "0.14.4",
                            "source": "registry+https://github.com/rust-lang/crates.io-index"},
            },
        ],
    },
    "warnings": {},
}


def test_cargo_audit_computes_severity_from_cvss_vector() -> None:
    findings = parse_cargo_audit(CARGO_AUDIT_JSON, direct={"hyper"})
    assert len(findings) == 2

    hyper = _by_pkg(findings, "hyper")
    assert hyper.severity == "critical", "9.8 з вектора, а не дефолтне medium"
    assert hyper.transitive is False
    assert hyper.fixed_in == "0.14.10"
    assert hyper.cve == "CVE-2021-32714"
    assert hyper.vuln_id == "RUSTSEC-2021-0079"

    time = _by_pkg(findings, "time")
    assert time.transitive is True, "time немає в Cargo.toml → транзитивна"
    assert time.version == "0.1.44"
    assert time.severity == "high", "6.6…7.0 діапазон scope-changed вектора"


def test_cargo_audit_clean_lockfile() -> None:
    assert parse_cargo_audit({"vulnerabilities": {"found": False, "count": 0,
                                                  "list": []}}) == []


# ─── composer audit ──────────────────────────────────────────────────

COMPOSER_AUDIT_JSON = {
    "advisories": {
        "guzzlehttp/guzzle": [
            {
                "advisoryId": "PKSA-1234-5678-9012",
                "packageName": "guzzlehttp/guzzle",
                "affectedVersions": ">=7.0.0,<7.4.5",
                "title": "Change in port should be considered a change in origin",
                "cve": "CVE-2022-31090",
                "link": "https://github.com/guzzle/guzzle/security/advisories/GHSA-q559-8m2m-g699",
                "reportedAt": "2022-06-09T21:11:00+00:00",
                "severity": "medium",
                "sources": [{"name": "GitHub", "remoteId": "GHSA-q559-8m2m-g699"}],
            }
        ]
    },
    "abandoned": {"swiftmailer/swiftmailer": "symfony/mailer"},
}


def test_composer_audit_advisories_and_abandoned_stay_separable() -> None:
    findings = parse_composer_audit(
        COMPOSER_AUDIT_JSON,
        installed={"guzzlehttp/guzzle": "7.4.0", "swiftmailer/swiftmailer": "6.2.7"},
        direct={"guzzlehttp/guzzle"},
    )
    guzzle = _by_pkg(findings, "guzzlehttp/guzzle")
    assert guzzle.ecosystem == "Packagist"
    assert guzzle.version == "7.4.0"
    assert guzzle.severity == "medium"
    assert guzzle.cve == "CVE-2022-31090"
    assert guzzle.transitive is False

    abandoned = _by_pkg(findings, "swiftmailer/swiftmailer")
    assert abandoned.severity == "low", "покинутий пакет — не CVE, не роздуваємо"
    assert "symfony/mailer" in abandoned.summary
    assert abandoned.transitive is True


# ─── bundler-audit ───────────────────────────────────────────────────

BUNDLER_AUDIT_TEXT = """Updating ruby-advisory-db ...
Name: actionpack
Version: 6.0.3.2
CVE: 2020-8264
GHSA: 35mm-cc6r-8fjp
Criticality: Medium
URL: https://groups.google.com/g/rubyonrails-security/c/yQzUVfv42jk
Title: Possible XSS Vulnerability in Action Pack in development mode
Solution: upgrade to ~> 6.0.3, >= 6.0.3.3

Name: nokogiri
Version: 1.10.9
CVE: 2020-26247
Criticality: High
URL: https://github.com/sparklemotion/nokogiri/security/advisories/GHSA-vr8q-g5c7-m54m
Title: Nokogiri::XML::Schema trusts user-provided content
Solution: upgrade to >= 1.11.0.rc4

Vulnerabilities found!
"""


def test_bundler_audit_text_blocks() -> None:
    findings = parse_bundler_audit(BUNDLER_AUDIT_TEXT, direct={"actionpack"})
    assert len(findings) == 2

    actionpack = _by_pkg(findings, "actionpack")
    assert actionpack.ecosystem == "RubyGems"
    assert actionpack.version == "6.0.3.2"
    assert actionpack.severity == "medium"
    assert actionpack.vuln_id == "GHSA-35mm-cc6r-8fjp", "GHSA нормалізується з префіксом"
    assert actionpack.cve == "CVE-2020-8264"
    assert actionpack.fixed_in == "6.0.3"
    assert actionpack.transitive is False

    nokogiri = _by_pkg(findings, "nokogiri")
    assert nokogiri.severity == "high"
    assert nokogiri.vuln_id == "CVE-2020-26247", "без GHSA ідентифікатором стає CVE"
    assert nokogiri.transitive is True


# ─── допоміжне ───────────────────────────────────────────────────────


def test_loads_tolerates_leading_banner() -> None:
    """Кілька інструментів друкують попередження перед JSON."""
    assert _loads('npm warn config production\n{"ok": true}') == {"ok": True}
    assert _loads("") is None
    assert _loads("not json at all") is None


def test_json_stream_handles_concatenated_and_ndjson() -> None:
    assert _json_stream('{"a":1}{"b":2}') == [{"a": 1}, {"b": 2}]
    assert _json_stream('{"a":1}\n{"b":2}\n') == [{"a": 1}, {"b": 2}]
    assert _json_stream("garbage\n{\"a\":1}") == [{"a": 1}]


def test_run_tool_reports_missing_binary_as_not_run(tmp_path) -> None:
    """Найважливіше розрізнення в модулі: «інструмента немає» ≠ «нічого не
    знайдено». Перше має долетіти до UI як not_checked із причиною."""
    result = run_tool(["definitely-not-a-real-auditor-xyz", "--json"], tmp_path)
    assert result.ok is False
    assert "not installed" in result.error


def test_run_tool_treats_nonzero_exit_as_a_result(tmp_path) -> None:
    """Усі аудитори виходять з кодом 1, коли ЗНАЙШЛИ вразливості. Якщо
    вважати це помилкою — уразливий репозиторій отримає чистий вирок."""
    result = run_tool(["sh", "-c", 'printf "{\\"found\\": 1}"; exit 1'], tmp_path)
    assert result.ok is True, "ненульовий код — це знахідки, а не збій команди"
    assert result.code == 1
    assert _loads(result.stdout) == {"found": 1}


def test_severity_vocabularies_converge() -> None:
    assert normalize_severity("moderate") == "medium"      # GHSA/npm
    assert normalize_severity("info") == "low"             # npm
    assert normalize_severity("CRITICAL") == "critical"
    assert normalize_severity("") == "medium", "advisory існує → не 'none'"
    assert normalize_severity("", default="none") == "none"
    # OSV віддає вектор, не число — без обчислення все злипалося б у medium.
    assert cvss_base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H") == 9.8
    assert cvss_base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N") == 6.1
    assert cvss_base_score("CVSS:2.0/AV:N/AC:L/Au:N/C:P") is None
    assert cvss_base_score("7.5") == 7.5
