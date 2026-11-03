"""Packages a repository publishes, the external packages it imports, and the
facts file the indexer leaves beside the graph."""

from __future__ import annotations

import subprocess
from pathlib import Path

from src.indexing.graph.languages.go import GoExtractor
from src.indexing.graph.languages.java import JavaExtractor
from src.indexing.graph.languages.python import PythonExtractor
from src.indexing.graph.languages.typescript import TypeScriptExtractor
from src.indexing.graph.xrepo import load_facts
from src.indexing.graph.xrepo.manifests import published_packages


def _imports(extractor, rel: str, source: str):
    res = extractor.extract(Path(rel), source=source.encode())
    assert res.facts is not None
    return {i.package: (sorted(i.names), sorted(c.name for c in i.calls)) for i in res.facts.imports}


def test_python_from_import_and_module_import_with_their_calls() -> None:
    src = '''
import os
import acme_billing as ab
from acme_billing.client import pay_invoice, Client as C
from . import local
from json import dumps

def f(o):
    pay_invoice(o.id, 1)
    C.create(o)
    ab.refund(o)
'''
    got = _imports(PythonExtractor(), "shop/a.py", src)
    assert got == {
        "acme_billing": ([], ["refund"]),
        "acme_billing.client": (["Client", "pay_invoice"], ["Client.create", "pay_invoice"]),
    }


def test_js_named_default_namespace_and_require() -> None:
    src = '''
import ui, { Button as B, helper } from "@acme/ui";
import * as billing from "acme-billing";
import local from "./local";
const { pay } = require("acme-pay");
B(); helper(1); billing.charge(2); pay(3); new ui.Thing();
'''
    got = _imports(TypeScriptExtractor(), "src/a.ts", src)
    assert got == {
        "@acme/ui": (["Button", "default", "helper"], ["Button", "Thing", "helper"]),
        "acme-billing": ([], ["charge"]),
        "acme-pay": (["pay"], ["pay"]),
    }


def test_go_import_alias_and_versioned_path() -> None:
    src = '''package main
import (
  "fmt"
  billing "github.com/acme/billing-client/v2"
  "github.com/acme/other"
)
func main() { billing.Pay(1); other.Do(); fmt.Println() }
'''
    assert _imports(GoExtractor(), "main.go", src) == {
        "github.com/acme/billing-client/v2": (["Pay"], ["Pay"]),
        "github.com/acme/other": (["Do"], ["Do"]),
    }


def test_java_import_of_a_class_and_its_calls() -> None:
    src = '''
import com.acme.billing.Client;
import java.util.List;
class A { void f() { Client.pay(1); new Client(); } }
'''
    assert _imports(JavaExtractor(), "A.java", src) == {
        "com.acme.billing.Client": (["Client"], ["Client", "Client.pay"])}


def test_manifests_pyproject_package_json_go_mod_and_pom(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "Acme.Billing-Client"\n')
    (tmp_path / "acme_billing_client").mkdir()
    (tmp_path / "acme_billing_client" / "__init__.py").write_text("")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text('{"name": "@acme/web-kit", "private": true}')
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "go.mod").write_text("module github.com/acme/svc\n\ngo 1.25\n")
    (tmp_path / "java").mkdir()
    (tmp_path / "java" / "pom.xml").write_text(
        "<project><parent><groupId>com.acme</groupId></parent>"
        "<artifactId>billing-api</artifactId>"
        "<dependencies><dependency><groupId>x</groupId><artifactId>dep</artifactId>"
        "</dependency></dependencies></project>")
    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "node_modules" / "dep" / "package.json").write_text('{"name": "dep"}')

    got = {(p.ecosystem, p.name): p for p in published_packages(tmp_path)}

    assert set(got) == {("pypi", "Acme.Billing-Client"), ("npm", "@acme/web-kit"),
                        ("go", "github.com/acme/svc"), ("maven", "com.acme:billing-api")}
    assert got[("pypi", "Acme.Billing-Client")].modules == ["acme_billing_client"]
    assert got[("npm", "@acme/web-kit")].file == "web/package.json"


def _git(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)


def test_the_indexer_writes_the_facts_beside_the_graph(tmp_path: Path) -> None:
    from src.config import Settings
    from src.indexing.graph.pipeline import index_repo_graph

    repo = tmp_path / "repos" / "acme-billing-service"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pyproject.toml").write_text('[project]\nname = "acme-billing-client"\n')
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "api.py").write_text(
        'import httpx\nfrom billing_core import charge\n\n'
        '@app.post("/pay/{id}")\ndef pay(id):\n    charge(id)\n'
        '    httpx.get("http://ledger/entries")\n')
    (repo / "docker-compose.yml").write_text(
        "services:\n  ledger:\n    image: registry.example.com/acme/ledger:1\n")
    _git(repo)
    settings = Settings(workspace_dir=tmp_path)

    index_repo_graph(repo, "acme-billing-service", settings)
    facts = load_facts(settings.repo_graph_path("acme-billing-service"))

    assert facts is not None
    assert [(r.method, r.path, r.file) for r in facts.routes] == [("POST", "/pay/{}", "pkg/api.py")]
    assert [(c.host, c.path) for c in facts.calls] == [("ledger", "/entries")]
    assert [p.name for p in facts.published] == ["acme-billing-client"]
    assert [i.package for i in facts.imports if i.package == "billing_core"] == ["billing_core"]
    assert [(s.name, s.image) for s in facts.services] == [
        ("ledger", "registry.example.com/acme/ledger:1")]


def test_a_repository_indexed_before_facts_existed_reads_as_unknown(tmp_path: Path) -> None:
    graph = tmp_path / "graph.fdblite"
    graph.write_bytes(b"")
    assert load_facts(graph) is None
    graph.with_name(graph.name + ".xrepo.json").write_text("{not json")
    assert load_facts(graph) is None
    graph.with_name(graph.name + ".xrepo.json").write_text('{"version": 999}')
    assert load_facts(graph) is None
