"""The wizard's last step shows a finding instead of asking for another key.

Six steps of configuration — workspace, LLM key, git, index, vault, agent —
ran before the product had demonstrated anything at all. Ten minutes of setup
is a long time to spend on a tool whose whole argument is a finding you can
check in five seconds, and the order made the argument last.

So step 7 runs the dependency audit and puts ONE finding on screen, chosen for
being PROVEN: a lock file that disagrees with its manifest, a package that runs
a script at install, a dependency fetched from outside the registry. Those need
no defending — the reader opens the file at that line and agrees or disagrees.
A model's finding in this position would ask for trust that has not been earned
yet, on the first thing anybody sees.

Pinned here because every part of it is quiet to break: a step dropped from the
`steps` array still renders while the progress bar silently stops counting it,
and a hygiene kind appended to the proven list looks like every other string.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
ONBOARDING = (WEB / "app" / "(app)" / "onboarding" / "page.tsx").read_text()
STEP = (WEB / "components" / "first-proof-step.tsx").read_text()
DEPENDENCIES = (WEB / "app" / "(app)" / "dependencies" / "page.tsx").read_text()
API = (WEB / "lib" / "api.ts").read_text()
MESSAGES = WEB / "lib" / "i18n" / "messages"
EN = json.loads((MESSAGES / "en.json").read_text(encoding="utf-8"))

LOCALES = sorted(p.stem for p in MESSAGES.glob("*.json"))
PROOF_KEYS = [k for k in EN if k.startswith("onboarding.proof.")]


def _strip_comments(source: str) -> str:
    """Comments here NAME what they replaced in order to explain it.

    Grepping them is how a guard passes while the thing it guards is gone,
    which has happened on this repo more than once.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"^\s*//.*$", "", source, flags=re.M)


# ─── the step is a step, not a decoration ────────────────────────────


def test_the_step_is_counted_by_the_progress_bar():
    """Rendering a seventh card while `steps` still holds six reads as 6/6
    "all done" with an unfinished step below it."""
    body = _strip_comments(ONBOARDING)
    start = body.find("const steps = [")
    assert start > 0, "the step list is gone"
    steps = body[start:body.find("];", start)]
    assert "proofDone" in steps, "step 7 is not counted in the progress bar"
    assert "<StepShell n={7}" in body


def test_done_is_read_from_a_finished_audit_and_not_from_a_visit():
    """The other six read live system state, so this one must too — a step
    that ticks itself because the card was scrolled past is a poster."""
    body = _strip_comments(ONBOARDING)
    assert 'depsRun.data?.status === "done"' in body
    assert "localStorage" not in body[body.find("const proofDone"):
                                      body.find("const proofDone") + 200]


def test_the_step_and_the_page_share_one_request():
    """Two query keys for the same run is two audits' worth of polling and a
    card that can disagree with the tick beside it."""
    assert ONBOARDING.count('"deps-latest-onboarding"') == 1
    assert STEP.count('"deps-latest-onboarding"') >= 1


# ─── proven, and only proven ─────────────────────────────────────────


def test_only_facts_can_be_the_first_thing_shown():
    """The badge says "Proven". Adding a kind to this list is a one-word edit
    that can make the badge a lie on the first screen a new user sees."""
    body = _strip_comments(STEP)
    start = body.find("const PROVEN_KINDS")
    assert start > 0, "the allow-list is gone"
    kinds = set(re.findall(r'"([a-z_]+)"', body[start:body.find("]", start)]))
    assert kinds <= {"lock_drift", "install_script", "non_registry"}, (
        f"a kind that is not a fact is being shown as proven: {kinds}"
    )


def test_the_typosquat_guess_is_not_presented_as_proof():
    """`suspect_name` is edit distance <= 1 against a hard-coded list of ~150
    popular names, and its own message ends "confirm this is the package you
    meant" — a check that asks the reader to confirm is not one that proves.

    Deterministic and correct are different properties: the computation is
    exact, the conclusion is a guess, and the badge is about the conclusion. It
    belongs on the dependencies page with the other hygiene findings, not under
    "Proven" on the first screen anybody sees.
    """
    body = _strip_comments(STEP)
    start = body.find("const PROVEN_KINDS")
    kinds = set(re.findall(r'"([a-z_]+)"', body[start:body.find("]", start)]))
    assert "suspect_name" not in kinds

    # And it is still a real check that still reaches the user elsewhere.
    hygiene = (ROOT / "src" / "deps" / "hygiene.py").read_text()
    assert 'KIND_SUSPECT_NAME = "suspect_name"' in hygiene
    assert "deps.hygieneKind.suspect_name" in EN


