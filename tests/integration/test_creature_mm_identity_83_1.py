"""RED-phase contract for story 83-1 — creature pool-member promotion draws
a Monster Manual bestiary identity (ADR-059) instead of the person-shaped
placeholder (HpPool 10/10, no creature_id/threat_level/abilities/morale).

Epic 83 (NPC Creature Identity): when the narrator classifies a mention as
``is_creature=True`` (ping-pong #74), a novel pool member is minted correctly
(no culture, no person-name) — but when that member engages mechanically and
is promoted by ``_promote_pool_member_to_npc`` / ``resolve_status_target``,
it still receives the SAME person-shaped placeholder every human NPC gets:
``HpPool(10, 10, 10)``, ``creature_id=None``, ``threat_level=None``,
``abilities=[]``, ``morale=None``.

The Monster Manual generator already exists: ``_creature_patch_from_enemy``
in ``dispatch/monster_manual_inject.py`` builds an ``NpcPatch`` with all
those fields.  The wire from a creature-typed pool promotion → MM generation
does NOT exist.  This file pins the *contract* that wire must satisfy.

== ACs this test suite covers ==

AC-1  creature promotion sets creature_id / threat_level / abilities / morale
      and draws HP from the bestiary, NOT the 10/10 placeholder.
AC-2  creature has NO person-shaped identity (no OCEAN profile seeded).
AC-3  a new OTEL span fires recording the bestiary draw (creature_id,
      threat_level, hp, source) so the GM panel can distinguish "creature
      drew MM identity" from "person placeholder".
AC-4  persons (is_creature=False) are completely unaffected — regression guard.
AC-5  wiring test: drive a creature mention through ``_apply_npc_mentions``
      (pool-mint seam) → ``resolve_status_target`` (promotion seam) and
      assert the resulting Npc carries creature fields reachable from
      production code paths.
AC-6  creature_id is non-None and deterministically stable so that 83-3
      (threat reconciliation) can key on it.

== Seam map (for Dev) ==

  ``_apply_npc_mentions`` (narration_apply.py:1564)
      Step-3 novel branch, line 1913: mints NpcPoolMember(is_creature=True,
      drawn_from="narrator_invented") — correct, no culture. The MM draw
      does NOT happen here.

  ``_promote_pool_member_to_npc`` (narration_apply.py:1057)
      ALWAYS builds HpPool(10, 10, 10) and sets NO creature fields even when
      member.is_creature is True. This is the gap: a creature member must be
      routed to the MM generator instead of the person-placeholder path.

  ``resolve_status_target`` (narration_apply.py:1199)
      Only call site of ``_promote_pool_member_to_npc``; has ``snapshot`` in
      scope. Does NOT currently have ``MonsterManual`` access — Dev must
      thread it through (or embed creature data in ``NpcPoolMember`` at
      pool-mint time, or another mechanism).

  ``_creature_patch_from_enemy`` (dispatch/monster_manual_inject.py:231)
      Existing helper that maps an encountergen ``enemies[i]`` row into an
      ``NpcPatch`` with creature fields.  Dev should reuse, not reimplement.

  ``_seed_invented_npc_identity`` (narration_apply.py:1115)
      Must add an ``is_creature`` guard so it does NOT seed OCEAN / scenario
      roles onto creature pool members (AC-2). Currently only guards on
      ``drawn_from != "narrator_invented"`` — a creature from that lineage
      would incorrectly receive an OCEAN profile today.

== Test doctrine ==

All tests drive the *real* promotion seam (``resolve_status_target``) on a
synthetic ``GameSnapshot``.  No source-text wiring tests (server CLAUDE.md).
OTEL tests use the same ``WatcherSpanProcessor`` harness as story 72-5.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from opentelemetry.sdk.trace import TracerProvider

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot, Npc
from sidequest.server.narration_apply import (
    _apply_npc_mentions,  # noqa: PLC2701 — driving the real seam
    resolve_status_target,
)
from sidequest.server.watcher import WatcherSpanProcessor
from sidequest.telemetry import spans as spans_module
from sidequest.telemetry.watcher_hub import watcher_hub

# ---------------------------------------------------------------------------
# Span name this story must introduce — a lie-detector for the bestiary draw.
# Dev registers this constant in telemetry/spans/npc.py (or a new module).
# The test asserts by matching the OTEL ``field`` key in watcher events, so
# the exact span string is the contract.
# ---------------------------------------------------------------------------
SPAN_CREATURE_BESTIARY_DRAW_FIELD = "npc.creature_bestiary_draw"

# A synthetic encountergen enemy dict — the shape ``_creature_patch_from_enemy``
# consumes.  Dev's creature-data carrier (NpcPoolMember field, MM lookup, etc.)
# must produce an equivalent result.
_ENEMY_STUB: dict = {
    "name": "Forest Lion",
    "creature_id": "forest_lion",
    "threat_level": 2,
    "hp": 18,
    "abilities": ["Pounce — knocks target prone", "Rake — 2d4 claw damage"],
    "morale": "bold",
    "role": "ambush predator",
}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


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


def _creature_member(name: str = "Forest Lion") -> NpcPoolMember:
    """Minimal creature pool member — the shape the production mention-mint
    path leaves behind after ``_apply_npc_mentions`` processes a creature
    ``NpcMention``.  No creature data blob yet; that's what the story wires."""
    return NpcPoolMember(
        name=name,
        drawn_from="narrator_invented",
        is_creature=True,
    )


