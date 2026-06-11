"""Per-session mutation-state initialization — peer of magic_init.py.

Called at chargen confirmation alongside init_magic_state_for_session.
Packs without a mutations.yaml catalog skip silently (deliberate
authoring choice, mirroring magic_init's absent-file doctrine)."""

from __future__ import annotations

import logging

from sidequest.game.session import GameSnapshot
from sidequest.mutation.chargen import seed_character_mutations
from sidequest.mutation.models import MutationCatalog
from sidequest.mutation.saints import SaintRegistry, apply_saint_preset
from sidequest.mutation.state import MutationState
from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

logger = logging.getLogger(__name__)


def init_mutation_state_for_session(
    snapshot: GameSnapshot,
    *,
    catalog: MutationCatalog | None,
    character_name: str,
    character_class: str,
    session_id: str,
    saints: SaintRegistry | None = None,
    saint_id: str | None = None,
) -> None:
    if saint_id is not None:
        # Saint-Marked route (story 103-1): the world's Saint canon supplies a
        # curated preset over the same MP economy. A saint_id with no registry
        # or no catalog is a configuration error — fail loud, never improvise
        # a Saint (No Silent Fallbacks).
        if saints is None:
            raise ValueError(
                f"saint_id {saint_id!r} given but the active world ships no Saint "
                "registry (worlds/<slug>/saints.yaml)"
            )
        if catalog is None:
            raise ValueError(
                f"saint_id {saint_id!r} given but the pack has no mutation catalog "
                "(mutations.yaml) to price the bundle against"
            )
        if snapshot.mutation_state is None:
            snapshot.mutation_state = MutationState()
        seeded = apply_saint_preset(
            snapshot.mutation_state,
            catalog,
            saints,
            actor=character_name,
            saint_id=saint_id,
            session_id=session_id,
        )
        logger.info(
            "mutation_init: saint preset %r applied to %r mp=%d bundle=%s drawback=%s",
            saint_id,
            character_name,
            seeded.mp_remaining,
            seeded.positive_ids,
            seeded.negative_ids,
        )
        _watcher_publish(
            "mutation.saint_init",
            {
                "session_id": session_id,
                "actor": character_name,
                "saint_id": saint_id,
                "mp_remaining": seeded.mp_remaining,
                "bundle_count": len(seeded.positive_ids),
            },
            component="mutation",
            severity="info",
        )
        return
    if catalog is None:
        # Pack has no mutation system — deliberate authoring choice. Per
        # the OTEL Observability Principle, surface justified
        # non-engagement to the GM panel rather than staying silent.
        _watcher_publish(
            "mutation.init_skipped",
            {
                "session_id": session_id,
                "actor": character_name,
                "reason": "no_catalog",
            },
            component="mutation",
            severity="info",
        )
        return
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
            character_name,
            character_class,
            seeded.mp_remaining,
            seeded.negative_ids,
        )
        _watcher_publish(
            "mutation.init",
            {
                "session_id": session_id,
                "actor": character_name,
                "class": character_class,
                "mp_remaining": seeded.mp_remaining,
                "negatives": len(seeded.negative_ids),
            },
            component="mutation",
            severity="info",
        )
    else:
        # Non-mutant class — the catalog exists but this character's class
        # isn't in mp_economy.mutant_classes. Justified non-engagement.
        _watcher_publish(
            "mutation.init_skipped",
            {
                "session_id": session_id,
                "actor": character_name,
                "class": character_class,
                "reason": "non_mutant_class",
            },
            component="mutation",
            severity="info",
        )
