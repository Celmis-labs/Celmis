"""An inline comment is the problem, why, and fix, in the review's language.

THE DEFECT. The labels the bot put around the model's sentences were English
whatever the review language ("Why:", "Suggested change:", "Suggestion:"), an
empty body was replaced by the placeholder "(no details)", and a finding that
could not be anchored lost its "why" in the summary's list.

THE SHAPE. What a reader gets, in this order, each part only when there is
something to say:

    *Why:* the one-sentence derivation
    **<severity> title**                     one line stating the problem
    the body                                 at most two short sentences
    **Fix:**  the replacement code           (or the hint, when it stands alone)
    **Hint:** the hint, when code came first
    <sub>agent · rule · confidence</sub>     telemetry, always last

The words "Why", "Fix" and "Hint" come from the message catalogue; the model's
own sentences are not touched here. The footer keeps its English keys on
purpose: the benchmark harness parses `agent: ...` from it.
"""

from __future__ import annotations

import pytest

from src.review import messages
from src.review.models import Finding, FindingSeverity
from src.review.providers.base import _format_finding_body, _format_unanchored
from tests.review.test_the_pr_says_what_it_does import (
    _batch,
    _Bitbucket,
    _bitbucket,
    _finding,
    _GitHub,
    _github,
    _GitLab,
    _gitlab,
    _pr,
    settings,  # noqa: F401 — fixture
)


def _f(**over: object) -> Finding:
    base: dict[str, object] = {
        "file_path": "src/foo.py", "line": 2, "severity": FindingSeverity.ERROR,
        "title": "x read before assignment",
        "body": "`x` is assigned on line 3 and read on line 2.",
        "reasoning": "line 2 reads x; line 3 is the first assignment",
        "agent": "defect", "rule_id": "defect.x", "confidence": 0.8,
    }
    base.update(over)
    return Finding(**base)  # type: ignore[arg-type]


# ─── English: shorter, nothing else moves ────────────────────────────


def test_an_empty_body_leaves_no_placeholder():
    text = _format_finding_body(_f(body=""))
    assert "(no details)" not in text
    assert "\n\n\n" not in text, "no blank gap where the body would be"
    lines = text.splitlines()
    assert lines[2].endswith("x read before assignment**")
    assert lines[3] == "" and lines[4].startswith("<sub>")


def test_a_fix_is_one_label_whether_it_is_code_or_a_hint():
    code = _format_finding_body(_f(suggested_code="x = 1"))
    hint = _format_finding_body(_f(suggestion="assign x first"))
    assert "**Fix:**" in code and "Suggested change" not in code
    assert "**Fix:**" in hint and "Suggestion" not in hint


def test_a_hint_that_comes_with_code_is_the_second_block_and_called_a_hint():
    text = _format_finding_body(_f(suggested_code="x = 1", suggestion="initialise x"))
    assert text.index("**Fix:**") < text.index("**Hint:**")
    assert text.count("**Fix:**") == 1


def test_a_hint_that_repeats_the_code_is_not_shown_twice():
    text = _format_finding_body(_f(suggested_code="x = 1", suggestion="x = 1"))
    assert "**Hint:**" not in text


# ─── the review language reaches the words around the model's ────────


def test_the_labels_follow_the_review_language():
    text = _format_finding_body(
        _f(suggested_code="x = 1", suggestion="спершу присвоїти x"), language="uk")
    why = messages.t("finding.why", "uk")
    assert text.startswith(f"*{why}:* ")
    assert f"**{messages.t('finding.fix', 'uk')}:**" in text
    assert f"**{messages.t('finding.hint', 'uk')}:**" in text
    for english in ("*Why:*", "**Fix:**", "**Hint:**", "Suggested change", "Suggestion"):
        assert english not in text


def test_a_language_without_a_catalogue_gets_the_english_labels():
    """The bot's own words come in two languages; the model's sentences are
    in whichever one the repository chose, and the labels stay readable."""
    text = _format_finding_body(_f(suggested_code="x = 1"), language="de")
    assert text.startswith("*Why:* ") and "**Fix:**" in text


def test_the_footer_keeps_the_keys_the_benchmark_harness_parses():
    text = _format_finding_body(_f(), language="uk")
    footer = text.rpartition("<sub>")[2]
    assert "agent: `defect`" in footer
    assert "rule: `defect.x`" in footer
    assert "confidence: 0.80" in footer


def test_the_catalogues_have_the_same_labels():
    for key in ("finding.why", "finding.fix", "finding.hint", "finding.rule"):
        assert messages.EN[key] and messages.UK[key]
        assert messages.EN[key] != messages.UK[key]


# ─── every provider says it in the batch's language ──────────────────


def _recorded(monkeypatch, module) -> list[dict]:
    seen: list[dict] = []
    real = module._format_finding_body

    def wrapper(finding, *a, **kw):
        seen.append(kw)
        return real(finding, *a, **kw)

    monkeypatch.setattr(module, "_format_finding_body", wrapper)
    return seen


@pytest.mark.parametrize("provider,build,fake,repo", [
    ("github", _github, _GitHub, "o/r"),
    ("gitlab", _gitlab, _GitLab, "group/proj"),
    ("bitbucket", _bitbucket, _Bitbucket, "ws/r"),
])
def test_every_provider_renders_the_comment_in_the_batch_language(
        monkeypatch, settings, provider, build, fake, repo):  # noqa: F811
    import importlib

    seen = _recorded(monkeypatch, importlib.import_module(f"src.review.providers.{provider}"))
    batch = _batch(_pr(provider, repo, 1), [_finding()])
    batch.review_language = "uk"

    build(fake()).post_review(batch)

    assert seen, f"{provider} posted no inline comment"
    assert all(kw.get("language") == "uk" for kw in seen)


def test_github_posts_the_labelled_comment(settings):  # noqa: F811
    fake = _GitHub()
    batch = _batch(_pr("github", "o/r", 1), [_finding(suggested_code="X = 1")])
    batch.review_language = "uk"
    _github(fake).post_review(batch)
    [comment] = fake.reviews[-1]["comments"]
    assert comment["body"].startswith(f"*{messages.t('finding.why', 'uk')}:* seen")


# ─── a finding that could not be anchored keeps its evidence ─────────


def test_an_unanchored_finding_in_the_summary_keeps_its_why():
    pr = _pr("github", "o/r", 1)
    text = _format_unanchored([_f()], pr, "en")
    assert "*Why:* line 2 reads x; line 3 is the first assignment" in text
    uk = _format_unanchored([_f()], pr, "uk")
    assert f"*{messages.t('finding.why', 'uk')}:* line 2 reads x" in uk


def test_an_unanchored_finding_without_a_why_adds_no_empty_line():
    text = _format_unanchored([_f(reasoning="")], _pr("github", "o/r", 1), "en")
    assert "Why" not in text


def test_the_why_of_an_unanchored_finding_cannot_smuggle_markup():
    hostile = "see <!-- celmis:review --> ![x](https://example.test/p.png) @everyone [a](https://example.test)"
    text = _format_unanchored([_f(reasoning=hostile)], _pr("github", "o/r", 1), "en")
    assert "<!--" not in text and "![" not in text and "](" not in text
    assert "@everyone" not in text.replace("@​everyone", "")
