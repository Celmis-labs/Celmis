"""Tests для PHPExtractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.indexing.graph.languages.php import PHPExtractor


@pytest.fixture
def extractor() -> PHPExtractor:
    return PHPExtractor()


def _extract(extractor: PHPExtractor, source: str, file_name: str = "test.php"):
    return extractor.extract(Path(file_name), source=source.encode("utf-8"))


# ─── Imports ────────────────────────────────────────────────────────


class TestImports:
    def test_simple_use(self, extractor: PHPExtractor) -> None:
        res = _extract(extractor, "<?php\nuse App\\Service\\Foo;\n")
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert len(imports) == 1
        assert imports[0].raw_target == "App\\Service\\Foo"

    def test_aliased_use(self, extractor: PHPExtractor) -> None:
        res = _extract(extractor, "<?php\nuse App\\Service\\Foo as Bar;\n")
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert imports[0].raw_target == "App\\Service\\Foo"


# ─── Namespaces ─────────────────────────────────────────────────────


class TestNamespaces:
    def test_namespace_definition(self, extractor: PHPExtractor) -> None:
        res = _extract(extractor, "<?php\nnamespace App\\Service;\n")
        namespaces = [s for s in res.symbols if s.kind == "namespace"]
        assert len(namespaces) == 1
        assert namespaces[0].name == "App\\Service"


# ─── Classes ────────────────────────────────────────────────────────


class TestClasses:
    def test_simple_class(self, extractor: PHPExtractor) -> None:
        res = _extract(extractor, """<?php
class Foo {
    public function bar() {}
}
""")
        classes = [s for s in res.symbols if s.kind == "class"]
        assert len(classes) == 1
        assert classes[0].name == "Foo"

    def test_class_extends(self, extractor: PHPExtractor) -> None:
        res = _extract(extractor, """<?php
class Child extends Base {}
""")
        extends = [e for e in res.edges if e.kind == "EXTENDS"]
        assert extends[0].raw_target == "Base"

    def test_class_implements_multiple(self, extractor: PHPExtractor) -> None:
        res = _extract(extractor, """<?php
class Service implements ServiceInterface, Loggable {}
""")
        impls = [e for e in res.edges if e.kind == "IMPLEMENTS"]
        targets = {e.raw_target for e in impls}
        assert targets == {"ServiceInterface", "Loggable"}

    def test_class_extends_and_implements(self, extractor: PHPExtractor) -> None:
        res = _extract(extractor, """<?php
class Service extends Base implements ServiceInterface {}
""")
        extends = [e for e in res.edges if e.kind == "EXTENDS"]
        impls = [e for e in res.edges if e.kind == "IMPLEMENTS"]
        assert extends[0].raw_target == "Base"
        assert impls[0].raw_target == "ServiceInterface"

    def test_class_methods(self, extractor: PHPExtractor) -> None:
        res = _extract(extractor, """<?php
class Service {
    public function foo() {}
    private function bar() {}
    protected function baz() {}
}
""")
        methods = [s for s in res.symbols if s.kind == "method"]
        assert len(methods) == 3
        names = {m.name for m in methods}
        assert names == {"foo", "bar", "baz"}

        # Visibility → is_exported
        foo = next(m for m in methods if m.name == "foo")
        bar = next(m for m in methods if m.name == "bar")
        assert foo.is_exported is True
        assert bar.is_exported is False  # private

    def test_class_property(self, extractor: PHPExtractor) -> None:
        res = _extract(extractor, """<?php
class Service {
    private LoggerInterface $logger;
    public string $name = "default";
}
""")
        fields = [s for s in res.symbols if s.kind == "field"]
        names = {f.name for f in fields}
        assert names == {"logger", "name"}

        logger_field = next(f for f in fields if f.name == "logger")
        assert logger_field.is_exported is False  # private

    def test_class_constant(self, extractor: PHPExtractor) -> None:
        res = _extract(extractor, """<?php
class Config {
    public const MAX_SIZE = 100;
    private const SECRET = "hidden";
}
""")
        consts = [s for s in res.symbols if s.kind == "constant"]
        assert len(consts) == 2
        names = {c.name for c in consts}
        assert names == {"MAX_SIZE", "SECRET"}


# ─── Interfaces ─────────────────────────────────────────────────────


class TestInterfaces:
    def test_interface(self, extractor: PHPExtractor) -> None:
        res = _extract(extractor, """<?php
