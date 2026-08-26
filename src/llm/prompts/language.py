"""What language generated output is written in.

Every documentation prompt said "Пишеш українською" ("You write in Ukrainian")
in its system instruction, so a workspace whose people read German got
Ukrainian documentation and had no way to say otherwise. The interface has
sixteen locales; the thing the product actually produces had one.

The prompts are English (they were Ukrainian when this was written), but the
directive is separate from them either way: translating a prompt into every
supported language would be that many times the surface for a wording change,
and that many chances for a translation to quietly mean something else.

So the language is one directive, appended last. Last because recency wins when
a long prompt asks for output in a different language — a directive at the top
gets outvoted by the hundred lines beneath it.

The supported set lives here rather than beside each consumer. Review comments
already had their own copy of it, and two lists drift: a language offered for
review comments but not for documentation is a dropdown that silently means
something different depending on which page you opened.
"""

from __future__ import annotations

from typing import Final

#: Code → English name. The set of languages any generated output can use.
#:
#: Codes are what the UI stores and what the review pipeline has used in
#: production; do not renumber them.
LANGUAGE_NAMES: Final[dict[str, str]] = {
    "en": "English", "uk": "Ukrainian", "pl": "Polish", "de": "German",
    "fr": "French", "es": "Spanish", "pt": "Portuguese", "it": "Italian",
    "nl": "Dutch", "cs": "Czech", "sk": "Slovak", "ro": "Romanian",
    "hu": "Hungarian", "bg": "Bulgarian", "el": "Greek", "tr": "Turkish",
    "sv": "Swedish", "no": "Norwegian", "da": "Danish", "fi": "Finnish",
    "et": "Estonian", "lv": "Latvian", "lt": "Lithuanian", "hr": "Croatian",
    "sr": "Serbian", "ka": "Georgian", "he": "Hebrew", "ar": "Arabic",
    "hi": "Hindi", "vi": "Vietnamese", "th": "Thai", "id": "Indonesian",
    "ms": "Malay", "ja": "Japanese", "ko": "Korean",
    "zh-CN": "Simplified Chinese", "zh-TW": "Traditional Chinese",
}

#: The phrase in the language itself, where we have one.
#:
#: A stronger anchor than an English exonym — "auf Deutsch" sits in the target
#: language and pulls the model into it, while "in German" is a description of
#: the target from outside it. Partial on purpose: a wrong native phrase is
#: worse than none, so this only covers what can be stated confidently, and
#: everything else falls back to the English name alone.
NATIVE_PHRASES: Final[dict[str, str]] = {
    "uk": "українською мовою",
    "en": "in English",
    "de": "auf Deutsch",
    "fr": "en français",
    "es": "en español",
    "it": "in italiano",
    "pt": "em português",
    "nl": "in het Nederlands",
    "pl": "po polsku",
    "cs": "česky",
    "sk": "po slovensky",
    "ro": "în română",
    "tr": "Türkçe",
    "ja": "日本語で",
    "ko": "한국어로",
    "zh-CN": "用简体中文",
    "zh-TW": "用繁體中文",
}

#: What a workspace gets when it has never chosen. Ukrainian, because that is
#: what every prompt hardcoded before this existed — any other default would
#: silently reword every existing vault on its next regeneration.
DEFAULT_DOC_LANGUAGE: Final[str] = "uk"

#: Kept as the public name for the supported set.
DOC_LANGUAGES: Final[dict[str, str]] = LANGUAGE_NAMES


def is_supported(language: str | None) -> bool:
    return bool(language) and language in LANGUAGE_NAMES


def normalise(language: str | None) -> str:
    """A usable language code, never an exception.

    Generation runs in a background worker; a value stored months ago must not
    take a run down. An unknown code falls back to the default and the caller
    logs it.
    """
    if not language:
        return DEFAULT_DOC_LANGUAGE
    raw = language.strip()
    if raw in LANGUAGE_NAMES:
        return raw
    # "de-AT" → "de", "en_GB" → "en". Chinese is deliberately excluded from
    # this collapse: zh-CN and zh-TW are different scripts, and folding them to
    # a bare "zh" would silently pick one.
    base = raw.replace("_", "-").split("-")[0].lower()
    if base == "zh":
        return "zh-CN" if raw.lower().endswith("cn") else "zh-TW"
    return base if base in LANGUAGE_NAMES else DEFAULT_DOC_LANGUAGE


def language_directive(language: str | None) -> str:
    """The instruction appended to a system prompt."""
    code = normalise(language)
    english = LANGUAGE_NAMES[code]
    native = NATIVE_PHRASES.get(code)
    target = f"{native} ({english})" if native else english
    return (
        f"\n\nOUTPUT LANGUAGE: write the entire document {target}. This "
        f"overrides the language of these instructions. Keep identifiers, "
        f"file paths, code and log lines exactly as they appear in the "
        f"source — translate the prose around them, never the code."
    )


def with_language(system_prompt: str, language: str | None) -> str:
    """A system prompt that asks for `language`.

    Appended rather than substituted so the prompt files stay readable as
    prose, and so a prompt that forgets to carry a placeholder still gets the
    directive.
    """
    return system_prompt.rstrip() + language_directive(language)


__all__ = [
    "DEFAULT_DOC_LANGUAGE",
    "DOC_LANGUAGES",
    "LANGUAGE_NAMES",
    "NATIVE_PHRASES",
    "is_supported",
    "language_directive",
    "normalise",
    "with_language",
]
