"""Regex patterns for detecting secrets in code before sending it to an LLM."""

import re

# Named patterns — the label goes into the redaction stats and into the
# [REDACTED:label] placeholder
SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    # Provider API keys
    "openai-key": re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    "anthropic-key": re.compile(r"sk-ant-[A-Za-z0-9_-]{50,}"),
    "google-key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "github-token": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    # GitHub fine-grained personal access tokens (since 2022)
    "github-pat-fine": re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    "slack-token": re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "stripe-key": re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{20,}"),
    "aws-access-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "aws-secret": re.compile(r"(?<![A-Za-z0-9/+])[A-Za-z0-9/+]{40}(?![A-Za-z0-9/+])"),
    # Standard formats
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "pem-block": re.compile(
        r"-----BEGIN [A-Z ]+-----[\s\S]+?-----END [A-Z ]+-----",
    ),
    "bearer": re.compile(r"[Bb]earer\s+[A-Za-z0-9._-]{20,}"),
    "basic-auth": re.compile(r"[Bb]asic\s+[A-Za-z0-9+/=]{20,}"),
    # URL with embedded credentials: https://user:pass@host
    "url-with-creds": re.compile(r"https?://[^:\s]+:[^@\s]+@[^\s\"'<>]+"),
    # DB connection strings with a password: postgres/mysql/mongodb/redis/amqp + variants
    "db-conn-string": re.compile(
        r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?|mssql)://"
        r"[^:\s/@]+:[^@\s]+@[^\s\"'<>]+"
    ),
    # Secrets in the URL query string: ?api_key=..., &token=..., ?password=...
    "url-query-secret": re.compile(
        r"[?&](?:api[_-]?key|token|secret|password|access[_-]?token|auth)"
        r"=[A-Za-z0-9._-]{12,}"
    ),
    # Generic high-entropy base64 (last-resort)
    "high-entropy-base64": re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/])"),
}

# Variable names that hint at a secret (for the semantic stage)
SECRET_NAME_KEYWORDS: set[str] = {
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "api-key",
    "auth",
    "credential",
    "private_key",
    "privatekey",
    "access_key",
    "accesskey",
}

# Regex for assignments of string literals to suspicious names:
#   const/let/var/def  NAME  = "value"
#   NAME = 'value'
#   NAME: "value"  (YAML-style)
SEMANTIC_ASSIGN = re.compile(
    r"""
    (?P<prefix>
        (?:const|let|var|final|static)\s+(?P<name1>[A-Za-z_][A-Za-z0-9_]*)\s*[:=]\s*  # JS/TS
      | (?P<name2>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*                                       # Python / generic
      | (?P<name3>[A-Za-z_][A-Za-z0-9_-]*)\s*:\s*                                      # YAML-style
    )
    (?P<quote>['"])
    (?P<value>[^'"\n]{16,})
    (?P=quote)
    """,
    re.VERBOSE,
)


def is_suspicious_name(name: str) -> bool:
    """Checks whether a variable name looks like a secret."""
    lower = name.lower().replace("-", "_")
    return any(kw in lower for kw in SECRET_NAME_KEYWORDS)


#: The subset safe to run over LOG LINES.
#:
#: Not the whole set, and the exclusions matter. `aws-secret` matches any
#: 40-character run of base64 alphabet and `high-entropy-base64` any run of 40
#: or more — which in a log means every git SHA, every content hash, every
#: base64 payload we deliberately print. Redacting those would make the logs
#: useless to read while protecting nothing: a git SHA is not a secret.
#:
#: What is left is shape-specific: a pattern here matches because the string
#: announces what it is (`ghp_`, `sk-ant-`, `AIza`, `Bearer `, a URL with a
#: password in it), not because it looks random.
LOG_REDACTION_PATTERNS: dict[str, "re.Pattern[str]"] = {
    name: SECRET_PATTERNS[name]
    for name in (
        "url-with-creds",
        "db-conn-string",
        "url-query-secret",
        "openai-key",
        "anthropic-key",
        "google-key",
        "github-token",
        "github-pat-fine",
        "slack-token",
        "stripe-key",
        "aws-access-key",
        "jwt",
        "pem-block",
        "bearer",
        "basic-auth",
    )
}
