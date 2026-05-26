"""Beneath Sünden Plan 7 Task 6 — frontier-crossing / frontier-approach
region-transition seam.

This is the **producer side** of the look-ahead wiring seam: a thin,
real observer-dispatch hook installed on the **real production
region-transition path** — ``GameSnapshot._apply_world_patch_inner``'s
``patch.current_region`` apply (the ADR-011 ``WorldStatePatch`` apply that
production uses: the narrator escape-hatch tool
``sidequest/agents/tools/apply_world_patch.py`` and
``sidequest/server/dispatch/monster_manual_inject.py`` both flow through
``GameSnapshot.apply_world_patch``). It is NOT a parallel navigation path
and NOT a stub.

Why this point (and not ``room_movement``): ``room_movement.py``'s
runtime surface (``validate_room_transition`` / ``apply_validated_move``)
is explicitly deferred to a later story (see its module docstring) and is
ROOM-level — orthogonal to the REGION transition the materializer's
frontier needs. The only code that mutates ``snap.current_region``
mid-session is the ``patch.current_region`` apply; ADR-055's region INIT
(``region_init.init_region_location``) is turn-1 only. So the
frontier-crossing hook lands here, extending the patch-apply
region-transition + ADR-055 ``region_init`` dedup-append semantics.

Two responsibilities (spec §7):

1. **Frontier-approach** — when the party transitions toward an
   unexpanded frontier edge, the registered observers are notified so a
   look-ahead materialization can be enqueued. Task 6 ships the **real
   dispatch seam**; the async worker that consumes it (idempotency,
   ``lookahead_breadth``, background execution) is **Task 7** — it
   ``register_frontier_observer``s itself. Until then the registry is
   genuinely empty: a real seam with zero consumers, the
   ``telemetry.watcher_hub`` honest-deferral precedent (NOT a stub — the
   producer is fully wired into production).

2. **Frontier-crossing → promote-to-active** — the committed expansion
   is already *live* from Task 6's commit transaction (it is in
   ``dungeon_map``). "Promote to active" is the minimal real
   session/region state recognition: the crossed-into region is
   dedup-appended into ``snap.discovered_regions`` exactly as ADR-055
   ``region_init.init_region_location`` does for the starting region
   (extending that contract to look-ahead-materialized regions, NOT a
   new mechanism).

OTEL: every fired transition emits ``frontier.region_transition`` so the
GM panel (the lie detector) sees the seam engaged rather than trusting
narration that the dungeon grew (the OTEL Observability Principle).

No Silent Fallbacks: an observer that raises propagates loudly (a broken
look-ahead enqueue is a real bug, never swallowed). The hook fires ONLY
on a genuine region change (different non-empty ``to_region``), never on
an unrelated patch.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from sidequest.telemetry.spans.dungeon_materialize import (
    frontier_region_transition_span,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sidequest.game.session import GameSnapshot

__all__ = [
    "FrontierObserver",
    "notify_region_transition",
    "register_frontier_observer",
    "registered_observer_count",
    "unregister_frontier_observer",
]

# A frontier-approach observer. Task 7's async look-ahead worker
# registers one; it receives the live snapshot + the from/to region so it
# can decide approach-vs-crossing against the live frontier (the snapshot
# does not itself carry the DungeonStore — that wiring is Task 7's).
FrontierObserver = Callable[..., None]

# Module-level registry — the telemetry.watcher_hub observer-set
# precedent. Starts empty: a real dispatch seam with zero consumers is
# honest-deferral, not a stub (the producer side IS wired into the real
# production region-transition path; the consumer is Task 7).
_OBSERVERS: list[FrontierObserver] = []


def register_frontier_observer(observer: FrontierObserver) -> None:
    """Register a frontier-approach observer (Task 7's worker registers
    here). Idempotent per identity — re-registering the same callable is
    a no-op so a re-entrant session setup cannot double-enqueue."""
    if observer not in _OBSERVERS:
        _OBSERVERS.append(observer)


def unregister_frontier_observer(observer: FrontierObserver) -> None:
    """Remove a previously registered observer. Unknown observer → no-op
    (teardown must be safe to call unconditionally)."""
    if observer in _OBSERVERS:
        _OBSERVERS.remove(observer)


def registered_observer_count() -> int:
    """Number of registered observers — lets the GM panel / Task 7 assert
    the seam has a live consumer (Verify Wiring, Not Just Existence)."""
    return len(_OBSERVERS)


def notify_region_transition(
    snapshot: GameSnapshot,
    *,
    pc_name: str,
    from_region: str | None,
    to_region: str,
) -> None:
    """Fire the frontier seam for one real PER-PC region transition.

    Called from ``GameSnapshot._apply_world_patch_inner`` AFTER the moving
    PC's ``pc_regions[pc_name]`` is set to ``to_region`` (the real production
    per-PC region-transition point — Movement subsystem §Q2). ``pc_name`` is
    WHICH PC moved (split-party legibility); the spawn/teleport anchor path
    passes the seated PCs it seeded (or a ``"__anchor__"`` sentinel when no PC
    is seated yet). ``from_region`` is that PC's pre-transition region (may be
    ``""``/``None`` before their first region is set).

    Two real effects, in order:

    1. **Promote-to-active**: dedup-append ``to_region`` into
       ``snapshot.discovered_regions`` (the ADR-055
       ``region_init.init_region_location`` semantics, extended to
       look-ahead-materialized regions — existing order preserved for
       save compatibility).
    2. **Frontier-approach dispatch**: notify every registered observer
       (Task 7's worker) with the live snapshot + from/to region so a
       look-ahead materialization can be enqueued. An observer that
       raises propagates loudly (No Silent Fallbacks).

    Emits ``frontier.region_transition`` so the GM panel sees the seam
    engaged (the OTEL Observability Principle — the only way to tell the
    look-ahead is wired vs. the narrator improvising the dungeon grew).
    """
    with frontier_region_transition_span(
        from_region=from_region or "",
        to_region=to_region,
        observers=len(_OBSERVERS),
        pc_name=pc_name,
    ):
        # 1. Promote-to-active: the crossed-into region is now recognized in
        #    the SHARED fog-of-war set (a region ANY PC entered is on the
        #    whole table's map — the existing fog model; ADR-055 region_init
        #    dedup-append, extended).
        if to_region not in snapshot.discovered_regions:
            snapshot.discovered_regions.append(to_region)

        # 2. Frontier-approach dispatch to Task 7's worker(s). Snapshot
        #    of the list so an observer that (un)registers mid-dispatch
        #    does not mutate the iteration. Loud: no try/except swallow.
        #    ``pc_name`` threads through so the worker materializes the
        #    frontier around THIS PC's new region (split-party legibility).
        for observer in list(_OBSERVERS):
            observer(
                snapshot=snapshot,
                pc_name=pc_name,
                from_region=from_region,
                to_region=to_region,
            )