def _creature_snapshot(*, npc_name: str = "Forest Lion") -> GameSnapshot:
    """Snapshot with exactly one creature pool member awaiting promotion."""
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Brunt")],
        npc_pool=[_creature_member(npc_name)],
    )


def _person_snapshot(*, npc_name: str = "Mira the Innkeeper") -> GameSnapshot:
    """Snapshot with a human (non-creature) pool member — regression guard."""
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Brunt")],
        npc_pool=[
            NpcPoolMember(
                name=npc_name,
                drawn_from="narrator_invented",
                is_creature=False,
            )
        ],
    )


async def _setup_otel(monkeypatch: pytest.MonkeyPatch, label: str) -> list[dict]:
    """Wire a WatcherSpanProcessor that captures watcher events; return the
    capture list.  Mirrors the harness used in test_npc_spawn_disposition_otel.
    """
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


async def _wait_for_event(
    captured: list[dict], field_value: str, *, timeout_s: float = 1.0
) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        for evt in captured:
            if (
                evt.get("event_type") == "state_transition"
                and evt.get("fields", {}).get("field") == field_value
            ):
                return evt
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"Expected state_transition with field={field_value!r} within {timeout_s}s; "
        f"captured: {[(e.get('event_type'), e.get('fields', {}).get('field')) for e in captured]}"
    )


# ---------------------------------------------------------------------------
# Tripwire — Npc model already carries creature fields (ADR-059, session.py).
# This PASSES today; it is a safety-net ensuring the model is ready for Dev.
# ---------------------------------------------------------------------------


def test_npc_model_has_creature_id_field() -> None:
    """``Npc`` must expose a ``creature_id`` field per ADR-059 (already live)."""
    assert "creature_id" in Npc.model_fields, (
        "Npc is missing the `creature_id` field — ADR-059 requires it."
    )


def test_npc_model_has_threat_level_field() -> None:
    assert "threat_level" in Npc.model_fields


def test_npc_model_has_abilities_field() -> None:
    assert "abilities" in Npc.model_fields


def test_npc_model_has_morale_field() -> None:
    assert "morale" in Npc.model_fields


# ---------------------------------------------------------------------------
# AC-1a — creature_id is populated on the promoted Npc (FAILS: always None).
# ---------------------------------------------------------------------------


def test_creature_promotion_sets_creature_id() -> None:
    """AC-1: when a creature-classified pool member is promoted via
    ``resolve_status_target``, the resulting ``Npc`` must carry a non-None
    ``creature_id`` drawn from the Monster Manual bestiary.

    FAILS until ``_promote_pool_member_to_npc`` (or its call site) routes
    creature members through ``_creature_patch_from_enemy`` / MM generation.
    """
    snapshot = _creature_snapshot()
    promoted = resolve_status_target(
        snapshot,
        actor_name="Forest Lion",
        turn_num=1,
        trigger="status_change",
    )

    assert promoted is not None, "resolve_status_target must promote the creature pool member"
    assert promoted.creature_id is not None, (
        "creature Npc must have a creature_id drawn from the Monster Manual bestiary; "
        "currently None because _promote_pool_member_to_npc uses the person-placeholder path "
        "regardless of is_creature. Wire _creature_patch_from_enemy (83-1)."
    )


