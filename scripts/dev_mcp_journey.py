#!/usr/bin/env python3
"""The whole developer-MCP journey over real HTTP, on an in-process Celmis.

What it does, in order (every step through the product's own HTTP surface, not
through its internals, except the clock moves a token's expiry into the past):

1. start Celmis (``tests/e2e_local/stack.py``: the real FastAPI app on a real
   socket, real indexer, real MCP mounts) with users in every role: superadmin,
   owner, admin, editor, member, viewer, plus two developers;
2. admins create teams and access rules over REST; one repository is left
   without any rule on purpose;
3. the superadmin issues MCP tokens over REST: developer A (two repositories),
   developer B (one other repository), a token that is then expired and one
   that is then revoked;
4. a real MCP client (the SDK, Streamable HTTP, a new session per call) runs
   the scenarios against ``/mcp/dev/`` and records, for every call, characters,
   estimated tokens (chars / 3.7) and latency; a grep-and-read baseline is run
   on the same questions;
5. a security matrix over HTTP: another developer's repository is absent from
   every tool, expired and revoked tokens are refused, an unruled repository is
   invisible, write is refused, no planted secret appears in any output, the
   audit has one row per call and holds no content.

Run::

    scripts/dev_mcp_journey.py --out ~/.cache/celmis-journey/run1
    scripts/dev_mcp_journey.py --real DIR[,DIR...] --src-root ~/code --out ~/private/out

``--real`` indexes clones (``git clone file://``: tracked files only) of real
service directories and runs the three developer requests on them. Its output
must stay OUTSIDE this repository (the script refuses otherwise); a real secret
value is never printed, only the file path and variable name of a leak.

Exit status: 0 when the matrix is green and nothing leaked, 1 otherwise.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from scripts import dev_mcp_e2e as e2e  # noqa: E402
from tests.e2e_local.client import McpClient, McpHttpError, ToolResult  # noqa: E402

DEV = "/mcp/dev/"
FULL = "/mcp/"
NOBODY = "github_nobody-nothing"


# ───────────────────────────── recording ───────────────────────────────────


@dataclass
class Call:
    who: str
    path: str
    tool: str
    args: dict[str, Any]
    chars: int
    ms: float
    status: str            # ok | tool_error | http_<code>
    text: str = field(repr=False, default="")
    raw: str = field(repr=False, default="")

    @property
    def tokens(self) -> int:
        return e2e.est_tokens(self.chars)


class Recorder(McpClient):
    """An MCP client that remembers every call it makes (text kept in memory)."""

    def __init__(self, base_url: str, path: str, token: str | None, who: str,
                 sink: list[Call]) -> None:
        super().__init__(base_url, path, token)
        self.who, self.path, self.sink = who, path, sink

    def call(self, tool: str, args: dict[str, Any] | None = None) -> ToolResult:
        t0 = time.perf_counter()
        try:
            res = super().call(tool, args)
        except McpHttpError as exc:
            self.sink.append(Call(self.who, self.path, tool, args or {}, len(exc.body),
                                  (time.perf_counter() - t0) * 1000, f"http_{exc.status}",
                                  exc.body, exc.body))
            raise
        self.sink.append(Call(self.who, self.path, tool, args or {}, len(res.text),
                              (time.perf_counter() - t0) * 1000,
                              "tool_error" if res.is_error else "ok", res.text, res.raw))
        return res


class Api:
    """The REST API as one user (a real session JWT, the workspace header)."""

    def __init__(self, base: str, user, workspace: str = "ws-a") -> None:
        from src.api.jwt_auth import issue_token

        token, _ = issue_token(user_id=user.id, email=user.email)
        self.http = httpx.Client(base_url=base, timeout=60, headers={
            "Authorization": f"Bearer {token}", "X-Workspace": workspace})
        self.texts: list[str] = []

    def req(self, method: str, url: str, **kw: Any) -> httpx.Response:
        r = self.http.request(method, url, **kw)
        self.texts.append(r.text)
        return r


# ───────────────────────────── matrix ──────────────────────────────────────


@dataclass
class Check:
    id: str
    claim: str
    ok: bool
    detail: str = ""


class Matrix:
    def __init__(self) -> None:
        self.rows: list[Check] = []

    def add(self, cid: str, claim: str, ok: bool, detail: str = "") -> bool:
        self.rows.append(Check(cid, claim, bool(ok), detail if not ok else detail[:160]))
        mark = "ok  " if ok else "FAIL"
        print(f"  [{mark}] {cid} {claim}" + ("" if ok else f"  <- {detail[:200]}"), flush=True)
        return bool(ok)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.rows)


def failing(rep, *, ignore: tuple[str, ...] = ()) -> list[str]:
    """Ids of the scenarios that ran and failed (``ignore``: ids that name repos by design)."""
    return [r.id for r in rep.ran if not (r.celmis and r.celmis.passed)
            and r.id.split("-")[0] not in ignore] + list(rep.leaks) + list(rep.extra_leaks)


def visible_slugs(text: str) -> set[str]:
    return {e["slug"] for e in e2e.parse_idx_entries(text)}


def plain(text: str, typed: str) -> str:
    """An answer with the caller's spelling and the ranked ``similar:`` hint removed."""
    return "\n".join(ln for ln in text.replace(typed, "<R>").splitlines()
                     if not ln.startswith("similar:"))


