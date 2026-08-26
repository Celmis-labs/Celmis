"""E2E for the review-fix MCP gates: route_incident, bootstrap_client,
migrate_consumers now enforce research access."""
import mcp.server.auth.middleware.auth_context as authctx

from src.mcp_server import http_app as H
from src.mcp_server.auth import JwtConfig, issue_token
from src.mcp_server.identity import caller_access

CONTAINER_USER = "c0d30d4b-a727-4d54-849f-27e540115d84"
ETL = "gitlab_acme-qa-test-etl"
SHARED = "gitlab_acme-qa-test-shared"
PROJ = "e2e0e2e0-0000-4000-8000-000000000001"
cfg = JwtConfig.from_env()

class FakeToken:
    def __init__(self, raw):
        self.token = raw
        self.client_id = "code-analyzer-cli"
        self.scopes = ["read:graph"]
def set_caller(uid):
    authctx.get_access_token = (lambda: FakeToken(issue_token(cfg, subject=uid, scopes=["read:graph"])))

fails=[]
def check(n,c,extra=""):
    print(("PASS " if c else "FAIL ")+n+(f"  {extra}" if extra else ""))
    if not c:
        fails.append(n)

set_caller(CONTAINER_USER)

# route_incident/get_owner share the same gate on lookup_owner. Verify the
# gate predicate the tool uses (caller_access → researchable/path_visible).
_c, acc = caller_access([SHARED, ETL])
check("route_incident gate: SHARED not researchable", not acc[SHARED].researchable)
check("route_incident gate: ETL creds path hidden", not acc[ETL].path_visible("src/credentials/x.py"))

# bootstrap_client impl → target ETL, sibling SHARED must be blocked + no leak
res = H._bootstrap_client_impl(project_id=PROJ, target_repo_slug=ETL, target_endpoint=None, language="typescript")
ux_repos = {u["consumer_repo"] for u in res.get("usage_examples", [])}
check("bootstrap: no SHARED usage_examples", SHARED not in ux_repos, f"repos={ux_repos}")
check("bootstrap: SHARED in blocked_repos", SHARED in res.get("blocked_repos", []), f"blocked={res.get('blocked_repos')}")

# migrate_consumers with a nonsense symbol (no real callers) → SHARED skipped for access
res2 = H._migrate_consumers_impl(project_id=PROJ, symbol="__nonexistent_symbol_zzz__",
    old_text="a", new_text="b", user_id=CONTAINER_USER, commit_message=None)
skips = {(x["repo_slug"], x.get("reason","")) for x in res2.get("results", [])}
shared_access_skip = any(rs==SHARED and "research access" in reason for rs,reason in skips)
check("migrate: SHARED skipped for access", shared_access_skip, f"skips={skips}")

# admin bypass on bootstrap
set_caller("default")
# ensure admin flag: default is admin
resA = H._bootstrap_client_impl(project_id=PROJ, target_repo_slug=ETL, target_endpoint=None, language="typescript")
check("admin bootstrap: no blocked_repos", not resA.get("blocked_repos"), f"blocked={resA.get('blocked_repos')}")

print("\nRESULT:", "ALL_PASS" if not fails else f"FAILURES={fails}")