def test_the_evidence_itself_is_on_screen():
    """`detail` is prose and `location` is a path; neither is the proof. A
    deterministic check whose evidence cannot be looked at reads, to the person
    reading it, exactly like a guess."""
    body = _strip_comments(STEP)
    assert "proven.excerpt" in body, "the line's text is not rendered"
    assert "proven.line" in body, "the line number is not rendered"


def test_an_item_carrying_its_evidence_is_preferred_over_one_that_does_not():
    """npm records `hasInstallScript` as a boolean and never the script body,
    so some items genuinely have no excerpt. Picking one of those while a fully
    evidenced item of the same kind sits in the list wastes the demonstration."""
    assert "withEvidence" in _strip_comments(STEP)


def test_a_clean_result_does_not_read_as_a_failure():
    """Finding nothing is the good outcome, and the SBOM is still the
    deliverable."""
    body = _strip_comments(STEP)
    assert "onboarding.proof.allClean" in body
    assert "deps.downloadSbom" in body, "the SBOM button is gone"
    assert "deps.downloadEvidence" in body


# ─── the three things the card used to say that were not true ────────


def test_a_download_carries_credentials():
    """These were `<a href={sbomUrl} download>`, which is a browser navigation:
    it sends cookies and no Authorization header. Every export endpoint reads
    that header and nothing else — `get_current_user` has no cookie fallback,
    and the Next proxy is page-routing only (its matcher excludes /api), so it
    injects none either.

    So "Download SBOM" saved a file containing
    `{"detail":"Missing or invalid Authorization header"}` — verified against
    production in a real browser session before this was fixed. The one
    artefact procurement asks for by name, delivered as an error body.
    """
    for name, source in (("step", STEP), ("dependencies page", DEPENDENCIES)):
        body = _strip_comments(source)
        for url in ("depsApi.sbomUrl", "depsApi.evidenceUrl"):
            i = body.find(url)
            assert i > 0, f"{name}: {url} is no longer used"
            # The failure shape is an anchor whose href is the API URL.
            window = body[max(0, i - 200):i]
            assert "<a href=" not in window, (
                f"{name}: {url} is back inside an <a href>, which sends no "
                "Authorization header and downloads a 401 body"
            )
        assert "downloadWithAuth" in body, f"{name}: not using the auth helper"

    api = _strip_comments(API)
    assert "export async function downloadWithAuth" in api
    # requestHeaders, not a hand-rolled bearer: it also carries X-Workspace,
    # without which the export resolves to the account's DEFAULT workspace.
    helper = api[api.find("export async function downloadWithAuth"):]
    assert "requestHeaders(token)" in helper[:800]


def test_a_failed_audit_says_so_instead_of_looking_unstarted():
    """`status === "error"` set neither `done` nor `running`, so the only thing
    that rendered was the Run button again, with no explanation — identical to
    "never started". The endpoint answers 202 with status "error" and the
    recovery instruction in `run.error` when the queue slot is held; the card
    toasted "Audit started" over it and the instruction was never shown, so
    clicking again reproduced it forever.
    """
    body = _strip_comments(STEP)
    assert 'run?.status === "error"' in body, "the error state is still invisible"
    assert "run?.error" in body, "the recovery instruction is not rendered"
    # The toast must branch on what the endpoint actually returned.
    onsuccess = body[body.find("onSuccess:"):]
    assert 'r?.status === "error"' in onsuccess[:400], (
        "a refused start is still announced as success"
    )


def test_an_audit_that_read_nothing_is_not_called_clean():
    """The auditor writes status "done" even when every repository failed to
    clone: there is no guard on an empty scanned list. So an empty hygiene list
    means either "your supply chain is clean" or "no file was ever opened", and
    the card said the first — a proven-clean bill of health and an empty SBOM,
    to a first-run user, off a run that read nothing.

    The dependencies page already renders exactly this warning; the new card
    did not.
    """
    body = _strip_comments(STEP)
    assert "repos_scanned" in body, "the card still ignores what was scanned"
    assert "nothingScanned" in body
    # allClean must not be reachable when nothing was read.
    i = body.find("onboarding.proof.allClean")
    assert i > 0
    assert "!nothingScanned" in body[max(0, i - 300):i], (
        "the clean bill of health can still be shown for a zero-scan run"
    )


