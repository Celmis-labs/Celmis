"""A deliberately small reference implementation of the ``/mcp/dev/`` contract.

It exists to test the HARNESS before, and independently of, the real tools: the
scenario runner, its scoring, the baseline arm and the leak scan need a server
that speaks the frozen contract (``idx:`` first line, ``slug path:start-end``
hits, ``howto`` with names and sources and no values). This is that server, in
about two hundred lines of ``git grep`` and regexes over the fixture checkouts.

It is NOT Celmis: no graph, no ranking worth the name, no access rules. With
``leaky=True`` it also drops the secret-file filter and the redaction, which is
how the tests prove that the runner's leak scan would catch a server that
returned a canary.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

SECRET_PATH = re.compile(
    r"(^|/)(\.env(?!\.(example|sample|template)$)[^/]*|id_rsa[^/]*|[^/]*\.(pem|key|p12|tfvars)|"
    r"secrets?/.*|[^/]*credentials[^/]*\.json|[^/]*secret[^/]*\.ya?ml)$")
REDACTIONS = [
    (re.compile(r"(://[^:/@\s]+:)[^@\s]+(@)"), r"\1[REDACTED:dsn-password]\2"),
    (re.compile(r"sk_live_[A-Za-z0-9_]+"), "[REDACTED:api-key]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:aws-key]"),
]
DEF_RES = {
    ".py": re.compile(r"^(?P<ind>\s*)(?P<kind>def|class) (?P<name>\w+)"),
    ".ts": re.compile(r"^(?P<ind>\s*)(?:export )?(?:async )?(?P<kind>function|class|interface) "
                      r"(?P<name>\w+)"),
    ".go": re.compile(r"^(?P<kind>func) (?:\([^)]*\) )?(?P<name>\w+)"),
}
TOPICS = {
    "db": ("create_engine", "new Pool", "sql.Open", "createPool", "build_engine", "DATABASE_URL"),
    "auth": ("jwt", "Authorization", "Middleware", "HTTPBearer", "get_current_user"),
    "config": ("os.getenv", "os.environ", "process.env", "os.Getenv", "loadConfig"),
}
ENV_NAME = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
SOURCE_FILES = (".env.example", "docker-compose.yml", "bitbucket-pipelines.yml",
                "internal/config/config.go", "config/gateway.yaml")
MORE = "… +{n} more · cursor={c}"


class ReferenceDev:
    def __init__(self, repos: dict[str, Path], token: str, *, leaky: bool = False) -> None:
        self.repos = {self.slug(name): path for name, path in repos.items()}
        self.token = token
        self.leaky = leaky
        self.url = ""
        self._server = None
        self._thread = None

    @staticmethod
    def slug(logical: str) -> str:
        return "github_" + logical.replace("/", "-")

    # ── helpers ──────────────────────────────────────────────────────

    def _git(self, slug: str, *args: str) -> str:
        done = subprocess.run(["git", "-C", str(self.repos[slug]), *args], capture_output=True,
                              text=True, timeout=30)
        return done.stdout

    def _sha(self, slug: str) -> str:
        return self._git(slug, "rev-parse", "HEAD").strip()[:8]

    def _idx(self, slugs: list[str]) -> str:
        return "idx: " + " · ".join(f"{s} develop@{self._sha(s)} 1m fresh" for s in slugs)

    def _files(self, slug: str) -> list[str]:
        names = [n for n in self._git(slug, "ls-files").splitlines() if n]
        return names if self.leaky else [n for n in names if not SECRET_PATH.search(n)]

    def _lines(self, slug: str, path: str) -> list[str]:
        return self._git(slug, "show", f"HEAD:{path}").splitlines()

    def _clean(self, text: str) -> str:
        if self.leaky:
            return text
        for pattern, repl in REDACTIONS:
            text = pattern.sub(repl, text)
        return re.sub(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", "[REDACTED:private-key]",
                      text, flags=re.S)

    def _resolve(self, repo: str) -> str | None:
        return repo if repo in self.repos else next(
            (s for s in self.repos if s.endswith(repo.replace("/", "-"))), None)

    def _symbols(self, slug: str, path: str) -> list[dict]:
        rx = DEF_RES.get(Path(path).suffix)
        if rx is None:
            return []
        lines = self._lines(slug, path)
        found = [(i + 1, m) for i, ln in enumerate(lines) if (m := rx.match(ln))]
        out = []
        for n, (start, m) in enumerate(found):
            end = (found[n + 1][0] - 1) if n + 1 < len(found) else len(lines)
            while end > start and not lines[end - 1].strip():
                end -= 1
            out.append({"name": m.group("name"), "kind": m.group("kind"), "start": start,
                        "end": end, "sig": lines[start - 1].strip()})
        return out

    def _all_symbols(self, slugs: list[str]):
        for s in slugs:
            for path in self._files(s):
                for sym in self._symbols(s, path):
                    yield s, path, sym

    # ── tools ────────────────────────────────────────────────────────

    def repos_tool(self) -> str:
        slugs = sorted(self.repos)
        return "\n".join([self._idx(slugs), *(f"{s} develop@{self._sha(s)} code" for s in slugs)])

    def find(self, query: str, repo: str = "", limit: int = 10) -> str:
        slugs = [self._resolve(repo)] if repo else sorted(self.repos)
        slugs = [s for s in slugs if s]
        q = query.lower()
        hits = []
        for s, path, sym in self._all_symbols(slugs):
            n = sym["name"].lower()
            rank = 0 if n == q else 1 if n.startswith(q) else 2 if q in n else None
            if rank is not None:
                hits.append((rank, s, path, sym))
        hits.sort(key=lambda h: (h[0], h[1], h[2], h[3]["start"]))
        lines = [self._idx(slugs)]
        lines += [f"{s} {p}:{y['start']}-{y['end']} {y['kind']} {self._clean(y['sig'])}"
                  for _r, s, p, y in hits[:limit]]
        if len(hits) > limit:
            lines.append(MORE.format(n=len(hits) - limit, c=f"o{limit}"))
        if not hits:
            lines.append(f'no symbol "{query}"')
        return "\n".join(lines)

    def outline(self, repo: str, path: str) -> str:
        s = self._resolve(repo)
        if s is None or path not in self._files(s):
            return "idx: unknown\nrepo or path not found"
        lines = [self._idx([s])]
        lines += [f"{s} {path}:{y['start']}-{y['end']} {y['kind']} {y['name']}"
                  for y in self._symbols(s, path)]
        return "\n".join(lines)

    def read_symbol(self, repo: str, name: str, path: str = "") -> str:
        s = self._resolve(repo)
        if s is None:
            return "idx: unknown\nrepo not found"
        for _s, p, y in self._all_symbols([s]):
            if y["name"] == name and (not path or p == path):
                body = self._lines(s, p)[y["start"] - 1:y["end"]][:60]
                numbered = [f"{y['start'] + i:>5}| {self._clean(ln)}" for i, ln in enumerate(body)]
                return "\n".join([self._idx([s]),
                                  f"{s} {p}:{y['start']}-{y['end']} {y['kind']} {name}", *numbered])
        return f'{self._idx([s])}\nno symbol "{name}" in {s}'

    def refs(self, repo: str, symbol: str, direction: str = "callers") -> str:
        s = self._resolve(repo)
        if s is None:
            return "idx: unknown\nrepo not found"
        lines = [self._idx([s])]
        for path in self._files(s):
            syms = self._symbols(s, path)
            for i, ln in enumerate(self._lines(s, path), 1):
                if re.search(rf"\b{re.escape(symbol)}\s*\(", ln) and not any(
                        y["start"] == i and y["name"] == symbol for y in syms):
                    owner = next((y["name"] for y in syms if y["start"] <= i <= y["end"]), "(module)")
                    lines.append(f"{s} {path}:{i} {direction[:-1]} {owner} {self._clean(ln.strip())}")
        if len(lines) == 1:
            lines.append(f'no {direction} of "{symbol}" in {s}')
        return "\n".join(lines)

    def grep(self, pattern: str, repo: str = "") -> str:
        slugs = [self._resolve(repo)] if repo else sorted(self.repos)
        slugs = [s for s in slugs if s]
        lines = [self._idx(slugs)]
        for s in slugs:
            allowed = set(self._files(s))
            for row in self._git(s, "grep", "-n", "-I", "-i", "-F", "-e", pattern).splitlines():
                m = re.match(r"^(.*?):(\d+):(.*)$", row)
                if m and m.group(1) in allowed:
                    lines.append(f"{s} {m.group(1)}:{m.group(2)} {self._clean(m.group(3).strip())[:160]}")
        return "\n".join(lines[:21] if len(lines) > 1 else lines + ["no matches"])

    def map(self, repo: str) -> str:
        s = self._resolve(repo)
        if s is None:
            return "idx: unknown\nrepo not found"
        return "\n".join([self._idx([s]), *(f"{s} {p}" for p in self._files(s))])

    def howto(self, topic: str, repo: str) -> str:
        s = self._resolve(repo)
        if s is None or topic not in TOPICS:
            return "idx: unknown\nrepo not found or unknown topic"
        files = self._files(s)
        slices, names = [], []
        for path in files:
            if path.startswith("tests/") or Path(path).suffix not in DEF_RES:
                continue
            body = self._lines(s, path)
            for i, ln in enumerate(body, 1):
                if any(k in ln for k in TOPICS[topic]):
                    sym = next((y for y in self._symbols(s, path) if y["start"] <= i <= y["end"]),
                               None)
                    if sym and (path, sym["start"]) not in [(a, b) for a, b, *_ in slices]:
                        slices.append((path, sym["start"], sym["end"], sym["name"]))
        out = [self._idx([s]), f"howto {topic} {s}"]
        for n, (path, a, b, name) in enumerate(slices[:3], 1):
            out.append(f"{n}) {path}:{a}-{b} {name}")
            body = self._lines(s, path)[a - 1:b]
            out += [f"   {self._clean(ln)}" for ln in body[:14]]
            names += ENV_NAME.findall("\n".join(body))
        names += [n for p in files for n in ENV_NAME.findall(
            "\n".join(self._lines(s, p))) if p in SOURCE_FILES and topic in ("db", "config", "auth")
            and not n.startswith(("NODE", "GIT", "HTTP", "ES20"))]
        names = sorted(set(names))
        out.append("inputs (names only; values are never available via Celmis):")
        for name in names:
            where = [f"{p}" for p in files if p in SOURCE_FILES and name in "\n".join(
                self._lines(s, p))]
            out.append(f"  {name}  read in code | {' | '.join(where) or 'code only'}")
        legacy = [(p, i) for p in files for i, ln in enumerate(self._lines(s, p), 1)
                  if REDACTIONS[0][0].search(ln)]
        for p, i in legacy:
            out.append(f"warning: hardcoded credential {p}:{i} (dsn-password), do not copy")
        out.append("next: copy the pattern; obtain values from the sources above (deployment "
                   "variables / secret store); ask the user or ops for them; do not search for "
                   "values.")
        return self._clean("\n".join(out)) if not self.leaky else "\n".join(out)

    # ── serving ──────────────────────────────────────────────────────

    def build_app(self):
        mcp = FastMCP("reference-dev", streamable_http_path="/",
                      transport_security=TransportSecuritySettings(
                          enable_dns_rebinding_protection=False))
        for fn in (self.repos_tool, self.find, self.outline, self.read_symbol, self.refs,
                   self.grep, self.map, self.howto):
            name = "repos" if fn.__name__ == "repos_tool" else fn.__name__
            mcp.add_tool(fn, name=name, structured_output=False)
        mcp.add_tool(lambda question: self._idx(sorted(self.repos)) + "\nask is not available",
                     name="ask", structured_output=False)
        inner = mcp.streamable_http_app()
        token = self.token

        async def app(scope, receive, send):
            if scope["type"] == "lifespan":
                return await inner(scope, receive, send)
            if scope["type"] == "http":
                headers = dict(scope["headers"])
                if headers.get(b"authorization", b"").decode() != f"Bearer {token}":
                    await send({"type": "http.response.start", "status": 401,
                                "headers": [(b"content-type", b"application/json")]})
                    return await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                scope = dict(scope, path="/")
            return await inner(scope, receive, send)

        return app

    def start(self) -> ReferenceDev:
        server = uvicorn.Server(uvicorn.Config(self.build_app(), host="127.0.0.1", port=0,
                                               log_level="error", lifespan="on"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 30
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        port = server.servers[0].sockets[0].getsockname()[1]
        self._server, self._thread, self.url = server, thread, f"http://127.0.0.1:{port}"
        return self

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
            self._thread.join(timeout=10)

    def __enter__(self) -> ReferenceDev:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
