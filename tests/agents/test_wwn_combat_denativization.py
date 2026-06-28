"""WWN combat de-nativization (sq-playtest 2026-06-22, ADR-143/113/074).

A seated WN ``hp_depletion`` combat used to brick the turn: the narrator was
handed the full combat WRITE toolset AND told to drive beats, so it ground
roll/apply/advance in its own claude-agent-sdk loop past ``max_turns=8`` and the
turn died (``Reached maximum number of turns (8)``) before any beat resolved.

The fix mirrors the Fate precedent (``narrator.py`` contest/conflict branches):
under a WN binding the ruleset OWNS the round — combat resolves on the player's
DICE_THROW via ``run_wn_round`` (epic 108), so the narrator must NARRATE the
seated beat, not resolve it. Three coordinated gates, all keyed on the single
predicate ``is_live_wn_combat``:

* A1 — the narrator tool filter withholds combat-RESOLUTION tools.
* A2 — the narrator prompt drops the native beat menu (de-nativized live zone).
* A3 — narration-apply drops a stray narrator beat_selection before the legacy
  dial arm (so a hallucinated beat can't double-apply HP outside the WN round).

These tests pin all three plus the native-pack (``dial``) regression guard.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import sidequest.agents.tools  # noqa: F401 — wires the WRITE-tool adapters onto default_registry
from sidequest.agents.narrator import NarratorAgent
from sidequest.agents.orchestrator import (
    BeatSelection,
    NarrationTurnResult,
    NpcMention,
    Orchestrator,
    TurnContext,
)
from sidequest.agents.prompt_framework.core import PromptRegistry
from sidequest.agents.tool_registry import ToolContext, default_registry
from sidequest.agents.tooling_protocol import ToolResultBlock, ToolUseBlock
from sidequest.game.encounter import (
    EncounterActor,
    EncounterMetric,
    StructuredEncounter,
    is_live_wn_combat,
    wn_binding_owns_combat_resolution,
)
from sidequest.game.persistence import GameMode
from sidequest.game.repository import SaveRepository
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.models.pack import GenrePack
from sidequest.genre.models.rules import (
    BeatDef,
    ConfrontationDef,
    MetricDef,
    RulesConfig,
    WwnConfig,
)
from sidequest.protocol.dice import RollOutcome
from sidequest.server import narration_apply
from sidequest.server.narration_apply import _apply_narration_result_to_snapshot
from sidequest.server.session_room import SessionRoom
from tests.agents.fakes.fake_anthropic_sdk_client import (
    FakeAnthropicSdkClient,
    ScriptedResponse,
)

# The five combat-RESOLUTION tools withheld from the narrator on a live WN combat.
_COMBAT_RESOLUTION_TOOLS = frozenset(
    {"roll_dice", "apply_damage", "advance_encounter_beat", "advance_confrontation", "apply_status"}
)


def _wn_combat_encounter(*, win_condition: str = "hp_depletion", resolved: bool = False):
    return StructuredEncounter(
        encounter_type="combat",
        category="combat",
        win_condition=win_condition,  # type: ignore[arg-type]
        resolved=resolved,
        player_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=10),
        opponent_metric=EncounterMetric(name="hp", current=0, starting=0, threshold=10),
        actors=[
            EncounterActor(name="Groucho", role="combatant", side="player"),
            EncounterActor(name="Pale Thing", role="combatant", side="opponent"),
        ],
    )


def _combat_cdef() -> ConfrontationDef:
    # A native-shaped combat def (default win_condition); the de-nativization
    # branch is gated on the suppress_native_combat PARAM, not on cdef fields, so
    # any valid cdef exercises it. ``strike`` is the stray beat A3 drops.
    return ConfrontationDef(
        type="combat",
        label="Dungeon Combat",
        category="combat",
        player_metric=MetricDef(name="momentum", threshold=10),
        opponent_metric=MetricDef(name="momentum", threshold=10),
        beats=[
            BeatDef(id="strike", label="Strike", kind="strike", base=2, stat_check="STR"),
            BeatDef(id="brace", label="Brace", kind="brace", base=1, stat_check="CON"),
        ],
    )


# ---------------------------------------------------------------------------
# Predicate — is_live_wn_combat (the single source of truth for all 3 gates)
# ---------------------------------------------------------------------------


def test_predicate_true_for_live_wn_hp_depletion_combat() -> None:
    assert is_live_wn_combat(_wn_combat_encounter(), "wwn") is True
    # Whole WN family qualifies.
    for slug in ("swn", "wwn", "cwn", "awn"):
        assert is_live_wn_combat(_wn_combat_encounter(), slug) is True


def test_predicate_false_when_resolved_absent_or_not_wn_or_not_hp() -> None:
    # Resolved combat is over — narrate the resolution, don't suppress tools.
    assert is_live_wn_combat(_wn_combat_encounter(resolved=True), "wwn") is False
    # No encounter at all.
    assert is_live_wn_combat(None, "wwn") is False
    # Non-WN binding (native dial / Fate) keeps its own path (ADR-143 cuts both ways).
    assert is_live_wn_combat(_wn_combat_encounter(), "dial") is False
    assert is_live_wn_combat(_wn_combat_encounter(), "fate") is False
    assert is_live_wn_combat(_wn_combat_encounter(), None) is False
    # A WN encounter that is NOT hp_depletion combat (e.g. a dial chase) is untouched.
    assert is_live_wn_combat(_wn_combat_encounter(win_condition="dial_threshold"), "wwn") is False


# ---------------------------------------------------------------------------
# A1 — registry withholds combat-resolution tools, keeps read tools
# ---------------------------------------------------------------------------


def test_exclude_combat_resolution_withholds_exactly_the_resolution_tools() -> None:
    full = {t.name for t in default_registry.tool_definitions("wwn")}
    filtered = {
        t.name for t in default_registry.tool_definitions("wwn", exclude_combat_resolution=True)
    }
    assert full - filtered == set(_COMBAT_RESOLUTION_TOOLS), (
        "exactly the combat-resolution tools must be withheld on a live WN combat"
    )
    # Read/lookup tools the narrator still needs to NARRATE must survive.
    assert {"query_encounter", "lookup_monster"} <= filtered


def test_exclude_combat_resolution_defaults_off_unchanged() -> None:
    # Default False = no behavior change for every existing call site / native pack.
    assert {t.name for t in default_registry.tool_definitions("wwn")} == {
        t.name for t in default_registry.tool_definitions("wwn", exclude_combat_resolution=False)
    }


# ---------------------------------------------------------------------------
# A2 — narrator prompt de-nativizes WN combat (no native beat menu)
# ---------------------------------------------------------------------------


def test_build_encounter_context_suppresses_native_wn_combat_beat_menu() -> None:
    narrator = NarratorAgent()
    reg = PromptRegistry()
    narrator.build_encounter_context(
        reg,
        encounter=_wn_combat_encounter(),
        cdef=_combat_cdef(),
        encounter_summary="The Pale Thing rises from the black water.",
        suppress_native_combat=True,
    )
    composed = reg.compose(narrator.name())
    # Resolved-exchange context + participants still reach the narrator.
    assert "The Pale Thing rises from the black water." in composed
    assert "Groucho" in composed and "Pale Thing" in composed
    # De-nativized: resolution is by the player's throw; no beat-driving.
    assert "Do NOT emit beat_selections" in composed
    assert "player's die throw" in composed
    # The native beat menu MUST NOT render (the instruction that drove the loop).
    assert "beat_selections.beat_id MUST be one of" not in composed


def test_build_encounter_context_keeps_native_beat_menu_when_not_suppressed() -> None:
    """Native-pack regression guard: with suppress_native_combat False (a dial
    pack, or any non-WN combat), the native beat menu still renders — the legacy
    beat-driven path is untouched (ADR-143 leaves native engines alone)."""
    narrator = NarratorAgent()
    reg = PromptRegistry()
    narrator.build_encounter_context(
        reg,
        encounter=_wn_combat_encounter(),
        cdef=_combat_cdef(),
        encounter_summary="stub",
        suppress_native_combat=False,
    )
    composed = reg.compose(narrator.name())
    assert "beat_selections.beat_id MUST be one of" in composed
    assert "strike" in composed and "brace" in composed


# ---------------------------------------------------------------------------
# A3 — narration-apply drops a stray narrator beat before the legacy dial arm
# ---------------------------------------------------------------------------


_WN_ATTRIBUTE_MAP = {
    a: a for a in ("STRENGTH", "DEXTERITY", "CONSTITUTION", "INTELLIGENCE", "WISDOM", "CHARISMA")
}


def _wn_pack() -> GenrePack:
    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(
        ruleset="wwn",
        ability_score_names=list(_WN_ATTRIBUTE_MAP.values()),
        wwn=WwnConfig(attribute_map=_WN_ATTRIBUTE_MAP),
        confrontations=[_combat_cdef()],
    )
    pack.effective_cultures.return_value = ([], "genre")
    pack.source_dir = None
    pack.worlds = {}
    return pack


def _make_room() -> tuple[SessionRoom, GameSnapshot]:
    room = SessionRoom(slug="beneath_sunden", mode=GameMode.SOLO)
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        turn_manager=TurnManager(interaction=1),
    )
    room.bind_world(snapshot=snap, store=MagicMock(spec=SaveRepository))
    return room, snap


def _opponent_strike_result() -> NarrationTurnResult:
    # A stray OPPONENT beat (bypasses the PC-consent gate so from_explicit_action
    # stays at its production default of False) the narrator could hallucinate.
    return NarrationTurnResult(
        narration="The Pale Thing lunges at Groucho.",
        beat_selections=[
            BeatSelection(actor="Pale Thing", beat_id="strike", outcome=RollOutcome.Success)
        ],
        npcs_present=[NpcMention(name="Pale Thing", side="opponent", role="hostile")],
    )


def _capture_watcher(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    events: list[dict] = []

    def _spy(event_type, fields, *, component=None, severity="info"):
        events.append({"event_type": event_type, "fields": fields, "component": component})

    monkeypatch.setattr(narration_apply, "_watcher_publish", _spy)
    return events


def test_live_wn_combat_drops_stray_beat_and_does_not_touch_the_dial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live WN ``hp_depletion`` combat drops a stray narrator beat BEFORE the
    legacy dial arm — the opponent dial stays 0, no ``beat_applied`` fires, and a
    ``wn_combat_beat_dropped_engine_owns_round`` event surfaces on the GM panel.
    Resolution belongs to the player's DICE_THROW (run_wn_round), not the narrator."""
    events = _capture_watcher(monkeypatch)
    room, snap = _make_room()
    snap.encounter = _wn_combat_encounter()

    _apply_narration_result_to_snapshot(
        snap, _opponent_strike_result(), "Groucho", room=room, pack=_wn_pack()
    )

    assert snap.encounter is not None
    assert snap.encounter.opponent_metric.current == 0, (
        "a live WN combat resolves via run_wn_round on the player's throw, NOT the "
        f"native dial — a stray beat advanced the dial to {snap.encounter.opponent_metric.current}"
    )
    assert not [e for e in events if e["fields"].get("op") == "beat_applied"], (
        "no beat_applied may fire — the stray beat was dropped, not applied"
    )
    dropped = [
        e for e in events if e["fields"].get("op") == "wn_combat_beat_dropped_engine_owns_round"
    ]
    assert len(dropped) == 1, (
        "the narration pipeline must DROP the stray beat for a live WN combat and "
        f"surface it on the GM panel; got ops={[e['fields'].get('op') for e in events]}"
    )
    assert dropped[0]["fields"].get("beat_id") == "strike"


