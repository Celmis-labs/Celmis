"""One Qdrant collection, three tenants: prove they cannot read each other.

The vector store used to have no tenant at all. Isolation was "every caller
passes the right repo list", which is a rule a person has to remember, and
`_probe_related_repos` was the proof it does not work: it queried the WHOLE
collection with no filter and put the repo slugs it found into the answer.

So these tests do not check that a filter was constructed. They index real
points into a real Qdrant, search as one tenant, and assert the other tenant's
content is not in the result. If isolation breaks, the assertion fails on
content, not on a call signature.

`QdrantClient(":memory:")` is the real local implementation of the query and
filter engine — the same code path the embedded deployment runs in production.
The one thing it cannot do is server-side BM25 inference (that needs fastembed),
so the hybrid symbols search is exercised against a fake client that applies
`must` conditions itself; `test_missing_key_never_matches_an_equality_filter`
pins the single semantic that fake depends on against the real engine.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest import mock

import pytest
from qdrant_client import QdrantClient, models

import scripts.backfill_vector_workspaces as bf
from src.config import get_settings
from src.retrieval.tier1_vault import VaultRetriever
from src.retrieval.vector_store import VECTOR_WORKSPACE_KEY, VectorScope


def _stable_point_id(key: str) -> str:
    """The vault's point id, computed the way the product computes it.

    Inlined rather than imported. It used to come from
    `src.indexing.vectors.qdrant_indexer`, a module the running product never
    reached and which has been deleted — but these tests are about the VAULT
    (`VaultRetriever`, `scripts.backfill_vector_workspaces`), which is live,
    and they only ever borrowed the helper to seed a point. Importing it from
    a dead module was what made a live security file look like a dependent of
    one. The live generator is `src/vault/writer.py::note_id`; this is the
    same uuid5, kept local so the fixture cannot drift into testing an import.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


ALPHA = "ws-alpha"
BETA = "ws-beta"
DIM = 8

# One shared direction, so every note is a near-perfect match for every query
# and nothing is filtered out by `retrieval_min_score`. The test is about who
# may see a point, never about ranking.
VEC = [1.0] + [0.0] * (DIM - 1)


# ─── real in-memory Qdrant: the vault collection ────────────────────


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def qdrant(settings):
    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name=settings.qdrant_collection,
        vectors_config=models.VectorParams(size=DIM, distance=models.Distance.COSINE),
    )
    yield client
    client.close()


@pytest.fixture
def fake_embed(monkeypatch):
    """Every text embeds to the same vector — see VEC."""
    import src.llm.completion as completion

    monkeypatch.setattr(completion, "embed", lambda *a, **k: list(VEC), raising=False)
    monkeypatch.setattr(
        completion, "embed_batch",
        lambda texts, *a, **k: [list(VEC) for _ in texts], raising=False,
    )


def _seed_note(retriever: VaultRetriever, *, repo: str, note_path: str, secret: str) -> None:
    """`secret` becomes the note's embedded text, i.e. its `content` payload —
    the thing that would actually be pasted into another tenant's prompt.

    The point id carries the workspace so that two tenants can hold the same
    (repo, note_path) at once. Production ids do not need to: a repo slug is
    bound to exactly one workspace at registration (`existing_slug_binding`).
    Here we want the collision on purpose, to check the FILTER rather than
    accidentally checking that one upsert overwrote the other.
    """
    retriever.upsert_notes_batch([(
        _stable_point_id(f"{retriever.workspace_id}:{repo}:{note_path}"),
        secret,
        {"repo": repo, "note_path": note_path, "type": "module"},
    )])


def _repos(hits) -> set[str]:
    return {h.repo for h in hits}


def _secrets(hits) -> set[str]:
    return {h.content for h in hits}


@pytest.fixture
def two_tenants(settings, qdrant, fake_embed):
    """Two workspaces, one repo each, in ONE collection."""
    alpha = VaultRetriever(settings, qdrant, workspace_id=ALPHA)
    beta = VaultRetriever(settings, qdrant, workspace_id=BETA)
    _seed_note(alpha, repo="alpha-api", note_path="modules/billing.md",
               secret="ALPHA_ONLY_BILLING")
    _seed_note(beta, repo="beta-api", note_path="modules/billing.md",
               secret="BETA_ONLY_BILLING")
    return alpha, beta


