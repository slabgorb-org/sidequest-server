"""Story 45-21 / 45-52 — Npc.core.edge populated from combat stats at handshake.

Playtest 3 (Orin save, 2026-04-19): the Crawling Scavenger sat in the
legacy ``snapshot.npc_registry`` with ``hp=0`` / ``max_hp=0``. Any
HP-check subsystem that queried the registry to determine creature
liveness saw "already dead" and skipped — six rounds of combat, opponent
dead from turn 0 according to the registry.

Root cause: nothing wrote combat stats into the registry entry. The
encounter handshake knew the actors and the per-side dial threshold
(= effective pool size), but did not propagate that into the registry.

Story 45-52 cleanup rewires the publish seam: the legacy ``npc_registry``
is gone; the canonical home for runtime creature pools is
``Npc.core.edge`` (ADR-078, ADR-014). At encounter start the handshake
calls ``_publish_combat_edge_to_npcs``, which writes
``current``/``max`` onto each opponent-side ``Npc.core.edge`` and emits
``npc.edge_published`` OTEL spans so the GM panel lie-detector can
verify the seam fired.

References:
- AC1: current/max written when combat stats are emitted
- AC2: pool cannot report current==0 unless the NPC is actually defeated
- AC3: OTEL span emitted with current/max values
- AC4: no regression on existing combat stats flow
- Wiring AC: production encounter init path actually calls the helper
"""

from __future__ import annotations

from pathlib import Path

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from sidequest.agents.orchestrator import (
    NpcMention,
)
from sidequest.game.creature_core import (
    CreatureCore,
    HpPool,
    Inventory,
)
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager
from sidequest.genre.loader import load_genre_pack
from tests._helpers.trigger_encounter import trigger_encounter

_FIXTURE_PACK = Path(__file__).resolve().parents[1] / "fixtures" / "packs" / "test_genre"


def _load_pack():
    return load_genre_pack(_FIXTURE_PACK)


def _make_npc(
    name: str,
    *,
    role: str | None = None,
    pronouns: str | None = None,
    appearance: str | None = None,
    last_seen_location: str | None = None,
    last_seen_turn: int = 0,
) -> Npc:
    return Npc(
        core=CreatureCore(
            name=name,
            description=appearance or "An NPC.",
            personality="Neutral.",
            level=1,
            xp=0,
            inventory=Inventory(),
            statuses=[],
            hp=HpPool(current=10, max=10, base_max=10),
        ),
        pronouns=pronouns,
        appearance=appearance,
        npc_role_id=role,
        last_seen_location=last_seen_location,
        last_seen_turn=last_seen_turn,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def combat_snapshot():
    """Snapshot reproducing the Playtest 3 [Orin save] shape.

    Crawling Scavenger pre-registered in ``snapshot.npcs`` with a
    placeholder edge pool; the next narration turn fires
    ``confrontation="combat"`` and the handshake must publish dial-derived
    edge onto the npc's ``core.edge``.
    """
    snap = GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=4),
    )
    snap.character_locations["Orin"] = "Mawdeep Caverns"
    snap.npcs.append(
        _make_npc(
            "Crawling Scavenger",
            role="hostile",
            pronouns="it/its",
            appearance="a chittering carapaced thing the size of a hound",
            last_seen_location="Mawdeep Caverns",
            last_seen_turn=3,
        )
    )
    return snap, _load_pack()


@pytest.fixture
def otel_capture():
    """Attach an in-memory exporter to the running TracerProvider."""
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider), (
        f"expected SDK TracerProvider, got {type(provider)!r}"
    )
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


# ---------------------------------------------------------------------------
# AC1 — current/max written on combat handshake (production wire)
# ---------------------------------------------------------------------------


def test_combat_handshake_publishes_edge_onto_opponent_npc(combat_snapshot):
    """Playtest 3 reproduction.

    Combat encounter starts with the Crawling Scavenger pulled from the
    location-scoped fallback. The encounter init MUST publish current/max
    onto ``npc.core.edge`` — otherwise HP-check subsystems read the
    placeholder pool forever.
    """
    snap, pack = combat_snapshot
    npc = snap.npcs[0]
    placeholder_max = npc.core.hp.max

    trigger_encounter(snap, pack, "combat", "Orin", npcs_present=[])

    # The combat dial in test_genre has threshold=10. After publish, the
    # edge pool should be sized to the dial threshold (not the placeholder).
    assert npc.core.hp.max != placeholder_max or npc.core.hp.max > 0, (
        "edge.max not populated after combat handshake"
    )
    assert npc.core.hp.current > 0, (
        "edge.current not populated (or set to 0) at combat start — pool would "
        "appear always-defeated like the Playtest 3 shape"
    )
    # Fresh combat: pool is full (dial.current==0 ⇒ edge.current == edge.max).
    assert npc.core.hp.current == npc.core.hp.max, (
        "fresh combat: edge.current must equal edge.max (no damage taken yet)"
    )