# ---------------------------------------------------------------------------
# A1 EXTENDED (sq-playtest 2026-06-24 criticals) — the tool filter keys on the
# WN BINDING, not a live encounter. On a fresh descent the attack can fail to
# seat (stale-zone creature / literary-verb routing), so NO encounter is live —
# yet the narrator must STILL be denied the combat-resolution toolset, or it
# grinds roll/apply/advance past max_turns and the websocket turn crashes.
# ---------------------------------------------------------------------------


def test_wn_binding_owns_combat_resolution_true_for_whole_family_no_encounter_needed() -> None:
    # The tool-gate predicate is BINDING-level: it ignores the encounter entirely
    # (that is the whole point — it must fire when nothing has seated).
    for slug in ("swn", "wwn", "cwn", "awn"):
        assert wn_binding_owns_combat_resolution(slug) is True


def test_wn_binding_owns_combat_resolution_false_for_native_fate_and_unbound() -> None:
    # Native dial + Fate keep their own toolset (ADR-143 cuts both ways); an
    # unbound/pack-less path is unchanged.
    assert wn_binding_owns_combat_resolution("dial") is False
    assert wn_binding_owns_combat_resolution("fate") is False
    assert wn_binding_owns_combat_resolution(None) is False


def _dial_pack() -> GenrePack:
    """A native (non-WN) pack — the regression guard: it must KEEP combat tools."""
    pack = MagicMock(spec=GenrePack)
    pack.rules = RulesConfig(ruleset="dial", confrontations=[_combat_cdef()])
    pack.effective_cultures.return_value = ([], "genre")
    pack.source_dir = None
    pack.worlds = {}
    return pack


