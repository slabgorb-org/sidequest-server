"""Story 57-5 RED — game_state snapshot slimming (ADR-110 Phase A + Phase B).

The per-turn ``<game_state>`` block is the single largest uncached blob in
the narrator prompt: 5-10 KB/turn, ~1-2k tokens. It is constructed at
``session_helpers.py:485`` via ``json.loads(snapshot.model_dump_json())``
and serialized at ``session_helpers.py:558`` with ``json.dumps(payload, indent=2)``.

ADR-110 commits to a two-phase >=50% byte reduction with zero
narrator-quality regression:

  * Phase A (zero-risk): switch encoding to
    ``snapshot.model_dump(mode="json", exclude_defaults=True, exclude_none=True)``
    plus ``json.dumps(..., separators=(",", ":"))``. Drops pretty-print
    whitespace and pydantic default fields.

  * Phase B (audit-driven): drop fields not consumed by the narrator
    prompt assembly. Confirmed drop candidates:
      - ``active_tropes`` — re-rendered in Recency-zone
        ``pending_trope_context`` / Valley-zone ``active_trope_summary``
        (see ``orchestrator.py:1721``); the raw list is internal-only.
      - ``axis_values`` — re-rendered in narrative axis sections.
      - ``genie_wishes`` — deferred subsystem (P5).
      - ``achievement_tracker`` — deferred subsystem (P6).
    Anti-confabulation anchors that MUST survive (gaslighting doctrine,
    project_narrator_gaslighting_doctrine.md):
      - ``characters``, ``npcs``, ``character_locations``,
        ``room_states``, ``quest_log``.

  * Observability: OTEL span ``prompt.game_state.bytes`` fires per turn
    with ``phase_a_applied``, ``phase_b_applied``, ``bytes_before``,
    ``bytes_after``. The GM panel must be able to verify the cut.

  * Acceptance gate: ``bytes_after / bytes_before <= 0.5`` on a fixed
    snapshot fixture. Round-trip equivalence holds (defaults reconstruct
    on parse).

These tests are RED until Dev applies Phase A + Phase B at
``session_helpers.py:485+558`` and adds the OTEL span emitter.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.server.session_handler import _build_turn_context, _SessionData
from tests._helpers.session_room import room_for

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


# ---------------------------------------------------------------------------
# Fixture helpers — mirror tests/server/test_session_helpers_narrative_strip.py
# ---------------------------------------------------------------------------


def _character(name: str) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            inventory=Inventory(),
            hp=HpPool(current=8, max=10, base_max=10),
        ),
        backstory="hero",
        char_class="Delver",
        race="Human",
    )


def _npc(name: str, *, last_seen_location: str | None = "Main Hall") -> Npc:
    """Build an Npc seated in-scene with the acting PC by default.

    Story 61-2 / ADR-110 Phase C added an in-scene filter to
    ``state_summary["npcs"]``: NPCs whose ``last_seen_location`` doesn't
    match the acting PC's current room (or who aren't named in an
    unresolved encounter's actor list) are dropped from the dump and
    addressable only via ``npc_pool``. Seat the 57-5 fixture NPCs at
    the acting PC's location ("Main Hall") so the
    ``test_anchor_preserved_npcs_with_content`` contract — "NPCs the
    fixture sets DO survive the per-turn projection" — still holds
    under the new contract.
    """
    return Npc(
        core=CreatureCore(
            name=name,
            description="grizzled veteran",
            personality="dour",
            inventory=Inventory(),
            hp=HpPool(current=5, max=5, base_max=5),
        ),
        last_seen_location=last_seen_location,
    )


def _make_snapshot() -> GameSnapshot:
    """Build a slim but representative GameSnapshot for the mawdeep world.

    Populates anti-confabulation anchors (npcs, characters, quest_log,
    character_locations) so we can verify Phase B doesn't strip them.
    """
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=6),
        characters=[_character("Alice")],
        npcs=[_npc("Brokar the Loremaster"), _npc("Tessa the Sharpshooter")],
        quest_log={"main": "Find the lost vault.", "side": "Recover Brokar's medallion."},
        notes=["Brokar trusts Alice.", "Vault sealed by ancient sigil."],
        atmosphere="Damp stone, low torchlight.",
        current_region="upper_caverns",
    )
    snap.character_locations["Alice"] = "Main Hall"
    snap.player_seats["player:alice"] = "Alice"
    return snap


def _build_sd(snapshot: GameSnapshot, *, player_name: str = "Alice") -> _SessionData:
    pack = load_genre_pack(CONTENT_GENRE_PACKS / snapshot.genre_slug)
    return _SessionData(
        genre_slug=snapshot.genre_slug,
        world_slug=snapshot.world_slug,
        player_name=player_name,
        player_id=f"player:{player_name.lower()}",
        snapshot=snapshot,
        repository=MagicMock(),
        dungeon_repository=MagicMock(),
        telemetry_sink=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )


def _state_summary(snap: GameSnapshot) -> str:
    """Drive the production code path and return the state_summary text."""
    sd = _build_sd(snap)
    sd._room = room_for(snap, slug=snap.world_slug)
    ctx = _build_turn_context(sd, room=sd._room)
    assert ctx.state_summary is not None, (
        "state_summary missing from TurnContext — fixture set-up broke "
        "before the snapshot-slimming change could be exercised."
    )
    return ctx.state_summary


# ---------------------------------------------------------------------------
# AC #1 — Phase A: compact JSON encoding
# ---------------------------------------------------------------------------


def test_phase_a_state_summary_has_no_pretty_indent() -> None:
    """Phase A drops ``indent=2`` in favour of ``separators=(",", ":")``.

    With ``indent=2``, every nested object/array introduces ``\\n  ...``
    (newline + 2N spaces). The compact form emits a single line. We
    assert that the state_summary contains NO ``"\\n  "`` substring —
    that pattern is the unique fingerprint of ``indent=2`` and would be
    impossible under any reasonable compact encoding.
    """
    snap = _make_snapshot()
    state_summary = _state_summary(snap)

    assert "\n  " not in state_summary, (
        "state_summary still uses indent=2 pretty-printing (newline + "
        "2-space indent fingerprint found). Phase A's "
        '``json.dumps(payload, separators=(",", ":"))`` was not applied '
        "at session_helpers.py:558."
    )


def test_phase_a_state_summary_omits_pydantic_default_fields() -> None:
    """Phase A: ``model_dump(mode="json", exclude_defaults=True, exclude_none=True)``
    drops fields equal to their pydantic default.

    For a fresh snapshot built with default ``clock_t_hours=0.0``,
    ``last_saved_at=None``, and a default-factory empty list/dict for
    ``world_history`` / ``discovered_routes``, those keys MUST NOT
    appear in the serialized payload. Their presence is the
    fingerprint of the pre-Phase-A ``model_dump_json()`` (which
    serializes defaults).
    """
    snap = _make_snapshot()
    payload = json.loads(_state_summary(snap))

    # ``last_saved_at`` is None on a fresh snapshot — exclude_none=True
    # must remove it.
    assert "last_saved_at" not in payload, (
        "Phase A leak: ``last_saved_at`` is None on a fresh snapshot but "
        "still appears in state_summary. exclude_none=True was not "
        "applied at session_helpers.py:485."
    )
    # ``clock_t_hours`` defaults to 0.0 — exclude_defaults=True must
    # remove it on a fresh snapshot.
    assert "clock_t_hours" not in payload, (
        "Phase A leak: ``clock_t_hours`` equals its 0.0 default but still "
        "appears in state_summary. exclude_defaults=True was not applied "
        "at session_helpers.py:485."
    )
    # Empty default-factory list.
    assert "discovered_routes" not in payload, (
        "Phase A leak: ``discovered_routes`` is an empty default list "
        "but still appears in state_summary. exclude_defaults=True was "
        "not applied at session_helpers.py:485."
    )


# ---------------------------------------------------------------------------
# AC #2 — Phase B: field-pruning allowlist
# ---------------------------------------------------------------------------


def test_phase_b_drops_active_tropes_from_state_summary() -> None:
    """``active_tropes`` is re-rendered in the Recency-zone
    ``pending_trope_context`` and Valley-zone ``active_trope_summary``
    blocks (orchestrator.py:1721, 1546). The raw list is internal
    bookkeeping — its presence in the Valley-zone game_state blob is
    pure duplication. Phase B drops it."""
    snap = _make_snapshot()
    payload = json.loads(_state_summary(snap))

    assert "active_tropes" not in payload, (
        "Phase B drop missing: ``active_tropes`` still in state_summary. "
        "The raw trope list rides into the prompt twice (Valley zone "
        "game_state and Recency-zone pending_trope_context). Drop it "
        "from game_state — narrator reads it from the dedicated section."
    )


def test_phase_b_drops_axis_values_from_state_summary() -> None:
    """``axis_values`` is re-rendered in narrative-axis prompt sections.
    Drop it from the game_state blob to kill duplicate emission."""
    snap = _make_snapshot()
    payload = json.loads(_state_summary(snap))

    assert "axis_values" not in payload, (
        "Phase B drop missing: ``axis_values`` still in state_summary. "
        "Narrator reads axes from the dedicated narrative_axis section."
    )


def test_phase_b_drops_genie_wishes_from_state_summary() -> None:
    """``genie_wishes`` is a P5-deferred subsystem (consequence engine,
    F9). No prompt section consumes it. Drop it."""
    snap = _make_snapshot()
    payload = json.loads(_state_summary(snap))

    assert "genie_wishes" not in payload, (
        "Phase B drop missing: ``genie_wishes`` still in state_summary. "
        "P5-deferred subsystem with no narrator consumer — pure bloat."
    )


def test_phase_b_drops_achievement_tracker_from_state_summary() -> None:
    """``achievement_tracker`` is a P6-deferred subsystem. No prompt
    section consumes it. Drop it."""
    snap = _make_snapshot()
    payload = json.loads(_state_summary(snap))

    assert "achievement_tracker" not in payload, (
        "Phase B drop missing: ``achievement_tracker`` still in "
        "state_summary. P6-deferred subsystem with no narrator consumer."
    )


# ---------------------------------------------------------------------------
# AC #3 — byte reduction >= 50%
# ---------------------------------------------------------------------------


def test_state_summary_byte_reduction_at_least_50_percent() -> None:
    """ADR-110 acceptance gate: combined Phase A + Phase B must reduce
    the serialized ``state_summary`` by >= 50% relative to the
    pre-change encoding (``model_dump_json`` + ``indent=2``) on the
    same fixture.

    Without this, the cost-savings claim is unverifiable and the story
    does not pass.
    """
    snap = _make_snapshot()

    # Baseline = the pre-Phase-A encoding pattern as it stood at
    # session_helpers.py:485+558 before this story. This is the reference
    # the >=50% gate is measured against.
    baseline_payload = json.loads(snap.model_dump_json())
    baseline_text = json.dumps(baseline_payload, indent=2)
    bytes_before = len(baseline_text.encode("utf-8"))

    new_text = _state_summary(snap)
    bytes_after = len(new_text.encode("utf-8"))

    ratio = bytes_after / bytes_before if bytes_before else 1.0
    assert ratio <= 0.5, (
        f"Phase A + Phase B reduction below ADR-110 acceptance gate: "
        f"bytes_before={bytes_before} bytes_after={bytes_after} "
        f"ratio={ratio:.3f} (gate: <=0.5). Either Phase A wasn't "
        f"applied, Phase B's drop list is too short, or the fixture "
        f"has grown large enough to need Option C (diff-with-anchor)."
    )


# ---------------------------------------------------------------------------
# AC #4 — pydantic round-trip equivalence
# ---------------------------------------------------------------------------


def test_compact_form_round_trips_to_model_dump_equivalent() -> None:
    """A consumer reparsing the compact form back into ``GameSnapshot``
    must produce a ``model_dump()`` equal to the original's. This holds
    because pydantic reconstructs defaults on parse, so dropping
    default-equal fields at serialization is lossless.

    Tested directly on the pydantic model (independent of session_helpers'
    in-place mutations), since the round-trip property is what makes
    Phase A safe to apply anywhere the snapshot is serialized.
    """
    snap = _make_snapshot()

    compact_payload = snap.model_dump(
        mode="json",
        exclude_defaults=True,
        exclude_none=True,
    )
    compact_text = json.dumps(compact_payload, separators=(",", ":"))

    parsed = GameSnapshot.model_validate_json(compact_text)

    assert parsed.model_dump(mode="json") == snap.model_dump(mode="json"), (
        "Compact serialization is NOT round-trip equivalent: parsing the "
        "exclude_defaults+exclude_none form back into GameSnapshot did "
        "not reconstruct a model_dump-equal snapshot. Either a field "
        "has a custom serializer that defeats the default-reconstruction "
        "(log a Design Deviation per ADR-110 §Assumptions) or "
        "exclude_defaults+exclude_none drops something whose absence is "
        "not safe."
    )


# ---------------------------------------------------------------------------
# AC #5 — anti-confabulation anchors preserved (gaslighting doctrine)
# ---------------------------------------------------------------------------


def test_anchor_preserved_characters_with_content() -> None:
    """Per project_narrator_gaslighting_doctrine.md and ADR-014, the
    narrator must SEE the PC roster — otherwise it confabulates names,
    races, classes. Phase B's drop list MUST NOT include ``characters``."""
    snap = _make_snapshot()
    payload = json.loads(_state_summary(snap))

    assert "characters" in payload, (
        "Gaslighting-doctrine anchor stripped: ``characters`` is absent "
        "from state_summary. The narrator will confabulate PC identity "
        "(playtest 3 evropi regression class)."
    )
    chars = payload["characters"]
    assert isinstance(chars, list) and chars, (
        "Gaslighting-doctrine anchor empty: ``characters`` is present "
        "but empty despite the fixture seeding Alice."
    )


