"""Tests для JavaExtractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.indexing.graph.languages.java import JavaExtractor


@pytest.fixture
def extractor() -> JavaExtractor:
    return JavaExtractor()


def _extract(extractor: JavaExtractor, source: str, file_name: str = "Test.java"):
    return extractor.extract(Path(file_name), source=source.encode("utf-8"))


# ─── Imports + Package ──────────────────────────────────────────────


class TestImportsAndPackage:
    def test_package(self, extractor: JavaExtractor) -> None:
        res = _extract(extractor, "package com.example.app;")
        pkgs = [s for s in res.symbols if s.kind == "package"]
        assert len(pkgs) == 1
        assert pkgs[0].name == "com.example.app"

    def test_simple_import(self, extractor: JavaExtractor) -> None:
        res = _extract(extractor, """
package com.example;
import java.util.List;
""")
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert imports[0].raw_target == "java.util.List"

    def test_static_import(self, extractor: JavaExtractor) -> None:
        res = _extract(extractor, """
package com.example;
import static java.util.Arrays.asList;
""")
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert imports[0].raw_target == "java.util.Arrays.asList"


# ─── Classes ────────────────────────────────────────────────────────


class TestClasses:
    def test_simple_class(self, extractor: JavaExtractor) -> None:
        res = _extract(extractor, """
public class Foo {
    public void bar() {}
}
""")
        classes = [s for s in res.symbols if s.kind == "class"]
        assert len(classes) == 1
        assert classes[0].name == "Foo"
        assert classes[0].is_exported is True

    def test_package_private_class(self, extractor: JavaExtractor) -> None:
        res = _extract(extractor, """
class Foo {}
""")
        classes = [s for s in res.symbols if s.kind == "class"]
        assert classes[0].is_exported is False  # no public

    def test_class_extends(self, extractor: JavaExtractor) -> None:
        res = _extract(extractor, """
public class Child extends Base {}
""")
        extends = [e for e in res.edges if e.kind == "EXTENDS"]
        assert extends[0].raw_target == "Base"

    def test_class_extends_generic(self, extractor: JavaExtractor) -> None:
        res = _extract(extractor, """
public class Child extends Base<String> {}
""")
        extends = [e for e in res.edges if e.kind == "EXTENDS"]
        # Generic stripped — лиш bare type name
        assert extends[0].raw_target == "Base"

    def test_class_implements(self, extractor: JavaExtractor) -> None:
        res = _extract(extractor, """
public class Foo implements Bar, Baz {}
""")
        impls = [e for e in res.edges if e.kind == "IMPLEMENTS"]
        targets = {e.raw_target for e in impls}
        assert targets == {"Bar", "Baz"}

    def test_class_extends_and_implements(self, extractor: JavaExtractor) -> None:
        res = _extract(extractor, """
public class Foo extends Base implements Bar {}
""")
        extends = [e for e in res.edges if e.kind == "EXTENDS"]
        impls = [e for e in res.edges if e.kind == "IMPLEMENTS"]
        assert extends[0].raw_target == "Base"
        assert impls[0].raw_target == "Bar"


class TestMethods:
    def test_methods(self, extractor: JavaExtractor) -> None:
        res = _extract(extractor, """
public class Foo {
    public void publicMethod() {}
    private void privateMethod() {}
    protected int protMethod(int x) { return x; }
}
""")
        methods = [s for s in res.symbols if s.kind == "method"]
        assert len(methods) == 3
        names = {m.name for m in methods}
        assert names == {"publicMethod", "privateMethod", "protMethod"}

        public_m = next(m for m in methods if m.name == "publicMethod")
        private_m = next(m for m in methods if m.name == "privateMethod")
        assert public_m.is_exported is True
        assert private_m.is_exported is False

    def test_constructor(self, extractor: JavaExtractor) -> None:
        res = _extract(extractor, """
public class Foo {
    public Foo(int x) {
        this.x = x;
    }
}
""")
        methods = [s for s in res.symbols if s.kind == "method"]
        # Constructor named '<init>'
        ctors = [m for m in methods if m.name == "<init>"]
        assert len(ctors) == 1


class TestFields:
    def test_field(self, extractor: JavaExtractor) -> None:
        res = _extract(extractor, """
public class Foo {
    private String name;
    public int count;
}
""")
        fields = [s for s in res.symbols if s.kind == "field"]
        names = {f.name for f in fields}
        assert names == {"name", "count"}

    def test_constant(self, extractor: JavaExtractor) -> None:
        """final UPPERCASE → constant kind."""
        res = _extract(extractor, """
public class Foo {
    public static final int MAX_SIZE = 100;
}
""")
        consts = [s for s in res.symbols if s.kind == "constant"]
        assert len(consts) == 1
        assert consts[0].name == "MAX_SIZE"

    def test_multiple_field_declaration(self, extractor: JavaExtractor) -> None:
        """`int x, y, z;` → 3 fields."""
        res = _extract(extractor, """
