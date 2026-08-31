"""Run eslint over the web app and decide whether the build may pass.

WHY A RATCHET RATHER THAN A CLEAN BILL. There are 42 findings today. A gate
that demanded zero would be turned off within a week, and a gate that is off
is what this replaces: `ci.yml` said the rules-of-hooks rule was "enforced as
a TEST ... so it is blocking there rather than advisory here", and it was
blocking nowhere. The tests that would have enforced it carry
`skipif(not _eslint_available())`, and the python job never installs
`web/node_modules` — so in CI they reported SKIPPED, in green.

Two rules, and they are different in kind:

  * `react-hooks/rules-of-hooks` is FATAL at any count. It is the rule whose
    violation once took every authenticated page down with React error #310,
    and there are zero today. One is a regression, not a trend.

  * everything else RATCHETS from a measured baseline. Findings may fall, and
    the baseline follows them down; they may not rise. The number is written
    down here rather than inferred, so lowering it is a visible edit.

The count is read from eslint's JSON, not from its exit status: eslint exits
non-zero when it reports anything at all, including warnings, so the status
alone cannot tell "42 known findings" from "43".
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import subprocess
import sys

WEB = pathlib.Path(__file__).resolve().parents[1] / "web"

#: Measured 2026-08-31 on `eslint app components lib`: 42 findings across 9
#: rules, 31 of them errors. Lower this when the number falls; raising it is
#: the change this file exists to make somebody argue for.
BASELINE = 42

#: Zero tolerated, whatever the baseline says. The outage rule.
FATAL_RULES = ("react-hooks/rules-of-hooks",)

TARGETS = ("app", "components", "lib")


def run_eslint() -> list[dict]:
    binary = WEB / "node_modules" / ".bin" / "eslint"
    if not binary.exists():
        print(f"eslint is not installed at {binary}. Run `pnpm install` in web/.",
              file=sys.stderr)
        raise SystemExit(2)
    proc = subprocess.run(
        [str(binary), *TARGETS, "--format", "json"],
        cwd=WEB, capture_output=True, text=True, timeout=900, check=False,
    )
    if not proc.stdout.strip():
        print(f"eslint produced no JSON. stderr:\n{proc.stderr[:2000]}", file=sys.stderr)
        raise SystemExit(2)
    return json.loads(proc.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=int, default=BASELINE,
                        help="maximum total findings tolerated")
    args = parser.parse_args()

    report = run_eslint()
    rules: collections.Counter[str] = collections.Counter()
    fatal: list[str] = []
    errors = 0
    for entry in report:
        for message in entry.get("messages", []):
            rule = message.get("ruleId") or "(parse error)"
            rules[rule] += 1
            if message.get("severity") == 2:
                errors += 1
            if rule in FATAL_RULES:
                fatal.append(
                    f"{entry.get('filePath', '?')}:{message.get('line')} {rule} — "
                    f"{message.get('message', '')[:120]}",
                )

    total = sum(rules.values())
    print(f"eslint: {total} findings ({errors} errors) across {len(rules)} rules")
    for rule, count in rules.most_common():
        print(f"  {count:4}  {rule}")

    failed = False
    if fatal:
        failed = True
        print(f"\n::error::{len(fatal)} rules-of-hooks violation(s). This is the "
              f"rule whose breach took every authenticated page down with React "
              f"error #310; zero is the only acceptable count.")
        for line in fatal:
            print(f"  {line}")

    if total > args.baseline:
        failed = True
        print(f"\n::error::{total} findings, baseline is {args.baseline}. "
              f"Fix the new ones, or argue for a higher number in "
              f"scripts/eslint_gate.py where it is written down.")
    elif total < args.baseline:
        print(f"\n::notice::{total} findings, below the baseline of "
              f"{args.baseline}. Lower BASELINE in scripts/eslint_gate.py so "
              f"the ratchet holds the ground you just took.")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