class _StubReg:
    """Minimal PromptRegistry stand-in for the SDK drive (mirrors the hybrid-split
    fake): the tool gate reads ``context.pack``, never the registry, so a stub
    that satisfies the compose API is enough to reach ``complete_with_tools``."""

    def compose_split(self, agent_name: str) -> tuple[str, str]:
        return ("system text", "user text")

    def compose_split_by_zone(self, agent_name: str):
        from sidequest.agents.prompt_framework.types import AttentionZone

        return ({AttentionZone.Primacy: "system text"}, "user text")

    def registry(self, agent_name: str) -> list:
        return []


async def _advertised_tools_on_sdk_turn(
    monkeypatch: pytest.MonkeyPatch, *, pack: GenrePack, encounter: object | None
) -> set[str]:
    """Drive the SDK narration path for ``pack`` (with ``encounter`` live or None)
    and return the set of tool NAMES actually advertised to the model — the real
    A1 wiring assertion (``FakeAnthropicSdkClient`` records the ``tools=`` array)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = FakeAnthropicSdkClient(
        responses=[
            ScriptedResponse(
                text="The black water is still.",
                stop_reason="end_turn",
                input_tokens=200,
                output_tokens=24,
                cached_input_read_tokens=0,
                cached_input_write_tokens=0,
                model="claude-sonnet-4-6",
            )
        ]
    )
    orch = Orchestrator(client=client)

    async def _spy_dispatch(block: ToolUseBlock, ctx: ToolContext) -> ToolResultBlock:
        return ToolResultBlock(tool_use_id=block.id, content="ok", is_error=False)

    monkeypatch.setattr(default_registry, "dispatch", _spy_dispatch)

    async def _fake_build_prompt(self: Orchestrator, action: str, context: TurnContext):
        return ("prompt-text", _StubReg())

    monkeypatch.setattr(Orchestrator, "build_narrator_prompt", _fake_build_prompt)

    ctx = TurnContext(
        character_name="Groucho",
        genre="caverns_and_claudes",
        turn_number=2,
        pack=pack,
        encounter=encounter,
    )
    await orch.run_narration_turn("I attack the pale thing with my short sword.", ctx)
    assert client.recorded_requests, "SDK path never reached complete_with_tools"
    return {t.name for t in client.recorded_requests[0].tools}


@pytest.mark.asyncio
async def test_wn_pack_withholds_combat_tools_with_no_live_encounter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression the 2026-06-24 criticals hit: a WN pack with NO live
    encounter (the unseated attack) must still withhold every combat-resolution
    tool, so the narrator cannot grind them past max_turns."""
    advertised = await _advertised_tools_on_sdk_turn(monkeypatch, pack=_wn_pack(), encounter=None)
    leaked = advertised & set(_COMBAT_RESOLUTION_TOOLS)
    assert not leaked, (
        "on a WN pack the narrator must hold NO combat-resolution tool even with "
        f"no live encounter (the unseated-attack starve); leaked={sorted(leaked)}"
    )
    # The read/lookup tools it needs to NARRATE the scene must still be advertised.
    assert {"query_encounter", "lookup_monster"} <= advertised


