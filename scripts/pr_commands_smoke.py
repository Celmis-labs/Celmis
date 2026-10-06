"""Send one signed, synthetic comment delivery to a running Celmis.

Use it to check, end to end, that the comment webhook is reachable, verifies,
and accepts a command — without opening a pull request and typing a comment.
The delivery names a repository and pull request you choose; the repository
must be registered for review in the workspace, or the receiver refuses it
(fail closed), which is itself a useful answer.

    WEBHOOK_SECRET=... python -m scripts.pr_commands_smoke \
        https://celmis.example.com/webhook/github/<workspace-id> \
        --provider github --repo acme/shop --pr 7 \
        --comment "@celmis help"

The secret is read from the environment and never printed. The result is the
HTTP status and the receiver's JSON answer; a command that was queued shows up
on the pull request's commands timeline, and `help` answers in the thread.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import uuid

import httpx


def _github(repo: str, pr: int, comment: str, actor: str) -> tuple[dict, dict]:
    payload = {
        "action": "created",
        "issue": {"number": pr, "state": "open", "title": "Smoke test",
                  "pull_request": {"url": f"https://api.github.com/repos/{repo}/pulls/{pr}"}},
        "comment": {"id": int(uuid.uuid4().int % 10**9), "body": comment,
                    "user": {"login": actor, "id": 1, "type": "User"},
                    "author_association": "MEMBER"},
        "repository": {"full_name": repo, "private": True},
    }
    return payload, {"X-GitHub-Event": "issue_comment",
                     "X-GitHub-Delivery": str(uuid.uuid4())}


def _bitbucket(repo: str, pr: int, comment: str, actor: str) -> tuple[dict, dict]:
    payload = {
        "actor": {"nickname": actor, "uuid": "{00000000-0000-0000-0000-000000000001}"},
        "comment": {"id": int(uuid.uuid4().int % 10**9), "content": {"raw": comment}},
        "pullrequest": {"id": pr, "state": "OPEN", "title": "Smoke test",
                        "source": {"commit": {"hash": "0" * 40}},
                        "destination": {"branch": {"name": "develop"}}},
        "repository": {"full_name": repo, "is_private": True},
    }
    return payload, {"X-Event-Key": "pullrequest:comment_created",
                     "X-Request-UUID": str(uuid.uuid4())}


def _gitlab(repo: str, pr: int, comment: str, actor: str) -> tuple[dict, dict]:
    payload = {
        "object_attributes": {"id": int(uuid.uuid4().int % 10**9), "note": comment,
                              "noteable_type": "MergeRequest", "system": False},
        "merge_request": {"iid": pr, "state": "opened", "title": "Smoke test",
                          "target_branch": "develop"},
        "project": {"path_with_namespace": repo, "visibility_level": 0},
        "user": {"id": 1, "username": actor},
    }
    return payload, {"X-Gitlab-Event": "Note Hook"}


BUILDERS = {"github": _github, "bitbucket": _bitbucket, "gitlab": _gitlab}


def _sign(provider: str, body: bytes, secret: str) -> dict:
    if provider == "gitlab":
        return {"X-Gitlab-Token": secret}
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    name = "X-Hub-Signature-256" if provider == "github" else "X-Hub-Signature"
    return {name: f"sha256={digest}"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url", help="the workspace's webhook URL for the provider")
    ap.add_argument("--provider", choices=sorted(BUILDERS), default="github")
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--comment", default="@celmis help")
    ap.add_argument("--actor", default="smoke-test")
    args = ap.parse_args(argv)

    secret = os.environ.get("WEBHOOK_SECRET", "")
    if not secret:
        print("Set WEBHOOK_SECRET (the repository's webhook secret).", file=sys.stderr)
        return 2
    payload, headers = BUILDERS[args.provider](args.repo, args.pr, args.comment, args.actor)
    body = json.dumps(payload).encode()
    headers = {**headers, **_sign(args.provider, body, secret),
               "Content-Type": "application/json"}
    resp = httpx.post(args.url, content=body, headers=headers, timeout=30.0)
    print(resp.status_code, resp.text[:500])
    return 0 if resp.status_code < 300 else 1


if __name__ == "__main__":
    raise SystemExit(main())
