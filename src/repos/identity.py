"""One person, several git identities.

A developer shows up as three rows — "Olha Kovalenko" from the provider
account, `olha.kovalenko.ua@example.com` from their laptop, and
`olhakovalenko@Laptop-Olha.local` from a machine where git was never
configured. Scoping an audit to one of the three silently misses the work done
under the other two.

The rule here is deliberately narrow: two identities are SUGGESTED as the same
person when they share a distinctive token — a name-like part at least five
characters long, from the local part of an email or from a display name. It
never merges anything by itself. A suggestion is offered, a person confirms it,
and the confirmation is what gets stored: deciding on someone's behalf that two
rows are one human changes which repositories an audit covers, and that is the
kind of wrong nobody notices.

Only the local part of an email is read. Using the domain would put everyone at
`@acme.ua` in one bucket, which is a company, not a person.
"""

from __future__ import annotations

import re

#: Below this a token is too weak to link on: "a", "dev", "ops", "olha" as an
#: initials-style prefix. Five characters is where surnames start.
MIN_DISTINCTIVE = 5

#: A shortened first name only counts when it OPENS a longer token, never when
#: it merely appears inside one.
MIN_PREFIX = 4

#: Tokens that recur across unrelated people and would link everyone.
_NOISE = frozenset({
    "admin", "user", "users", "local", "localhost", "gmail", "mail", "email",
    "github", "gitlab", "bitbucket", "noreply", "no-reply", "none", "root",
    "build", "builder", "runner", "actions", "service", "account", "macbook",
    "desktop", "laptop", "windows", "ubuntu", "debian", "docker", "jenkins",
    "developer", "support", "office", "team", "test", "example",
})


#: Local parts that name a process rather than a person. `root@agents.acme.tech`
#: is a real commit made on a server; nobody is going to be asked about it, and
#: it should not sit in a people-picker between two colleagues.
_ROBOT_LOCALS = frozenset({
    "root", "ci", "cd", "bot", "build", "builder", "deploy", "deployer",
    "jenkins", "runner", "actions", "gitlab-runner", "service", "svc",
    "system", "daemon", "www-data", "ubuntu", "ec2-user", "administrator",
    "noreply", "no-reply", "do-not-reply", "automation", "pipeline",
})

#: Suffixes providers attach to machine accounts.
_ROBOT_MARKERS = ("[bot]", "-bot", "_bot", "+bot")


def is_robot(identity: str) -> bool:
    """A commit author that is a machine, not a colleague.

    Judged on the local part alone — `root@agents.acme.tech` and
    `root@billing.acme.tech` are two servers, not two people. Deliberately
    NOT applied to `name@Someones-MacBook.local`: that is a person whose git
    was never configured, and hiding them would lose their work.
    """
    lowered = identity.lower().strip()
    if any(marker in lowered for marker in _ROBOT_MARKERS):
        return True
    local = lowered.split("@", 1)[0] if "@" in lowered else lowered
    return local in _ROBOT_LOCALS


def tokens(identity: str) -> set[str]:
    """Name-like parts of an identity, lowercased.

    For an email only the local part is read, with any `+tag` dropped.
    """
    local = identity.split("@", 1)[0] if "@" in identity else identity
    local = local.split("+", 1)[0]
    return {p for p in re.split(r"[^a-z0-9]+", local.lower()) if p}


def distinctive(identity: str) -> set[str]:
    """Tokens strong enough to link two identities together."""
    return {
        t for t in tokens(identity)
        if len(t) >= MIN_DISTINCTIVE and t not in _NOISE and not t.isdigit()
    }


def _linked(a: str, b: str) -> bool:
    """Do these two look like the same person?

    Either they share a distinctive token outright, or one identity's
    distinctive token appears inside the other's — which is the
    `olhakovalenko` / "Olha Kovalenko" case, where a name written without
    separators cannot be split back apart.
    """
    da, db = distinctive(a), distinctive(b)
    if da & db:
        return True
    if _contained(da, tokens(b)) or _contained(db, tokens(a)):
        return True
    # A shortened first name against a full surname: `bond.dmitr` beside
    # "Dmytro Bondarenko". Four characters is too weak to link on its own, so
    # it only counts as the START of a longer token — "bond" opens
    # "bondarenko", where "onda" appearing somewhere inside would mean nothing.
    return _prefixes(tokens(a), tokens(b)) or _prefixes(tokens(b), tokens(a))


def _contained(strong: set[str], other: set[str]) -> bool:
    """A distinctive token written inside a run-together name.

    "kovalenko" sits in `olhakovalenko`, which no separator can split apart.
    """
    return any(
        token in candidate
        for token in strong
        for candidate in other
        if len(candidate) > len(token)
    )


def _prefixes(short: set[str], long_: set[str]) -> bool:
    return any(
        len(token) >= MIN_PREFIX and token not in _NOISE and not token.isdigit()
        and candidate.startswith(token) and len(candidate) >= MIN_DISTINCTIVE + 1
        for token in short
        for candidate in long_
        if candidate != token
    )


def _pick_label(members: list[str]) -> str:
    """The member a human recognises: a written name, else a real email.

    Sorted first so the choice is stable — the same set must always produce
    the same label, or the group key changes between requests.
    """
    ordered = sorted(members)
    named = [m for m in ordered if "@" not in m]
    if named:
        return named[0]
    real = [m for m in ordered if not m.endswith(".local")]
    return (real or ordered)[0]


def suggest_groups(identities: list[str]) -> dict[str, str]:
    """identity → group label, for identities that look like one person.

    Union-find over the link rule. Identities that link to nothing map to
    themselves, so callers can apply the result unconditionally.
    """
    parent: dict[str, str] = {i: i for i in identities}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(identities):
        for b in identities[i + 1:]:
            if _linked(a, b):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra

    clusters: dict[str, list[str]] = {}
    for identity in identities:
        clusters.setdefault(find(identity), []).append(identity)
    return {
        identity: _pick_label(members)
        for members in clusters.values()
        for identity in members
    }


def apply_aliases(
    identities: list[str], aliases: dict[str, list[str]],
) -> dict[str, str]:
    """identity → label, using CONFIRMED groupings only.

    `aliases` is {label: [identity, …]} as a person saved it. Anything not
    named there maps to itself: a saved grouping is a statement about the
    identities it lists and about nothing else.
    """
    mapping = {i: i for i in identities}
    for label, members in (aliases or {}).items():
        for member in members:
            if member in mapping:
                mapping[member] = label
    return mapping


__all__ = [
    "MIN_DISTINCTIVE",
    "apply_aliases",
    "distinctive",
    "is_robot",
    "suggest_groups",
    "tokens",
]
