"""A long note is indexed in chunks so a passage deep inside it can be found.
The answer prompt then showed 300 characters of the hit, which is the chunk's
header and little else: the passage that matched never reached the model."""

from __future__ import annotations

from src.qa.multi_repo_retriever import (
    VAULT_CONTEXT_CHARS,
    VAULT_EXCERPT_CHARS,
    MultiRepoRetriever,
    MultiRepoVaultHit,
)


def _hit(path: str, content: str) -> MultiRepoVaultHit:
    return MultiRepoVaultHit(note_path=path, score=0.8, type="module", module=None, repo="acme",
                             symbols=[], keywords=[], content=content, path=None)


def test_the_matching_passage_of_a_chunk_is_in_the_prompt() -> None:
    chunk = "# Module: billing\n\n" + "filler " * 80 + "The refund window is 14 days."
    assert len(chunk) > 300
    text = MultiRepoRetriever._format_vault_hits([_hit("modules/billing.md", chunk)])
    assert "The refund window is 14 days." in text


def test_one_hit_and_all_of_them_stay_within_their_budget() -> None:
    hits = [_hit(f"modules/m{i}.md", "x" * 10_000) for i in range(10)]
    text = MultiRepoRetriever._format_vault_hits(hits)
    shown = [line.split("): ", 1)[1].rstrip("…") for line in text.splitlines()]
    assert len(shown) == 10  # every hit is still named
    assert max(map(len, shown)) == VAULT_EXCERPT_CHARS
    assert sum(map(len, shown)) == VAULT_CONTEXT_CHARS
