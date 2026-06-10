"""Chargen mutation seeding — the catalog's mutant_classes carries the
grant (plan deviation note 2: mutant_wasteland has no classes.yaml, so
the ClassDef seam can't; content-driven keeps it homebrew-extensible).

Negatives are rolled FIRST (AWN p.16 — burdens before gifts); the MP
they grant stays unspent for the guided in-play / chargen spend via
acquire_ops. Idempotent: an already-seeded actor returns the existing
state unchanged (re-entrant chargen handlers must not re-roll)."""

from __future__ import annotations

from sidequest.mutation.acquire_ops import acquire_random_negative
from sidequest.mutation.models import MutationCatalog
from sidequest.mutation.state import CharacterMutationState, MutationState


def seed_character_mutations(
    state: MutationState,
    catalog: MutationCatalog,
    *,
    actor: str,
    character_class: str,
    session_id: str,
) -> CharacterMutationState | None:
    if character_class not in catalog.mp_economy.mutant_classes:
        return None
    if actor in state.characters:
        return state.characters[actor]  # idempotent — never re-roll
    cs = CharacterMutationState(mp_remaining=catalog.mp_economy.base_mp)
    state.characters[actor] = cs
    for _ in range(catalog.mp_economy.chargen_negatives_rolled):
        acquire_random_negative(state, catalog, actor=actor, session_id=session_id, source="chargen")
    return cs
