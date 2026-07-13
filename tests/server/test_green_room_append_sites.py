"""Every production append routes through admit() — proven by behavior,
not source text: each path fires green_room.materialized with its source
(Green Room Task 3, ADR-156).

Two representative sites, one per conversion shape:

* ``preload_authored_npcs`` (world_materialization.py) — a fresh, unstamped
  Npc that must be freshly stamped AUTHORED at the gate.
* the 162-3 generics last-resort seat (encounter_lifecycle.py) — an Npc that
  already carries a stamped ``Origin(kind=GENERIC, ...)`` and must pass
  through the gate verbatim, still emitting the materialized span.

Fixtures are lifted verbatim from the two files the task-3 brief cites —
``tests/game/test_world_materialization_authored_npcs.py`` (the actual home
of ``preload_authored_npcs`` coverage; ``test_world_materialization.py``
itself covers ``WorldBuilder``/chapter materialization, not the authored
preload) and ``tests/server/test_162_3_generics_last_resort_seating.py`` —
not reinvented.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.disposition import Disposition
from sidequest.game.npc_development import DISPOSITION_DRIFT_PER_MILESTONE
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.game.world_materialization import preload_authored_npcs
from sidequest.genre.models.authored_npc import AuthoredNpc
from sidequest.server.narration_apply import (
    _promote_engaged_pool_member,
    resolve_status_target,
)
from tests.server.test_162_3_generics_last_resort_seating import (
    _ROUTER_NAME,
    _drive,
    _generics_pack,
    _snapshot,
)


def _make_authored_npc(npc_id: str, disposition: int = 0) -> AuthoredNpc:
    # Same shape as test_world_materialization_authored_npcs.py's _make_npc.
    return AuthoredNpc(
        id=npc_id,
        name=f"Authored-{npc_id}",
        pronouns="they/them",
        role="crew",
        appearance="brief description",
        initial_disposition=disposition,
    )


def test_authored_preload_admits(otel_capture) -> None:
    # Drive preload_authored_npcs with a 1-NPC authored world fixture — the
    # exact MagicMock-state / _make_npc shape
    # test_world_materialization_authored_npcs.py's test_fresh_session_
    # preloads_npcs uses.
    state = MagicMock()
    state.npcs = []
    state.characters = []
    state.turn_manager = MagicMock(interaction=1)

    authored = [_make_authored_npc("captain", disposition=60)]

    preload_authored_npcs(state, authored)

    assert len(state.npcs) == 1
    materialized = [
        s for s in otel_capture.get_finished_spans() if s.name == "green_room.materialized"
    ]
    assert any(
        dict(s.attributes or {}).get("canonical_source") == "preload_authored"
        and dict(s.attributes or {}).get("canonical_tier") == 1
        for s in materialized
    ), (
        f"expected a preload_authored green_room.materialized span (tier 1); got "
        f"{[dict(s.attributes or {}) for s in materialized]}"
    )


def test_generics_seat_admits_under_router_name(otel_capture) -> None:
    # Drive instantiate_encounter_from_trigger with a materialized_threat
    # naming an unknown person (_ROUTER_NAME) and a pack whose bestiary has
    # one generics row — staged exactly as
    # TestGenericsSeatTheLastResortOther.test_unbacked_opponent_seats_from_
    # generics_not_stub does.
    snap = _snapshot()
    pack = _generics_pack()

    enc = _drive(snap, pack)

    assert enc is not None
    spans = [s for s in otel_capture.get_finished_spans() if s.name == "green_room.materialized"]
    assert any(
        dict(s.attributes or {}).get("canonical_source") == "seeder.generics" for s in spans
    ), (
        f"expected a seeder.generics green_room.materialized span; got "
        f"{[dict(s.attributes or {}) for s in spans]}"
    )
    opponent = next(a for a in enc.actors if a.side == "opponent")
    # The seat keeps the ROUTER-given name (narrator continuity) — the
    # bestiary generic row donates stats/creature_id only, not identity.
    assert opponent.name == _ROUTER_NAME


# ---------------------------------------------------------------------------
# Reviewer fix 2 (task-3 rework): a site's post-promotion mutation must land
# on the RESOLVED record when admit() folds the candidate onto an existing
# same-identity roster entry. Pre-fix, both narration_apply pool-promotion
# sites mutated the LOCAL candidate before admit() — on a fold, _fill_absent's
# allowlist deliberately excludes live disposition/ocean/beat-log state
# (ADR-139 Inv-2), so the just-computed milestone / OCEAN seed was silently
# dropped. The collision is staged with a diacritic-variant name ("Dona
# Espina" vs the roster's "Doña Espina"): the sites' EXACT-match pre-checks
# miss it, the pool leg matches, and admit()'s normalize_name identity key
# folds the candidate onto the existing record.
# ---------------------------------------------------------------------------


def _collision_snapshot(*, ocean: dict | None = None, disposition: int = 10) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        turn_manager=TurnManager(interaction=6),
    )
    snap.npcs.append(
        Npc(
            core=CreatureCore(
                name="Doña Espina",
                description="A wary trader.",
                personality="Guarded.",
                inventory=Inventory(),
                hp=HpPool(current=10, max=10, base_max=10),
            ),
            disposition=Disposition(disposition),
            ocean=ocean,
        )
    )
    snap.npc_pool.append(
        NpcPoolMember(
            name="Dona Espina",
            drawn_from="narrator_invented",
            non_transactional_interactions=3,
        )
    )
    return snap


def test_engaged_promotion_milestone_lands_on_merged_record() -> None:
    """Site `_promote_engaged_pool_member`: the ADR-128 tier milestone
    (disposition drift + recorded beat) must land on the SURVIVING record when
    the promotion candidate folds onto an existing roster identity — the
    engagement event happened to that person whether the identity is new or
    pre-existing."""
    snap = _collision_snapshot(disposition=10)
    existing = snap.npcs[0]
    member = snap.npc_pool[0]

    promoted = _promote_engaged_pool_member(
        snapshot=snap,
        member=member,
        turn_num=6,
        trigger="tier",
        actor_loc="Salt Camp",
    )

    assert len(snap.npcs) == 1, (
        f"the fold must not duplicate the identity; roster: {[n.core.name for n in snap.npcs]!r}"
    )
    assert promoted is existing, "the promotion must return the surviving (merged) record"
    assert member not in snap.npc_pool, "97-1: the pool entry is consumed by promotion"
    assert int(existing.disposition) == 10 + DISPOSITION_DRIFT_PER_MILESTONE, (
        f"the tier-milestone drift must land on the surviving record, not the "
        f"folded candidate; disposition={int(existing.disposition)}"
    )
    assert any(b.delta == DISPOSITION_DRIFT_PER_MILESTONE for b in existing.disposition_log), (
        f"the milestone beat must be recorded on the surviving record's "
        f"disposition_log; got {existing.disposition_log!r}"
    )


def test_status_target_preserves_existing_ocean_on_merged_record() -> None:
    """Site `resolve_status_target`: the story 72-9 OCEAN seed must NEVER
    clobber a surviving record's live personality (ADR-042) when the promotion
    candidate folds onto it."""
    live_ocean = {"openness": 9.0, "conscientiousness": 2.0}
    snap = _collision_snapshot(ocean=dict(live_ocean))
    existing = snap.npcs[0]

    resolved = resolve_status_target(
        snap, actor_name="Dona Espina", turn_num=6, trigger="status_change"
    )

    assert resolved is existing
    assert len(snap.npcs) == 1
    assert existing.ocean == live_ocean, (
        f"the surviving record's live OCEAN must be untouched; got {existing.ocean!r}"
    )


def test_status_target_seeds_ocean_on_merged_record_lacking_one() -> None:
    """Site `resolve_status_target`: when the SURVIVING record lacks an OCEAN
    profile, the story 72-9 seed must land on it — pre-fix the seed was
    applied to the local candidate before admit() and silently dropped on the
    fold."""
    snap = _collision_snapshot(ocean=None)
    existing = snap.npcs[0]

    resolved = resolve_status_target(
        snap, actor_name="Dona Espina", turn_num=6, trigger="status_change"
    )

    assert resolved is existing
    assert len(snap.npcs) == 1
    assert existing.ocean, (
        "the OCEAN seed must land on the surviving record after the fold "
        "(72-9: an invented person turning mechanical gets a real profile)"
    )
