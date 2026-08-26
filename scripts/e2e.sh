#!/usr/bin/env bash
# Run the end-to-end suites against the running stack.
#
# `tests/` is deliberately not baked into the image (it would ship test
# fixtures to production), so we bind-mount it into a throwaway container that
# shares the stack's network and environment.
#
#   ./scripts/e2e.sh            # everything that needs no credentials
#   ./scripts/e2e.sh access qa  # just these
set -euo pipefail
cd "$(dirname "$0")/.."

SUITES=("$@")
if [ ${#SUITES[@]} -eq 0 ]; then
  SUITES=(access qa mcp_tools mcp_review_tools gitcreds)
fi

failed=()
for s in "${SUITES[@]}"; do
  printf '%-10s ' "$s"
  if out=$(docker compose run --rm --no-deps \
             -v "$PWD/tests:/app/tests:ro" \
             -e E2E_API_BASE="${E2E_API_BASE:-http://api:8000}" \
             api python "tests/e2e/$s.py" 2>&1); then
    echo "$out" | grep -E '^RESULT' || echo 'PASS (no RESULT line)'
  else
    echo "$out" | grep -E '^RESULT|Error' | head -1 || echo FAILED
    failed+=("$s")
  fi
done

if [ ${#failed[@]} -gt 0 ]; then
  echo
  echo "FAILED: ${failed[*]}"
  exit 1
fi
echo
echo "All suites passed."
