"""RED — Story 72-4: route narrator-invented NPC names through ADR-091 namegen.

When the narrator invents an NPC mid-scene it hands back a *bare name string*
and the Step-3 "novel" branch of ``_apply_npc_mentions``
(``sidequest/server/narration_apply.py``) mints
``NpcPoolMember(drawn_from="narrator_invented", name=mention.name)`` with that
raw string verbatim — bypassing the culture-bound ADR-091 generator
(``build_from_culture`` / ``NameGenerator.generate_person``). A carefully
authored ``space_opera``/``perseus_cloud`` table can therefore still get a
narrator-minted "Bob Hegemonic" that breaks the table's sense of place.

This is a **WIRING** story (CLAUDE.md "Don't Reinvent — Wire Up What Exists"):
the generator, culture model, corpus loading, Markov chain, stem-collision
filter, and the three corpus-health OTEL guards already exist and ship in every
pack. What is missing is the wire between the narrator-invented mint seam and
that generator. These tests pin that wire.

Test-design contract (pinned by TEA; see session 72-4 assessment / deviations):

* ``_apply_npc_mentions`` gains a naming-context seam — ``name_generator:
  NameGenerator | None`` plus ``culture_name``/``culture_source`` — threaded
  from its caller ``_apply_narration_result_to_snapshot`` (story context's
  PREFERRED design: "thread the already-resolved generator/culture in rather
  than reaching for a global"). When a generator is supplied, the Step-3 novel
  branch mints a culture-true *generated* name instead of the raw string.
* Culture is resolved by the **caller** via ``Pack.effective_cultures(world)``
  (NOT raw ``pack.cultures`` — that divergence was the perseus_cloud session-894
  0-NPCs-seeded bug) and the resolved ``(culture_name, source)`` rides along so
  the provenance span can record it.
* New provenance span ``npc.invented_name_routed`` records resolved culture +
  source, the narrator's bare name, the generated name, and a collision-reroll
  flag.
* No Silent Fallbacks: when no culture is bound for the active world (or
  generation fails), the path fails **loud** — ``npc.invented_name_unrouted``
  (``severity="warning"``) fires; the engine never silently mints the raw string
  with no signal. (Recovery — raise vs deliberate degrade — is Dev's call; the
  loudness is the invariant.)
* The existing mint-branch spans (``npc.referenced`` with
  ``match_strategy="invented"`` and ``npc.auto_registered``) and the
  namegen-internal guards must keep firing unchanged.

Span names/attrs are asserted as **strings**, not imported constants, so the
suite collects cleanly while the constants do not yet exist (clean RED).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.agents.orchestrator import NarrationTurnResult, NpcMention
from sidequest.game.character import Character
from sidequest.game.creature_core import CreatureCore
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot, Npc
from sidequest.genre.names.generator import NameGenerator
from sidequest.server.narration_apply import _apply_npc_mentions
from tests._helpers.session_room import room_for

ROUTED_SPAN = "npc.invented_name_routed"
UNROUTED_SPAN = "npc.invented_name_unrouted"
REFERENCED_SPAN = "npc.referenced"
AUTO_REGISTERED_SPAN = "npc.auto_registered"

CONTENT_GENRE_PACKS = (
    Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
)
SPACE_OPERA_DIR = CONTENT_GENRE_PACKS / "space_opera"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _core(name: str) -> CreatureCore:
    return CreatureCore(name=name, description="X.", personality="Y.")


def _pc(name: str) -> Character:
    return Character(
        core=_core(name),
        backstory="A wanderer.",
        char_class="adventurer",
        race="human",
    )


def _mention(
    name: str,
    *,
    role: str = "",
    pronouns: str = "",
    appearance: str = "",
) -> NpcMention:
    return NpcMention(name=name, role=role, pronouns=pronouns, appearance=appearance)


def _result(narration: str, npcs_present: list[NpcMention]) -> NarrationTurnResult:
    return NarrationTurnResult(
        narration=narration,
        npcs_present=list(npcs_present),
        is_degraded=False,
    )


class _SeqNameGenerator(NameGenerator):
    """Deterministic stand-in: yields a fixed sequence from ``generate_person``.

    ``NameGenerator`` is a dataclass with defaults for every field, so
    ``super().__init__()`` constructs a valid (empty) instance. Overriding
    ``generate_person`` lets a test force collisions / pre-existing-name hits
    without depending on corpus content — the real generator is exercised by
    the end-to-end wiring test below.
    """

    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self._iter = iter(names)
        self.person_calls = 0

    def generate_person(self, pattern: str | None = None) -> str:  # noqa: ARG002
        self.person_calls += 1
        return next(self._iter)


def _attrs_for(otel_capture, span_name: str) -> list[dict]:
    return [
        dict(s.attributes or {})
        for s in otel_capture.get_finished_spans()
        if s.name == span_name
    ]


# ===========================================================================
# AC1 — generated, not raw (unit, at the mint seam)
# ===========================================================================


def test_invented_branch_mints_generated_name_not_raw_string(otel_capture) -> None:
    """A novel narrator name is replaced by a culture-bound *generated* name.

    The minted ``NpcPoolMember.name`` must be the generator's output, NOT the
    narrator's bare ``mention.name`` ("Bob Hegemonic"). This is the whole point
    of 72-4: invented NPCs become genre/culture-true by construction.
    """
    gen = _SeqNameGenerator(["Veyra Solnë"])
    snapshot = GameSnapshot()

    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Bob Hegemonic", role="deckhand", pronouns="he/him")],
        turn_num=3,
        name_generator=gen,
        culture_name="Spacer",
        culture_source="world",
    )

    assert len(snapshot.npc_pool) == 1
    member = snapshot.npc_pool[0]
    assert member.name == "Veyra Solnë", (
        "Step-3 novel branch must mint the generator's output, not the "
        f"narrator's bare string; got {member.name!r}"
    )
    assert member.name != "Bob Hegemonic"
    # Provenance is preserved — still a narrator-invented mint, just culture-routed.
    assert member.drawn_from == "narrator_invented"
    # Non-name identity fields the narrator supplied still ride along.
    assert member.role == "deckhand"
    assert gen.person_calls == 1


# ===========================================================================
# AC3 — provenance span fired (unit, at the mint seam)
# ===========================================================================


def test_invented_branch_emits_provenance_span(otel_capture) -> None:
    """The rerouted mint emits ``npc.invented_name_routed`` with culture id +
    source, the narrator's original bare name, and the generated name."""
    gen = _SeqNameGenerator(["Veyra Solnë"])
    snapshot = GameSnapshot()

    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Bob Hegemonic")],
        turn_num=7,
        name_generator=gen,
        culture_name="Spacer",
        culture_source="world",
    )

    routed = _attrs_for(otel_capture, ROUTED_SPAN)
    assert len(routed) == 1, (
        f"exactly one {ROUTED_SPAN} span must fire; got {len(routed)}"
    )
    attrs = routed[0]
    assert attrs.get("original_name") == "Bob Hegemonic"
    assert attrs.get("npc_name") == "Veyra Solnë"  # the minted/generated name
    assert attrs.get("culture") == "Spacer"
    assert attrs.get("culture_source") == "world"
    assert attrs.get("collision_reroll") is False
    assert attrs.get("turn_number") == 7