def refused(client: Recorder, tool: str = "repos") -> int | None:
    """HTTP status when the endpoint refuses the credential, None when it answers."""
    try:
        client.call(tool, {} if tool == "repos" else None)
        return None
    except McpHttpError as exc:
        return exc.status


# ───────────────────────────── the synthetic world ─────────────────────────


def setup_access(st, calls: list[Call]) -> dict[str, Any]:
    """Teams and rules as the workspace admin, over REST. Returns ids and the 4th repo."""
    admin = Api(st.url, st.users["admin"])
    out: dict[str, Any] = {"api": {"admin": admin}}
    backend = admin.req("POST", "/api/teams", json={"name": "backend"}).json()["id"]
    edge = admin.req("POST", "/api/teams", json={"name": "edge"}).json()["id"]
    for team, who in ((backend, "dev"), (backend, "member"), (edge, "editor")):
        r = admin.req("PUT", f"/api/teams/{team}/members/{st.users[who].id}",
                      json={"role": "member"})
        assert r.status_code in (200, 201), r.text[:200]
    for team, logical in ((backend, "acme/shop"), (backend, "acme/billing"), (edge, "acme/gateway")):
        r = admin.req("PUT", "/api/access/rules", json={
            "team_id": team, "repo_slug": st.slug_of(logical), "visibility": "code"})
        assert r.status_code == 200, r.text[:200]
    out.update(backend=backend, edge=edge)
    return out


def add_unruled_repo(st) -> str:
    """A fourth repository (a clone of shop's source) that no team rule mentions."""
    from tests.e2e_local.world import _git  # noqa: PLC2701

    src = st.root / "src-repos" / "acme-legacy"
    subprocess.run(["git", "clone", "-q", "--no-local", f"file://{st.world.repos['acme/shop']}",
                    str(src)], check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})
    _git(src, "checkout", "-q", "-B", "develop")
    repo = st.add_repo("acme/legacy", "https://github.com/acme/legacy", src, workspace="ws-a")
    st.world.repos["acme/legacy"] = src
    return repo.slug


def issue(su: Api, st, who: str, repos: list[str], *, profile: str = "dev",
          days: int = 30, label: str = "") -> tuple[str, str]:
    r = su.req("POST", "/api/admin/mcp-tokens", json={
        "user_ref": st.users[who].email, "workspace_id": st.ws_ids["ws-a"],
        "repos": [st.slug_of(x) if x in st.repos else x for x in repos],
        "expires_in_days": days, "profile": profile, "label": label or f"journey-{who}"})
    assert r.status_code == 201, f"issue {who}: {r.status_code} {r.text[:200]}"
    body = r.json()
    return body["token"], body["id"]