@pytest.mark.asyncio
async def test_native_dial_pack_keeps_combat_tools_with_no_encounter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: a native (non-WN) pack is untouched — it keeps the full
    combat toolset whether or not an encounter is live (ADR-143 leaves native
    engines alone)."""
    advertised = await _advertised_tools_on_sdk_turn(monkeypatch, pack=_dial_pack(), encounter=None)
    assert set(_COMBAT_RESOLUTION_TOOLS) <= advertised, (
        "a native dial pack must keep its combat-resolution tools; "
        f"missing={sorted(set(_COMBAT_RESOLUTION_TOOLS) - advertised)}"
    )


# ---------------------------------------------------------------------------
# A2 COLD-SEAT (sq-playtest 2026-06-27 — the PRIMARY lie-detector crash) — the
# dispatch bank seats the WN combat encounter on the snapshot AFTER
# ``_build_turn_context`` already computed the encounter projection
# (in_combat / encounter / confrontation_def / encounter_summary) as
# False/None. ``refresh_turn_context_post_dispatch`` refreshed npcs + region but
# NOT the encounter fields, so on the COLD-SEAT turn ``context.in_combat`` stayed
# False — the A2 de-nativized prompt zone (orchestrator.py:2225 gate) never
# fired AND the "AVAILABLE ENCOUNTER TYPES" start-a-confrontation menu DID. The
# narrator, denied the resolution tools (A1, binding-keyed) but told to START a
# fresh confrontation rather than narrate the seated one, ground its SDK tool
# loop past max_turns=8 and crashed the websocket. The fix: the post-dispatch
# refresh recomputes the encounter projection from the just-seated snapshot.
# ---------------------------------------------------------------------------


class _FakeRefreshSessionData:
    """Duck-typed ``_SessionData`` for ``refresh_turn_context_post_dispatch``: it
    reads ``snapshot.npcs`` (npc refresh), ``_project_current_region`` (genre /
    world / pack — returns None on a non-procedural-dungeon world), and the
    encounter-field refresh reads ``genre_pack.rules.confrontations``."""

    def __init__(self, snapshot: GameSnapshot, pack: GenrePack) -> None:
        self.snapshot = snapshot
        self.genre_pack = pack
        # A non-beneath_sunden world so _project_current_region returns None
        # cleanly (the encounter refresh is what these tests pin).
        self.genre_slug = "caverns_and_claudes"
        self.world_slug = "test_nondungeon"
        self.player_id = "p1"
        self.dungeon_repository = None


def _nondungeon_snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="test_nondungeon",
        turn_manager=TurnManager(interaction=3),
    )


def test_refresh_post_dispatch_reflects_a_freshly_seated_wn_combat() -> None:
    """The cold-seat root cause: the dispatch bank seats the encounter AFTER the
    pre-dispatch context build, so ``refresh_turn_context_post_dispatch`` must
    recompute the encounter projection. Otherwise ``context.in_combat`` stays
    False, the A2 de-nativized zone never fires, and the narrator grinds the SDK
    loop past max_turns on the seat turn."""
    from sidequest.agents.orchestrator import TurnContext
    from sidequest.server.session_helpers import refresh_turn_context_post_dispatch

    snap = _nondungeon_snapshot()
    sd = _FakeRefreshSessionData(snap, _wn_pack())

    # Pre-dispatch context: _build_turn_context saw NO encounter (the attack has
    # not seated yet), so every encounter flag is False/None.
    ctx = TurnContext(character_name="Groucho", genre="caverns_and_claudes", turn_number=3)
    assert ctx.in_combat is False and ctx.encounter is None

    # The dispatch bank seats the WN combat on the snapshot mid-turn.
    snap.encounter = _wn_combat_encounter()

    refresh_turn_context_post_dispatch(ctx, sd=sd, snapshot=snap)

    assert ctx.in_combat is True, (
        "post-dispatch refresh did not pick up the freshly-seated WN combat — "
        "context.in_combat stayed False, so the A2 de-nativized prompt zone "
        "(orchestrator.py:2225 gate) never fires and the narrator grinds the "
        "SDK tool loop past max_turns on the cold-seat turn"
    )
    assert ctx.in_encounter is True
    assert ctx.encounter is snap.encounter
    assert (
        ctx.confrontation_def is not None and ctx.confrontation_def.confrontation_type == "combat"
    )
    assert ctx.encounter_summary is not None
    # The refreshed context satisfies the EXACT predicate that gates all three
    # de-nativization arms (orchestrator.py:2251 suppress_native_combat) — the
    # wiring assertion that the refresh actually un-sticks the A2 zone.
    assert is_live_wn_combat(ctx.encounter, "wwn") is True


def test_refresh_post_dispatch_no_encounter_leaves_combat_flags_false() -> None:
    """A turn whose dispatch seats NO confrontation (movement, look, talk) must
    leave the encounter flags False — the refresh must never invent combat."""
    from sidequest.agents.orchestrator import TurnContext
    from sidequest.server.session_helpers import refresh_turn_context_post_dispatch

    snap = _nondungeon_snapshot()  # snapshot.encounter is None
    sd = _FakeRefreshSessionData(snap, _wn_pack())
    ctx = TurnContext(character_name="Groucho", genre="caverns_and_claudes", turn_number=3)

    refresh_turn_context_post_dispatch(ctx, sd=sd, snapshot=snap)

    assert ctx.in_combat is False
    assert ctx.in_encounter is False
    assert ctx.encounter is None
    assert ctx.confrontation_def is None
    assert ctx.encounter_summary is None


def test_refresh_post_dispatch_resolved_encounter_does_not_reseat_combat() -> None:
    """A RESOLVED encounter (combat just ended this turn) must NOT flip in_combat
    back on — mirrors ``_build_turn_context``'s ``not encounter.resolved`` guard
    so a closed combat doesn't keep re-seating the live zone."""
    from sidequest.agents.orchestrator import TurnContext
    from sidequest.server.session_helpers import refresh_turn_context_post_dispatch

    snap = _nondungeon_snapshot()
    sd = _FakeRefreshSessionData(snap, _wn_pack())
    ctx = TurnContext(character_name="Groucho", genre="caverns_and_claudes", turn_number=3)

    snap.encounter = _wn_combat_encounter(resolved=True)
    refresh_turn_context_post_dispatch(ctx, sd=sd, snapshot=snap)

    assert ctx.in_combat is False
    assert ctx.in_encounter is False
    assert ctx.encounter is None


