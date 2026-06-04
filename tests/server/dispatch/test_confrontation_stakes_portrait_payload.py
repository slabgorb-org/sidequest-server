"""RED — Story 85-3 (Tier B confrontation mode): the CONFRONTATION payload
gains ``stakes`` (always present) and per-opponent ``actors[].portrait_url``
(injected resolver, opponent-only) so the promoted dockview panel can show a
stakes banner and give the dial "a face" (ADR-116 — a confrontation requires
an Other).

Contract decided by the Architect (The White Queen, 2026-06-04 — recorded in
``.session/85-3-session.md``):

  * ``stakes: str | None`` is ALWAYS present in the payload dict — NOT
    additive-conditional like the hp keys. ``payload["stakes"] = active_stakes
    or None`` runs unconditionally; an empty string normalizes to ``None`` so
    the UI's "collapse when absent" branch keys off a single falsy contract.
  * Portrait resolution uses an INJECTED ``portrait_resolver: Callable[[str],
    str | None]`` (sibling to the existing ``core_resolver``), applied ONLY to
    ``side == "opponent"`` actors. ``None`` on resolver miss (No Silent
    Fallbacks). Players/companions get portraits via PARTY_STATUS, never here.
  * Production wires ``active_stakes=snapshot.active_stakes`` and a
    ``portrait_resolver`` lambda → ``_resolve_npc_portrait_url(pack,
    world_slug, ...)`` at the call sites; the builder stays unit-testable with
    a fake resolver. ``portrait_url`` rides the serialized actor *dict*, so
    ``EncounterActor`` the pydantic model needs no new field.

These tests drive the REAL builder + the REAL
``make_confrontation_frame_supplier`` — no source-text grep (CLAUDE.md "No
Source-Text Wiring Tests"). They mirror the additive ``win_condition`` /
``player_hp`` precedent in ``test_confrontation_payload_hp.py`` and the
fixture-driven supplier wiring in ``test_wwn_cast_spell_wiring.py``.
"""

from __future__ import annotations

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
)
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.character import ClassDef
from sidequest.genre.models.rules import BeatDef, ConfrontationDef, MetricDef
from sidequest.protocol.messages import ConfrontationPayload
from sidequest.server.dispatch.confrontation import (
    build_confrontation_payload,
    make_confrontation_frame_supplier,
)

_STAKES = "Shake the cruisers or lose the cargo"


# ---------------------------------------------------------------------------
# Fixtures — a dial confrontation with player + opponent + neutral actors so
# the opponent-only portrait scoping is observable.
# ---------------------------------------------------------------------------


def _beat() -> BeatDef:
    return BeatDef(id="floor_it", label="Floor It", kind="push", base=2, stat_check="SPD")


def _cdef() -> ConfrontationDef:
    return ConfrontationDef(
        type="chase",
        label="Highway Pursuit",
        category="movement",
        player_metric=MetricDef(name="separation", starting=0, threshold=10),
        opponent_metric=MetricDef(name="pursuit", starting=0, threshold=10),
        beats=[_beat()],
    )


def _enc() -> StructuredEncounter:
    return StructuredEncounter(
        encounter_type="chase",
        player_metric=EncounterMetric(name="separation", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="pursuit", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Magpie", role="driver", side="player"),
            EncounterActor(name="Divvie Sergeant", role="pursuer", side="opponent"),
            EncounterActor(name="Bystander", role="witness", side="neutral"),
        ],
    )


def _actor(payload: dict, name: str) -> dict:
    """Find the serialized actor dict by name."""
    for a in payload["actors"]:
        if a["name"] == name:
            return a
    raise AssertionError(f"actor {name!r} not in payload actors: {payload['actors']!r}")


# ---------------------------------------------------------------------------
# stakes — always present, normalized, never additive-conditional.
# ---------------------------------------------------------------------------


