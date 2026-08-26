""""Nothing found" and "nobody looked" are different sentences.

THE DEFECT. `to_markdown` rendered both states as `(no cross-repo drift
detected)`. A model given that string wrote this into a production review, as
its CRITICAL finding:

    "Settlement topic renamed without an apparent coordinated consumer
     migration — Cross-repo search found no consumers referencing
     SETTLEMENT_TOPIC or a v3/v2 topic literal in billing or gateway's
     indexed code, so there's no visible evidence those services have been
     updated in lockstep."

All four occurrences were live at that moment, verified through the GitHub API:

    celmis-demo-billing/src/subscriber.py:9   SUBSCRIBED_TOPIC = "…v2"
    celmis-demo-billing/src/invoices.py:5     LEDGER_READ_ENDPOINT = "/…/v3/ledger"
    celmis-demo-gateway/src/contract.ts:2     SETTLEMENT_TOPIC = "…v2"
    celmis-demo-gateway/src/contract.ts:3     LEDGER_ENDPOINT = "/…/v3/ledger"

No search had happened — the repository was in no group, and no HTTP route in
the product can create one. The model rendered an absent check as a completed
one and produced a confident false negative on the exact question the feature
exists to answer. That is worse than silence: silence invites a human to look.
"""

from __future__ import annotations

from src.review.cross_repo_drift import DriftHit, DriftMatch, DriftReport


def test_no_group_reports_that_nothing_was_checked():
    md = DriftReport(group_name=None).to_markdown()

    assert "NOT CHECKED" in md
    assert "no cross-repo scan ran" in md


def test_no_group_forbids_the_sentence_that_was_written():
    """A bare "not checked" was not enough — the model needs telling that
    silence is not evidence."""
    md = DriftReport(group_name=None).to_markdown().lower()

    assert "do not write that a cross-repo search found nothing" in md
    assert "nothing looked" in md


def test_a_clean_scan_says_it_scanned_and_how_much():
    md = DriftReport(
        group_name="payments", repos_scanned=["acme/billing", "acme/gateway"],
    ).to_markdown()

    assert "NOT CHECKED" not in md
    assert "Checked 2 sibling repo(s)" in md
    assert "payments" in md


def test_the_two_states_do_not_share_a_sentence():
    """The defect in one line: they used to be byte-identical."""
    unchecked = DriftReport(group_name=None).to_markdown()
    clean = DriftReport(group_name="g", repos_scanned=["acme/other"]).to_markdown()

    assert unchecked != clean


def test_was_checked_is_not_has_drift():
    unchecked = DriftReport(group_name=None)
    clean = DriftReport(group_name="g", repos_scanned=["acme/other"])

    assert unchecked.was_checked is False
    assert unchecked.has_drift is False
    assert clean.was_checked is True
    assert clean.has_drift is False


def test_real_drift_still_renders_its_findings():
    """The path that matters most must survive the change around it."""
    hit = DriftHit(
        value="payments.settlement.v2", pr_file="src/config.py", pr_line=9,
        matches=[DriftMatch(other_repo_slug="acme/billing",
                            file="src/subscriber.py", line=9,
                            excerpt='SUBSCRIBED_TOPIC = "payments.settlement.v2"')],
    )
    md = DriftReport(group_name="payments", hits=[hit],
                     repos_scanned=["acme/billing"]).to_markdown()

    assert "Cross-repo drift detected" in md
    assert "acme/billing/src/subscriber.py:9" in md
    assert "NOT CHECKED" not in md


def test_hits_alone_prove_a_scan_happened():
    """The first version of `was_checked` read only `repos_scanned`, so a
    report carrying real matches but no scanned list rendered as NOT CHECKED —
    suppressing exactly the finding the feature exists to produce. Two existing
    tests caught it; this one keeps it caught."""
    hit = DriftHit(
        value="v", pr_file="a.py", pr_line=1,
        matches=[DriftMatch(other_repo_slug="acme/other", file="b.py",
                            line=2, excerpt="v")],
    )
    report = DriftReport(group_name="g", hits=[hit], repos_scanned=[])

    assert report.was_checked is True
    assert "NOT CHECKED" not in report.to_markdown()
    assert "acme/other/b.py:2" in report.to_markdown()
