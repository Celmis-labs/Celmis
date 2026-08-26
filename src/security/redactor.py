"""Redaction pipeline — removes secrets from code before sending it to an LLM
or writing it to the vault.

Fail-closed: if the redactor raises an exception — the call does NOT go
further.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from detect_secrets.core.scan import scan_line
from detect_secrets.settings import transient_settings

from src.security.patterns import (
    SECRET_PATTERNS,
    SEMANTIC_ASSIGN,
    is_suspicious_name,
)

logger = logging.getLogger(__name__)

# Curated plugin list — we DISABLE Base64HighEntropyString and
# HexHighEntropyString because they give false positives on ordinary
# identifiers (export, interface, CamelCase class names, etc.).
_CURATED_PLUGINS: dict = {
    "plugins_used": [
        {"name": "AWSKeyDetector"},
        {"name": "AzureStorageKeyDetector"},
        {"name": "BasicAuthDetector"},
        {"name": "CloudantDetector"},
        {"name": "DiscordBotTokenDetector"},
        {"name": "GitHubTokenDetector"},
        {"name": "IbmCloudIamDetector"},
        {"name": "IbmCosHmacDetector"},
        {"name": "JwtTokenDetector"},
        {"name": "MailchimpDetector"},
        {"name": "NpmDetector"},
        {"name": "PrivateKeyDetector"},
        {"name": "SendGridDetector"},
        {"name": "SlackDetector"},
        {"name": "SoftlayerDetector"},
        {"name": "SquareOAuthDetector"},
        {"name": "StripeDetector"},
        {"name": "TelegramBotTokenDetector"},
        {"name": "TwilioKeyDetector"},
    ]
}


@dataclass
class RedactionStats:
    """Statistics for a single redact() call — goes into the audit log."""

    secrets_found: int = 0
    patterns_matched: list[str] = field(default_factory=list)
    bytes_in: int = 0
    bytes_out: int = 0

    def as_dict(self) -> dict:
        return {
            "secrets_found": self.secrets_found,
            "patterns_matched": sorted(set(self.patterns_matched)),
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
        }


class Redactor:
    """Runs text through the stages: detect-secrets (code only), regex patterns, semantic.

    Mode:
      - 'code': all stages (detect-secrets + regex + semantic)
      - 'markdown': regex + semantic only. detect-secrets is skipped because it
         gives a lot of false positives on natural language (the detector takes
         the headings 'Architecture', 'Purpose' for Base64 High Entropy).
    """

    def __init__(self, fail_closed: bool = True) -> None:
        self.fail_closed = fail_closed

    def redact(
        self,
        text: str,
        source_hint: str = "",
        mode: str = "code",
    ) -> tuple[str, RedactionStats]:
        """Returns (redacted_text, stats). On error with fail_closed=True — raises.

        mode: 'code' (the default, all stages) or 'markdown' (no detect-secrets).
        """
        stats = RedactionStats(bytes_in=len(text), bytes_out=len(text))
        if not text:
            return text, stats

        from src.config import get_settings
        if not get_settings().redaction_enabled:
            return text, stats

        try:
            # Order: regex spans first (e.g. PEM block) — then detect-secrets +
            # semantic. Otherwise detect-secrets strips the headers (BEGIN/END)
            # and the regex pattern no longer matches the multi-line block.
            text = self._stage_regex(text, stats)
            if mode == "code":
                text = self._stage_detect_secrets(text, stats)
            text = self._stage_semantic(text, stats)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Redactor crashed on %s: %s", source_hint, exc)
            if self.fail_closed:
                raise
            return "[REDACTION-FAILED]", stats

        stats.bytes_out = len(text)
        return text, stats

    # ─── Stage 1 ─────────────────────────────────────────────────────
    def _stage_detect_secrets(self, text: str, stats: RedactionStats) -> str:
        """Yelp's detect-secrets — curated detectors only (no high-entropy noise)."""
        lines = text.splitlines(keepends=True)
        out_lines: list[str] = []
        with transient_settings(_CURATED_PLUGINS):
            for line in lines:
                replaced = line
                for secret in scan_line(line):
                    if not secret.secret_value:
                        continue
                    placeholder = f"[REDACTED:{secret.type}]"
                    replaced = replaced.replace(secret.secret_value, placeholder)
                    stats.secrets_found += 1
                    stats.patterns_matched.append(f"ds:{secret.type}")
                out_lines.append(replaced)
        return "".join(out_lines)

    # ─── Stage 2 ─────────────────────────────────────────────────────
    def _stage_regex(self, text: str, stats: RedactionStats) -> str:
        """Exact regexes for known formats."""
        for label, pattern in SECRET_PATTERNS.items():
            new_text, n = pattern.subn(f"[REDACTED:{label}]", text)
            if n > 0:
                stats.secrets_found += n
                stats.patterns_matched.append(label)
                text = new_text
        return text

    # ─── Stage 3 ─────────────────────────────────────────────────────
    def _stage_semantic(self, text: str, stats: RedactionStats) -> str:
        """Detects assignments to variables with suspicious names: SECRET='...'."""

        def replace(match: re.Match[str]) -> str:
            name = match.group("name1") or match.group("name2") or match.group("name3") or ""
            if not is_suspicious_name(name):
                return match.group(0)
            stats.secrets_found += 1
            stats.patterns_matched.append("semantic-assign")
            quote = match.group("quote")
            prefix = match.group("prefix")
            return f"{prefix}{quote}[REDACTED:literal]{quote}"

        return SEMANTIC_ASSIGN.sub(replace, text)


# ─── Module-level singleton API ──────────────────────────────────────
_default_redactor: Redactor | None = None


def _get_redactor() -> Redactor:
    global _default_redactor
    if _default_redactor is None:
        from src.config import get_settings

        _default_redactor = Redactor(fail_closed=get_settings().redaction_fail_closed)
    return _default_redactor


def redact(
    text: str,
    source_hint: str = "",
    mode: str = "code",
) -> tuple[str, RedactionStats]:
    """Public entry point.

    Args:
        mode: 'code' for source code (all stages), 'markdown' for Gemini output
              (skips detect-secrets because it gives false positives on natural
              language).
    """
    return _get_redactor().redact(text, source_hint=source_hint, mode=mode)
