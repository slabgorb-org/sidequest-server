"""Story 49-1 RED — session_helpers strips narrative_log from state_summary.

ADR-009 / Attention-Aware Prompt Zones: ``narrative_log`` currently rides
into the narrator prompt via the ``<game_state>`` JSON blob (Valley
zone) built at ``session_helpers.py:308`` (``snapshot.model_dump_json()``).
That puts conversational history in an attention-decayed zone, which is
how the post-098 narrator started losing prior-turn details (2026-05-11
Glenross gender flip).

This story:
  1. Moves the last K=2 entries into a high-attention Recency-zone section
     (covered by ``test_orchestrator_recency_narrative.py``).
  2. Removes the duplicate from the ``<game_state>`` JSON dump so the same
     data does not ride twice — once high-attention, once decayed. (AC #3
     — covered here.)
  3. Populates ``TurnContext.recent_narrative_log`` from the durable
     SQLite narrative_log via ``sd.store.recent_narrative(K)``.

  *(Originally this AC sourced from ``snapshot.narrative_log``. Per
  sq-playtest 2026-05-15: that mirror is only ever populated by
  world_materialization + lore_seeding, never by per-turn appends, so
  the recency injection was stuck at ``turn_count=0/total_tokens=0``
  forever in live play. The SQLite store is the canonical source —
  same alignment that Story 45-11's round-invariant lie-detector
  uses.)*

The integration uses live ``_build_turn_context`` to prove the wiring is
end-to-end — not just that the field exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, EdgePool, Inventory
from sidequest.game.session import GameSnapshot, NarrativeEntry
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from sidequest.server.session_handler import _build_turn_context, _SessionData
from tests._helpers.session_room import room_for

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _character(name: str) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="d",
            personality="p",
            inventory=Inventory(),
            edge=EdgePool(current=8, max=10, base_max=10),
        ),
        backstory="hero",
        char_class="Delver",
        race="Human",
    )


def _entry(*, round_: int, author: str, content: str) -> NarrativeEntry:
    return NarrativeEntry(round=round_, author=author, content=content)


def _glenross_log() -> list[NarrativeEntry]:
    """Six-entry log spanning rounds 1..5; the last 4 are the Recency
    window. Mirrors the 2026-05-11 Glenross playtest shape."""
    return [
        _entry(round_=1, author="Player", content="I enter the parlor."),
        _entry(round_=1, author="narrator", content="The fire crackles in the grate."),
        _entry(round_=2, author="Player", content="I greet the gardener."),
        _entry(round_=2, author="narrator", content="The gardener tips his cap."),
        _entry(round_=3, author="Player", content="I follow him to the bench."),
        _entry(
            round_=3,
            author="narrator",
            content="Father lies pale, the secateurs resting on the blotter.",
        ),
    ]


def _build_sd(snapshot: GameSnapshot, *, player_name: str = "Alice") -> _SessionData:
    pack = load_genre_pack(CONTENT_GENRE_PACKS / snapshot.genre_slug)
    sd = _SessionData(
        genre_slug=snapshot.genre_slug,
        world_slug=snapshot.world_slug,
        player_name=player_name,
        player_id=f"player:{player_name.lower()}",
        snapshot=snapshot,
        store=MagicMock(),
        genre_pack=pack,
        orchestrator=MagicMock(),
    )
    return sd


def _make_snapshot_with_log(log: list[NarrativeEntry]) -> GameSnapshot:
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=6),
        characters=[_character("Alice")],
        narrative_log=log,
    )
    snap.character_locations["Alice"] = "Main Hall"
    snap.player_seats["player:alice"] = "Alice"
    return snap


# ---------------------------------------------------------------------------
# AC #3: narrative_log dropped (or capped at 1) from state_summary JSON
# ---------------------------------------------------------------------------


def test_state_summary_does_not_carry_full_narrative_log():
    """When the snapshot has 6 narrative entries, the
    ``state_summary`` JSON the orchestrator receives must NOT carry all
    6 — that's the "JSON blob in Valley zone" pattern this story
    exists to kill. AC #3 allows either dropping the key entirely or
    capping at 1 (so the model can still feel one anchor). 6 entries in
    the JSON dump means the fix never landed.
    """
    log = _glenross_log()
    snap = _make_snapshot_with_log(log)
    sd = _build_sd(snap)
    sd._room = room_for(snap, slug="mawdeep")

    ctx = _build_turn_context(sd, room=sd._room)
    assert ctx.state_summary is not None

    payload = json.loads(ctx.state_summary)
    entries_in_state = payload.get("narrative_log", [])
    assert len(entries_in_state) <= 1, (
        "state_summary still carries the full narrative_log "
        f"({len(entries_in_state)} entries); the JSON-blob leak in the Valley "
        "zone was not stripped — same disease that caused the Glenross "
        "gender flip."
    )


def test_state_summary_prose_content_not_duplicated():
    """The whole prose of the most recent narrator turn must not appear
    inside the state_summary JSON blob. Recency-zone injection makes the
    duplicate pure decayed-attention noise — and exactly the noise that
    drowns out the high-attention copy when it does land."""
    log = _glenross_log()
    snap = _make_snapshot_with_log(log)
    sd = _build_sd(snap)
    sd._room = room_for(snap, slug="mawdeep")

    ctx = _build_turn_context(sd, room=sd._room)
    assert ctx.state_summary is not None

    # The most recent narrator entry's distinctive phrase must not be in
    # the state_summary JSON dump.
    distinctive_phrase = "secateurs resting on the blotter"
    assert distinctive_phrase not in ctx.state_summary, (
        "recent narrator prose still being dumped via state_summary JSON "
        "in the Valley zone — Recency-zone injection alone is not enough; "
        "the duplicate must be stripped."
    )


# ---------------------------------------------------------------------------
# AC #1 wiring: TurnContext.recent_narrative_log populated from snapshot
# ---------------------------------------------------------------------------


def test_recent_narrative_log_populated_on_turn_context_from_store():
    """``_build_turn_context`` MUST populate
    ``TurnContext.recent_narrative_log`` with the last K=2 entries from
    the durable SQLite store (K tightened from 4→2 in 57-1). Without
    this the orchestrator's Recency-zone section has no input and the
    fix is a no-op end-to-end (wiring test per CLAUDE.md).

    sq-playtest 2026-05-15: was previously sourced from
    ``snapshot.narrative_log``, but that mirror is only populated by
    world_materialization + lore_seeding (never by per-turn appends),
    so live recency injection stayed at 0/0 forever. The store is the
    canonical source.
    """
    log = _glenross_log()
    snap = _make_snapshot_with_log(log)
    sd = _build_sd(snap)
    sd._room = room_for(snap, slug="mawdeep")
    # Live store API: ``recent_narrative(limit)`` returns the most recent
    # ``limit`` entries oldest-first. Mirror the slice the SQLite query
    # would have produced over the durable log.
    sd.store.recent_narrative.return_value = list(log[-2:])

    ctx = _build_turn_context(sd, room=sd._room)

    recent = list(ctx.recent_narrative_log)
    assert recent, (
        "TurnContext.recent_narrative_log is empty — the store's 6 "
        "entries did not flow into the Recency-zone seam."
    )
    assert len(recent) == 2, f"expected last K=2 entries (post-57-1 default); got {len(recent)}"

    # And it MUST be the LAST two — not the first two. Chronological,
    # most-recent-window semantics.
    expected_contents = [e.content for e in log[-2:]]
    actual_contents = [e.content for e in recent]
    assert actual_contents == expected_contents, (
        f"recent_narrative_log holds wrong slice — "
        f"expected last-2 {expected_contents}, got {actual_contents}"
    )
    # Wiring assertion: _build_turn_context actually called the store, not
    # the snapshot mirror. Defends against a future refactor silently
    # reverting to ``snapshot.narrative_log[-K:]`` (which would pass the
    # content assertion above when the store happens to be empty — the
    # original 2026-05-15 bug repro).
    sd.store.recent_narrative.assert_called_once_with(2)


def test_recent_narrative_log_empty_on_fresh_session():
    """Symmetric guard: a store with no narrative_log must produce an
    empty list (not a None, not a synthetic placeholder). Zero-byte-leak
    discipline."""
    snap = _make_snapshot_with_log([])
    sd = _build_sd(snap)
    sd._room = room_for(snap, slug="mawdeep")
    sd.store.recent_narrative.return_value = []

    ctx = _build_turn_context(sd, room=sd._room)
    assert list(ctx.recent_narrative_log) == []