def test_refresh_cold_seat_emits_gm_panel_watcher_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """OTEL lie-detector (CLAUDE.md): the cold-seat refresh emits a
    ``state_transition`` op=cold_seat_context_refreshed watcher event so the GM
    panel can verify the seat-turn narration was handed the live-encounter zone.
    A turn that seats NO encounter emits nothing (no false positive)."""
    from sidequest.agents.orchestrator import TurnContext
    from sidequest.server.session_helpers import refresh_turn_context_post_dispatch

    events: list[dict] = []

    def _spy(event_type, fields, *, component=None, severity="info"):
        events.append({"event_type": event_type, "fields": fields, "component": component})

    monkeypatch.setattr("sidequest.telemetry.watcher_hub.publish_event", _spy)

    # A turn whose dispatch seats nothing: no cold-seat event.
    snap = _nondungeon_snapshot()
    sd = _FakeRefreshSessionData(snap, _wn_pack())
    ctx = TurnContext(character_name="Groucho", genre="caverns_and_claudes", turn_number=3)
    refresh_turn_context_post_dispatch(ctx, sd=sd, snapshot=snap)
    assert not [e for e in events if e["fields"].get("op") == "cold_seat_context_refreshed"]

    # The cold-seat: a confrontation dispatch seated combat this turn.
    snap.encounter = _wn_combat_encounter()
    refresh_turn_context_post_dispatch(ctx, sd=sd, snapshot=snap)
    cold = [e for e in events if e["fields"].get("op") == "cold_seat_context_refreshed"]
    assert len(cold) == 1, "the cold-seat refresh must surface exactly one GM-panel event"
    assert cold[0]["fields"]["encounter_type"] == "combat"
    assert cold[0]["fields"]["in_combat"] is True
    assert cold[0]["component"] == "encounter"


