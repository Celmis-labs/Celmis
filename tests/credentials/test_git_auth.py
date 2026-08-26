"""Bitbucket auth shape — the exact rule the live API enforces."""
from src.credentials.git_auth import describe_auth, git_auth_kwargs

EMAIL = "ci-bot@acme.tech"

TOK = "ATATT3xFfGF0" + "x" * 180

# GIT: an Atlassian API token must go out as x-bitbucket-api-token-auth —
# the email form makes git fail with a misleading "no access" (prod-verified).
kw = git_auth_kwargs("bitbucket", TOK, {"atlassian_email": EMAIL})
assert kw == {"api_token": TOK}, kw
assert describe_auth("bitbucket", TOK, {"atlassian_email": EMAIL}) == \
    "bitbucket:x-bitbucket-api-token-auth"

# REST API: the SAME token needs the registered email as username.
kw = git_auth_kwargs("bitbucket", TOK, {"atlassian_email": EMAIL}, purpose="api")
assert kw == {"username": EMAIL, "password": TOK}, kw
assert describe_auth("bitbucket", TOK, {"atlassian_email": EMAIL}, purpose="api") == \
    "bitbucket:email+api-token"

# Repository/workspace access token uses x-token-auth.
kw = git_auth_kwargs("bitbucket", "ATCTT3xFfGF0abc", {})
assert kw == {"username": "x-token-auth", "password": "ATCTT3xFfGF0abc"}, kw

# Legacy app password uses the Bitbucket username.
kw = git_auth_kwargs("bitbucket", "app-pass-123", {"username": "acme"})
assert kw == {"username": "acme", "password": "app-pass-123"}, kw

# No stored email: API falls back to the token form rather than guessing.
assert git_auth_kwargs("bitbucket", "ATATTnoemail", {}, purpose="api") == \
    {"api_token": "ATATTnoemail"}

# Other providers keep the token-URL form.
assert git_auth_kwargs("github", "ghp_abc", {}) == {"api_token": "ghp_abc"}
assert git_auth_kwargs("gitlab", "glpat-abc", {}) == {"api_token": "glpat-abc"}

print("git auth shape tests: OK")
