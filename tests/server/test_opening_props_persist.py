"""Story 126-18 (RED) — opening-scene inanimate props must persist to scene state.

Forensic bug (sq-playtest-pingpong 2026-06-19, stored save
``2026-06-19-annees_folles-cd25d503``): the pulp_noir/annees_folles opening
establishes a tableau in prose — *"The envelope is on the marble between the
glass and the ashtray…"* — but turn-1 interaction with the envelope is DENIED
(*"There is no envelope. The marble tabletop holds nothing but your glass and a
brass ashtray"*). The string ``envelope`` was ABSENT from the entire snapshot;
``room_states={}``. The opening's interactable props were narrated as prose and
written to NO state object, so the next stateless per-turn narrator (ADR-098)
reads an empty scene, the ``must_not_narrate`` guard fires, and an
explicitly-baited hook is retracted (SOUL Yes-And / Diamonds-and-Coal).

SCOPE: OBJECTS ONLY. Opening-scene NPCs already persist + are interactable (the
``preload_authored_npcs`` path, ``world_materialization.py``). These tests must
never regress that path — props are a sibling of NPCs, not a replacement.

DESIGN CONTRACT (TEA, mirroring the working NPC path — "Don't Reinvent, Wire Up
What Exists", server CLAUDE.md). The Architect/Dev may refine the exact names;
see the TEA deviation log. The behavioral assertions (AC1 snapshot-dump, AC2
router-visibility) are deliberately STORAGE-AGNOSTIC so a different honored
store than ``room_states`` still satisfies them:

  1. ``OpeningSetting.present_props: list[str]`` — twin of ``present_npcs``;
     the interactable inanimate prop labels the opening establishes.
  2. ``persist_opening_props(snapshot, props, *, room_id)`` — twin of
     ``preload_authored_npcs``; writes the props into ``room_states`` (the AC's
     named honored-scene-truth store) so the next turn's router/narrator sees
     them.
  3. flat-only ``opening.props_persisted`` span carrying ``props_persisted``
     (count) + ``prop_ids`` — twin of ``npc.authored_loaded`` (GM-panel
     lie-detector for the inverted failure mode: good narration, empty state).

ALL tests FAIL NOW (RED): ``present_props`` is not a field, ``persist_opening_props``
does not exist, and nothing writes opening props to any persisted store. They go
GREEN once Dev adds the authoring field, the persistence helper, the span, and
wires the helper into the opening-resolution seam
(``_populate_opening_directive_on_chargen_complete``).

SKIP != RED — these must run and FAIL, not skip.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, Inventory
from sidequest.game.session import GameSnapshot
from sidequest.genre.models.narrative import Opening, OpeningSetting, OpeningTrigger
from sidequest.telemetry.spans import Span

# The span the fix must add. Asserted as a string LITERAL (not an imported
# constant) so the test is RED today — the constant does not exist yet. Mirrors
# the SKIP_SPAN_NAME pattern in tests/game/test_71_7_authored_crew_fresh_gate.py.
PROPS_PERSISTED_SPAN = "opening.props_persisted"

# The annees_folles tableau props from the forensic save.
ENVELOPE = "envelope"
PERNOD = "Pernod glass"
ASHTRAY = "brass ashtray"
ROOM_ID = "annees_folles:cafe_le_dome"

CONTENT_ROOT = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"


def _fresh_snapshot() -> GameSnapshot:
    """A fresh single-PC snapshot anchored in ROOM_ID (so projection keeps it)."""
    snap = GameSnapshot(genre_slug="pulp_noir", world_slug="annees_folles")
    snap.characters.append(
        Character(
            core=CreatureCore(
                name="Margot",
                description="A weary investigator.",
                personality="watchful",
                inventory=Inventory(),
            ),
            char_class="Detective",
            race="Human",
            backstory="A past she'd rather not discuss.",
            pronouns="she/her",
        )
    )
    # Anchor the party in the prop room so apply_snapshot_slimming keeps it
    # (room_states is projected to the acting PC's current room).
    snap.character_locations["Margot"] = ROOM_ID
    return snap


# ---------------------------------------------------------------------------
# AC1 (authoring surface) — the opening can DECLARE interactable props
# ---------------------------------------------------------------------------


def test_opening_setting_has_present_props_field() -> None:
    """AC1: an Opening must be able to declare its interactable inanimate props,
    the object-world twin of ``present_npcs``. Reflection tripwire (sanctioned by
    server CLAUDE.md "No Source-Text Wiring Tests" — interrogates the runtime
    model, not source text). RED today: the field does not exist."""
    assert "present_props" in OpeningSetting.model_fields, (
        "OpeningSetting must gain a 'present_props' field (twin of 'present_npcs') "
        "so an opening can declare interactable inanimate props (envelope, Pernod, "
        f"ashtray). Current fields: {sorted(OpeningSetting.model_fields)!r}"
    )


def test_opening_with_present_props_constructs() -> None:
    """AC1: a location-anchored opening can be authored WITH present_props.
    RED today: ``OpeningSetting`` is ``extra='forbid'`` and has no such field, so
    construction raises ValidationError."""
    opening = Opening(
        id="annees_folles_cafe",
        triggers=OpeningTrigger(mode="solo"),
        setting=OpeningSetting(
            location_label="Café Le Dôme",
            present_props=[ENVELOPE, PERNOD, ASHTRAY],  # type: ignore[call-arg]
        ),
        establishing_narration=("The envelope is on the marble between the glass and the ashtray."),
        first_turn_invitation="Smoke curls toward the tin ceiling.",
    )
    assert list(opening.setting.present_props) == [ENVELOPE, PERNOD, ASHTRAY]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# AC1 (persistence) — props land in a persisted, honored-scene-truth store
# ---------------------------------------------------------------------------


def test_persist_opening_props_writes_into_room_states() -> None:
    """AC1: persisting opening props creates a room_states entry that records
    them. Storage-field-agnostic WITHIN room_states (asserts the labels appear in
    the room state's dump, not a specific attribute name). RED today:
    ``persist_opening_props`` does not exist (ImportError)."""
    from sidequest.server.websocket_handlers.opening_helpers import persist_opening_props

    snap = _fresh_snapshot()
    persist_opening_props(snap, [ENVELOPE, PERNOD, ASHTRAY], room_id=ROOM_ID)

    assert ROOM_ID in snap.room_states, (
        "persist_opening_props must create a room_states entry for the opening's "
        f"room; room_states={snap.room_states!r}"
    )
    room_dump = json.dumps(snap.room_states[ROOM_ID].model_dump(mode="json"))
    for prop in (ENVELOPE, PERNOD, ASHTRAY):
        assert prop in room_dump, (
            f"opening prop {prop!r} not recorded in room_states[{ROOM_ID!r}]: {room_dump}"
        )


def test_persisted_opening_prop_appears_in_full_snapshot() -> None:
    """AC1 (the forensic test): after the opening, the prop label is present
    SOMEWHERE in the serialized snapshot — directly reproducing the bug, where
    'envelope' was ABSENT from the entire snapshot. RED today."""
    from sidequest.server.websocket_handlers.opening_helpers import persist_opening_props

    snap = _fresh_snapshot()
    persist_opening_props(snap, [ENVELOPE, PERNOD, ASHTRAY], room_id=ROOM_ID)

    dump = json.dumps(snap.model_dump(mode="json"))
    assert ENVELOPE in dump, (
        "Forensic regression: the opening-established 'envelope' must be present "
        "in the snapshot after scene setup (before turn 1). It was absent in save "
        "2026-06-19-annees_folles-cd25d503 — narrated but never persisted."
    )


def test_persist_opening_props_empty_is_noop() -> None:
    """No props → no room_states entry and no span. A hard no-op, mirroring the
    ``preload_authored_npcs`` empty-list contract (nothing to persist, nothing to
    observe — no silent phantom state)."""
    from sidequest.server.websocket_handlers.opening_helpers import persist_opening_props

    snap = _fresh_snapshot()
    with patch.object(Span, "open", wraps=Span.open) as span_open:
        persist_opening_props(snap, [], room_id=ROOM_ID)

    assert ROOM_ID not in snap.room_states, (
        "Empty props must not fabricate a room_states entry (no phantom scene state)."
    )
    persisted = [
        c for c in span_open.call_args_list if c.args and c.args[0] == PROPS_PERSISTED_SPAN
    ]
    assert not persisted, (
        f"Empty props must not emit '{PROPS_PERSISTED_SPAN}' — there is nothing to observe."
    )


# ---------------------------------------------------------------------------
# AC4 (OTEL) — props_persisted span with count + ids (GM-panel lie-detector)
# ---------------------------------------------------------------------------


def test_persist_opening_props_emits_span_with_count_and_ids() -> None:
    """AC4: persisting props emits an ``opening.props_persisted`` span carrying
    the count and the prop ids, so the GM panel can confirm props were WRITTEN
    (not just narrated). RED today."""
    from sidequest.server.websocket_handlers.opening_helpers import persist_opening_props

    snap = _fresh_snapshot()
    props = [ENVELOPE, PERNOD, ASHTRAY]
    with patch.object(Span, "open", wraps=Span.open) as span_open:
        persist_opening_props(snap, props, room_id=ROOM_ID)

    persisted = [
        c for c in span_open.call_args_list if c.args and c.args[0] == PROPS_PERSISTED_SPAN
    ]
    assert len(persisted) == 1, (
        f"expected exactly one '{PROPS_PERSISTED_SPAN}' span; got {len(persisted)}. "
        f"All spans: {[c.args[0] for c in span_open.call_args_list if c.args]!r}"
    )
    attrs = persisted[0].args[1] if len(persisted[0].args) > 1 else {}
    assert attrs.get("props_persisted") == len(props), (
        f"span must carry props_persisted == {len(props)}; got {attrs.get('props_persisted')!r}"
    )
    prop_ids = str(attrs.get("prop_ids", ""))
    for prop in props:
        assert prop in prop_ids, (
            f"span 'prop_ids' must enumerate persisted props; {prop!r} missing from {prop_ids!r}"
        )


# ---------------------------------------------------------------------------
# AC3 (scope) — OBJECTS ONLY; NPC persistence is untouched
# ---------------------------------------------------------------------------


def test_persist_opening_props_does_not_touch_npcs() -> None:
    """AC3: prop persistence must not add to, drop from, or otherwise disturb
    ``snapshot.npcs`` — props are objects, not NPCs. RED today (ImportError)."""
    from sidequest.server.websocket_handlers.opening_helpers import persist_opening_props

    snap = _fresh_snapshot()
    # A pre-existing opening-scene NPC, as the working preload path would seed.
    sentinel_npc = MagicMock()
    sentinel_npc.core = MagicMock()
    sentinel_npc.core.name = "Henri"
    snap.npcs.append(sentinel_npc)
    npcs_before = list(snap.npcs)

    persist_opening_props(snap, [ENVELOPE, PERNOD, ASHTRAY], room_id=ROOM_ID)

    assert snap.npcs == npcs_before, (
        "prop persistence must not mutate snapshot.npcs (objects-only scope; "
        f"NPC seeding must be untouched). before={npcs_before!r} after={snap.npcs!r}"
    )
    assert sentinel_npc in snap.npcs, "the opening-scene NPC must survive prop persistence"


# ---------------------------------------------------------------------------
# AC2 — the persisted prop is VISIBLE to the router/narrator state summary,
# so the must_not_narrate guard has no basis to deny it on turn 1.
# ---------------------------------------------------------------------------


def test_persisted_prop_visible_in_router_state_summary() -> None:
    """AC2 (deterministic proxy): the bug is that the per-turn router (ADR-113)
    serializes the snapshot, finds no trace of the envelope, and emits a
    ``must_not_narrate`` directive. After persistence, the prop must appear in
    the slimmed state summary the router consumes — removing the guard's basis to
    fire. (The full 'guard never fires' is an LLM behavior verified in playtest;
    this pins the deterministic precondition.) RED today."""
    from sidequest.server.intent_router_pass import _build_state_summary
    from sidequest.server.websocket_handlers.opening_helpers import persist_opening_props

    snap = _fresh_snapshot()
    persist_opening_props(snap, [ENVELOPE, PERNOD, ASHTRAY], room_id=ROOM_ID)

    summary = _build_state_summary(snap)
    assert ENVELOPE in json.dumps(summary), (
        "The router's state summary must contain the opening-established 'envelope' "
        "after persistence — otherwise the router cannot know it exists and emits "
        "must_not_narrate, denying the player's turn-1 interaction (the live bug). "
        f"summary keys={sorted(summary)!r}"
    )


# ---------------------------------------------------------------------------
# WIRING — the production opening-resolution seam actually persists the props.
# (server CLAUDE.md: "Every Test Suite Needs a Wiring Test" + "No Source-Text
# Wiring Tests" → drive the real seam, assert the OTEL span fired.)
# ---------------------------------------------------------------------------


def test_opening_resolution_seam_persists_present_props() -> None:
    """WIRING: ``_populate_opening_directive_on_chargen_complete`` (the sole
    production caller of opening resolution at chargen-complete) must persist the
    resolved opening's ``present_props`` and fire ``opening.props_persisted``.

    Drives the REAL seam against the real shipping pulp_noir pack, with the
    resolver monkeypatched to return a location-anchored opening that declares
    present_props (real content authoring is a content follow-up). RED today: the
    opening cannot even be constructed with present_props (field absent), and the
    seam has no prop-persistence step, so the span never fires.
    """
    import sidequest.server.session_handler  # noqa: F401 — circular-reexport ordering
    from sidequest.game.world_materialization import (
        CampaignMaturity,
        materialize_from_genre_pack,
    )
    from sidequest.genre.loader import GenreLoader
    from sidequest.server.session_helpers import _world_history_value
    from sidequest.server.websocket_handlers import opening_helpers

    pack = GenreLoader([CONTENT_ROOT]).load("pulp_noir")
    world_slug = "annees_folles"
    history = _world_history_value(pack, world_slug)
    snap = materialize_from_genre_pack(history, CampaignMaturity.Fresh, "pulp_noir", world_slug)
    snap.characters.append(
        Character(
            core=CreatureCore(
                name="Margot",
                description="A weary investigator.",
                personality="watchful",
                inventory=Inventory(),
            ),
            char_class="Detective",
            race="Human",
            backstory="A past she'd rather not discuss.",
            pronouns="she/her",
        )
    )

    prop_opening = Opening(
        id="annees_folles_cafe_props",
        triggers=OpeningTrigger(mode="solo"),
        setting=OpeningSetting(
            location_label="Café Le Dôme",
            present_props=[ENVELOPE, PERNOD, ASHTRAY],  # type: ignore[call-arg]
        ),
        establishing_narration=("The envelope is on the marble between the glass and the ashtray."),
        first_turn_invitation="Smoke curls toward the tin ceiling.",
    )

    session_data = SimpleNamespace(
        opening_directive=None,
        opening_seed=None,
        _resolved_opening_id=None,
        genre_slug="pulp_noir",
        world_slug=world_slug,
    )

    with (
        patch.object(opening_helpers, "_resolve_opening_post_chargen", return_value=prop_opening),
        patch.object(Span, "open", wraps=Span.open) as span_open,
    ):
        opening_helpers._populate_opening_directive_on_chargen_complete(
            session_data=session_data,  # type: ignore[arg-type]
            snapshot=snap,
            pack=pack,
            world_slug=world_slug,
            mode="solo",
        )

    persisted = [
        c for c in span_open.call_args_list if c.args and c.args[0] == PROPS_PERSISTED_SPAN
    ]
    assert persisted, (
        f"the opening-resolution seam must persist present_props and fire "
        f"'{PROPS_PERSISTED_SPAN}'. Spans seen: "
        f"{[c.args[0] for c in span_open.call_args_list if c.args]!r}"
    )
    dump = json.dumps(snap.model_dump(mode="json"))
    assert ENVELOPE in dump, (
        "after the production opening-resolution seam runs, the envelope must be "
        "persisted in the snapshot (the live bug: narrated, never persisted)."
    )
