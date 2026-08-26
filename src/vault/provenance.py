"""What produced this document, recorded on the document.

A vault note is handed to a new developer, shown to an auditor, and committed
to a repository. It outlives the subscription that produced it — that is the
best thing about it — which means it also outlives every place we could
otherwise have kept the answer to "where did this come from".

So the answer travels with the file. Not a watermark: a block a person can read
and a machine can parse, saying who generated it, from which commit, with which
engine and model, and in what language.

Three separate reasons, and each would justify it on its own:

  It is a compliance artefact now. The CRA asks for technical documentation
  beside the inventory, and a document that cannot say what produced it or from
  which revision is weaker evidence than one that can — the same argument that
  puts sha256 sums in the evidence pack.

  Documentation is where a model is freest to invent. A reader who knows a
  document came from the api engine — one prompt, no lookups — should weigh it
  differently from one written by the agent after twelve queries against the
  index. Recording `tools_used` is what makes that difference visible instead
  of a matter of trust.

  Somebody will eventually ask why two documents in one vault disagree. Usually
  the answer is that they were generated months apart, from different commits,
  by different models. Without this it is unanswerable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

#: The mark itself. Deliberately the product name and not a URL: a document
#: pasted into a wiki or printed to PDF should still say what made it, and a
#: link is the first thing to rot.
GENERATOR = "celmis"


#: The distribution names in pyproject.toml, newest first.
#:
#: This comment used to read "the distribution name, which is NOT the product
#: name — looking up 'celmis' returned 'unknown' on every document". That was
#: true until the project was renamed to `celmis` before its first tag, and it
#: is now exactly backwards: "celmis" is the answer and the old name is the
#: fallback, kept because a container built before the rename still carries it.
#:
#: The point the old comment was making still stands, which is why the
#: fallback exists at all: a version field that is always the same string is
#: worse than no field, because it looks like an answer.
_DISTRIBUTIONS = ("celmis", "code-analysis-system")


#: The value a broken build bakes into its own metadata.
#:
#: `pyproject.toml` reads `src.__version__`, and while that attribute was
#: itself an `importlib.metadata` lookup the two chased each other: setuptools
#: evaluated it before the install existed, got this string, and wrote it into
#: the metadata that the lookup then read back. Reading it here would put
#: "0.0.0+unknown" on every generated document — the exact failure the comment
#: above `_DISTRIBUTIONS` describes, one indirection later.
_FIXED_POINT = "0.0.0+unknown"


def _version() -> str:
    """The running version, or "unknown" — never an exception.

    This block is written during a long background build, and a missing
    version must not cost somebody their documentation.

    Metadata first, the literal in `src/__init__.py` second. The literal is
    readable without an install, which is exactly the case metadata cannot
    answer — a source checkout, a test, or an image whose metadata was built
    from the loop described above.
    """
    installed = ""
    try:
        from importlib.metadata import PackageNotFoundError, version

        for _dist in _DISTRIBUTIONS:
            try:
                installed = version(_dist)
                break
            except PackageNotFoundError:
                installed = ""
    except Exception:  # noqa: BLE001
        installed = ""
    if installed and not installed.startswith(_FIXED_POINT):
        return installed
    try:
        from src import __version__
        return __version__ or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def build(
    *,
    engine: str | None = None,
    model: str | None = None,
    language: str | None = None,
    tools_used: list[str] | None = None,
    commit: str | None = None,
) -> dict[str, Any]:
    """The provenance block for one generated document.

    Absent fields are omitted rather than written as null: a frontmatter key
    with no value reads as "we checked and there is none", which is a different
    claim from "this predates the field".
    """
    block: dict[str, Any] = {
        "generated_by": GENERATOR,
        "version": _version(),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    if engine:
        block["engine"] = engine
    if model:
        block["model"] = model
    if language:
        block["language"] = language
    if commit:
        block["commit"] = commit
    if tools_used is not None:
        # The count, not the list: the list is long and repetitive, and what a
        # reader needs is whether the document was researched at all. Zero on
        # the agent engine means it answered from the prompt — which is the one
        # thing that engine exists to prevent, and now it is on the document.
        block["research_calls"] = len(tools_used)
        if tools_used:
            block["tools_used"] = sorted(set(tools_used))
    return block


def _public_model(model: Any) -> str:
    """The model name, with this installation's own identifiers taken out.

    Routed through a LiteLLM gateway, a deployment is named
    `litellm_proxy/celmis-<workspace-uuid>-chat`, and the footer put that
    verbatim into a document people download and forward. The reader gains
    nothing from the workspace id; the sender loses an internal identifier
    into a Word file attached to an email.

    What survives is the part that answers "which model wrote this": the
    provider prefix and the role. A deployment name that carries no id is
    passed through untouched.
    """
    name = str(model)
    head, _, tail = name.rpartition("/")
    for prefix in ("celmis-",):
        if tail.startswith(prefix):
            rest = tail[len(prefix):]
            # <uuid>-<role> → keep the role, drop the id.
            role = rest.rsplit("-", 1)[-1] if "-" in rest else ""
            tail = f"{prefix}{role}" if role else prefix.rstrip("-")
            break
    return f"{head}/{tail}" if head else tail


def as_footer(block: dict[str, Any]) -> str:
    """One line for the bottom of an exported document.

    Exports go to people who will never open the frontmatter — a Word file
    attached to an email, a PDF in a filing. The mark has to survive that trip
    or it only marks the copies nobody hands over.
    """
    parts = [f"Generated by {block.get('generated_by', GENERATOR)}"]
    if block.get("version") and block["version"] != "unknown":
        parts[0] += f" {block['version']}"
    if block.get("engine"):
        parts.append(f"engine: {block['engine']}")
    if block.get("model"):
        parts.append(f"model: {_public_model(block['model'])}")
    if block.get("commit"):
        parts.append(f"commit: {str(block['commit'])[:8]}")
    if block.get("generated_at"):
        parts.append(str(block["generated_at"])[:19].replace("T", " ") + " UTC")
    if block.get("research_calls") is not None:
        parts.append(f"{block['research_calls']} index lookups")
    return " · ".join(parts)


__all__ = ["GENERATOR", "as_footer", "build"]
