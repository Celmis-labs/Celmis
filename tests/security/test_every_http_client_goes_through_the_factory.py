"""One door out, and a list of the doors that are still cut into the wall.

The measurement that started this: 27 places in `src/` wrote
``httpx.Client(...)`` by hand, two called the egress factory, and one of those
two was the local-embeddings probe — the single request that never leaves the
customer's network anyway. The allowlist in src/security/egress.py was real
code guarding almost nothing, while the docs read as though it guarded
everything. That gap is what this file closes: not by pretending the
conversion is done, but by making the remaining work countable.

Two invariants, and they fail in opposite directions on purpose:

  * a module that builds its own client and is NOT in
    :data:`src.deployment.UNGUARDED_HTTP_SITES` fails — that is a new hole,
  * a module that IS listed and no longer builds one fails too — that is a
    stale exemption, and a work list nobody removes entries from is a
    dumping ground.

The scan reads AST nodes, never source text. A string literal or a comment
containing ``httpx.Client(`` — including the ones in this file's own
docstrings — is invisible to it, which is the property that makes the guard
trustworthy rather than grep-shaped.

Two shapes count as building a client. One is constructing ``httpx.Client`` or
``httpx.AsyncClient``; both now have a factory to go through, so neither is
ever the only way to make a request. The other is calling a module-level verb
— ``httpx.get(...)`` and friends — which constructs an ephemeral unguarded
client inside httpx and discards it per call. The first version of this scan
knew only the class names; see :data:`_MODULE_VERBS` for what that missed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src import deployment
from src.deployment import UNGUARDED_HTTP_SITES

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

#: The only two files that may construct a client without being listed:
#: src/security/egress.py owns the whitelist transports and src/http.py is the
#: factory every other module is supposed to call.
FACTORY_FILES = frozenset({"src/http.py", "src/security/egress.py"})

#: Both shapes, and the async one is not a precaution any more: egress.py now
#: holds an AsyncWhitelistTransport and http.py a `build_async_client`, so a
#: bare `httpx.AsyncClient(...)` at a call site is a hole with a door standing
#: open next to it. `src/` still has no async caller — the exemption is by
#: path, so the factory's own AsyncClient is fine and every other module's is
#: not, whichever of the two shapes it uses.
_CLIENT_ATTRS = frozenset({"Client", "AsyncClient"})

#: The module-level verbs. ``httpx.get(url)`` reads as though it uses some
#: ambient client, but httpx builds a brand-new Client inside the call —
#: no transport, no allowlist — and throws it away. While the scan knew only
#: the two class names above, 28 of these calls sat in six modules with no
#: entry on the work list, so the day the registry emptied, strict mode would
#: have booted on an assertion that was false rather than merely incomplete.
#: ``stream`` is here because ``httpx.stream()`` is the same ephemeral client
#: wearing a context manager.
_MODULE_VERBS = frozenset({
    "get", "post", "put", "patch", "delete", "head", "options", "request",
    "stream",
})

#: Everything the scan treats as "this line makes a client".
_CLIENT_MAKERS = _CLIENT_ATTRS | _MODULE_VERBS


# ─── the scan ────────────────────────────────────────────────────────


def _names_bound_to_httpx(tree: ast.AST) -> tuple[set[str], set[str]]:
    """(names bound to the httpx module, names bound to its client-makers).

    ``import httpx as hx``, ``from httpx import Client as C`` and
    ``from httpx import get as fetch`` all have to be caught, or the guard is
    a spelling test.
    """
    modules: set[str] = set()
    makers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "httpx":
                    modules.add(alias.asname or "httpx")
        elif isinstance(node, ast.ImportFrom) and node.module == "httpx":
            for alias in node.names:
                if alias.name in _CLIENT_MAKERS:
                    makers.add(alias.asname or alias.name)
    return modules, makers


def raw_client_lines(source: str, filename: str = "<test>") -> list[int]:
    """Line numbers where ``source`` constructs an httpx client directly."""
    tree = ast.parse(source, filename=filename)
    modules, makers = _names_bound_to_httpx(tree)
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if (
            isinstance(fn, ast.Attribute)
            and isinstance(fn.value, ast.Name)
            and fn.value.id in modules
            and fn.attr in _CLIENT_MAKERS
        ) or (isinstance(fn, ast.Name) and fn.id in makers):
            hits.append(node.lineno)
    return sorted(hits)


def scan(directory: Path, *, base: Path, exempt: frozenset[str] = FACTORY_FILES) -> dict[str, list[int]]:
    """Every module under ``directory`` that builds its own client.

    Keys are paths relative to ``base``, so the real scan and a synthetic tree
    in tmp_path produce the same shape of answer.
    """
    found: dict[str, list[int]] = {}
    for path in sorted(directory.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(base).as_posix()
        if rel in exempt:
            continue
        lines = raw_client_lines(path.read_text(encoding="utf-8"), rel)
        if lines:
            found[rel] = lines
    return found


# ─── the invariant ───────────────────────────────────────────────────


def test_the_work_list_matches_the_tree_exactly():
    """UNGUARDED_HTTP_SITES is the registry an operator's strict-mode boot
    consults. If it drifts from reality, strict mode either refuses over work
    that is done or permits a hole that is open."""
    found = scan(SRC, base=ROOT)
    unlisted = sorted(set(found) - set(UNGUARDED_HTTP_SITES))
    stale = sorted(set(UNGUARDED_HTTP_SITES) - set(found))
    assert not unlisted, (
        "these modules build an httpx client directly and are not in "
        "src/deployment.py:UNGUARDED_HTTP_SITES — route them through "
        f"src.http.build_client, or add them WITH A REASON: {unlisted}"
    )
    assert not stale, (
        "these are listed as unconverted but no longer build a client — "
        f"delete the entries, the list only means something while it shrinks: {stale}"
    )


def test_every_entry_carries_a_reason_and_a_real_file():
    for path, why in UNGUARDED_HTTP_SITES.items():
        assert (ROOT / path).is_file(), f"{path} does not exist"
        assert len(why.strip()) >= 20, f"{path} has no usable reason: {why!r}"


def test_the_exempt_files_hold_exactly_the_two_guarded_clients():
    """What the exemption is FOR, stated as a count.

    src/security/egress.py owns every ``httpx`` client construction in this
    codebase, and there are exactly two: the ``httpx.Client`` behind
    ``build_http_client`` and the ``httpx.AsyncClient`` behind
    ``build_async_http_client``. Two rather than one because httpx dispatches
    the two flavours through different transport methods; two rather than
    three because a third would be a policy nobody is watching.

    src/http.py constructs nothing at all — it fills the allowlist in from
    Settings and delegates, for both flavours. If this file ever starts
    building its own, there are two places that decide what a guarded client
    is, and they will not stay the same.
    """
    lines = {rel: raw_client_lines((ROOT / rel).read_text(encoding="utf-8"), rel)
             for rel in FACTORY_FILES}
    assert len(lines["src/security/egress.py"]) == 2, (
        "src/security/egress.py should build exactly the sync and the async "
        f"guarded client and nothing else, found {lines['src/security/egress.py']}"
    )
    assert lines["src/http.py"] == [], (
        "src/http.py is supposed to DELEGATE to src.security.egress rather "
        "than construct its own client — two constructors means two policies"
    )


def test_the_llm_gateway_is_converted():
    """The gateway is the chat and review path. It was a bare client, which is
    why 'the allowlist protects the LLM calls' was untrue. Named separately so
    a regression here reads as itself and not as a list diff."""
    assert "src/llm/gateway.py" not in UNGUARDED_HTTP_SITES
    assert "src/llm/gateway.py" not in scan(SRC, base=ROOT)


# ─── the scanner is not a grep ───────────────────────────────────────


def _tree(tmp_path: Path, rel: str, source: str) -> Path:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    return tmp_path / "src"


def test_a_new_raw_client_is_caught(tmp_path):
    """The proof that the guard bites: add one, the scan reports it."""
    src = _tree(tmp_path, "src/feature.py", "import httpx\n\nc = httpx.Client(timeout=5.0)\n")
    assert scan(src, base=tmp_path) == {"src/feature.py": [3]}


def test_a_new_raw_async_client_is_caught(tmp_path):
    """The async half of the same proof. There is a `build_async_client` now,
    so an ``httpx.AsyncClient(...)`` at a call site is a choice rather than an
    absence, and it has to fail exactly the way the sync one does."""
    src = _tree(
        tmp_path, "src/feature.py",
        "import httpx\n\nc = httpx.AsyncClient(timeout=5.0)\n",
    )
    assert scan(src, base=tmp_path) == {"src/feature.py": [3]}


def test_an_unlisted_file_falls_out_of_the_invariant(tmp_path):
    """The scanner finding it is half the guard; the comparison above has to
    turn that into a failure. Same expression, run against a tree where the
    answer is known."""
    src = _tree(tmp_path, "src/brand_new.py", "import httpx\nc = httpx.Client()\n")
    found = scan(src, base=tmp_path)
    assert sorted(set(found) - set(UNGUARDED_HTTP_SITES)) == ["src/brand_new.py"]


def test_an_unlisted_async_client_falls_out_of_the_invariant_too(tmp_path):
    """Same comparison as the sync case, so the two shapes cannot be one
    guarded and one merely scanned for."""
    src = _tree(tmp_path, "src/brand_new.py", "import httpx\nc = httpx.AsyncClient()\n")
    found = scan(src, base=tmp_path)
    assert sorted(set(found) - set(UNGUARDED_HTTP_SITES)) == ["src/brand_new.py"]


def test_an_aliased_import_is_caught(tmp_path):
    src = _tree(
        tmp_path, "src/feature.py",
        "import httpx as hx\nfrom httpx import AsyncClient as AC\n\n"
        "a = hx.Client()\nb = AC()\n",
    )
    assert scan(src, base=tmp_path) == {"src/feature.py": [4, 5]}


def test_a_mention_in_a_comment_or_a_string_is_not_a_client(tmp_path):
    """The recurring trap in this repo: a test that greps for a token finds it
    in the comment explaining its absence. AST nodes cannot be fooled that
    way, and this proves it rather than asserting it."""
    src = _tree(
        tmp_path, "src/feature.py",
        '"""Never write httpx.Client( here — call src.http.build_client."""\n'
        "import httpx\n\n"
        "# httpx.Client(timeout=5) would bypass the allowlist\n"
        'BANNED = "httpx.Client("\n'
        "client = httpx.Client\n",  # a reference, not a construction
    )
    assert scan(src, base=tmp_path) == {}


def test_a_module_level_verb_is_caught(tmp_path):
    """httpx.get() looks client-less, and that is the trap: httpx builds a
    brand-new unguarded Client inside the call. The first guard knew only the
    class names, so 28 of these sat invisible across six modules."""
    src = _tree(
        tmp_path, "src/feature.py",
        "import httpx\n\n"
        "r = httpx.get('https://api.github.com/user')\n"
        "with httpx.stream('GET', 'https://example.com') as resp:\n"
        "    pass\n",
    )
    assert scan(src, base=tmp_path) == {"src/feature.py": [3, 4]}


def test_an_aliased_verb_is_caught(tmp_path):
    src = _tree(
        tmp_path, "src/feature.py",
        "import httpx as hx\nfrom httpx import post as send\n\n"
        "a = hx.get('https://x')\nb = send('https://x')\n",
    )
    assert scan(src, base=tmp_path) == {"src/feature.py": [4, 5]}


def test_a_verb_on_anything_but_the_httpx_module_is_not_a_hit(tmp_path):
    """The verbs are ordinary method names on the guarded client itself —
    every CONVERTED call site says client.get(...). Flagging those would make
    the guard cry wolf over exactly the code it exists to get written."""
    src = _tree(
        tmp_path, "src/feature.py",
        "from src.http import build_client\n\n"
        "client = build_client()\n"
        "client.get('https://x')\n"
        "client.stream('GET', 'https://x')\n",
    )
    assert scan(src, base=tmp_path) == {}


@pytest.mark.parametrize("shape", ["Client", "AsyncClient"])
def test_the_factory_files_are_skipped_by_path(tmp_path, shape):
    """Both flavours, because the exemption is what lets egress.py build the
    async client at all — a scan that exempted only the sync shape would fail
    on the factory it exists to protect."""
    src = _tree(tmp_path, "src/http.py", f"import httpx\n\nc = httpx.{shape}()\n")
    assert scan(src, base=tmp_path) == {}


# ─── the mode ────────────────────────────────────────────────────────


@pytest.fixture()
def egress_mode(monkeypatch):
    """Set CELMIS_EGRESS_MODE for one test, cache reset both ways."""
    def _set(value: str | None):
        if value is None:
            monkeypatch.delenv(deployment.EGRESS_ENV_VAR, raising=False)
        else:
            monkeypatch.setenv(deployment.EGRESS_ENV_VAR, value)
        deployment.reset_mode_cache()
        return deployment.get_egress_mode()
    yield _set
    monkeypatch.undo()
    deployment.reset_mode_cache()


@pytest.fixture()
def no_db(monkeypatch):
    """Startup checks count workspaces; these tests are about egress and must
    not wait on a database that may not be running."""
    monkeypatch.setattr(deployment, "count_workspaces", lambda: None)


def test_the_default_is_permissive(egress_mode):
    """An upgrade must not turn this on for anyone: the registry still names
    the tenant-destination sites (MCP sources, notification webhooks), so a
    strict default would refuse to boot on an install that worked
    yesterday."""
    assert egress_mode(None) is deployment.EgressMode.PERMISSIVE
    assert deployment.is_strict_egress() is False


def test_permissive_startup_is_unchanged(egress_mode, no_db):
    egress_mode(None)
    report = deployment.run_startup_checks(strict_secret=False)
    assert report["egress_mode"] == "permissive"
    assert report["unguarded_http_sites"] == len(UNGUARDED_HTTP_SITES)
    assert "egress_caveat" not in report


def test_strict_refuses_at_startup_while_a_raw_client_remains(egress_mode, no_db):
    egress_mode("strict")
    assert UNGUARDED_HTTP_SITES, "this test is meaningless once the list empties"
    with pytest.raises(deployment.EgressModeError) as exc:
        deployment.run_startup_checks(strict_secret=False)
    message = str(exc.value)
    # The refusal has to be actionable: which files, and what the operator
    # can do about it right now. The named file used to be a sync client;
    # since the conversion wave it is the MCP evidence collector, whose
    # destination is tenant data and so cannot move behind the allowlist.
    assert "src/mcp_client/registry.py" in message
    assert "src.http.build_client" in message


def test_strict_starts_once_the_list_is_empty(egress_mode, no_db, monkeypatch):
    """The check is about the registry's contents, not a permanent 'no'."""
    monkeypatch.setattr(deployment, "UNGUARDED_HTTP_SITES", {})
    egress_mode("strict")
    report = deployment.run_startup_checks(strict_secret=False)
    assert report["egress_mode"] == "strict"
    assert report["unguarded_http_sites"] == 0


def test_strict_says_what_it_cannot_promise(egress_mode, no_db, monkeypatch, caplog):
    """An operator who turns strict on is about to repeat the promise to a
    customer. The SDKs it does not cover are named at every start, not in a
    commit message."""
    monkeypatch.setattr(deployment, "UNGUARDED_HTTP_SITES", {})
    egress_mode("strict")
    with caplog.at_level("WARNING", logger="src.deployment"):
        report = deployment.run_startup_checks(strict_secret=False)
    caveat = str(report["egress_caveat"])
    for named in ("google-genai", "litellm", "firewall"):
        assert named in caveat
    assert caveat in caplog.text


def test_on_and_off_are_not_spellings_of_either_mode(egress_mode):
    """`CELMIS_EGRESS_MODE=off` reads as "no egress" to one operator and
    "guard disabled" to the next. The one who guesses wrong must get a
    refusal rather than the permissive side by accident."""
    for ambiguous in ("off", "on", "true", "1"):
        with pytest.raises(deployment.EgressModeError):
            egress_mode(ambiguous)


def test_a_misspelt_egress_mode_is_not_the_permissive_one(egress_mode):
    """Same rule as the tenancy switch: a typo in a security setting must not
    resolve to the side that allows more."""
    with pytest.raises(deployment.EgressModeError):
        egress_mode("strcit")
