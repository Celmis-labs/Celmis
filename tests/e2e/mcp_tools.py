"""In-container E2E for MCP research-access enforcement.

Simulates FastMCP's auth context by monkeypatching get_access_token to return
a token carrying a given user's MCP JWT, then calls the tool impls directly.
"""
import mcp.server.auth.middleware.auth_context as authctx
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from src.access.resolver import glob_match
from src.db.models import Project, ProjectRepo
from src.db.session import get_database_url
from src.mcp_server import http_app as H
from src.mcp_server.auth import JwtConfig, issue_token
from src.mcp_server.identity import caller_access, resolve_caller

CONTAINER_USER = "c0d30d4b-a727-4d54-849f-27e540115d84"
ADMIN_USER = "default"
ETL = "gitlab_acme-qa-test-etl"      # container team has rule (deny creds)
SHARED = "gitlab_acme-qa-test-shared"  # rule for other team → denied

URL = get_database_url().replace("postgresql+asyncpg://", "postgresql+psycopg://")
eng = create_engine(URL)

# ── ensure a project with BOTH repos exists ──
PROJ_ID = "e2e0e2e0-0000-4000-8000-000000000001"
with Session(eng) as s:
    s.execute(delete(ProjectRepo).where(ProjectRepo.project_id == PROJ_ID))
    s.execute(delete(Project).where(Project.id == PROJ_ID))
    s.commit()
    s.add(Project(id=PROJ_ID, workspace_id="default", name="E2E MCP Proj", owner_user_id=ADMIN_USER))
    s.add(ProjectRepo(project_id=PROJ_ID, repo_slug=ETL, role="primary"))
    s.add(ProjectRepo(project_id=PROJ_ID, repo_slug=SHARED, role="dep"))
    s.commit()

cfg = JwtConfig.from_env()

def mint(user_id):
    return issue_token(cfg, subject=user_id, scopes=["read:graph"])


class FakeToken:
    def __init__(self, raw):
        self.token = raw
        self.client_id = "code-analyzer-cli"
        self.scopes = ["read:graph"]

def set_caller(user_id):
    raw = mint(user_id) if user_id else None
    authctx.get_access_token = (lambda: FakeToken(raw)) if raw else (lambda: None)

fails=[]
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ")+name+(f"  {extra}" if extra else ""))
    if not cond:
        fails.append(name)

# ── container user identity resolves correctly ──
set_caller(CONTAINER_USER)
c = resolve_caller()
check("caller resolves container user", c.user_id == CONTAINER_USER and c.authenticated and not c.is_admin, f"uid={c.user_id} admin={c.is_admin}")

# ── list_accessible_repos hides SHARED ──
res = H._list_accessible_repos_impl()
slugs = [r["repo_slug"] for r in res["repos"]]
check("list_accessible hides SHARED", SHARED not in slugs and ETL in slugs, f"got={slugs}")

# ── search_symbols in project → SHARED blocked + notice, ETL creds filtered ──
res2 = H._search_symbols_impl(PROJ_ID, "e", None, 50)
blocked = res2.get("blocked_repos", [])
notice = res2.get("access_notice", "")
repos_in_matches = {m["repo_slug"] for m in res2["matches"]}
check("search: SHARED in blocked_repos", SHARED in blocked, f"blocked={blocked}")
check("search: notice mentions SHARED", SHARED in notice, f"notice={notice[:80]}")
check("search: no matches from SHARED", SHARED not in repos_in_matches, f"match_repos={repos_in_matches}")
# any matched files must not be under denied globs
bad = [m for m in res2["matches"] if m["repo_slug"]==ETL and m.get("file") and (glob_match(m["file"],"**/credentials/**") or glob_match(m["file"],"**/crypto/**") or glob_match(m["file"],"**/*secret*"))]
check("search: no denied files leaked from ETL", not bad, f"leaked={[m['file'] for m in bad]}")

# ── get_my_access via caller_access ──
_caller, acc = caller_access([ETL, SHARED])
check("my_access ETL researchable", acc[ETL].researchable and not acc[SHARED].researchable)

# ── admin bypass: sees SHARED ──
set_caller(ADMIN_USER)
resA = H._list_accessible_repos_impl()
slugsA=[r["repo_slug"] for r in resA["repos"]]
check("admin sees SHARED", SHARED in slugsA and ETL in slugsA, f"got={slugsA}")
resA2 = H._search_symbols_impl(PROJ_ID, "e", None, 50)
check("admin search: no blocked_repos", not resA2.get("blocked_repos"), f"blocked={resA2.get('blocked_repos')}")

# ── unauthenticated (dev/stdio) falls open ──
set_caller(None)
cu = resolve_caller()
check("unauth falls open (admin-like)", (not cu.authenticated) and cu.is_admin)

print("\nRESULT:", "ALL_PASS" if not fails else f"FAILURES={fails}")
