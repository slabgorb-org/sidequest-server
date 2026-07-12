"""ADR-156 §6 (Amendment B) — attach-before-mint in the narrator-mention paths.

Both narrator-mint feeders (`narration_apply._apply_npc_mentions`'s Step-3
novel branch; `session_helpers._auto_mint_prose_only_npcs`'s prose-extraction
mint) share one preamble contract: *resolve -> attach -> only then mint*
(`narration_apply._attach_before_mint`). Two attach legs:

  1. ``resolve_roster_npc`` (canonical -> alias -> invented_from) against
     ``snapshot.npcs`` — a hit attaches the incoming name as a fresh alias
     instead of minting a twin pool member.
  2. The "Ihnsch" case (2026-07-10 Chico trace, ADR-156 design doc §3.4): an
     active, unresolved ``snapshot.encounter`` with EXACTLY ONE live
     (non-withdrawn) ``side="opponent"`` actor whose resolved identity
     carries an EMPTY alias ledger and a GENERIC or NARRATOR_INVENTED origin
     is a lone, still-unnamed Other. A HOSTILE mention is that Other's first
     prose name and attaches instead of minting a phantom twin beside it.
     Two live opponents is ambiguous — never guess (No Silent Fallbacks) —
     so it falls through to mint like any other novel name.

WHAT THIS SUITE PINS:
  * test 1 — a prose-extracted honorific name that already resolves to a
    roster identity via a leg the prose-extraction feeder's own dedup
    (bare ``.casefold()`` exact-name set) misses — the alias ledger —
    attaches instead of minting a twin (RED pre-implementation: the
    prose-extraction feeder had no resolver call at all).
  * test 2 — the Ihnsch case itself: a hostile mention against a lone,
    unaliased GENERIC-origin seated Other attaches, no twin (RED
    pre-implementation).
  * test 3 — a BYSTANDER mention (non-hostile) with a seated Other in scene
    does NOT glue onto the enemy — still mints (green guard, pins existing
    pass-through behavior).
  * test 4 — TWO live seated opponents is ambiguous; the hostile mention
    still mints rather than guessing which Other it belongs to (green
    guard, pins existing pass-through behavior).

Test 1 targets `_auto_mint_prose_only_npcs` (session_helpers.py) rather than
`_apply_npc_mentions`'s Step 1 because `_apply_npc_mentions`'s own Step-1
roster match (`_npc_name_match_keys`, story 162-10) is ALREADY a strict
superset of `resolve_roster_npc`'s three legs — canonical, alias, and
invented_from, all folded through the same `normalize_name`, PLUS
comma-flip/leading-article variants `resolve_roster_npc` does not fold. Any
mention `resolve_roster_npc` could match against `snapshot.npcs`, Step 1 has
already matched and consumed (`continue`s) before Step 3's attach-before-mint
preamble is ever reached — so the Step-3 preamble's first leg is a genuine,
reachable behavior change only via the SEATED-OTHER leg (test 2), not the
plain-resolve leg (which IS new, reachable behavior at the OTHER call site,
where no such resolver check existed at all).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.origin import Origin, OriginKind, resolve_roster_npc
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.server.narration_apply import _apply_npc_mentions
from sidequest.server.session_helpers import _auto_mint_prose_only_npcs

_ALIAS_ATTACHED_SPAN = "green_room.alias_attached"
_MINT_SPAN = "green_room.mint"
_INVENTED_ROUTED_SPAN = "npc.invented_name_routed"


@pytest.fixture
def local_otel() -> Iterator[InMemorySpanExporter]:
    """Self-contained in-memory exporter (mirrors the 162-10 resolver-seam
    suite so this file doesn't depend on conftest fixture ordering)."""
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def _core(name: str, *, hp: int = 8) -> CreatureCore:
    return CreatureCore(
        name=name,
        description="A statted adversary.",
        personality="Relentless.",
        inventory=Inventory(),
        hp=HpPool(current=hp, max=hp, base_max=hp),
        armor_class=12,
    )


def _snapshot() -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=1),
    )


def _seat_lone_opponent(
    snap: GameSnapshot, name: str, *, origin_kind: OriginKind, aliases: list[str] | None = None
) -> Npc:
    """Seed a GENERIC/NARRATOR_INVENTED-origin roster Npc AND seat it as the
    active encounter's sole live opponent-side actor — the shape a
    target-first seater (ADR-156 §3.3) leaves behind before the narrator's
    next mention pass runs."""
    npc = Npc(core=_core(name), origin=Origin(kind=origin_kind), aliases=list(aliases or []))
    snap.npcs.append(npc)
    snap.encounter = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="player", threshold=10),
        opponent_metric=EncounterMetric(name="opponent", threshold=10),
        actors=[EncounterActor(name=name, role="foe", side="opponent")],
        resolved=False,
    )
    return npc


