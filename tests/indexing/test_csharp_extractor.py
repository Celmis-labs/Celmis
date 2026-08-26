"""Tests для CSharpExtractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.indexing.graph.languages.csharp import CSharpExtractor


@pytest.fixture
def extractor() -> CSharpExtractor:
    return CSharpExtractor()


def _extract(extractor: CSharpExtractor, source: str, file_name: str = "Test.cs"):
    return extractor.extract(Path(file_name), source=source.encode("utf-8"))


# ─── Namespace + Imports ────────────────────────────────────────────


class TestNamespacesAndUsings:
    def test_file_scoped_namespace(self, extractor: CSharpExtractor) -> None:
        res = _extract(extractor, "namespace MyApp.Services;\n")
        ns = [s for s in res.symbols if s.kind == "namespace"]
        assert len(ns) == 1
        assert ns[0].name == "MyApp.Services"

    def test_block_namespace(self, extractor: CSharpExtractor) -> None:
        res = _extract(extractor, """
namespace MyApp {
    public class Foo {}
}
""")
        ns = [s for s in res.symbols if s.kind == "namespace"]
        classes = [s for s in res.symbols if s.kind == "class"]
        assert len(ns) == 1
        assert ns[0].name == "MyApp"
        # Class всередині namespace block — теж зареєстрований
        assert len(classes) == 1
        assert classes[0].name == "Foo"
        assert classes[0].module == "MyApp"

    def test_using(self, extractor: CSharpExtractor) -> None:
        res = _extract(extractor, "using System.Threading.Tasks;")
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert imports[0].raw_target == "System.Threading.Tasks"


# ─── Classes ────────────────────────────────────────────────────────


class TestClasses:
    def test_simple_class(self, extractor: CSharpExtractor) -> None:
        res = _extract(extractor, """
public class Foo {
    public void Bar() {}
}
""")
        classes = [s for s in res.symbols if s.kind == "class"]
        assert len(classes) == 1
        assert classes[0].name == "Foo"
        assert classes[0].is_exported is True

    def test_class_extends_only_class(self, extractor: CSharpExtractor) -> None:
        """`: Base` без 'I' prefix → EXTENDS."""
        res = _extract(extractor, """
public class Child : Base {}
""")
        extends = [e for e in res.edges if e.kind == "EXTENDS"]
        assert extends[0].raw_target == "Base"

    def test_class_implements_interface_via_convention(self, extractor: CSharpExtractor) -> None:
        """`: IFoo` → IMPLEMENTS (heuristic 'I' prefix)."""
        res = _extract(extractor, """
public class Service : IUserService {}
""")
        impls = [e for e in res.edges if e.kind == "IMPLEMENTS"]
        assert impls[0].raw_target == "IUserService"

    def test_class_extends_and_implements(self, extractor: CSharpExtractor) -> None:
        res = _extract(extractor, """
public class Service : BaseService, IUserService, IDisposable {}
""")
        extends = [e for e in res.edges if e.kind == "EXTENDS"]
        impls = [e for e in res.edges if e.kind == "IMPLEMENTS"]
        assert {e.raw_target for e in extends} == {"BaseService"}
        assert {e.raw_target for e in impls} == {"IUserService", "IDisposable"}

    def test_class_methods(self, extractor: CSharpExtractor) -> None:
        res = _extract(extractor, """
public class Foo {
    public void Pub() {}
    private void Priv() {}
    protected int Prot() => 1;
}
""")
        methods = [s for s in res.symbols if s.kind == "method"]
        names = {m.name for m in methods}
        assert names == {"Pub", "Priv", "Prot"}
        pub = next(m for m in methods if m.name == "Pub")
        priv = next(m for m in methods if m.name == "Priv")
        assert pub.is_exported is True
        assert priv.is_exported is False

    def test_constructor(self, extractor: CSharpExtractor) -> None:
        res = _extract(extractor, """
public class Foo {
    public Foo(int x) {}
}
""")
        methods = [s for s in res.symbols if s.kind == "method"]
        ctors = [m for m in methods if m.name == ".ctor"]
        assert len(ctors) == 1


class TestFields:
    def test_field(self, extractor: CSharpExtractor) -> None:
        res = _extract(extractor, """
public class Foo {
    private readonly ILogger _log;
    public string Name;
}
""")
        fields = [s for s in res.symbols if s.kind == "field"]
        names = {f.name for f in fields}
        assert names == {"_log", "Name"}

    def test_const(self, extractor: CSharpExtractor) -> None:
        res = _extract(extractor, """
