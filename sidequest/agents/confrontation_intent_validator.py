"""Confrontation intent validator.

Spec: docs/superpowers/specs/2026-05-20-confrontation-intent-validator-design.md

Activates the dormant ``ActionRewrite.intent`` field as the authoritative
intent signal. Pure function. No I/O. Stateless. Never raises on bad input.

The ``validate()`` function lands in Task 3. This module starts with
``tokenize()`` because pack-load (Task 2) and validation (Task 3) MUST use
byte-identical tokenization rules.
"""

from __future__ import annotations

import re

# Conservative stopword set. The goal is to drop function words that add
# no semantic signal, not to do NLP.
_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "to", "for", "with", "in", "on", "at", "of",
    "and", "or", "but", "is", "are", "was", "were", "be", "been",
    "by", "from",
})

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _strip_suffix(token: str) -> str:
    """Light suffix strip. Not a Porter stemmer — keeps 'draw' vs 'drawer'
    distinct by only stripping when the stem is at least 3 chars."""
    for suffix in ("ing", "ed", "s"):
        if token.endswith(suffix) and len(token) > len(suffix) + 2:
            return token[: -len(suffix)]
    return token


def tokenize(text: str) -> frozenset[str]:
    """Tokenize ``text`` into a stopword-stripped, suffix-stripped set.

    Used identically at pack-load (to derive intent_verb_set per
    ConfrontationDef) and at validation time. Idempotent.
    """
    if not text or not text.strip():
        return frozenset()
    lowered = text.lower()
    raw = (t for t in _TOKEN_SPLIT.split(lowered) if t)
    return frozenset(_strip_suffix(t) for t in raw if t not in _STOPWORDS)