def test_the_wording_does_not_promise_a_line_that_is_not_there():
    """npm's lock records `hasInstallScript` as a boolean and never the script
    body, so an install-script finding has a file and no line — and it is the
    finding most likely to be shown first. "Open the file at that line" then
    points at something that is not on screen, in the one card whose whole
    argument is that you need not take its word for anything."""
    body = _strip_comments(STEP)
    assert "onboarding.proof.whyProvenNoLine" in body
    assert "proven.line" in body[body.find("whyProvenNoLine") - 300:]


def test_the_subtitle_agrees_with_the_step_count():
    """It sat directly above a counter reading 6/7 and said "Six steps"."""
    page = _strip_comments(ONBOARDING)
    start = page.find("const steps = [")
    count = page[start:page.find("];", start)].count(",") + 1
    assert count == 7, f"the step list has {count} entries"
    for locale in LOCALES:
        data = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
        subtitle = data["onboarding.wizardSubtitle"].lower()
        for stale in ("six steps", "шість крок", "sechs schritte", "六步",
                      "sei passi", "seis pasos", "sept étapes".replace("sept", "six")):
            assert stale not in subtitle, f"{locale} still promises six steps"


def test_the_audit_needs_no_llm_key_and_says_so():
    """It is the one capability that works before step 2, which is exactly why
    it can be the demonstration."""
    assert "onboarding.proof.noKeyNeeded" in _strip_comments(STEP)


# ─── the duplicate type that hid the evidence ────────────────────────


def test_the_hygiene_shape_is_declared_once():
    """It was declared twice — in api.ts and again in the dependencies page —
    and the copy in the page silently lacked `line` and `excerpt`. The auditor
    extracted the evidence, the API returned it, and the page dropped it, with
    nothing failing anywhere.

    A second structural declaration is the failure mode, so it is the thing
    banned; an alias to the shared type is fine.
    """
    body = _strip_comments(DEPENDENCIES)
    match = re.search(r"type HygieneItem\s*=\s*(.)", body)
    assert match, "HygieneItem is gone from the dependencies page"
    assert match.group(1) != "{", (
        "the page re-declares the hygiene shape instead of importing it; "
        "that is how line/excerpt went missing the first time"
    )
    assert "type HygieneItem = {" in API, "the shared declaration is gone"
    for field in ("line?: number | null", "excerpt?: string"):
        assert field in API, f"the shared type lost {field}"


def test_the_dependencies_page_renders_the_evidence_too():
    """The step shows one finding; the page shows the rest, and it had the same
    hole."""
    body = _strip_comments(DEPENDENCIES)
    hygiene = body[body.find('t("deps.hygieneTitle")'):]
    assert "item.excerpt" in hygiene, "the page still hides the excerpt"
    assert "item.line" in hygiene, "the page still hides the line number"


# ─── the strings ─────────────────────────────────────────────────────


def test_the_step_actually_has_strings_to_show():
    assert len(PROOF_KEYS) >= 8, f"only {len(PROOF_KEYS)} proof keys"


@pytest.mark.parametrize("locale", LOCALES)
def test_every_locale_carries_the_new_keys(locale: str):
    data = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    missing = [k for k in PROOF_KEYS if k not in data]
    assert not missing, f"{locale} is missing {missing}"


@pytest.mark.parametrize("locale", LOCALES)
def test_no_locale_falls_back_to_english(locale: str):
    """A key present in every file but holding the English string is the same
    outage as a missing key, and it passes a completeness test."""
    if locale == "en":
        pytest.skip("it is the English")
    data = json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf-8"))
    untranslated = [k for k in PROOF_KEYS if data.get(k) == EN[k]]
    assert not untranslated, f"{locale} still shows English for {untranslated}"


def test_no_russian_reached_the_catalogue():
    """A hard product rule, and Ukrainian is the locale a machine translation
    slips into."""
    assert not (MESSAGES / "ru.json").exists()
    uk = json.loads((MESSAGES / "uk.json").read_text(encoding="utf-8"))
    for key in PROOF_KEYS:
        # ы, э, ъ, ё do not occur in Ukrainian.
        assert not set("ыэъё") & set(uk[key].lower()), f"{key}: {uk[key]}"