def test_stakes_key_always_present_defaults_none():
    # No active_stakes threaded (legacy / no-arg path): the key is STILL present
    # with value None. Unlike the hp keys, stakes is meaningful — or explicitly
    # absent — on every confrontation, so the UI always reads one falsy contract.
    payload = build_confrontation_payload(encounter=_enc(), cdef=_cdef(), genre_slug="road_warrior")
    assert "stakes" in payload, f"'stakes' must always be present; keys={sorted(payload)!r}"
    assert payload["stakes"] is None


def test_stakes_carries_active_stakes_value():
    payload = build_confrontation_payload(
        encounter=_enc(),
        cdef=_cdef(),
        genre_slug="road_warrior",
        active_stakes=_STAKES,
    )
    assert payload["stakes"] == _STAKES


def test_empty_active_stakes_normalizes_to_none():
    # active_stakes defaults to "" on GameSnapshot; the builder must emit None,
    # not "", so the UI banner collapses on a single falsy check.
    payload = build_confrontation_payload(
        encounter=_enc(),
        cdef=_cdef(),
        genre_slug="road_warrior",
        active_stakes="",
    )
    assert payload["stakes"] is None


# ---------------------------------------------------------------------------
# portrait_url — injected resolver, opponent-only, None on miss.
# ---------------------------------------------------------------------------


def test_opponent_actor_carries_resolved_portrait_url():
    payload = build_confrontation_payload(
        encounter=_enc(),
        cdef=_cdef(),
        genre_slug="road_warrior",
        portrait_resolver=lambda name: f"https://cdn/{name}.png" if name == "Divvie Sergeant" else None,
    )
    assert _actor(payload, "Divvie Sergeant")["portrait_url"] == "https://cdn/Divvie Sergeant.png"


def test_opponent_portrait_url_none_on_resolver_miss():
    # No Silent Fallbacks: an opponent whose name is not in the manifest gets
    # None, never a substituted placeholder.
    payload = build_confrontation_payload(
        encounter=_enc(),
        cdef=_cdef(),
        genre_slug="road_warrior",
        portrait_resolver=lambda name: None,
    )
    assert _actor(payload, "Divvie Sergeant")["portrait_url"] is None


def test_player_side_actor_never_gets_portrait_even_with_resolver():
    # Players/companions are portrait'd via PARTY_STATUS — the confrontation
    # resolver must be scoped to opponents so we don't double-resolve allies.
    payload = build_confrontation_payload(
        encounter=_enc(),
        cdef=_cdef(),
        genre_slug="road_warrior",
        portrait_resolver=lambda name: "https://cdn/anyone.png",
    )
    assert _actor(payload, "Magpie")["portrait_url"] is None


def test_neutral_side_actor_never_gets_portrait_even_with_resolver():
    payload = build_confrontation_payload(
        encounter=_enc(),
        cdef=_cdef(),
        genre_slug="road_warrior",
        portrait_resolver=lambda name: "https://cdn/anyone.png",
    )
    assert _actor(payload, "Bystander")["portrait_url"] is None


def test_no_portrait_resolver_yields_none_portrait_key_present():
    # Defensive / legacy: no resolver threaded → every actor still carries a
    # portrait_url key (the UI ActorChip reads it), value None.
    payload = build_confrontation_payload(encounter=_enc(), cdef=_cdef(), genre_slug="road_warrior")
    for actor in payload["actors"]:
        assert "portrait_url" in actor, f"actor {actor['name']!r} missing portrait_url key"
        assert actor["portrait_url"] is None


# ---------------------------------------------------------------------------
# Protocol boundary — the production broadcast does
# ConfrontationPayload(**build_confrontation_payload(...)). That model is
# extra="forbid", so `stakes` MUST be a declared field or the wrap raises and
# crashes the confrontation broadcast on every turn. `actors` is list[dict] so
# the per-actor portrait_url survives without a schema change.
# ---------------------------------------------------------------------------


def test_stakes_survives_protocol_boundary():
    payload = build_confrontation_payload(
        encounter=_enc(),
        cdef=_cdef(),
        genre_slug="road_warrior",
        active_stakes=_STAKES,
    )
    model = ConfrontationPayload(**payload)  # must NOT raise (extra="forbid")
    assert model.stakes == _STAKES