def test_search_returns_only_the_callers_workspace(two_tenants):
    """THE test. Two repos, two workspaces, one collection, one question."""
    alpha, beta = two_tenants

    alpha_hits = alpha.search("billing")
    assert _repos(alpha_hits) == {"alpha-api"}
    assert "BETA_ONLY_BILLING" not in " ".join(_secrets(alpha_hits))

    beta_hits = beta.search("billing")
    assert _repos(beta_hits) == {"beta-api"}
    assert "ALPHA_ONLY_BILLING" not in " ".join(_secrets(beta_hits))


def test_naming_the_other_tenants_repo_does_not_reach_it(two_tenants):
    """The repo filter used to BE the isolation, so ask for the other repo by
    name: the tenant filter has to hold on its own."""
    alpha, _ = two_tenants
    assert alpha.search("billing", repo="beta-api") == []


def test_colliding_repo_slug_stays_split(settings, qdrant, fake_embed):
    """Same slug registered in two tenants — the slug can no longer decide."""
    alpha = VaultRetriever(settings, qdrant, workspace_id=ALPHA)
    beta = VaultRetriever(settings, qdrant, workspace_id=BETA)
    _seed_note(alpha, repo="shared-name", note_path="a.md", secret="ALPHA_SIDE")
    _seed_note(beta, repo="shared-name", note_path="b.md", secret="BETA_SIDE")

    assert _secrets(alpha.search("x", repo="shared-name")) == {"ALPHA_SIDE"}
    assert _secrets(beta.search("x", repo="shared-name")) == {"BETA_SIDE"}


def test_untenanted_caller_sees_nothing_rather_than_everything(settings, qdrant,
                                                               two_tenants):
    """'default' is what arrives when nobody said which tenant this is. It must
    not resolve to "no filter"."""
    for placeholder in ("default", "", None):
        r = VaultRetriever(settings, qdrant, workspace_id=placeholder)
        assert r.scope.matches_nothing
        assert r.search("billing") == []


# ─── the points that were already there ─────────────────────────────


def _seed_legacy(qdrant, settings, *, repo: str, secret: str) -> str:
    """A point exactly as it was written before workspace_id existed."""
    point_id = _stable_point_id(f"legacy:{repo}")
    qdrant.upsert(
        collection_name=settings.qdrant_collection,
        points=[models.PointStruct(
            id=point_id, vector=list(VEC),
            payload={"repo": repo, "note_path": "modules/legacy.md",
                     "type": "module", "content": secret},
        )],
    )
    return point_id


def test_preexisting_point_is_invisible_to_every_tenant(settings, qdrant,
                                                        two_tenants):
    """It must not silently become visible to everyone."""
    alpha, beta = two_tenants
    _seed_legacy(qdrant, settings, repo="alpha-api", secret="LEGACY_SECRET")

    assert "LEGACY_SECRET" not in " ".join(_secrets(alpha.search("legacy")))
    assert "LEGACY_SECRET" not in " ".join(_secrets(beta.search("legacy")))


def test_preexisting_point_still_exists_and_a_global_scope_sees_it(settings, qdrant,
                                                                   two_tenants):
    """...and it must not silently vanish either."""
    _seed_legacy(qdrant, settings, repo="alpha-api", secret="LEGACY_SECRET")

    found = qdrant.query_points(
        collection_name=settings.qdrant_collection,
        query=list(VEC),
        query_filter=models.Filter(must=VectorScope.global_admin().must_conditions()),
        limit=50, with_payload=True,
    ).points
    assert "LEGACY_SECRET" in {(p.payload or {}).get("content") for p in found}