def run_synthetic(out: Path, *, baseline: bool = True) -> int:
    from tests.e2e_local.stack import Stack

    t_start = time.perf_counter()
    m = Matrix()
    sink: list[Call] = []
    tmp = Path(tempfile.mkdtemp(prefix="celmis-journey-"))
    st = Stack(tmp)
    timings: dict[str, float] = {}
    try:
        t0 = time.perf_counter()
        print("1. start Celmis", flush=True)
        st.start()
        timings["start_and_index"] = time.perf_counter() - t0
        slug_legacy = add_unruled_repo(st)
        shop, billing, gateway = (st.slug_of(x) for x in ("acme/shop", "acme/billing", "acme/gateway"))
        canaries = [e2e.Secret(k, v) for k, v in st.world.canaries.items()]
        # user ids and workspace roles really exist in the product's tables
        m.add("U1", "six roles exist: superadmin, owner, admin, editor, member, viewer",
              all(n in st.users for n in ("su", "owner", "admin", "editor", "member", "viewer")))

        print("2. teams and access rules over REST", flush=True)
        acc = setup_access(st, sink)
        apis = {n: Api(st.url, st.users[n]) for n in ("su", "owner", "admin", "editor", "member", "viewer")}

        print("3. the superadmin issues tokens over REST", flush=True)
        t0 = time.perf_counter()
        tok_a, id_a = issue(apis["su"], st, "dev", ["acme/shop", "acme/billing"], label="dev A")
        tok_b, id_b = issue(apis["su"], st, "editor", ["acme/gateway"], label="dev B")
        tok_x, id_x = issue(apis["su"], st, "member", ["*"], label="to be expired")
        tok_r, id_r = issue(apis["su"], st, "viewer", ["*"], label="to be revoked")
        tok_m, id_m = issue(apis["su"], st, "member", ["*"], label="member all")
        tok_v, id_v = issue(apis["su"], st, "viewer", ["*"], label="viewer all")
        tok_o, id_o = issue(apis["su"], st, "owner", ["*"], label="owner all")
        tok_ad, id_ad = issue(apis["su"], st, "admin", ["*"], label="admin all")
        tok_su, id_su = issue(apis["su"], st, "su", ["*"], label="superadmin all")
        tok_full, id_full = issue(apis["su"], st, "dev", ["*"], profile="full", label="full read-only")
        timings["issue_tokens"] = time.perf_counter() - t0

        def client(who: str, token: str, path: str = DEV) -> Recorder:
            return Recorder(st.url, path, token, who, sink)

        a, b = client("devA", tok_a), client("devB", tok_b)
        # who may issue
        for who in ("owner", "admin", "editor", "member", "viewer"):
            r = apis[who].req("POST", "/api/admin/mcp-tokens", json={
                "user_ref": st.users[who].email, "workspace_id": st.ws_ids["ws-a"],
                "repos": ["*"], "expires_in_days": 1})
            m.add(f"T1-{who}", f"{who} cannot issue an MCP token (403)", r.status_code == 403,
                  str(r.status_code))
            r = apis[who].req("POST", "/api/mcp/token", json={})
            m.add(f"T2-{who}", f"{who} cannot self-issue through the browser route (403)",
                  r.status_code == 403, str(r.status_code))
        # list shows token ids but never a token value
        lst = apis["su"].req("GET", "/api/admin/mcp-tokens").text
        m.add("T3", "the token list never contains a token value",
              not any(t in lst for t in (tok_a, tok_b, tok_x, tok_r)))

        print("4. scenarios over /mcp/dev/", flush=True)
        data = e2e.load_scenarios(e2e.BUNDLED_GOLD)
        reports: dict[str, Any] = {}
        for label, cl, only in (("devA", a, None), ("owner_all", client("owner", tok_o), None),
                                ("devB", b, None)):
            t0 = time.perf_counter()
            rep = e2e.run_scenarios(cl, data, clones=dict(st.world.repos), secrets_=canaries,
                                    baseline=baseline, only=only)
            timings[f"scenarios_{label}"] = time.perf_counter() - t0
            reports[label] = rep
            print(f"   {label}: {len(rep.ran)} ran, {len(rep.results) - len(rep.ran)} skipped, "
                  f"{'PASS' if rep.ok else 'FAIL'}", flush=True)
        # S9 ("which repositories can I read") names all three repos by design, so a
        # restricted token is checked on its own text instead.
        bad_a = failing(reports["devA"], ignore=("S9",))
        m.add("S-A", "developer A: every scenario on svc-a/svc-b passes; scenarios about svc-c are "
              "skipped, not wrong", not bad_a and 0 < len(reports["devA"].ran) < len(data["scenarios"]),
              "; ".join(bad_a))
        bad_all = failing(reports["owner_all"])
        m.add("S-all", "owner (all repos): every scenario passes", not bad_all, "; ".join(bad_all))
        skipped_b = {r.id.split("-")[0] for r in reports["devB"].results if r.skipped}
        # S4 and S8 ask across repositories: for B they must find nothing, so their gold
        # (svc-a / svc-b hits) is not met; B-symbols below checks the emptiness.
        bad_b = failing(reports["devB"], ignore=("S4", "S8", "S9"))
        m.add("S-B", "developer B: scenarios about other services are not visible, the rest pass",
              not bad_b and {"S1", "S2", "S5", "S6", "S7", "S13", "S14"} <= skipped_b, "; ".join(bad_b))
        # the same repositories asked about in this journey's own three requests
        # (kept in gold.yaml as S2, S13, S14)

        print("5. security matrix over HTTP", flush=True)
        run_matrix(st, m, sink, canaries, apis, acc, clients=dict(
            a=a, b=b, tok_x=tok_x, id_x=id_x, tok_r=tok_r, id_r=id_r, tok_m=tok_m, tok_v=tok_v,
            tok_o=tok_o, tok_ad=tok_ad, tok_su=tok_su, tok_full=tok_full, id_a=id_a, id_b=id_b,
            tok_a=tok_a, tok_a_now=tok_a, tok_b=tok_b, slug_legacy=slug_legacy), client=client)

        # leak scan over everything captured
        blobs = [(c.who + ":" + c.tool, c.text + c.raw) for c in sink]
        blobs += [("rest", t) for api in apis.values() for t in api.texts]
        blobs += [("rest-admin", t) for t in acc["api"]["admin"].texts]
        blobs += [("server log", st.log_text()), ("audit rows", json.dumps(st.audit_rows(), default=str))]
        leaks = [f"{where}: {label}" for where, text in blobs for label in e2e.scan(text, canaries)]
        issued = [tok_a, tok_b, tok_x, tok_r, tok_m, tok_v, tok_o, tok_ad, tok_su, tok_full]
        m.add("L1", f"no planted secret in any of {len(blobs)} captured outputs, REST bodies, logs and "
              f"audit rows ({len(canaries)} canaries)", not leaks, "; ".join(leaks[:5]))
        in_logs = [t for t in issued if t in st.log_text()]
        m.add("L2", "no token value in the server log", not in_logs)
        # a token value is shown once, in the response that issued it, and nowhere else
        elsewhere = [where for where, text in blobs
                     if '"mcp_json"' not in text and any(t in text for t in issued)]
        m.add("L3", "a token value appears only in the response that issued it (never in a list, "
              "call log, audit or log)", not elsewhere, "; ".join(elsewhere[:5]))
        timings["total"] = time.perf_counter() - t_start

        write_outputs(out, st, sink, m, reports, timings, data, tmp_note="synthetic acme fixtures")
        return 0 if m.ok else 1
    finally:
        st.stop()
        shutil.rmtree(tmp, ignore_errors=True)


@contextlib.contextmanager
def self_service_on():
    """CELMIS_MCP_SELF_SERVICE=true for the length of the block (settings are cached)."""
    from src.config import get_settings

    prev = os.environ.get("CELMIS_MCP_SELF_SERVICE")
    os.environ["CELMIS_MCP_SELF_SERVICE"] = "true"
    get_settings.cache_clear()
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("CELMIS_MCP_SELF_SERVICE", None)
        else:
            os.environ["CELMIS_MCP_SELF_SERVICE"] = prev
        get_settings.cache_clear()