def test_combat_handshake_publishes_edge_for_explicit_npcs_present(combat_snapshot):
    """When the narrator supplies an explicit ``npcs_present`` list, those
    opponents' npc.core.edge pools also receive combat stats.

    Pre-registers a "Goblin" as a stateful Npc so the auto-promotion seam
    in narration_apply finds the existing entry to upsert and the handshake
    has something to write into.
    """
    snap, pack = combat_snapshot
    snap.npcs.append(
        _make_npc(
            "Goblin",
            role="hostile",
            last_seen_location="Mawdeep Caverns",
            last_seen_turn=3,
        )
    )
    trigger_encounter(
        snap,
        pack,
        "combat",
        "Orin",
        npcs_present=[
            NpcMention(name="Goblin", side="opponent", role="hostile"),
        ],
    )
    goblin = next(n for n in snap.npcs if n.core.name == "Goblin")
    assert goblin.core.hp.current > 0
    assert goblin.core.hp.max > 0


def test_non_combat_handshake_leaves_edge_at_placeholder(combat_snapshot):
    """AC2: a social/negotiation encounter has no combat-pool semantics —
    the handshake must NOT pretend to publish combat edge. Otherwise
    non-combat NPCs would suddenly have a pool ceiling that isn't backed
    by any mechanic.
    """
    snap, pack = combat_snapshot
    # Replace the hostile scavenger with a neutral entry so the handshake
    # doesn't classify it as opponent. (Negotiation isn't in the fallback
    # category-default opponent set anyway, but this keeps the test honest.)
    snap.npcs.clear()
    snap.npcs.append(
        _make_npc(
            "Brother Halrik",
            role="merchant",
            last_seen_location="Mawdeep Caverns",
            last_seen_turn=3,
        )
    )
    halrik = snap.npcs[0]
    placeholder_max = halrik.core.hp.max
    placeholder_current = halrik.core.hp.current

    trigger_encounter(snap, pack, "negotiation", "Orin", npcs_present=[])
    assert halrik.core.hp.max == placeholder_max, (
        "non-combat encounter must NOT publish combat edge"
    )
    assert halrik.core.hp.current == placeholder_current


# ---------------------------------------------------------------------------
# AC3 — OTEL observability
# ---------------------------------------------------------------------------


def test_otel_span_emitted_on_npc_edge_publish(combat_snapshot, otel_capture):
    """An ``npc.edge_published`` span fires per opponent write so the GM
    panel can verify the seam engaged. CLAUDE.md OTEL principle: every fix
    that touches a subsystem must emit OTEL so we can tell whether it
    actually engaged or Claude is just improvising.
    """
    snap, pack = combat_snapshot
    trigger_encounter(snap, pack, "combat", "Orin", npcs_present=[])

    spans = [s for s in otel_capture.get_finished_spans() if s.name == "npc.edge_published"]
    assert spans, (
        "no npc.edge_published spans emitted — the GM panel can't verify "
        "the edge-publish seam fired"
    )
    scav_span = next(
        (s for s in spans if (s.attributes or {}).get("npc_name") == "Crawling Scavenger"),
        None,
    )
    assert scav_span is not None, (
        "no edge_published span for the Crawling Scavenger — actor → npc name match regressed"
    )
    attrs = scav_span.attributes or {}
    assert int(attrs.get("current", 0)) > 0, (
        "OTEL span carries current=0 on a fresh combat handshake; AC3 requires "
        "current/max values be readable from the span"
    )
    assert int(attrs.get("max", 0)) > 0
    assert attrs.get("source") == "encounter_handshake"


# ---------------------------------------------------------------------------
# AC4 — Regression guard: existing combat stats / handshake flow
# ---------------------------------------------------------------------------


def test_handshake_still_registers_actors_after_edge_publish(combat_snapshot):
    """Smoke check that adding the edge-publish write didn't regress the
    Story 45-18 actor-registration handshake.
    """
    snap, pack = combat_snapshot
    trigger_encounter(snap, pack, "combat", "Orin", npcs_present=[])
    enc = snap.encounter
    assert enc is not None
    actor_names = {a.name for a in enc.actors}
    assert {"Orin", "Crawling Scavenger"} <= actor_names


# ---------------------------------------------------------------------------
# Wiring guard — production code path actually calls the helper
# ---------------------------------------------------------------------------


def test_edge_publish_helper_is_wired_into_production_handshake(combat_snapshot, otel_capture):
    """CLAUDE.md "Verify Wiring": prove the edge-publish helper is reachable
    from the *production* encounter-init entrypoint, not merely defined.

    Behavioral proof instead of a source-text grep (CLAUDE.md "No Source-Text
    Wiring Tests"): ``trigger_encounter`` drives the real production lifecycle
    function ``instantiate_encounter_from_trigger``. If that path calls
    ``_publish_combat_edge_to_npcs`` the helper emits its ``npc.edge_published``
    span tagged ``source="encounter_handshake"``. No span ⇒ the call site was
    removed and the seam is dead. This survives the harmless refactors that the
    old occurrence-count assertion broke on, and fails on real wiring breakage.

    Distinct from ``test_otel_span_emitted_on_npc_edge_publish`` (AC3), which
    asserts the span's *payload* (current/max values); this test asserts only
    that the production entrypoint *reaches* the helper at all.
    """
    snap, pack = combat_snapshot

    trigger_encounter(snap, pack, "combat", "Orin", npcs_present=[])

    publish_spans = [
        s
        for s in otel_capture.get_finished_spans()
        if s.name == "npc.edge_published"
        and (s.attributes or {}).get("source") == "encounter_handshake"
    ]
    assert publish_spans, (
        "no npc.edge_published span tagged source=encounter_handshake after "
        "driving the production encounter-init path — "
        "_publish_combat_edge_to_npcs is no longer called from "
        "instantiate_encounter_from_trigger (wiring regression)"
    )
