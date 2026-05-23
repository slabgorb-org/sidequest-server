"""RED-phase tests for Story 22-3 — active→ghost seed expiry transition.

Contract under test (AC3 / lifecycle wiring):

* A turn-tick seam walks ``snapshot.active_seeds`` once per narration
  turn and migrates any entry whose ``is_expired(now_turn)`` is True out
  of ``active_seeds`` and into ``seed_ghosts`` (via ``SeedState.to_ghost``).
* The migration preserves seed identity (``id``, ``name``,
  ``delivery_hints``) and stamps ``expired_at_turn`` to the current turn.
* The tick is idempotent — a second tick on the same turn must not
  duplicate ghosts or revive expired ones.
* The tick is engine-driven, not narrator-tool-driven: even when the
  narrator does not call any time-advancing tool, expiry still fires.
  (Project memory: ``project_narrator_gaslighting_doctrine`` — state is
  materialized, not improvised. Lifespan elapses whether or not the
  narrator opts in.)

The 22-1 archive flagged this as the open question: ``is_expired()`` /
``to_ghost()`` exist but have no caller. 22-3 wires the caller. We test
the public behavior — Dev picks the seam (new ``tick_seeds`` sibling of
``tick_tropes``, or extension of ``tick_tropes``); either is acceptable
so long as the snapshot's seed lists end up correct.

Test discipline: synthetic fixtures only, no pack loading. Per
``feedback_tests_not_point_at_content`` we do not couple this test to
``tea_and_murder``'s authored deck.
"""

from __future__ import annotations

import pytest

from sidequest.game.session import GameSnapshot, SeedGhost, SeedState
from sidequest.genre.models.tropes import SeedTrope

# The function under test does not yet exist. Dev will create it. Both
# import paths below are plausible — we try them in order and skip-with-
# instruction if neither resolves yet, so the RED phase produces a clean
# failure message rather than a noisy ImportError stack.
_TICK_IMPORT_CANDIDATES = (
    ("sidequest.game.seed_tick", "tick_seeds"),
    ("sidequest.game.seed_deck", "tick_seeds"),
    ("sidequest.game.trope_tick", "tick_seeds"),
)


def _resolve_tick_seeds():
    for module_path, attr in _TICK_IMPORT_CANDIDATES:
        try:
            module = __import__(module_path, fromlist=[attr])
        except ModuleNotFoundError:
            continue
        fn = getattr(module, attr, None)
        if callable(fn):
            return fn
    return None


def _make_pack(seeds: list[SeedTrope]):
    """A duck-typed pack — only ``seed_tropes`` is read by the tick.

    Matches the duck-typing the existing ``tick_tropes`` uses (it reads
    only ``pack.tropes``) — keeps fixtures lean.
    """

    class _Pack:
        def __init__(self, _seeds: list[SeedTrope]):
            self.seed_tropes = _seeds
            # Some implementations may also look at `tropes` for
            # parallelism; provide an empty list so the duck-type matches.
            self.tropes = []

    return _Pack(seeds)


def _seed(seed_id: str, lifespan: int = 5) -> SeedTrope:
    return SeedTrope(
        id=seed_id,
        name=f"Seed {seed_id}",
        description="Authored prose for " + seed_id,
        flavor_tags=["test"],
        lifespan_turns=lifespan,
        delivery_hints=["hint A", "hint B"],
        narrative_hint="connect to whichever NPC matters",
    )


def _active(seed_id: str, activated_at: int, lifespan: int = 5) -> SeedState:
    return SeedState(
        id=seed_id,
        name=f"Seed {seed_id}",
        activated_at_turn=activated_at,
        flavor_tags=["test"],
        lifespan_turns=lifespan,
        delivery_hints=["hint A", "hint B"],
    )


def test_tick_seeds_function_is_exposed():
    """RED guard: the implementation seam must exist. Dev picks the
    module; one of the candidate import paths must resolve to a
    callable. Without this, no other tests in this file can run."""
    fn = _resolve_tick_seeds()
    assert fn is not None, (
        "Could not import a tick_seeds callable from any of "
        f"{[m for m, _ in _TICK_IMPORT_CANDIDATES]}. Dev: add the engine "
        "function (sibling pattern of tick_tropes) and re-run. The "
        "signature this suite expects is "
        "``tick_seeds(snapshot, pack, *, now_turn: int) -> None`` — "
        "mutates snapshot.active_seeds and snapshot.seed_ghosts in place."
    )


def test_expired_seed_moves_from_active_to_ghost_on_tick():
    fn = _resolve_tick_seeds()
    if fn is None:
        pytest.skip("tick_seeds not yet wired — see test_tick_seeds_function_is_exposed")

    snap = GameSnapshot(genre_slug="x", world_slug="y")
    # Activated at turn 1, lifespan 3 → expires at turn 4 (is_expired
    # is inclusive at activated_at + lifespan_turns).
    snap.active_seeds = [_active("alpha", activated_at=1, lifespan=3)]
    pack = _make_pack([_seed("alpha", lifespan=3)])

    fn(snap, pack, now_turn=4)

    assert snap.active_seeds == [], (
        "Expired seed must be removed from active_seeds. Found: "
        f"{[s.id for s in snap.active_seeds]}"
    )
    assert len(snap.seed_ghosts) == 1
    ghost = snap.seed_ghosts[0]
    assert isinstance(ghost, SeedGhost)
    assert ghost.id == "alpha"
    assert ghost.expired_at_turn == 4, (
        "Ghost must stamp expired_at_turn to the tick's now_turn, not "
        "the seed's activation turn or lifespan boundary."
    )
    assert ghost.delivery_hints == ["hint A", "hint B"], (
        "delivery_hints must carry through to_ghost so cross-session "
        "callback prose retains the original sensory anchors."
    )