def run_matrix(st, m: Matrix, sink: list[Call], canaries, apis, acc, *, clients: dict[str, Any],
               client) -> None:
    c = clients
    shop, billing, gateway = (st.slug_of(x) for x in ("acme/shop", "acme/billing", "acme/gateway"))
    legacy = c["slug_legacy"]
    a, b = c["a"], c["b"]

    # ---- visibility per caller -------------------------------------------------
    seen_a = visible_slugs(a.call("repos").text)
    m.add("V1", "developer A sees exactly svc-a and svc-b", seen_a == {shop, billing}, str(sorted(seen_a)))
    seen_b = visible_slugs(b.call("repos").text)
    m.add("V2", "developer B sees exactly svc-c", seen_b == {gateway}, str(sorted(seen_b)))
    # A token the superadmin issued reaches exactly its list, whatever the holder's team
    # rights (documented: the superadmin is authoritative). With "*" that is everything.
    seen_m = visible_slugs(client("member", c["tok_m"]).call("repos").text)
    m.add("V3", "a superadmin-issued * token reaches every repo of the workspace, ruled or not "
          "(by design)", seen_m == {shop, billing, gateway, legacy}, str(sorted(seen_m)))
    seen_v = visible_slugs(client("viewer", c["tok_v"]).call("repos").text)
    m.add("V4", "...for a viewer too: the person is the superadmin's choice", seen_v == seen_m,
          str(sorted(seen_v)))
    # The person's own rights apply to everything that is not a superadmin-issued list:
    # self-service tokens (opt-in), the browser and the REST API.
    with self_service_on():
        selfs = {}
        for who in ("member", "viewer", "editor"):
            r = apis[who].req("POST", "/api/mcp/token", json={})
            selfs[who] = r.json().get("token", "") if r.status_code == 200 else ""
            m.add(f"V7-{who}", f"with self-service on, {who} can issue a * token for themselves",
                  r.status_code == 200, str(r.status_code))
    c["tok_self_m"] = selfs["member"]
    sm = visible_slugs(client("member-self", selfs["member"]).call("repos").text)
    sv = visible_slugs(client("viewer-self", selfs["viewer"]).call("repos").text)
    se = visible_slugs(client("editor-self", selfs["editor"]).call("repos").text)
    m.add("V8", "member's own * token: the repos their team has a rule on, not the edge repo, "
          "not the repo without a rule", sm == {shop, billing}, str(sorted(sm)))
    m.add("V9", "viewer without a team: own * token sees nothing, not even the existence of repos",
          sv == set(), str(sorted(sv)))
    m.add("V10", "editor in the edge team: own * token sees only the edge repo", se == {gateway},
          str(sorted(se)))
    m.add("V6", "a repository without a rule is invisible to member, viewer and editor "
          "(self-issued tokens)", legacy not in sm | sv | se)
    for who in ("member", "viewer", "editor", "owner", "admin"):
        txt = apis[who].req("GET", "/api/repos").text
        shown = {x for x in (shop, billing, gateway, legacy) if x in txt}
        want = {"member": {shop, billing}, "viewer": set(), "editor": {gateway},
                "owner": {shop, billing, gateway, legacy}, "admin": {shop, billing, gateway, legacy}}[who]
        m.add(f"V11-{who}", f"REST /api/repos for {who} lists exactly their repos", shown == want,
              str(sorted(shown)))
    for who, key in (("owner", "tok_o"), ("admin", "tok_ad"), ("su", "tok_su")):
        seen = visible_slugs(client(who, c[key]).call("repos").text)
        m.add(f"V5-{who}", f"{who} (token *) sees all four including the unruled repo",
              seen == {shop, billing, gateway, legacy}, str(sorted(seen)))

    # ---- developer B cannot learn that svc-a / svc-b exist ----------------------
    probes = {
        "outline": lambda r: {"repo": r, "path": "app/db.py"},
        "read_symbol": lambda r: {"repo": r, "name": "build_engine"},
        "refs": lambda r: {"repo": r, "symbol": "build_engine", "direction": "callers"},
        "grep": lambda r: {"repo": r, "pattern": "create_engine"},
        "map": lambda r: {"repo": r},
        "find": lambda r: {"repo": r, "query": "build_engine"},
        "howto": lambda r: {"repo": r, "topic": "db"},
    }
    for tool, mk in probes.items():
        base = plain(b.call(tool, mk(NOBODY)).text, NOBODY)
        for typed in (shop, billing, legacy):
            got = plain(b.call(tool, mk(typed)).text, typed)
            m.add(f"B-{tool}-{typed.split('_')[-1]}",
                  f"dev B asking {tool} about a blocked repo gets the answer for a missing one",
                  got == base, f"{got[:120]!r} vs {base[:120]!r}")
    b_text = "\n".join(c_.text for c_ in sink if c_.who == "devB")
    hidden = [s for s in (shop, billing, legacy, "acme/shop", "acme/billing") if s in b_text
              and s not in {typed for typed in ()}]
    # the slugs appear in dev B's own answers only inside the echo of what dev B typed
    echoes = [s for s in hidden if s not in "\n".join(
        json.dumps(c_.args) for c_ in sink if c_.who == "devB")]
    m.add("B-text", "no slug, count or notice for a blocked repo in any dev B output", not echoes,
          ", ".join(echoes))
    findings = b.call("find", {"query": "create_order"}).text + b.call(
        "grep", {"pattern": "DB_POOL_SIZE"}).text + b.call("find", {"query": "createPool"}).text
    m.add("B-symbols", "dev B finds nothing from svc-a/svc-b by symbol or text across repos",
          not any(w in findings for w in ("orders.py", "pool.ts", "config.ts")), findings[:200])
    m.add("B-counts", "dev B's repos answer carries no count of hidden repositories",
          not re.search(r"\b(hidden|blocked|other|more) repos?\b|\b[2-9]\d* repos\b",
                        b.call("repos").text.lower()))

    # ---- credentials that must not work ----------------------------------------
    for label, tok in (("expired", c["tok_x"]), ("revoked", c["tok_r"])):
        who = "member" if label == "expired" else "viewer"
        cl = client(who, tok)
        before = refused(cl)
        m.add(f"X-{label}-live", f"a fresh {label}-to-be token works first", before is None, str(before))
    st.expire_token(c["id_x"])
    r = apis["su"].req("POST", f"/api/admin/mcp-tokens/{c['id_r']}/revoke")
    m.add("X-revoke-api", "the superadmin revokes a token over REST", r.status_code == 200, r.text[:120])
    s1 = refused(client("member", c["tok_x"]))
    s2 = refused(client("viewer", c["tok_r"]))
    m.add("X-expired", "an expired token is refused at once (401/403)", s1 in (401, 403), str(s1))
    m.add("X-revoked", "a revoked token is refused at once (401/403)", s2 in (401, 403), str(s2))
    for label, tok in (("garbage", "not-a-token"), ("empty", "")):
        s = refused(client("anon", tok or None))
        m.add(f"X-{label}", f"a {label} credential is refused (401/403)", s in (401, 403), str(s))
    devfull = client("devA-full", c["tok_a"], FULL)
    s3 = refused(devfull, "list_accessible_repos")
    if s3 is None:  # the mount accepts the token; every tool must refuse it
        outs = [devfull.call(t, args) for t, args in (
            ("list_accessible_repos", {}), ("search_symbols", {"query": "create_order"}),
            ("get_api_surface", {"repo": shop}))]
        s3 = 403 if all(o.is_error and "scope" in o.text.lower() for o in outs) else None
    m.add("X-dev-on-full", "a dev token cannot call the full /mcp tools (refused by scope)",
          s3 in (401, 403), str(s3))

    # ---- write ------------------------------------------------------------------
    full = client("full-ro", c["tok_full"], FULL)
    names = {t["name"] for t in full.list_tools()}
    m.add("W1", "a read-only full token does not even list a write tool", "add_repo" not in names)
    res = full.call("add_repo", {"url": "https://github.com/acme/never-added", "index": False})
    refused_ = res.is_error or any(w in res.text.lower() for w in (
        "denied", "scope", "not allowed", "forbidden", "unknown tool"))
    m.add("W2", "calling a write tool with a read-only token is refused", refused_, res.text[:160])
    r = apis["su"].req("POST", "/api/admin/mcp-tokens", json={
        "user_ref": st.users["dev"].email, "workspace_id": st.ws_ids["ws-a"], "repos": ["*"],
        "profile": "dev", "allow_write": True})
    m.add("W3", "a dev-profile token cannot be given write rights (422)", r.status_code == 422,
          str(r.status_code))
    dev_tools = {t["name"] for t in a.list_tools()}
    m.add("W4", "the dev profile lists only read tools",
          dev_tools == {"repos", "find", "outline", "read_symbol", "refs", "grep", "map", "ask", "howto"},
          str(sorted(dev_tools)))

    # ---- rule changes take effect live ------------------------------------------
    r = acc["api"]["admin"].req("PUT", "/api/access/rules", json={
        "team_id": acc["backend"], "repo_slug": legacy, "visibility": "code"})
    now = visible_slugs(client("member-self", c["tok_self_m"]).call("repos").text)
    m.add("R1", "granting the unruled repo to a team makes it appear for that team's member",
          legacy in now and r.status_code == 200, str(sorted(now)))
    rid = [x["id"] for x in acc["api"]["admin"].req("GET", f"/api/access/rules?repo_slug={legacy}").json()]
    for i in rid:
        acc["api"]["admin"].req("DELETE", f"/api/access/rules/{i}")
    now = visible_slugs(client("member-self", c["tok_self_m"]).call("repos").text)
    m.add("R2", "removing the rule hides it again", legacy not in now, str(sorted(now)))
    r = apis["su"].req("PATCH", f"/api/admin/mcp-tokens/{c['id_a']}", json={"repos": ["acme/shop"
                                                                                   if False else shop]})
    now = visible_slugs(a.call("repos").text)
    m.add("R3", "the superadmin narrows developer A's token live: svc-b disappears",
          r.status_code == 200 and now == {shop}, f"{r.status_code} {sorted(now)}")

    # ---- the Claude Code plugin ---------------------------------------------------
    plugin = ROOT / "packaging" / "claude-plugin" / "celmis-code"
    spec = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["celmis"]
    url = spec["url"].replace("${CELMIS_URL}", st.url)
    auth = spec["headers"]["Authorization"].replace("${CELMIS_TOKEN}", c["tok_a_now"])
    m.add("P1", "the plugin's .mcp.json points at /mcp/dev/ and reads the token from the environment",
          url.endswith("/mcp/dev/") and "${CELMIS_TOKEN}" in spec["headers"]["Authorization"]
          and c["tok_a_now"] not in (plugin / ".mcp.json").read_text(encoding="utf-8"))
    base, _, path = url.partition("/mcp/")
    via = Recorder(base, "/mcp/" + path, auth.split(" ", 1)[1], "plugin", sink)
    m.add("P2", "a client configured exactly as the plugin configures it connects and lists repos",
          bool(visible_slugs(via.call("repos").text)))
    # the dirty-guard hook, fed a REAL find answer, with a local checkout that differs
    work = st.root / "dirty-guard" / "shop"
    work.parent.mkdir(exist_ok=True)
    subprocess.run(["git", "clone", "-q", "--no-local", f"file://{st.world.repos['acme/shop']}", str(work)],
                   check=True, capture_output=True, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})
    subprocess.run(["git", "-C", str(work), "remote", "set-url", "origin", "https://github.com/acme/shop.git"],
                   check=True, capture_output=True)
    answer = via.call("find", {"query": "create_order", "repo": shop}).text
    (work / "app" / "services" / "orders.py").write_text("changed locally\n", encoding="utf-8")
    event = {"tool_name": "mcp__plugin_celmis-code_celmis__find", "cwd": str(work), "tool_input": {},
             "tool_response": [{"type": "text", "text": answer}]}
    done = subprocess.run([sys.executable, "-I", str(plugin / "hooks" / "celmis_hook.py"), "postmcp"],
                          input=json.dumps(event), capture_output=True, text=True, timeout=30)
    m.add("P3", "the dirty-guard hook flags a hit file that differs locally from the indexed commit",
          done.returncode == 0 and "stale-locally" in done.stdout and "orders.py" in done.stdout,
          (done.stdout + done.stderr)[:200])

    # ---- audit -------------------------------------------------------------------
    rows = st.wait_audit(len([x for x in sink if x.status in ("ok", "tool_error")
                              and x.path in (DEV, FULL)]) - 1, timeout=30)
    cols = set(rows[0]) if rows else set()
    m.add("A1", "the audit has rows with who, token, tool, repos, status, size and no content columns",
          {"tool", "token_id", "user_id", "repos", "status", "result_bytes"} <= cols
          and not ({"result", "content", "args", "query", "text"} & cols), str(sorted(cols)))
    needle = "needle_journey_43210"
    a.call("grep", {"pattern": needle, "repo": shop})
    time.sleep(1.0)
    rows = st.audit_rows()
    m.add("A2", "a search term never reaches the audit (only a hash)",
          needle not in json.dumps(rows, default=str))
    by_token = {r["token_id"] for r in rows}
    m.add("A3", "rows exist for every token that called", {c["id_a"], c["id_b"]} <= by_token)
    calls = apis["su"].req("GET", "/api/admin/mcp-calls").text
    m.add("A4", "the superadmin can read the call log over REST, and it carries no secret or content",
          needle not in calls and not e2e.scan(calls, canaries) and len(calls) > 50)
    lst = apis["su"].req("GET", "/api/admin/mcp-tokens").json()
    used = {t["id"]: t["last_used_at"] for t in lst}
    m.add("A5", "last-used is recorded for a token that was used", bool(used.get(c["id_b"])))