@pytest.mark.asyncio
async def test_refreshed_cold_seat_context_fires_denativized_zone_not_start_menu() -> None:
    """Wiring (CLAUDE.md "Every Test Suite Needs a Wiring Test") — the cold-seat
    chain end to end. A pre-dispatch context (in_combat False) plus a snapshot the
    dispatch bank just seated a WN combat on, AFTER the refresh, builds a narrator
    prompt that (a) carries the A2 de-nativized "player throws to resolve, do NOT
    call dice/beat tools" directive and (b) does NOT carry the "AVAILABLE
    ENCOUNTER TYPES" start-a-confrontation menu. Pre-fix the cold-seat got the
    exact opposite of both — the menu told it to START a fight while the
    resolution tools were withheld — and it ground the loop to a crash."""
    from sidequest.agents.orchestrator import Orchestrator, TurnContext
    from sidequest.server.session_helpers import refresh_turn_context_post_dispatch

    snap = _nondungeon_snapshot()
    pack = _wn_pack()
    sd = _FakeRefreshSessionData(snap, pack)

    ctx = TurnContext(
        character_name="Groucho",
        genre="caverns_and_claudes",
        turn_number=3,
        pack=pack,
        available_confrontations=[("combat", "Dungeon Combat", "combat")],
    )
    assert ctx.in_combat is False  # pre-dispatch

    snap.encounter = _wn_combat_encounter()
    refresh_turn_context_post_dispatch(ctx, sd=sd, snapshot=snap)
    assert ctx.in_combat is True  # post-refresh

    orch = Orchestrator(client=FakeAnthropicSdkClient(responses=[]))
    prompt_text, _registry = await orch.build_narrator_prompt(
        "I level my spear and charge the nearest tender.", ctx
    )

    # A2 de-nativized directive present — narrate the seat, the player throws to
    # resolve (the instruction that lets the seat turn converge).
    assert "Do NOT emit beat_selections" in prompt_text
    assert "player's die throw" in prompt_text
    # The start-a-confrontation menu is suppressed now combat is live.
    assert "AVAILABLE ENCOUNTER TYPES" not in prompt_text


