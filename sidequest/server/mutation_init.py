"""Per-session mutation-state initialization — peer of magic_init.py.

Called at chargen confirmation alongside init_magic_state_for_session.
Packs without a mutations.yaml catalog skip silently (deliberate
authoring choice, mirroring magic_init's absent-file doctrine)."""

from __future__ import annotations

import logging

from sidequest.game.session import GameSnapshot
from sidequest.mutation.chargen import seed_character_mutations
from sidequest.mutation.models import MutationCatalog
from sidequest.mutation.state import MutationState

logger = logging.getLogger(__name__)


def init_mutation_state_for_session(
    snapshot: GameSnapshot,
    *,
    catalog: MutationCatalog | None,
    character_name: str,
    character_class: str,
    session_id: str,
) -> None:
    if catalog is None:
        return  # pack has no mutation system — deliberate authoring choice
    if snapshot.mutation_state is None:
        snapshot.mutation_state = MutationState()
    seeded = seed_character_mutations(
        snapshot.mutation_state,
        catalog,
        actor=character_name,
        character_class=character_class,
        session_id=session_id,
    )
    if seeded is not None:
        logger.info(
            "mutation_init: seeded %r (class=%s) mp=%d negatives=%s",
            character_name, character_class, seeded.mp_remaining, seeded.negative_ids,
        )