def test_invented_branch_preserves_existing_invented_spans(otel_capture) -> None:
    """The existing mint telemetry must keep firing after the reroute.

    Story guardrail: ``npc.referenced`` (match_strategy="invented") and
    ``npc.auto_registered`` are pre-existing GM-panel signals and must NOT be
    dropped when the name is rerouted through namegen.
    """
    gen = _SeqNameGenerator(["Veyra Solnë"])
    snapshot = GameSnapshot()

    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Bob Hegemonic")],
        turn_num=2,
        name_generator=gen,
        culture_name="Spacer",
        culture_source="genre",
    )

    referenced = _attrs_for(otel_capture, REFERENCED_SPAN)
    invented = [a for a in referenced if a.get("match_strategy") == "invented"]
    assert len(invented) == 1, (
        "npc.referenced(match_strategy='invented') must still fire on the "
        "rerouted novel branch"
    )
    # The referenced span reports the canonical (minted) name.
    assert invented[0].get("npc_name") == "Veyra Solnë"

    auto_reg = _attrs_for(otel_capture, AUTO_REGISTERED_SPAN)
    assert len(auto_reg) == 1, "npc.auto_registered must still fire on novel mint"
    assert auto_reg[0].get("npc_name") == "Veyra Solnë"


# ===========================================================================
# Stem-collision reroll (unit) — has_stem_collision reject + re-roll
# ===========================================================================


