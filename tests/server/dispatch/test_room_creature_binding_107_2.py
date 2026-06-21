"""Story 107-2 (RED) — server-side per-room creature binding + OTEL.

Covers the server half of AC1/AC2/AC3 and all of AC5. TEA-defined contract
(logged as a deviation; ratified by Keith's 2026-06-13 "proceed fixture-driven"
ruling because the live per-room key is owned by the still-unstarted 107-1):

1. ``resolve_room_creatures(pack, world_slug, room_id) -> list[str]`` — reads the
   room's ``encounter_creatures`` binding and returns the bound bestiary ids.
   Empty list for a non-combat room (legitimate); raises
   ``RoomCreatureBindingError`` for a binding that references an unknown bestiary
   id (No Silent Fallbacks — AC5: an unresolved binding fails LOUD, never a
   silent empty pool). Emits ``monster_manual.room_bound``.

2. ``monster_manual_inject.inject(..., room_id=<id>)`` — when a room id is given
   (sourced from ``snapshot.region_for()`` / ``pc_regions`` — 107-1's key), the
   room's bound bestiary creature is materialized into ``snapshot.npcs`` with its
   AUTHORED name (AC1: "Gnaw-Swarm", not "the creature of animal musk") and the
   ``monster_manual.room_bound`` span fires (AC5 lie-detector). ``room_id=None``
   preserves today's behavior exactly (back-compat — every existing inject test
   passes None implicitly).

These tests are fixture-driven: the room key is set directly rather than produced
by a live descent, so they RED now and the 107-1 live-key wiring closes the
end-to-end loop later (tracked as a blocking Delivery Finding).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import yaml

from sidequest.game.monster_manual import MonsterManual
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.server.dispatch import monster_manual_inject  # noqa: E402

# RED: this module does not exist yet — the whole file fails at collection until
# Naomi (dev) authors sidequest/server/dispatch/room_creature_binding.py.
from sidequest.server.dispatch.room_creature_binding import (  # noqa: E402
    RoomCreatureBindingError,
    resolve_room_creatures,
)
from sidequest.telemetry.spans.monster_manual import (  # noqa: E402
    SPAN_MONSTER_MANUAL_ROOM_BOUND,
)
from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

# ---------------------------------------------------------------------------
# Synthetic world builder — a tmp pack with one bestiary entry + one room.
# Lets the resolver/inject contract be exercised with zero shipped-content
# dependency (and zero 107-1 dependency).
# ---------------------------------------------------------------------------


def _synthetic_pack(tmp_path: Path, *, room_id: str, encounter_creatures: list[str]):
    from sidequest.genre.models.bestiary import Bestiary, BestiaryEntry

    bestiary = Bestiary(
        entries=[
            BestiaryEntry(
                id="real_beast",
                name="Real Beast",
                level=1,
                hp=4,
                armor_class=12,
                attack_bonus=1,
                abilities=["Gnaws"],
            )
        ]
    )
    rooms_dir = tmp_path / "worlds" / "sunken" / "rooms"
    rooms_dir.mkdir(parents=True)
    (rooms_dir / f"{room_id}.yaml").write_text(
        yaml.safe_dump(
            {
                "room_type": "settlement",
                "name": "Test Room",
                "encounter_creatures": encounter_creatures,
            }
        ),
        encoding="utf-8",
    )
    return SimpleNamespace(
        source_dir=tmp_path,
        rules=SimpleNamespace(combat_encounters=True),
        effective_bestiary=lambda world: (bestiary, "world"),
    )


def _snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="sunken",
        characters=[],
        quest_log={},
        lore_established=[],
        discovered_regions=[],
        turn_manager=TurnManager(),
    )


# ---------------------------------------------------------------------------
# resolve_room_creatures
# ---------------------------------------------------------------------------


def test_resolve_returns_bound_creature_ids(tmp_path: Path) -> None:
    pack = _synthetic_pack(tmp_path, room_id="den", encounter_creatures=["real_beast"])
    assert resolve_room_creatures(pack, "sunken", "den") == ["real_beast"]


def test_resolve_returns_empty_for_unbound_room(tmp_path: Path) -> None:
    """A room with no `encounter_creatures` is a legitimate non-combat room —
    return [], do NOT raise (the fail-loud is only for a *declared* binding that
    cannot resolve)."""
    pack = _synthetic_pack(tmp_path, room_id="quiet", encounter_creatures=[])
    assert resolve_room_creatures(pack, "sunken", "quiet") == []


def test_resolve_raises_on_dangling_bestiary_ref(tmp_path: Path) -> None:
    """AC5 fail-loud: a binding that references an unknown bestiary id is an
    authoring error — raise, never silently surface an empty pool (the 87-4 bug
    shape)."""
    pack = _synthetic_pack(tmp_path, room_id="bad", encounter_creatures=["ghost_beast"])
    with pytest.raises(RoomCreatureBindingError):
        resolve_room_creatures(pack, "sunken", "bad")


def test_resolve_raises_binding_error_not_attribute_error_on_none_bestiary(
    tmp_path: Path,
) -> None:
    """153-26 rework (HIGH): a declared binding against a world whose
    ``effective_bestiary`` is ``None`` (no bestiary authored at all) must raise
    :class:`RoomCreatureBindingError` — the SAME loud-but-typed authoring-error
    signal as a dangling ref — NOT a raw ``AttributeError`` from dereferencing
    ``None.entries``. The materializer degrade path catches
    ``RoomCreatureBindingError`` to stay loud-but-graceful; a bare
    ``AttributeError`` would slip that catch and crash the player-facing
    connect (Reviewer HIGH finding)."""
    rooms_dir = tmp_path / "worlds" / "sunken" / "rooms"
    rooms_dir.mkdir(parents=True)
    (rooms_dir / "den.yaml").write_text(
        yaml.safe_dump({"encounter_creatures": ["real_beast"]}),
        encoding="utf-8",
    )
    pack = SimpleNamespace(
        source_dir=tmp_path,
        effective_bestiary=lambda world: (None, "world"),
    )
    with pytest.raises(RoomCreatureBindingError):
        resolve_room_creatures(pack, "sunken", "den")


def test_resolve_raises_binding_error_not_yaml_error_on_malformed_room_yaml(
    tmp_path: Path,
) -> None:
    """153-26 rework round 2 (HIGH): a MALFORMED ``rooms/<id>.yaml`` (a fat-fingered
    homebrew edit — unterminated flow sequence here) must raise the typed
    :class:`RoomCreatureBindingError`, NOT leak a raw ``yaml.YAMLError``. The
    materializer degrade path catches ``RoomCreatureBindingError`` to stay
    loud-but-graceful; a bare ``yaml.YAMLError`` would slip that catch and crash
    the player-facing connect on degrade — the same failure mode as a dangling
    ref, via a sibling exception."""
    # Build a valid pack first (creates the rooms dir + bestiary), then drop a
    # malformed sibling room file in.
    pack = _synthetic_pack(tmp_path, room_id="other", encounter_creatures=["real_beast"])
    rooms_dir = tmp_path / "worlds" / "sunken" / "rooms"
    # Unterminated YAML flow sequence → yaml.safe_load raises yaml.YAMLError.
    (rooms_dir / "broken.yaml").write_text(
        "encounter_creatures: [real_beast, ghost\n", encoding="utf-8"
    )
    with pytest.raises(RoomCreatureBindingError):
        resolve_room_creatures(pack, "sunken", "broken")


def test_resolve_emits_room_bound_span(tmp_path: Path) -> None:
    """AC5 lie-detector: resolving a room's binding emits monster_manual.room_bound
    naming the room and the bound creature, so the GM panel can confirm the
    narrator drew the authored creature for this room."""
    pack = _synthetic_pack(tmp_path, room_id="den", encounter_creatures=["real_beast"])
    with mock.patch("sidequest.server.dispatch.room_creature_binding.Span.open") as span_open:
        resolve_room_creatures(pack, "sunken", "den")
    attrs = next(
        call.args[1]
        for call in span_open.call_args_list
        if call.args and call.args[0] == SPAN_MONSTER_MANUAL_ROOM_BOUND
    )
    assert attrs["room_id"] == "den"
    assert "real_beast" in attrs["bound_creatures"]


# ---------------------------------------------------------------------------
# inject(..., room_id=...) — materializes the AUTHORED opponent
# ---------------------------------------------------------------------------


def test_inject_with_room_id_materializes_authored_opponent(tmp_path: Path) -> None:
    """AC1/AC2: given the party's room id, inject surfaces the room's bound
    bestiary creature into snapshot.npcs under its AUTHORED name ('Real Beast'),
    so the narrator names the authored creature instead of improvising 'the
    creature of animal musk'. The Manual is empty here — the opponent comes purely
    from the per-room binding."""
    sd = SimpleNamespace(
        genre_slug="caverns_and_claudes",
        world_slug="sunken",
        genre_pack=_synthetic_pack(tmp_path, room_id="den", encounter_creatures=["real_beast"]),
        monster_manual=MonsterManual(genre="caverns_and_claudes", world="sunken"),
    )
    snap = _snapshot()
    monster_manual_inject.inject(
        sd, snap, current_location="The Den", in_combat=True, room_id="den"
    )
    names = [n.core.name for n in snap.npcs]
    assert "Real Beast" in names, (
        f"per-room bound opponent not materialized under its authored name; got {names}"
    )


def test_inject_without_room_id_is_unchanged(tmp_path: Path) -> None:
    """Back-compat guard: room_id=None (the default every existing caller uses)
    must NOT materialize binding creatures — the new path is strictly additive and
    gated on a room id being supplied."""
    sd = SimpleNamespace(
        genre_slug="caverns_and_claudes",
        world_slug="sunken",
        genre_pack=_synthetic_pack(tmp_path, room_id="den", encounter_creatures=["real_beast"]),
        monster_manual=MonsterManual(genre="caverns_and_claudes", world="sunken"),
    )
    snap = _snapshot()
    count = monster_manual_inject.inject(sd, snap, current_location="The Den", in_combat=True)
    assert count == 0
    assert snap.npcs == []


def test_inject_with_room_id_emits_room_bound_span(tmp_path: Path) -> None:
    sd = SimpleNamespace(
        genre_slug="caverns_and_claudes",
        world_slug="sunken",
        genre_pack=_synthetic_pack(tmp_path, room_id="den", encounter_creatures=["real_beast"]),
        monster_manual=MonsterManual(genre="caverns_and_claudes", world="sunken"),
    )
    snap = _snapshot()
    with mock.patch.object(monster_manual_inject.Span, "open") as span_open:
        monster_manual_inject.inject(
            sd, snap, current_location="The Den", in_combat=True, room_id="den"
        )
    fired = [c.args[0] for c in span_open.call_args_list if c.args]
    assert SPAN_MONSTER_MANUAL_ROOM_BOUND in fired, (
        "inject() did not emit monster_manual.room_bound for the bound room"
    )


# ---------------------------------------------------------------------------
# Span declaration contract
# ---------------------------------------------------------------------------


def test_room_bound_span_is_declared_flat() -> None:
    """The span name is stable and registered as a flat-only span (parity with
    monster_manual.injected), so the GM-panel dashboard reads it without change."""
    from sidequest.telemetry.spans._core import FLAT_ONLY_SPANS

    assert SPAN_MONSTER_MANUAL_ROOM_BOUND == "monster_manual.room_bound"
    assert SPAN_MONSTER_MANUAL_ROOM_BOUND in FLAT_ONLY_SPANS


# ---------------------------------------------------------------------------
# Wiring — drive the REAL production inject() with REAL shipped content.
#
# CLAUDE.md "No Source-Text Wiring Tests": this asserts BEHAVIOR through the
# production injection function (the one websocket_session_handler calls),
# end-to-end resolve_room_creatures -> bestiary -> snapshot.npcs, on the real
# beneath_sunden content. The handler->inject room_id plumbing (region_for) is
# 107-1's seam, tracked as a blocking Delivery Finding.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk")
def test_real_beneath_sunden_entrance_surfaces_authored_gnaw_swarm() -> None:
    from sidequest.genre.loader import load_genre_pack

    try:
        pack_dir = find_pack_path("caverns_and_claudes")
    except PackNotFound:
        pytest.skip("caverns_and_claudes not on disk")
    pack = load_genre_pack(pack_dir)

    # AC3 (server, real content): the entrance binds gnaw_swarm.
    bound = resolve_room_creatures(pack, "beneath_sunden", "entrance")
    assert "gnaw_swarm" in bound, (
        "entrance room does not resolve to the authored gnaw_swarm — the first "
        "fight will still improvise"
    )

    # AC1 (real content, through production inject): the materialized opponent
    # carries the bestiary's authored name, not an improvised label.
    bestiary, _ = pack.effective_bestiary("beneath_sunden")
    gnaw = next(e for e in bestiary.entries if e.id == "gnaw_swarm")

    sd = SimpleNamespace(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        genre_pack=pack,
        monster_manual=MonsterManual(genre="caverns_and_claudes", world="beneath_sunden"),
    )
    snap = _snapshot()
    snap.world_slug = "beneath_sunden"
    monster_manual_inject.inject(
        sd, snap, current_location="Under the Rope", in_combat=True, room_id="entrance"
    )
    names = [n.core.name for n in snap.npcs]
    assert gnaw.name in names, (
        f"production inject did not surface the authored {gnaw.name!r} for the "
        f"entrance room; got {names}"
    )
