"""E2E: password reset must not be an unauthenticated takeover path.

Runs inside the api container against the live server on localhost:8000.
"""
import json
import os
import sys
import urllib.error
import urllib.request

# Inside the running api container this is localhost; from a throwaway
# `docker compose run` container the API lives at http://api:8000.
BASE = os.environ.get("E2E_API_BASE", "http://localhost:8000")
ADMIN_EMAIL = os.environ["E2E_ADMIN_EMAIL"]
ADMIN_PW = os.environ["E2E_ADMIN_PW"]
TARGET_EMAIL = os.environ["E2E_TARGET_EMAIL"]
NEW_PW = os.environ["E2E_TARGET_NEW_PW"]

fails = []


def call(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else " :: " + str(detail)))
    if not cond:
        fails.append(name)


# 1. The public endpoint must never hand back a token.
st, body = call("POST", "/api/auth/forgot-password", {"email": TARGET_EMAIL})
check("forgot-password 200", st == 200, (st, body))
check("no reset_token in public response", "reset_token" not in (body or {}), body)
check("no token-ish field at all",
      not any("token" in k for k in (body or {})), body)

# 2. Admin can mint a link.
st, tok = call("POST", "/api/auth/login", {"email": ADMIN_EMAIL, "password": ADMIN_PW})
check("admin login", st == 200 and "access_token" in (tok or {}), (st, tok))
admin_token = (tok or {}).get("access_token")

st, users = call("GET", "/api/users", token=admin_token)
check("admin can list users", st == 200, (st, users))
target = next((u for u in (users or []) if u["email"] == TARGET_EMAIL), None)
check("target user found", target is not None, TARGET_EMAIL)

st, link = call("POST", f"/api/users/{target['id']}/reset-link", token=admin_token)
check("admin reset-link 200", st == 200, (st, link))
check("link has url+expiry", bool(link and link.get("url") and link.get("expires_at")), link)
raw_token = (link or {}).get("url", "").split("token=")[-1]
check("token is substantial", len(raw_token) > 30, raw_token)

# 3. Anonymous must not reach the admin endpoint.
st, _ = call("POST", f"/api/users/{target['id']}/reset-link")
check("anon reset-link rejected", st in (401, 403), st)

# 4. Non-admin must not reach it either.
st, ttok = call("POST", "/api/auth/login", {"email": TARGET_EMAIL, "password": os.environ["E2E_TARGET_PW"]})
if st == 200:
    st2, _ = call("POST", f"/api/users/{target['id']}/reset-link",
                  token=ttok["access_token"])
    check("non-admin reset-link 403", st2 == 403, st2)
else:
    print(f"  SKIP non-admin check (target login failed: {st})")

# 5. Reset works, returns no session.
st, body = call("POST", "/api/auth/reset-password",
                {"token": raw_token, "password": NEW_PW})
check("reset-password 204", st == 204, (st, body))
check("reset returns NO session token",
      not (isinstance(body, dict) and "access_token" in body), body)

# 6. New password works.
st, tok2 = call("POST", "/api/auth/login", {"email": TARGET_EMAIL, "password": NEW_PW})
check("login with new password", st == 200 and "access_token" in (tok2 or {}), (st, tok2))

# 7. Token is single-use.
st, _ = call("POST", "/api/auth/reset-password",
             {"token": raw_token, "password": NEW_PW + "x"})
check("token cannot be replayed", st == 400, st)

print("RESULT: " + ("ALL_PASS" if not fails else "FAILED " + ", ".join(fails)))
sys.exit(1 if fails else 0)
