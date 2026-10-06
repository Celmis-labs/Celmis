"""Compatibility alias: the implementation lives in :mod:`src.security.secret_literals`,
so the code Q&A, PR chat and ``howto`` share the same floor as the dev profile."""

from src.security.secret_literals import MASK, mask_literals, name_is_secret

__all__ = ["MASK", "mask_literals", "name_is_secret"]