# ---------------------------------------------------------------------------
# AC-1b — threat_level is populated (FAILS: always None).
# ---------------------------------------------------------------------------


def test_creature_promotion_sets_threat_level() -> None:
    """AC-1: the promoted creature Npc must carry a ``threat_level`` (1-4).

    FAILS until the MM wire lands: ``_promote_pool_member_to_npc`` always
    leaves ``threat_level=None`` for pool-promoted Npcs.
    """
    snapshot = _creature_snapshot()
    promoted = resolve_status_target(
        snapshot,
        actor_name="Forest Lion",
        turn_num=1,
        trigger="status_change",
    )

    assert promoted is not None
    assert promoted.threat_level is not None, (
        "creature Npc must have a threat_level from the MM; currently None."
    )
    assert isinstance(promoted.threat_level, int), "threat_level must be an int (B/X tier 1-4)"
    assert 1 <= promoted.threat_level <= 4, (
        f"threat_level={promoted.threat_level!r} is outside the B/X 1-4 band"
    )


# ---------------------------------------------------------------------------
# AC-1c — abilities are populated (FAILS: always empty list).
# ---------------------------------------------------------------------------


def test_creature_promotion_sets_abilities() -> None:
    """AC-1: the promoted creature Npc must carry abilities from the bestiary.

    FAILS: ``_promote_pool_member_to_npc`` builds ``Npc(core=...)`` with the
    model-default ``abilities=[]`` and no follow-up patch.
    """
    snapshot = _creature_snapshot()
    promoted = resolve_status_target(
        snapshot,
        actor_name="Forest Lion",
        turn_num=1,
        trigger="status_change",
    )

    assert promoted is not None
    assert promoted.abilities, (
        "creature Npc must have at least one ability from the MM; got empty list. "
        "Wire _creature_patch_from_enemy so abilities land on the promoted Npc."
    )


# ---------------------------------------------------------------------------
# AC-1d — morale is populated (FAILS: always None).
# ---------------------------------------------------------------------------


def test_creature_promotion_sets_morale() -> None:
    """AC-1: the promoted creature Npc must carry a morale descriptor.

    FAILS: ``_promote_pool_member_to_npc`` never sets morale on pool-promoted
    Npcs.
    """
    snapshot = _creature_snapshot()
    promoted = resolve_status_target(
        snapshot,
        actor_name="Forest Lion",
        turn_num=1,
        trigger="status_change",
    )

    assert promoted is not None
    assert promoted.morale is not None, (
        "creature Npc must have a morale field from the MM; currently None."
    )
    assert isinstance(promoted.morale, str) and promoted.morale.strip(), (
        "morale must be a non-blank string (e.g. 'bold', 'cowardly', 'steady')"
    )


# ---------------------------------------------------------------------------
# AC-1e — HP is NOT the person placeholder 10/10 (FAILS: always 10/10).
# ---------------------------------------------------------------------------


def test_creature_promotion_hp_not_person_placeholder() -> None:
    """AC-1: the promoted creature Npc must have HP drawn from the bestiary,
    NOT the ``HpPool(current=10, max=10, base_max=10)`` person placeholder that
    ``_promote_pool_member_to_npc`` unconditionally builds today.

    The placeholder is a hardcoded lie: a Forest Lion has nothing to do with
    10 HP.  After the MM wire lands, a tier-2 creature should draw its HP from
    the encountergen data (e.g. 18 HP for a Forest Lion).

    NOTE: the assertion checks ``hp.max != 10`` to distinguish bestiary HP from
    the placeholder.  A genuine bestiary creature might coincidentally have
    max=10; if so the assertion may need loosening — but for RED phase this is
    the correct discriminator.
    """
    snapshot = _creature_snapshot()
    promoted = resolve_status_target(
        snapshot,
        actor_name="Forest Lion",
        turn_num=1,
        trigger="status_change",
    )

    assert promoted is not None
    hp_pool = promoted.core.hp
    assert hp_pool is not None, "creature Npc must have an HpPool"
    assert hp_pool.max != 10 or hp_pool.current != 10, (
        f"creature Npc has the person-placeholder HP 10/10 — bestiary HP must be used instead. "
        f"Wire _creature_patch_from_enemy so HP comes from the MM, not the hardcoded placeholder. "
        f"hp.current={hp_pool.current} hp.max={hp_pool.max}"
    )


