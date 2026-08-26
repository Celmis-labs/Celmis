"""Prompt for generating a per-module PRD-style document."""

# The output language is NOT set here. `with_language()` appends it as the last
# line — see src/llm/prompts/language.py. The line "Пишеш українською"
# ("You write in Ukrainian") that used to sit here made the documentation
# Ukrainian for every workspace, regardless of the language its people read
# it in.
MODULE_PRD_SYSTEM = """You are a code analyst who produces PRD/BRD-style documentation from code.
Style: structured, factual, no filler.
Never invent facts — if information is missing, write "not determined from code".
"""

MODULE_PRD_PROMPT = """# Task
Based on the provided code, dependency graph and security findings, produce module
documentation in Markdown format.

# Structure of the output document

## Purpose
2-4 sentences: what this module does at a high level. Business purpose, not technical.

## Public API
List of exported symbols (functions, classes, constants).
For each — the signature and 1 sentence on its purpose.

## Internal Architecture
Briefly: key functions and how they are connected. 3-6 bullet points.

## Business Rules
Business rules extracted from the code: validations, constraints, edge cases.
Format: bullet list. If there are no rules — say so.

## Data Flow
How data gets into the module and what happens to it. 2-3 sentences or a diagram.

## Dependencies
- External (npm/pypi/etc): list
- Internal (other modules of the project): list

## Security Considerations
If there are Semgrep findings — describe each one: what, where (file:line), severity, recommendation.
If there are none — "Not detected".

## Integration Notes
How other code should use this module. 1-3 call examples,
if the code has JSDoc/docstring — use it.

# Important
- Do NOT add sections that contain nothing — skip them.
- Do NOT include full code in the document. Only short snippets (1-3 lines) for examples.
- Code references: [file](relative/path.ts#L42) markdown format.
"""
