"""Our own tree-sitter code analyzer — the replacement for CGC in v3.0.

Structure:
    extractor.py    — SymbolExtractor contract + types (SymbolInfo, EdgeInfo)
    languages/      — per-language adapters (typescript, vue; others — deferred)
    configs.py      — RepoContext (tsconfig paths, package.json deps, env, CI)
    resolver.py     — heuristic name resolution
    graph_store.py  — GraphStore interface (FalkorDBLite — embedded, persistent)
    pagerank.py     — ranking of symbols for the retriever (opt.)

Replaced src/indexing/codegraph.py, which wrapped a retired external
backend and has been deleted.
"""
