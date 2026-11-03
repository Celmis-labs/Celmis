"""The routes a repository exposes, read through the real language adapters."""

from __future__ import annotations

from pathlib import Path

from src.indexing.graph.languages.go import GoExtractor
from src.indexing.graph.languages.java import JavaExtractor
from src.indexing.graph.languages.python import PythonExtractor
from src.indexing.graph.languages.typescript import TypeScriptExtractor


def _routes(extractor, rel: str, source: str) -> set[tuple[str, str]]:
    res = extractor.extract(Path(rel), source=source.encode())
    assert res.facts is not None
    return {(r.method, r.path) for r in res.facts.routes}


def test_fastapi_decorators_with_the_router_prefix() -> None:
    src = '''
from fastapi import APIRouter
router = APIRouter(prefix="/v1")

@router.post("/invoices/{invoice_id}/pay")
async def pay(invoice_id: int): ...

@router.get("/invoices/{invoice_id:path}")
def get(invoice_id): ...
'''
    assert _routes(PythonExtractor(), "app/api.py", src) == {
        ("POST", "/v1/invoices/{}/pay"), ("GET", "/v1/invoices/{**}")}


def test_flask_route_with_methods_and_converters() -> None:
    src = '''
@bp.route("/items/<int:item_id>", methods=["PUT", "DELETE"])
def change(item_id): ...

@bp.route("/items")
def listing(): ...
'''
    assert _routes(PythonExtractor(), "app.py", src) == {
        ("PUT", "/items/{}"), ("DELETE", "/items/{}"), ("GET", "/items")}


def test_starlette_route_objects_and_add_route() -> None:
    src = '''
routes = [Route("/health", health), Route("/orders/{oid}", create, methods=["POST"])]
app.add_route("/ping", ping)
'''
    assert _routes(PythonExtractor(), "app.py", src) == {
        ("GET", "/health"), ("POST", "/orders/{}"), ("GET", "/ping")}


def test_a_cache_get_with_a_key_is_not_a_route() -> None:
    src = '''
@cache.get("not-a-path")
def f(): ...

value = cache.get("/also-no-decorator")
'''
    assert _routes(PythonExtractor(), "x.py", src) == set()


def test_express_routes_and_route_chain() -> None:
    src = '''
const app = express();
app.get("/users/:id", (req, res) => {});
router.post("/users", createUser);
router.route("/orders/:oid").put(update);
api.get("/users/1", options);
axios.get("/users/2", cfg);
'''
    got = _routes(TypeScriptExtractor(), "server.ts", src)
    assert got == {("GET", "/users/{}"), ("POST", "/users"), ("PUT", "/orders/{}")}


def test_next_app_router_route_file() -> None:
    src = '''
export async function GET(req) {}
export const POST = async (req) => {};
export function helper() {}
'''
    got = _routes(TypeScriptExtractor(), "web/app/api/(admin)/invoices/[id]/route.ts", src)
    assert got == {("GET", "/api/invoices/{}"), ("POST", "/api/invoices/{}")}


def test_next_pages_api_is_any_method() -> None:
    got = _routes(TypeScriptExtractor(), "pages/api/status/[...slug].ts",
                  "export default function handler(req, res) {}")
    assert got == {("*", "/api/status/{**}")}


def test_go_net_http_gin_group_and_chi_nesting() -> None:
    src = '''package main
func main() {
  http.HandleFunc("POST /orders/{id}", h)
  mux.Handle("/static/", fs)
  g := r.Group("/v1")
  g.GET("/users/:id", h)
  r.Route("/admin", func(r chi.Router) {
    r.Get("/stats", h)
    r.Post("/reset", h)
  })
}
'''
    assert _routes(GoExtractor(), "main.go", src) == {
        ("POST", "/orders/{}"), ("*", "/static"), ("GET", "/v1/users/{}"),
        ("GET", "/admin/stats"), ("POST", "/admin/reset")}


def test_spring_mappings_with_class_prefix() -> None:
    src = '''
@RestController
@RequestMapping("/api/billing")
public class InvoiceController {
  @PostMapping("/invoices/{id}/pay")
  public void pay(@PathVariable long id) {}
  @RequestMapping(value = "/ping", method = RequestMethod.GET)
  public void ping() {}
  @DeleteMapping
  public void clear() {}
}
'''
    assert _routes(JavaExtractor(), "InvoiceController.java", src) == {
        ("POST", "/api/billing/invoices/{}/pay"), ("GET", "/api/billing/ping"),
        ("DELETE", "/api/billing")}
