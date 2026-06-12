"""RED tests for Story 72-2 leg 1 — disposition survives pool→Npc promotion.

Epic 72 (NPC Identity Hardening) DEEP-DIVE #1: an NPC is modelled twice with
no consistency invariant — ``snapshot.npc_pool`` (identity scaffold, *no
mechanical state today*) and ``snapshot.npcs`` (mechanical state, carries
``disposition``). ``_promote_pool_member_to_npc``
(``sidequest/server/narration_apply.py``) builds a fresh ``Npc`` with **no
``disposition=`` argument**, so a logical person with a known non-neutral
disposition silently re-promotes to neutral-0. A bartender the table spent ten
turns befriending re-promotes as a stranger.

This story makes promotion **disposition-preserving**. Per the story context
(``context-story-72-2.md``), *where* the disposition is carried from is a
design decision for the implementer; these tests pin the **round-trip**
(known-disposition in → same disposition out) and the **OTEL wiring** (the
``promoted_from_pool`` watcher event carries the preserved value), which are
the refactor-stable contracts regardless of mechanism.

Mechanism under test here: a ``disposition`` field on ``NpcPoolMember`` (the
"scaffold-side value" the context-story sanctions, and the session-file AC1).
See the TEA deviation log in ``.session/72-2-session.md`` — if Dev carries the
disposition from a different source (e.g. a same-name ``Npc``), the fixture
wiring of how the value reaches the member changes but the round-trip and
event assertions stand.

Sibling wiring test for the spawn-disposition span (72-5) lives in
``test_npc_spawn_disposition_otel.py``; the no-prior-disposition regression
guard below keeps that neutral-default behaviour intact.
"""

from __future__ import annotations

import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool
from sidequest.game.disposition import Attitude
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot
from sidequest.server.narration_apply import resolve_status_target
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub


def _make_pc(name: str) -> Character:
    return Character(
        core=CreatureCore(
            name=name,
            description="x",
            personality="x",
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        char_class="Fighter",
        race="Human",
        backstory=f"{name} test",
    )


# ---------------------------------------------------------------------------
# AC1 — disposition field exists on the scaffold (reflection tripwire).
#
# Sanctioned reflection-based type check (CLAUDE.md "No Source-Text Wiring
# Tests" exception): interrogates the runtime model, not source text.
# ---------------------------------------------------------------------------


def test_npc_pool_member_carries_disposition_field() -> None:
    """``NpcPoolMember`` must expose a ``disposition`` field so a known
    relationship can be carried through promotion. RED until Dev adds it
    (the model is ``extra='forbid'`` today with no disposition field)."""
    assert "disposition" in NpcPoolMember.model_fields, (
        "NpcPoolMember has no `disposition` field; promotion cannot preserve "
        "a known disposition. Add `disposition` to the scaffold (default "
        "neutral) per Story 72-2 AC1."
    )


def test_disposition_field_defaults_neutral() -> None:
    """A scaffold minted with no explicit disposition is neutral-0 — adding
    the field must NOT regress the narrator-invented / legacy-member path
    (72-5: invented people are born neutral, never hostile)."""
    member = NpcPoolMember(name="Wexley", drawn_from="narrator_invented")
    # Disposition coerces to a bare int; neutral is 0.
    assert int(member.disposition) == 0
    assert member.disposition.attitude() == Attitude.NEUTRAL


# ---------------------------------------------------------------------------
# AC1 — round-trip: known disposition in → same disposition out.
# ---------------------------------------------------------------------------


def test_promotion_preserves_nonneutral_disposition() -> None:
    """A pool member with a known friendly disposition (+18) promotes to an
    ``Npc`` that keeps that exact value and attitude. RED today: the member
    cannot even be constructed with a disposition (no field), and the
    promotion drops to neutral-0 regardless."""
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Hero")],
        npc_pool=[
            NpcPoolMember(name="Mara", drawn_from="world_authored", disposition=18),
        ],
    )

    promoted = resolve_status_target(
        snapshot, actor_name="Mara", turn_num=4, trigger="test"
    )

    assert promoted is not None
    assert int(promoted.disposition) == 18, (
        "promotion flattened a known friendly disposition back to neutral"
    )
    assert promoted.disposition.attitude() == Attitude.FRIENDLY


