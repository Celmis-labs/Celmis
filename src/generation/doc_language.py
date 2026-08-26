"""Which language a workspace's generated documentation is written in.

Stored beside the workspace's LLM configuration rather than in a new table.
That blob is already workspace-scoped, already encrypted at rest, already
carries "how this workspace generates things", and needs no migration — and a
migration for one string is a schema change every deployment has to survive in
order to hold a value that fits in a dropdown.

The resolution order is the whole design:

    explicit argument  →  workspace setting  →  DEFAULT_DOC_LANGUAGE

An argument wins because "generate this one in English for the customer" is a
real request that must not require changing a setting and changing it back. The
workspace setting exists because documentation for a repository should not
depend on who happened to press the button.
"""

from __future__ import annotations

import logging

from src.llm.prompts.language import (
    DEFAULT_DOC_LANGUAGE,
    DOC_LANGUAGES,
    normalise,
)

logger = logging.getLogger(__name__)

#: Key inside the workspace LLM config blob.
CONFIG_KEY = "docs_language"


def get_workspace_language(workspace_id: str = "default") -> str:
    """The workspace's configured documentation language.

    Never raises. Generation runs in a background worker, and a credential
    store that is briefly unreachable must not take a vault build down — it
    falls back to the default and says so in the log.
    """
    try:
        from src.api.routers.llm import _load_workspace_config

        blob = _load_workspace_config(workspace_id) or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("docs_language_load_failed ws=%s err=%s", workspace_id, exc)
        return DEFAULT_DOC_LANGUAGE

    stored = blob.get(CONFIG_KEY)
    if stored and normalise(stored) != stored:
        # Either an unknown code or a regional tag. Worth a line: silently
        # writing German when the row says "de-AT" is fine, silently writing
        # Ukrainian when it says "xx" is a setting that does nothing.
        logger.info("docs_language_normalised ws=%s stored=%s used=%s",
                    workspace_id, stored, normalise(stored))
    return normalise(stored)


def set_workspace_language(language: str, workspace_id: str = "default") -> str:
    """Persist the workspace's documentation language. Returns what was stored.

    Raises ValueError on an unsupported code — this one IS a user action with a
    UI to show the error in, unlike the read path.
    """
    if language not in DOC_LANGUAGES:
        raise ValueError(
            f"unsupported documentation language {language!r}; "
            f"expected one of {', '.join(sorted(DOC_LANGUAGES))}"
        )
    from src.api.routers.llm import _load_workspace_config, _save_workspace_config

    blob = _load_workspace_config(workspace_id) or {}
    blob[CONFIG_KEY] = language
    _save_workspace_config(blob, updated_by="docs_language", workspace_id=workspace_id)
    logger.info("docs_language_set ws=%s language=%s", workspace_id, language)
    return language


def resolve_doc_language(
    override: str | None = None, workspace_id: str = "default",
) -> str:
    """The language this particular generation run should write in."""
    if override:
        resolved = normalise(override)
        if resolved != normalise(str(override)):  # pragma: no cover - defensive
            logger.info("docs_language_override_normalised raw=%s", override)
        return resolved
    return get_workspace_language(workspace_id)


__all__ = [
    "CONFIG_KEY",
    "ENGINE_CONFIG_KEY",
    "get_workspace_engine",
    "get_workspace_language",
    "resolve_doc_engine",
    "resolve_doc_language",
    "set_workspace_engine",
    "set_workspace_language",
]


# ─── which engine writes the documentation ───────────────────────────
#
# Same resolution order as the language, and for the same reason: a run-level
# choice must beat the workspace, because the real workflow is "the API engine
# swept the whole repository, now let the agent do the five modules that
# matter". A setting you have to change and change back does not support that.

ENGINE_CONFIG_KEY = "docs_engine"


def get_workspace_engine(workspace_id: str = "default") -> str:
    """The workspace's configured documentation engine. Never raises."""
    from src.generation.engines import DEFAULT_ENGINE, ENGINES

    try:
        from src.api.routers.llm import _load_workspace_config

        blob = _load_workspace_config(workspace_id) or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("docs_engine_load_failed ws=%s err=%s", workspace_id, exc)
        return DEFAULT_ENGINE
    stored = str(blob.get(ENGINE_CONFIG_KEY) or "")
    if stored and stored not in ENGINES:
        logger.info("docs_engine_unknown ws=%s stored=%s — using %s",
                    workspace_id, stored, DEFAULT_ENGINE)
    return stored if stored in ENGINES else DEFAULT_ENGINE


def set_workspace_engine(engine: str, workspace_id: str = "default") -> str:
    """Persist the workspace's documentation engine."""
    from src.generation.engines import ENGINES

    if engine not in ENGINES:
        raise ValueError(
            f"unsupported documentation engine {engine!r}; "
            f"expected one of {', '.join(ENGINES)}")
    from src.api.routers.llm import _load_workspace_config, _save_workspace_config

    blob = _load_workspace_config(workspace_id) or {}
    blob[ENGINE_CONFIG_KEY] = engine
    _save_workspace_config(blob, updated_by="docs_engine", workspace_id=workspace_id)
    logger.info("docs_engine_set ws=%s engine=%s", workspace_id, engine)
    return engine


def resolve_doc_engine(
    override: str | None = None, workspace_id: str = "default",
) -> str:
    """The engine this particular generation run should use."""
    from src.generation.engines import ENGINES

    if override:
        if override in ENGINES:
            return override
        logger.info("docs_engine_override_unknown value=%s — ignoring", override)
    return get_workspace_engine(workspace_id)
