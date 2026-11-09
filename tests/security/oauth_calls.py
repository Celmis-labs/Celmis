"""Call the OAuth endpoint functions directly, the way the router's tests always did.

The functions now take a Request (for the public address) and a JSON body, and
answer with a response object; these wrappers give the old call shape back.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from starlette.requests import Request

BASE_HEADERS = [(b"host", b"celmis.example.com"), (b"x-forwarded-proto", b"https"),
                (b"x-forwarded-host", b"celmis.example.com"), (b"x-forwarded-prefix", b"/backend")]


def request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/oauth/token", "headers": BASE_HEADERS,
                    "query_string": b"", "scheme": "https", "server": ("celmis.example.com", 443)})


async def consent(oauth_mod, *, session, user, client_id, redirect_uri, challenge, scope, workspace="ws-a",
                  decision="allow") -> str:
    """The code the consent call put in the redirect."""
    body = oauth_mod.ConsentIn(client_id=client_id, redirect_uri=redirect_uri, code_challenge=challenge,
                               scope=scope, workspace=workspace, decision=decision)
    resp = await oauth_mod.authorize_consent(request=request(), payload=body, session=session, user=user)
    return parse_qs(urlsplit(json.loads(resp.body)["redirect_to"]).query)["code"][0]


async def token(oauth_mod, *, session, **form) -> dict:
    form.setdefault("client_secret", None)
    for k in ("code", "redirect_uri", "code_verifier", "refresh_token", "resource"):
        form.setdefault(k, None)
    resp = await oauth_mod.token_exchange(request=request(), session=session, **form)
    return json.loads(resp.body)