# ---------------------------------------------------------------------------
# Test 1 — resolve-via-alias attaches, no prose-extraction mint (RED pre-impl)
# ---------------------------------------------------------------------------


def test_prose_name_resolving_via_alias_attaches_before_prose_mint(
    local_otel: InMemorySpanExporter,
) -> None:
    """A roster Npc already carries "Mrs. Gow" as a recorded alias (an
    earlier turn's narrator-mention attach). The prose-extraction feeder's
    OWN dedup set (`known_names`) only ever collects `npc.core.name` —
    never `npc.aliases` — so honorific prose re-citing "Mrs. Gow" misses
    that dedup and, pre-implementation, mints a phantom twin pool member.
    The attach-before-mint preamble's `resolve_roster_npc` call (which DOES
    walk the alias ledger) catches it instead."""
    snap = _snapshot()
    josephine = Npc(core=_core("Josephine Roth"), aliases=["Mrs. Gow"])
    snap.npcs.append(josephine)
    pool_before = len(snap.npc_pool)

    _auto_mint_prose_only_npcs(
        snapshot=snap,
        narration_text="Mrs. Gow tended the body. She washed him and laid him out.",
        emitted_mentions=[],
        turn_num=5,
    )

    assert len(snap.npc_pool) == pool_before, (
        f"a mention resolving via the alias ledger must NOT mint a twin pool "
        f"member; pool: {[m.name for m in snap.npc_pool]!r}"
    )
    # The alias was already present — attach_alias must decline the
    # duplicate re-add (no second "Mrs. Gow" entry) while still declining
    # to mint.
    assert josephine.aliases == ["Mrs. Gow"], (
        f"attach must be a no-op dedup against the EXISTING alias, not a "
        f"duplicate append; aliases: {josephine.aliases!r}"
    )
    mint_spans = [s for s in local_otel.get_finished_spans() if s.name == _MINT_SPAN]
    assert mint_spans == [], (
        f"no green_room.mint span may fire when the mention attaches instead "
        f"of minting; got {[dict(s.attributes or {}) for s in mint_spans]!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — the Ihnsch case: hostile mention attaches to the lone unaliased
# seated Other (RED pre-impl)
# ---------------------------------------------------------------------------


def test_hostile_mention_attaches_to_lone_unaliased_seated_other(
    local_otel: InMemorySpanExporter,
) -> None:
    """The Ihnsch case (ADR-156 design doc §3.4): the seater has already
    seated "the Scrapborn" (a GENERIC-backed target-first mint) as the
    encounter's sole live opponent. The narrator's next mention names the
    same person "Ihnsch of the Rusted Works" — textually unrelated to "the
    Scrapborn", so no resolver leg can reconcile them by name. The
    seated-Other leg attaches it as an alias instead of minting a twin."""
    snap = _snapshot()
    scrapborn = _seat_lone_opponent(snap, "the Scrapborn", origin_kind=OriginKind.GENERIC)
    pool_before = len(snap.npc_pool)

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[NpcMention(name="Ihnsch of the Rusted Works", role="hostile")],
        turn_num=6,
    )

    assert len(snap.npc_pool) == pool_before, (
        f"the hostile mention must attach to the seated Other, not mint a "
        f"twin; pool: {[m.name for m in snap.npc_pool]!r}"
    )
    seated = resolve_roster_npc(snap.npcs, "Ihnsch of the Rusted Works")
    assert seated is not None and seated.core.name == "the Scrapborn", (
        f"the alias must resolve back to the seated Other; got {seated!r}"
    )
    assert scrapborn.aliases == ["Ihnsch of the Rusted Works"]

    alias_spans = [
        dict(s.attributes or {})
        for s in local_otel.get_finished_spans()
        if s.name == _ALIAS_ATTACHED_SPAN
    ]
    assert any(a.get("alias") == "Ihnsch of the Rusted Works" for a in alias_spans), (
        f"green_room.alias_attached must fire for the attach; got {alias_spans!r}"
    )
    mint_spans = [s for s in local_otel.get_finished_spans() if s.name == _MINT_SPAN]
    assert mint_spans == [], "no green_room.mint span may fire — the mention attached, not minted"
    # ADR-156 §6 docstring contract: attach-before-mint runs BEFORE the
    # culture-name-routing block, so a mention that attaches never reaches
    # (and never pays for / logs) the namer at all.
    routed_spans = [s for s in local_otel.get_finished_spans() if s.name == _INVENTED_ROUTED_SPAN]
    assert routed_spans == [], (
        f"the culture namer must never engage for an attaching mention; got "
        f"{[dict(s.attributes or {}) for s in routed_spans]!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — a bystander mention does not glue onto the seated enemy (green
# guard, pins existing pass-through behavior)
# ---------------------------------------------------------------------------


def test_bystander_mention_with_seated_other_still_mints(
    local_otel: InMemorySpanExporter,
) -> None:
    """A non-hostile mention with a live seated Other in scene must NOT
    glue onto the enemy — the seated-Other leg only opens for a hostile
    cite. This already passes pre-implementation (no special-casing existed
    to divert it) and must keep passing after."""
    snap = _snapshot()
    _seat_lone_opponent(snap, "the Scrapborn", origin_kind=OriginKind.GENERIC)

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[NpcMention(name="Old Weka", role="bystander")],
        turn_num=6,
    )

    assert any(m.name == "Old Weka" for m in snap.npc_pool), (
        f"a bystander mention must mint normally, not attach to the enemy; "
        f"pool: {[m.name for m in snap.npc_pool]!r}"
    )