public class Foo {
    public const int MaxRetries = 3;
}
""")
        consts = [s for s in res.symbols if s.kind == "constant"]
        assert len(consts) == 1
        assert consts[0].name == "MaxRetries"

    def test_property(self, extractor: CSharpExtractor) -> None:
        res = _extract(extractor, """
public class Foo {
    public string Name { get; set; }
}
""")
        # Property реєструється як field (для simplicity)
        fields = [s for s in res.symbols if s.kind == "field"]
        assert any(f.name == "Name" for f in fields)


# ─── Interfaces ─────────────────────────────────────────────────────


class TestInterfaces:
    def test_interface(self, extractor: CSharpExtractor) -> None:
        res = _extract(extractor, """
public interface IUserService {
    Task FindAsync(long id);
}
""")
        ifaces = [s for s in res.symbols if s.kind == "interface"]
        methods = [s for s in res.symbols if s.kind == "method"]
        assert ifaces[0].name == "IUserService"
        assert methods[0].name == "FindAsync"


# ─── Enums ──────────────────────────────────────────────────────────


class TestEnums:
    def test_enum(self, extractor: CSharpExtractor) -> None:
        res = _extract(extractor, """
public enum Status { Active, Inactive, Pending }
""")
        enums = [s for s in res.symbols if s.kind == "enum"]
        consts = [s for s in res.symbols if s.kind == "constant"]
        assert enums[0].name == "Status"
        assert {c.name for c in consts} == {"Active", "Inactive", "Pending"}


# ─── Calls ──────────────────────────────────────────────────────────


class TestCalls:
    def test_simple_invocation(self, extractor: CSharpExtractor) -> None:
        res = _extract(extractor, """
public class Foo {
    public void Run() {
        Helper();
        Logger.Info("msg");
        obj.Method();
    }
}
""")
        calls = [e for e in res.edges if e.kind == "CALLS"]
        targets = {c.raw_target for c in calls}
        assert "Helper" in targets
        assert "Info" in targets
        assert "Method" in targets

    def test_object_creation(self, extractor: CSharpExtractor) -> None:
        res = _extract(extractor, """
public class Foo {
    public void Run() {
        var x = new Other();
        new Service().DoIt();
    }
}
""")
        calls = [e for e in res.edges if e.kind == "CALLS"]
        targets = {c.raw_target for c in calls}
        assert "Other" in targets
        assert "Service" in targets
        assert "DoIt" in targets


# ─── Real-world ─────────────────────────────────────────────────────


class TestRealWorld:
    def test_full_module_smoke(self, extractor: CSharpExtractor) -> None:
        source = '''
namespace MyApp.Services;

using System.Threading.Tasks;
using Microsoft.Extensions.Logging;
using MyApp.Models;

public interface IUserService {
    Task<User?> FindById(long id);
}

public class UserService : BaseService, IUserService {
    private readonly ILogger _log;
    public const int MaxRetries = 3;

    public UserService(ILogger log) {
        _log = log;
    }

    public async Task<User?> FindById(long id) {
        _log.LogInformation("finding");
        return await _repo.FindAsync(id);
    }

    private void Helper() {
        Helper.StaticCall();
        new Other().ChainedCall();
    }
}

public enum Status { Active, Inactive }
'''
        res = extractor.extract(Path("UserService.cs"), source=source.encode("utf-8"))

        kinds = {s.kind for s in res.symbols}
        assert "namespace" in kinds
        assert "interface" in kinds
        assert "class" in kinds
        assert "method" in kinds
        assert "field" in kinds
        assert "constant" in kinds  # MaxRetries
        assert "enum" in kinds

        # Imports
        imports = {e.raw_target for e in res.edges if e.kind == "IMPORTS"}
        assert "System.Threading.Tasks" in imports
        assert "Microsoft.Extensions.Logging" in imports

        # base_list classification — BaseService=EXTENDS, IUserService=IMPLEMENTS
        extends = {e.raw_target for e in res.edges if e.kind == "EXTENDS"}
        impls = {e.raw_target for e in res.edges if e.kind == "IMPLEMENTS"}
        assert "BaseService" in extends
        assert "IUserService" in impls

        # CALLS
        call_targets = {e.raw_target for e in res.edges if e.kind == "CALLS"}
        assert "LogInformation" in call_targets
        assert "FindAsync" in call_targets
        assert "StaticCall" in call_targets
        assert "Other" in call_targets  # new Other()
        assert "ChainedCall" in call_targets

        # Methods
        methods = [s for s in res.symbols if s.kind == "method"]
        method_names = {m.name for m in methods}
        assert "FindById" in method_names
        assert ".ctor" in method_names
        assert "Helper" in method_names
