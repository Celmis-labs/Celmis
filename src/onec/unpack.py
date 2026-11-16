"""A 1C file attached to an agent session, made readable for the agent.

The agent has Read and no shell, so an `.erf` arrives as bytes it can do
nothing with. Unpacked next to it, as `<name>.d/`, it becomes text files laid
out like a configuration dump plus a `REPORT.md`: what the file is, how it
compares with the session's repositories that are configuration dumps, and the
static checks. Nothing is run.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from src.onec.checks import check
from src.onec.compare import compare
from src.onec.config import Config, DirSource
from src.onec.container import DEFAULT_LIMITS, is_container
from src.onec.external import ExternalFile, try_inspect
from src.onec.report import render

logger = logging.getLogger(__name__)

#: What an attachment may be to be unpacked: external reports and data
#: processors, and an ordinary form's `Form.bin`.
SUFFIXES = {".erf", ".epf", ".bin"}
MAX_BYTES = 20 * 1024 * 1024
REPORT = "REPORT.md"


def _pick(ext: ExternalFile, roots: list[Path]):  # noqa: ANN202
    """(config, comparison) against the first dump holding the counterpart,
    else the first dump; (None, None) when no root is a dump."""
    first = None
    for root in roots:
        cfg = Config(DirSource(root), label=f"the session's repository {root.name}")
        if not cfg.ok:
            continue
        c = compare(ext, cfg)
        if c.counterpart is not None:
            return cfg, c
        first = first or (cfg, c)
    return first or (None, None)


def unpack(path: Path, repo_dirs: list[Path]) -> str | None:
    """Unpack the attachment at ``path``; the report's path relative to the
    attachments directory's parent, or None when it is not a 1C container."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_BYTES or not is_container(data):
        return None
    out = path.parent / f"{path.name}.d"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    rel_out = f"{path.parent.name}/{out.name}"
    ext, why = try_inspect(data, path.name, DEFAULT_LIMITS)
    if ext is None:
        (out / REPORT).write_text(f"external file, not from the index: {path.name}\n"
                                  f"could not be read: {why}\n", encoding="utf-8")
        return f"{rel_out}/{REPORT}"
    base = out.resolve()
    for rel, text in ext.texts.items():
        target = (out / rel).resolve()
        if not target.is_relative_to(base):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    try:
        cfg, comparison = _pick(ext, [d for d in repo_dirs if d.is_dir()])
        checks = check(ext, cfg)
    except Exception as exc:  # noqa: BLE001 - the unpacked code is still worth having
        logger.warning("onec_attachment_check_failed file=%s err=%s", path.name, exc)
        cfg = comparison = checks = None
    hint = (f"the file's code is unpacked under `{rel_out}/` with the paths of a "
            "configuration dump; read it with Read and compare with the repository")
    report = render(ext, comparison, checks, read_hint=hint,
                    no_config="no repository of this session is a configuration dump, "
                              "so nothing was compared")
    (out / REPORT).write_text(report, encoding="utf-8")
    return f"{rel_out}/{REPORT}"


def describe_attachment(rel: str, report: str | None) -> str:
    """How the agent is told about an attachment."""
    if report is None:
        return rel
    return (f"{rel} (a 1C file, unpacked: read {report} first; the code is beside it)")
