"""Failing tests — Story 49-3: location-patch enforcement when narrator
titles drift from state.

Playtest 2026-05-11 (Glenross) — the narrator silently relocated the scene
across four different **Room Title** headers (Bee Garden → Manse Garden →
Front Parlour → Study → Sickroom Passage) while canonical state held
``character_locations[Ziggy]='the_manse'`` for turns 1-5. The narrator wrote
new bold-title headers in prose without filling the structured
``patch.location`` field — ``has_location=False`` in game_patch.extracted
logs on turns 2-5. SOUL.md "Illusionism" failure mode: narrator and state
machine on different tracks, GM panel can't see the drift.

Story 49-3 fix:
1. **Auto-fill** ``result.location`` from a leading ``**Bold Title**`` in
   the prose when the structured patch field is empty AND the bold title
   disagrees with the current per-character location.
2. **Loud OTEL span** ``narrator.location_drift_repaired`` (severity=WARNING)
   so Sebastien's GM panel surfaces every repair — prompt iteration target.
3. (Out of scope for this test file — covered by orchestrator prompt-tests)
   Add a Recency-zone guardrail reminding the narrator to fill the patch
   when prose changes rooms.

Auto-fill, not fail-loud: blocking a turn is more expensive than a repair.
The OTEL span is the load-bearing audit so we can iterate on the prompt
later. ACs 1, 2, 6, 7 from .session/49-3-session.md.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, EdgePool, Inventory
from sidequest.game.persistence import GameMode
from sidequest.game.session import GameSnapshot
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.server.session_handler import _SessionData
from sidequest.server.session_room import SessionRoom

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


# ---------------------------------------------------------------------------
# Fixtures — mirror test_narration_apply_no_backfill.py shape
# ---------------------------------------------------------------------------


def _char(name: str) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            inventory=Inventory(),
            edge=EdgePool(current=10, max=10, base_max=10),
        ),
        backstory=f"{name}'s tale.",
        char_class="Delver",
        race="Human",
    )


def _sd(player_id: str, player_name: str, characters: list[Character]) -> _SessionData:
    return _SessionData(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        player_name=player_name,
        player_id=player_id,
        snapshot=GameSnapshot(
            genre_slug="caverns_and_claudes",
            world_slug="mawdeep",
            turn_manager=TurnManager(interaction=3),
            characters=list(characters),
        ),
        store=MagicMock(),
        genre_pack=load_genre_pack(CONTENT_GENRE_PACKS / "caverns_and_claudes"),
        orchestrator=MagicMock(),
        mode=GameMode.MULTIPLAYER,
    )


def _ziggy_session(prior_location: str = "the_manse") -> tuple[_SessionData, SessionRoom]:
    """Glenross-style fixture: single PC ``Ziggy`` parked at ``the_manse``."""
    ziggy = _char("Ziggy")
    sd = _sd("p:ziggy", "Ziggy", [ziggy])
    sd.snapshot.character_locations = {"Ziggy": prior_location}
    sd.snapshot.player_seats = {"p:ziggy": "Ziggy"}

    room = SessionRoom(slug="slug-drift-repair", mode=GameMode.MULTIPLAYER)
    room.seat("p:ziggy", character_slot="Ziggy")
    return sd, room


# ---------------------------------------------------------------------------
# AC1 — auto-fill from leading bold title when patch.location is empty
# and the bold title disagrees with current state.
# ---------------------------------------------------------------------------


def test_location_drift_repair_autofills_from_leading_bold_title() -> None:
    """The Glenross repro: narrator writes ``**The Manse — Front Parlour**``
    as the room header but leaves ``patch.location`` empty. State held
    ``the_manse``. Auto-fill must promote the bold title into
    ``character_locations[acting_char]`` so the GM panel and downstream
    consumers see the actual room."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot

    sd, room = _ziggy_session(prior_location="the_manse")

    result = NarrationTurnResult(
        narration=(
            "**The Manse — Front Parlour**\n\n"
            "The kettle hisses on the brass stand. Wisteria-light filters "
            "through the long window."
        ),
        location=None,
    )

    _apply_narration_result_to_snapshot(
        sd.snapshot,
        result,
        sd.player_name,
        room=room,
        pack=sd.genre_pack,
        acting_character_name="Ziggy",
    )

    assert sd.snapshot.character_locations.get("Ziggy") == "The Manse — Front Parlour", (
        "Story 49-3 AC1: when patch.location is empty and the narration "
        "opens with a bold room title that disagrees with current state, "
        "the title must be auto-promoted into character_locations so the "
        "GM panel sees the actual room instead of the stale prior location."
    )


