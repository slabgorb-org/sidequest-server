"""Tests for ``sidequest.server.dispatch.monster_manual_inject``.

Covers the per-turn injection seam that materializes Monster Manual
entries into ``snapshot.npcs`` instead of appending text to the narrator
prompt (gaslighting doctrine — the Python deviation from the Rust port).

Includes a wiring test that asserts the module is actually called from
``_execute_narration_turn`` (CLAUDE.md: every test suite needs a wiring
test).
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from sidequest.game.monster_manual import EntryState, ManualEncounter, ManualNpc, MonsterManual
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.server.dispatch import monster_manual_inject


def _snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="mutant_wasteland",
        world_slug="flickering_reach",
        characters=[],
        quest_log={},
        lore_established=[],
        discovered_regions=[],
        turn_manager=TurnManager(),
    )


def _manual_with(
    npcs: list[ManualNpc] | None = None, encounters: list[ManualEncounter] | None = None
) -> MonsterManual:
    return MonsterManual(
        genre="mutant_wasteland",
        world="flickering_reach",
        npcs=list(npcs or []),
        encounters=list(encounters or []),
    )


def _human(
    name: str, *, state: EntryState = EntryState.AVAILABLE, activated_location: str | None = None
) -> ManualNpc:
    return ManualNpc(
        data={
            "name": name,
            "role": "scavenger",
            "culture": "Scrapborn",
            "ocean_summary": "blunt and competitive",
            "dialogue_quirks": ["shouts prices"],
        },
        name=name,
        role="scavenger",
        culture="Scrapborn",
        state=state,
        activated_location=activated_location,
    )


def _creature_encounter(*, enemy_name: str, tier: int = 2, hp: int = 9) -> ManualEncounter:
    return ManualEncounter(
        data={
            "enemies": [
                {
                    "name": enemy_name,
                    "class": "salt_burrower",
                    "tier": tier,
                    "hp": hp,
                    "role": "burrowing ambusher",
                    "abilities": ["Burrow — emerges from a tile within 5m"],
                    "morale": "steady",
                }
            ]
        },
        label=f"1x {enemy_name} (tier {tier})",
        tier=tier,
        state=EntryState.AVAILABLE,
    )


class _FakeSessionData:
    """Minimal stand-in for ``_SessionData`` used by ``ensure_loaded``.

    Only the attributes ``ensure_loaded`` and ``inject`` touch are
    populated; everything else stays unset so the test fails loudly if
    the helper grows new dependencies without us noticing.
    """

    def __init__(
        self,
        *,
        genre_slug: str = "mutant_wasteland",
        world_slug: str = "flickering_reach",
        genre_pack: object | None = None,
    ) -> None:
        self.genre_slug = genre_slug
        self.world_slug = world_slug
        self.genre_pack = genre_pack
        self.monster_manual: MonsterManual | None = None


# ---------------------------------------------------------------------------
# ensure_loaded
# ---------------------------------------------------------------------------


def test_ensure_loaded_returns_none_without_genre() -> None:
    sd = _FakeSessionData(genre_slug="")
    assert monster_manual_inject.ensure_loaded(sd) is None
    assert sd.monster_manual is None


def test_ensure_loaded_is_idempotent() -> None:
    manual = _manual_with(
        npcs=[_human("Krag"), _human("Vex"), _human("Mab"), _human("Tess")],
        encounters=[_creature_encounter(enemy_name="Salt Burrower")],
    )
    sd = _FakeSessionData()
    sd.monster_manual = manual
    # No seeding (already populated) — second call returns the same instance.
    first = monster_manual_inject.ensure_loaded(sd)
    second = monster_manual_inject.ensure_loaded(sd)
    assert first is manual
    assert second is manual


def test_ensure_loaded_skips_seed_without_source_dir(tmp_path: Path) -> None:
    sd = _FakeSessionData(genre_slug="ghost_genre", world_slug="ghost_world", genre_pack=None)
    # Redirect manuals dir to tmp so the test doesn't touch ~/.sidequest.
    with mock.patch(
        "sidequest.game.monster_manual.MonsterManual._manuals_dir", return_value=tmp_path
    ):
        loaded = monster_manual_inject.ensure_loaded(sd)
    assert loaded is not None
    assert loaded.needs_seeding()  # would-have-seeded but no source_dir → empty pool
    assert sd.monster_manual is loaded


def test_ensure_loaded_swallows_seed_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Pack:
        source_dir = tmp_path / "packs" / "mutant_wasteland"

    sd = _FakeSessionData(genre_pack=_Pack())

    def _explode(**_kwargs: object) -> None:
        raise RuntimeError("encountergen unavailable")

    monkeypatch.setattr(
        "sidequest.server.dispatch.pregen.seed_manual",
        _explode,
    )
    with mock.patch(
        "sidequest.game.monster_manual.MonsterManual._manuals_dir", return_value=tmp_path
    ):
        loaded = monster_manual_inject.ensure_loaded(sd)
    # Seed crashed; helper continues with whatever was on disk (empty Manual).
    assert loaded is not None
    assert sd.monster_manual is loaded


# ---------------------------------------------------------------------------
# inject — patch generation
# ---------------------------------------------------------------------------


def test_inject_no_manual_is_noop() -> None:
    sd = _FakeSessionData()
    snap = _snapshot()
    assert monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=False) == 0
    assert snap.npcs == []


def test_inject_materializes_available_humans_top_three() -> None:
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        npcs=[_human(f"Person{i}") for i in range(5)],
    )
    snap = _snapshot()
    count = monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=False)
    assert count == 3
    names = [n.core.name for n in snap.npcs]
    assert names == ["Person0", "Person1", "Person2"]
    # Humans default to neutral disposition (creature fields absent).
    assert all(int(n.disposition) == 0 for n in snap.npcs)


def test_inject_filters_active_humans_by_location_substring() -> None:
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        npcs=[
            _human(
                "Anchored", state=EntryState.ACTIVE, activated_location="The Collapsed Transit Hub"
            ),
            _human("Elsewhere", state=EntryState.ACTIVE, activated_location="Far Plateau"),
            _human("FloatAvail"),
        ],
    )
    snap = _snapshot()
    monster_manual_inject.inject(sd, snap, current_location="Collapsed Transit", in_combat=False)
    names = [n.core.name for n in snap.npcs]
    assert "Anchored" in names
    assert "Elsewhere" not in names
    assert "FloatAvail" in names  # available NPCs always surface (top 3 cap)


def test_inject_skips_dormant_humans() -> None:
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        npcs=[
            _human("DormantOne", state=EntryState.DORMANT, activated_location="The Dome"),
            _human("DormantTwo", state=EntryState.DORMANT, activated_location="The Dome"),
        ],
    )
    snap = _snapshot()
    count = monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=False)
    assert count == 0
    assert snap.npcs == []


def test_inject_materializes_encounter_creatures_with_hostile_disposition() -> None:
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        encounters=[_creature_encounter(enemy_name="Salt Burrower", tier=2, hp=12)],
    )
    snap = _snapshot()
    count = monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=True)
    assert count == 1
    npc = snap.npcs[0]
    assert npc.core.name == "Salt Burrower"
    # Hostile default from _npc_from_patch when creature fields are present.
    assert int(npc.disposition) == -20
    assert npc.threat_level == 2
    assert npc.creature_id == "salt_burrower"
    # HP→EdgePool translation (ADR-078).
    assert npc.core.edge.current == 12
    assert npc.core.edge.max == 12


def test_inject_out_of_combat_caps_encounters() -> None:
    encs = [_creature_encounter(enemy_name=f"Mob{i}") for i in range(5)]
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(encounters=encs)
    snap = _snapshot()
    count = monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=False)
    # Out of combat: only the first 2 encounters' enemies materialize.
    assert count == 2
    assert [n.core.name for n in snap.npcs] == ["Mob0", "Mob1"]


def test_inject_in_combat_materializes_all_available_encounters() -> None:
    encs = [_creature_encounter(enemy_name=f"Mob{i}") for i in range(5)]
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(encounters=encs)
    snap = _snapshot()
    count = monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=True)
    assert count == 5


def test_inject_is_idempotent_across_turns() -> None:
    """Re-injecting the same Manual into a snapshot merges, doesn't duplicate."""
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        encounters=[_creature_encounter(enemy_name="Salt Burrower", hp=9)],
    )
    snap = _snapshot()
    monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=True)
    monster_manual_inject.inject(sd, snap, current_location="The Dome", in_combat=True)
    # _merge_npc_patch path — same name lands once.
    assert len(snap.npcs) == 1