def write_outputs(out: Path, st, sink: list[Call], m: Matrix, reports: dict[str, Any],
                  timings: dict[str, float], data: dict[str, Any], tmp_note: str) -> None:
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    per_call = [{"who": c.who, "tool": c.tool, "args": c.args, "chars": c.chars, "tokens": c.tokens,
                 "ms": round(c.ms), "status": c.status} for c in sink]
    audit_ms = [r.get("duration_ms") for r in st.audit_rows()]
    (out / "calls.jsonl").write_text("\n".join(json.dumps(x) for x in per_call) + "\n")
    (out / "outputs.jsonl").write_text("\n".join(
        json.dumps({"who": c.who, "tool": c.tool, "args": c.args, "text": c.text}) for c in sink) + "\n")
    summary = {
        "note": tmp_note, "timings_s": {k: round(v, 1) for k, v in timings.items()},
        "matrix": [asdict(r) for r in m.rows], "matrix_ok": m.ok,
        "scenarios": {k: e2e.report_json(v) for k, v in reports.items()},
        "calls": len(sink), "server_ms_median": sorted(x for x in audit_ms if x is not None)[
            len([x for x in audit_ms if x is not None]) // 2] if audit_ms else None,
    }
    (out / "journey.json").write_text(json.dumps(summary, indent=2, default=str))
    for k, v in reports.items():
        (out / f"scenarios-{k}.txt").write_text(e2e.render(v) + "\n")
    # per-tool table
    by_tool: dict[str, list[Call]] = {}
    for c in sink:
        if c.status in ("ok", "tool_error"):
            by_tool.setdefault(c.tool, []).append(c)
    lines = [f"{'tool':12} {'calls':>5} {'avg chars':>10} {'avg tok':>8} {'max tok':>8} "
             f"{'client ms med':>14}"]
    for tool, cs in sorted(by_tool.items()):
        ms = sorted(x.ms for x in cs)
        lines.append(f"{tool:12} {len(cs):>5} {sum(x.chars for x in cs) // len(cs):>10} "
                     f"{sum(x.tokens for x in cs) // len(cs):>8} {max(x.tokens for x in cs):>8} "
                     f"{round(ms[len(ms) // 2]):>14}")
    (out / "per-tool.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nmatrix: {sum(r.ok for r in m.rows)}/{len(m.rows)} ok -> {out}")


# ───────────────────────────── real repositories ───────────────────────────

_REAL_SECRET_LITERAL = re.compile(
    r"""(?ix)\b(?:password|passwd|pwd|secret|api[_-]?key|token|private[_-]?key)\w*\s*[:=]\s*
        ["']([^"'\s${}<>()\[\]]{8,})["']""")
_PLACEHOLDER = re.compile(r"(?i)changeme|example|your[-_]|xxx|placeholder|dummy|test|sample|"
                          r"password|secret|token|none|null|true|false|localhost|\.\.\.|<")


def literal_secrets_in(clone: Path) -> list[e2e.Secret]:
    """Values of password-like literals in TRACKED files (in memory; the name is the path)."""
    found: dict[str, e2e.Secret] = {}
    files = subprocess.run(["git", "-C", str(clone), "ls-files"], capture_output=True, text=True).stdout
    for rel in files.splitlines():
        p = clone / rel
        try:
            if p.stat().st_size > 400_000 or p.suffix.lower() in (".png", ".jpg", ".pdf", ".zip", ".lock"):
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for mm in _REAL_SECRET_LITERAL.finditer(text):
            val = mm.group(1)
            if _PLACEHOLDER.search(val) or len(set(val)) < 5:
                continue
            # A header or field NAME ("x-api-key") has no digit and is made of lowercase words;
            # secrets are mixed or long.
            if not re.search(r"\d", val) and len(val) < 20 and not re.search(r"[A-Z]", val):
                continue
            found.setdefault(val, e2e.Secret(f"{clone.name}:{rel}", val))
    return list(found.values())


_SECRET_NAME = re.compile(r"(?i)pass|secret|token|key|private|credential|signature|salt|cert")
_DSN_PW = re.compile(r"://[^:/@\s]+:[^@\s]+@")


def public_value(secret: e2e.Secret, clones: list[Path]) -> bool:
    """True for a local ``.env`` value that is public by construction.

    A value of a variable whose name does not look secret, that is not a DSN with a
    password, and that is committed in the repository (a default in code, a tracked
    ``.env.config``) is configuration, not a secret: the howto tool shows defaults in code
    on purpose. Everything else stays armed.
    """
    var = secret.name.split(":")[-1]
    if _SECRET_NAME.search(var) or _DSN_PW.search(secret.value):
        return False
    for clone in clones:
        hit = subprocess.run(["git", "-C", str(clone), "grep", "-qF", "--", secret.value],
                             capture_output=True)
        if hit.returncode == 0:
            return True
    return False


def navigation_scenarios(cl: Recorder, repos: dict[str, Path], slugs: dict[str, str]) -> list[dict[str, Any]]:
    """find / outline / read_symbol / refs on a symbol that howto itself pointed at."""
    out: list[dict[str, Any]] = []
    for name, slug in slugs.items():
        text = cl.call("howto", {"topic": "db", "repo": slug}).text
        m = re.search(r"(?m)^\S+ (?P<path>\S+):\d+-\d+ (?P<sym>[A-Za-z_]\w+)$", text)
        if not m:
            continue
        path, sym = m.group("path"), m.group("sym")
        short = re.sub(r"[^A-Za-z0-9]", "", name)[:14]
        out.append({"id": f"R-nav-{short}", "ask": f"Navigate to {sym} in {name}: find, outline, read, callers",
                    "steps": [{"tool": "find", "args": {"query": sym, "repo": name}},
                              {"tool": "outline", "args": {"repo": name, "path": path}},
                              {"tool": "read_symbol", "args": {"repo": name, "name": sym}},
                              {"tool": "refs", "args": {"repo": name, "symbol": sym, "direction": "callers"}}],
                    "baseline": {"repos": [name], "terms": [sym]}})
    return out


def real_scenarios(repos: dict[str, Path]) -> dict[str, Any]:
    """The three request types on every real repo, plus the generic tool probes."""
    scen: list[dict[str, Any]] = []
    for name in repos:
        short = re.sub(r"[^A-Za-z0-9]", "", name)[:14]
        scen.append({"id": f"R-db-{short}", "ask": f"Make a DB connection like in service {name}",
                     "steps": [{"tool": "howto", "args": {"topic": "db", "repo": name}}],
                     "baseline": {"repos": [name], "terms": ["DATABASE_URL", "create_engine", "connect"]},
                     "guidance": []})
        scen.append({"id": f"R-cred-{short}",
                     "ask": f"Find the DB connection in {name}, its credentials, and use them here",
                     "steps": [{"tool": "howto", "args": {"topic": "db", "repo": name}},
                               {"tool": "howto", "args": {"topic": "config", "repo": name}}],
                     "baseline": {"repos": [name], "terms": ["password", "getenv", "environ"]}})
        scen.append({"id": f"R-auth-{short}",
                     "ask": f"How is authorization done in {name}; do the same here",
                     "steps": [{"tool": "howto", "args": {"topic": "auth", "repo": name}}],
                     "baseline": {"repos": [name], "terms": ["Authorization", "jwt", "keycloak"]}})
        scen.append({"id": f"R-tools-{short}", "ask": f"Orient in {name}",
                     "steps": [{"tool": "repos", "args": {}},
                               {"tool": "map", "args": {"repo": name}},
                               {"tool": "grep", "args": {"pattern": "getenv|environ", "repo": name}}],
                     "baseline": {"repos": [name], "terms": ["getenv", "environ"]}})
        # what an agent hunting for secrets would try: the leak scan is the verdict
        probes = ("password", "secret", "token", "api[_-]?key", "BEGIN", "postgres(ql)?://",
                  "mongodb(\\+srv)?://", "Bearer ")
        scen.append({"id": f"R-probe-{short}", "ask": f"Probe: hunt for credentials in {name}",
                     "steps": [{"tool": "grep", "args": {"pattern": p_, "repo": name}} for p_ in probes]
                     + [{"tool": "outline", "args": {"repo": name, "path": rel}}
                        for rel in (".env", ".env.prod", "secrets/db_password.txt", "id_rsa")]})
    return {"version": 1, "repos": {n: {"dir": p.name} for n, p in repos.items()}, "scenarios": scen}


def run_real(out: Path, src_root: Path, dirs: list[str]) -> int:
    from tests.e2e_local.stack import Stack

    e2e.check_paths(out, None)
    m = Matrix()
    sink: list[Call] = []
    tmp = Path(tempfile.mkdtemp(prefix="celmis-journey-real-"))
    st = Stack(tmp, repos=())
    try:
        st.start()
        clones: dict[str, Path] = {}
        for d in dirs:
            src = src_root / d
            dst = tmp / "clones" / d
            dst.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "-q", "--no-local", f"file://{src}", str(dst)],
                           check=True, capture_output=True,
                           env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})
            branch = subprocess.run(["git", "-C", str(dst), "rev-parse", "--abbrev-ref", "HEAD"],
                                    capture_output=True, text=True).stdout.strip()
            subprocess.run(["git", "-C", str(dst), "checkout", "-q", "-B", "develop"], check=True)
            t0 = time.perf_counter()
            repo = st.add_repo(d, f"https://github.com/realorg/{d}", dst, workspace="ws-a")
            print(f"indexed {d} ({branch}) in {time.perf_counter() - t0:.1f}s", flush=True)
            clones[d] = dst
            st.repos[d] = repo
        secrets_ = e2e.load_env_secrets(src_root, dirs)
        secrets_ = [x for x in secrets_ if not public_value(x, list(clones.values()))]
        for dst in clones.values():
            secrets_ += literal_secrets_in(dst)
        print(f"leak scan armed: {len({s.name for s in secrets_})} secret names "
              "(.env values and tracked literals; values stay in memory)", flush=True)
        su = Api(st.url, st.users["su"])
        tok, _tid = issue(su, st, "dev", list(clones), label="real repos")
        cl = Recorder(st.url, DEV, tok, "real", sink)
        data = real_scenarios(clones)
        data["scenarios"] += navigation_scenarios(cl, clones, {d: st.slug_of(d) for d in clones})
        rep = e2e.run_scenarios(cl, data, clones=clones, secrets_=secrets_, baseline=True)
        # the gold-free scenarios only check budget, idx line, leaks, errors
        write_real(out, sink, rep, secrets_)
        leaks = rep.leaks + rep.extra_leaks
        for c_ in sink:
            for lab in set(e2e.scan(c_.text, secrets_)) | set(e2e.scan(c_.raw, secrets_)):
                leaks.append(f"leak: {c_.tool} output contains the value of {lab}")
        for lab in e2e.scan(st.log_text(), secrets_):
            leaks.append(f"leak: server log, {lab}")
        for lab in e2e.scan(json.dumps(st.audit_rows(), default=str), secrets_):
            leaks.append(f"leak: audit, {lab}")
        m.add("REAL-leak", "no secret value of the real repositories in any output, log or audit row",
              not leaks, "; ".join(sorted(set(leaks))[:10]))
        (out / "real-matrix.json").write_text(json.dumps([asdict(r) for r in m.rows], indent=2))
        print(e2e.render(rep))
        return 0 if m.ok else 1
    finally:
        st.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def write_real(out: Path, sink: list[Call], rep, secrets_: list[e2e.Secret]) -> None:
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    safe = []
    for c in sink:
        leaked = e2e.scan(c.text, secrets_) or e2e.scan(c.raw, secrets_)
        safe.append({"tool": c.tool, "args": c.args, "chars": c.chars, "tokens": c.tokens,
                     "ms": round(c.ms), "status": c.status,
                     "text": "(withheld: leak)" if leaked else c.text})
    (out / "real-calls.json").write_text(json.dumps(safe, indent=1))
    (out / "real-report.txt").write_text(e2e.render(rep) + "\n")
    (out / "real-report.json").write_text(json.dumps(e2e.report_json(rep), indent=2))


# ───────────────────────────── entry ───────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import logging

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--real", help="comma separated directory names under --src-root")
    p.add_argument("--src-root", type=Path, default=Path.home() / "code")
    p.add_argument("--no-baseline", action="store_true")
    args = p.parse_args(argv)
    for noisy in ("mcp", "httpx", "uvicorn", "src", "alembic", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    out = (args.out or Path("~/.cache/celmis-journey") / time.strftime("%Y%m%d-%H%M%S")).expanduser()
    e2e.check_paths(out, None)
    if args.real:
        return run_real(out, args.src_root.expanduser(), args.real.split(","))
    return run_synthetic(out, baseline=not args.no_baseline)


if __name__ == "__main__":
    sys.exit(main())
