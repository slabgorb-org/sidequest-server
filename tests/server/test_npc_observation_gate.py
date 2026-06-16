"""Story 49-6 RED — Ratification gate for narrator_invented NPCs.

Background: Story 49-2 (completed 2026-05-12) added a server-side catch-loop
that auto-mints ``NpcPoolMember`` entries when the narrator names a person
via role (Father, mother, the doctor, ...) or honorific (Mrs. Gow, Dr.
Sallow, ...) in prose but omits them from ``npcs_present``. The mint is
fire-and-forget — once an NPC is in the pool it stays there.

That over-pins phantom NPCs. The Glenross 2026-05-11 playtest showed the
failure mode in reverse: turn 5 narrator wrote about Father; turn 6
narrator drifted to "the wee one's mother" — if both had been auto-minted
without a ratification step, the pool would carry Father and Mother (a
gender-flipped pair) forever, even though only one of them was a real
recurring character in the world.

This story closes the loop with a one-turn observation gate. Auto-minted
``NpcPoolMember`` entries are flagged ``observation_pending=True``. On
the next turn, the gate examines the narrator's ``npcs_present`` patch:
- Pending member referenced (by name OR role) → flag flips to False
  (member promoted to persistent pool status). Emits
  ``npc.observation_gate_promoted``.
- Pending member NOT referenced → removed from ``npc_pool`` entirely.
  Emits ``npc.observation_gate_purged``.

Non-pending pool members (world-authored, already-promoted, legacy
``narrator_invented`` migrated entries) are untouched.

Spec sources:
- ``.session/49-6-session.md`` — AC list (epic does not enumerate AC
  itself; SM derived from the story-context paragraph).
- ``sprint/epic-49.yaml`` — 49-6 story metadata, 49-2 sibling completed.

Contracts under test:

1. **Model change.** ``NpcPoolMember`` gains
   ``observation_pending: bool = False`` (default keeps legacy snapshots
   working — they read as already-ratified).

2. **OTEL spans.**
   - ``SPAN_NPC_OBSERVATION_GATE_PROMOTED = "npc.observation_gate_promoted"``
   - ``SPAN_NPC_OBSERVATION_GATE_PURGED = "npc.observation_gate_purged"``
   Both route to ``state_transition`` under ``component="npc_registry"``.
   Helpers ``npc_observation_gate_promoted_span`` /
   ``npc_observation_gate_purged_span`` exported from
   ``sidequest.telemetry.spans``.

3. **Auto-mint wiring.** ``_auto_mint_prose_only_npcs`` sets
   ``observation_pending=True`` on every member it appends to
   ``snapshot.npc_pool``.

4. **Gate function.** A new helper
   ``_apply_npc_observation_gate(*, snapshot, emitted_mentions,
   turn_num) -> None`` in ``sidequest.server.session_helpers`` evaluates
   every ``observation_pending=True`` pool member against
   ``emitted_mentions``: promote on match, purge on miss.

5. **Pipeline wiring.** ``_apply_narration_result_to_snapshot``
   invokes the gate BEFORE auto-mint runs for the current turn (so the
   gate examines the *prior* turn's pending members against the
   *current* turn's mentions, never the same turn's own freshly-minted
   members).

Related: CLAUDE.md OTEL Observability Principle, ADR-031 Game Watcher,
SOUL.md "Living World" / "Cut the Dull Bits" (we keep names that recur,
drop names that don't earn their place).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult, NpcMention
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot
from tests._helpers.session_room import room_for

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROMOTED_SPAN_NAME = "npc.observation_gate_promoted"
PURGED_SPAN_NAME = "npc.observation_gate_purged"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pending_member(
    *,
    name: str,
    role: str | None = None,
    pronouns: str | None = None,
    drawn_from: str = "dialogue_extraction",
) -> NpcPoolMember:
    """Build a freshly-auto-minted pool member (observation_pending=True).

    Mirrors the kwargs ``_auto_mint_prose_only_npcs`` uses at mint time.
    """
    return NpcPoolMember(
        name=name,
        role=role,
        pronouns=pronouns,
        drawn_from=drawn_from,
        observation_pending=True,
    )


def _mention(name: str = "", role: str = "") -> NpcMention:
    """Build a minimal ``NpcMention`` matching the narrator's structured
    emission shape (bare-name or role-only forms both valid)."""
    return NpcMention(name=name, role=role)


def _spans_named(otel_capture: InMemorySpanExporter, span_name: str) -> list:
    """Filter captured spans by name.

    Return type is bare ``list`` because the OTEL exporter yields
    ``ReadableSpan`` instances under an SDK-version-dependent type; the
    49-2 RED file made the same choice for the same reason.
    """
    return [s for s in otel_capture.get_finished_spans() if s.name == span_name]


def _attr(span, key: str):
    """Safe attribute lookup — span.attributes can be ``None`` for spans
    constructed without an attribute dict."""
    return (span.attributes or {}).get(key)


# ===========================================================================
# Group A — Model field tests
# ===========================================================================


def test_npc_pool_member_has_observation_pending_field():
    """The pool member model must carry an ``observation_pending`` field
    so the gate has a flag to read. Without this, the auto-mint cannot
    distinguish "needs ratification" from "already canonical" and the
    gate has no place to write its decision.
    """
    member = NpcPoolMember(name="Father", drawn_from="dialogue_extraction")
    assert hasattr(member, "observation_pending"), (
        "NpcPoolMember must expose 'observation_pending' — the ratification "
        "gate has no flag to read otherwise. Add `observation_pending: "
        "bool = False` to sidequest/game/npc_pool.py."
    )


def test_npc_pool_member_observation_pending_default_is_false():
    """Default MUST be ``False`` so legacy snapshots (saved before this
    story) and world-authored pool members (genre packs that pre-populate
    npc_pool at session start) read as already-ratified. A default of
    ``True`` would cause the gate to purge every prior NPC on the first
    post-migration turn — Keith's durable-retention requirement (memory
    note feedback_durable_retention) forbids this.
    """
    member = NpcPoolMember(name="Hilde", drawn_from="world_authored")
    assert member.observation_pending is False, (
        f"Default observation_pending must be False to preserve legacy "
        f"saves and world-authored pool entries; got {member.observation_pending!r}."
    )


def test_npc_pool_member_observation_pending_accepts_true():
    """The constructor must accept ``observation_pending=True`` so
    auto-mint can flag newly-minted entries."""
    member = NpcPoolMember(
        name="Father",
        role="father",
        pronouns="he/him",
        drawn_from="dialogue_extraction",
        observation_pending=True,
    )
    assert member.observation_pending is True, (
        "observation_pending must round-trip through the constructor."
    )


def test_npc_pool_member_observation_pending_round_trips_through_serialization():
    """Pydantic round-trip must preserve ``observation_pending`` so the
    SQLite save store (GameSnapshot is persisted as serialized JSON via
    SqliteStore) does not lose the gate flag between turns. If
    serialization drops the field, every ``observation_pending=True``
    entry becomes ratified-by-default on reload — turning the gate off
    silently for any save that survives a restart.
    """
    original = NpcPoolMember(
        name="Mother",
        role="mother",
        pronouns="she/her",
        drawn_from="dialogue_extraction",
        observation_pending=True,
    )
    blob = original.model_dump()
    restored = NpcPoolMember(**blob)
    assert restored.observation_pending is True, (
        "observation_pending must survive model_dump/round-trip; got "
        f"{restored.observation_pending!r}. Field is required to be a "
        "regular pydantic field, not an excluded/computed property."
    )


# ===========================================================================
# Group B — OTEL span catalog
# ===========================================================================


def test_span_observation_gate_promoted_is_defined_in_catalog():
    """The promote span must register in the telemetry catalog so the
    GM panel can render the lie-detector signal. Per the OTEL
    Observability Principle (CLAUDE.md), every gate decision MUST emit
    a span — without it Sebastien's panel cannot tell whether the gate
    is running or whether the system is silently dropping entries.
    """
    from sidequest.telemetry import spans as spans_module

    assert hasattr(spans_module, "SPAN_NPC_OBSERVATION_GATE_PROMOTED"), (
        "SPAN_NPC_OBSERVATION_GATE_PROMOTED missing from telemetry "
        "catalog — the GM panel has no way to distinguish promote vs "
        "purge decisions, breaking the OTEL Observability Principle."
    )
    assert spans_module.SPAN_NPC_OBSERVATION_GATE_PROMOTED == PROMOTED_SPAN_NAME, (
        f"Span name must be exactly {PROMOTED_SPAN_NAME!r} for the GM panel filter to match."
    )


def test_span_observation_gate_purged_is_defined_in_catalog():
    """The purge span must register in the telemetry catalog. Purge is
    the destructive arm of the gate — without an audit span, NPC
    deletions are invisible and impossible to debug if the gate ever
    purges someone it shouldn't.
    """
    from sidequest.telemetry import spans as spans_module

    assert hasattr(spans_module, "SPAN_NPC_OBSERVATION_GATE_PURGED"), (
        "SPAN_NPC_OBSERVATION_GATE_PURGED missing — purge decisions "
        "must be auditable. The GM panel must see every drop."
    )
    assert spans_module.SPAN_NPC_OBSERVATION_GATE_PURGED == PURGED_SPAN_NAME, (
        f"Span name must be exactly {PURGED_SPAN_NAME!r}."
    )


def test_observation_gate_promoted_span_is_routed():
    """Live spans must register a ``SpanRoute`` or the watcher drops
    them. Promote routes as ``state_transition`` under
    ``component="npc_registry"`` (parallel to ``npc.auto_registered``,
    ``npc.auto_minted_from_prose``).
    """
    from sidequest.telemetry.spans import SPAN_ROUTES

    assert PROMOTED_SPAN_NAME in SPAN_ROUTES, (
        f"{PROMOTED_SPAN_NAME!r} not in SPAN_ROUTES — GameWatcher will "
        "drop the event and the GM panel will never see the promote."
    )
    route = SPAN_ROUTES[PROMOTED_SPAN_NAME]
    assert route.event_type == "state_transition", (
        "Promote event must route as state_transition for GM-panel rendering."
    )
    assert route.component == "npc_registry", (
        "Component must be 'npc_registry' to share the NPC-state column "
        "with auto_registered / auto_minted_from_prose / referenced."
    )


def test_observation_gate_purged_span_is_routed():
    """Purge span must also register in SPAN_ROUTES with the same
    component as its sibling spans."""
    from sidequest.telemetry.spans import SPAN_ROUTES

    assert PURGED_SPAN_NAME in SPAN_ROUTES, (
        f"{PURGED_SPAN_NAME!r} not in SPAN_ROUTES — purges silently dropped."
    )
    route = SPAN_ROUTES[PURGED_SPAN_NAME]
    assert route.event_type == "state_transition"
    assert route.component == "npc_registry"


def test_observation_gate_span_helpers_are_exported():
    """The context-manager helpers must be exported from
    ``sidequest.telemetry.spans`` so production code can import them
    flat (parallel to ``npc_auto_minted_from_prose_span``)."""
    from sidequest.telemetry import spans as spans_module

    assert hasattr(spans_module, "npc_observation_gate_promoted_span"), (
        "npc_observation_gate_promoted_span helper must be exported."
    )
    assert hasattr(spans_module, "npc_observation_gate_purged_span"), (
        "npc_observation_gate_purged_span helper must be exported."
    )


def test_observation_gate_promoted_span_route_extracts_required_attrs():
    """The route's extract lambda must surface the attributes the GM
    panel needs: ``name``, ``role``, ``turn_number``, ``op``."""
    from sidequest.telemetry.spans import SPAN_ROUTES

    route = SPAN_ROUTES[PROMOTED_SPAN_NAME]

    class _Stub:
        attributes = {
            "npc_name": "Father",
            "role": "father",
            "turn_number": 6,
        }

    extracted = route.extract(_Stub())
    assert extracted.get("op") == "observation_gate_promoted", (
        "Route must surface op='observation_gate_promoted' so the GM "
        "panel can filter promote events distinct from purge."
    )
    assert extracted.get("name") == "Father"
    assert extracted.get("role") == "father"
    assert extracted.get("turn_number") == 6


def test_observation_gate_purged_span_route_extracts_required_attrs():
    """The purge route must surface the same attribute set with
    op='observation_gate_purged' so the GM panel can distinguish drops
    from promotions."""
    from sidequest.telemetry.spans import SPAN_ROUTES

    route = SPAN_ROUTES[PURGED_SPAN_NAME]

    class _Stub:
        attributes = {
            "npc_name": "Mother",
            "role": "mother",
            "turn_number": 8,
        }

    extracted = route.extract(_Stub())
    assert extracted.get("op") == "observation_gate_purged"
    assert extracted.get("name") == "Mother"
    assert extracted.get("role") == "mother"
    assert extracted.get("turn_number") == 8


# ===========================================================================
# Group C — Auto-mint wiring (mint side)
# ===========================================================================


def test_auto_mint_flags_new_member_observation_pending_true(otel_capture):
    """Every NPC the prose-scanner appends must carry
    ``observation_pending=True``. Otherwise the gate has nothing to
    evaluate next turn and the false-positive scenario (the entire
    motivation for this story) remains open.
    """
    from sidequest.server.session_helpers import _auto_mint_prose_only_npcs

    snapshot = GameSnapshot()
    _auto_mint_prose_only_npcs(
        snapshot=snapshot,
        narration_text="The patient is Father; he is grave.",
        emitted_mentions=[],
        turn_num=5,
    )

    pool_after = list(snapshot.npc_pool)
    assert pool_after, (
        "Auto-mint must produce a pool member for the Father scenario "
        "(this is the 49-2 baseline — if THIS assertion fails the test "
        "is mis-fixturing, not failing 49-6)."
    )
    minted = next((m for m in pool_after if (m.role or "").casefold() == "father"), None)
    assert minted is not None, (
        f"Father not found in pool — fixture broken. Pool: {[(m.name, m.role) for m in pool_after]}"
    )
    assert minted.observation_pending is True, (
        f"Auto-mint must flag new members observation_pending=True so "
        f"the gate sees them next turn; got "
        f"observation_pending={minted.observation_pending!r}."
    )


def test_auto_mint_preserves_unrelated_pool_members_observation_state():
    """Auto-mint scanning prose for new NPCs must not flip
    ``observation_pending`` on existing pool members. A world-authored
    barkeep (observation_pending=False, drawn_from='world_authored')
    must stay non-pending even if a new mint happens in the same turn.
    """
    from sidequest.server.session_helpers import _auto_mint_prose_only_npcs

    snapshot = GameSnapshot()
    snapshot.npc_pool.append(
        NpcPoolMember(
            name="Hilde",
            role="barkeep",
            pronouns="she/her",
            drawn_from="world_authored",
            observation_pending=False,
        )
    )

    _auto_mint_prose_only_npcs(
        snapshot=snapshot,
        narration_text="The patient is Father; he is grave.",
        emitted_mentions=[],
        turn_num=5,
    )

    hilde = next((m for m in snapshot.npc_pool if m.name == "Hilde"), None)
    assert hilde is not None, "Pre-existing pool member must not be removed by auto-mint."
    assert hilde.observation_pending is False, (
        "Auto-mint must not flip observation_pending on existing members; "
        f"Hilde now has observation_pending={hilde.observation_pending!r}."
    )


# ===========================================================================
# Group D — Gate evaluation (unit tests)
# ===========================================================================


def test_gate_promotes_pending_member_when_name_matches_mention(otel_capture):
    """Headline AC: a pending member whose ``name`` appears in the
    current turn's ``emitted_mentions`` is RATIFIED — its
    ``observation_pending`` flag flips to ``False`` and the member
    remains in ``snapshot.npc_pool``.
    """
    from sidequest.server.session_helpers import _apply_npc_observation_gate

    snapshot = GameSnapshot()
    snapshot.npc_pool.append(_pending_member(name="Father", role="father", pronouns="he/him"))

    _apply_npc_observation_gate(
        snapshot=snapshot,
        emitted_mentions=[_mention(name="Father")],
        turn_num=6,
    )

    survivor = next((m for m in snapshot.npc_pool if m.name == "Father"), None)
    assert survivor is not None, (
        "Father must remain in the pool after a matched mention; "
        f"pool after gate: {[m.name for m in snapshot.npc_pool]}"
    )
    assert survivor.observation_pending is False, (
        "Ratified member's observation_pending must flip to False; got "
        f"{survivor.observation_pending!r}."
    )


def test_gate_promotes_pending_member_when_role_matches_mention(otel_capture):
    """A pending member whose ``role`` (case-folded) appears in any
    ``emitted_mentions.role`` slot is also ratified. The narrator may
    cite "the father" as a role rather than naming him, and the gate
    must recognize that as the same entity.
    """
    from sidequest.server.session_helpers import _apply_npc_observation_gate

    snapshot = GameSnapshot()
    snapshot.npc_pool.append(_pending_member(name="Father", role="father", pronouns="he/him"))

    _apply_npc_observation_gate(
        snapshot=snapshot,
        emitted_mentions=[_mention(name="", role="father")],
        turn_num=6,
    )

    survivor = next((m for m in snapshot.npc_pool if m.name == "Father"), None)
    assert survivor is not None, (
        "Role-only match must ratify (narrator may cite by role without name)."
    )
    assert survivor.observation_pending is False


def test_gate_match_is_case_insensitive(otel_capture):
    """Name matching must be case-folded so a narrator cite of "father"
    (lowercase) ratifies a pool entry named "Father" (titlecase). This
    parallels the casefold dedup logic in
    ``_auto_mint_prose_only_npcs`` (lines 921-927 of session_helpers.py).
    """
    from sidequest.server.session_helpers import _apply_npc_observation_gate

    snapshot = GameSnapshot()
    snapshot.npc_pool.append(_pending_member(name="Father", role="father", pronouns="he/him"))

    _apply_npc_observation_gate(
        snapshot=snapshot,
        emitted_mentions=[_mention(name="FATHER")],
        turn_num=6,
    )

    survivor = next((m for m in snapshot.npc_pool if m.name == "Father"), None)
    assert survivor is not None, "Case-folded match must ratify"
    assert survivor.observation_pending is False, (
        "ALL-CAPS narrator cite must still match titlecase pool entry."
    )


def test_gate_purges_pending_member_absent_from_mentions(otel_capture):
    """Headline AC: a pending member that does NOT appear in the
    current turn's ``emitted_mentions`` is REMOVED from
    ``snapshot.npc_pool`` entirely. This is the false-positive cleanup
    that motivates the story.
    """
    from sidequest.server.session_helpers import _apply_npc_observation_gate

    snapshot = GameSnapshot()
    snapshot.npc_pool.append(_pending_member(name="Mother", role="mother", pronouns="she/her"))

    _apply_npc_observation_gate(
        snapshot=snapshot,
        emitted_mentions=[_mention(name="Reverend Murchison")],
        turn_num=8,
    )

    assert not any(m.name == "Mother" for m in snapshot.npc_pool), (
        "Mother must be removed from the pool when absent from this "
        "turn's mentions; pool after gate: "
        f"{[m.name for m in snapshot.npc_pool]}"
    )


def test_gate_promote_emits_observation_gate_promoted_span(otel_capture):
    """Per the OTEL Observability Principle, every promote decision MUST
    fire ``npc.observation_gate_promoted`` with the member's name, role,
    and the current turn number. Without the span the GM panel cannot
    verify the gate is engaged.
    """
    from sidequest.server.session_helpers import _apply_npc_observation_gate

    snapshot = GameSnapshot()
    snapshot.npc_pool.append(_pending_member(name="Father", role="father", pronouns="he/him"))

    _apply_npc_observation_gate(
        snapshot=snapshot,
        emitted_mentions=[_mention(name="Father")],
        turn_num=6,
    )

    spans = _spans_named(otel_capture, PROMOTED_SPAN_NAME)
    assert len(spans) == 1, (
        f"Exactly one promote span expected for one promotion; got {len(spans)} "
        f"({[s.name for s in spans]})."
    )
    span = spans[0]
    assert _attr(span, "npc_name") == "Father", (
        f"Span must carry npc_name='Father'; got {_attr(span, 'npc_name')!r}."
    )
    assert _attr(span, "role") == "father", (
        f"Span must carry role='father'; got {_attr(span, 'role')!r}."
    )
    assert _attr(span, "turn_number") == 6, (
        f"Span must carry turn_number=6 (the turn that resolved the gate); got "
        f"{_attr(span, 'turn_number')!r}."
    )


def test_gate_purge_emits_observation_gate_purged_span(otel_capture):
    """Every purge decision MUST fire ``npc.observation_gate_purged``
    with the member's name, role, and turn number. Destructive operations
    without audit trails are exactly the silent-failure mode CLAUDE.md
    prohibits.
    """
    from sidequest.server.session_helpers import _apply_npc_observation_gate

    snapshot = GameSnapshot()
    snapshot.npc_pool.append(_pending_member(name="Mother", role="mother", pronouns="she/her"))

    _apply_npc_observation_gate(
        snapshot=snapshot,
        emitted_mentions=[_mention(name="Reverend Murchison")],
        turn_num=8,
    )

    spans = _spans_named(otel_capture, PURGED_SPAN_NAME)
    assert len(spans) == 1, f"Exactly one purge span expected for one purge; got {len(spans)}."
    span = spans[0]
    assert _attr(span, "npc_name") == "Mother"
    assert _attr(span, "role") == "mother"
    assert _attr(span, "turn_number") == 8


def test_gate_does_not_touch_non_pending_members(otel_capture):
    """Members with ``observation_pending=False`` (world-authored,
    legacy-promoted, or already-ratified) MUST be ignored by the gate
    even if they are absent from this turn's mentions. The gate is
    one-time ratification, not perpetual presence checking. World-NPCs
    persist forever; only freshly-minted entries face the gate.
    """
    from sidequest.server.session_helpers import _apply_npc_observation_gate

    snapshot = GameSnapshot()
    snapshot.npc_pool.append(
        NpcPoolMember(
            name="Hilde",
            role="barkeep",
            pronouns="she/her",
            drawn_from="world_authored",
            observation_pending=False,
        )
    )

    _apply_npc_observation_gate(
        snapshot=snapshot,
        emitted_mentions=[],  # Hilde NOT mentioned
        turn_num=42,
    )

    assert any(m.name == "Hilde" for m in snapshot.npc_pool), (
        "Hilde must survive — non-pending members are exempt from the gate."
    )
    assert not _spans_named(otel_capture, PROMOTED_SPAN_NAME), (
        "Non-pending member must not emit promote span."
    )
    assert not _spans_named(otel_capture, PURGED_SPAN_NAME), (
        "Non-pending member must not emit purge span — durable retention "
        "(memory note feedback_durable_retention) means world-authored "
        "NPCs are forever."
    )


def test_gate_empty_mentions_purges_all_pending(otel_capture):
    """With zero mentions in this turn's narration, every
    ``observation_pending`` member is purged. This is the worst-case
    fixture: narrator delivers a turn with no NPC structure at all, so
    nothing pending survives.
    """
    from sidequest.server.session_helpers import _apply_npc_observation_gate

    snapshot = GameSnapshot()
    snapshot.npc_pool.extend(
        [
            _pending_member(name="Father", role="father", pronouns="he/him"),
            _pending_member(name="Mother", role="mother", pronouns="she/her"),
            _pending_member(name="Dr. Sallow", role=None, pronouns="he/him"),
        ]
    )

    _apply_npc_observation_gate(
        snapshot=snapshot,
        emitted_mentions=[],
        turn_num=10,
    )

    assert snapshot.npc_pool == [], (
        "Empty mentions must purge all pending; pool remaining: "
        f"{[m.name for m in snapshot.npc_pool]}"
    )
    purge_spans = _spans_named(otel_capture, PURGED_SPAN_NAME)
    assert len(purge_spans) == 3, (
        f"Three purges expected (Father, Mother, Dr. Sallow); got {len(purge_spans)}."
    )


def test_gate_empty_pool_is_noop(otel_capture):
    """Calling the gate on an empty pool must be a no-op — no spans, no
    errors. Defensive: the gate fires every turn including ones where
    no auto-mints have ever happened.
    """
    from sidequest.server.session_helpers import _apply_npc_observation_gate

    snapshot = GameSnapshot()

    _apply_npc_observation_gate(
        snapshot=snapshot,
        emitted_mentions=[_mention(name="Reverend Murchison")],
        turn_num=6,
    )

    assert snapshot.npc_pool == []
    assert not _spans_named(otel_capture, PROMOTED_SPAN_NAME)
    assert not _spans_named(otel_capture, PURGED_SPAN_NAME)


def test_gate_mixed_pool_promotes_some_and_purges_others(otel_capture):
    """The gate must apply per-member: a pool with pending Father (in
    mentions) and pending Mother (not in mentions) and non-pending Hilde
    (irrelevant) must emerge with Father ratified, Mother purged, Hilde
    untouched. Tests that the gate iterates and decides independently
    rather than batch-purging or batch-promoting.
    """
    from sidequest.server.session_helpers import _apply_npc_observation_gate

    snapshot = GameSnapshot()
    snapshot.npc_pool.extend(
        [
            _pending_member(name="Father", role="father", pronouns="he/him"),
            _pending_member(name="Mother", role="mother", pronouns="she/her"),
            NpcPoolMember(
                name="Hilde",
                role="barkeep",
                pronouns="she/her",
                drawn_from="world_authored",
                observation_pending=False,
            ),
        ]
    )

    _apply_npc_observation_gate(
        snapshot=snapshot,
        emitted_mentions=[_mention(name="Father")],
        turn_num=6,
    )

    names_remaining = {m.name for m in snapshot.npc_pool}
    assert names_remaining == {"Father", "Hilde"}, (
        f"Father (ratified) and Hilde (untouched) must remain; Mother "
        f"(unmatched pending) must be purged. Got: {names_remaining}."
    )
    father = next(m for m in snapshot.npc_pool if m.name == "Father")
    assert father.observation_pending is False, "Father must be ratified."
    hilde = next(m for m in snapshot.npc_pool if m.name == "Hilde")
    assert hilde.observation_pending is False, "Hilde stays non-pending."

    assert len(_spans_named(otel_capture, PROMOTED_SPAN_NAME)) == 1
    assert len(_spans_named(otel_capture, PURGED_SPAN_NAME)) == 1


# ===========================================================================
# Group E — Multi-turn regression (Glenross scenario from AC)
# ===========================================================================


def test_glenross_two_turn_father_survives_promotes(otel_capture):
    """Canonical AC scenario, part 1: turn 5 auto-mint Father from
    prose; turn 6 narrator emits Father in ``npcs_present``. Father is
    ratified.
    """
    from sidequest.server.session_helpers import (
        _apply_npc_observation_gate,
        _auto_mint_prose_only_npcs,
    )

    snapshot = GameSnapshot()

    # Turn 5 — auto-mint Father
    _auto_mint_prose_only_npcs(
        snapshot=snapshot,
        narration_text="The patient is Father; he is grave.",
        emitted_mentions=[],
        turn_num=5,
    )
    minted = next((m for m in snapshot.npc_pool if m.name == "Father"), None)
    assert minted is not None, "Setup: Father must be auto-minted in turn 5"
    assert minted.observation_pending is True, (
        "Setup: Father must be observation_pending=True post-mint"
    )

    # Turn 6 — narrator emits Father; gate ratifies
    _apply_npc_observation_gate(
        snapshot=snapshot,
        emitted_mentions=[_mention(name="Father")],
        turn_num=6,
    )

    father = next((m for m in snapshot.npc_pool if m.name == "Father"), None)
    assert father is not None, "Father must survive ratification"
    assert father.observation_pending is False, (
        "Father must be ratified (observation_pending=False) after turn 6 mention"
    )
    assert len(_spans_named(otel_capture, PROMOTED_SPAN_NAME)) == 1


def test_glenross_two_turn_mother_dropped_is_purged(otel_capture):
    """Canonical AC scenario, part 2: turn 7 auto-mint Mother from
    prose; turn 8 narrator omits Mother. Mother is purged.

    This is the *exact* false-positive scenario this story exists to
    fix: the 2026-05-11 Glenross playtest turn 6 invented "the wee
    one's mother" with no follow-up; without the gate, Mother would
    have lived in the pool forever as a phantom NPC.
    """
    from sidequest.server.session_helpers import (
        _apply_npc_observation_gate,
        _auto_mint_prose_only_npcs,
    )

    snapshot = GameSnapshot()

    # Turn 7 — auto-mint Mother
    _auto_mint_prose_only_npcs(
        snapshot=snapshot,
        narration_text="The wee one's mother; she is weeping in the kitchen.",
        emitted_mentions=[],
        turn_num=7,
    )
    minted = next((m for m in snapshot.npc_pool if m.name == "Mother"), None)
    assert minted is not None, "Setup: Mother must be auto-minted in turn 7"
    assert minted.observation_pending is True

    # Turn 8 — narrator OMITS Mother; gate purges
    _apply_npc_observation_gate(
        snapshot=snapshot,
        emitted_mentions=[_mention(name="Reverend Murchison")],
        turn_num=8,
    )

    assert not any(m.name == "Mother" for m in snapshot.npc_pool), (
        f"Mother must be purged; pool remaining: {[m.name for m in snapshot.npc_pool]}"
    )
    purges = _spans_named(otel_capture, PURGED_SPAN_NAME)
    assert len(purges) == 1
    assert _attr(purges[0], "npc_name") == "Mother"
    assert _attr(purges[0], "turn_number") == 8


def test_glenross_four_turn_sequence_father_survives_mother_purged(otel_capture):
    """Full AC regression: 4-turn fixture where the same pool sees one
    ratification (Father) and one purge (the constable). Tests that the gate
    runs once per turn and processes each pending member based on its
    own turn-of-mint context — Father auto-minted turn 5, ratified turn
    6; the constable auto-minted turn 7, purged turn 8.

    Turn 6's gate evaluation only resolves turn 5's pending entries
    (Father), not turn 7's mints (which haven't happened yet). Turn 8's
    gate evaluation resolves turn 7's pending entries (the constable).
    """
    from sidequest.server.session_helpers import (
        _apply_npc_observation_gate,
        _auto_mint_prose_only_npcs,
    )

    snapshot = GameSnapshot()

    # Turn 5 — mint Father
    _auto_mint_prose_only_npcs(
        snapshot=snapshot,
        narration_text="The patient is Father; he is grave.",
        emitted_mentions=[],
        turn_num=5,
    )

    # Turn 6 — gate ratifies Father (narrator emits him)
    _apply_npc_observation_gate(
        snapshot=snapshot,
        emitted_mentions=[_mention(name="Father")],
        turn_num=6,
    )

    # Turn 7 — mint the constable (non-gender-paired role, unlike Mother)
    _auto_mint_prose_only_npcs(
        snapshot=snapshot,
        narration_text="The constable arrives at the scene; she is weeping in the kitchen.",
        emitted_mentions=[],
        turn_num=7,
    )

    # Mid-sequence guard: Father ratified, Nurse pending.
    father = next((m for m in snapshot.npc_pool if m.name == "Father"), None)
    constable = next((m for m in snapshot.npc_pool if m.name == "the constable"), None)
    assert father is not None and father.observation_pending is False, (
        "Mid-sequence: Father must already be ratified by turn 7."
    )
    assert constable is not None and constable.observation_pending is True, (
        "Mid-sequence: the constable must be observation_pending after turn 7 mint."
    )

    # Turn 8 — gate purges the constable (narrator omits her). Father (already
    # ratified, non-pending) must be untouched even though he's also
    # absent from turn 8 mentions.
    _apply_npc_observation_gate(
        snapshot=snapshot,
        emitted_mentions=[_mention(name="Reverend Murchison")],
        turn_num=8,
    )

    names_after = {m.name for m in snapshot.npc_pool}
    assert names_after == {"Father", "Reverend Murchison"} or names_after == {"Father"}, (
        f"After turn 8: Father (ratified, persistent) must remain; "
        f"the constable (pending, unmatched) must be purged. Got: {names_after}"
    )
    father_final = next(m for m in snapshot.npc_pool if m.name == "Father")
    assert father_final.observation_pending is False, (
        "Father must remain ratified — gate does not re-evaluate already-ratified members."
    )
    assert not any(m.name == "the constable" for m in snapshot.npc_pool), (
        "the constable must be purged."
    )

    promotes = _spans_named(otel_capture, PROMOTED_SPAN_NAME)
    purges = _spans_named(otel_capture, PURGED_SPAN_NAME)
    assert len(promotes) == 1, f"Exactly one promote (Father, turn 6); got {len(promotes)}."
    assert _attr(promotes[0], "npc_name") == "Father"
    assert _attr(promotes[0], "turn_number") == 6
    assert len(purges) == 1, f"Exactly one purge (the constable, turn 8); got {len(purges)}."
    assert _attr(purges[0], "npc_name") == "the constable"
    assert _attr(purges[0], "turn_number") == 8


def test_apply_narration_result_invokes_observation_gate(otel_capture):
    """Wiring assertion (CLAUDE.md "Every Test Suite Needs a Wiring
    Test"): ``_apply_narration_result_to_snapshot`` must call the gate
    BEFORE auto-mint runs, so the gate evaluates the PRIOR turn's
    pending members against the CURRENT turn's mentions — never the
    current turn's own freshly-minted entries (which would be a degenerate
    self-promotion).

    Fixture: snapshot pre-loaded with a pending Mother from a prior
    turn; current turn's narration mentions Reverend Murchison but
    omits Mother. After apply, Mother must be purged (gate ran).
    """
    from sidequest.server.narration_apply import (
        _apply_narration_result_to_snapshot,
    )

    snapshot = GameSnapshot()
    snapshot.npc_pool.append(_pending_member(name="Mother", role="mother", pronouns="she/her"))

    result = NarrationTurnResult(
        narration="Reverend Murchison waits in the parlour.",
        npcs_present=[
            _mention(name="Reverend Murchison", role="reverend"),
        ],
        is_degraded=False,
    )

    room = room_for(snapshot)
    _apply_narration_result_to_snapshot(
        snapshot,
        result,
        "Ziggy",
        room=room,
        pack=None,
        acting_character_name="Ziggy",
    )

    assert not any(m.name == "Mother" for m in snapshot.npc_pool), (
        "Mother must be purged by the gate run from within "
        "_apply_narration_result_to_snapshot. If she survives, the gate "
        "is not wired into the apply pipeline. Pool after apply: "
        f"{[m.name for m in snapshot.npc_pool]}"
    )
    purges = _spans_named(otel_capture, PURGED_SPAN_NAME)
    assert len(purges) >= 1, (
        f"At least one purge span expected from apply-pipeline run; got {len(purges)}."
    )


def test_apply_narration_result_runs_gate_before_auto_mint(otel_capture):
    """Order-of-operations guard: the gate must run BEFORE
    ``_auto_mint_prose_only_npcs`` so this turn's auto-mints are NOT
    immediately evaluated against this turn's own mentions (which would
    trivially promote everything that the prose-scanner just matched —
    a self-fulfilling no-op gate).

    Fixture: snapshot has a pending Father from a prior turn; current
    turn's narration prose introduces a new role (Mother) but the
    narrator's ``npcs_present`` only lists "Reverend Murchison". After
    apply:
    - The PRIOR-turn Father must be purged (no mention this turn).
    - The CURRENT-turn auto-minted Mother must be observation_pending=True
      (auto-mint ran AFTER the gate, so Mother enters the system fresh —
      she will face the gate NEXT turn, not this one).

    If the gate runs AFTER auto-mint, Mother would be evaluated against
    her own turn's mentions (which omit her), and the gate would purge
    her immediately — the self-cancellation bug.
    """
    from sidequest.server.narration_apply import (
        _apply_narration_result_to_snapshot,
    )

    snapshot = GameSnapshot()
    snapshot.npc_pool.append(_pending_member(name="Father", role="father", pronouns="he/him"))

    result = NarrationTurnResult(
        narration=(
            "Reverend Murchison waits in the parlour. The wee one's mother "
            "is weeping in the kitchen; she has not spoken since the bee swarm."
        ),
        npcs_present=[
            _mention(name="Reverend Murchison", role="reverend"),
        ],
        is_degraded=False,
    )

    room = room_for(snapshot)
    _apply_narration_result_to_snapshot(
        snapshot,
        result,
        "Ziggy",
        room=room,
        pack=None,
        acting_character_name="Ziggy",
    )

    # Father (prior-turn pending, no mention this turn) → purged
    assert not any(m.name == "Father" for m in snapshot.npc_pool), (
        "Father must be purged by the gate; absence indicates correct order."
    )
    # Mother (this-turn auto-mint) must EXIST and be observation_pending=True.
    # If she's missing → gate ran AFTER auto-mint and purged her. If she's
    # present with observation_pending=False → gate ran AFTER auto-mint and
    # auto-promoted her against the same turn's prose.
    mother = next((m for m in snapshot.npc_pool if m.name == "Mother"), None)
    assert mother is not None, (
        "Mother must be auto-minted this turn; if missing, the gate ran "
        "AFTER auto-mint and purged her — wrong order."
    )
    assert mother.observation_pending is True, (
        "Mother must enter the pool as observation_pending=True; got "
        f"{mother.observation_pending!r}. If False, either auto-mint sets "
        "the wrong default OR the gate ran AFTER auto-mint and self-ratified her."
    )


@pytest.mark.parametrize(
    "drawn_from",
    [
        "world_authored",
        "narrator_invented",
        "name_generator",
        "legacy_registry",
    ],
)
def test_gate_ignores_non_dialogue_extraction_when_non_pending(otel_capture, drawn_from):
    """Defensive: pool members from sources OTHER than
    ``dialogue_extraction`` (world_authored, narrator_invented from the
    structured patch, name_generator, legacy_registry) default to
    ``observation_pending=False`` and must survive the gate regardless
    of mention status. This protects durable retention for every
    non-prose mint provenance.

    Note: this story does NOT auto-flag those provenances as pending;
    the gate is scoped to fresh prose-only mints. If a future story
    extends the gate to other provenances it can override this test.
    """
    from sidequest.server.session_helpers import _apply_npc_observation_gate

    snapshot = GameSnapshot()
    snapshot.npc_pool.append(
        NpcPoolMember(
            name="Captain Yseult",
            role="captain",
            pronouns="she/her",
            drawn_from=drawn_from,
            observation_pending=False,
        )
    )

    _apply_npc_observation_gate(
        snapshot=snapshot,
        emitted_mentions=[],  # Captain not mentioned
        turn_num=42,
    )

    assert any(m.name == "Captain Yseult" for m in snapshot.npc_pool), (
        f"Pool member from drawn_from={drawn_from!r} must survive — "
        "observation_pending=False is the immunity flag."
    )
    assert not _spans_named(otel_capture, PURGED_SPAN_NAME), (
        f"No purge span must fire for non-pending {drawn_from!r} member."
    )