def test_anchor_preserved_npcs_with_content() -> None:
    """Materialized NPCs are the load-bearing anti-confabulation anchor —
    world_materialization._apply_npc() writes them specifically so the
    narrator cannot invent names. Phase B MUST NOT drop ``npcs``."""
    snap = _make_snapshot()
    payload = json.loads(_state_summary(snap))

    assert "npcs" in payload, (
        "Gaslighting-doctrine anchor stripped: ``npcs`` is absent from "
        "state_summary. Materialized NPC list is the narrator's primary "
        "anti-confabulation source — without it Brokar becomes "
        "'the loremaster' and Tessa drifts to 'the sharpshooter' with "
        "wrong details."
    )
    npcs = payload["npcs"]
    assert isinstance(npcs, list) and len(npcs) == 2, (
        f"Gaslighting-doctrine anchor truncated: ``npcs`` has "
        f"{len(npcs) if isinstance(npcs, list) else 'non-list'} entries "
        f"but the fixture seeded 2."
    )


def test_anchor_preserved_character_locations_when_populated() -> None:
    """``character_locations`` is the per-PC source of truth post-Wave-2B
    (story 45-48). Multiplayer location-header correctness depends on it.
    Phase B MUST NOT drop it."""
    snap = _make_snapshot()
    payload = json.loads(_state_summary(snap))

    assert "character_locations" in payload, (
        "Per-PC location anchor stripped: ``character_locations`` is "
        "absent from state_summary despite the fixture populating "
        "Alice -> Main Hall."
    )


