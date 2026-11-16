"""What an external report, external data processor or `Form.bin` holds.

An external file (`.erf` / `.epf`) is a container whose entry `root` names the
main object's entry (`{2,<uuid>,}`). That entry's text is the object's
metadata (name, synonyms, the comment people keep the version in); `<uuid>.0`,
when present, is a nested container whose `text` is the object module. Every
form and template is an entry `<uuid>` (its metadata) beside `<uuid>.0` (its
content): a form's content is a container of `form` (the description, with the
event bindings) and `module`; a spreadsheet template's is a MOXCEL document.

The code is laid out the way a configuration dump would hold the same object,
so a path in the external file reads like the path of its counterpart:

    Reports/<Name>/Ext/ObjectModule.bsl
    Reports/<Name>/Forms/<Form>/Ext/Form/Module.bsl
    Reports/<Name>/Forms/<Form>/Ext/Form.txt          the form description
    Reports/<Name>/Templates/<Template>/Ext/Template.txt   areas and parameters

A lone `Form.bin` is laid out as `Form/Module.bsl` and `Form/Form.txt`.

Read without running anything: a protected module or one shipped without its
source is reported as having no code, never guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from src.onec import brace
from src.onec.container import (
    DEFAULT_LIMITS,
    Container,
    ContainerError,
    Entry,
    Limits,
    read_container,
    text_of,
)

#: The platform's type of an event binding inside a form description: the
#: handler's name follows it, `{<event>,e1692cc2-…,{3,"<Handler>",…`.
_HANDLER = re.compile(
    r"e1692cc2-605b-4535-84dd-28440238746c,\s*\{\d+,\s*\"([^\W\d][\w]*)\"")
#: A named area of a MOXCEL template: `"<Name>",{1,{<kind>,…,<uuid>}`.
_AREA = re.compile(
    r"\"((?:[^\"]|\"\")+)\",\s*\{1,\s*\{[0-9],-?\d+,-?\d+,-?\d+,-?\d+,[0-9a-f-]{36}\}")
#: A cell parameter of a MOXCEL template: `{"<format>","<Parameter>"}`.
_PARAM = re.compile(r"\{\"(?:[^\"]|\"\")*\",\s*\"([^\W\d]\w*)\"\}")
#: Class ids of the two external object kinds.
_KINDS = {
    "e41aff26-25cf-4bb6-b6c1-3f478a75f374": "Reports",
    "c3831ec8-d8d5-4f93-8a22-f9bfae07327f": "DataProcessors",
}
_SAFE = re.compile(r"[^\w.\-]+")


@dataclass
class Module:
    path: str
    text: str | None  # None: the module exists but has no readable source
    owner: str        # "object" or the form's name
    note: str = ""


@dataclass
class Form:
    name: str
    synonyms: dict[str, str]
    handlers: list[str]
    module: Module | None
    description_path: str = ""


@dataclass
class Template:
    name: str
    kind: str  # "spreadsheet" | "text" | "binary" | "empty"
    areas: list[str]
    params: list[str]
    path: str = ""


@dataclass
class ExternalFile:
    file_name: str
    kind: str       # "Reports" | "DataProcessors" | "Form" | "Container"
    name: str
    synonyms: dict[str, str]
    comment: str
    created: datetime | None
    saved: datetime | None
    modules: list[Module] = field(default_factory=list)
    forms: list[Form] = field(default_factory=list)
    templates: list[Template] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    entries: int = 0
    texts: dict[str, str] = field(default_factory=dict)  # path → text (the tree)

    @property
    def label(self) -> str:
        return {"Reports": "external report", "DataProcessors": "external data processor",
                "Form": "ordinary form (Form.bin)"}.get(self.kind, "1C container")

    @property
    def base(self) -> str:
        """The dump-like folder of the object: `Reports/<Name>`, or `Form`."""
        return f"{self.kind}/{self.name}" if self.kind in _KINDS.values() else "Form"


def _safe(name: str, fallback: str) -> str:
    cleaned = _SAFE.sub("_", name or "").strip("._")
    return cleaned[:120] or fallback


def _module_text(entry: Entry | None) -> tuple[str | None, str]:
    """(text, note) of a module entry, which may be plain text or a container of
    `text` and the compiled `image`."""
    if entry is None:
        return None, "no module"
    if entry.children is not None:
        box = Container(entry.children)
        text_entry = box.get("text")
        if text_entry is None:
            return None, "compiled without source: there is no code to read"
        entry = text_entry
    if not entry.data:
        return "", ""
    text = text_of(entry.data)
    if text is None:
        return None, "protected or binary: there is no code to read"
    return text, ""


def _template(name: str, data: bytes) -> Template:
    if not data:
        return Template(name, "empty", [], [])
    if data[:6] == b"MOXCEL":
        txt = data.decode("utf-8", errors="replace")
        areas = sorted({m.replace('""', '"') for m in _AREA.findall(txt)}, key=str.lower)
        params = sorted(set(_PARAM.findall(txt)), key=str.lower)
        return Template(name, "spreadsheet", areas, params)
    text = text_of(data)
    if text is not None:
        return Template(name, "text", [], [])
    return Template(name, "binary", [], [])


def _form_parts(content: Entry | None) -> tuple[str | None, Entry | None]:
    """(form description text, module entry) of a form's `<uuid>.0`."""
    if content is None:
        return None, None
    if content.children is not None:
        box = Container(content.children)
        desc = box.get("form")
        return (text_of(desc.data) if desc else None), box.get("module")
    return text_of(content.data), None


