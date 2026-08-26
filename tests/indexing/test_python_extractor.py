"""Tests для PythonExtractor — symbols + edges from .py files."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.indexing.graph.languages.python import PythonExtractor


@pytest.fixture
def extractor() -> PythonExtractor:
    return PythonExtractor()


def _extract(extractor: PythonExtractor, source: str, file_name: str = "test.py"):
    """Helper: парс source string і повернути result."""
    return extractor.extract(Path(file_name), source=source.encode("utf-8"))


# ─── Imports ────────────────────────────────────────────────────────


class TestImports:
    def test_simple_import(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, "import os\n")
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert len(imports) == 1
        assert imports[0].raw_target == "os"

    def test_dotted_import(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, "import os.path\n")
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert imports[0].raw_target == "os.path"

    def test_aliased_import(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, "import os.path as osp\n")
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert imports[0].raw_target == "os.path"

    def test_multi_import(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, "import os, sys, json\n")
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert len(imports) == 3
        assert {e.raw_target for e in imports} == {"os", "sys", "json"}

    def test_from_import(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, "from typing import List, Optional\n")
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert len(imports) == 2
        targets = {e.raw_target for e in imports}
        assert targets == {"typing::List", "typing::Optional"}

    def test_from_aliased_import(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, "from a.b import helper as h\n")
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert imports[0].raw_target == "a.b::helper"

    def test_relative_import(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, "from .module import helper\n")
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert imports[0].raw_target == ".module::helper"

    def test_wildcard_import(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, "from os import *\n")
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert imports[0].raw_target == "os::*"


# ─── Functions ──────────────────────────────────────────────────────


class TestFunctions:
    def test_simple_function(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, """
def foo(x):
    return x + 1
""")
        funcs = [s for s in res.symbols if s.kind == "function"]
        assert len(funcs) == 1
        assert funcs[0].name == "foo"
        assert funcs[0].is_exported is True

    def test_underscore_function_not_exported(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, """
def _private():
    pass
""")
        funcs = [s for s in res.symbols if s.kind == "function"]
        assert funcs[0].is_exported is False

    def test_async_function(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, """
async def fetch_data():
    return await api()
""")
        funcs = [s for s in res.symbols if s.kind == "function"]
        assert len(funcs) == 1
        assert funcs[0].name == "fetch_data"

    def test_decorated_function(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, """
@cached
@retry(3)
def my_func():
    return helper()
""")
        funcs = [s for s in res.symbols if s.kind == "function"]
        assert len(funcs) == 1
        assert funcs[0].name == "my_func"

    def test_function_calls_captured(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, """
def caller():
    helper(x)
    other.method()
    return module.compute()
""")
        calls = [e for e in res.edges if e.kind == "CALLS"]
        callees = {e.raw_target for e in calls}
        # helper, method (з other.method()), compute (з module.compute())
        assert "helper" in callees
        assert "method" in callees
        assert "compute" in callees


# ─── Classes ────────────────────────────────────────────────────────


class TestClasses:
    def test_simple_class(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, """
class MyClass:
    pass
""")
        classes = [s for s in res.symbols if s.kind == "class"]
        assert len(classes) == 1
        assert classes[0].name == "MyClass"

    def test_class_extends(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, """
class Child(Base):
    pass
""")
        extends = [e for e in res.edges if e.kind == "EXTENDS"]
        assert len(extends) == 1
        assert extends[0].raw_target == "Base"

    def test_class_extends_multiple(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, """
class Child(Base, Mixin):
    pass
""")
        extends = [e for e in res.edges if e.kind == "EXTENDS"]
        assert {e.raw_target for e in extends} == {"Base", "Mixin"}

    def test_class_extends_qualified(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, """
class Child(module.Base):
    pass
""")
        extends = [e for e in res.edges if e.kind == "EXTENDS"]
        assert extends[0].raw_target == "module.Base"

    def test_class_metaclass_skipped(self, extractor: PythonExtractor) -> None:
        """metaclass=Meta — keyword_argument, не treat як EXTENDS."""
        res = _extract(extractor, """
class C(Base, metaclass=Meta):
    pass
""")
        extends = [e for e in res.edges if e.kind == "EXTENDS"]
        assert {e.raw_target for e in extends} == {"Base"}

    def test_class_methods(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, """
class MyClass:
    def method_one(self):
        pass

    def method_two(self, arg):
        return arg
""")
        methods = [s for s in res.symbols if s.kind == "method"]
        assert len(methods) == 2
        names = {m.name for m in methods}
        assert names == {"method_one", "method_two"}
        # module = class name (для qualified resolution)
        assert all(m.module == "MyClass" for m in methods)

    def test_method_defined_in_class(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, """