# ---------------------------------------------------------------------------
# Playtest 2026-05-11 regression — location stamp on injected NPCs.
#
# Manual-injected NPCs (humans + encounter creatures) were materialized
# into ``snapshot.npcs`` with ``location=None``, ``last_seen_location=None``,
# ``pool_origin=None``. Downstream, ``in_same_zone()`` masked every
# co-located target, the narrator received ``npcs_present=0`` for the
# entire dive, and the monster manual was effectively dormant. Inject
# must stamp ``location=current_location`` (or the Active anchor) onto
# every patch so the projection layer can match them to the party.
# ---------------------------------------------------------------------------


def test_inject_stamps_current_location_on_available_humans() -> None:
    """Available (non-Active) humans materialize at the party's current location
    so ``in_same_zone()`` matches them — otherwise the monster manual is
    invisible to the narrator (playtest 2026-05-11)."""
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(npcs=[_human("Hob")])
    snap = _snapshot()

    monster_manual_inject.inject(sd, snap, current_location="Sünden Square", in_combat=False)

    assert len(snap.npcs) == 1
    assert snap.npcs[0].core.name == "Hob"
    assert snap.npcs[0].location == "Sünden Square", (
        "Available human injected with location=None; in_same_zone() will "
        "mask this NPC from every co-located visibility query."
    )


