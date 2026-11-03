"""Per-repository facts that let a review find consumers in OTHER repositories.

The graph of a repository stops at its boundary: calls into a package and HTTP
calls to a route are not edges, so nothing in the graph says who uses what
outside. The extractors in this package record that boundary at index time
(routes exposed, HTTP calls made, packages published, external imports and the
places their names are called) into a JSON file next to the graph, and
`src.groups.impact` matches them across the repositories of one workspace.

A repository indexed before these facts existed has no file; every reader
treats that as "unknown", never as "no consumers".
"""

from src.indexing.graph.xrepo.model import (
    CallFact,
    ExportFact,
    FactSet,
    ImportCall,
    ImportFact,
    PublishedPackage,
    RouteFact,
    ServiceFact,
    facts_path,
    forget_facts,
    load_facts,
    methods_compatible,
    normalize_path,
    paths_match,
    save_facts,
)

__all__ = [
    "CallFact", "ExportFact", "FactSet", "ImportCall", "ImportFact", "PublishedPackage", "RouteFact",
    "ServiceFact", "facts_path", "forget_facts", "load_facts", "methods_compatible", "normalize_path", "paths_match",
    "save_facts",
]