# ---------------------------------------------------------------------------
# A2 RESOLUTION-TURN (sq-playtest 2026-06-27 — the PRIMARY lie-detector crash,
# session 16570; the resolution-turn TWIN of the cold-seat above and of #1086) —
# on the WWN combat KILL turn the player's DICE_THROW resolves the encounter, so
# ``in_combat`` legitimately flips False the instant it resolves AND the encounter
# is reaped. The resolution close stamps ``snapshot.pending_resolution_signal``
# (dispatch/dice.py ``_emit_player_beat_resolution_close`` for player_victory),
# but ``refresh_turn_context_post_dispatch`` (and ``_build_turn_context``) never
# copy it into ``TurnContext.pending_resolution_signal`` — the [ENCOUNTER
# RESOLVED] zone has been DORMANT since the story-49-5 wrapper deletion (see the
# TurnContext field docstring in orchestrator.py). So on the victory turn the
# narrator gets NEITHER the de-nativized "throw already resolved; just narrate the
# outcome; do NOT emit beat_selections" directive NOR a suppressed
# start-a-confrontation menu (the menu is gated on ``pending_resolution_signal is
# None``, always true today) → it flails its SDK tool loop past max_turns=8
# (AnthropicSdkLoopExceeded) → the victory NARRATION is never persisted and the
# client falls back to the opening card (ADR-133). The fix mirrors #1086: the
# post-dispatch refresh threads the stamped signal + surfaces a GM-panel watcher
# event (twin of cold_seat_context_refreshed). The DICE-path ``_build_turn_context``
# seam is pinned in tests/server/test_resolution_signal_turn_context_wiring.py.
# ---------------------------------------------------------------------------


def _victory_resolution_signal():
    from sidequest.game.resolution_signal import ResolutionSignal

    return ResolutionSignal(
        encounter_type="combat",
        outcome="player_victory",
        final_player_metric=0,
        final_opponent_metric=0,
    )


# The GM-panel watcher op the resolution-turn bridge surfaces (twin of
# cold_seat_context_refreshed). Shared contract with the _build_turn_context seam.
_RESOLUTION_OP = "resolution_context_refreshed"


def test_refresh_post_dispatch_threads_resolution_signal_to_context() -> None:
    """The resolution-turn root cause: the kill resolves and stamps
    ``snapshot.pending_resolution_signal``, so the post-dispatch refresh must copy
    it into ``ctx.pending_resolution_signal``. Otherwise the field stays None, the
    [ENCOUNTER RESOLVED] zone never fires, and the narrator grinds the SDK loop
    past max_turns on the victory turn (the resolution-turn twin of the cold-seat
    crash above)."""
    from sidequest.agents.orchestrator import TurnContext
    from sidequest.server.session_helpers import refresh_turn_context_post_dispatch

    # Resolution turn: the encounter has resolved + been reaped (no live
    # encounter), only the one-shot signal carries the close payload.
    snap = _nondungeon_snapshot()
    snap.pending_resolution_signal = _victory_resolution_signal()
    sd = _FakeRefreshSessionData(snap, _wn_pack())
    ctx = TurnContext(character_name="Groucho", genre="caverns_and_claudes", turn_number=4)
    assert ctx.pending_resolution_signal is None  # pre-dispatch

    refresh_turn_context_post_dispatch(ctx, sd=sd, snapshot=snap)

    assert ctx.pending_resolution_signal is not None, (
        "post-dispatch refresh did not thread snapshot.pending_resolution_signal — "
        "ctx.pending_resolution_signal stayed None, so the [ENCOUNTER RESOLVED] "
        "zone never fires and the narrator grinds the SDK loop past max_turns on "
        "the victory turn (sq-playtest 2026-06-27, session 16570)"
    )
    assert ctx.pending_resolution_signal.outcome == "player_victory"


def test_refresh_post_dispatch_no_resolution_signal_leaves_context_none() -> None:
    """A turn that resolves nothing leaves ``ctx.pending_resolution_signal`` None —
    the refresh must never invent a resolution (which would re-fire the close
    every turn)."""
    from sidequest.agents.orchestrator import TurnContext
    from sidequest.server.session_helpers import refresh_turn_context_post_dispatch

    snap = _nondungeon_snapshot()  # snapshot.pending_resolution_signal is None
    sd = _FakeRefreshSessionData(snap, _wn_pack())
    ctx = TurnContext(character_name="Groucho", genre="caverns_and_claudes", turn_number=4)

    refresh_turn_context_post_dispatch(ctx, sd=sd, snapshot=snap)

    assert ctx.pending_resolution_signal is None


