"""Opening-scene inanimate props must persist to scene state (Story 126-18, hardened 126-28).

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

The behavior shipped in 126-18: ``OpeningSetting.present_props`` is a field,
``persist_opening_props`` writes the labels into ``room_states``, the flat-only
``opening.props_persisted`` span fires with count + ids, and the opening-resolution
seam (``_populate_opening_directive_on_chargen_complete``) wires it end-to-end.

Story 126-28 hardens this suite so it exercises the load-bearing edges the 126-18
RED set proxied past:
  - AC1 now drives the REAL snapshot-slimming projection (seeds ``player_seats`` so
    ``party_location()`` resolves, then asserts a decoy room is dropped while the
    prop room survives) — the prior assertion passed because projection was SKIPPED
    (no seated PC → ``party_location()`` None → ``room_states`` passed through
    unprojected), i.e. for the wrong reason.
  - the de-dup / idempotency contract (persisting twice never double-writes),
  - the wiring span payload (``props_persisted == N`` + the prop ids, not just
    truthy), all three props in the forensic snapshot test, and
  - the chassis-anchor seam path (``interior_room`` resolution, no ``location_label``).
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
    # (room_states is projected to the acting PC's current room). Seating the PC
    # in ``player_seats`` is load-bearing, not decoration: the router's
    # ``_build_state_summary`` resolves the projection room via
    # ``party_location()`` in *consensus* mode, which reads ``player_seats`` —
    # with no seated PC it returns None and slimming is SKIPPED, so a prop would
    # survive the summary for the wrong reason (the 126-28 hardening target).
    snap.player_seats["seat-1"] = "Margot"
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
            present_props=[ENVELOPE, PERNOD, ASHTRAY],
        ),
        establishing_narration=("The envelope is on the marble between the glass and the ashtray."),
        first_turn_invitation="Smoke curls toward the tin ceiling.",
    )
    assert list(opening.setting.present_props) == [ENVELOPE, PERNOD, ASHTRAY]


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
    """AC1 (the forensic test): after the opening, EVERY established prop label is
    present in the serialized snapshot — directly reproducing the bug, where the
    whole tableau ('envelope', Pernod glass, brass ashtray) was ABSENT from the
    snapshot. 126-28: assert all three, not just the envelope — the bug retracted
    the entire tableau, so a one-prop assertion underspecifies the regression."""
    from sidequest.server.websocket_handlers.opening_helpers import persist_opening_props

    snap = _fresh_snapshot()
    persist_opening_props(snap, [ENVELOPE, PERNOD, ASHTRAY], room_id=ROOM_ID)

    dump = json.dumps(snap.model_dump(mode="json"))
    for prop in (ENVELOPE, PERNOD, ASHTRAY):
        assert prop in dump, (
            f"Forensic regression: the opening-established {prop!r} must be present "
            "in the snapshot after scene setup (before turn 1). The whole tableau was "
            "absent in save 2026-06-19-annees_folles-cd25d503 — narrated, never persisted."
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


def test_persist_opening_props_is_idempotent() -> None:
    """126-28: persisting the same opening props twice must not double-write.

    The opening-resolution seam is not guaranteed to fire exactly once across a
    session's lifetime (resume re-runs chargen-complete paths; ADR-098 stateless
    turns re-materialize). A second pass with the same labels must leave the room
    holding each prop exactly once — no duplicate envelope, no growing list. The
    helper de-dups per label (``if prop not in room_state.props``); this pins that
    contract so a future refactor to a different store can't silently regress it."""
    from sidequest.server.websocket_handlers.opening_helpers import persist_opening_props

    snap = _fresh_snapshot()
    props = [ENVELOPE, PERNOD, ASHTRAY]
    persist_opening_props(snap, props, room_id=ROOM_ID)
    persist_opening_props(snap, props, room_id=ROOM_ID)

    stored = snap.room_states[ROOM_ID].props
    assert stored == props, (
        "persisting the same props twice must not double-write — each label appears "
        f"exactly once and order is preserved. Expected {props!r}, got {stored!r}."
    )
    # A partial re-persist (one overlapping, one new) appends only the new label.
    persist_opening_props(snap, [ENVELOPE, "matchbook"], room_id=ROOM_ID)
    assert snap.room_states[ROOM_ID].props == [*props, "matchbook"], (
        "an overlapping re-persist must append only the genuinely-new prop, never "
        f"re-add an existing one; got {snap.room_states[ROOM_ID].props!r}"
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


def test_persisted_prop_survives_the_real_slimming_projection() -> None:
    """AC2 (the load-bearing test): the bug is that the per-turn router (ADR-113)
    serializes the slimmed snapshot, finds no trace of the envelope, and emits a
    ``must_not_narrate`` directive. After persistence, the prop must survive the
    REAL ``apply_snapshot_slimming`` projection ``_build_state_summary`` runs —
    not merely appear because slimming was skipped.

    126-28 hardening: ``_build_state_summary`` resolves the projection room via
    ``party_location()`` in consensus mode, which needs a seated PC
    (``player_seats``). The 126-18 version seeded only ``character_locations``, so
    ``party_location()`` returned None, the room_states projection was SKIPPED, and
    ``room_states`` passed through whole — the envelope survived for the WRONG
    reason. We now seat the PC (via ``_fresh_snapshot``) AND plant a DECOY room the
    party is NOT in: a passing test must drop the decoy (proving the projection
    ran) while keeping the prop room."""
    from sidequest.server.intent_router_pass import _build_state_summary
    from sidequest.server.websocket_handlers.opening_helpers import persist_opening_props

    decoy_room = "annees_folles:back_alley"
    decoy_prop = "manhole cover"

    snap = _fresh_snapshot()
    persist_opening_props(snap, [ENVELOPE, PERNOD, ASHTRAY], room_id=ROOM_ID)
    # A room the party is NOT standing in — slimming must project it away.
    persist_opening_props(snap, [decoy_prop], room_id=decoy_room)

    summary = _build_state_summary(snap)
    room_states = summary.get("room_states", {})

    # Projection actually RAN: only the party's room survives (decoy dropped).
    assert set(room_states) == {ROOM_ID}, (
        "the real snapshot-slimming projection must keep ONLY the acting party's "
        f"room ({ROOM_ID!r}) and drop the decoy ({decoy_room!r}); got "
        f"room_states keys={sorted(room_states)!r}. If the decoy survived, "
        "party_location() returned None and slimming was skipped — the prop would "
        "then be visible for the wrong reason."
    )
    projected = json.dumps(room_states)
    for prop in (ENVELOPE, PERNOD, ASHTRAY):
        assert prop in projected, (
            f"the opening-established {prop!r} must survive the projection into the "
            "router's state summary — otherwise the router emits must_not_narrate and "
            f"denies the player's turn-1 interaction (the live bug). room_states={projected}"
        )
    assert decoy_prop not in projected, (
        "the decoy room's prop must NOT leak into the projected summary — it confirms "
        "the projection genuinely pruned non-current rooms rather than passing through."
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
            present_props=[ENVELOPE, PERNOD, ASHTRAY],
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
    assert len(persisted) == 1, (
        f"the opening-resolution seam must fire exactly one '{PROPS_PERSISTED_SPAN}' "
        f"span. Spans seen: {[c.args[0] for c in span_open.call_args_list if c.args]!r}"
    )
    # 126-28: assert the span PAYLOAD, not just that it fired — the GM-panel
    # lie-detector is only useful if it carries the real count and ids (a truthy
    # span with a wrong/empty payload would still pass a presence-only check).
    attrs = persisted[0].args[1] if len(persisted[0].args) > 1 else {}
    assert attrs.get("props_persisted") == 3, (
        "the seam's span must carry props_persisted == 3 (the authored tableau); "
        f"got {attrs.get('props_persisted')!r}"
    )
    prop_ids = str(attrs.get("prop_ids", ""))
    for prop in (ENVELOPE, PERNOD, ASHTRAY):
        assert prop in prop_ids, (
            f"the seam's span 'prop_ids' must enumerate every persisted prop; {prop!r} "
            f"missing from {prop_ids!r}"
        )
    dump = json.dumps(snap.model_dump(mode="json"))
    for prop in (ENVELOPE, PERNOD, ASHTRAY):
        assert prop in dump, (
            f"after the production opening-resolution seam runs, {prop!r} must be "
            "persisted in the snapshot (the live bug: narrated, never persisted)."
        )


def test_opening_resolution_seam_persists_props_on_chassis_anchor() -> None:
    """WIRING (126-28): the seam must persist present_props on the CHASSIS-anchor
    path too. The 126-18 wiring test only covered the ``location_label`` anchor;
    the seam resolves ``room_id = location_label or interior_room``, so a
    chassis-anchored opening (no ``location_label``, props belong to the ship's
    ``interior_room``) exercises the second, previously-untested branch.

    Drives the REAL seam with a chassis-anchored opening. The shipping pulp_noir
    world has no matching ``chassis_instance`` (the lookup falls through to
    ``chassis=None``, exactly as for a location anchor), but prop persistence keys
    off ``interior_room`` regardless — so the props must land in
    ``room_states[interior_room]`` and the span must fire.
    """
    import sidequest.server.session_handler  # noqa: F401 — circular-reexport ordering
    from sidequest.game.world_materialization import (
        CampaignMaturity,
        materialize_from_genre_pack,
    )
    from sidequest.genre.loader import GenreLoader
    from sidequest.server.session_helpers import _world_history_value
    from sidequest.server.websocket_handlers import opening_helpers

    interior_room = "le_dome_zinc:back_booth"

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

    # Chassis anchor: chassis_instance + interior_room, NO location_label. The
    # OpeningSetting invariant forbids present_npcs on a chassis anchor, but
    # present_props is allowed on either anchor kind (objects, not crew).
    chassis_opening = Opening(
        id="le_dome_zinc_booth_props",
        triggers=OpeningTrigger(mode="solo"),
        setting=OpeningSetting(
            chassis_instance="le_dome_zinc",
            interior_room=interior_room,
            present_props=[ENVELOPE, PERNOD, ASHTRAY],
        ),
        establishing_narration=("The envelope waits in the booth, between the glass and the ashtray."),
        first_turn_invitation="The zinc bar hums under the lamps.",
    )

    session_data = SimpleNamespace(
        opening_directive=None,
        opening_seed=None,
        _resolved_opening_id=None,
        genre_slug="pulp_noir",
        world_slug=world_slug,
    )

    with (
        patch.object(opening_helpers, "_resolve_opening_post_chargen", return_value=chassis_opening),
        patch.object(Span, "open", wraps=Span.open) as span_open,
    ):
        opening_helpers._populate_opening_directive_on_chargen_complete(
            session_data=session_data,  # type: ignore[arg-type]
            snapshot=snap,
            pack=pack,
            world_slug=world_slug,
            mode="solo",
        )

    # Props key off interior_room (not location_label, which is None here).
    assert interior_room in snap.room_states, (
        "chassis-anchored opening props must persist to the ship's interior_room; "
        f"room_states keys={sorted(snap.room_states)!r}"
    )
    room_props = snap.room_states[interior_room].props
    assert room_props == [ENVELOPE, PERNOD, ASHTRAY], (
        f"interior_room must hold the authored tableau; got {room_props!r}"
    )
    persisted = [
        c for c in span_open.call_args_list if c.args and c.args[0] == PROPS_PERSISTED_SPAN
    ]
    assert len(persisted) == 1, (
        f"the chassis-anchor seam must fire exactly one '{PROPS_PERSISTED_SPAN}' span. "
        f"Spans seen: {[c.args[0] for c in span_open.call_args_list if c.args]!r}"
    )
    attrs = persisted[0].args[1] if len(persisted[0].args) > 1 else {}
    assert attrs.get("props_persisted") == 3, (
        f"chassis-anchor span must carry props_persisted == 3; got {attrs.get('props_persisted')!r}"
    )
    assert attrs.get("room_id") == interior_room, (
        f"chassis-anchor span must key room_id to the interior_room; got {attrs.get('room_id')!r}"
    )
