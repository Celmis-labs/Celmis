"""Who consumes a route or a symbol, across the repositories of one workspace."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.groups import impact
from src.indexing.graph.xrepo import (
    CallFact,
    FactSet,
    ImportFact,
    PublishedPackage,
    RouteFact,
    ServiceFact,
)
from src.mcp_server import tenancy, tools

OWNER = FactSet(
    routes=[RouteFact("POST", "/invoices/{}/pay", "app/main.py", 10, "pay", "fastapi")],
    published=[PublishedPackage("pypi", "acme-billing-client", "pyproject.toml",
                                ["acme_billing_client"])],
)


def _web(host: str = "billing-service", path: str = "/invoices/{}/pay") -> FactSet:
    return FactSet(
        calls=[CallFact("POST", path, "shop/checkout.py", 11, host, "", "httpx", "checkout")],
        imports=[ImportFact("acme-billing-client", ["pay_invoice"], "shop/checkout.py", 6, "pypi",
                            [])],
    )


def test_a_call_to_the_owning_host_is_a_strong_consumer() -> None:
    got = impact.route_consumers(
        owner_slug="acme-billing-service", owner_facts=OWNER, method="POST",
        path="/invoices/{id}/pay", others={"acme-web-shop": _web()})
    assert [(c.repo_slug, c.file, c.line, c.kind, c.strong) for c in got] == [
        ("acme-web-shop", "shop/checkout.py", 11, "http", True)]


def test_a_matching_path_to_an_unknown_host_is_only_weak() -> None:
    got = impact.route_consumers(
        owner_slug="acme-billing-service", owner_facts=OWNER, method="POST",
        path="/invoices/{id}/pay", others={"acme-web-shop": _web(host="")})
    assert [c.strong for c in got] == [False]


def test_a_call_to_another_repository_of_the_workspace_is_not_a_consumer() -> None:
    others = {"acme-web-shop": _web(host="inventory"), "acme-inventory": FactSet()}
    got = impact.route_consumers(
        owner_slug="acme-billing-service", owner_facts=OWNER, method="POST",
        path="/invoices/{id}/pay", others=others,
        host_map={"inventory": "acme-inventory"})
    assert got == []


def test_a_wrong_method_or_path_is_not_a_consumer() -> None:
    for method, path in (("GET", "/invoices/{id}/pay"), ("POST", "/invoices/{id}/refund")):
        assert impact.route_consumers(
            owner_slug="acme-billing-service", owner_facts=OWNER, method=method, path=path,
            others={"acme-web-shop": _web()}) == []


def test_the_configured_host_map_makes_a_foreign_host_strong() -> None:
    got = impact.route_consumers(
        owner_slug="acme-billing-service", owner_facts=OWNER, method="POST",
        path="/invoices/{id}/pay", others={"acme-web-shop": _web(host="api.example.com")},
        host_map={"api.example.com": "acme-billing-service"})
    assert [c.strong for c in got] == [True]


def test_the_compose_service_of_the_owner_makes_the_host_strong() -> None:
    owner = FactSet(routes=OWNER.routes, published=OWNER.published,
                    services=[ServiceFact("billing", "", "", "docker-compose.yml")])
    got = impact.route_consumers(
        owner_slug="acme-billing-service", owner_facts=owner, method="POST",
        path="/invoices/{id}/pay", others={"acme-web-shop": _web(host="billing")})
    assert [c.strong for c in got] == [True]


def test_an_import_of_the_published_package_plus_a_call_is_strong() -> None:
    web = FactSet(imports=[ImportFact("acme-billing-client", ["pay_invoice"], "shop/checkout.py",
                                      6, "pypi", [])])
    # an import with no call of the name is weaker than one that calls it
    got = impact.symbol_consumers(
        owner_facts=OWNER, file="acme_billing_client/client.py", names=["pay_invoice"],
        others={"acme-web-shop": web})
    assert [(c.repo_slug, c.line, c.kind) for c in got] == [("acme-web-shop", 6, "import")]


def test_a_file_that_no_package_publishes_has_no_symbol_consumers() -> None:
    assert impact.symbol_consumers(
        owner_facts=OWNER, file="scripts/one_off.py", names=["x"],
        others={"acme-web-shop": _web()}) == []
    assert impact.symbol_consumers(
        owner_facts=None, file="a.py", names=["x"], others={"acme-web-shop": _web()}) == []


# ─── tenancy ─────────────────────────────────────────────────────


@pytest.fixture
def disk(monkeypatch):
    loaded: list[str] = []
    monkeypatch.setattr(tools, "list_repo_slugs",
                        lambda: ["acme-billing-service", "acme-web-shop", "acme-old", "rival-app"])
    monkeypatch.setattr(tenancy, "workspace_slugs",
                        lambda ws: {"acme-billing-service", "acme-web-shop", "acme-old"}
                        if ws == "ws-acme" else set())
    facts = {"acme-web-shop": _web(), "rival-app": _web(), "acme-old": None}

    def load(slug):  # noqa: ANN001, ANN202
        loaded.append(slug)
        return facts[slug]

    return loaded, load


def _mode(monkeypatch, mode: str) -> None:
    from src.deployment import reset_mode_cache

    monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", mode)
    reset_mode_cache()


@pytest.fixture(autouse=True)
def _reset_mode():
    from src.deployment import reset_mode_cache

    yield
    reset_mode_cache()


def test_under_multi_tenant_only_the_workspace_is_consulted(disk, monkeypatch) -> None:
    _mode(monkeypatch, "multi_tenant")
    loaded, load = disk
    got = impact.consumers_of_route(
        workspace_id="ws-acme", owner_slug="acme-billing-service", method="POST",
        path="/invoices/{id}/pay", owner_facts=OWNER, load=load)
    assert sorted(loaded) == ["acme-old", "acme-web-shop"]
    assert "rival-app" not in loaded
    assert {c.repo_slug for c in got.consumers} == {"acme-web-shop"}
    assert got.checked == ["acme-web-shop"] and got.unknown == ["acme-old"]


def test_an_unknown_workspace_consults_nothing(disk, monkeypatch) -> None:
    _mode(monkeypatch, "multi_tenant")
    loaded, load = disk
    for ws in ("", None, "ws-nobody"):
        got = impact.consumers_of_route(
            workspace_id=ws, owner_slug="acme-billing-service", method="POST",
            path="/invoices/{id}/pay", owner_facts=OWNER, load=load)
        assert got.consumers == [] and got.checked == []
    assert loaded == []


def test_a_repository_without_facts_is_unknown_not_silent(disk, monkeypatch) -> None:
    _mode(monkeypatch, "multi_tenant")
    _, load = disk
    got = impact.consumers_of_symbols(
        workspace_id="ws-acme", owner_slug="acme-billing-service",
        file="acme_billing_client/client.py", names=["pay_invoice"], owner_facts=OWNER, load=load)
    assert got.unknown == ["acme-old"]


def test_an_unreadable_registry_consults_nothing(monkeypatch) -> None:
    _mode(monkeypatch, "multi_tenant")

    def broken():  # noqa: ANN202
        raise RuntimeError("down")

    monkeypatch.setattr(tools, "list_repo_slugs", broken)
    assert impact.workspace_facts("ws-acme", "x", load=lambda s: FactSet()) == ({}, [])


def test_under_single_tenant_every_other_repository_is_consulted(disk, monkeypatch) -> None:
    _mode(monkeypatch, "single_tenant")
    loaded, load = disk
    impact.workspace_facts("ws-acme", "acme-billing-service", load=load)
    assert sorted(loaded) == ["acme-old", "acme-web-shop", "rival-app"]


def _ns(**kw):  # noqa: ANN202
    return SimpleNamespace(**kw)


# ─── review findings: import links need the module, hole-only paths are weak ───


def _owner_with(file_exports=None):  # noqa: ANN202
    from src.indexing.graph.xrepo import ExportFact

    return FactSet(
        published=[PublishedPackage("pypi", "acme-billing", "pyproject.toml", ["acme_billing"])],
        exports=file_exports or [ExportFact("acme_billing/__init__.py", ".api", ["pay"])])


def _importer(module: str, names: list[str], called: str = "pay") -> FactSet:
    from src.indexing.graph.xrepo import ImportCall

    return FactSet(imports=[ImportFact(module, names, "shop/a.py", 3, "pypi",
                                       [ImportCall(called, 9)])])


def test_an_import_of_a_sibling_module_is_not_a_strong_consumer_of_the_changed_one() -> None:
    got = impact.symbol_consumers(
        owner_facts=_owner_with(), file="acme_billing/legacy.py", names=["pay"],
        others={"shop": _importer("acme_billing.api", ["pay"])})
    assert got and not any(c.strong for c in got)


def test_an_import_of_the_defining_module_is_strong() -> None:
    got = impact.symbol_consumers(
        owner_facts=_owner_with(), file="acme_billing/legacy.py", names=["pay"],
        others={"shop": _importer("acme_billing.legacy", ["pay"])})
    assert [c.strong for c in got] == [True]
    got = impact.symbol_consumers(
        owner_facts=_owner_with(), file="acme_billing/legacy.py", names=["pay"],
        others={"shop": _importer("acme_billing", ["legacy"])})
    assert any(c.strong for c in got)


def test_a_package_import_is_strong_only_when_its_init_reexports_the_module() -> None:
    from src.indexing.graph.xrepo import ExportFact

    imp = {"shop": _importer("acme_billing", ["pay"])}
    with_export = _owner_with([ExportFact("acme_billing/__init__.py", ".api", ["pay"])])
    assert [c.strong for c in impact.symbol_consumers(
        owner_facts=with_export, file="acme_billing/api.py", names=["pay"], others=imp)] == [True]
    # re-exported from a different module, or no export facts at all: weak
    assert not any(c.strong for c in impact.symbol_consumers(
        owner_facts=with_export, file="acme_billing/legacy.py", names=["pay"], others=imp))
    assert not any(c.strong for c in impact.symbol_consumers(
        owner_facts=FactSet(published=with_export.published), file="acme_billing/api.py",
        names=["pay"], others=imp))


def test_javascript_subpath_and_go_package_paths_must_name_the_file() -> None:
    npm = FactSet(published=[PublishedPackage("npm", "@acme/billing", "package.json", [])])
    go = FactSet(published=[PublishedPackage("go", "example.com/acme/billing", "go.mod", [])])
    from src.indexing.graph.xrepo import ImportCall

    def imp(eco: str, spec: str) -> dict:
        return {"x": FactSet(imports=[ImportFact(spec, ["pay"], "a", 1, eco, [ImportCall("pay", 5)])])}

    assert impact.symbol_consumers(owner_facts=npm, file="src/pay.ts", names=["pay"],
                                   others=imp("npm", "@acme/billing/pay"))[0].strong
    assert not impact.symbol_consumers(owner_facts=npm, file="src/pay.ts", names=["pay"],
                                       others=imp("npm", "@acme/billing/other"))[0].strong
    assert not impact.symbol_consumers(owner_facts=npm, file="src/pay.ts", names=["pay"],
                                       others=imp("npm", "@acme/billing"))[0].strong
    strong = impact.symbol_consumers(owner_facts=go, file="invoice/pay.go", names=["pay"],
                                     others=imp("go", "example.com/acme/billing/invoice"))
    assert strong[0].strong
    assert not impact.symbol_consumers(owner_facts=go, file="invoice/pay.go", names=["pay"],
                                       others=imp("go", "example.com/acme/billing/other"))[0].strong


def test_a_call_path_made_only_of_holes_is_never_strong() -> None:
    web = _web(host="api.example.com", path="/{}")
    got = impact.route_consumers(
        owner_slug="acme-billing-service", owner_facts=OWNER, method="POST",
        path="/pay", others={"acme-web-shop": web},
        host_map={"api.example.com": "acme-billing-service"})
    assert got and not any(c.strong for c in got)
    assert "no literal segment" in got[0].why


def test_paths_match_agrees_that_holes_are_weak() -> None:
    from src.indexing.graph.xrepo import paths_match

    assert paths_match("/pay", "/{}") == "holes"
    assert paths_match("/pay", "/pay") == "exact"
    assert paths_match("/", "/") == "exact"
