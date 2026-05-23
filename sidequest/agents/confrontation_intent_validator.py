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
from dataclasses import dataclass
from typing import Any, Literal, Protocol

# Conservative stopword set. The goal is to drop function words that add
# no semantic signal, not to do NLP.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "to",
        "for",
        "with",
        "in",
        "on",
        "at",
        "of",
        "and",
        "or",
        "but",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "by",
        "from",
    }
)

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _strip_suffix(token: str) -> str:
    """Light suffix strip. Not a Porter stemmer — keeps 'draw' vs 'drawer'
    distinct, and refuses to mangle '-ss' words like 'cross', 'press', 'pass'."""
    for suffix in ("ing", "ed", "s"):
        if not token.endswith(suffix):
            continue
        if len(token) <= len(suffix) + 2:
            continue
        if suffix == "s" and token.endswith("ss"):
            continue  # cross, press, pass, boss, class — don't strip
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


# ---------------------------------------------------------------------------
# Severity type
# ---------------------------------------------------------------------------

Severity = Literal["warn", "soft_suggest", "reprompt"]


# ---------------------------------------------------------------------------
# ValidationResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:
    """Result of a confrontation intent vs declared-type check.

    Always represents a flagged mismatch. The validator returns None when
    there is nothing to flag.
    """

    matched_type: str
    declared: str | None
    severity: Severity
    matched_tokens: tuple[str, ...]


# ---------------------------------------------------------------------------
# Protocols (duck-typed — no concrete model imports at runtime)
# ---------------------------------------------------------------------------


class _ConfrontationDefLike(Protocol):
    confrontation_type: str
    on_intent_mismatch: Severity
    intent_verb_set: frozenset[str]


class _PackLike(Protocol):
    rules: Any


# ---------------------------------------------------------------------------
# validate()
# ---------------------------------------------------------------------------


def validate(
    action_rewrite: Any,
    declared_confrontation: str | None,
    pack: _PackLike | None,
    *,
    active_encounter: bool,
) -> ValidationResult | None:
    """Compare narrator-declared intent against declared confrontation.

    Returns ``None`` for any non-flag case (no intent, encounter active,
    declared matches inferred, nothing matches, pack missing). Never raises.
    """
    if pack is None:
        return None
    rules = getattr(pack, "rules", None)
    if rules is None:
        return None
    if active_encounter:
        return None
    if action_rewrite is None:
        return None
    intent = (getattr(action_rewrite, "intent", "") or "").strip()
    if not intent:
        return None

    intent_tokens = tokenize(intent)
    if not intent_tokens:
        return None

    defs = getattr(rules, "confrontations", None) or []
    scored: list[tuple[int, int, _ConfrontationDefLike, frozenset[str]]] = []
    for idx, cdef in enumerate(defs):
        verbs = getattr(cdef, "intent_verb_set", None) or frozenset()
        if not verbs:
            continue
        overlap = intent_tokens & verbs
        if overlap:
            scored.append((len(overlap), idx, cdef, overlap))

    if not scored:
        return None

    # Most overlap wins; ties broken by pack-declaration order (lower idx first).
    scored.sort(key=lambda x: (-x[0], x[1]))
    _, _, best_cdef, best_overlap = scored[0]

    if declared_confrontation == best_cdef.confrontation_type:
        return None

    return ValidationResult(
        matched_type=best_cdef.confrontation_type,
        declared=declared_confrontation,
        severity=best_cdef.on_intent_mismatch,
        matched_tokens=tuple(sorted(best_overlap)),
    )