def test_invented_branch_rerolls_on_stem_collision(otel_capture) -> None:
    """A generated name that trips ``has_stem_collision`` is rejected and the
    route re-rolls; the minted name is the clean one and the provenance span
    records ``collision_reroll=True``.

    "Frandrew Andrew" is the canonical stem-collision artifact
    (``has_stem_collision`` returns True). The route must not mint it.
    """
    gen = _SeqNameGenerator(["Frandrew Andrew", "Veyra Solnë"])
    snapshot = GameSnapshot()

    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Bob Hegemonic")],
        turn_num=4,
        name_generator=gen,
        culture_name="Spacer",
        culture_source="world",
    )

    assert len(snapshot.npc_pool) == 1
    member = snapshot.npc_pool[0]
    assert member.name == "Veyra Solnë", (
        "the stem-collision candidate must be rejected and re-rolled; "
        f"got {member.name!r}"
    )
    assert gen.person_calls == 2, "route must re-roll exactly once past the collision"

    routed = _attrs_for(otel_capture, ROUTED_SPAN)
    assert len(routed) == 1
    assert routed[0].get("collision_reroll") is True
    assert routed[0].get("npc_name") == "Veyra Solnë"


# ===========================================================================
# AC5 — pre-existing names respected (Step-1 / Step-2 not regenerated)
# ===========================================================================


def test_existing_npc_hit_is_not_regenerated(otel_capture) -> None:
    """A mention matching an existing ``snapshot.npcs`` entry takes the Step-1
    path — last_seen updated, generator NEVER consulted, no reroute span."""
    gen = _SeqNameGenerator(["ShouldNotBeUsed"])
    snapshot = GameSnapshot(
        characters=[_pc("Hero")],
        npcs=[Npc(core=_core("Boris"))],
    )
    snapshot.character_locations["Hero"] = "Dock7"

    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Boris")],
        turn_num=5,
        acting_character_name="Hero",
        name_generator=gen,
        culture_name="Spacer",
        culture_source="world",
    )

    assert gen.person_calls == 0, "existing Npc must not be run through namegen"
    assert snapshot.npcs[0].last_seen_turn == 5
    assert snapshot.npc_pool == []
    assert _attrs_for(otel_capture, ROUTED_SPAN) == [], (
        "no reroute span may fire for an npcs_hit"
    )


def test_existing_pool_member_hit_is_not_regenerated(otel_capture) -> None:
    """A mention matching an existing ``npc_pool`` member takes the Step-2 path
    — additive upsert only, generator NEVER consulted, no reroute span."""
    gen = _SeqNameGenerator(["ShouldNotBeUsed"])
    snapshot = GameSnapshot(
        npc_pool=[NpcPoolMember(name="Marya", drawn_from="legacy_registry")],
    )

    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Marya", pronouns="she/her")],
        turn_num=1,
        name_generator=gen,
        culture_name="Spacer",
        culture_source="world",
    )

    assert gen.person_calls == 0, "existing pool member must not be regenerated"
    assert len(snapshot.npc_pool) == 1
    assert snapshot.npc_pool[0].name == "Marya"
    assert snapshot.npc_pool[0].pronouns == "she/her"  # additive upsert preserved
    assert _attrs_for(otel_capture, ROUTED_SPAN) == []


