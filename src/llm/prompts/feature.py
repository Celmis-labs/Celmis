"""Prompt for the cross-module feature PRD."""

# Output language — via `with_language()`, see src/llm/prompts/language.py.
FEATURE_SYSTEM = """You are a product analyst who extracts a PRD from code.
Target a business reader (PM, BA) — fewer technical details, more about system behaviour.
"""

FEATURE_PROMPT = """# Task
Based on the code that implements a specific feature (spread across several modules),
produce a PRD document.

# Structure

## Feature: <name>
One sentence — what the feature does from the user's point of view.

## User Story
As a user, I want <action>, so that <goal>.

## Preconditions
State of the system/user required before the feature runs.

## Main Flow (Happy Path)
Numbered list of steps. Each step — 1-2 sentences.
In parentheses, a link to the code: (see [`functionName`](file.ts#L42))

## Alternative Flows
- Edge case 1: what happens
- Edge case 2: ...

## Business Rules
Rules extracted from the code: limits, validations, blocks.
State where each rule comes from (file:line).

## Error Handling
How the system reacts to errors: network, validation, auth.

## Security & Privacy
What is sensitive in this feature: PII, credentials, external calls.

## Metrics / Analytics
If the code has event tracking — list it. Otherwise "Not detected".

# Important
- Business language, not "calls handleSubmit" but "the form is submitted".
- If something is not in the code — "not defined", do not invent it.
- Links to code: markdown-links.
"""