# ---------------------------------------------------------------------------
# Test 4 — two live seated opponents is ambiguous; never guess (green guard,
# pins existing pass-through behavior)
# ---------------------------------------------------------------------------


def test_two_seated_others_no_ambiguous_attach(local_otel: InMemorySpanExporter) -> None:
    """Two live opponent-side actors in the same encounter: the seated-Other
    leg requires EXACTLY ONE live opponent, so it declines rather than
    guessing which one the mention belongs to (No Silent Fallbacks) — the
    mention mints normally. Pins existing pass-through behavior."""
    snap = _snapshot()
    scrapborn = Npc(core=_core("the Scrapborn"), origin=Origin(kind=OriginKind.GENERIC))
    ironjaw = Npc(core=_core("the Ironjaw"), origin=Origin(kind=OriginKind.GENERIC))
    snap.npcs.extend([scrapborn, ironjaw])
    snap.encounter = StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="player", threshold=10),
        opponent_metric=EncounterMetric(name="opponent", threshold=10),
        actors=[
            EncounterActor(name="the Scrapborn", role="foe", side="opponent"),
            EncounterActor(name="the Ironjaw", role="foe", side="opponent"),
        ],
        resolved=False,
    )

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[NpcMention(name="Grudge Talon", role="hostile")],
        turn_num=6,
    )

    assert any(m.name == "Grudge Talon" for m in snap.npc_pool), (
        f"ambiguous (two live opponents) must fall through to mint, never "
        f"guess which one; pool: {[m.name for m in snap.npc_pool]!r}"
    )
    assert scrapborn.aliases == [] and ironjaw.aliases == [], (
        "neither seated opponent may receive a guessed alias attachment"
    )


# ---------------------------------------------------------------------------
# green_room.mint emission at both mint sites — a separate pin from the
# brief's four RED/PASS behavior tests above (span-emission is new
# instrumentation on the ALREADY-passing genuine-mint path, so folding it
# into tests 3/4 would flip their pre-implementation PASS to FAIL and
# muddy the RED/PASS split the brief specifies for those two).
# ---------------------------------------------------------------------------


def test_green_room_mint_span_fires_on_genuine_narrator_mention_mint(
    local_otel: InMemorySpanExporter,
) -> None:
    """The narration_apply.py Step-3 mint site must emit green_room.mint
    (identity_key/prose_name/source) alongside its existing telemetry when
    a mention is genuinely novel."""
    snap = _snapshot()

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[NpcMention(name="Old Weka", role="bystander")],
        turn_num=6,
    )

    mint_spans = [
        dict(s.attributes or {}) for s in local_otel.get_finished_spans() if s.name == _MINT_SPAN
    ]
    assert any(
        s.get("prose_name") == "Old Weka" and s.get("source") == "narrator_mention"
        for s in mint_spans
    ), (
        f"green_room.mint must fire with prose_name/source for the genuine "
        f"narrator-mention mint; got {mint_spans!r}"
    )
    assert all(s.get("identity_key") for s in mint_spans), (
        f"identity_key must be non-empty on every green_room.mint span; got {mint_spans!r}"
    )


def test_green_room_mint_span_fires_on_genuine_prose_extraction_mint(
    local_otel: InMemorySpanExporter,
) -> None:
    """The session_helpers.py `_auto_mint_prose_only_npcs` mint site must
    also emit green_room.mint when a prose-extracted honorific/role name is
    genuinely novel (no roster resolve, prose_extraction source)."""
    snap = _snapshot()

    _auto_mint_prose_only_npcs(
        snapshot=snap,
        narration_text="Father lies pale. He cannot speak.",
        emitted_mentions=[],
        turn_num=5,
    )

    mint_spans = [
        dict(s.attributes or {}) for s in local_otel.get_finished_spans() if s.name == _MINT_SPAN
    ]
    assert any(
        s.get("prose_name") == "Father" and s.get("source") == "prose_extraction"
        for s in mint_spans
    ), (
        f"green_room.mint must fire with prose_name/source for the genuine "
        f"prose-extraction mint; got {mint_spans!r}"
    )