def test_generated_name_colliding_with_existing_member_does_not_duplicate(
    otel_capture,
) -> None:
    """AC5(c): a generated name equal to an existing store member must resolve
    to the existing member or re-roll — never create a duplicate identity.

    The generator first yields "Marya" (already in the pool); the route must
    not mint a second "Marya". It then yields a clean name the route may use.
    Invariant asserted: exactly one pool member named "Marya" survives.
    """
    gen = _SeqNameGenerator(["Marya", "Veyra Solnë"])
    snapshot = GameSnapshot(
        npc_pool=[NpcPoolMember(name="Marya", drawn_from="legacy_registry")],
    )

    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Bob Hegemonic")],
        turn_num=6,
        name_generator=gen,
        culture_name="Spacer",
        culture_source="world",
    )

    maryas = [m for m in snapshot.npc_pool if m.name.casefold() == "marya"]
    assert len(maryas) == 1, (
        "a generated name colliding with an existing pool member must not "
        f"create a duplicate identity; found {len(maryas)} 'Marya' members"
    )


# ===========================================================================
# Backward-compat — no naming context = legacy raw mint (existing callers)
# ===========================================================================


def test_no_name_generator_falls_back_to_raw_mint(otel_capture) -> None:
    """Called without a naming context (the legacy signature), the novel branch
    keeps its raw-mint behavior so existing callers/tests stay green.

    This is NOT the silent-fallback the story prohibits: production always
    threads the pack/world (proved by the wiring test below). The no-context
    path is the explicitly context-free call, and it fires no reroute span.
    """
    snapshot = GameSnapshot()

    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Erewhon", role="hermit")],
        turn_num=1,
    )

    assert len(snapshot.npc_pool) == 1
    assert snapshot.npc_pool[0].name == "Erewhon"
    assert _attrs_for(otel_capture, ROUTED_SPAN) == []


# ===========================================================================
# AC2 + AC4 + WIRING — drive the real production path end-to-end
# ===========================================================================

pytestmark_e2e = pytest.mark.skipif(
    not SPACE_OPERA_DIR.exists(),
    reason="sidequest-content/genre_packs/space_opera not checked out",
)


@pytestmark_e2e
def test_wiring_full_apply_routes_invented_name_through_namegen(
    otel_capture, monkeypatch
) -> None:
    """MANDATORY WIRING TEST (CLAUDE.md "Every Test Suite Needs a Wiring Test").

    Drive the REAL ``_apply_narration_result_to_snapshot`` →
    ``_apply_npc_mentions`` production path with a real ``space_opera`` pack and
    an invented NPC. The minted name must be the culture-bound generated name,
    not the narrator's bare string, and the provenance span must fire — proving
    the route is reachable end-to-end, not merely callable in isolation.

    ``NameGenerator.generate_person`` is patched on the class to a sentinel so
    the assertion is crisp regardless of corpus draw; ``build_from_culture``
    still runs for real (corpus read + Markov train), so this also proves the
    caller resolves the corpus directory correctly.
    """
    from sidequest.genre import load_genre_pack
    from sidequest.server.session_handler import _apply_narration_result_to_snapshot

    monkeypatch.setattr(
        NameGenerator, "generate_person", lambda self, pattern=None: "Veyra Solnë"
    )

    pack = load_genre_pack(SPACE_OPERA_DIR)
    snapshot = GameSnapshot(genre_slug="space_opera", world_slug="perseus_cloud")
    snapshot.character_locations["Rux"] = "The Drift"

    result = _result(
        narration="A stranger steps from the airlock shadow.",
        npcs_present=[_mention("Bob Hegemonic", role="smuggler", pronouns="he/him")],
    )

    _apply_narration_result_to_snapshot(
        snapshot,
        result,
        "player",
        room=room_for(snapshot, slug="perseus_cloud"),
        pack=pack,
        world="perseus_cloud",
        acting_character_name="Rux",
    )

    invented = [m for m in snapshot.npc_pool if m.drawn_from == "narrator_invented"]
    assert len(invented) == 1, (
        "exactly one narrator-invented pool member must be minted end-to-end"
    )
    assert invented[0].name == "Veyra Solnë", (
        "WIRING FAILURE: the production apply path did not route the invented "
        f"name through namegen; minted {invented[0].name!r} (the raw narrator "
        "string would be 'Bob Hegemonic'). The generator/culture context is not "
        "threaded from _apply_narration_result_to_snapshot into _apply_npc_mentions."
    )
    assert invented[0].name != "Bob Hegemonic"

    routed = _attrs_for(otel_capture, ROUTED_SPAN)
    assert len(routed) == 1, (
        f"the provenance span must fire on the production path; got {len(routed)}"
    )
    assert routed[0].get("original_name") == "Bob Hegemonic"
    assert routed[0].get("npc_name") == "Veyra Solnë"


