"""Tests для GoExtractor."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.indexing.graph.languages.go import GoExtractor


@pytest.fixture
def extractor() -> GoExtractor:
    return GoExtractor()


def _extract(extractor: GoExtractor, source: str, file_name: str = "main.go"):
    return extractor.extract(Path(file_name), source=source.encode("utf-8"))


# ─── Package + Imports ──────────────────────────────────────────────


class TestImports:
    def test_simple_import(self, extractor: GoExtractor) -> None:
        res = _extract(extractor, '''package main
import "fmt"
''')
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert len(imports) == 1
        assert imports[0].raw_target == "fmt"

    def test_grouped_imports(self, extractor: GoExtractor) -> None:
        res = _extract(extractor, '''package main
import (
    "fmt"
    "errors"
    "context"
)
''')
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert {e.raw_target for e in imports} == {"fmt", "errors", "context"}

    def test_aliased_import(self, extractor: GoExtractor) -> None:
        res = _extract(extractor, '''package main
import log "github.com/sirupsen/logrus"
''')
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert len(imports) == 1
        # raw_target має path::alias
        assert imports[0].raw_target == "github.com/sirupsen/logrus::log"

    def test_third_party_path(self, extractor: GoExtractor) -> None:
        res = _extract(extractor, '''package main
import "github.com/spf13/cobra"
''')
        imports = [e for e in res.edges if e.kind == "IMPORTS"]
        assert imports[0].raw_target == "github.com/spf13/cobra"


# ─── Functions ──────────────────────────────────────────────────────


class TestFunctions:
    def test_simple_function(self, extractor: GoExtractor) -> None:
        res = _extract(extractor, '''package main
func Foo(x int) int {
    return x + 1
}
''')
        funcs = [s for s in res.symbols if s.kind == "function"]
        assert len(funcs) == 1
        assert funcs[0].name == "Foo"
        assert funcs[0].is_exported is True  # PascalCase = exported

    def test_unexported_function(self, extractor: GoExtractor) -> None:
        res = _extract(extractor, '''package main
func helper() {}
''')
        funcs = [s for s in res.symbols if s.kind == "function"]
        assert funcs[0].name == "helper"
        assert funcs[0].is_exported is False  # lowercase = unexported

    def test_main_function(self, extractor: GoExtractor) -> None:
        res = _extract(extractor, '''package main
func main() {
    fmt.Println("hello")
}
''')
        funcs = [s for s in res.symbols if s.kind == "function"]
        assert funcs[0].name == "main"

    def test_function_calls(self, extractor: GoExtractor) -> None:
        res = _extract(extractor, '''package main
func caller() {
    helper()
    fmt.Println("hi")
    obj.Method()
}
''')
        calls = [e for e in res.edges if e.kind == "CALLS"]
        callees = {e.raw_target for e in calls}
        assert "helper" in callees
        assert "Println" in callees  # selector_expression: fmt.Println → "Println"
        assert "Method" in callees   # obj.Method → "Method"


# ─── Methods ────────────────────────────────────────────────────────


class TestMethods:
    def test_pointer_receiver_method(self, extractor: GoExtractor) -> None:
        res = _extract(extractor, '''package main
type Server struct {}
func (s *Server) Start() error {
    return nil
}
''')
        methods = [s for s in res.symbols if s.kind == "method"]
        assert len(methods) == 1
        assert methods[0].name == "Start"
        assert methods[0].module == "Server"  # receiver type

    def test_value_receiver_method(self, extractor: GoExtractor) -> None:
        res = _extract(extractor, '''package main
type Point struct{ x, y int }
func (p Point) Distance() float64 {
    return 0.0
}
''')
        methods = [s for s in res.symbols if s.kind == "method"]
        assert methods[0].name == "Distance"
        assert methods[0].module == "Point"

    def test_method_defined_in_struct(self, extractor: GoExtractor) -> None:
        res = _extract(extractor, '''package main
type Server struct {}
func (s *Server) Start() {}
''')
        defined_in = [
            e for e in res.edges
            if e.kind == "DEFINED_IN" and e.from_id.endswith("Server.Start")
        ]
        # Має бути edge до Server type AND до file_module
        targets = [e.to_id for e in defined_in]
        assert any(t and "Server" in t and not t.endswith("__module__") for t in targets)

    def test_unexported_method(self, extractor: GoExtractor) -> None:
        res = _extract(extractor, '''package main
type Server struct{}
func (s *Server) handle() {}
''')
        methods = [s for s in res.symbols if s.kind == "method"]
        assert methods[0].is_exported is False


# ─── Types ──────────────────────────────────────────────────────────


class TestTypes:
    def test_struct(self, extractor: GoExtractor) -> None:
        res = _extract(extractor, '''package main
type Server struct {
    addr string
}
''')
        structs = [s for s in res.symbols if s.kind == "struct"]
        assert len(structs) == 1
        assert structs[0].name == "Server"

    def test_interface(self, extractor: GoExtractor) -> None:
        res = _extract(extractor, '''package main
type Reader interface {
    Read(p []byte) (n int, err error)
}
''')
        interfaces = [s for s in res.symbols if s.kind == "interface"]
        assert len(interfaces) == 1
        assert interfaces[0].name == "Reader"

    def test_type_alias(self, extractor: GoExtractor) -> None:
        res = _extract(extractor, '''package main
type UserID int
''')
        # Alias реєструється як struct (Go має лише symbol kind)
        types_ = [s for s in res.symbols if s.kind in ("struct", "interface")]
        assert len(types_) == 1
        assert types_[0].name == "UserID"


# ─── Real-world fragment ────────────────────────────────────────────


class TestRealWorld:
    def test_full_module_smoke(self, extractor: GoExtractor) -> None:
        source = '''package main

import (
    "context"
    "fmt"
    "errors"
    log "github.com/sirupsen/logrus"
)

type ServiceInterface interface {
    Process(ctx context.Context) error
}

type Server struct {
    addr string
    svc  ServiceInterface
}

func NewServer(addr string) *Server {
    return &Server{addr: addr}
}

func (s *Server) Start() error {
    log.Info("starting")
    return s.svc.Process(context.Background())
}

func (s *Server) handleRequest(req Request) {
    fmt.Println(req)
}

func main() {
    s := NewServer(":8080")
    if err := s.Start(); err != nil {
        log.Error(err)
    }
}
'''
        res = extractor.extract(Path("server.go"), source=source.encode("utf-8"))

        assert not any("read_failed" in pe for pe in res.parse_errors)

        kinds = {s.kind for s in res.symbols}
        assert "file_module" in kinds
        assert "function" in kinds  # NewServer, main
        assert "method" in kinds    # Start, handleRequest
        assert "struct" in kinds    # Server
        assert "interface" in kinds # ServiceInterface

        # Imports
        imports = {e.raw_target for e in res.edges if e.kind == "IMPORTS"}
        assert "context" in imports
        assert "fmt" in imports
        assert "errors" in imports
        assert "github.com/sirupsen/logrus::log" in imports

        # Symbols
        names = {s.name for s in res.symbols if s.kind != "file_module"}
        assert "Server" in names
        assert "ServiceInterface" in names
        assert "NewServer" in names
        assert "Start" in names
        assert "handleRequest" in names
        assert "main" in names

        # Method module = receiver type
        start_method = next(s for s in res.symbols if s.name == "Start")
        assert start_method.module == "Server"

        # CALLS
        call_targets = {e.raw_target for e in res.edges if e.kind == "CALLS"}
        assert "Info" in call_targets       # log.Info → "Info"
        assert "Process" in call_targets    # s.svc.Process → "Process"
        assert "Background" in call_targets # context.Background → "Background"
        assert "Println" in call_targets    # fmt.Println → "Println"
        assert "NewServer" in call_targets  # main: NewServer(":8080")
        assert "Start" in call_targets      # main: s.Start()
