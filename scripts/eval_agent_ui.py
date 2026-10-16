#!/usr/bin/env python3
"""Live evaluation: does the Celmis agent pick the right screen and fields?

NOT part of CI — it calls a real model. It runs a fixed set of realistic setup
questions, in English, Ukrainian and Russian, through the real planner
(`src.automation.chat.interpret`, so the real system prompt, catalogue and
knowledge) and then through the same validation the app applies
(`ui_catalogue.build_ui_action`) against a made-up repository list. For each
question it checks which verb the model chose, which destination, which
repository (after fuzzy resolution) and which fields it asked to spotlight.

The model is any OpenAI-compatible endpoint, set from the environment so
nothing about a particular gateway lives in the repository:

    CELMIS_EVAL_BASE_URL   e.g. https://gateway.example.com/v1
    CELMIS_EVAL_API_KEY    the key for it
    CELMIS_EVAL_MODEL      a small, cheap model name

    python scripts/eval_agent_ui.py            # all cases, one run each
    python scripts/eval_agent_ui.py -k prompts # only cases whose id contains it
    python scripts/eval_agent_ui.py --runs 2   # repeat to see flakiness

Cost: each case sends one planner prompt (about 12k tokens) and gets back a few
hundred; the whole set is a small fraction of a dollar on a flash-class model.
The exit code is the number of failed cases, so it can gate a manual release
check. Nothing here touches a database or the network except the model call.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPOS = [
    {"repo": "acme-shop-api", "full_name": "acme/shop-api"},
    {"repo": "acme-shop-web", "full_name": "acme/shop-web"},
    {"repo": "acme-billing", "full_name": "acme/billing"},
    {"repo": "acme-docs", "full_name": "acme/docs"},
    {"repo": "tools-cli", "full_name": "tools/cli"},
]

EDITOR = {"role": "editor"}
VIEWER = {"role": "viewer"}
ADMIN = {"role": "admin"}


@dataclass
class Case:
    id: str
    question: str
    #: Expected destination of the show_in_ui step; None = NO show_in_ui at all.
    dest: str | None
    #: Expected resolved repo slug ("" = workspace-wide, None = not checked).
    repo: str | None = ""
    #: At least one of these focus keys must be among the spotlighted fields.
    any_focus: tuple[str, ...] = ()
    #: Expected error of the resolution (e.g. repo_ambiguous).
    error: str | None = None
    mode: str | None = None
    caller: dict = field(default_factory=lambda: dict(EDITOR))
    #: The plan must also contain a `help` step (a knowledge question).
    needs_help: bool = False


CASES = [
    Case("prompts-en", "I need to add custom prompts for repo shop-api",
         "review_settings.prompts", "acme-shop-api",
         ("base_instruction", "prompt_guidelines")),
    Case("prompts-uk", "Мені треба додати кастомні промпти для репозиторію shop-api",
         "review_settings.prompts", "acme-shop-api",
         ("base_instruction", "prompt_guidelines")),
    Case("guidelines-security-en", "add a guideline for the security agent in billing",
         "review_settings.prompts", "acme-billing", ("prompt_guidelines",)),
    Case("ignore-lock-uk", "де вимкнути перевірку lock-файлів у acme/docs?",
         "review_settings.filters", "acme-docs", ("ignore_globs",)),
    Case("severity-en", "where do I change the severity threshold for the web repo?",
         "review_settings.filters", "acme-shop-web", ("comment_min_severity",)),
    Case("verifier-en", "how can I turn on the verifier agent in acme/docs?",
         "review_settings.categories", "acme-docs", ("verifier_enabled",)),
    Case("agent-model-uk", "змінити модель для агента безпеки в acme/docs",
         "review_settings.categories", "acme-docs",
         ("agent_model", "agent_reasoning", "agent_model_toggle")),
    Case("advanced-prompts-en", "what do the advanced settings give when configuring prompts?",
         "review_settings.prompts", None, ("prompt_replace", "prompt_extend",
                                           "prompt_preview", "prompt_guidelines",
                                           "base_instruction"),
         needs_help=True),
    Case("slack-en", "we want failed-review alerts in our Slack, where do I set that up?",
         "notifications", "", ("notifications_channel", "notifications_binding")),
    Case("github-token-uk", "куди вставити токен GitLab, щоб підключити репозиторії?",
         "connections", "", ("connection_provider",)),
    Case("memory-en", "I want to teach the reviewer that we never use moment.js, where do I write that down?",
         "memories", "", ("memories_add",)),
    Case("ambiguous-en", "I want custom prompts for the shop repo",
         "review_settings.prompts", None, (), error="repo_ambiguous"),
    Case("drafts-en", "where can I enable review of draft pull requests in acme-docs?",
         "review_settings.general", "acme-docs", ("run_on_drafts",)),
    Case("max-comments-uk", "де обмежити кількість коментарів у billing?",
         "review_settings.filters", "acme-billing", ("max_inline_comments",)),
    Case("mcp-en", "add an MCP evidence source for acme/docs",
         "review_settings.advanced", "acme-docs", ("mcp_sources",)),
    Case("api-key-en", "I need to configure our Anthropic API key for the workspace",
         "llm", "", ("llm_provider_keys",), caller=dict(ADMIN)),
    Case("autopause-ru", "как включить автопаузу при частых пушах в acme-docs?",
         "review_settings.general", "acme-docs",
         ("review_cadence", "auto_pause_pushes")),
    Case("viewer-link-en", "I need custom prompts for the docs repo",
         "review_settings.prompts", "acme-docs", ("base_instruction", "prompt_guidelines"),
         mode="link", caller=dict(VIEWER)),
    Case("explicit-order-en", "set the severity threshold to warning for acme/web",
         None, None),  # an order with a value is a write plan, not a tour
    Case("no-ui-en", "what is Celmis?", None, None),
]


def _install_model(base_url: str, api_key: str, model: str) -> None:
    """Replace the gateway client with a plain OpenAI-compatible call."""
    import litellm

    import src.llm.client as llm_client

    def build(*_a, **_k):
        def generate(*, prompt, system_instruction=None, temperature=0.0,
                     max_output_tokens=None, on_delta=None, **_kw):
            resp = litellm.completion(
                model=f"openai/{model}", api_base=base_url, api_key=api_key,
                temperature=temperature or 0, max_tokens=max_output_tokens,
                messages=[{"role": "system", "content": system_instruction or ""},
                          {"role": "user", "content": prompt}],
            )
            return types.SimpleNamespace(text=resp.choices[0].message.content or "")
        return types.SimpleNamespace(generate=generate)

    llm_client.build_llm_client = build  # type: ignore[assignment]


def run_case(case: Case) -> tuple[bool, str]:
    from src.automation import ui_catalogue as cat
    from src.automation.chat import interpret

    plan = interpret(case.question, workspace_id="eval", user_id="eval",
                     caller=case.caller)
    if not plan.steps:
        print(f"      note: {plan.note[:200]}")
    ui_steps = [s for s in plan.steps if s.action == "show_in_ui"]
    verbs = [s.action for s in plan.steps]
    if case.dest is None:
        ok = not ui_steps
        return ok, f"verbs={verbs}" + ("" if ok else " (expected no show_in_ui)")
    if not ui_steps:
        return False, f"verbs={verbs} (no show_in_ui)"
    args = ui_steps[0].arguments
    out = cat.build_ui_action(
        destination=args.get("destination"), repo=args.get("repo"),
        focus=args.get("focus"), repos=REPOS, caller=case.caller)
    problems = []
    if case.needs_help and "help" not in verbs:
        problems.append("no help step")
    if case.error:
        if out.get("error") != case.error:
            problems.append(f"error={out.get('error')!r}, wanted {case.error!r}")
        got_dest = cat.resolve_destination(args.get("destination"))
        if got_dest is None or got_dest.id != case.dest:
            problems.append(f"destination={args.get('destination')!r}")
        return not problems, f"verbs={verbs} {args.get('destination')} repo={args.get('repo')!r} -> {out.get('error')}" + (
            f" [{'; '.join(problems)}]" if problems else "")
    if not out.get("ok"):
        return False, f"resolution failed: {out.get('error')} args={json.dumps(args, ensure_ascii=False)[:160]}"
    if out["destination"] != case.dest:
        problems.append(f"destination={out['destination']}, wanted {case.dest}")
    if case.repo is not None and out["repo"] != case.repo:
        problems.append(f"repo={out['repo']!r}, wanted {case.repo!r}")
    keys = [s["target"] for s in out["steps"]]
    wanted = {cat.FIELDS_BY_KEY[k].anchor.split(":")[0] for k in case.any_focus}
    if case.any_focus and not any(t.split(":")[0] in wanted for t in keys):
        problems.append(f"focus={keys}, wanted one of {list(case.any_focus)}")
    if case.mode and out["mode"] != case.mode:
        problems.append(f"mode={out['mode']}, wanted {case.mode}")
    return not problems, (
        f"verbs={verbs} {out['destination']} repo={out['repo']!r} "
        f"mode={out['mode']} focus={keys}" + (f" [{'; '.join(problems)}]" if problems else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-k", default="", help="only cases whose id contains this")
    ap.add_argument("--runs", type=int, default=1)
    args = ap.parse_args()

    base = os.environ.get("CELMIS_EVAL_BASE_URL", "")
    key = os.environ.get("CELMIS_EVAL_API_KEY", "")
    model = os.environ.get("CELMIS_EVAL_MODEL", "")
    if not (base and key and model):
        print("Set CELMIS_EVAL_BASE_URL, CELMIS_EVAL_API_KEY and CELMIS_EVAL_MODEL.",
              file=sys.stderr)
        return 2
    _install_model(base, key, model)

    failed = 0
    total = 0
    for case in CASES:
        if args.k not in case.id:
            continue
        for _run in range(args.runs):
            total += 1
            try:
                ok, detail = run_case(case)
            except Exception as exc:  # noqa: BLE001 - report, keep going
                ok, detail = False, f"raised {type(exc).__name__}: {str(exc)[:120]}"
            failed += 0 if ok else 1
            print(f"{'PASS' if ok else 'FAIL'}  {case.id:<24} {case.question[:56]!r}\n      {detail}")
    print(f"\n{total - failed}/{total} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