# ---------------------------------------------------------------------------
# AC-2 — NO person-shaped identity: creature must not get OCEAN profile.
# ---------------------------------------------------------------------------


def test_creature_promotion_no_ocean_profile() -> None:
    """AC-2: a creature pool member promoted through ``resolve_status_target``
    must NOT receive an OCEAN personality profile.

    Today ``_seed_invented_npc_identity`` only guards on
    ``drawn_from != "narrator_invented"`` — it does NOT guard on
    ``member.is_creature``.  A creature with
    ``drawn_from="narrator_invented"`` (the mint-path default) therefore
    incorrectly receives an OCEAN profile:  a Forest Lion has no Big-Five
    personality, it has a stat block.

    FAILS until ``_seed_invented_npc_identity`` adds an ``is_creature`` guard
    (or the promotion seam skips that function for creatures entirely).
    """
    snapshot = _creature_snapshot()
    promoted = resolve_status_target(
        snapshot,
        actor_name="Forest Lion",
        turn_num=1,
        trigger="status_change",
    )

    assert promoted is not None
    assert promoted.ocean is None, (
        "creature Npc must NOT have an OCEAN profile seeded — creatures have stat blocks, "
        "not Big-Five personalities. _seed_invented_npc_identity must guard on is_creature."
    )


# ---------------------------------------------------------------------------
# AC-3 — OTEL: a creature-routing span fires recording the bestiary draw.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_creature_routing_otel_span_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-3: when a creature pool member is promoted, a watcher event fires
    with ``field="npc.creature_bestiary_draw"`` recording at minimum
    ``creature_id`` and ``threat_level`` so the GM panel can prove the engine
    drew from the MM rather than improvising a placeholder.

    FAILS until:
      (a) a new SPAN_CREATURE_BESTIARY_DRAW constant + SpanRoute are registered
          in ``telemetry/spans/``, AND
      (b) ``_promote_pool_member_to_npc`` (or its call site) emits the span when
          the creature-branch fires.

    The field key ``"npc.creature_bestiary_draw"`` is the contract string that
    the implementation must honour.
    """
    captured = await _setup_otel(monkeypatch, "test-creature-routing-span")
    snapshot = _creature_snapshot()

    resolve_status_target(
        snapshot,
        actor_name="Forest Lion",
        turn_num=2,
        trigger="status_change",
    )
    await asyncio.sleep(0)  # let WatcherSpanProcessor flush

    evt = await _wait_for_event(
        captured,
        SPAN_CREATURE_BESTIARY_DRAW_FIELD,
        timeout_s=1.0,
    )

    fields = evt.get("fields", {})
    assert "creature_id" in fields, (
        f"bestiary-draw span must record creature_id; got fields={fields!r}"
    )
    assert "threat_level" in fields, (
        f"bestiary-draw span must record threat_level; got fields={fields!r}"
    )
    assert "hp" in fields, (
        f"bestiary-draw span must record hp; got fields={fields!r}"
    )
    assert "source" in fields, (
        f"bestiary-draw span must record source (e.g. 'mm'); got fields={fields!r}"
    )
    assert fields.get("source") == "mm", (
        f"source must be 'mm' to distinguish MM draw from person-placeholder; "
        f"got source={fields.get('source')!r}"
    )


# ---------------------------------------------------------------------------
# AC-3b — spawn_disposition span is_creature=True for creature promotions.
# (The existing span already fires; this locks the is_creature=True invariant.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_disposition_span_is_creature_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3b: the existing ``npc.spawn_disposition`` span must report
    ``is_creature=True`` for a creature pool-member promotion.

    This already works (ping-pong #74 wired ``member.is_creature``), so this
    test functions as a regression guard.  A creature must ALSO be hostile
    (disposition=-20) when drawn from the MM.

    PASSES once AC-1 (creature_id set) is live, because the
    ``is_creature`` logic in ``_npc_from_patch`` triggers on creature fields.
    For now it FAILS because the promotion path sets ``is_creature=False`` on
    the span (the span reads from the Npc's materialized state, not the pool
    member flag).

    Wait — let me re-check.  ``_promote_pool_member_to_npc`` at line 1099
    explicitly passes ``is_creature=member.is_creature`` to the span.  So this
    span DOES fire with ``is_creature=True`` today.  This test locks that.
    """
    captured = await _setup_otel(monkeypatch, "test-spawn-disp-creature")
    snapshot = _creature_snapshot()

    resolve_status_target(
        snapshot,
        actor_name="Forest Lion",
        turn_num=3,
        trigger="test",
    )
    await asyncio.sleep(0)

    evt = await _wait_for_event(captured, "npc.spawn_disposition", timeout_s=1.0)
    fields = evt.get("fields", {})
    assert fields.get("is_creature") is True, (
        f"spawn_disposition span must report is_creature=True for a creature pool member; "
        f"got is_creature={fields.get('is_creature')!r}"
    )


