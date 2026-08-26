"""Hermetic in-container E2E for Q&A retrieval access enforcement.

Access resolution + filtering are REAL (DB-backed); only the Gemini embed and
Qdrant query are mocked so the test doesn't depend on external services.
"""
import asyncio
from types import SimpleNamespace

from src.access.resolver import glob_match
from src.qa.multi_repo_retriever import MultiRepoRetriever

CONTAINER_USER = "c0d30d4b-a727-4d54-849f-27e540115d84"
ETL = "gitlab_acme-etl"
SHARED = "gitlab_acme-shared"
Q = "how is data loaded and transformed"
DENY = ["**/credentials/**", "**/crypto/**", "**/*secret*"]

fails=[]
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ")+name+(f"  {extra}" if extra else ""))
    if not cond:
        fails.append(name)

def pt(repo, path, score, typ="module"):
    return SimpleNamespace(score=score, payload={
        "note_path": f"modules/{path.split('/')[-1]}.md", "type": typ,
        "module": path.split('/')[-1], "repo": repo, "symbols": [],
        "keywords": [], "content": "x", "path": path, "cross_refs": [],
    })

def install_mocks(r):
    r.vault_ret.gemini.embed = lambda **k: SimpleNamespace(embedding=[0.01]*768)
    def qp(**kw):
        # Both queries now carry a filter — the probe is workspace-scoped too,
        # so `query_filter is None` no longer tells them apart. It asks for one
        # payload key; the retrieval query asks for all of them.
        if kw.get("with_payload") == ["repo"]:
            # probe within the tenant → SHARED shows up (relevant, inaccessible)
            return SimpleNamespace(points=[
                SimpleNamespace(score=0.80, payload={"repo": ETL}),
                SimpleNamespace(score=0.71, payload={"repo": SHARED}),
            ])
        # accessible-repos query → ETL hits incl a denied-path note
        return SimpleNamespace(points=[
            pt(ETL, "src/loaders", 0.82),
            pt(ETL, "src/credentials", 0.77),   # must be dropped
        ])
    r.vault_ret.qdrant.query_points = qp

# A real tenant, not the "default" placeholder: "default" means "nobody said
# which workspace this is", and a vector search scoped to it returns nothing.
WS = "ws-e2e"


async def main():
    r = MultiRepoRetriever()
    install_mocks(r)

    # 1) container user, both repos, code ON
    ctx = await r.retrieve(question=Q, repos=[ETL, SHARED],
        user_id=CONTAINER_USER, is_admin=False, workspace_id=WS, include_code=True)
    check("blocked_repos has SHARED", SHARED in ctx.blocked_repos, f"blocked={ctx.blocked_repos}")
    check("access_notice mentions SHARED", SHARED in ctx.access_notice)
    check("credentials vault-hit dropped", all("credentials" not in (h.path or "") for h in ctx.vault_hits),
          f"paths={[h.path for h in ctx.vault_hits]}")
    check("no SHARED files_read", all(not f.startswith(SHARED+'/') for f in ctx.files_read))
    leaked=[f for f in ctx.files_read if '/' in f and any(glob_match(f.split('/',1)[1], g) for g in DENY)]
    check("no denied files leaked", not leaked, f"leaked={leaked}")

    # 2) code OFF
    ctx2 = await r.retrieve(question=Q, repos=[ETL],
        user_id=CONTAINER_USER, is_admin=False, workspace_id=WS, include_code=False)
    check("code OFF → no files_read + flag", len(ctx2.files_read)==0 and ctx2.code_included is False)
    check("code OFF → prompt note", "вимкнено користувачем" in ctx2.prompt)

    # 3) admin, both repos → no boundary; probe skipped for admins
    r3 = MultiRepoRetriever()
    install_mocks(r3)
    ctx3 = await r3.retrieve(question=Q, repos=[ETL, SHARED],
        user_id="default", is_admin=True, workspace_id=WS, include_code=True)
    check("admin no blocked_repos", not ctx3.blocked_repos, f"blocked={ctx3.blocked_repos}")
    check("admin no notice", ctx3.access_notice=="")
    check("admin keeps credentials hit (bypass)", any("credentials" in (h.path or "") for h in ctx3.vault_hits))

    # 4) only-denied repo → short-circuit, no embed/query needed
    ctx4 = await r.retrieve(question=Q, repos=[SHARED],
        user_id=CONTAINER_USER, is_admin=False, workspace_id=WS, include_code=True)
    check("only-denied → no vault hits", len(ctx4.vault_hits)==0)
    check("only-denied → notice present", SHARED in ctx4.access_notice)

    print("\nRESULT:", "ALL_PASS" if not fails else f"FAILURES={fails}")

asyncio.run(main())