def test_promotion_preserves_hostile_disposition() -> None:
    """Negative-side round-trip: a known hostile disposition (-22) survives
    promotion too — preservation is value-faithful, not just sign-faithful."""
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Hero")],
        npc_pool=[
            NpcPoolMember(name="Grish", drawn_from="world_authored", disposition=-22),
        ],
    )

    promoted = resolve_status_target(
        snapshot, actor_name="Grish", turn_num=4, trigger="test"
    )

    assert promoted is not None
    assert int(promoted.disposition) == -22
    assert promoted.disposition.attitude() == Attitude.HOSTILE


def test_promotion_with_no_prior_disposition_stays_neutral() -> None:
    """Regression guard (72-5): a member carrying the default disposition
    still promotes neutral-0. Preservation must not drag a no-prior-value
    member off neutral. This pins the boundary the AC1 change must not
    break."""
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Hero")],
        npc_pool=[NpcPoolMember(name="Wexley", drawn_from="narrator_invented")],
    )

    promoted = resolve_status_target(
        snapshot, actor_name="Wexley", turn_num=4, trigger="test"
    )

    assert promoted is not None
    assert int(promoted.disposition) == 0
    assert promoted.disposition.attitude() == Attitude.NEUTRAL


# ---------------------------------------------------------------------------
# AC5 (leg 1) — OTEL wiring: the promoted_from_pool watcher event carries the
# preserved disposition so the GM panel can see the value rode through
# promotion rather than being reset. Drives the REAL seam
# (resolve_status_target → _promote_pool_member_to_npc) and asserts the
# routed watcher event, not source text.
# ---------------------------------------------------------------------------


async def _setup_watcher(monkeypatch: pytest.MonkeyPatch, label: str) -> list[dict]:
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001

    captured: list[dict] = []

    class _Sock:
        async def send_json(self, data: dict) -> None:
            captured.append(data)

    await watcher_hub.subscribe(_Sock())  # type: ignore[arg-type]

    provider = TracerProvider()
    provider.add_span_processor(WatcherSpanProcessor(watcher_hub))
    local_tracer = provider.get_tracer(label)
    monkeypatch.setattr(spans_module, "tracer", lambda: local_tracer)

    return captured


async def _wait_for_op(
    captured: list[dict], op: str, *, timeout_s: float = 1.0
) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        for evt in captured:
            if (
                evt.get("event_type") == "state_transition"
                and evt.get("fields", {}).get("op") == op
            ):
                return evt
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"Expected state_transition with op={op!r} within {timeout_s}s; "
        f"captured ops: "
        f"{[e.get('fields', {}).get('op') for e in captured]}"
    )


@pytest.mark.asyncio
async def test_promoted_from_pool_event_carries_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``promoted_from_pool`` watcher event must carry the preserved
    disposition (and its attitude band) so the GM panel can verify the value
    survived promotion. RED today: the event payload carries name / pool_origin
    / drawn_from / trigger / turn but NO disposition attribute."""
    captured = await _setup_watcher(monkeypatch, "test-72-2-promote-disposition")

    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Hero")],
        npc_pool=[
            NpcPoolMember(name="Mara", drawn_from="world_authored", disposition=18),
        ],
    )

    promoted = resolve_status_target(
        snapshot, actor_name="Mara", turn_num=7, trigger="befriend"
    )
    await asyncio.sleep(0)

    assert promoted is not None
    assert int(promoted.disposition) == 18

    evt = await _wait_for_op(captured, "promoted_from_pool")
    assert evt["fields"]["name"] == "Mara"
    assert evt["fields"]["disposition"] == 18, (
        "promoted_from_pool event must carry the preserved disposition value; "
        f"got fields={evt['fields']!r}"
    )
    assert evt["fields"]["attitude"] == "friendly", (
        "promoted_from_pool event must carry the preserved attitude band"
    )
