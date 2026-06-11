"""Alias accretion on promotion — ADR-118 §A4 (Story 84-2, WI-5).

Promoted / yes-and entities **accrete epithets** the longer a campaign runs, so
mention-matching "gets smarter with no new pipeline." This mirrors the 75-1 lore
accretion shape (:func:`sidequest.game.lore_accretion.accrete_facts_to_lore`):
idempotent mint → case-folded dedup → a result struct → an OTEL span.

The dedup/blank/idempotency policy is the shared
:func:`sidequest.game.alias_resolution.accrete_aliases` (one merge rule for the
resolver and the accreter — no drift). This module adds the stateful side: it
mutates ``npc.aliases`` in place and emits the ``entity.alias_accreted`` span so the
GM panel can verify aliases are ENGINE-written, not narrator-improvised
(CLAUDE.md OTEL principle). The span fires ONLY on a real accretion — a no-op turn
(every epithet a duplicate/blank) emits nothing, so the lie-detector isn't spammed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sidequest.game.alias_resolution import accrete_aliases
from sidequest.game.session import Npc
from sidequest.telemetry.spans.span import Span

# The accretion span name (ADR-118 §A4 / CLAUDE.md OTEL principle). Pinned to a
# constant so the emitter here and the GM-panel reader agree on the exact string.
SPAN_ALIAS_ACCRETED = "entity.alias_accreted"

# Conservative appositive-epithet extraction. §A4 makes alias correctness
# LOAD-BEARING: a garbage extractor pollutes the DOMINANT mention signal, so we
# extract ONLY a DETERMINER-led, LOWERCASE noun-phrase appositive anchored on the
# NPC's name — and reject anything that is actually a SCENE CLAUSE ("Borin, the
# torch sputters and dies"; "the crowd parts, Borin walks through"). Two guards,
# defense in depth (TEA 84-2 review):
#   1. FINITE-VERB rejection (primary) — a valid epithet is a noun phrase with NO
#      finite verb ("the old smith"); a clause has one ("sputters", "parts"). This
#      is the ONLY signal that catches the epithet-FIRST mirror, where comma
#      position can't tell a descriptor from a clause.
#   2. COMMA-CLOSED name-first appositives (reinforcing) — a real name-first
#      epithet is comma-bracketed ("Borin, the old smith,"); the garbage name-first
#      cases are comma-OPEN ("Borin, the torch sputters…").
# The epithet body is lowercase-locked (case-sensitive) so a capitalized proper-name
# run ("Borin, Thorn, and Vex") can't misfire; the name anchor matches
# case-insensitively. The minted epithet is the leading 1-3-word determiner phrase.

# Determiners that may lead an epithet.
_DETERMINER = r"(?:the|a|an|old|young)"
# A determiner-led noun phrase: determiner + 1-3 lowercase words. This is the SHAPE
# that gets MINTED (truncated to the leading words).
_EPITHET_BODY = rf"{_DETERMINER}\s+[a-z][a-z'-]+(?:\s+[a-z][a-z'-]+){{0,2}}"

# Finite present-tense 3rd-person scene verbs — the matrix verbs and the common
# scene verbs that occupy the "the <noun> <verb>s" clause position an appositive
# epithet can be mistaken for. This curated list is the SOLE finite-verb signal.
#
# Story 84-7 removed the prior structural "-s ending ⇒ finite verb" fallback: ``-s``
# is genuinely ambiguous between a 3rd-person verb ("sputters") and a plural NOUN
# ("spurs", "moons", "the silver spurs"), and the structural rule produced
# false positives that wrongly REJECTED valid plural-noun epithets — the bug this
# story fixes. No reliable structural separator exists without a full POS tagger
# (out of scope: no nltk/spacy dependency), so the curated list — well-matched to
# the narrow determiner-led syntactic position, where the clause verbs are common
# and enumerable — carries the signal alone. Unlisted scene verbs in that position
# now mint instead of being caught structurally; that is the accepted trade for not
# misclassifying plural-noun descriptors (§A4 "miss before mint-garbage" still holds
# for every verb in the list).
_FINITE_VERB_STOPLIST: frozenset[str] = frozenset(
    {
        "sputters", "swings", "parts", "creaks", "howls", "walks", "enters", "dies",
        "stands", "sits", "turns", "looks", "moves", "steps", "nods", "raises",
        "leans", "draws", "speaks", "shouts", "whispers", "runs", "falls", "rises",
        "opens", "closes", "slams", "crashes", "rumbles", "groans", "hisses",
        "flickers", "glows", "burns", "drips", "echoes", "rattles", "snaps",
        "is", "was", "has", "goes", "comes", "stares", "glares", "watches",
    }
)


def _looks_like_finite_verb(word: str) -> bool:
    """True when ``word`` is a present-tense 3rd-person finite verb (a clause tell).

    Membership in the curated :data:`_FINITE_VERB_STOPLIST` is the sole test. The
    old structural ``-s`` fallback was removed in Story 84-7 (see the stoplist
    comment): it could not tell a 3sg verb from a plural noun and so wrongly rejected
    plural-noun epithets. The list is biased to the common scene verbs that share the
    determiner-led position with an appositive, so a false "is a verb" — which would
    wrongly reject a valid epithet — stays rare."""
    w = word.lower().strip("'-")
    if not w:
        return False
    return w in _FINITE_VERB_STOPLIST


def _phrase_has_finite_verb(phrase: str) -> bool:
    """True when any token in ``phrase`` is a finite verb — i.e. it is a clause, not
    a noun-phrase epithet."""
    return any(_looks_like_finite_verb(tok) for tok in re.split(r"[\s,]+", phrase) if tok)


def _leading_epithet(phrase: str) -> str | None:
    """Truncate a determiner-led ``phrase`` to its leading 1-3-word epithet, if it is
    a clean noun phrase.

    Truncate FIRST (to the leading determiner + 1-3 words), THEN verb-check that
    truncated epithet — not the whole span. This matters for a long valid epithet
    like "the grand high warlock of the seven towers": its TRUNCATION ("the grand
    high warlock") is a clean noun phrase, even though a later word ("towers") would
    trip the structural -s heuristic. A scene clause's verb falls INSIDE the leading
    1-3 words ("the crowd parts" → "parts"), so it is still caught.

    Returns the minted epithet, or ``None`` when ``phrase`` has no determiner-led
    head, or that head carries a finite verb (it is a clause, not a descriptor)."""
    phrase = phrase.strip().rstrip(".,;:")
    m = re.match(rf"^({_EPITHET_BODY})\b", phrase)
    if not m:
        return None
    epithet = m.group(1).strip()
    if _phrase_has_finite_verb(epithet):
        return None
    return epithet


def extract_epithets_for_npc(narration: str, npc_name: str) -> list[str]:
    """Extract appositive noun-phrase epithets the narration attaches to ``npc_name``.

    CONSERVATIVE by design (§A4 — alias correctness is load-bearing). Two anchored
    forms, both rejecting scene clauses via the finite-verb guard:

      * ``Name, <the epithet>,``  — comma-CLOSED appositive ("Borin, the old smith,")
      * ``<the epithet>, Name``   — epithet-first appositive ("the old smith, Borin")

    A clause masquerading as an appositive ("Borin, the torch sputters…",
    "the crowd parts, Borin…") is rejected because the noun phrase carries a finite
    verb. The name-first form additionally requires the closing comma (the garbage
    name-first cases are comma-open). Returns the minted epithet phrases (may be
    empty); de-dup/blank/idempotency policy is left to :func:`accrete_npc_aliases`.
    """
    if not narration.strip() or not npc_name.strip():
        return []
    name = re.escape(npc_name.strip())
    epithets: list[str] = []

    # Name first, COMMA-CLOSED: "Borin, <appositive>," — the appositive runs from the
    # name's comma to the next comma. Reject if that span is a clause (finite verb);
    # else mint its leading determiner phrase.
    for m in re.finditer(rf"(?i:\b{name}\b),\s+([^,]+?),", narration):
        epithet = _leading_epithet(m.group(1))
        if epithet:
            epithets.append(epithet)

    # Epithet first: "<phrase>, Borin" — the phrase before the name's comma. Reject
    # if it is a clause; else mint its leading determiner phrase. Comma-closure can't
    # disambiguate here (the closing comma IS the name), so the verb guard carries it.
    for m in re.finditer(rf"([^,.]+?),\s+(?i:\b{name}\b)", narration):
        epithet = _leading_epithet(m.group(1))
        if epithet:
            epithets.append(epithet)

    return epithets


@dataclass
class AliasAccretionResult:
    """Outcome of one alias-accretion call (mirrors ``AccretionResult`` shape)."""

    accreted: list[str] = field(default_factory=list)
    skipped_duplicate: int = 0
    skipped_blank: int = 0


def accrete_npc_aliases(
    npc: Npc,
    epithets: list[str],
    *,
    turn: int,
) -> AliasAccretionResult:
    """Append genuinely-new ``epithets`` to ``npc.aliases`` and observe the change.

    Idempotent and case-folded-dedup via :func:`accrete_aliases`: a blank epithet is
    skipped (counted ``skipped_blank``); an epithet already present (case-insensitively)
    is skipped (counted ``skipped_duplicate``); genuinely-new epithets are appended in
    order and returned in ``accreted``. ``npc.aliases`` is reassigned (not mutated in a
    way that bypasses Pydantic) only when something accreted.

    Emits a single ``entity.alias_accreted`` span carrying the npc name and the
    accreted aliases — ONLY when ``accreted`` is non-empty. A no-op accretion emits
    no span (don't spam the GM-panel lie-detector with empty turns).
    """
    before = list(npc.aliases)
    merged = accrete_aliases(before, epithets)
    accreted = merged[len(before) :]

    # Per-epithet bookkeeping for the result struct. Walk the epithets the same way
    # ``accrete_aliases`` does: a blank epithet is ``skipped_blank``; a non-blank
    # epithet whose case-folded key is already present (an existing alias or an
    # earlier epithet this call) is ``skipped_duplicate``; the first occurrence of a
    # genuinely-new key is the accreted one (already in ``accreted``).
    skipped_blank = 0
    skipped_duplicate = 0
    seen = {a.strip().casefold() for a in before if a.strip()}
    for epithet in epithets:
        key = epithet.strip().casefold()
        if not key:
            skipped_blank += 1
        elif key in seen:
            skipped_duplicate += 1
        else:
            seen.add(key)  # first sight of a new key — accreted, not skipped

    if accreted:
        # Reassign so Pydantic sees the mutation (model_config extra=forbid still
        # permits assigning a declared field's value).
        npc.aliases = merged
        with Span.open(
            SPAN_ALIAS_ACCRETED,
            {
                "npc_name": npc.core.name,
                "aliases_accreted": ", ".join(accreted),
                "alias_count": len(merged),
                "turn": int(turn),
            },
        ):
            pass

    return AliasAccretionResult(
        accreted=accreted,
        skipped_duplicate=skipped_duplicate,
        skipped_blank=skipped_blank,
    )
