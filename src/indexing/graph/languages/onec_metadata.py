"""1C:Enterprise metadata extractor — one symbol per object of a configuration dump.

The BSL modules say what the code does; the metadata XML says what the objects
ARE called by the people who use them. A catalog, document, register or report is
an XML file (`Documents/<Name>.xml` in a Designer dump) whose `Synonym` holds the
name shown in the interface, once per interface language. That is the word a user
types ("розкрій", "остатки") while the code says something else, so it is kept
searchable: the synonyms, in every language the file has, are the symbol's
docstring.

Symbol model:
    <vendor.onec.catalog | document | informationregister | ...>
                 — one per object, named `<Type>.<Object>` exactly as the BSL
                   extractor names the modules of the object (`Catalogs.Items`),
                   so `find` matches the object, its module and `Type.Object.Method`.
                   signature: attributes, dimensions, resources, tabular
                   sections (with their columns), forms — names only, capped.
                   docstring: the synonyms per language, then the comment.

Edge model:
    REFERENCES   — a subsystem → each object in its content
    (the BSL extractor adds: module → the object it belongs to, method → the
    data objects its code and queries name)

What is NOT covered: EDT's `.mdo` files (a different XML), forms' own XML,
roles and rights, predefined data, the type of each attribute.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from src.indexing.graph.configs import RepoContext
from src.indexing.graph.extractor import EdgeInfo, ExtractionResult, SymbolExtractor, SymbolInfo
from src.indexing.graph.languages.onec import SINGULAR, TYPES, any_object

logger = logging.getLogger(__name__)

#: A metadata XML is a few hundred KB at most; past this it is not one.
MAX_METADATA_BYTES = 8 * 1024 * 1024
_MAX_DOC_CHARS = 1500
_MAX_SIGNATURE_CHARS = 700
_FORBIDDEN = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)
#: Child kinds listed in the signature, in this order, under this label.
_PARTS = (
    ("Attribute", "attributes"), ("Dimension", "dimensions"), ("Resource", "resources"),
    ("EnumValue", "values"), ("TabularSection", "tabular"), ("Form", "forms"),
)
_LANG_ORDER = ("ru", "uk", "en")


def is_metadata_path(path: Path) -> bool:
    """`<Type>/<Object>.xml` of a Designer dump (the type directory names the
    kind of object; the content is checked when the file is read)."""
    return path.suffix.lower() == ".xml" and path.parent.name in TYPES


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(node: ET.Element | None, name: str) -> ET.Element | None:
    if node is None:
        return None
    for c in node:
        if _local(c.tag) == name:
            return c
    return None


def _text(node: ET.Element | None, name: str) -> str:
    c = _child(node, name)
    return (c.text or "").strip() if c is not None else ""


def synonyms(props: ET.Element | None) -> dict[str, str]:
    """`{language: text}` of a Synonym element, empty texts left out."""
    out: dict[str, str] = {}
    syn = _child(props, "Synonym")
    if syn is None:
        return out
    for item in syn:
        lang, content = _text(item, "lang"), _text(item, "content")
        if lang and content:
            out[lang] = content
    return out


def _ordered(syn: dict[str, str]) -> list[tuple[str, str]]:
    first = [(k, syn[k]) for k in _LANG_ORDER if k in syn]
    return first + [(k, v) for k, v in sorted(syn.items()) if k not in _LANG_ORDER]


class OneCMetadataExtractor(SymbolExtractor):
    language = "onec"
    extensions = ()  # `.xml` is shared; dispatch is by the type directory

    def __init__(
        self,
        ctx: RepoContext | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.ctx = ctx
        self.repo_root = (
            Path(repo_root).resolve()
            if repo_root
            else (ctx.repo_root if ctx else None)
        )

    def extract(self, file_path: Path, source: bytes | None = None) -> ExtractionResult:
        rel = self._relpath(file_path)
        if source is None:
            try:
                if file_path.stat().st_size > MAX_METADATA_BYTES:
                    return ExtractionResult(parse_errors=[f"too_large file={rel}"])
                source = file_path.read_bytes()
            except OSError as e:
                return ExtractionResult(parse_errors=[f"read_failed: {e}"])
        elif len(source) > MAX_METADATA_BYTES:
            return ExtractionResult(parse_errors=[f"too_large file={rel}"])
        if b"MetaDataObject" not in source[:4096]:
            return ExtractionResult()  # some other XML in a folder named like a type
        if _FORBIDDEN.search(source[:4096]):
            # Entities are never in a dump; refuse rather than expand them.
            return ExtractionResult(parse_errors=[f"entity_declaration file={rel}"])
        try:
            root = ET.fromstring(source)
        except ET.ParseError as e:
            return ExtractionResult(parse_errors=[f"xml_error file={rel}: {e}"])

        obj = next(iter(root), None)
        props = _child(obj, "Properties")
        name = _text(props, "Name")
        plural = file_path.parent.name
        if obj is None or not name or plural not in SINGULAR:
            return ExtractionResult()
        qualified = f"{plural}.{name}"
        syn = synonyms(props)
        comment = _text(props, "Comment")
        child_objects = _child(obj, "ChildObjects")

        doc = "\n".join(f"{lang}: {text}" for lang, text in _ordered(syn))
        if comment:
            doc += f"\ncomment: {comment}"
        fields = self._field_words(child_objects)
        if fields:
            doc += "\nfields: " + fields
        sym = SymbolInfo(
            id=f"{rel}::{qualified}", name=qualified,
            kind=f"vendor.onec.{SINGULAR[plural].lower()}", file=rel, start_line=1,
            end_line=None, language="onec", module=plural,
            signature=self._signature(SINGULAR[plural], child_objects),
            docstring=doc.strip()[:_MAX_DOC_CHARS] or None,
        )
        result = ExtractionResult(symbols=[sym])
        if plural == "Subsystems":
            for item in self._content(props):
                target = any_object(*item)
                if target:
                    result.edges.append(EdgeInfo(from_id=sym.id, to_id=None, kind="REFERENCES",
                                                 confidence="unresolved", raw_target=target))
        return result

    @staticmethod
    def _content(props: ET.Element | None) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        content = _child(props, "Content")
        for item in content if content is not None else ():
            kind, _, name = (item.text or "").strip().partition(".")
            if kind and name and "." not in name:
                out.append((kind, name))
        return out

    @staticmethod
    def _signature(singular: str, children: ET.Element | None) -> str:
        parts: list[str] = [singular]
        if children is not None:
            for tag, label in _PARTS:
                names: list[str] = []
                for c in children:
                    if _local(c.tag) != tag:
                        continue
                    # Forms, commands and templates are listed as `<Form>Name</Form>`.
                    nm = _text(_child(c, "Properties"), "Name") or (c.text or "").strip()
                    if tag == "TabularSection" and nm:
                        cols = [_text(_child(a, "Properties"), "Name")
                                for a in (_child(c, "ChildObjects") or ())
                                if _local(a.tag) == "Attribute"]
                        nm += "(" + ", ".join(x for x in cols if x) + ")"
                    if nm:
                        names.append(nm)
                if names:
                    parts.append(f"{label}: " + ", ".join(names))
        return "; ".join(parts)[:_MAX_SIGNATURE_CHARS]

    @staticmethod
    def _field_words(children: ET.Element | None) -> str:
        """`Name (synonym)` of the attributes, dimensions, resources and tabular
        sections — what a person calls a field is often the only place a word
        of the question appears."""
        if children is None:
            return ""
        out: list[str] = []
        for c in children:
            if _local(c.tag) not in ("Attribute", "Dimension", "Resource", "TabularSection"):
                continue
            props = _child(c, "Properties")
            nm = _text(props, "Name")
            syn = synonyms(props)
            label = next((syn[k] for k in _LANG_ORDER if k in syn), "")
            if nm:
                out.append(f"{nm} ({label})" if label and label != nm else nm)
        return ", ".join(out)

    def _relpath(self, file_path: Path) -> str:
        if self.repo_root is not None:
            try:
                return file_path.resolve().relative_to(self.repo_root).as_posix()
            except (ValueError, OSError):
                pass
        return file_path.as_posix()
