"""A small world for the dev-profile tests: two cloned repositories (real git
commits, real FalkorDBLite graphs) and a stub for who may read what.

The graphs are built by hand so that every line number in a graph row is the
line number of a real line in the committed file. The tests then check what
the tools print against `git show <sha>:<path>`.

Secret-looking values are assembled at runtime from fragments: this
repository's own scanners must never see one literally.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.indexing.graph.extractor import EdgeInfo, SymbolInfo
from src.indexing.graph.graph_store import make_graph_store

SHOP, BILLING = "github_acme-shop", "github_acme-billing"
#: Built at runtime — never a literal in this file.
CANARY_PW = "hunter" + "2canary" + "Pw"
CANARY_TOKEN = "tok" + "_live_" + "canaryAbCdEf0123456789"
DSN = f"postgresql://app:{CANARY_PW}@db.internal/shop"
#: The committed-secret shapes the generic code redactor does not know.
SHAPE_PW = "Zq8x" + "Lm2Pv9"
LONG_SECRET = "django-insecure-" + "9xk2q7wLp4" + "Rt6Yb8NcV0"

DB_PY = f'''\
import psycopg2


def connect():
    return psycopg2.connect(host='db', user='app', password='{SHAPE_PW}')


SYNC = create_order_remote(api_key='{SHAPE_PW}')
'''
COMPOSE_YML = (
    "services:\n  db:\n    environment:\n"
    f"      DB_PASSWORD={SHAPE_PW}\n      password: {SHAPE_PW}\n"
    f'      secret_key: "{SHAPE_PW}Aa"\n      CACHE: redis://:{SHAPE_PW}@cache:6379/0\n'
    "      OK_PASSWORD: ${OK_PASSWORD}\n"
)
#: Longer than the grep line cut, with the secret straddling the cut.
LONG_LINE_PY = "x = 1  # " + "a" * 140 + f"; SECRET_KEY = '{LONG_SECRET}'\n"
#: `connect` carries a secret default in its signature, so a metadata-only reader could see one.
SIGNATURES = {"app/db.py::connect": f"def connect(password='{SHAPE_PW}')"}

ORDERS_PY = f'''\
"""Order services."""
from app.models import Cart

DATABASE_URL = "{DSN}"


class OrderService:
    """Creates and cancels orders."""

    def create_order(self, cart, user):
        """Create an order from a cart."""
        total = sum(i.price for i in cart.items)
        return {{"user": user, "total": total}}

    def cancel_order(self, order_id):
        return order_id


def create_order(cart, user):
    return OrderService().create_order(cart, user)


def long_report(rows):
    out = []
''' + "".join(f"    out.append(rows[{i}])\n" for i in range(120)) + "    return out\n"

FILES: dict[str, dict[str, str]] = {
    SHOP: {
        "app/services/orders.py": ORDERS_PY,
        "app/api/orders.py": (
            "from app.services.orders import create_order\n\n\n"
            "def post_order(req):\n    return create_order(req.cart, req.user)\n\n\n"
            "def user_summary(user):\n    return user\n"
        ),
        "tests/test_orders.py": (
            "from app.services.orders import create_order\n\n\n"
            "def test_create_order():\n    assert create_order(None, 'u')\n"
        ),
        "vendor/lib.py": "def create_order(x):\n    return x\n",
        "config/settings.yaml": "service: shop\nRETRY_LIMIT: 3\n",
        "secrets/loader.py": "def load_secret():\n    return None\n",
        ".env": f"DB_PASSWORD={CANARY_PW}\nAPI_TOKEN={CANARY_TOKEN}\nRETRY_LIMIT=9\n",
        "deploy/server.key": "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKcanary\n"
                             "-----END RSA PRIVATE KEY-----\n",
        "README.md": "# shop\nRETRY_LIMIT is documented here.\n",
        "app/db.py": DB_PY,
        "deploy/compose.yml": COMPOSE_YML,
        "app/long_line.py": LONG_LINE_PY,
    },
    BILLING: {
        "src/invoices.py": (
            "def create_invoice(order):\n    return {'order': order}\n\n\n"
            "def sync_orders():\n    return create_order_remote()\n"
        ),
        "src/client.py": "def create_order_remote():\n    return 'create_order(…)'\n",
    },
}

def _span(file: str, text: str, name: str, kind: str, nth: int = 0) -> tuple:
    """(file, name, kind, start, end) found by looking for the declaration, so a
    line number in a graph row is a line number of the committed text."""
    lines = text.split("\n")
    keys = {"class": f"class {name}", "variable": f"{name} =", "method": f"def {name}(self",
            "function": f"def {name}("}
    key = keys[kind]
    starts = [i for i, ln in enumerate(lines, 1)
              if ln.lstrip().startswith(key) and (kind == "method" or not ln.startswith(" "))]
    start = starts[nth]
    end = start
    indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
    for j in range(start + 1, len(lines) + 1):
        ln = lines[j - 1]
        if ln.strip() and (len(ln) - len(ln.lstrip())) <= indent:
            break
        if ln.strip():
            end = j
    return (file, name, kind, start, end)


def _syms(slug: str) -> list[tuple]:
    f = FILES[slug]
    o = "app/services/orders.py"
    if slug == SHOP:
        out = [
            _span(o, f[o], "OrderService", "class"),
            _span(o, f[o], "create_order", "method"),
            _span(o, f[o], "cancel_order", "method"),
            _span(o, f[o], "create_order", "function"),
            _span(o, f[o], "long_report", "function"),
            _span("app/api/orders.py", f["app/api/orders.py"], "post_order", "function"),
            _span("app/api/orders.py", f["app/api/orders.py"], "user_summary", "function"),
            _span("tests/test_orders.py", f["tests/test_orders.py"], "test_create_order", "function"),
            _span("vendor/lib.py", f["vendor/lib.py"], "create_order", "function"),
            _span("secrets/loader.py", f["secrets/loader.py"], "load_secret", "function"),
            (o, "DATABASE_URL", "variable", 4, 4),
            _span("app/db.py", f["app/db.py"], "connect", "function"),
        ]
        return out + [(o, f"user{i}", "variable", 4, 4) for i in range(25)]
    return [
        _span("src/invoices.py", f["src/invoices.py"], "create_invoice", "function"),
        _span("src/invoices.py", f["src/invoices.py"], "sync_orders", "function"),
        _span("src/client.py", f["src/client.py"], "create_order_remote", "function"),
    ]


EDGES: dict[str, list[tuple]] = {
    SHOP: [
        ("app/api/orders.py::post_order", "app/services/orders.py::create_order"),
        ("tests/test_orders.py::test_create_order", "app/services/orders.py::create_order"),
        ("app/services/orders.py::create_order", "app/services/orders.py::cancel_order"),
    ],
    BILLING: [("src/invoices.py::sync_orders", "src/client.py::create_order_remote")],
}


def _git(path: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@example.com", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.fixture(scope="session")
def world(tmp_path_factory):
    """(workspace_dir, {slug: head sha}). Settings point at it for the module."""
    import os

    from src.config import get_settings

    root = tmp_path_factory.mktemp("celmis-dev")
    prev = os.environ.get("WORKSPACE_DIR")
    os.environ["WORKSPACE_DIR"] = str(root)
    get_settings.cache_clear()
    settings = get_settings()
    shas: dict[str, str] = {}
    for slug, files in FILES.items():
        clone = settings.repo_path(slug)
        clone.mkdir(parents=True)
        _git(clone, "init", "-q", "-b", "develop")
        for rel, text in files.items():
            p = clone / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        _git(clone, "add", "-A", "-f")
        _git(clone, "commit", "-q", "-m", "init")
        shas[slug] = _git(clone, "rev-parse", "HEAD")
        db = settings.repo_graph_path(slug)
        db.parent.mkdir(parents=True, exist_ok=True)
        store = make_graph_store(db)
        try:
            rows = _syms(slug)
            # A method shares its name with a function in the same file, so its
            # id carries the start line; an edge names `file::name` and means
            # the function when there is one.
            by_name = {f"{f}::{n}": (f"{f}::{n}@{s}" if k == "method" else f"{f}::{n}")
                       for f, n, k, s, e in sorted(rows, key=lambda r: r[2] == "method")[::-1]}
            syms = [SymbolInfo(
                id=f"{f}::{n}@{s}" if k == "method" else f"{f}::{n}", name=n, kind=k, file=f, start_line=s, end_line=e,
                language="python", is_exported=not f.startswith(("tests/", "vendor/")),
                signature=SIGNATURES.get(f"{f}::{n}"))
                for f, n, k, s, e in rows]
            store.add_symbols_batch(syms)
            store.add_edges_batch([EdgeInfo(from_id=by_name[a], to_id=by_name[b], kind="CALLS",
                                            confidence="strong") for a, b in EDGES[slug]])
            from src.indexing.graph.pagerank import write_ranks
            write_ranks(store)
            store.commit()
        finally:
            store.close()
    yield root, shas
    if prev is None:
        os.environ.pop("WORKSPACE_DIR", None)
    else:
        os.environ["WORKSPACE_DIR"] = prev
    get_settings.cache_clear()


def _decision(slug: str, level: str, deny: tuple[str, ...] = ()):  # noqa: ANN202
    from src.access.resolver import RepoAccessDecision, _RuleView

    if level == "code" and not deny:
        return RepoAccessDecision.full(slug)
    return RepoAccessDecision(
        repo_slug=slug, visibility=level, open_default=False,
        rules=(_RuleView(level, (), deny, ()),), deny_globs=deny)


@pytest.fixture
def freshness(monkeypatch, world):
    """Control what `read_index_states` says. Returns a dict slug -> overrides."""
    from src.repos import index_state

    _root, shas = world
    overrides: dict[str, dict] = {s: {} for s in shas}

    def fake(slugs):
        out = {}
        now = datetime.now(UTC)
        for s in slugs:
            if s not in shas or overrides[s].get("missing"):
                continue
            o = overrides[s]
            out[s] = index_state.RepoIndexInfo(
                repo_slug=s, last_indexed_sha=o.get("sha", shas[s]),
                last_indexed_at=now - timedelta(hours=o.get("age_h", 2.5)),
                last_full_rebuild_at=None, last_indexed_files=0, last_error=None,
                last_error_at=None, last_checked_at=o.get("checked", now - timedelta(minutes=5)),
                last_remote_sha=o.get("remote", o.get("sha", shas[s])),
                last_check_error=None, indexed_branch=o.get("branch", "develop"))
        return out

    monkeypatch.setattr(index_state, "read_index_states", fake)
    return overrides


@pytest.fixture(scope="session")
def stores(world):
    """One open, shared graph per repository for the whole session: opening a
    FalkorDBLite graph and closing it again costs several seconds, which the
    tools' per-call open would pay in every test. The tools' own open/close
    behaviour is covered separately (test_graph_open_and_close)."""
    import src.config as cfg

    opened: dict[str, object] = {}

    def get(slug: str):  # noqa: ANN202
        if slug not in opened:
            opened[slug] = make_graph_store(cfg.get_settings().repo_graph_path(slug))
        return opened[slug]

    yield get
    for store in opened.values():
        store.close()


@pytest.fixture
def shared_graphs(monkeypatch, stores):
    from contextlib import contextmanager

    from src.mcp_server.dev_profile import common

    @contextmanager
    def fake_open(slug: str):
        yield stores(slug)

    monkeypatch.setattr(common, "open_store", fake_open)
    return stores


@pytest.fixture
def as_caller(monkeypatch, world, freshness, shared_graphs):
    """`as_caller(shop="code", billing="metadata", deny={...})` — who reads what."""
    from src.mcp_server.dev_profile import access

    def apply(levels: dict[str, str] | None = None, deny: dict[str, tuple[str, ...]] | None = None):
        levels = levels or {SHOP: "code", BILLING: "code"}
        deny = deny or {}
        decisions = {s: _decision(s, lvl, deny.get(s, ()))
                     for s, lvl in levels.items() if lvl != "none"}
        monkeypatch.setattr(
            access, "mcp_scope",
            lambda candidates=None: access.DevScope(
                decisions=dict(decisions), user_id="u-1", workspace_id="ws-1"))

    apply()
    return apply


@pytest.fixture(autouse=True)
def _howto_freshness_provider_is_restored(monkeypatch):
    """Building the dev server installs the profile's freshness into `howto`
    (process-global); give every test its own copy so nothing leaks out."""
    from src.mcp_server.howto import engine

    monkeypatch.setattr(engine, "_idx_provider", engine._idx_provider)