def test_stakes_none_survives_protocol_boundary():
    payload = build_confrontation_payload(encounter=_enc(), cdef=_cdef(), genre_slug="road_warrior")
    model = ConfrontationPayload(**payload)
    assert model.stakes is None


def test_opponent_portrait_url_survives_protocol_boundary():
    payload = build_confrontation_payload(
        encounter=_enc(),
        cdef=_cdef(),
        genre_slug="road_warrior",
        portrait_resolver=lambda name: "https://cdn/sergeant.png" if name == "Divvie Sergeant" else None,
    )
    model = ConfrontationPayload(**payload)
    opp = next(a for a in model.actors if a["name"] == "Divvie Sergeant")
    assert opp["portrait_url"] == "https://cdn/sergeant.png"


# ---------------------------------------------------------------------------
# Call-site WIRING — make_confrontation_frame_supplier must thread
# snapshot.active_stakes into the per-recipient projection. Mirrors the
# fixture-driven supplier wiring in test_wwn_cast_spell_wiring.py (no
# source-text grep). Proves the production seam — not just the builder —
# carries the stakes onto the wire.
# ---------------------------------------------------------------------------


def _fighter_class() -> ClassDef:
    return ClassDef(
        id="road_captain",
        display_name="Road Captain",
        rpg_role="tank",
        jungian_default="warrior",
        prime_requisite="SPD",
        minimum_score=9,
        kit_table="road_captain_kit",
        flavor="Keeps the rig pointed forward.",
        encounter_beat_choices=["floor_it"],
    )


def _fighter_char(name: str) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="A road captain.",
            personality="steady",
            hp=HpPool(current=8, max=8, base_max=8),
        ),
        backstory="Drove the last convoy out.",
        char_class="Road Captain",
        race="Human",
    )


class _FakeGenrePack:
    """Minimal stub satisfying resolve_recipient_pc's `.classes` access and the
    portrait resolver's `.worlds` access (a real GenrePack has both). `worlds`
    is empty so `_world_portrait_slugs` returns an empty manifest → opponent
    portraits resolve to None, which is fine: this test asserts stakes wiring,
    not portraits."""

    def __init__(self, classes: list[ClassDef]) -> None:
        self.classes = classes
        self.worlds: dict[str, object] = {}


def _seated_snapshot(active_stakes: str, *, player_id: str = "player-1") -> GameSnapshot:
    snap = GameSnapshot(genre_slug="road_warrior", world_slug="test_world")
    snap.characters = [_fighter_char("Magpie")]
    snap.player_seats = {player_id: "Magpie"}
    snap.active_stakes = active_stakes
    return snap


def test_frame_supplier_threads_active_stakes_onto_the_wire():
    player_id = "player-1"
    snap = _seated_snapshot(_STAKES, player_id=player_id)
    supplier = make_confrontation_frame_supplier(
        snapshot=snap,
        genre_pack=_FakeGenrePack(classes=[_fighter_class()]),
        encounter=_enc(),
        cdef=_cdef(),
        genre_slug="road_warrior",
    )
    frame = supplier(player_id)
    assert frame is not None, "supplier must return a ConfrontationPayload for a seated PC"
    assert frame.stakes == _STAKES, (
        "make_confrontation_frame_supplier must thread snapshot.active_stakes into the "
        f"per-recipient CONFRONTATION frame; got stakes={frame.stakes!r}"
    )


def test_frame_supplier_emits_none_stakes_when_session_has_none():
    player_id = "player-1"
    snap = _seated_snapshot("", player_id=player_id)  # default empty session stakes
    supplier = make_confrontation_frame_supplier(
        snapshot=snap,
        genre_pack=_FakeGenrePack(classes=[_fighter_class()]),
        encounter=_enc(),
        cdef=_cdef(),
        genre_slug="road_warrior",
    )
    frame = supplier(player_id)
    assert frame is not None
    assert frame.stakes is None
