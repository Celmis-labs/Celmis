"""Which marker styles and HTML tags does Bitbucket Cloud hide or render?

The invisible-marker design (`src/review/markers.py`) rests on one assumption
that cannot be settled from the code: that a markdown reference definition
(`[//]: # (x)`) renders as nothing in a Bitbucket comment. This script settles
it on a throwaway pull request. Run it against a PR on the Celmis TEST
repository, never a real one — it posts four comments and deletes them again.

    python -m scripts.probe_bitbucket_markdown <workspace>/<repo> <pr-number> \
        [--user default] [--workspace default] [--keep]

For each probe it posts a comment, reads `content.html` from Bitbucket's answer
and says whether the marker (or the tag) shows up as visible text:

    html     <!-- celmis:probe -->            (what GitHub and GitLab hide)
    refdef   [//]: # (celmis:probe)           (the default style)
    zwsp     zero-width characters            (the fallback)
    tags     <sub>, <details>, &amp;          (what `bitbucket_flavour` rewrites)

Read the verdict lines: `refdef: hidden` means the default style works. If
`refdef: VISIBLE`, set REVIEW_MARKER_STYLE=zwsp (the provider also switches by
itself the first time it sees a marker in `content.html`).
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

from src.review import markers
from src.review.providers.bitbucket import BITBUCKET_API_BASE, BitbucketPRProvider

_TOKEN = "celmis:probe"
_TAG = re.compile(r"<[^>]*>")

#: name -> the raw markdown of the probe comment.
PROBES: dict[str, str] = {
    "html": f"Celmis markdown probe (will be deleted)\n\n<!-- {_TOKEN} -->\n\nend",
    "refdef": f"Celmis markdown probe (will be deleted)\n\n[//]: # ({_TOKEN})\n\nend",
    "zwsp": ("Celmis markdown probe (will be deleted)\n\n"
             + markers._zw_encode(_TOKEN) + "\n\nend"),
    "tags": (
        "Celmis markdown probe (will be deleted)\n\n"
        "<sub>sub-tag</sub> and <details><summary>details-tag</summary>inner</details> "
        "and A &amp;amp; B\n\nend"
    ),
}


def _visible(html: str) -> str:
    return _TAG.sub("", html or "")


def judge(name: str, html: str) -> str:
    """"hidden" / "VISIBLE" for the marker probes, what rendered for `tags`."""
    text = _visible(html)
    if name == "tags":
        shown = [t for t in ("<sub>", "<details>", "<summary>", "&amp;amp;") if t in text]
        rendered = [t for t in ("<sub>", "<details>") if t in (html or "")]
        return (f"shown as text: {', '.join(shown) or 'none'}; "
                f"kept as HTML: {', '.join(rendered) or 'none'}")
    leaked = _TOKEN in text or "[//]: #" in text
    return "VISIBLE" if leaked else "hidden"


def run_probe(provider: BitbucketPRProvider, ws: str, repo: str, pr: int,
              *, keep: bool = False) -> dict[str, Any]:
    """Post the probes on one PR; return {name: {"verdict", "comment_id", "html"}}."""
    url = f"{BITBUCKET_API_BASE}/repositories/{ws}/{repo}/pullrequests/{pr}/comments"
    report: dict[str, Any] = {}
    for name, raw in PROBES.items():
        resp = provider._http.post(url, json={"content": {"raw": raw}})
        if resp.status_code not in (200, 201):
            report[name] = {"verdict": f"HTTP {resp.status_code}", "comment_id": None, "html": ""}
            continue
        data = resp.json()
        html = str((data.get("content") or {}).get("html") or "")
        cid = data.get("id")
        report[name] = {"verdict": judge(name, html), "comment_id": cid, "html": html}
        if not keep and isinstance(cid, int):
            provider._http.delete(f"{url}/{cid}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("repo", help="<workspace>/<repo> of a TEST repository")
    parser.add_argument("pr", type=int, help="the pull request number to comment on")
    parser.add_argument("--user", default="default")
    parser.add_argument("--workspace", default="default", help="the Celmis workspace id")
    parser.add_argument("--keep", action="store_true", help="leave the probe comments")
    args = parser.parse_args(argv)

    ws, _, repo = args.repo.partition("/")
    if not ws or not repo:
        parser.error("repo must look like <workspace>/<repo>")
    with BitbucketPRProvider(user_id=args.user, workspace_id=args.workspace) as provider:
        report = run_probe(provider, ws, repo, args.pr, keep=args.keep)
    for name, row in report.items():
        print(f"{name:7s} {row['verdict']}")
    if report.get("refdef", {}).get("verdict") == "VISIBLE":
        print("-> set REVIEW_MARKER_STYLE=zwsp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
