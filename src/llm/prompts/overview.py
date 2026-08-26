"""Prompt for a short overview answer, when the vault notes by themselves give the answer."""

# Output language: the question's.
#
# A chat answer follows the person, not a setting: whoever asked chose the
# language of the question, and a workspace-wide default would be wrong for
# everyone else in the room. This is why Q&A does NOT go through
# `with_language()` — that is for generated documentation, which belongs to
# the repository rather than to whoever pressed the button. The line first
# written here was a hard-coded "You write in Ukrainian", which answered every
# question in Ukrainian regardless of what was asked.
#
# The rule NAMES NO LANGUAGE, and the omission is the fix. It used to read "a
# person who writes in German gets German back", and a review of the deployed
# product recorded an ENGLISH question answered entirely in German, twice out
# of two, on the no-code path. An instruction whose single worked example is a
# language is a mention of that language sitting in the context window, and a
# model with little else to go on can take a mention for an instruction.
#
# The second half — that the surrounding English is not a signal — is the
# other half of the same failure. Every indexed repository is English, so is
# every notice this pipeline injects, so is this file: on the degraded path
# the question can be the only non-English sentence in two thousand tokens.
# `language.py` says the same thing for generated docs ("recency wins when a
# long prompt asks for output in a different language"), which is why the rule
# is now also restated as the LAST line of the user prompt, where nothing sits
# beneath it to outvote it.

OVERVIEW_SYSTEM = """You answer questions about the system based on vault documentation.
Briefly. You reference specific notes.
OUTPUT LANGUAGE — mirror the question. Write the entire answer in the language the question is written in. These instructions, the notices, the retrieved notes and the code are all English; none of that is a signal about the answer, and the answer must not drift into the language of its surroundings. Keep identifiers, file paths, log lines and code exactly as they appear in the source — translate the prose around them, never the code.
"""

OVERVIEW_PROMPT = """# Previous conversation
{history}

# Question
{question}

# Available notes from vault

{notes}

# Task
Give a 2-5 sentence answer based on these notes. Reference notes as:
[[name]] — Obsidian wikilink format.

If the notes are not enough for an answer — say so directly: "There is not enough context
in the vault, the query needs to be refined or more documentation generated."

Do NOT invent facts beyond the notes.

# Language of the answer
Same language as the «Question» section above. Everything else in this prompt —
these instructions, the notes, the code — is English, and that is not what the
answer follows.
"""
