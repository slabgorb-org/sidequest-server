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

from sidequest.agents.orchestrator import NarrationTurnResult, NpcMention
from sidequest.agents.sidecar_extractor import _NPCS_PRESENT_ITEM_SCHEMA, SidecarExtraction
from sidequest.game.creature_core import CreatureCore, HpPool, Inventory
from sidequest.game.encounter import EncounterActor, EncounterMetric, StructuredEncounter
from sidequest.game.origin import Origin, OriginKind, resolve_roster_npc
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.server.narration_apply import (
    _apply_npc_mentions,
    merge_sidecar_extraction_npcs_present,
)
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


# ---------------------------------------------------------------------------
# Task-5 review fix (Important): the hostile signal must EXIST on real traffic.
# `_mention_is_hostile` reads mention.side / mention.role — but `side` is
# ENGINE-owned and name-keyed off already-seated actors (a NEW epithet like
# "Ihnsch of the Rusted Works" always resolves "neutral"), and `role` was read
# free-form by NpcMention.from_value yet never REQUESTED from the extractor LLM
# (_NPCS_PRESENT_ITEM_SCHEMA documented only name + is_place). So the
# seated-Other attach leg was correct-when-triggered but structurally starved
# of its trigger. Fix: the extractor schema now documents `role` as a stance
# enum. These two tests pin (a) the schema contract and (b) the REAL pipeline
# end-to-end — extraction dict -> merge_sidecar_extraction_npcs_present ->
# _apply_npc_mentions -> alias on the seated Other, no hand-built NpcMention.
# ---------------------------------------------------------------------------


def test_npcs_present_extractor_schema_documents_role_stance_enum() -> None:
    """`role` must be present in `_NPCS_PRESENT_ITEM_SCHEMA` with the exact
    stance enum, AND carry through to the emitted tool schema
    (`SidecarExtraction.model_json_schema()` is the forced tool's
    input_schema — the contract the extractor LLM actually sees). Without
    the enum in the emitted schema the reader is never told to classify
    stance, and the attach-before-mint hostile gate starves."""
    expected_enum = ["hostile", "friendly", "bystander", "neutral"]

    role = _NPCS_PRESENT_ITEM_SCHEMA["properties"].get("role")
    assert role is not None, (
        "_NPCS_PRESENT_ITEM_SCHEMA must document `role` — without it the "
        "extractor LLM never emits the stance signal _mention_is_hostile reads"
    )
    assert role.get("type") == "string"
    assert role.get("enum") == expected_enum, (
        f"role enum must be exactly {expected_enum!r} (the 'hostile' member is "
        f"what _mention_is_hostile matches on); got {role.get('enum')!r}"
    )

    # The annotation must carry into the EMITTED tool schema, not just the
    # module constant — model_json_schema() is what reaches the LLM.
    emitted = SidecarExtraction.model_json_schema()
    emitted_item = emitted["properties"]["npcs_present"]["items"]
    assert emitted_item["properties"]["role"]["enum"] == expected_enum, (
        "WithJsonSchema must surface the role enum in the emitted tool schema"
    )


def test_hostile_role_from_real_extraction_pipeline_attaches_to_seated_other(
    local_otel: InMemorySpanExporter,
) -> None:
    """Wiring (the gap the review exposed): drive the REAL pipeline — a fake
    extraction payload whose npcs_present item carries `role: "hostile"` and
    a novel name, through `merge_sidecar_extraction_npcs_present` (which
    overwrites `side` with the ENGINE-adjudicated value: "neutral" here,
    since a new epithet matches no seated actor name) and then the real
    `_apply_npc_mentions` consumer — and assert the name lands as an alias
    on the lone unaliased GENERIC seated Other. This proves the attach leg
    fires from production-shaped inputs, not a hand-built NpcMention."""
    snap = _snapshot()
    scrapborn = _seat_lone_opponent(snap, "the Scrapborn", origin_kind=OriginKind.GENERIC)
    pool_before = len(snap.npc_pool)

    result = NarrationTurnResult(
        narration=(
            "The wrench connects. Ihnsch of the Rusted Works staggers back "
            "into the tally line, spitting rust."
        ),
        npcs_present=[],  # stale/empty — the extraction is the sole source
    )
    extraction = SidecarExtraction(
        npcs_present=[{"name": "Ihnsch of the Rusted Works", "role": "hostile"}]
    )

    merged = merge_sidecar_extraction_npcs_present(result, extraction, snap)

    # The merge must carry role through from_value and leave it intact while
    # overwriting only side (engine-owned; a new epithet resolves "neutral").
    assert [m.role for m in merged.npcs_present] == ["hostile"], (
        f"role must survive the merge; got {[(m.name, m.role, m.side) for m in merged.npcs_present]!r}"
    )
    assert [m.side for m in merged.npcs_present] == ["neutral"], (
        "side is engine-adjudicated; an unseated new epithet must resolve neutral "
        "(if this fails, the test premise changed — side would then be the signal)"
    )

    _apply_npc_mentions(snapshot=snap, mentions=merged.npcs_present, turn_num=7)

    assert scrapborn.aliases == ["Ihnsch of the Rusted Works"], (
        f"the extractor-classified hostile mention must attach to the seated "
        f"Other through the real merge->apply pipeline; aliases: {scrapborn.aliases!r}"
    )
    assert len(snap.npc_pool) == pool_before, (
        f"no twin pool member may be minted; pool: {[m.name for m in snap.npc_pool]!r}"
    )
    seated = resolve_roster_npc(snap.npcs, "Ihnsch of the Rusted Works")
    assert seated is not None and seated.core.name == "the Scrapborn"