public class Foo {
    private int x, y, z;
}
""")
        fields = [s for s in res.symbols if s.kind == "field"]
        names = {f.name for f in fields}
        assert names == {"x", "y", "z"}


# ─── Interfaces ─────────────────────────────────────────────────────


class TestInterfaces:
    def test_interface(self, extractor: JavaExtractor) -> None:
        res = _extract(extractor, """
public interface Service {
    void process();
}
""")
        ifaces = [s for s in res.symbols if s.kind == "interface"]
        assert len(ifaces) == 1
        assert ifaces[0].name == "Service"

    def test_interface_extends(self, extractor: JavaExtractor) -> None:
        res = _extract(extractor, """
public interface Service extends BaseService, Loggable {}
""")
        extends = [e for e in res.edges if e.kind == "EXTENDS"]
        targets = {e.raw_target for e in extends}
        assert targets == {"BaseService", "Loggable"}


# ─── Enums ──────────────────────────────────────────────────────────


class TestEnums:
    def test_enum(self, extractor: JavaExtractor) -> None:
        res = _extract(extractor, """
public enum Status {
    ACTIVE, INACTIVE, PENDING
}
""")
        enums = [s for s in res.symbols if s.kind == "enum"]
        consts = [s for s in res.symbols if s.kind == "constant"]
        assert len(enums) == 1
        assert enums[0].name == "Status"
        # Enum constants
        const_names = {c.name for c in consts}
        assert const_names == {"ACTIVE", "INACTIVE", "PENDING"}


# ─── Calls ──────────────────────────────────────────────────────────


class TestCalls:
    def test_method_invocation(self, extractor: JavaExtractor) -> None:
        res = _extract(extractor, """
public class Foo {
    public void caller() {
        helper();
        log.info("msg");
        Helper.staticCall();
    }
}
""")
        calls = [e for e in res.edges if e.kind == "CALLS"]
        targets = {c.raw_target for c in calls}
        assert "helper" in targets
        assert "info" in targets
        assert "staticCall" in targets

    def test_this_method_call(self, extractor: JavaExtractor) -> None:
        res = _extract(extractor, """
public class Foo {
    public void caller() {
        this.helper();
    }
}
""")
        calls = [e for e in res.edges if e.kind == "CALLS"]
        helper_call = next(c for c in calls if c.raw_target == "helper")
        assert helper_call.confidence == "weak"  # this.X — same class

    def test_object_creation(self, extractor: JavaExtractor) -> None:
        """`new Foo()` теж CALLS edge до Foo."""
        res = _extract(extractor, """
public class Caller {
    public void run() {
        Other o = new Other();
        new Foo("x").chained();
    }
}
""")
        calls = [e for e in res.edges if e.kind == "CALLS"]
        targets = {c.raw_target for c in calls}
        assert "Other" in targets
        assert "Foo" in targets
        assert "chained" in targets


# ─── Real-world ─────────────────────────────────────────────────────


class TestRealWorld:
    def test_full_module_smoke(self, extractor: JavaExtractor) -> None:
        source = '''
package com.example;

import java.util.List;
import java.util.Optional;
import org.slf4j.Logger;

public interface UserService {
    User findById(Long id);
}

public class UserServiceImpl extends BaseService implements UserService {
    private final Logger log;
    public static final int MAX = 100;

    public UserServiceImpl(Logger log) {
        this.log = log;
    }

    @Override
    public User findById(Long id) {
        log.info("finding {}", id);
        return repo.findById(id).orElse(null);
    }

    private void helper() {
        Helper.staticCall();
        new Other().chainedCall();
    }
}
'''
        res = extractor.extract(Path("UserService.java"), source=source.encode("utf-8"))

        kinds = {s.kind for s in res.symbols}
        assert "package" in kinds
        assert "interface" in kinds
        assert "class" in kinds
        assert "method" in kinds
        assert "field" in kinds
        assert "constant" in kinds  # MAX

        # Imports
        imports = {e.raw_target for e in res.edges if e.kind == "IMPORTS"}
        assert "java.util.List" in imports
        assert "org.slf4j.Logger" in imports

        # EXTENDS / IMPLEMENTS
        assert any(e.raw_target == "BaseService" for e in res.edges if e.kind == "EXTENDS")
        assert any(e.raw_target == "UserService" for e in res.edges if e.kind == "IMPLEMENTS")

        # Methods (включно з ctor)
        methods = [s for s in res.symbols if s.kind == "method"]
        method_names = {m.name for m in methods}
        assert "findById" in method_names  # interface + impl
        assert "<init>" in method_names    # constructor
        assert "helper" in method_names

        # CALLS
        call_targets = {e.raw_target for e in res.edges if e.kind == "CALLS"}
        assert "info" in call_targets       # log.info
        assert "findById" in call_targets   # repo.findById
        assert "orElse" in call_targets     # chained
        assert "staticCall" in call_targets # Helper.staticCall
        assert "Other" in call_targets      # new Other()