class MyClass:
    def method_one(self):
        pass
""")
        # method.DEFINED_IN.MyClass має бути присутній
        defined_in = [
            e for e in res.edges
            if e.kind == "DEFINED_IN" and e.from_id.endswith("MyClass.method_one")
        ]
        assert len(defined_in) >= 1
        # Один з edges має target = MyClass id (закінчується на ::MyClass)
        class_targets = [e.to_id for e in defined_in if e.to_id and "MyClass" in e.to_id]
        assert class_targets

    def test_decorated_method(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, """
class MyClass:
    @staticmethod
    def helper():
        pass

    @property
    def value(self):
        return self._v
""")
        methods = [s for s in res.symbols if s.kind == "method"]
        assert len(methods) == 2
        assert {m.name for m in methods} == {"helper", "value"}

    def test_class_field(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, """
class MyClass:
    counter: int = 0
    name = "default"
""")
        fields = [s for s in res.symbols if s.kind == "field"]
        assert len(fields) == 2
        assert {f.name for f in fields} == {"counter", "name"}


# ─── Top-level variables / constants ────────────────────────────────


class TestTopLevelAssignment:
    def test_constant(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, "MAX_SIZE = 100\n")
        consts = [s for s in res.symbols if s.kind == "constant"]
        assert len(consts) == 1
        assert consts[0].name == "MAX_SIZE"

    def test_variable_lowercase(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, "config = {'a': 1}\n")
        vars_ = [s for s in res.symbols if s.kind == "variable"]
        assert len(vars_) == 1
        assert vars_[0].name == "config"

    def test_underscore_private(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, "_internal = 1\n")
        vars_ = [s for s in res.symbols if s.kind == "variable"]
        assert vars_[0].is_exported is False


# ─── DEFINED_IN edges ───────────────────────────────────────────────


class TestDefinedInEdges:
    def test_all_symbols_defined_in_file(self, extractor: PythonExtractor) -> None:
        res = _extract(extractor, """
def foo():
    pass

class Bar:
    pass

CONST = 1
""", file_name="example.py")
        # Усі symbols крім file_module мають DEFINED_IN file_module edge
        file_module_id = "example.py::__module__"
        defined_in_file = [
            e for e in res.edges
            if e.kind == "DEFINED_IN" and e.to_id == file_module_id
        ]
        # foo, Bar, CONST → 3
        non_file_symbols = [s for s in res.symbols if s.kind != "file_module"]
        assert len(non_file_symbols) >= 3
        assert len(defined_in_file) >= 3


# ─── Real-world fragment ────────────────────────────────────────────


class TestRealWorld:
    def test_full_module_smoke(self, extractor: PythonExtractor) -> None:
        """Real-ish Python file — exhaustive sanity check."""
        source = '''
"""Module docstring."""
import os
from typing import Optional, List
from .helpers import compute, validate

CONFIG_PATH = "/etc/app"

@dataclass
class User:
    """User model."""
    name: str
    age: int = 0

    def greet(self):
        return f"Hello, {self.name}"

class Service(BaseService):
    def __init__(self, repo: UserRepo):
        self.repo = repo

    def find(self, user_id: int) -> Optional[User]:
        return self.repo.find_by_id(user_id)

    async def list_all(self) -> List[User]:
        users = await self.repo.fetch_all()
        return [u for u in users if validate(u)]

def main():
    service = Service(repo=UserRepo())
    print(service.find(1))

if __name__ == "__main__":
    main()
'''
        res = extractor.extract(Path("svc.py"), source=source.encode("utf-8"))

        assert not any("read_failed" in pe for pe in res.parse_errors)

        kinds = {s.kind for s in res.symbols}
        assert "file_module" in kinds
        assert "function" in kinds  # main
        assert "class" in kinds  # User, Service
        assert "method" in kinds  # greet, find, list_all, __init__
        assert "constant" in kinds  # CONFIG_PATH
        assert "field" in kinds  # name, age у User

        # Imports
        import_targets = [e.raw_target for e in res.edges if e.kind == "IMPORTS"]
        assert "os" in import_targets
        assert "typing::Optional" in import_targets
        assert "typing::List" in import_targets
        assert ".helpers::compute" in import_targets

        # EXTENDS
        extends = [e for e in res.edges if e.kind == "EXTENDS"]
        assert any(e.raw_target == "BaseService" for e in extends)

        # CALLS
        call_targets = {e.raw_target for e in res.edges if e.kind == "CALLS"}
        assert "find_by_id" in call_targets  # self.repo.find_by_id у Service.find
        assert "fetch_all" in call_targets   # self.repo.fetch_all у Service.list_all
        assert "validate" in call_targets    # validate(u) у list_all
        assert "main" in call_targets        # main() у if __name__ block