def test_location_drift_repair_accepts_h2_prefixed_bold_title() -> None:
    """The narrator sometimes prefixes the title with ``## `` markdown
    heading syntax. The parser must still extract the bold-text content,
    not return None and skip the repair."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot

    sd, room = _ziggy_session(prior_location="the_manse")

    result = NarrationTurnResult(
        narration="## **Sickroom Passage**\n\nA candle gutters at the far end.",
        location=None,
    )

    _apply_narration_result_to_snapshot(
        sd.snapshot,
        result,
        sd.player_name,
        room=room,
        pack=sd.genre_pack,
        acting_character_name="Ziggy",
    )

    assert sd.snapshot.character_locations.get("Ziggy") == "Sickroom Passage", (
        "AC1 (markdown variant): the bold-title parser must accept the "
        "``## **Title**`` heading form, not just bare ``**Title**``."
    )


# ---------------------------------------------------------------------------
# AC2 — loud OTEL span fires with the audit attributes the GM panel needs.
# ---------------------------------------------------------------------------


def test_location_drift_repair_emits_otel_span(otel_capture) -> None:
    """Per CLAUDE.md OTEL Observability Principle: every drift-repair must
    emit ``narrator.location_drift_repaired`` so Sebastien's GM panel sees
    the lie-detector fire. Attributes must include the before/after pair
    plus actor context."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot

    sd, room = _ziggy_session(prior_location="the_manse")

    result = NarrationTurnResult(
        narration="**Front Parlour**\n\nThe parlour fire crackles low.",
        location=None,
    )

    _apply_narration_result_to_snapshot(
        sd.snapshot,
        result,
        sd.player_name,
        room=room,
        pack=sd.genre_pack,
        acting_character_name="Ziggy",
    )

    drift_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "narrator.location_drift_repaired"
    ]
    assert len(drift_spans) == 1, (
        "AC2: exactly one ``narrator.location_drift_repaired`` span must "
        f"fire on a single drift event; got {len(drift_spans)} "
        f"(span names seen: {[s.name for s in otel_capture.get_finished_spans()]})"
    )

    attrs = dict(drift_spans[0].attributes or {})
    assert attrs.get("old_state") == "the_manse", (
        "AC2: ``old_state`` must carry the stale character_locations entry "
        "before the repair, so the GM panel can show the before/after pair."
    )
    assert attrs.get("new_from_title") == "Front Parlour", (
        "AC2: ``new_from_title`` must carry the extracted bold-title text "
        "so the operator can correlate the prose with the repair."
    )
    assert attrs.get("character") == "Ziggy", (
        "AC2: ``character`` must carry the actor whose location was "
        "repaired — needed for multi-party GM panel filtering."
    )
    assert attrs.get("player_name") == "Ziggy", (
        "AC2: ``player_name`` must carry the player identity for audit."
    )
    # Turn is captured from snapshot.turn_manager.interaction (set to 3 in
    # the fixture). Cast through int() because OTEL attribute values may
    # land as IntAttributeValue or plain int depending on backend.
    assert int(attrs.get("turn", 0)) == 3, (
        "AC2: ``turn`` must carry the current interaction count so the "
        "GM panel can correlate drift to the playtest timeline."
    )


