# End-to-end suites

These are **not** collected by `pytest` — they need a running stack (Postgres,
Qdrant, a live API) and they write to real tables. Running them under the unit
suite would make `pytest` depend on Docker being up, so they stay standalone
scripts that exit non-zero on failure and print `RESULT: ALL_PASS`.

Run them inside the api container, which already has the environment:

```bash
./scripts/e2e.sh            # all of them
./scripts/e2e.sh access qa  # a subset
```

| Script | Covers | Needs |
|---|---|---|
| `access.py`   | Stage 22 research access: glob matching, deny-wins, visibility tiers | Postgres |
| `qa.py`       | Retrieval honours access rules (embed + Qdrant mocked, access real) | Postgres |
| `mcp_tools.py` | MCP tools enforce the same rules as the API | Postgres |
| `mcp_review_tools.py` | Review-fix MCP tools (`route_incident`, `bootstrap_client`, …) are gated | Postgres |
| `reset.py`    | Password reset is not an unauthenticated takeover path | live API |
| `gitcreds.py` | Git tokens resolve workspace-first and survive their owner leaving | credential store |

`reset.py` needs credentials passed in, and it changes a real user's password —
point it at a throwaway account:

```bash
docker compose run --rm --no-deps -v "$PWD/tests:/app/tests:ro" \
  -e E2E_API_BASE=http://api:8000 \
  -e E2E_ADMIN_EMAIL=admin@example.com -e E2E_ADMIN_PW='…' \
  -e E2E_TARGET_EMAIL=throwaway@example.com -e E2E_TARGET_PW='…' \
  -e E2E_TARGET_NEW_PW='…' \
  api python tests/e2e/reset.py
```

The file names matter: `tests/e2e` lands on `sys.path`, so a script called
`mcp.py` would shadow the installed `mcp` package. Hence `mcp_tools.py`.

Every script cleans up what it creates. If one fails partway, re-running it is
safe — they delete their own fixtures on entry as well as on exit.
