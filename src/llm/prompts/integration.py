"""Prompt for the integration guide — how to call a service/module from outside."""

# Output language — via `with_language()`, see src/llm/prompts/language.py.
INTEGRATION_SYSTEM = """You are a developer advocate writing integration guides for someone else's code.
The target audience is developers who are not familiar with the system.
Focus on practice: a call example, prerequisites, error handling.
"""

INTEGRATION_PROMPT = """# Task
Based on the code, produce an integration guide for a specific service/module.

# Structure

## Service: <name>
1-2 sentences: what the service does, when to call it.

## Entry Points
Table:
| Function / Endpoint | Signature | Purpose |
|---|---|---|

## Prerequisites
- Dependencies (which modules must be imported / which env vars are needed)
- Authentication requirements
- Initialization state (if .init() etc. is required)

## Usage Example
```<language>
// Minimal working call example.
// Base it on the real signatures from the code.
```
Explanation of the example: 2-3 sentences.

## Parameters Detail
For each entry point parameter:
- name, type, required/optional, default, constraints (from validations in the code)

## Response Shape
What the call returns — with types. Links to the types in the code if there are any.

## Error Handling
Typical errors the caller can get, what they mean, how to handle them.

## Edge Cases & Gotchas
Non-obvious things — rate limits, async/sync, thread safety, side effects.

# Important
- The code example MUST be compilable/runnable (based on the real signatures).
- File links: [name](path#L42)
- Do not invent endpoints / params that are not in the code.
"""