# ---------------------------------------------------------------------------
# AC-4 — Persons are unaffected: regression guard.
# ---------------------------------------------------------------------------


def test_person_promotion_creature_id_stays_none() -> None:
    """AC-4: a non-creature pool member (is_creature=False) must still promote
    through the existing person path — creature_id must stay None.

    PASSES today (no change to the person path is expected).  Locked here to
    catch regressions if the creature branch accidentally bleeds into persons.
    """
    snapshot = _person_snapshot()
    promoted = resolve_status_target(
        snapshot,
        actor_name="Mira the Innkeeper",
        turn_num=1,
        trigger="test",
    )

    assert promoted is not None
    assert promoted.creature_id is None, (
        "person Npc must NOT have a creature_id; the creature-identity path "
        "must be guarded by is_creature."
    )


def test_person_promotion_hp_stays_placeholder() -> None:
    """AC-4: a person pool member still gets the 10/10 placeholder after the
    creature-wire lands.  Regression guard — persons must be unaffected.

    PASSES today.
    """
    snapshot = _person_snapshot()
    promoted = resolve_status_target(
        snapshot,
        actor_name="Mira the Innkeeper",
        turn_num=1,
        trigger="test",
    )

    assert promoted is not None
    hp_pool = promoted.core.hp
    assert hp_pool is not None
    # The person placeholder is intentional for persons; lock it.
    assert hp_pool.max == 10 and hp_pool.current == 10, (
        "person pool member must still get the 10/10 HP placeholder after the "
        "creature-wire lands — the creature path must be strictly guarded."
    )


def test_person_promotion_gets_ocean() -> None:
    """AC-4: a narrator-invented person (is_creature=False) still gets OCEAN
    seeded.  Regression guard — the is_creature guard in
    _seed_invented_npc_identity must not block persons.

    PASSES today.
    """
    snapshot = _person_snapshot()
    promoted = resolve_status_target(
        snapshot,
        actor_name="Mira the Innkeeper",
        turn_num=1,
        trigger="test",
    )

    assert promoted is not None
    assert promoted.ocean is not None, (
        "narrator-invented PERSON must still receive an OCEAN profile — the "
        "is_creature guard in _seed_invented_npc_identity must only block "
        "creature members."
    )


# ---------------------------------------------------------------------------
# AC-5 — Wiring test: creature mention → pool → promote → MM identity.
# ---------------------------------------------------------------------------


