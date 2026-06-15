"""Alias-aware mention resolution + accretion merge — ADR-118 §A4 (Story 84-2, WI-5).

84-1 made ``mention`` the DOMINANT pertinence signal but left it NAME-MATCH ONLY
(``retrieval_orchestration.py`` carried the explicit WI-5 TODO). WI-5 widens the
match so a player reference by EPITHET ("the old man", "the seat of the fire king")
resolves to the canonical entity — ADR-048's own example — without forking the
word-boundary discipline that keeps "art" from matching inside "start".

This module is PURE: no daemon, no span, no store. Two helpers:

  * :func:`resolve_mention` — the alias-aware matcher. Word-bounded (``\\b``),
    case-insensitive, multi-word epithets matched as a PHRASE. Returns the
    canonical names referenced through EITHER the name OR any alias.
  * :func:`accrete_aliases` — the idempotent, case-folded-dedup merge the 75-1
    accretion path uses (append only genuinely-new epithets, never a blank, never
    a case-dup, never mutate the input).

The same matcher backs both the live mention seam
(:func:`sidequest.agents.npc_context.player_referenced_npcs_from_action`, which calls
this) and any direct caller, so name-match and alias-match share ONE word-boundary
implementation — no drift.
"""

from __future__ import annotations

import re

from sidequest.foundation.slug_fold import fold_to_ascii


def _phrase_matches(phrase: str, action_text: str) -> bool:
    """True when ``phrase`` occurs in ``action_text`` as a whole word/phrase.

    Word-bounded (``\\b``) and case-insensitive — the exact discipline of
    ``player_referenced_npcs_from_action``: a name/alias must occur as a complete
    token (or multi-token phrase), so "art" does not match inside "start". A blank
    phrase never matches (it would otherwise match everywhere).

    Both sides are first folded through the SHARED 101-8 NFKD helper
    (:func:`sidequest.foundation.slug_fold.fold_to_ascii`) so a diacritic-named entity
    resolves a reference written without the accent — and vice-versa — closing the
    §A4 diacritic split-brain (Story 84-7). The fold decomposes precomposed letters
    and drops combining marks (``é`` → ``e``), folding BOTH the candidate phrase and
    the action text so neither side's accent decides the match. ``IGNORECASE`` then
    handles case and ``\\b`` the word boundary, on the folded text — the same shared
    rule the slug surfaces use, not a second normalization.
    """
    phrase = phrase.strip()
    if not phrase:
        return False
    folded_phrase = fold_to_ascii(phrase)
    folded_action = fold_to_ascii(action_text)
    return re.search(rf"\b{re.escape(folded_phrase)}\b", folded_action, re.IGNORECASE) is not None


def resolve_mention(
    action_text: str,
    *,
    names: set[str],
    aliases_by_name: dict[str, list[str]],
) -> set[str]:
    """Return the canonical names ``action_text`` references by name OR alias.

    A name or any of its aliases that occurs as a word-bounded, case-insensitive
    phrase in the action resolves that canonical name. Multi-word epithets match as
    a phrase (not token-by-token). Not single-winner: an action that references two
    entities (one by name, one by alias) surfaces both.
    """
    if not action_text or not action_text.strip():
        return set()
    matched: set[str] = set()
    for name in names:
        if _phrase_matches(name, action_text):
            matched.add(name)
            continue
        for alias in aliases_by_name.get(name, []):
            if _phrase_matches(alias, action_text):
                matched.add(name)
                break
    return matched


def accrete_aliases(existing: list[str], new_epithets: list[str]) -> list[str]:
    """Idempotent, case-folded-dedup merge for the 75-1-shaped accretion path.

    Returns a NEW list: the existing aliases in their original order, then each
    genuinely-new epithet appended in order. An epithet is skipped when it is blank
    /whitespace, or when it case-folds to one already present (so "Old Man" never
    lands beside "old man"). The input ``existing`` list is never mutated —
    determinism the 75-6 reproject relies on.
    """
    result = list(existing)
    seen = {a.strip().casefold() for a in result if a.strip()}
    for epithet in new_epithets:
        key = epithet.strip().casefold()
        if not key or key in seen:
            continue
        result.append(epithet.strip())
        seen.add(key)
    return result
