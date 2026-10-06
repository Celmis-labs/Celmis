"""An incremental post notes the threads it is about to close, so the webhook
GitHub sends for them is not taken for a person resolving a finding."""

from __future__ import annotations

from src.review.learning import resolve
from src.review.models import PullRequest, ReviewBatch, ScopeInfo
from src.review.providers.base import IncrementalPost, OurThread, finish_incremental_post


class _Provider:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    def resolve_threads(self, pr, threads):
        # The delivery can beat the answer, so the note must already exist.
        ev = resolve.ThreadEvent(provider=pr.provider, repo=pr.repo, pr_number=pr.number,
                                 resolved=True, comment_ids=["501"])
        self.order.append("noted" if resolve.was_closed_by_us(ev) else "late")
        return {"resolved": len(threads), "failed": 0, "unsupported": 0}


def test_the_threads_are_noted_before_the_provider_closes_them():
    resolve._closed_by_us.clear()
    pr = PullRequest(provider="github", repo="acme/shop", number=7, title="t", description="",
                     author="a", base_ref="main", base_sha="b", head_ref="f", head_sha="h",
                     state="open", hunks=[])
    pr.scope = ScopeInfo(base_sha="1" * 40, new_commits=1)
    batch = ReviewBatch(pull_request=pr)
    state = IncrementalPost(
        threads=[], to_resolve=[OurThread(comment_id=501, path="a.py", thread_id="PRRT_1")],
        open_threads=[])
    order: list[str] = []
    finish_incremental_post(_Provider(order), batch, state, posted=0)
    assert order == ["noted"]
    resolve._closed_by_us.clear()