def test_inject_uses_activated_location_for_active_humans() -> None:
    """Active humans carry an explicit anchor — prefer that over the
    party's current_location (the anchor is the substring-match seam that
    gated their inclusion, so it's the canonical location)."""
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        npcs=[_human("Kern", state=EntryState.ACTIVE, activated_location="The Recruiter's Post")],
    )
    snap = _snapshot()

    monster_manual_inject.inject(
        sd, snap, current_location="Sünden Square — The Recruiter's Post", in_combat=False
    )

    npc = next(n for n in snap.npcs if n.core.name == "Kern")
    assert npc.location == "The Recruiter's Post"


def test_inject_stamps_current_location_on_encounter_creatures() -> None:
    """Creature patches from encounters must carry the party's current
    location too — otherwise creatures are invisible to in_same_zone()."""
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        encounters=[_creature_encounter(enemy_name="Chalk Moth", tier=1, hp=1)],
    )
    snap = _snapshot()

    monster_manual_inject.inject(sd, snap, current_location="Grimvault — Receiving", in_combat=True)

    creature = next(n for n in snap.npcs if n.core.name == "Chalk Moth")
    assert creature.location == "Grimvault — Receiving", (
        "Creature injected with location=None — invisible to in_same_zone()."
    )


def test_inject_leaves_location_none_when_current_location_blank() -> None:
    """Empty current_location is not a valid zone — don't stamp it.

    Pre-chargen and pre-opening turns sometimes call inject() without a
    bound location. An empty string would be no better than None for
    in_same_zone() matching and would pollute the projection. Leave
    location=None in this case (loud failure shape — the warning fires
    upstream)."""
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(
        encounters=[_creature_encounter(enemy_name="Driftling", tier=1, hp=2)],
    )
    snap = _snapshot()

    monster_manual_inject.inject(sd, snap, current_location="", in_combat=True)

    assert snap.npcs[0].location is None