def test_anchor_preserved_quest_log_when_populated() -> None:
    """``quest_log`` is the narrator's mission anchor. Phase B MUST NOT
    drop it when populated."""
    snap = _make_snapshot()
    payload = json.loads(_state_summary(snap))

    assert "quest_log" in payload, (
        "Mission anchor stripped: ``quest_log`` is absent from "
        "state_summary despite the fixture seeding two quests."
    )
    assert payload["quest_log"].get("main") == "Find the lost vault.", (
        "Mission anchor corrupted: ``quest_log['main']`` does not match "
        "the seeded value — the field is present but the content is "
        "wrong."
    )


# ---------------------------------------------------------------------------
# AC #6 — OTEL span `prompt.game_state.bytes`
# ---------------------------------------------------------------------------


def test_span_constant_prompt_game_state_bytes_is_exported() -> None:
    """ADR-110 §Observability requires ``prompt.game_state.bytes`` as the
    canonical span name. Dev wires this constant in
    ``sidequest/telemetry/spans/prompt.py`` (or equivalent) and
    re-exports through ``sidequest.telemetry.spans``."""
    from sidequest.telemetry import spans

    span_const = getattr(spans, "SPAN_PROMPT_GAME_STATE_BYTES", None)
    assert span_const == "prompt.game_state.bytes", (
        "OTEL span constant missing or wrong value: "
        f"SPAN_PROMPT_GAME_STATE_BYTES={span_const!r} (expected "
        '"prompt.game_state.bytes"). Add it to '
        "sidequest/telemetry/spans/ and re-export from spans/__init__.py."
    )