def handlers_of(description: str | None) -> list[str]:
    """Names of the procedures a form description binds to events."""
    if not description:
        return []
    seen: dict[str, str] = {}
    for name in _HANDLER.findall(description):
        seen.setdefault(name.lower(), name)
    return list(seen.values())


def _lone_form(file_name: str, box: Container) -> ExternalFile:
    desc = box.get("form")
    text, note = _module_text(box.get("module"))
    out = ExternalFile(file_name=file_name, kind="Form", name=_safe(file_name, "Form"),
                       synonyms={}, comment="", created=None, saved=None,
                       entries=sum(1 for _ in box.walk()))
    out.created, out.saved = box.dates()
    description = text_of(desc.data) if desc else None
    module = Module("Form/Module.bsl", text, "Form", note)
    form = Form("Form", {}, handlers_of(description), module, "Form/Form.txt")
    out.forms.append(form)
    out.modules.append(module)
    if text is not None:
        out.texts[module.path] = text
    else:
        out.notes.append(f"form module: {note}")
    if description is not None:
        out.texts[form.description_path] = description
    return out


def inspect_bytes(data: bytes, file_name: str, limits: Limits = DEFAULT_LIMITS) -> ExternalFile:
    """Read ``data`` (an `.erf`, `.epf` or `Form.bin`). Raises ContainerError."""
    box = read_container(data, limits)
    if box.get("root") is None and box.get("form") is not None:
        return _lone_form(file_name, box)
    root = box.get("root")
    root_text = text_of(root.data) if root else None
    main = ""
    if root_text:
        m = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", root_text)
        main = m.group(0) if m else ""
    created, saved = box.dates()
    out = ExternalFile(file_name=file_name, kind="Container", name=_safe(file_name, "External"),
                       synonyms={}, comment="", created=created, saved=saved,
                       entries=sum(1 for _ in box.walk()))
    main_entry = box.get(main) if main else None
    main_text = text_of(main_entry.data) if main_entry else None
    if main_text is None:
        out.notes.append("no main object entry: listed as a plain container")
        for path, e in box.walk():
            t = text_of(e.data) if e.children is None else None
            if t is not None and e.name in ("text", "module"):
                out.texts[f"entries/{_safe(path, 'entry')}.bsl"] = t
        return out

    for cls, kind in _KINDS.items():
        if cls in main_text[:400]:
            out.kind = kind
    if out.kind == "Container":
        lower = file_name.lower()
        out.kind = "Reports" if lower.endswith(".erf") else "DataProcessors"
    head = brace.header_of(main_text)
    if head:
        out.name = _safe(head[0], out.name)
        out.synonyms, out.comment = head[1], head[2]
    base = out.base

    inner = box.get(f"{main}.0")
    if inner is not None:
        obj_text, obj_note = _module_text(inner)
        module = Module(f"{base}/Ext/ObjectModule.bsl", obj_text, "object", obj_note)
        out.modules.append(module)
        if obj_text is not None:
            out.texts[module.path] = obj_text
        else:
            out.notes.append(f"object module: {obj_note}")
    else:
        out.notes.append("no object module")

    taken: set[str] = set()
    for e in box.entries:
        if not brace.UUID.match(e.name) or e.name == main:
            continue
        meta = text_of(e.data)
        head = brace.header_of(meta or "", e.name) if meta else None
        content = box.get(f"{e.name}.0")
        name = _safe(head[0] if head else e.name, e.name)
        while name.lower() in taken:
            name += "_"
        if content is not None and content.children is not None and (
                Container(content.children).get("form") is not None):
            taken.add(name.lower())
            description, mod_entry = _form_parts(content)
            folder = f"{base}/Forms/{name}"
            text, note = _module_text(mod_entry)
            module = Module(f"{folder}/Ext/Form/Module.bsl", text, name, note)
            form = Form(name, head[1] if head else {}, handlers_of(description), module,
                        f"{folder}/Ext/Form.txt")
            out.forms.append(form)
            out.modules.append(module)
            if text is not None:
                out.texts[module.path] = text
            elif note != "no module":
                out.notes.append(f"form {name}: module {note}")
            if description is not None:
                out.texts[form.description_path] = description
        elif content is not None and content.children is None and (
                content.data[:6] == b"MOXCEL" or (head and content.data[:1] != b"{")):
            taken.add(name.lower())
            tpl = _template(name, content.data)
            tpl.path = f"{base}/Templates/{name}/Ext/Template.txt"
            out.templates.append(tpl)
            out.texts[tpl.path] = _template_text(tpl, content.data)
        elif head and content is not None and content.children is None and text_of(content.data):
            # a managed form keeps its description and module in one text: second phase
            taken.add(name.lower())
            out.notes.append(f"{name}: a managed form or another text entry; its module "
                             "is not extracted yet")
    return out


def _template_text(tpl: Template, data: bytes) -> str:
    if tpl.kind == "text":
        return text_of(data) or ""
    lines = [f"template {tpl.name} ({tpl.kind})"]
    if tpl.areas:
        lines.append("areas: " + ", ".join(tpl.areas))
    if tpl.params:
        lines.append("parameters: " + ", ".join(tpl.params))
    return "\n".join(lines) + "\n"


def try_inspect(data: bytes, file_name: str,
                limits: Limits = DEFAULT_LIMITS) -> tuple[ExternalFile | None, str]:
    """(file, "") or (None, why it cannot be read)."""
    try:
        return inspect_bytes(data, file_name, limits), ""
    except ContainerError as exc:
        return None, str(exc)
    except (ValueError, IndexError, UnicodeError) as exc:
        return None, f"unreadable container: {type(exc).__name__}"