def test_inject_threat_tier_falls_back_to_encounter_tier() -> None:
    enc = ManualEncounter(
        data={
            "enemies": [
                {"name": "Tier-less Foe", "class": "shade", "hp": 3, "role": "lurker"},
            ]
        },
        label="1x Tier-less Foe (tier 3)",
        tier=3,
        state=EntryState.AVAILABLE,
    )
    sd = _FakeSessionData()
    sd.monster_manual = _manual_with(encounters=[enc])
    snap = _snapshot()
    monster_manual_inject.inject(sd, snap, current_location="", in_combat=True)
    assert snap.npcs[0].threat_level == 3


# ---------------------------------------------------------------------------
# mark_active_from_narration
# ---------------------------------------------------------------------------


def test_mark_active_from_narration_flips_state_for_named_npcs() -> None:
    manual = _manual_with(npcs=[_human("Krag Dustwelder"), _human("Vex")])
    activated = monster_manual_inject.mark_active_from_narration(
        manual,
        "Krag Dustwelder waves you over to the workbench.",
        current_location="The Workbench",
    )
    assert activated == ["Krag Dustwelder"]
    krag = next(n for n in manual.npcs if n.name == "Krag Dustwelder")
    assert krag.state == EntryState.ACTIVE
    assert krag.activated_location == "The Workbench"
    vex = next(n for n in manual.npcs if n.name == "Vex")
    assert vex.state == EntryState.AVAILABLE


def test_mark_active_from_narration_skips_already_active() -> None:
    """Only AVAILABLE → ACTIVE transitions are returned (Rust parity)."""
    manual = _manual_with(
        npcs=[_human("Krag", state=EntryState.ACTIVE, activated_location="Older Scene")],
    )
    activated = monster_manual_inject.mark_active_from_narration(
        manual,
        "Krag turns toward the door.",
        current_location="New Scene",
    )
    assert activated == []
    # activated_location stays anchored to the first scene — mark_active
    # only re-anchors if previously None.
    assert manual.npcs[0].activated_location == "Older Scene"


def test_mark_active_from_narration_empty_narration_returns_empty() -> None:
    manual = _manual_with(npcs=[_human("Krag")])
    assert monster_manual_inject.mark_active_from_narration(manual, "", "any") == []


# ---------------------------------------------------------------------------
# mark_all_dormant
# ---------------------------------------------------------------------------


def test_mark_all_dormant_none_safe() -> None:
    monster_manual_inject.mark_all_dormant(None)  # must not raise


def test_mark_all_dormant_transitions_active_entries() -> None:
    manual = _manual_with(
        npcs=[
            _human("ActiveOne", state=EntryState.ACTIVE, activated_location="loc"),
            _human("AvailableOne"),
        ],
        encounters=[_creature_encounter(enemy_name="ActiveCreature")],
    )
    manual.encounters[0].state = EntryState.ACTIVE
    monster_manual_inject.mark_all_dormant(manual)
    assert manual.npcs[0].state == EntryState.DORMANT
    assert manual.npcs[1].state == EntryState.AVAILABLE  # unchanged
    assert manual.encounters[0].state == EntryState.DORMANT


# ---------------------------------------------------------------------------
# Wiring — production path actually calls the helper
# ---------------------------------------------------------------------------


