#!/usr/bin/env bash
#
# Fill a .env with real secrets, in the formats each one actually needs.
#
# `.env.example` ships every secret EMPTY, and that is deliberate: the
# previous version put the generating command beside the variable —
# `CELMIS_MASTER_KEY=   # openssl rand -hex 24` — and a dotenv file has no
# inline comments, so every install that copied it ran with a master
# password printed in the repository. An empty value fails loudly; a
# plausible one does not fail at all.
#
# So the example stays empty and this script fills it, because the formats
# are not interchangeable: CREDENTIAL_MASTER_KEY must be a Fernet key (32
# raw bytes, url-safe base64, 44 chars) and rejects the 64 hex characters
# `openssl rand -hex 32` produces — the failure surfaces in the UI as
# "Failed to fetch", days later. LITELLM_MASTER_KEY must start with `sk-`
# or the gateway refuses to provision.
#
#   ./scripts/init-env.sh              # create .env, or fill blanks in one
#   ./scripts/init-env.sh --check      # report what is missing, write nothing
#
# Idempotent: a variable that already has a value is never touched. Run it
# again after pulling new variables and it fills only the new blanks.
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_FILE="${CELMIS_ENV_FILE:-.env}"
EXAMPLE=".env.example"
CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

[[ -f "$EXAMPLE" ]] || { echo "$EXAMPLE not found — run from the repo root" >&2; exit 1; }

if [[ ! -f "$ENV_FILE" && $CHECK_ONLY -eq 0 ]]; then
  cp "$EXAMPLE" "$ENV_FILE"
  echo "created $ENV_FILE from $EXAMPLE"
fi
[[ -f "$ENV_FILE" ]] || { echo "$ENV_FILE not found; run without --check first" >&2; exit 1; }

CHECK_ONLY=$CHECK_ONLY ENV_FILE=$ENV_FILE python3 - <<'PY'
import os
import re
import secrets
import sys

env_file = os.environ["ENV_FILE"]
check_only = os.environ["CHECK_ONLY"] == "1"


def fernet_key() -> str:
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        # Same bytes, same encoding — the library is not required to make one.
        import base64
        return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    return Fernet.generate_key().decode()


#: name -> generator. A variable NOT in this table is never generated: it
#: either comes from a third party (an OAuth client secret, a provider API
#: key) or it is a keypair this script has no business inventing.
GENERATORS = {
    "POSTGRES_PASSWORD":       lambda: secrets.token_hex(24),
    "CELMIS_JWT_SECRET":       lambda: secrets.token_hex(32),
    "NEXTAUTH_SECRET":         lambda: secrets.token_hex(32),
    "MCP_JWT_SECRET":          lambda: secrets.token_hex(32),
    # Fernet, not hex — see the header. Getting this wrong is invisible
    # until the first credential read.
    "CREDENTIAL_MASTER_KEY":   fernet_key,
    "CELMIS_MASTER_KEY":       lambda: secrets.token_hex(24),
    "CELMIS_OPS_TOKEN":        lambda: secrets.token_hex(24),
    # The gateway refuses a master key without this prefix.
    "LITELLM_MASTER_KEY":      lambda: "sk-" + secrets.token_hex(24),
    "LITELLM_SALT_KEY":        lambda: secrets.token_hex(24),
    "REVIEW_WEBHOOK_SECRET":   lambda: secrets.token_hex(24),
    "REVIEW_GITLAB_TOKEN":     lambda: secrets.token_hex(24),
    "REVIEW_BITBUCKET_SECRET": lambda: secrets.token_hex(24),
    # The sandbox refuses to start without this one. The deploy checks for
    # 32+ characters, so hex(32) — 64 chars — clears it either way.
    "SANDBOX_TOKEN":           lambda: secrets.token_hex(32),
    # Generated here so the observability overlay has one before it is ever
    # switched on. It used to default to `admin`, on a port that was bound to
    # every interface — and the overlay is opt-in, so that was the reward for
    # deciding to watch your own instance.
    "GRAFANA_ADMIN_PASSWORD":  lambda: secrets.token_urlsafe(24),
}