def test_backfilled_point_becomes_visible_to_its_owner_only(settings, qdrant,
                                                            two_tenants):
    """What the backfill does, done by hand: set_payload on the tenant key.

    This is the whole contract of scripts/backfill_vector_workspaces.py —
    afterwards the owner can find its own note and the other tenant still
    cannot.
    """
    alpha, beta = two_tenants
    point_id = _seed_legacy(qdrant, settings, repo="alpha-api", secret="LEGACY_SECRET")

    qdrant.set_payload(
        collection_name=settings.qdrant_collection,
        payload={VECTOR_WORKSPACE_KEY: ALPHA},
        points=[point_id], wait=True,
    )

    assert "LEGACY_SECRET" in " ".join(_secrets(alpha.search("legacy")))
    assert "LEGACY_SECRET" not in " ".join(_secrets(beta.search("legacy")))


def test_missing_key_never_matches_an_equality_filter(settings, qdrant):
    """The load-bearing Qdrant semantic, pinned against the real engine.

    Everything above rests on it: a point with no `workspace_id` cannot match
    `workspace_id == anything`. If a future client version made a missing key
    match, unattributed points would become visible to every tenant at once and
    every other test here would still pass.
    """
    qdrant.upsert(
        collection_name=settings.qdrant_collection,
        points=[models.PointStruct(id=_stable_point_id("bare"), vector=list(VEC),
                                   payload={"repo": "r"})],
    )
    got = qdrant.query_points(
        collection_name=settings.qdrant_collection,
        query=list(VEC),
        query_filter=models.Filter(must=VectorScope.for_workspace(ALPHA).must_conditions()),
        limit=50,
    ).points
    assert got == []


def test_delete_reaches_own_and_unattributed_but_not_the_other_tenant(
    settings, qdrant, fake_embed,
):
    """`delete_by_note_path` runs before a note is rewritten. It has to clear
    the tenant's own point AND the pre-backfill copy of the same note (else the
    collection keeps a stale, unreachable, undeletable duplicate) — and nothing
    of anyone else's."""
    alpha = VaultRetriever(settings, qdrant, workspace_id=ALPHA)
    beta = VaultRetriever(settings, qdrant, workspace_id=BETA)
    _seed_note(alpha, repo="shared-name", note_path="modules/legacy.md",
               secret="ALPHA_SIDE")
    _seed_note(beta, repo="shared-name", note_path="modules/legacy.md",
               secret="BETA_SIDE")
    _seed_legacy(qdrant, settings, repo="shared-name", secret="LEGACY_SECRET")

    alpha.delete_by_note_path("modules/legacy.md", repo="shared-name")

    assert alpha.search("x", repo="shared-name") == []
    assert _secrets(beta.search("x", repo="shared-name")) == {"BETA_SIDE"}

    survivors = {
        (p.payload or {}).get("content")
        for p in qdrant.query_points(
            collection_name=settings.qdrant_collection, query=list(VEC),
            limit=50, with_payload=True,
        ).points
    }
    assert "LEGACY_SECRET" not in survivors   # stale copy of the same note
    assert "BETA_SIDE" in survivors           # somebody else's, untouched


# ─── the backfill, run for real ─────────────────────────────────────


@pytest.fixture
def orphans(settings, qdrant):
    """A collection as production has it: points with no tenant, some of which
    can honestly be given one and some of which cannot."""
    rows = [
        (1, {"repo": "alpha-api", "content": "ALPHA"}),
        (2, {"repo": "beta-api", "content": "BETA"}),
        (3, {"repo": "ghost-repo", "content": "NOBODY_REGISTERED_ME"}),
        (4, {"repo": "double-repo", "content": "TWO_OWNERS"}),
        (5, {"repo": "legacy-repo", "content": "REGISTERED_TO_DEFAULT"}),
        (6, {"file": "x.py", "content": "NO_REPO_AT_ALL"}),
        (7, {"repo": "alpha-api", "content": "ALREADY_MINE",
             VECTOR_WORKSPACE_KEY: ALPHA}),
    ]
    qdrant.upsert(
        collection_name=settings.qdrant_collection,
        points=[models.PointStruct(id=i, vector=list(VEC), payload=pl)
                for i, pl in rows],
    )
    return {
        "mapping": {"alpha-api": ALPHA, "beta-api": BETA},
        "unmappable": {"double-repo": bf.AMBIGUOUS, "legacy-repo": bf.PLACEHOLDER},
    }