def test_location_drift_repair_span_is_warning_severity(otel_capture) -> None:
    """AC2 requires WARNING severity so the GM panel can surface the
    event in the lie-detector pane (not buried in INFO chatter). The
    span sets ``severity="warning"`` as an attribute — the
    ``server.watcher`` route translator promotes it to a warning-level
    typed event (see watcher.py ``attr_severity`` escape hatch)."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot

    sd, room = _ziggy_session(prior_location="the_manse")

    result = NarrationTurnResult(
        narration="**Study**\n\nThe doctor's books line the back wall.",
        location=None,
    )

    _apply_narration_result_to_snapshot(
        sd.snapshot,
        result,
        sd.player_name,
        room=room,
        pack=sd.genre_pack,
        acting_character_name="Ziggy",
    )

    drift_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "narrator.location_drift_repaired"
    ]
    assert drift_spans, "AC2 precondition: drift span must fire"
    severity = dict(drift_spans[0].attributes or {}).get("severity")
    assert severity == "warning", (
        "AC2: the drift span must carry ``severity='warning'`` so the "
        "watcher route translator promotes it past INFO. The GM panel's "
        "lie-detector pane filters on severity; INFO would bury this "
        "with routine state transitions."
    )


# ---------------------------------------------------------------------------
# Wiring tests — span constant exported, SpanRoute registered for watcher.
# These pin the wire-up so dev can't ship the auto-fill without the
# observability path the GM panel depends on.
# ---------------------------------------------------------------------------


def test_location_drift_repaired_span_constant_is_exported() -> None:
    """The span name is a contract — pin it as an importable constant so
    consumers can reference it without duplicating the string literal."""
    from sidequest.telemetry.spans.narrator import (
        SPAN_NARRATOR_LOCATION_DRIFT_REPAIRED,
    )

    assert SPAN_NARRATOR_LOCATION_DRIFT_REPAIRED == "narrator.location_drift_repaired"


def test_location_drift_repaired_span_is_routed_to_watcher() -> None:
    """SpanRoute registration is what makes the span visible to the GM
    panel — without it, the span lands in OTEL but no typed
    ``state_transition`` event is published. Verify the route exists and
    the extractor surfaces the audit fields the dashboard needs."""
    from sidequest.telemetry.spans import SPAN_ROUTES

    route = SPAN_ROUTES.get("narrator.location_drift_repaired")
    assert route is not None, (
        "SpanRoute for ``narrator.location_drift_repaired`` must be "
        "registered in SPAN_ROUTES; without it the watcher emits only the "
        "raw firehose row and the typed lie-detector event is silently lost."
    )
    assert route.event_type == "state_transition", (
        "Route event_type must be ``state_transition`` to match the rest "
        "of the location-/region- state transition family."
    )


# ---------------------------------------------------------------------------
# AC6 — when patch.location is explicitly set, trust the narrator's
# structured field. The auto-repair is a backstop, not a hijack.
# ---------------------------------------------------------------------------


def test_explicit_patch_location_is_not_overridden_by_bold_title() -> None:
    """If the narrator filled the structured ``location`` patch, that's a
    deliberate choice — the auto-repair must NOT second-guess it from the
    prose title. Otherwise the narrator can never name a room differently
    from the bold header (e.g. internal slug vs display name)."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot

    sd, room = _ziggy_session(prior_location="the_manse")

    result = NarrationTurnResult(
        narration="**Front Parlour**\n\nThe fire crackles low.",
        location="Library",
    )

    _apply_narration_result_to_snapshot(
        sd.snapshot,
        result,
        sd.player_name,
        room=room,
        pack=sd.genre_pack,
        acting_character_name="Ziggy",
    )

    assert sd.snapshot.character_locations.get("Ziggy") == "Library", (
        "AC6: explicit patch.location must win over the prose bold title. "
        "The auto-repair only runs when patch.location is falsy."
    )


def test_explicit_patch_location_emits_no_drift_span(otel_capture) -> None:
    """When patch.location is explicit, the drift-repair path is skipped
    entirely — no drift span, even if the bold title disagrees. (The
    bold title vs patch.location mismatch is a different concern; logging
    it would create false-positive drift events the operator has to
    dismiss.)"""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot

    sd, room = _ziggy_session(prior_location="the_manse")

    result = NarrationTurnResult(
        narration="**Front Parlour**\n\nThe fire crackles low.",
        location="Library",
    )

    _apply_narration_result_to_snapshot(
        sd.snapshot,
        result,
        sd.player_name,
        room=room,
        pack=sd.genre_pack,
        acting_character_name="Ziggy",
    )

    drift_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "narrator.location_drift_repaired"
    ]
    assert drift_spans == [], (
        "AC6: drift span must NOT fire when the narrator filled "
        "patch.location explicitly; the repair path is gated on the "
        "patch field being empty."
    )