def test_span_prompt_game_state_bytes_is_flat_only() -> None:
    """Per the spans-registry contract (tests/telemetry/test_routing_completeness.py),
    every span constant must be either routed (SPAN_ROUTES) or flat-only
    (FLAT_ONLY_SPANS). The bytes span is a leaf metric — flat-only."""
    from sidequest.telemetry.spans import FLAT_ONLY_SPANS, SPAN_PROMPT_GAME_STATE_BYTES

    assert SPAN_PROMPT_GAME_STATE_BYTES in FLAT_ONLY_SPANS, (
        f"Span {SPAN_PROMPT_GAME_STATE_BYTES!r} is not registered as "
        "flat-only — tests/telemetry/test_routing_completeness.py will "
        "fail until it lands in FLAT_ONLY_SPANS (or in SPAN_ROUTES if "
        "routing is added)."
    )


def test_span_prompt_game_state_bytes_fires_with_required_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The span fires per narrator turn at the encode site, carrying the
    four attributes ADR-110 §Observability mandates: phase_a_applied,
    phase_b_applied, bytes_before, bytes_after. Without these the
    GM-panel verification of the cut is impossible (Sebastien's
    lie-detector requirement)."""
    from sidequest.telemetry import spans as _spans

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(_spans, "tracer", lambda: provider.get_tracer("test"))

    snap = _make_snapshot()
    _state_summary(snap)

    finished = exporter.get_finished_spans()
    matching = [s for s in finished if s.name == "prompt.game_state.bytes"]
    assert len(matching) == 1, (
        f"Expected exactly one prompt.game_state.bytes span per turn; "
        f"got {len(matching)}. Finished spans: "
        f"{sorted({s.name for s in finished})}"
    )
    attrs = matching[0].attributes or {}
    for required in ("phase_a_applied", "phase_b_applied", "bytes_before", "bytes_after"):
        assert required in attrs, (
            f"prompt.game_state.bytes span missing required attribute "
            f"{required!r}. ADR-110 §Observability requires all four "
            f"(phase_a_applied, phase_b_applied, bytes_before, "
            f"bytes_after) so the GM panel can verify the cut."
        )
    bytes_before = attrs["bytes_before"]
    bytes_after = attrs["bytes_after"]
    assert isinstance(bytes_before, int) and bytes_before > 0, (
        f"bytes_before must be a positive int (got {bytes_before!r}). "
        "The span measures actual encoded byte counts, not placeholders."
    )
    assert isinstance(bytes_after, int) and bytes_after > 0, (
        f"bytes_after must be a positive int (got {bytes_after!r})."
    )
    assert bytes_after <= bytes_before, (
        f"prompt.game_state.bytes span reports bytes_after={bytes_after} "
        f"> bytes_before={bytes_before}. The cut should never grow the "
        "payload — if it does, the attribute wiring is inverted."
    )
