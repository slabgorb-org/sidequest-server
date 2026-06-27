"""Playtest 2026-05-31 (burning_peace MP) — a confrontation must seat the
NAMED OPPONENT as the Other, even when that opponent was minted only in
narration and is not yet a tracked NPC.

Live failure (OTEL lie-detector): both players committed force against a
present, *resisting* watcher ("lunge and grab the watcher by the wrist, twist
to pin"). The intent router classified it as ``confrontation=combat`` and the
dispatch ran — but the grey-robed watcher was never materialized into
``snapshot.npcs``, so the location fallback found no opponent
(``encounter.no_opponent_available``) and the contest collapsed to prose:
``encounter=null`` / ``total_beats_fired=0`` / no ``DICE_THROW``. That is the
exact Illusionism SideQuest exists to prevent (SOUL: mechanical scaffold).

Root cause: the router never named the Other. ``run_confrontation_dispatch``
already has the materialize-the-Other channel (story 59-23, ADR-116) but it was
keyed only on ``params["threat"]`` (ship_combat) and the router-prompt never
told the router to populate it for a personal contest. The fix generalizes the
channel to ``params["opponent"]`` and documents it in the router prompt.

These tests pin the ENGINE half (the prompt half is asserted behaviorally in
tests/agents/test_intent_router.py). Skips when sidequest-content is absent.
"""

from __future__ import annotations

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

pytestmark = pytest.mark.asyncio

# Content-authored combat opponent stats (elemental_harmony rules.yaml
# opponent_default_stats — "Wuxia mook: AC 12, HP 8").
_MOOK_HP = 8

# Full elemental_harmony canonical WN stat block so the player's initiative roll +
# attack params never KeyError (the WWN module fails loud on a missing stat).
_STATS = {
    "STR": 12,
    "DEX": 12,
    "CON": 12,
    "INT": 12,
    "WIS": 12,
    "CHA": 12,
}

_PC = "Sora Tidewalker"
_LOCATION = "Edo — The Forge Quarter & the Wharf"
# The narratively-minted adversary — named in the fiction, NOT a tracked NPC.
_OPPONENT = "the grey-robed watcher"


def _has_real_content() -> bool:
    return GENRE_PACKS_DIR.is_dir()


def _load_elemental_harmony():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("elemental_harmony"))
    except PackNotFound:
        pytest.skip("sidequest-content not on disk in this checkout")


def _player_character(name: str):
    from sidequest.game.character import Character
    from sidequest.game.creature_core import CreatureCore, Inventory

    return Character(
        core=CreatureCore(
            name=name,
            description="A wanderer of the tide reaches.",
            personality="steady",
            inventory=Inventory(items=[{"id": "fists", "name": "Fists"}]),
            hp={"current": 10, "max": 10, "base_max": 10},
        ),
        char_class="Wanderer",
        race="Tide Reaches",
        backstory="Followed the tide out.",
        stats=dict(_STATS),
    )


def _snapshot_with_no_opponent():
    """A wharf scene: the PC alone — the watcher exists only in prose."""
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager

    snap = GameSnapshot(
        genre_slug="elemental_harmony",
        world_slug="burning_peace",
        turn_manager=TurnManager(interaction=4),
    )
    snap.character_locations[_PC] = _LOCATION
    snap.characters.append(_player_character(_PC))
    return snap


def _opponent_dispatch(*, key: str = "sora_grapple", with_opponent: bool = True):
    from sidequest.protocol.dispatch import SubsystemDispatch, VisibilityTag

    params: dict = {"type": "combat"}
    if with_opponent:
        params["opponent"] = {
            "name": _OPPONENT,
            "description": "a grey-robed figure who twisted to resist the pin",
        }
    return SubsystemDispatch(
        subsystem="confrontation",
        params=params,
        idempotency_key=key,
        confidence=1.0,
        visibility=VisibilityTag(visible_to="all"),
    )


@pytest.fixture
def otel_capture():
    from sidequest.telemetry.setup import init_tracer

    init_tracer()  # idempotent
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), (
        f"expected SDK TracerProvider, got {type(provider)!r}"
    )
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def _opponent_names(enc) -> list[str]:
    return [a.name for a in enc.actors if a.side == "opponent"]


async def test_named_opponent_is_materialized_and_seated_as_other(otel_capture):
    """The grapple case: a personal ``combat`` confrontation naming an opponent
    who is NOT a tracked NPC materializes that opponent and seats it as the
    Other — the encounter starts instead of collapsing to prose."""
    if not _has_real_content():
        pytest.skip("sidequest-content not on disk in this checkout")

    from sidequest.agents.subsystems.confrontation import run_confrontation_dispatch

    pack = _load_elemental_harmony()
    snap = _snapshot_with_no_opponent()

    out = await run_confrontation_dispatch(
        _opponent_dispatch(),
        snapshot=snap,
        pack=pack,
        player_name=_PC,
        npcs_present=[],
    )

    assert out.data.get("error") is None, (
        f"naming the opponent must engage the encounter, not error; got {out.data!r}"
    )
    enc = snap.encounter
    assert enc is not None, (
        "a confrontation naming its opponent must instantiate an encounter "
        "(the watcher is seated as the Other), not no-op to prose"
    )
    assert _OPPONENT in _opponent_names(enc), (
        f"the named opponent {_OPPONENT!r} must be seated side=opponent; "
        f"got opponents={_opponent_names(enc)}"
    )

    # The materialized Other carries a real backing core seeded from
    # opponent_default_stats (hp_depletion needs an HpPool to deplete).
    core = snap.find_creature_core(_OPPONENT)
    assert core is not None, "materialized opponent must have a backing CreatureCore"
    assert core.hp.max == _MOOK_HP, (
        f"opponent hull must seed from opponent_default_stats (hp {_MOOK_HP}); "
        f"got max={core.hp.max}"
    )

    # GM-panel observability: the Other joined via materialization, not a
    # location-fallback seat.
    joined = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "participant.joined" and (s.attributes or {}).get("name") == _OPPONENT
    ]
    assert joined, f"no participant.joined span for the materialized Other {_OPPONENT!r}"
    assert any((s.attributes or {}).get("source") == "materialized" for s in joined), (
        "the opponent's participant.joined span must carry source='materialized' "
        f"got sources={[(s.attributes or {}).get('source') for s in joined]}"
    )


async def test_without_opponent_empty_scene_fails_loud(otel_capture):
    """PIN / regression: the pre-fix dispatch shape (type only, no opponent, no
    co-located NPC) still fails loud — proving the ``opponent`` field is what
    turns the silent no-op into an engaged contest, and that we did NOT seat a
    phantom one-sided encounter (No Silent Fallbacks)."""
    if not _has_real_content():
        pytest.skip("sidequest-content not on disk in this checkout")

    from sidequest.agents.subsystems.confrontation import run_confrontation_dispatch

    pack = _load_elemental_harmony()
    snap = _snapshot_with_no_opponent()

    out = await run_confrontation_dispatch(
        _opponent_dispatch(key="sora_no_opp", with_opponent=False),
        snapshot=snap,
        pack=pack,
        player_name=_PC,
        npcs_present=[],
    )

    assert snap.encounter is None, "no opponent + empty scene must not leave a one-sided encounter"
    assert out.data.get("error") == "no_opponent_available", (
        f"the handler must surface the no-opponent fail-loud signal; got {out.data!r}"
    )
