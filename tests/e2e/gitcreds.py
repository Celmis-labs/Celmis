"""E2E: git tokens resolve workspace-first and survive their owner leaving."""
import contextlib
import sys

from src.credentials import (
    WORKSPACE_GIT_USER,
    get_credential_store,
    resolve_git_credential,
)

store = get_credential_store()
fails = []
LABEL = "e2e-gitcreds"
GONE = "e2e-departed-admin"


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else " :: " + str(detail)))
    if not cond:
        fails.append(name)


def cleanup():
    for slot in (WORKSPACE_GIT_USER, GONE, "default"):
        with contextlib.suppress(Exception):
            store.delete(provider="github", user_id=slot, account_label=LABEL)


cleanup()

# Nothing stored anywhere → None, not an exception.
check("no creds → None",
      resolve_git_credential("github", user_id=GONE, account_label=LABEL) is None)

# Legacy: token owned by one admin. Resolver still finds it (back-compat).
store.save(provider="github", secret="tok-personal", metadata={},
           user_id=GONE, account_label=LABEL)
r = resolve_git_credential("github", user_id=GONE, account_label=LABEL)
check("legacy per-user row still resolves", r is not None and r.secret == "tok-personal", r)
check("resolver reports the tier that answered", r.user_id == GONE, r.user_id)

# THE BUG: a different user (the poller acting for a repo someone else
# registered, or after the owner is gone) could not see that token at all.
other = resolve_git_credential("github", user_id="someone-else", account_label=LABEL)
check("legacy row is invisible to everyone else (the bug)", other is None, other)

# Workspace slot: now everyone resolves it, whoever they are.
store.save(provider="github", secret="tok-workspace", metadata={},
           user_id=WORKSPACE_GIT_USER, account_label=LABEL)
for who in (GONE, "someone-else", "default", "brand-new-hire"):
    r = resolve_git_credential("github", user_id=who, account_label=LABEL)
    check(f"workspace token visible to {who}",
          r is not None and r.secret == "tok-workspace", r)

# Workspace wins over a stale personal row — one acting identity, not a race.
r = resolve_git_credential("github", user_id=GONE, account_label=LABEL)
check("workspace slot takes priority over legacy", r.secret == "tok-workspace", r.secret)

# Owner leaves: their row is deleted, polling keeps working.
store.delete(provider="github", user_id=GONE, account_label=LABEL)
r = resolve_git_credential("github", user_id=GONE, account_label=LABEL)
check("survives the owner being removed", r is not None and r.secret == "tok-workspace", r)

cleanup()
check("cleanup left nothing behind",
      resolve_git_credential("github", user_id=GONE, account_label=LABEL) is None)

print("RESULT: " + ("ALL_PASS" if not fails else "FAILED " + ", ".join(fails)))
sys.exit(1 if fails else 0)