def test_unexpired_seed_stays_active_on_tick():
    fn = _resolve_tick_seeds()
    if fn is None:
        pytest.skip("tick_seeds not yet wired")

    snap = GameSnapshot(genre_slug="x", world_slug="y")
    snap.active_seeds = [_active("beta", activated_at=2, lifespan=5)]
    pack = _make_pack([_seed("beta", lifespan=5)])

    # Turn 5 = activated_at(2) + 3 < activated_at(2) + lifespan(5) = 7
    fn(snap, pack, now_turn=5)

    assert len(snap.active_seeds) == 1, "Unexpired seed must remain active"
    assert snap.active_seeds[0].id == "beta"
    assert snap.seed_ghosts == [], "Unexpired seed must not produce a ghost"


def test_mixed_actives_partition_correctly():
    """Multiple actives, some expired and some not — the tick must
    bucket them correctly in a single pass. Catches a buggy implementation
    that mutates active_seeds during iteration and skips an entry."""
    fn = _resolve_tick_seeds()
    if fn is None:
        pytest.skip("tick_seeds not yet wired")

    snap = GameSnapshot(genre_slug="x", world_slug="y")
    snap.active_seeds = [
        _active("expired-a", activated_at=0, lifespan=2),  # expires turn 2
        _active("alive-b", activated_at=1, lifespan=10),
        _active("expired-c", activated_at=0, lifespan=3),  # expires turn 3
        _active("alive-d", activated_at=2, lifespan=8),
    ]
    pack = _make_pack(
        [
            _seed("expired-a", lifespan=2),
            _seed("alive-b", lifespan=10),
            _seed("expired-c", lifespan=3),
            _seed("alive-d", lifespan=8),
        ]
    )

    fn(snap, pack, now_turn=5)

    active_ids = {s.id for s in snap.active_seeds}
    ghost_ids = {g.id for g in snap.seed_ghosts}
    assert active_ids == {"alive-b", "alive-d"}, (
        f"Expected only alive-* in active_seeds; got {active_ids}"
    )
    assert ghost_ids == {"expired-a", "expired-c"}, (
        f"Expected expired-* in seed_ghosts; got {ghost_ids}"
    )


def test_tick_is_idempotent_within_same_turn():
    """Two ticks on the same now_turn must not duplicate ghosts or
    re-expire already-ghosted seeds. The narrator can call its
    time-advancing tools more than once per turn (rare but possible),
    and the engine must not double-count."""
    fn = _resolve_tick_seeds()
    if fn is None:
        pytest.skip("tick_seeds not yet wired")

    snap = GameSnapshot(genre_slug="x", world_slug="y")
    snap.active_seeds = [_active("alpha", activated_at=0, lifespan=2)]
    pack = _make_pack([_seed("alpha", lifespan=2)])

    fn(snap, pack, now_turn=3)
    fn(snap, pack, now_turn=3)

    assert len(snap.seed_ghosts) == 1, (
        f"Second tick on the same turn produced duplicate ghosts: "
        f"{[g.id for g in snap.seed_ghosts]}"
    )
    assert snap.active_seeds == []


def test_tick_on_empty_actives_is_a_no_op():
    """No actives means no work — but the tick must not raise and must
    leave the snapshot's seed lists untouched."""
    fn = _resolve_tick_seeds()
    if fn is None:
        pytest.skip("tick_seeds not yet wired")

    snap = GameSnapshot(genre_slug="x", world_slug="y")
    pack = _make_pack([])

    fn(snap, pack, now_turn=100)

    assert snap.active_seeds == []
    assert snap.seed_ghosts == []


def test_tick_preserves_pre_existing_ghosts():
    """A snapshot that already carries ghosts (from a prior session
    segment) must not have them lost when a new expiry fires. The tick
    appends, never replaces."""
    fn = _resolve_tick_seeds()
    if fn is None:
        pytest.skip("tick_seeds not yet wired")

    pre_existing = SeedGhost(
        id="ancestor",
        name="An Ancestor Seed",
        expired_at_turn=10,
        delivery_hints=["legacy hint"],
    )
    snap = GameSnapshot(genre_slug="x", world_slug="y")
    snap.seed_ghosts = [pre_existing]
    snap.active_seeds = [_active("fresh", activated_at=20, lifespan=2)]
    pack = _make_pack([_seed("fresh", lifespan=2)])

    fn(snap, pack, now_turn=22)

    ghost_ids = [g.id for g in snap.seed_ghosts]
    assert "ancestor" in ghost_ids, (
        "Pre-existing ghost was dropped — tick must append, not replace."
    )
    assert "fresh" in ghost_ids, "Freshly expired seed must be appended"
    assert snap.active_seeds == []
