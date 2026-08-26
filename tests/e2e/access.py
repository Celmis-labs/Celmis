"""In-container E2E for Stage 22 fine-grained research access.

Run inside celmis-api:  python tests/e2e/access.py
"""
import uuid

from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from src.access import resolve_access
from src.access.resolver import glob_match
from src.db.models import RepoAccessRule, Team, TeamMember
from src.db.session import get_database_url
from src.mcp_server import tools as legacy

URL = get_database_url().replace("postgresql+asyncpg://", "postgresql+psycopg://")
eng = create_engine(URL)

CONTAINER_USER = "c0d30d4b-a727-4d54-849f-27e540115d84"  # container@example.com (non-admin)
ADMIN_USER = "default"
TEAM_ID = "e2e-qa-team"
# pick a real indexed repo
repos = [r.slug for r in legacy.list_repos()]
print("indexed repos:", repos)
assert repos, "no indexed repos — cannot test"
REPO = repos[0]
OTHER = repos[1] if len(repos) > 1 else None

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)

# ── glob sanity ──
check("glob creds", glob_match("src/credentials/aws.py", "**/credentials/**"))
check("glob not-auth", not glob_match("src/auth/login.py", "**/credentials/**"))

with Session(eng) as s:
    # clean slate
    s.execute(delete(RepoAccessRule).where(RepoAccessRule.team_id == TEAM_ID))
    s.execute(delete(TeamMember).where(TeamMember.team_id == TEAM_ID))
    s.execute(delete(Team).where(Team.id == TEAM_ID))
    s.commit()
    # team + membership (container user)
    s.add(Team(id=TEAM_ID, workspace_id="default", name="E2E QA Team", description=""))
    s.add(TeamMember(team_id=TEAM_ID, user_id=CONTAINER_USER, role="member"))
    # rule: metadata + deny creds on REPO for this team
    s.add(RepoAccessRule(
        id=str(uuid.uuid4()), workspace_id="default", team_id=TEAM_ID,
        repo_slug=REPO, visibility="code",
        allow_globs=[], deny_globs=["**/credentials/**", "**/crypto/**", "**/*secret*"],
        sensitivity_tags=["creds", "crypto"], note="e2e",
    ))
    s.commit()

# ── resolver: container user has code access to REPO but denies apply ──
acc = resolve_access(user_id=CONTAINER_USER, is_admin=False, workspace_id="default", repos=[REPO])
d = acc[REPO]
check("container researchable REPO", d.researchable)
check("container code_visible REPO", d.code_visible)
check("container sees normal path", d.path_visible("src/api/handler.py"))
check("container DENIED creds path", not d.path_visible("src/credentials/aws.py"))
check("container DENIED secret file", not d.path_visible("src/util/mysecret.ts"))

# ── OTHER repo now has a rule for a DIFFERENT team → container user gets none ──
if OTHER:
    with Session(eng) as s:
        s.execute(delete(RepoAccessRule).where(RepoAccessRule.repo_slug == OTHER, RepoAccessRule.team_id == "e2e-other"))
        s.add(Team(id="e2e-other", workspace_id="default", name="E2E Other", description="")) if not s.get(Team, "e2e-other") else None
        s.add(RepoAccessRule(
            id=str(uuid.uuid4()), workspace_id="default", team_id="e2e-other",
            repo_slug=OTHER, visibility="code", allow_globs=[], deny_globs=[],
            sensitivity_tags=[], note="e2e-other-only",
        ))
        s.commit()
    acc2 = resolve_access(user_id=CONTAINER_USER, is_admin=False, workspace_id="default", repos=[OTHER])
    check("container DENIED OTHER (rule for other team)", not acc2[OTHER].researchable)

# ── admin bypass ──
accA = resolve_access(user_id=ADMIN_USER, is_admin=True, workspace_id="default", repos=[REPO])
check("admin sees creds (bypass)", accA[REPO].path_visible("src/credentials/aws.py"))

# ── fall-open: a repo with NO rule ──
FAKE = "nonexistent/repo-no-rule"
accF = resolve_access(user_id=CONTAINER_USER, is_admin=False, workspace_id="default", repos=[FAKE])
check("fall-open for unruled repo", accF[FAKE].researchable and accF[FAKE].path_visible("anything.py"))

print("\nREPO used:", REPO, "OTHER:", OTHER)
print("RESULT:", "ALL_PASS" if not fails else f"FAILURES={fails}")