def _owners(qdrant, settings) -> dict[int, str | None]:
    points, _ = qdrant.scroll(collection_name=settings.qdrant_collection, limit=100)
    return {p.id: (p.payload or {}).get(VECTOR_WORKSPACE_KEY) for p in points}


def _run_backfill(qdrant, settings, orphans, *, apply: bool,
                  only_workspace: str | None = None):
    return bf.backfill_collection(
        qdrant, settings.qdrant_collection,
        only_workspace=only_workspace, apply=apply, **orphans,
    )


def test_dry_run_writes_nothing(qdrant, settings, orphans):
    before = _owners(qdrant, settings)
    planned = _run_backfill(qdrant, settings, orphans, apply=False)

    assert planned == {ALPHA: 1, BETA: 1}, "it must still SAY what it would do"
    assert _owners(qdrant, settings) == before


def test_backfill_attributes_only_what_it_can_justify(qdrant, settings, orphans):
    _run_backfill(qdrant, settings, orphans, apply=True)

    assert _owners(qdrant, settings) == {
        1: ALPHA,      # registered to alpha
        2: BETA,       # registered to beta
        3: None,       # unregistered — no honest owner
        4: None,       # registered in two workspaces — fails closed
        5: None,       # registered to the 'default' placeholder — not a tenant
        6: None,       # no repo in the payload to map by
        7: ALPHA,      # already owned, untouched
    }


def test_backfill_is_idempotent(qdrant, settings, orphans):
    _run_backfill(qdrant, settings, orphans, apply=True)
    after_first = _owners(qdrant, settings)

    assert _run_backfill(qdrant, settings, orphans, apply=True) == {}
    assert _owners(qdrant, settings) == after_first


def test_backfill_never_deletes(qdrant, settings, orphans):
    """The points it cannot attribute must still be there afterwards."""
    _run_backfill(qdrant, settings, orphans, apply=True)

    points, _ = qdrant.scroll(collection_name=settings.qdrant_collection, limit=100)
    assert {(p.payload or {}).get("content") for p in points} >= {
        "NOBODY_REGISTERED_ME", "TWO_OWNERS", "REGISTERED_TO_DEFAULT",
        "NO_REPO_AT_ALL",
    }


def test_backfill_can_be_done_one_workspace_at_a_time(qdrant, settings, orphans):
    """Three tenants in production; a person wants to check one before the next."""
    _run_backfill(qdrant, settings, orphans, apply=True, only_workspace=BETA)

    owners = _owners(qdrant, settings)
    assert owners[2] == BETA
    assert owners[1] is None


def test_backfilled_points_are_reachable_by_their_owner(qdrant, settings, orphans,
                                                        fake_embed):
    """The point of the exercise: afterwards the right tenant can find its own
    notes again, and the other one still cannot."""
    _run_backfill(qdrant, settings, orphans, apply=True)

    assert "ALPHA" in _secrets(VaultRetriever(settings, qdrant, workspace_id=ALPHA)
                               .search("x"))
    assert "ALPHA" not in _secrets(VaultRetriever(settings, qdrant, workspace_id=BETA)
                                   .search("x"))


def test_a_repo_in_two_workspaces_is_never_guessed():
    """`repo_to_workspace` must fail closed on ambiguity rather than pick one."""
    cfgs = [
        SimpleNamespace(repo_slug="double-repo", workspace_id=ALPHA),
        SimpleNamespace(repo_slug="double-repo", workspace_id=BETA),
        SimpleNamespace(repo_slug="legacy-repo", workspace_id="default"),
        SimpleNamespace(repo_slug="alpha-api", workspace_id=ALPHA),
    ]
    with mock.patch("src.api.auto_review.get_auto_review_store",
                    return_value=SimpleNamespace(list_all=lambda: cfgs)):
        mapping, unmappable = bf.repo_to_workspace()

    assert mapping == {"alpha-api": ALPHA}
    assert unmappable == {"double-repo": bf.AMBIGUOUS,
                          "legacy-repo": bf.PLACEHOLDER}


# ─── the signatures that make forgetting impossible ─────────────────


def test_vault_retriever_cannot_be_built_without_a_workspace(settings, qdrant):
    with pytest.raises(TypeError):
        VaultRetriever(settings, qdrant)