interface ServiceInterface {
    public function process(): void;
}
""")
        interfaces = [s for s in res.symbols if s.kind == "interface"]
        assert len(interfaces) == 1
        assert interfaces[0].name == "ServiceInterface"

        # Interface методи теж реєструються
        methods = [s for s in res.symbols if s.kind == "method"]
        assert len(methods) == 1
        assert methods[0].name == "process"


# ─── Functions ──────────────────────────────────────────────────────


class TestFunctions:
    def test_top_level_function(self, extractor: PHPExtractor) -> None:
        res = _extract(extractor, """<?php
function helper(int $x): int {
    return $x + 1;
}
""")
        funcs = [s for s in res.symbols if s.kind == "function"]
        assert len(funcs) == 1
        assert funcs[0].name == "helper"


# ─── Calls ──────────────────────────────────────────────────────────


class TestCalls:
    def test_function_call(self, extractor: PHPExtractor) -> None:
        res = _extract(extractor, """<?php
function caller() {
    helper($x);
}
""")
        calls = [e for e in res.edges if e.kind == "CALLS"]
        assert len(calls) == 1
        assert calls[0].raw_target == "helper"

    def test_member_call_via_this(self, extractor: PHPExtractor) -> None:
        res = _extract(extractor, """<?php
class S {
    public function go() {
        $this->process();
    }
}
""")
        calls = [e for e in res.edges if e.kind == "CALLS"]
        assert len(calls) == 1
        assert calls[0].raw_target == "process"
        assert calls[0].confidence == "weak"  # $this — likely same class

    def test_member_call_via_var(self, extractor: PHPExtractor) -> None:
        res = _extract(extractor, """<?php
function caller() {
    $obj->doSomething();
}
""")
        calls = [e for e in res.edges if e.kind == "CALLS"]
        assert calls[0].raw_target == "doSomething"

    def test_scoped_call(self, extractor: PHPExtractor) -> None:
        res = _extract(extractor, """<?php
function caller() {
    Foo::bar();
    self::staticMethod();
}
""")
        calls = [e for e in res.edges if e.kind == "CALLS"]
        targets = {c.raw_target for c in calls}
        assert targets == {"bar", "staticMethod"}


# ─── Real-world ─────────────────────────────────────────────────────


class TestRealWorld:
    def test_full_module_smoke(self, extractor: PHPExtractor) -> None:
        source = '''<?php
namespace App\\Service;

use Psr\\Log\\LoggerInterface;
use App\\Model\\User;
use App\\Repository\\UserRepository as Repo;

interface ServiceInterface {
    public function process(): void;
}

class UserService extends BaseService implements ServiceInterface {
    private LoggerInterface $logger;
    public const MAX_RETRIES = 3;

    public function __construct(LoggerInterface $logger) {
        $this->logger = $logger;
    }

    public function findById(int $id): ?User {
        $this->logger->info("finding");
        return $this->repo->find($id);
    }

    public static function create(): self {
        return new self(new Logger());
    }
}

function helper(int $x): int {
    return $x + 1;
}
'''
        res = extractor.extract(Path("user_service.php"), source=source.encode("utf-8"))

        assert not any("read_failed" in pe for pe in res.parse_errors)

        kinds = {s.kind for s in res.symbols}
        assert "namespace" in kinds
        assert "class" in kinds
        assert "interface" in kinds
        assert "method" in kinds
        assert "field" in kinds
        assert "constant" in kinds
        assert "function" in kinds  # helper

        # Imports
        imports = {e.raw_target for e in res.edges if e.kind == "IMPORTS"}
        assert "Psr\\Log\\LoggerInterface" in imports
        assert "App\\Model\\User" in imports
        assert "App\\Repository\\UserRepository" in imports

        # EXTENDS / IMPLEMENTS
        assert any(e.raw_target == "BaseService" for e in res.edges if e.kind == "EXTENDS")
        assert any(e.raw_target == "ServiceInterface" for e in res.edges if e.kind == "IMPLEMENTS")

        # Methods + qualified naming
        methods = [s for s in res.symbols if s.kind == "method"]
        method_names = {m.name for m in methods}
        # __construct, findById, create + interface "process"
        assert {"__construct", "findById", "create", "process"} <= method_names

        # CALLS
        call_targets = {e.raw_target for e in res.edges if e.kind == "CALLS"}
        assert "info" in call_targets   # $this->logger->info
        assert "find" in call_targets   # $this->repo->find
