"""Tests для CppExtractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.indexing.graph.languages.cpp import CppExtractor


@pytest.fixture
def extractor() -> CppExtractor:
    return CppExtractor()


def _extract(extractor: CppExtractor, source: str, file_name: str = "test.cpp"):
    return extractor.extract(Path(file_name), source=source.encode("utf-8"))


# ─── Includes ───────────────────────────────────────────────────────


class TestIncludes:
    def test_system_include(self, extractor: CppExtractor) -> None:
        res = _extract(extractor, "#include <iostream>\n")
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert imports[0].raw_target == "iostream"

    def test_local_include(self, extractor: CppExtractor) -> None:
        res = _extract(extractor, '#include "myheader.h"\n')
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert imports[0].raw_target == "myheader.h"

    def test_multi_include(self, extractor: CppExtractor) -> None:
        res = _extract(extractor, """
#include <iostream>
#include <memory>
#include "config.h"
""")
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        targets = {e.raw_target for e in imports}
        assert targets == {"iostream", "memory", "config.h"}


# ─── Namespaces ─────────────────────────────────────────────────────


class TestNamespaces:
    def test_simple_namespace(self, extractor: CppExtractor) -> None:
        res = _extract(extractor, """
namespace app {
    class Foo {};
}
""")
        ns = [s for s in res.symbols if s.kind == "namespace"]
        classes = [s for s in res.symbols if s.kind == "class"]
        assert ns[0].name == "app"
        assert classes[0].name == "Foo"
        assert classes[0].module == "app"


# ─── Classes ────────────────────────────────────────────────────────


class TestClasses:
    def test_simple_class(self, extractor: CppExtractor) -> None:
        res = _extract(extractor, """
class Foo {
public:
    void bar();
};
""")
        classes = [s for s in res.symbols if s.kind == "class"]
        methods = [s for s in res.symbols if s.kind == "method"]
        assert classes[0].name == "Foo"
        assert methods[0].name == "bar"
        assert methods[0].module == "Foo"

    def test_struct(self, extractor: CppExtractor) -> None:
        res = _extract(extractor, """
struct Point {
    int x;
    int y;
};
""")
        structs = [s for s in res.symbols if s.kind == "struct"]
        fields = [s for s in res.symbols if s.kind == "field"]
        assert structs[0].name == "Point"
        names = {f.name for f in fields}
        assert names == {"x", "y"}

    def test_class_inheritance(self, extractor: CppExtractor) -> None:
        res = _extract(extractor, """
class Child : public Base {};
""")
        extends = [e for e in res.edges if e.kind == "EXTENDS"]
        assert extends[0].raw_target == "Base"

    def test_class_multiple_inheritance(self, extractor: CppExtractor) -> None:
        res = _extract(extractor, """
class Child : public Base, virtual public Other {};
""")
        extends = [e for e in res.edges if e.kind == "EXTENDS"]
        targets = {e.raw_target for e in extends}
        assert targets == {"Base", "Other"}


# ─── Methods (out-of-class) ─────────────────────────────────────────


class TestMethods:
    def test_method_definition_outside_class(self, extractor: CppExtractor) -> None:
        res = _extract(extractor, """
class Service {
public:
    void process();
};

void Service::process() {
    helper();
}
""")
        methods = [s for s in res.symbols if s.kind == "method"]
        # Очікуємо обидва: в декларації + у out-of-class definition
        process_methods = [m for m in methods if m.name == "process"]
        assert len(process_methods) >= 1
        assert all(m.module == "Service" for m in process_methods)

    def test_inline_method(self, extractor: CppExtractor) -> None:
        res = _extract(extractor, """
class Foo {
public:
    void bar() {
        helper();
    }
};
""")
        methods = [s for s in res.symbols if s.kind == "method"]
        assert any(m.name == "bar" and m.module == "Foo" for m in methods)
        # CALLS edge має бути від bar до helper
        calls = [e for e in res.edges if e.kind == "CALLS"]
        assert any(c.raw_target == "helper" for c in calls)


# ─── Free functions ─────────────────────────────────────────────────


class TestFreeFunctions:
    def test_free_function(self, extractor: CppExtractor) -> None:
        res = _extract(extractor, """
int add(int a, int b) {
    return a + b;
}
""")
        funcs = [s for s in res.symbols if s.kind == "function"]
        assert len(funcs) == 1
        assert funcs[0].name == "add"

    def test_main_function(self, extractor: CppExtractor) -> None:
        res = _extract(extractor, """
int main() {
    helper();
    return 0;
}
""")
        funcs = [s for s in res.symbols if s.kind == "function"]
        assert funcs[0].name == "main"


# ─── Calls ──────────────────────────────────────────────────────────


class TestCalls:
    def test_simple_call(self, extractor: CppExtractor) -> None:
        res = _extract(extractor, """
void run() {
    helper();
    obj.method();
    Foo::staticMethod();
}
""")
        calls = [e for e in res.edges if e.kind == "CALLS"]
        targets = {c.raw_target for c in calls}
        assert "helper" in targets
        assert "method" in targets       # obj.method → method
        assert "staticMethod" in targets  # Foo::staticMethod → staticMethod

    def test_arrow_call(self, extractor: CppExtractor) -> None:
        res = _extract(extractor, """
void run(Foo* p) {
    p->method();
}
""")
        calls = [e for e in res.edges if e.kind == "CALLS"]
        assert calls[0].raw_target == "method"

    def test_new_expression(self, extractor: CppExtractor) -> None:
        res = _extract(extractor, """
void run() {
    auto x = new Foo();
    new Bar();
}
""")
        calls = [e for e in res.edges if e.kind == "CALLS"]
        targets = {c.raw_target for c in calls}
        assert "Foo" in targets
        assert "Bar" in targets


# ─── Real-world ─────────────────────────────────────────────────────


class TestRealWorld:
    def test_full_module_smoke(self, extractor: CppExtractor) -> None:
        source = '''#include <iostream>
#include <memory>
#include "myheader.h"

namespace app {

class BaseService {};

class UserService : public BaseService {
public:
    UserService(int x);
    User* FindById(int id);
private:
    std::unique_ptr<Repo> repo_;
};

User* UserService::FindById(int id) {
    log_->info("finding");
    return repo_->Find(id);
}

void freeFunction() {
    SomeType obj;
    obj.method();
    Foo::staticMethod();
    new Bar();
}

}
'''
        res = extractor.extract(Path("service.cpp"), source=source.encode("utf-8"))

        kinds = {s.kind for s in res.symbols}
        assert "namespace" in kinds
        assert "class" in kinds
        assert "method" in kinds
        assert "function" in kinds  # freeFunction

        # Includes
        imports = {e.raw_target for e in res.edges if e.kind == "IMPORTS"}
        assert imports == {"iostream", "memory", "myheader.h"}

        # EXTENDS
        extends = [e for e in res.edges if e.kind == "EXTENDS"]
        assert extends[0].raw_target == "BaseService"

        # Methods
        methods = [s for s in res.symbols if s.kind == "method"]
        method_names = {m.name for m in methods}
        # FindById має бути registered (у declaration або definition)
        assert "FindById" in method_names

        # CALLS
        call_targets = {e.raw_target for e in res.edges if e.kind == "CALLS"}
        assert "info" in call_targets         # log_->info
        assert "Find" in call_targets         # repo_->Find
        assert "method" in call_targets       # obj.method
        assert "staticMethod" in call_targets # Foo::staticMethod
        assert "Bar" in call_targets          # new Bar()
