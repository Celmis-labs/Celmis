"""A review setting that a repository may override is wired in nine places.

Add a key to `INHERITABLE_FIELDS` and forget one of them and the setting half
works: it saves but is never read back, or it shows on the page but the review
never sees it, or the workspace default cannot hold it. Each of these has
happened. This test walks every key through every place, so the next new
setting fails here, naming the key and the place, instead of in production.

    resolver      BUILTIN_DEFAULTS has a value, the key is in V23_FIELDS (or older)
    storage       a column on `repo_review_policies` AND `workspace_review_defaults`
    API           a field on all four schema classes (repo in/out, workspace in/out)
    web           `api.ts` types it (policy, defaults), `model.ts` lists it, routes it
                  to a section and builds it into both drafts
    UI            the section named for it renders a control for it
    i18n          every text the review-settings components look up exists in `en`
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.api.schemas import (
    ReviewPolicyIn,
    ReviewPolicyOut,
    WorkspaceReviewDefaultsIn,
    WorkspaceReviewDefaultsOut,
)
from src.db.models import RepoReviewPolicy, WorkspaceReviewDefaults
from src.review.review_defaults import (
    BUILTIN_DEFAULTS,
    INHERITABLE_FIELDS,
    V23_FIELDS,
    install_defaults,
    resolve,
)
from src.review.settings import ReviewSettings

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
API_TS = (WEB / "lib" / "api.ts").read_text()
MODEL_TS = (WEB / "components" / "review-settings" / "model.ts").read_text()
SECTIONS = WEB / "components" / "review-settings"
EN = json.loads((WEB / "lib" / "i18n" / "messages" / "en.json").read_text())

#: The page section id (model.ts FIELD_SECTION) -> the component that renders it.
SECTION_FILES = {
    "general": "section-general.tsx",
    "categories": "section-categories.tsx",
    "filters": "section-filters.tsx",
    "prompts": "section-prompts.tsx",
    "summary": "section-summary.tsx",
    "messages": "section-messages.tsx",
    "commands": "section-commands.tsx",
    "learning": "section-learning.tsx",
}

KEYS = pytest.mark.parametrize("key", INHERITABLE_FIELDS)


def test_the_guard_walks_a_real_list() -> None:
    """A renamed constant would make every test below vacuous."""
    assert len(INHERITABLE_FIELDS) >= 23
    assert len(set(INHERITABLE_FIELDS)) == len(INHERITABLE_FIELDS)
    assert set(V23_FIELDS) <= set(INHERITABLE_FIELDS)
    assert V23_FIELDS[-1] == INHERITABLE_FIELDS[-1], (
        "V23_FIELDS must run to the end of the list, so a field appended later is picked up")


@KEYS
def test_the_resolver_knows_a_built_in_value(key: str) -> None:
    install = install_defaults(ReviewSettings())
    # A few keys take their built-in from the install (env) rather than from
    # BUILTIN_DEFAULTS: verifier_enabled, max_inline_comments, suppressed_rules.
    assert key in BUILTIN_DEFAULTS or key in install, f"{key} has no value to inherit"
    values, sources = resolve(None, None, install)
    assert key in values and sources[key] == "install"


@KEYS
def test_both_policy_tables_have_the_column(key: str) -> None:
    for table in (RepoReviewPolicy, WorkspaceReviewDefaults):
        assert key in table.__table__.columns, f"{table.__tablename__} has no column {key}"
        assert table.__table__.columns[key].nullable, f"{key}: NULL must mean inherit"


@KEYS
def test_all_four_schema_classes_carry_it(key: str) -> None:
    for cls in (ReviewPolicyIn, ReviewPolicyOut, WorkspaceReviewDefaultsIn,
                WorkspaceReviewDefaultsOut):
        assert key in cls.model_fields, f"{cls.__name__} has no field {key}"


@KEYS
def test_the_effective_value_is_reported_back(key: str) -> None:
    assert f"{key}_effective" in ReviewPolicyOut.model_fields, f"no {key}_effective on the repo page"


def _ts_mentions(source: str, key: str, pattern: str) -> int:
    return len(re.findall(pattern.format(key=re.escape(key)), source))


@KEYS
def test_api_ts_types_it_for_the_policy_and_for_the_defaults(key: str) -> None:
    assert _ts_mentions(API_TS, key, r"\b{key}\??:") >= 2, (
        f"web/lib/api.ts must type {key} on the policy and on the workspace defaults")


@KEYS
def test_model_ts_lists_routes_and_builds_it(key: str) -> None:
    assert f'"{key}",' in MODEL_TS, f"{key} is not in INHERITABLE_KEYS"
    assert re.search(rf"^\s{{2}}{re.escape(key)}:\s*\"[a-z]+\",", MODEL_TS, re.MULTILINE), (
        f"{key} is not routed to a section in FIELD_SECTION")
    # FIELD_SECTION, the repo payload and the workspace payload.
    assert _ts_mentions(MODEL_TS, key, r"\b{key}:\s") >= 3, (
        f"{key} is not built into both the repo and the workspace payload")


@KEYS
def test_a_section_renders_a_control_for_it(key: str) -> None:
    section = re.search(rf"^\s{{2}}{re.escape(key)}:\s*\"([a-z]+)\",", MODEL_TS, re.MULTILINE)
    assert section, f"{key} has no section in model.ts"
    source = (SECTIONS / SECTION_FILES[section.group(1)]).read_text()
    assert re.search(rf"[\"']{re.escape(key)}[\"']", source), (
        f"{SECTION_FILES[section.group(1)]} has no control for {key}")


def test_model_ts_knows_nothing_the_server_does_not() -> None:
    """`review_language` is the one key the page edits that lives in the LLM
    config blob instead; every other key of the page is an inheritable field."""
    listed = re.search(r"INHERITABLE_KEYS = \[(.*?)\] as const", MODEL_TS, re.DOTALL)
    assert listed
    page_keys = set(re.findall(r'"([a-z_]+)"', listed.group(1)))
    assert page_keys - set(INHERITABLE_FIELDS) == {"review_language"}
    assert set(INHERITABLE_FIELDS) <= page_keys


def test_every_text_a_review_settings_component_looks_up_exists_in_english() -> None:
    wanted: dict[str, str] = {}
    for path in sorted(SECTIONS.glob("*.tsx")):
        for key in re.findall(r"""\bt\(\s*["'`]([A-Za-z0-9_.]+)["'`]""", path.read_text()):
            wanted.setdefault(key, path.name)
    missing = {k: f for k, f in wanted.items() if k not in EN}
    assert wanted, "no t() call found: the pattern or the folder moved"
    assert not missing, f"texts used but not in en.json: {missing}"
