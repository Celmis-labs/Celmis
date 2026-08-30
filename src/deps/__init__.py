"""Dependency audit — declared library versions vs latest releases vs known
vulnerabilities (OSV.dev), across every registered repo in a workspace.

Deliberately LLM-free: registries and OSV are the source of truth, so the
report contains facts, not model guesses. The "Fix from here" hand-off is
where the agent comes in — after the human picks what to update.
"""