def test_websocket_session_handler_wires_monster_manual_inject() -> None:
    """CLAUDE.md wiring rule: production code must actually call inject.

    Tests can pass in isolation while the helper sits in a vestigial
    module nobody imports. Read the handler source and assert the
    inject + ensure_loaded calls survive any future refactor that
    silently severs the wiring.
    """
    handler_src = (
        Path(__file__).resolve().parents[3]
        / "sidequest"
        / "server"
        / "websocket_session_handler.py"
    )
    text = handler_src.read_text(encoding="utf-8")
    assert "monster_manual_inject" in text, (
        "websocket_session_handler.py no longer imports monster_manual_inject"
    )
    assert "monster_manual_inject.ensure_loaded" in text, (
        "_execute_narration_turn no longer calls ensure_loaded"
    )
    assert "monster_manual_inject.inject" in text, "_execute_narration_turn no longer calls inject"
    assert "monster_manual_inject.mark_active_from_narration" in text, (
        "post-narration mark_active_from_narration wire missing"
    )
    assert "monster_manual_inject.mark_all_dormant" in text, (
        "location-change mark_all_dormant wire missing"
    )
    assert "sd.monster_manual.save()" in text, (
        "Manual.save() is not called after each turn — lifecycle won't persist"
    )


# ---------------------------------------------------------------------------
# Regression — turn_context.monster_manual staleness (playtest 2026-05-17)
#
# _build_turn_context (session_helpers.py) snapshots monster_manual off
# sd.monster_manual at the *caller* (player_action.py / dice_throw.py /
# ws_handler), which runs BEFORE _execute_narration_turn's lazy
# ensure_loaded(sd). The handler refreshes turn_context.npcs from the
# post-inject snapshot but historically NOT turn_context.monster_manual,
# so the orchestrator received context.monster_manual=None on every
# session's turn 1 and every MP turn where the acting player's per-player
# _SessionData had not yet narrated. Consequence: ToolContext.monster_manual
# was None → lookup_monster returned nothing → narrator improvised stats,
# and the context_wired OTEL flag logged a false negative (~47% of turns
# in the 2026-05-17 logs).
# ---------------------------------------------------------------------------


def _fake_local_dm() -> MagicMock:
    """Dormant LocalDM stub (offline-only design, 2026-04-28 spec) — keeps
    _execute_narration_turn off the decompose path so the test isolates
    the monster_manual refresh seam."""
    from sidequest.protocol.dispatch import DispatchPackage

    fake_dm = MagicMock()
    fake_dm.decompose = AsyncMock(
        return_value=DispatchPackage(
            turn_id="t-mm-refresh",
            per_player=[],
            cross_player=[],
            confidence_global=0.0,
        )
    )
    return fake_dm


@pytest.mark.asyncio
async def test_execute_narration_turn_refreshes_stale_monster_manual(
    session_fixture,
) -> None:
    """The orchestrator must receive turn_context.monster_manual refreshed
    from sd.monster_manual after ensure_loaded — not the stale None the
    caller snapshotted before the lazy load."""
    from tests.server.conftest import (
        _build_turn_context_for_test,
        _make_minimal_narration_turn_result,
    )

    sd, handler = session_fixture

    # Manual is on the session. ensure_loaded short-circuits on its first
    # line (sd.monster_manual is not None) — no disk, no seeding.
    manual = _manual_with(npcs=[_human("Krag")])
    sd.monster_manual = manual

    captured: dict[str, object] = {}

    async def _capture(action: str, turn_context: object, *, room: object = None) -> object:
        captured["turn_context"] = turn_context
        return _make_minimal_narration_turn_result()

    sd.orchestrator.run_narration_turn = AsyncMock(side_effect=_capture)
    sd.local_dm = _fake_local_dm()
    handler._validator = MagicMock()
    handler._validator.submit = AsyncMock()
    handler._validator.is_running = MagicMock(return_value=True)

    # Production ordering: the caller built the context BEFORE the Manual
    # was visible, so monster_manual defaults to None.
    turn_context = _build_turn_context_for_test(sd)
    assert turn_context.monster_manual is None, "precondition: context built stale"

    await handler._execute_narration_turn(sd, "I look around.", turn_context)

    received = captured["turn_context"]
    assert received.monster_manual is manual, (
        "turn_context.monster_manual not refreshed from sd.monster_manual "
        "after ensure_loaded — orchestrator gets None, lookup_monster is "
        "dead, and context_wired logs a false negative."
    )
