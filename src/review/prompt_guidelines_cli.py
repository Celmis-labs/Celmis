"""Operator commands for the workspace half of migration c5d6e7f8a9b0.

The repository half is an Alembic migration (`alembic downgrade a7b8c9d0e1f2`
undoes it). The workspace prompts live in the encrypted credential store,
which Alembic does not manage, so the API's startup hook sorts them once
(`src.api.routers.agents.migrate_workspace_prompt_overrides`) and this undoes
or forces it:

    python -m src.review.prompt_guidelines_cli migrate
    python -m src.review.prompt_guidelines_cli revert

`revert` turns every guideline the conversion created back into the
replacement prompt it was and removes the marker — run it BEFORE going back
to a release older than 2.3.1, whose code reads only the replacements.
Guidelines a person wrote after the upgrade are left alone. (Staying on
2.3.1 after a revert, the next start sorts them again: the marker is gone.)
Prints workspaces and agents touched; never the text.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.review.prompt_guidelines_cli")
    parser.add_argument("command", choices=("migrate", "revert"))
    args = parser.parse_args(argv)

    from src.api.routers import agents

    if args.command == "migrate":
        done = agents.migrate_workspace_prompt_overrides()
        if done is None:
            print("migrate: already done (or this store cannot list workspaces)")
            return 0
    else:
        done = agents.revert_workspace_prompts_migration()
    for slot, names in sorted(done.items()):
        print(f"{args.command} {slot} agents={','.join(names)}")
    if not done:
        print(f"{args.command}: nothing to change")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
