"""Route extraction: framework syntax found at the indexed revision."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from src.indexing.routes import CANDIDATE_REGEX, extract_routes


@dataclass
class _Hit:
    path: str
    line: int
    text: str


def _hits(files: dict[str, str]):
    def grep(_pattern: str) -> list[_Hit]:
        out = []
        for path, text in files.items():
            for i, ln in enumerate(text.split("\n"), 1):
                out.append(_Hit(path, i, ln))
        return out

    return grep


def _extract(files: dict[str, str], **kw):
    return extract_routes(_hits(files), lambda p: files[p].split("\n"), **kw)


FASTAPI = '''\
from fastapi import APIRouter
router = APIRouter()


@router.get("/orders/{order_id}")
async def read_order(order_id: int):
    return {}


@router.post(
    "/orders",
)
def create_order_endpoint(body):
    return body
'''


def test_fastapi_decorators_give_method_path_and_the_function_below():
    got = {(r.method, r.path, r.handler) for r in _extract({"app/api.py": FASTAPI})}
    assert got == {
        ("GET", "/orders/{order_id}", "read_order"),
        ("POST", "/orders", "create_order_endpoint"),
    }


def test_a_flask_route_lists_every_method_it_declares():
    src = '@app.route("/ping", methods=["GET", "POST"])\ndef ping():\n    return "ok"\n'
    got = {(r.method, r.path, r.handler) for r in _extract({"svc/app.py": src})}
    assert got == {("GET", "/ping", "ping"), ("POST", "/ping", "ping")}


def test_a_flask_route_with_no_methods_is_a_get():
    src = '@app.route("/health")\ndef health():\n    return "ok"\n'
    assert [(r.method, r.path) for r in _extract({"a.py": src})] == [("GET", "/health")]


def test_express_calls_give_the_path_and_the_named_handler():
    src = "router.get('/users/:id', getUser);\napp.post('/users', async (req, res) => {\n});\n"
    got = {(r.method, r.path, r.handler) for r in _extract({"src/routes.js": src})}
    assert got == {("GET", "/users/:id", "getUser"), ("POST", "/users", "(inline)")}


def test_laravel_routes_name_the_controller_method():
    src = "Route::get('/invoices', [InvoiceController::class, 'index']);\n"
    (r,) = _extract({"routes/web.php": src})
    assert (r.method, r.path, r.handler, r.framework) == (
        "GET", "/invoices", "InvoiceController::index", "laravel")


def test_a_symfony_route_attribute_names_the_method_below_it():
    src = '#[Route("/api/items", name: "items")]\npublic function items(): Response\n{\n}\n'
    (r,) = _extract({"src/Controller/ItemController.php": src})
    assert (r.path, r.handler, r.framework) == ("/api/items", "items", "symfony")


def test_go_standard_and_gin_registrations_are_found():
    src = 'http.HandleFunc("/healthz", healthHandler)\nr.GET("/v1/items", listItems)\n'
    got = {(r.method, r.path, r.handler) for r in _extract({"main.go": src})}
    assert got == {("ANY", "/healthz", "healthHandler"), ("GET", "/v1/items", "listItems")}


def test_a_decorator_in_a_markdown_file_is_not_a_route():
    assert _extract({"README.md": '@app.get("/x")\n'}) == []


def test_files_the_caller_may_not_see_contribute_no_routes():
    files = {"a.py": '@app.get("/a")\ndef a():\n    pass\n',
             "secret/b.py": '@app.get("/b")\ndef b():\n    pass\n'}
    routes = _extract(files, path_filter=lambda p: not p.startswith("secret/"))
    assert [r.path for r in routes] == ["/a"]


def test_the_same_registration_is_reported_once_and_the_list_is_capped():
    src = "".join(f"router.get('/r{i}', h{i});\n" for i in range(50))
    assert len(_extract({"r.js": src}, max_routes=10)) == 10


def test_a_line_that_only_looks_like_a_route_is_ignored():
    src = "map.get('/not-a-route')\nconst s = \"router.get\";\n"
    assert _extract({"a.js": src}) == []


def test_the_candidate_regex_is_a_valid_posix_extended_expression_for_git(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "a.py").write_text('@app.get("/x")\ndef x():\n    pass\n')
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t", *a],
        capture_output=True, text=True, check=False)
    run("init", "-q")
    run("add", "-A")
    run("-c", "commit.gpgsign=false", "commit", "-q", "-m", "i")
    found = run("grep", "-nE", CANDIDATE_REGEX, "HEAD")
    assert found.returncode == 0 and "a.py" in found.stdout


def test_api_surface_of_a_real_repository_reads_the_indexed_revision(as_caller, world):
    """End to end through the `/mcp` tool implementation."""
    import subprocess as sp

    from src.config import get_settings
    from tests.mcp_server.dev.conftest import SHOP, _git

    clone = get_settings().repo_path(SHOP)
    (clone / "app" / "api").mkdir(parents=True, exist_ok=True)
    f = clone / "app" / "api" / "routes.py"
    f.write_text('@router.get("/orders")\ndef list_orders():\n    return []\n')
    _git(clone, "add", "-f", "app/api/routes.py")
    _git(clone, "commit", "-q", "-m", "routes")
    new_sha = _git(clone, "rev-parse", "HEAD")
    try:
        from src.mcp_server import http_app
        from src.mcp_server.dev_profile import freshness as fr

        orig = fr.read_freshness

        def pinned(slugs):
            out = orig(slugs)
            return {s: fr.RepoFresh(v.slug, v.branch, new_sha, v.age, v.state) for s, v in out.items()}

        import pytest

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(fr, "read_freshness", pinned)
            out = http_app._get_api_surface_impl(SHOP, None)
    finally:
        sp.run(["git", "-C", str(clone), "reset", "-q", "--hard", "HEAD~1"], check=False)
    assert {(e["method"], e["path"], e["handler"]) for e in out["endpoints"]} == {
        ("GET", "/orders", "list_orders")}
