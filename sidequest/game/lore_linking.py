"""Link a character to its own creation-seed lore fragments.

Story 93-4: the History section (93-3) is the home for player-linked lore.
The fragments already live in the per-session :class:`LoreStore` (seeded at
chargen confirm by :func:`seed_lore_from_char_creation`, ADR-048 / story
75-15). This module is the *link* that pulls back out the fragments that
belong to THIS character — its chosen chargen options — as a typed,
sheet-ready list.

Why the link is a value match, not "all CharacterCreation fragments"
--------------------------------------------------------------------
``seed_lore_from_char_creation`` seeds **one fragment per choice in every
scene** — including the choices the player did NOT pick — all into one
shared per-session store. So "this character's lore" is the fragments whose
``metadata['choice_label']`` matches an answer the character actually gave
(``Character.creation_answers[].value`` for ``kind == 'choice'``). That
value match is the per-character firewall: two players who answered the same
scene *differently* never see each other's pick in their History
(ADR-104/105 intent). Players who chose the *same* option both surface the
one shared fragment (identical content) — full per-player isolation would
require scoping fragment ids by player at seed time, which the seeder does
not yet do (tracked as a Delivery Finding).

No Silent Fallbacks: a *choice* answer with no matching fragment in the
store (e.g. a resume where seeding didn't run, or a content edit that
removed the choice) is logged at WARNING and skipped — never turned into a
fabricated row from the answer text. Freeform answers are not
fragment-backed (the seeder only iterates ``scene.choices``), so a freeform
answer with no fragment is expected-absent and does not warn.
"""

from __future__ import annotations

import logging

from sidequest.game.character import Character
from sidequest.game.lore_store import LoreFragment, LoreSource, LoreStore
from sidequest.protocol.models import LinkedLoreFragment

logger = logging.getLogger(__name__)


def linked_lore_for_character(store: LoreStore, character: Character) -> list[LinkedLoreFragment]:
    """Return the creation-seed lore fragments linked to ``character``.

    Filters the shared session store to the fragments matching the
    character's own chargen choices, projecting each into a
    :class:`LinkedLoreFragment` for the History 'Lore' subsection.
    """
    # Index this session's creation-seed fragments by (scene_id, choice_label)
    # — the only key derivable from Character.creation_answers (which records
    # the chosen LABEL, not the choice index in the fragment id).
    by_choice: dict[tuple[str, str], LoreFragment] = {}
    for fragment in store.fragments_iter():
        if fragment.source != LoreSource.CharacterCreation:
            continue
        scene_id = fragment.metadata.get("scene_id")
        label = fragment.metadata.get("choice_label")
        if scene_id is None or label is None:
            continue
        by_choice[(scene_id, label)] = fragment

    linked: list[LinkedLoreFragment] = []
    for answer in character.creation_answers:
        # Only choice answers are creation-seed-fragment-backed.
        if answer.kind != "choice":
            continue
        fragment = by_choice.get((answer.scene_id, answer.value))
        if fragment is None:
            # Loud skip — never fabricate a row from the answer text alone.
            logger.warning(
                "lore_link.unresolved scene_id=%s choice_label=%s character=%s — "
                "creation-seed fragment absent from store; skipping (no fabrication)",
                answer.scene_id,
                answer.value,
                character.core.name,
            )
            continue
        linked.append(
            LinkedLoreFragment(
                fragment_id=fragment.id,
                title=fragment.metadata.get("choice_label", ""),
                summary=fragment.content,
                source=fragment.source,
                # No dedicated lore-page route for creation-seed fragments yet
                # (No Silent Fallbacks — a dead link is worse than no link).
                lore_route=None,
            )
        )
    return linked


__all__ = ["linked_lore_for_character"]