def test_creature_mention_mints_creature_typed_pool_member() -> None:
    """AC-5 (first half): driving ``_apply_npc_mentions`` with a creature
    ``NpcMention`` mints a pool member with ``is_creature=True``.

    This half PASSES today (ping-pong #74 already lands is_creature on the
    pool member).  Locking it as the entry-gate for the wiring test.
    """
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Hero")],
    )

    mention = NpcMention(name="The Cave Bear", is_creature=True, is_new=True)
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[mention],
        turn_num=5,
    )

    pool_member = next(
        (m for m in snapshot.npc_pool if m.name == "The Cave Bear"),
        None,
    )
    assert pool_member is not None, (
        "_apply_npc_mentions must mint a pool member for a novel creature mention"
    )
    assert pool_member.is_creature is True, (
        "pool member minted from a creature NpcMention must have is_creature=True"
    )


def test_creature_mention_to_promotion_carries_mm_identity() -> None:
    """AC-5 (full wiring): drive ``_apply_npc_mentions`` (creature mention →
    pool member) → ``resolve_status_target`` (pool member → Npc) and assert
    the resulting ``Npc`` carries MM creature fields, not the person placeholder.

    FAILS until the wire from creature-typed pool promotion → MM generation
    lands: currently the Npc emerges with creature_id=None.
    """
    snapshot = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        characters=[_make_pc("Hero")],
    )

    # Step 1: narrator mentions a creature name — this should hit the novel
    # branch and mint a creature-typed pool member.
    mention = NpcMention(name="The Cave Bear", is_creature=True, is_new=True)
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[mention],
        turn_num=5,
    )

    # Confirm pool member exists with correct creature flag.
    pool_member = next(
        (m for m in snapshot.npc_pool if m.name == "The Cave Bear"),
        None,
    )
    assert pool_member is not None, "pool member must be minted by _apply_npc_mentions"
    assert pool_member.is_creature is True

    # Step 2: the creature engages mechanically — promote it.
    promoted = resolve_status_target(
        snapshot,
        actor_name="The Cave Bear",
        turn_num=6,
        trigger="status_change",
    )

    assert promoted is not None, "resolve_status_target must promote the creature from the pool"
    assert promoted.creature_id is not None, (
        "end-to-end wire FAILED: Npc promoted from a creature pool member must "
        "carry a creature_id from the MM bestiary.  Currently None because "
        "_promote_pool_member_to_npc doesn't call the MM generator.  "
        "Wire _creature_patch_from_enemy at the creature-promotion seam (83-1)."
    )
    assert promoted.threat_level is not None, (
        "Npc must carry a threat_level from the MM bestiary (not None)."
    )
    # HP must not be the person placeholder.
    assert promoted.core.hp.max != 10 or promoted.core.hp.current != 10, (
        "Npc must have HP from the MM bestiary, not the 10/10 person placeholder."
    )


# ---------------------------------------------------------------------------
# AC-6 — creature_id is stable: promote the same member twice → same id.
# ---------------------------------------------------------------------------


def test_creature_id_stable_across_promotions() -> None:
    """AC-6: promoting the same pool member (same name, same bestiary entry)
    must produce the same ``creature_id`` both times — 83-3 (recurring-threat
    reconciliation) keys on this identity.

    Two separate snapshots each containing the same pool member are promoted;
    both results must share the same non-None ``creature_id``.

    FAILS today because creature_id is always None (None == None would make
    the equality assertion trivially pass, but the preceding non-None assertion
    catches the real failure first).
    """
    snap_a = _creature_snapshot()
    snap_b = _creature_snapshot()

    promoted_a = resolve_status_target(
        snap_a, actor_name="Forest Lion", turn_num=1, trigger="combat"
    )
    promoted_b = resolve_status_target(
        snap_b, actor_name="Forest Lion", turn_num=1, trigger="combat"
    )

    assert promoted_a is not None
    assert promoted_b is not None

    # creature_id must be set on both (AC-1 prerequisite).
    assert promoted_a.creature_id is not None, (
        "creature_id must be non-None on first promotion (AC-1 prerequisite for AC-6)"
    )
    assert promoted_b.creature_id is not None, (
        "creature_id must be non-None on second promotion"
    )

    # And the same species must produce the same stable id.
    assert promoted_a.creature_id == promoted_b.creature_id, (
        f"creature_id must be deterministically stable across promotions of the same "
        f"species/pool-member: first={promoted_a.creature_id!r} second={promoted_b.creature_id!r}. "
        f"83-3 (threat reconciliation) requires this key to be stable."
    )