#: Left empty on purpose, with the reason the operator needs to hear.
NOT_OURS = {
    "GOOGLE_CLIENT_ID": "from Google Cloud Console — optional, for Google sign-in",
    "GOOGLE_CLIENT_SECRET": "from Google Cloud Console — optional",
    "GOOGLE_OAUTH_CLIENT_ID": "from Google Cloud Console — optional",
    "VAPID_PUBLIC_KEY": "an EC P-256 keypair, not a random string — "
                        "`npx web-push generate-vapid-keys`; optional",
    "VAPID_PRIVATE_KEY": "the private half of the pair above; optional",
    "GEMINI_API_KEY": "your provider key — or set it per workspace in the UI",
    "GITHUB_TOKEN": "a provider token, for the CLI inside the container — "
                    "users save their own via the Connections page",
    "GITLAB_TOKEN": "as above",
    "BITBUCKET_TOKEN": "as above",
    "SMTP_PASSWORD": "your mail server's — optional, only for digests/invites",
}

ASSIGN = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")

lines = open(env_file).read().splitlines()

# Duplicates first: dotenv keeps the LAST assignment, so a variable written
# twice silently loses its first value. For CREDENTIAL_MASTER_KEY that means
# every stored credential becomes unreadable, and nothing says why.
seen: dict[str, int] = {}
dupes: list[tuple[str, int, int]] = []
for i, line in enumerate(lines, 1):
    m = ASSIGN.match(line.strip())
    if not m:
        continue
    name = m.group(1)
    if name in seen:
        dupes.append((name, seen[name], i))
    seen[name] = i

if dupes:
    print("REFUSING: the same variable is assigned twice, and a dotenv parser")
    print("keeps the LAST one — the earlier value is silently discarded.")
    for name, first, second in dupes:
        print(f"  {name}: line {first} and line {second}")
    print("Delete the duplicate you do not want, then run this again.")
    sys.exit(2)

filled, skipped, missing = [], [], []
for i, line in enumerate(lines):
    m = ASSIGN.match(line.strip())
    if not m:
        continue
    name, value = m.group(1), m.group(2).strip()
    if value:
        continue
    if name in GENERATORS:
        if check_only:
            missing.append(name)
        else:
            lines[i] = f"{name}={GENERATORS[name]()}"
            filled.append(name)
    elif name in NOT_OURS:
        skipped.append(name)

if check_only:
    if missing:
        print("empty and generatable:", ", ".join(sorted(missing)))
        sys.exit(1)
    print("every generatable secret has a value")
    sys.exit(0)

if filled:
    open(env_file, "w").write("\n".join(lines) + "\n")
    os.chmod(env_file, 0o600)
    print(f"filled {len(filled)}: {', '.join(sorted(filled))}")
else:
    print("nothing to fill — every generatable secret already has a value")

if skipped:
    print("\nleft empty (not ours to invent):")
    for name in sorted(skipped):
        print(f"  {name}  — {NOT_OURS[name]}")

# A secret-shaped variable this script has neither a generator nor a reason
# for is the bug that produced SANDBOX_TOKEN: the run reported "filled 12",
# looked complete, and the sandbox would not start. Silence is what made it
# expensive, so say it out loud and exit non-zero.
unknown = sorted(
    name
    for i, line in enumerate(lines)
    if (m := ASSIGN.match(line.strip()))
    and not m.group(2).strip()
    and (name := m.group(1))
    and name not in GENERATORS
    and name not in NOT_OURS
    and any(w in name for w in ("SECRET", "KEY", "TOKEN", "PASSWORD"))
)
if unknown:
    print("\nEMPTY AND UNACCOUNTED FOR — this script neither generates these")
    print("nor knows why they are blank, so nobody will notice they are:")
    for name in unknown:
        print(f"  {name}")
    print("Add a generator or a reason in scripts/init-env.sh.")
    sys.exit(3)
PY

if [[ $CHECK_ONLY -eq 0 ]]; then
  echo
  echo "$ENV_FILE is chmod 600. It is gitignored; keep it that way."
  echo "CREDENTIAL_MASTER_KEY and LITELLM_SALT_KEY must never be rotated —"
  echo "rotating either makes existing stored credentials unreadable."
fi