# ---------------------------------------------------------------------------
# AC7 — when no bold title appears, silent no-op (no repair, no span).
# ---------------------------------------------------------------------------


def test_no_repair_when_narration_has_no_leading_bold_title() -> None:
    """A pure-prose turn (no scene change, no room header) must leave
    location alone. The auto-repair must NOT invent a location from
    inline bold or other formatting."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot

    sd, room = _ziggy_session(prior_location="the_manse")

    result = NarrationTurnResult(
        narration=(
            "Ziggy lingers by the kettle. The wisteria-light dims as a cloud crosses the sun."
        ),
        location=None,
    )

    _apply_narration_result_to_snapshot(
        sd.snapshot,
        result,
        sd.player_name,
        room=room,
        pack=sd.genre_pack,
        acting_character_name="Ziggy",
    )

    assert sd.snapshot.character_locations.get("Ziggy") == "the_manse", (
        "AC7: silent no-op when narration has no leading bold title — the "
        "prior character_locations entry must be preserved verbatim."
    )


def test_no_repair_when_inline_bold_is_not_a_leading_title(otel_capture) -> None:
    """Bold text mid-paragraph (emphasis on a word, not a room header)
    must NOT trigger the repair. The parser is anchored to a leading
    bold *title*, not any bold span anywhere in the prose."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot

    sd, room = _ziggy_session(prior_location="the_manse")

    result = NarrationTurnResult(
        narration=(
            "Ziggy sets the kettle down and whispers: “The **doctor** is "
            "lying.” The wisteria-light flickers."
        ),
        location=None,
    )

    _apply_narration_result_to_snapshot(
        sd.snapshot,
        result,
        sd.player_name,
        room=room,
        pack=sd.genre_pack,
        acting_character_name="Ziggy",
    )

    assert sd.snapshot.character_locations.get("Ziggy") == "the_manse", (
        "AC7 (edge): inline ``**doctor**`` emphasis must not be parsed as "
        "a room title; only a *leading* bold span counts."
    )

    drift_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "narrator.location_drift_repaired"
    ]
    assert drift_spans == [], (
        "AC7 (edge): no drift span when no leading bold title is present, "
        "even if there's bold emphasis elsewhere in the prose."
    )


# ---------------------------------------------------------------------------
# AC2 negative — repair must NOT fire when the bold title already matches
# current state. ("Emit only when auto-fill actually happens.")
# ---------------------------------------------------------------------------


def test_no_drift_span_when_bold_title_matches_current_state(otel_capture) -> None:
    """If the narrator's bold title agrees with character_locations
    already, there's no drift to repair — and no audit event to fire.
    Otherwise the GM panel sees a stream of false-positive 'repairs'
    every turn the narrator keeps the same room header."""
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.server.narration_apply import _apply_narration_result_to_snapshot

    sd, room = _ziggy_session(prior_location="The Manse — Front Parlour")

    result = NarrationTurnResult(
        narration="**The Manse — Front Parlour**\n\nThe kettle is empty now.",
        location=None,
    )

    _apply_narration_result_to_snapshot(
        sd.snapshot,
        result,
        sd.player_name,
        room=room,
        pack=sd.genre_pack,
        acting_character_name="Ziggy",
    )

    drift_spans = [
        s for s in otel_capture.get_finished_spans() if s.name == "narrator.location_drift_repaired"
    ]
    assert drift_spans == [], (
        "AC2 (negative): drift span must only fire when the candidate "
        "differs from current state. Identical bold title → no audit."
    )


# ---------------------------------------------------------------------------
# Watcher event wire — the GM panel reads typed events, not raw spans.
# Confirm the drift-repair publishes a ``state_transition`` event the
# panel can render.
# ---------------------------------------------------------------------------


# Note: the SpanRoute → publish_event watcher bridge requires the
# ``server.watcher`` SpanProcessor to be installed; unit-level coverage
# pins the *registration* (see ``test_location_drift_repaired_span_is_routed_to_watcher``)
# while integration coverage of the typed event lives in tests/integration/.
