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

# Conservative appositive-epithet pattern. §A4 makes alias correctness LOAD-BEARING:
# a garbage extractor pollutes the DOMINANT mention signal, so we extract ONLY the
# tightest, unambiguous form — a DETERMINER-led, LOWERCASE epithet appositive
# directly attached to the NPC's name: "Borin, the old smith" / "the old smith,
# Borin". Requirements that keep out garbage:
#   * a leading determiner ("the"/"a"/"an"/"old"/"young") — a real epithet reads
#     "the old smith", not a bare noun;
#   * lowercase words only (matched case-SENSITIVELY) — a capitalized run is a
#     proper name, never an epithet, so "Borin, Thorn, and Vex" can't misfire;
#   * 1-3 trailing words — an epithet, not a clause.
# The name anchor matches case-insensitively (player/narrator casing varies); only
# the epithet body is lowercase-locked.
_EPITHET_BODY = r"(?:the|a|an|old|young)\s+[a-z][a-z'-]+(?:\s+[a-z][a-z'-]+){0,2}"


def extract_epithets_for_npc(narration: str, npc_name: str) -> list[str]:
    """Extract appositive epithets the narration attaches to ``npc_name``.

    CONSERVATIVE by design (§A4 — alias correctness is load-bearing): matches only
    the two tightest appositive forms, both anchored on the canonical name and both
    requiring a determiner-led, lowercase epithet:

      * ``Name, <the epithet>``  — "Borin, the old smith"
      * ``<the epithet>, Name``  — "the old smith, Borin"

    The lowercase + determiner requirement means a comma-separated run of proper
    names ("Borin, Thorn, and Vex") is NOT mistaken for an epithet. Returns the
    matched epithet phrases (may be empty); de-dup/blank/idempotency policy is left
    to :func:`accrete_npc_aliases`.
    """
    if not narration.strip() or not npc_name.strip():
        return []
    name = re.escape(npc_name.strip())
    epithets: list[str] = []
    # Name first: "Borin, the old smith" — name case-insensitive, epithet lowercase.
    for m in re.finditer(rf"(?i:\b{name}\b),\s+({_EPITHET_BODY})\b", narration):
        epithets.append(m.group(1).strip())
    # Epithet first: "the old smith, Borin".
    for m in re.finditer(rf"\b({_EPITHET_BODY}),\s+(?i:\b{name}\b)", narration):
        epithets.append(m.group(1).strip())
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
