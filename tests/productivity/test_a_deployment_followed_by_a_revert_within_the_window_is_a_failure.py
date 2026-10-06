"""Change failure: a revert or hotfix that reaches production within the window marks a deployment failed."""

from __future__ import annotations

from tests.productivity.support import (
    FakeProvider,
    at,
    aware,
    deployments_in,
    enable,
    make_engine,
    pr,
    sync,
)


def _run(prs, **settings):
    engine = make_engine()
    enable(engine, production_branches=["master"], **settings)
    sync(engine, FakeProvider(prs))
    return deployments_in(engine)


def test_a_revert_the_next_day_fails_the_deployment_it_undid() -> None:
    deps = _run([
        pr(20, target="master", title="PROJ-1: Export", merged=at(-10)),
        pr(21, target="master", title="PROJ-2: Totals", merged=at(-6)),
        pr(22, target="master", title='Revert "PROJ-2: Totals"', source="revert-ab12", merged=at(-5)),
    ])
    assert [d.is_failure for d in deps] == [False, True, False]
    failed = deps[1]
    assert failed.failed_by_pr_number == 22
    assert aware(failed.recovered_at) == at(-5)


def test_a_revert_after_the_window_is_a_new_change_not_a_failure() -> None:
    deps = _run([
        pr(20, target="master", title="PROJ-1: Export", merged=at(-30)),
        pr(22, target="master", title='Revert "PROJ-1: Export"', merged=at(-5)),
    ])
    assert not any(d.is_failure for d in deps)


def test_the_window_is_a_setting() -> None:
    prs = [
        pr(20, target="master", title="PROJ-1: Export", merged=at(-30)),
        pr(22, target="master", title='Revert "PROJ-1: Export"', merged=at(-20)),
    ]
    assert not any(d.is_failure for d in _run(prs))
    assert [d.is_failure for d in _run(prs, failure_window_days=15)] == [True, False]


def test_a_hotfix_fails_the_latest_deployment_before_it() -> None:
    deps = _run([
        pr(30, target="master", title="Feature A", merged=at(-4)),
        pr(31, target="master", title="Feature B", merged=at(-3)),
        pr(32, target="master", title="Totals broke", source="hotfix/totals", merged=at(-2)),
    ])
    assert [d.is_failure for d in deps] == [False, True, False]
    assert deps[1].failed_by_pr_number == 32


def test_a_revert_that_names_a_pr_fails_that_prs_deployment_not_the_latest() -> None:
    deps = _run([
        pr(40, target="master", title="Feature A", merged=at(-5)),
        pr(41, target="master", title="Feature B", merged=at(-4)),
        pr(42, target="master", title="Revert Feature A", description="This reverts pull request #40", merged=at(-3)),
    ])
    assert [d.is_failure for d in deps] == [True, False, False]


def test_a_revert_merged_in_the_same_burst_as_what_it_undoes_fails_nothing() -> None:
    deps = _run([
        pr(50, target="master", title="Feature A", merged=at(-5)),
        pr(51, target="master", title='Revert "Feature A"', merged=at(-5, minutes=10)),
    ])
    assert len(deps) == 1 and not deps[0].is_failure


def test_the_first_recovery_is_the_one_that_counts() -> None:
    deps = _run([
        pr(60, target="master", title="Feature A", merged=at(-6)),
        pr(61, target="master", title="Totals", source="hotfix/a", merged=at(-5)),
        pr(62, target="master", title="Totals again", source="hotfix/b", merged=at(-4)),
    ])
    assert deps[0].is_failure and deps[0].failed_by_pr_number == 61
    assert aware(deps[0].recovered_at) == at(-5)
