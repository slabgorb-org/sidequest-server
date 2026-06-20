"""RED — Story 126-32: bind narrated NPCs to seeded/registry entities.

One root seam, three manifestations (epic-126 NPC-binding cluster). The root
cause is a TIMING bug in ``narration_apply.py``: a narrator-named NPC is minted
into ``snapshot.npc_pool`` but is NOT promoted to ``snapshot.npcs`` before the
downstream mint/seating logic runs. The opponent-seater and the canonical-figure
binder both read ``snapshot.npcs`` only, so the narrated identity is invisible to
them and a hollow duplicate is fabricated instead. The fix promotes narrated NPCs
to ``snapshot.npcs`` *before* mint/seating.

The three manifestations these tests pin:

(a) **Narrated antagonist -> seated Fate opponent identity by exact name.**
    ``dust_and_lead`` seated a phantom "Henry Shaw" CreatureCore
    (``description="Fate conflict opponent"``, no pronouns/appearance) beside the
    narrator-established antagonist already sitting in the pool. The Defect-A
    seater fix shipped (the bestiary "Western Diamondback" is no longer
    conscripted for a non-combat Fate standoff); the *identity-binding residual*
    is open — the human stub the seater mints must carry the narrated identity,
    not a hollow phantom.

(b) **Described canonical figure -> seeded registry NPC.** A narrator reference
    to a runtime-wired authored figure (oz "The Good Witch of the North",
    she/her, disposition 25 — ADR-059 npcs.yaml roster) must reconcile to that
    registry entity and carry its pronouns/disposition, not mint a hollow
    culture-routed duplicate ("Amaranth Warmacre").

(c) **Defect B namegen — shuffle_fallback culture/region reroute.** A
    narrator-supplied proper name in the Anglo-settler ``dust_and_lead`` frontier
    must NOT be blind-shuffled across to the Ndé (Apache) people-group. The
    ``npc.invented_name_routed`` span records ``resolution_strategy`` — a
    narrator-named NPC routed by the blind ``shuffle_fallback`` to a culturally
    incongruous people-group is the defect; the mint must be keyed to the current
    region instead.

Test doctrine (mirrors test_npc_ongoing_threat_reconciliation.py): assert
BEHAVIOR (the snapshot carries the narrated identity; no hollow duplicate) plus
the OTEL CONTRACT, driving the REAL production seams. Mechanism is Dev's choice —
these do not assert *which* promotion lever is used. Spans are referenced by
string literal so RED failures are behavioral, never a collection-time
ImportError.

DESIGN NOTE (see session 126-32 Delivery Findings, blocking): manifestations (b)
and (c) carry open design questions — the "seeded registry" lookup trigger (b)
and the region->culture mapping (c, which has no content representation today).
These tests pin the OBSERVABLE contract; the resolution mechanism is gated on the
findings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.creature_core import CreatureCore
from sidequest.game.encounter import EncounterActor
from sidequest.game.session import GameSnapshot, Npc
from sidequest.server.dispatch.encounter_lifecycle import _seed_fate_opponents
from sidequest.server.narration_apply import _apply_npc_mentions

CONTENT_GENRE_PACKS = Path(__file__).resolve().parents[3] / "sidequest-content" / "genre_packs"
SPAGHETTI_WESTERN_DIR = CONTENT_GENRE_PACKS / "spaghetti_western"

ROUTED_SPAN = "npc.invented_name_routed"
FATE_OPPONENT_SEEDED_SPAN = "fate.opponent.seeded"

_HAS_DUST_AND_LEAD = (SPAGHETTI_WESTERN_DIR / "worlds" / "dust_and_lead" / "world.yaml").exists()
requires_content = pytest.mark.skipif(
    not _HAS_DUST_AND_LEAD,
    reason="sidequest-content/genre_packs/spaghetti_western/worlds/dust_and_lead not checked out",
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _mention(
    name: str,
    *,
    role: str = "",
    pronouns: str = "",
    appearance: str = "",
    side: str = "neutral",
    is_creature: bool = False,
) -> NpcMention:
    return NpcMention(
        name=name,
        role=role,
        pronouns=pronouns,
        appearance=appearance,
        side=side,
        is_creature=is_creature,
    )


def _attrs_for(otel_capture, span_name: str) -> list[dict]:
    return [
        dict(s.attributes or {}) for s in otel_capture.get_finished_spans() if s.name == span_name
    ]


def _npcs_named(snapshot: GameSnapshot, name: str) -> list[Npc]:
    return [n for n in snapshot.npcs if n.core.name == name]


# ===========================================================================
# Manifestation (a) — narrated Fate opponent binds by identity, not phantom
# ===========================================================================


@requires_content
def test_narrated_fate_opponent_binds_identity_not_phantom(otel_capture) -> None:
    """A narrator-established antagonist seated as a Fate opponent must keep its
    identity, not be replaced by a hollow phantom.

    Drives the REAL narration->seat ordering: the narrator names "Henry Shaw"
    (side="opponent", he/him, with appearance) which lands in the cast; then the
    Fate conflict seats an opponent of that exact name. Today the narrated
    identity sits in ``snapshot.npc_pool`` while ``_seed_fate_opponents`` reads
    only ``snapshot.npcs`` — so it CREATES a phantom
    (``description="Fate conflict opponent"``, no pronouns, ``created=True``) and
    the cattle-baron the player has been talking to is discarded. After the
    promotion fix the seater BINDS to the established identity
    (``created=False``), carrying the narrated pronouns/appearance.

    No pack is threaded into ``_apply_npc_mentions`` so the raw narrator name is
    preserved (this isolates the seating residual from the namegen reroute that
    manifestation (c) covers).
    """
    from sidequest.genre import load_genre_pack

    pack = load_genre_pack(SPAGHETTI_WESTERN_DIR)
    assert pack.rules is not None and pack.rules.ruleset == "fate", (
        "precondition: spaghetti_western must bind the Fate ruleset"
    )

    snapshot = GameSnapshot(genre_slug="spaghetti_western", world_slug="dust_and_lead")
    snapshot.character_locations["Rux"] = "Sangre del Paso"

    narrated_appearance = "a weathered cattle baron in a black frock coat"
    # The narrator establishes the antagonist as a named, gendered, described
    # person and marks them the party's opponent.
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[
            _mention(
                "Henry Shaw",
                role="cattle baron",
                pronouns="he/him",
                appearance=narrated_appearance,
                side="opponent",
            )
        ],
        turn_num=1,
        acting_character_name="Rux",
    )

    # The Fate conflict seats an opponent of that exact name (router free-string
    # -> EncounterActor). This is where the residual fires.
    _seed_fate_opponents(
        snapshot=snapshot,
        actors=[EncounterActor(name="Henry Shaw", role="cattle baron", side="opponent")],
        pack=pack,
        turn=1,
        acting_character_name="Rux",
    )

    seated = _npcs_named(snapshot, "Henry Shaw")
    assert len(seated) == 1, (
        "exactly one 'Henry Shaw' identity must be seated; got "
        f"{len(seated)} (a phantom seated beside the narrated cast member is a "
        "split identity)"
    )
    opponent = seated[0]
    # Identity carried, not a hollow phantom.
    assert opponent.pronouns == "he/him", (
        "the seated Fate opponent must carry the narrated pronouns; a phantom "
        f"mint loses them (got {opponent.pronouns!r})"
    )
    assert opponent.appearance == narrated_appearance, (
        "the seated Fate opponent must carry the narrated appearance, not a "
        f"generic stub (got {opponent.appearance!r})"
    )
    assert opponent.core.description != "Fate conflict opponent", (
        "a 'Fate conflict opponent' description is the hollow-phantom tell — the "
        "seater fabricated a new identity instead of binding the narrated one"
    )
    # The opponent still gets a Fate sheet (the seater's job) — binding must not
    # cost the mechanical surface.
    assert opponent.core.fate_sheet is not None, (
        "the bound opponent must still carry a Fate sheet for the conflict engine"
    )

    # OTEL contract: the seeded span must report it BOUND an existing identity
    # (created=False), not minted a phantom (created=True). This is the
    # GM-panel lie-detector for the residual.
    seeded_spans = _attrs_for(otel_capture, FATE_OPPONENT_SEEDED_SPAN)
    henry_spans = [s for s in seeded_spans if s.get("opponent") == "Henry Shaw"]
    assert henry_spans, f"a {FATE_OPPONENT_SEEDED_SPAN} span must fire for the seated opponent"
    assert henry_spans[-1].get("created") is False, (
        "the seater must BIND to the narrated identity (created=False), not "
        "fabricate a phantom (created=True)"
    )


# ===========================================================================
# Manifestation (b) — canonical figure binds to seeded registry NPC
# ===========================================================================


def test_canonical_figure_binds_to_registry_not_hollow_mint(otel_capture) -> None:
    """A narrator reference to a runtime-wired authored figure must reconcile to
    the seeded registry NPC, not mint a hollow culture-routed duplicate.

    Models the oz repro: "The Good Witch of the North" is a runtime-wired roster
    NPC (ADR-059 npcs.yaml: she/her, disposition 25). The narrator re-introduces
    the canonical figure ("the Good Witch of the North") on a later turn. Today
    the bare-string reference misses the name-only Step 1/2 lookups and falls to
    the Step-3 person-mint, which culture-routes a phantom name ("Amaranth
    Warmacre") — the player sees a stranger where the kindly Witch should stand,
    and her authored disposition/pronouns are lost.

    The binder must collapse the reference onto the registry identity and carry
    its pronouns + disposition. Mechanism (epithet/canonical reconciliation vs
    registry promotion) is Dev's choice; this asserts only the outcome.

    DESIGN-GATED (session finding, blocking): the exact "seeded registry" lookup
    trigger is unconfirmed against the playtest repro. This pins the contract.
    """
    registry_witch = Npc(
        core=CreatureCore(
            name="The Good Witch of the North",
            description="A little old woman, kindly and slightly muddled.",
            personality="kindly",
        ),
        pronouns="she/her",
        appearance="a little old woman in a white gown hung with tiny bells",
        disposition=25,
    )
    snapshot = GameSnapshot(genre_slug="wry_whimsy", world_slug="oz", npcs=[registry_witch])

    # The narrator names the canonical figure by a near-canonical reference (no
    # leading article) — a routine narrator variance the name-only lookups miss.
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Good Witch of the North", role="mentor", pronouns="she/her")],
        turn_num=4,
        acting_character_name="Dorothy",
    )

    # No hollow duplicate may be minted: the cast still holds a SINGLE Good Witch
    # identity and the narrator-invented pool stays empty (no "Amaranth Warmacre").
    invented = [m for m in snapshot.npc_pool if m.drawn_from == "narrator_invented"]
    assert invented == [], (
        "the canonical figure must bind to the seeded registry NPC, not mint a "
        f"hollow culture-routed duplicate; minted {[m.name for m in invented]!r}"
    )
    witches = [n for n in snapshot.npcs if "good witch of the north" in n.core.name.casefold()]
    assert len(witches) == 1, (
        f"exactly one Good-Witch-of-the-North identity must exist; got {len(witches)}"
    )
    # Authored identity preserved through the bind.
    assert witches[0].pronouns == "she/her"
    assert int(witches[0].disposition) == 25, (
        "the registry NPC's authored disposition must survive the reconciliation, "
        f"not flatten to a fresh-mint neutral (got {int(witches[0].disposition)})"
    )


# ===========================================================================
# Manifestation (c) — namegen does not blind-shuffle a name to the wrong people
# ===========================================================================


@requires_content
def test_invented_name_not_shuffle_routed_to_wrong_people_group(otel_capture) -> None:
    """A narrator-supplied proper name must not be blind-shuffled to a culturally
    incongruous people-group.

    ``dust_and_lead`` binds three cultures: Sangre Anglo (settlers/lawmen),
    Sangre Frontera, and Ndé (Apache). The starting region (``sangre_del_paso``)
    is Anglo-settler frontier. When the narrator names an unaffiliated stranger
    and the route has no deterministic self-match, today's
    ``_resolve_invented_naming_context`` blind-``random.shuffle``s the bound
    cultures and takes the first that builds — so the same Anglo-frontier
    stranger is rerouted to an Apache (Ndé) name. The
    ``npc.invented_name_routed`` span records ``resolution_strategy`` and the
    chosen ``culture``; the defect is the blind ``shuffle_fallback`` landing on a
    people-group that does not fit the current region.

    The route must be keyed to the current region instead of blind-shuffled.
    This asserts the OBSERVABLE contract (no blind shuffle for a named stranger
    in the Anglo region) — the region->culture mechanism is Dev's choice.

    DESIGN-GATED (session finding, blocking): there is no region->culture binding
    in the world content today, so "key the mint to the current region" requires
    a design decision. This test pins the contract that motivates it.
    """
    import random

    from sidequest.genre import load_genre_pack
    from sidequest.server.session_handler import _apply_narration_result_to_snapshot
    from tests._helpers.session_room import room_for

    pack = load_genre_pack(SPAGHETTI_WESTERN_DIR)
    cultures, _ = pack.effective_cultures("dust_and_lead")
    culture_names = {c.name for c in cultures}
    assert "Ndé" in culture_names, (
        "precondition: dust_and_lead must bind the Ndé culture (the wrong-people "
        f"reroute target); bound: {sorted(culture_names)}"
    )

    snapshot = GameSnapshot(genre_slug="spaghetti_western", world_slug="dust_and_lead")
    snapshot.character_locations["Rux"] = "Sangre del Paso"

    # Seed RNG so the blind shuffle deterministically lands on a wrong-people
    # routing if the route is still shuffle-based. The fix keys to the region and
    # ignores this shuffle entirely.
    random.seed(1)

    from sidequest.agents.orchestrator import NarrationTurnResult

    result = NarrationTurnResult(
        narration="A marshal steps off the noon train into the dust.",
        npcs_present=[_mention("Marshal Tate Buckley", role="lawman", pronouns="he/him")],
        is_degraded=False,
    )
    _apply_narration_result_to_snapshot(
        snapshot,
        result,
        "player",
        room=room_for(snapshot, slug="dust_and_lead"),
        pack=pack,
        world="dust_and_lead",
        acting_character_name="Rux",
    )

    routed = _attrs_for(otel_capture, ROUTED_SPAN)
    assert routed, (
        "the invented-name route must fire its provenance span for a narrator-named stranger"
    )
    strategy = routed[-1].get("resolution_strategy")
    assert strategy != "shuffle_fallback", (
        "a narrator-named stranger in the Anglo-settler frontier must be routed "
        "by a region-keyed strategy, not the blind people-group shuffle "
        f"(got resolution_strategy={strategy!r}, culture={routed[-1].get('culture')!r})"
    )