def test_refresh_resolution_turn_emits_gm_panel_watcher_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OTEL lie-detector (CLAUDE.md), twin of #1086's cold_seat_context_refreshed:
    threading the signal on the resolution turn emits a ``state_transition``
    op=resolution_context_refreshed watcher event (component="encounter") so the
    GM panel can verify the victory turn was handed the resolution zone. A turn
    that resolves nothing emits nothing (no false positive)."""
    from sidequest.agents.orchestrator import TurnContext
    from sidequest.server.session_helpers import refresh_turn_context_post_dispatch

    events: list[dict] = []

    def _spy(event_type, fields, *, component=None, severity="info"):
        events.append({"event_type": event_type, "fields": fields, "component": component})

    monkeypatch.setattr("sidequest.telemetry.watcher_hub.publish_event", _spy)

    # No resolution: no event.
    snap = _nondungeon_snapshot()
    sd = _FakeRefreshSessionData(snap, _wn_pack())
    ctx = TurnContext(character_name="Groucho", genre="caverns_and_claudes", turn_number=4)
    refresh_turn_context_post_dispatch(ctx, sd=sd, snapshot=snap)
    assert not [e for e in events if e["fields"].get("op") == _RESOLUTION_OP]

    # The resolution turn: a stamped signal surfaces exactly one GM-panel event.
    snap.pending_resolution_signal = _victory_resolution_signal()
    refresh_turn_context_post_dispatch(ctx, sd=sd, snapshot=snap)
    resolved = [e for e in events if e["fields"].get("op") == _RESOLUTION_OP]
    assert len(resolved) == 1, (
        "the resolution refresh must surface exactly one GM-panel event "
        f"(twin of cold_seat_context_refreshed); got ops="
        f"{[e['fields'].get('op') for e in events]}"
    )
    assert resolved[0]["fields"]["outcome"] == "player_victory"
    assert resolved[0]["component"] == "encounter"


@pytest.mark.asyncio
async def test_refreshed_resolution_context_fires_denativized_zone_not_start_menu() -> None:
    """Wiring (CLAUDE.md "Every Test Suite Needs a Wiring Test"), the AC3
    resolution-turn analogue of the cold-seat wiring test above: a pre-dispatch
    context (signal None) plus a snapshot the kill just stamped a resolution on,
    AFTER the refresh, builds a narrator prompt that (a) carries the de-nativized
    [ENCOUNTER RESOLVED] "do NOT emit beat_selections" directive and (b) does NOT
    carry the "AVAILABLE ENCOUNTER TYPES" start-a-confrontation menu. Pre-fix the
    victory turn got the exact opposite of both — the menu told it to START a
    fresh fight — and it ground the loop to a crash."""
    from sidequest.agents.orchestrator import Orchestrator, TurnContext
    from sidequest.server.session_helpers import refresh_turn_context_post_dispatch

    snap = _nondungeon_snapshot()
    pack = _wn_pack()
    sd = _FakeRefreshSessionData(snap, pack)

    ctx = TurnContext(
        character_name="Groucho",
        genre="caverns_and_claudes",
        turn_number=4,
        pack=pack,
        available_confrontations=[("combat", "Dungeon Combat", "combat")],
    )
    assert ctx.pending_resolution_signal is None  # pre-dispatch

    snap.pending_resolution_signal = _victory_resolution_signal()
    refresh_turn_context_post_dispatch(ctx, sd=sd, snapshot=snap)
    assert ctx.pending_resolution_signal is not None  # post-refresh

    orch = Orchestrator(client=FakeAnthropicSdkClient(responses=[]))
    prompt_text, _registry = await orch.build_narrator_prompt(
        "I wrench my axe free and let the Pale Thing fall.", ctx
    )

    # The de-nativized resolution directive present — narrate the close, do not
    # drive beats (the instruction that lets the victory turn converge).
    assert "[ENCOUNTER RESOLVED]" in prompt_text
    assert "Do NOT emit beat_selections" in prompt_text
    # The start-a-confrontation menu is suppressed now a resolution is pending.
    assert "AVAILABLE ENCOUNTER TYPES" not in prompt_text