@pytestmark_e2e
def test_wiring_provenance_records_effective_culture_source(
    otel_capture, monkeypatch
) -> None:
    """AC2 regression guard (perseus_cloud session-894 divergence).

    Culture must be resolved via ``Pack.effective_cultures(world)`` — NOT raw
    ``pack.cultures``. The provenance span's ``culture``/``culture_source`` must
    match what ``effective_cultures`` returns for the active world, so a world
    that binds its own cultures cannot be silently bypassed by the genre set.
    """
    from sidequest.genre import load_genre_pack
    from sidequest.server.session_handler import _apply_narration_result_to_snapshot

    monkeypatch.setattr(
        NameGenerator, "generate_person", lambda self, pattern=None: "Veyra Solnë"
    )

    pack = load_genre_pack(SPACE_OPERA_DIR)
    expected_cultures, expected_source = pack.effective_cultures("perseus_cloud")
    expected_names = {c.name for c in expected_cultures}
    assert expected_names, "precondition: perseus_cloud must resolve a culture set"

    snapshot = GameSnapshot(genre_slug="space_opera", world_slug="perseus_cloud")
    result = _result(
        narration="A figure detaches from the crowd.",
        npcs_present=[_mention("Bob Hegemonic")],
    )

    _apply_narration_result_to_snapshot(
        snapshot,
        result,
        "player",
        room=room_for(snapshot, slug="perseus_cloud"),
        pack=pack,
        world="perseus_cloud",
        acting_character_name=None,
    )

    routed = _attrs_for(otel_capture, ROUTED_SPAN)
    assert len(routed) == 1
    assert routed[0].get("culture_source") == expected_source, (
        "provenance source must match Pack.effective_cultures(world)[1] — "
        "resolving via raw pack.cultures is the session-894 regression"
    )
    assert routed[0].get("culture") in expected_names, (
        f"resolved culture {routed[0].get('culture')!r} must be one bound for "
        f"perseus_cloud ({sorted(expected_names)})"
    )


@pytestmark_e2e
def test_wiring_fails_loud_when_no_culture_bound(otel_capture, monkeypatch) -> None:
    """AC4 — No Silent Fallbacks.

    When the active world resolves no culture, the path must fail LOUD: a
    warning-severity ``npc.invented_name_unrouted`` span fires and the condition
    is surfaced, rather than silently minting the raw narrator string with no
    signal. (Whether the engine then raises or deliberately degrades is Dev's
    call; the loud signal is the invariant asserted here.)
    """
    from sidequest.genre import load_genre_pack
    from sidequest.genre.models.pack import GenrePack
    from sidequest.server.session_handler import _apply_narration_result_to_snapshot

    pack = load_genre_pack(SPACE_OPERA_DIR)
    # Force the unresolved-culture condition independent of pack content.
    monkeypatch.setattr(
        GenrePack, "effective_cultures", lambda self, world: ([], "genre")
    )

    snapshot = GameSnapshot(genre_slug="space_opera", world_slug="perseus_cloud")
    result = _result(
        narration="Someone calls your name from the dark.",
        npcs_present=[_mention("Bob Hegemonic")],
    )

    _apply_narration_result_to_snapshot(
        snapshot,
        result,
        "player",
        room=room_for(snapshot, slug="perseus_cloud"),
        pack=pack,
        world="perseus_cloud",
        acting_character_name=None,
    )

    unrouted = _attrs_for(otel_capture, UNROUTED_SPAN)
    assert len(unrouted) == 1, (
        "unresolved culture must fail loud with exactly one "
        f"{UNROUTED_SPAN} span; got {len(unrouted)}"
    )
    assert unrouted[0].get("severity") == "warning"
    assert unrouted[0].get("original_name") == "Bob Hegemonic"
    # The reroute success span must NOT fire when culture is unresolved —
    # otherwise the GM panel would show a culture-true route that never happened.
    assert _attrs_for(otel_capture, ROUTED_SPAN) == []
