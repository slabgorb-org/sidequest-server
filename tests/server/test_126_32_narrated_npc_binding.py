"""Story 126-32: bind narrated NPCs to existing identities, not fabricated ones.

Two manifestations of the NPC-binding cluster (epic 126):

(a) **Narrated antagonist -> seated Fate opponent.** ``_seed_fate_opponents``
    (`sidequest/server/dispatch/encounter_lifecycle.py`) builds its opponent map
    from ``snapshot.npcs`` only. But confrontation seating runs in the
    pre-narrator dispatch bank (ADR-113), BEFORE that turn's post-narrator
    ``_apply_npc_mentions`` mint — so a narrated antagonist established on a prior
    turn lives in ``snapshot.npc_pool``, invisible to the seater. It fabricated a
    phantom ``Npc`` (``description="Fate conflict opponent"``, no pronouns,
    ``created=True``) beside the cattle-baron the player had been talking to. The
    fix consults the pool and promotes the narrated identity (``created=False``).

(b) **Active-conversation person -> existing identity (recency scene-guard).**
    The oz repro (Keith, 2026-06-20): while the player was talking to "The Good
    Witch of the North", the narrator's re-reference missed the name-only Steps
    1/2 (a title is not the personal name the namegen would mint) and the Step-3
    person-mint culture-routed a STRANGER ("Amaranth Warmacre") standing next to
    her. There is a creature scene-guard (`_reconcile_ongoing_threat`: "one
    active creature in scene") but no person equivalent. The fix adds one: a
    non-new (``is_new=False``) person reference in a scene with a recently-engaged
    person reconciles to that person instead of minting. A genuine new arrival
    (``is_new=True``) still mints (Living World).

Manifestation (c) (namegen region->culture routing) was SPLIT to a follow-up
story by Keith on 2026-06-20 — it needs a region->culture content mapping that
does not exist today. Its RED-phase contract is preserved in git (commit
64c60385).

Test doctrine (mirrors test_npc_ongoing_threat_reconciliation.py): assert
BEHAVIOR (existing identity kept; no fabricated stranger) plus the OTEL CONTRACT,
driving the REAL production seams. Spans are referenced by string literal so a
failure is behavioral, never a collection-time ImportError.
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
    is_new: bool = False,
) -> NpcMention:
    return NpcMention(
        name=name,
        role=role,
        pronouns=pronouns,
        appearance=appearance,
        side=side,
        is_creature=is_creature,
        is_new=is_new,
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

    Drives the production ordering: a prior turn's narration named "Henry Shaw"
    (side="opponent", he/him, with appearance) which lives in
    ``snapshot.npc_pool``; then the Fate conflict seats an opponent of that exact
    name. Today the narrated identity sits in the pool while
    ``_seed_fate_opponents`` reads only ``snapshot.npcs`` — so it CREATES a
    phantom (``description="Fate conflict opponent"``, no pronouns,
    ``created=True``) and the cattle-baron the player has been talking to is
    discarded. The fix consults the pool and BINDS the established identity
    (``created=False``), carrying the narrated pronouns/appearance.

    No pack is threaded into ``_apply_npc_mentions`` so the raw narrator name is
    preserved (the namegen reroute is split out to a follow-up story).
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
    # person and marks them the party's opponent (lands in the pool).
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


@requires_content
def test_novel_fate_opponent_with_no_pool_member_still_seeds_phantom(otel_capture) -> None:
    """Guard the unchanged path: a truly-novel opponent with NO narrated pool
    identity still gets a seeded ephemeral stub (``created=True``).

    The 126-32 fix only redirects the seater when a matching pool member exists;
    a name the narrator never established must still seat a loud, ephemeral Fate
    stub (No Silent Fallbacks — the conflict engine needs an Other with a sheet).
    """
    from sidequest.genre import load_genre_pack

    pack = load_genre_pack(SPAGHETTI_WESTERN_DIR)
    snapshot = GameSnapshot(genre_slug="spaghetti_western", world_slug="dust_and_lead")
    snapshot.character_locations["Rux"] = "Sangre del Paso"

    _seed_fate_opponents(
        snapshot=snapshot,
        actors=[EncounterActor(name="Nobody In Particular", role="foe", side="opponent")],
        pack=pack,
        turn=1,
        acting_character_name="Rux",
    )

    seated = _npcs_named(snapshot, "Nobody In Particular")
    assert len(seated) == 1
    assert seated[0].core.fate_sheet is not None
    assert seated[0].ephemeral is True, (
        "a fabricated stub must be ephemeral (reaped with the encounter)"
    )

    seeded_spans = _attrs_for(otel_capture, FATE_OPPONENT_SEEDED_SPAN)
    novel = [s for s in seeded_spans if s.get("opponent") == "Nobody In Particular"]
    assert novel and novel[-1].get("created") is True, (
        "a truly-novel opponent with no pool identity must still seed a phantom (created=True)"
    )


# ===========================================================================
# Manifestation (b) — active-conversation person scene-guard
# ===========================================================================

PERSON_RECONCILED_SPAN = "npc.person_reconciled"


def _active_witch() -> Npc:
    """The kindly Good Witch of the North, present in the player's scene and
    engaged on a prior turn (the active conversation partner)."""
    return Npc(
        core=CreatureCore(
            name="The Good Witch of the North",
            description="A little old woman, kindly and slightly muddled.",
            personality="kindly",
        ),
        pronouns="she/her",
        appearance="a little old woman in a white gown hung with tiny bells",
        disposition=25,
        last_seen_location="Munchkin Country",
        last_seen_turn=3,
    )


def test_active_conversation_person_reconciles_not_stranger_mint(otel_capture) -> None:
    """A non-new person reference, in a scene with one recently-engaged person,
    must reconcile to that person — not mint a stranger beside them.

    The oz repro: talking to "The Good Witch of the North", the narrator's
    re-reference (a name/title that misses the exact name-only Steps 1/2) today
    falls to the Step-3 person-mint and culture-routes "Amaranth Warmacre", a
    stranger conjured into the room. The recency scene-guard must collapse the
    reference onto the active conversation partner instead.
    """
    witch = _active_witch()
    snapshot = GameSnapshot(genre_slug="wry_whimsy", world_slug="oz", npcs=[witch])
    snapshot.character_locations["Dorothy"] = "Munchkin Country"

    # The narrator re-refers to the witch under a name that misses Steps 1/2 and
    # is NOT flagged as a new arrival. No pack threaded, so absent the fix the
    # raw string mints verbatim (the stranger) rather than culture-routing.
    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Locasta", role="kindly witch", is_new=False)],
        turn_num=4,
        acting_character_name="Dorothy",
    )

    invented = [m for m in snapshot.npc_pool if m.drawn_from == "narrator_invented"]
    assert invented == [], (
        "a non-new reference in a one-person scene must reconcile to the active "
        f"conversation partner, not mint a stranger; minted {[m.name for m in invented]!r}"
    )
    persons = [n for n in snapshot.npcs if n.creature_id is None]
    assert len(persons) == 1 and persons[0].core.name == "The Good Witch of the North", (
        "the single scene person must survive unchanged — no phantom forked beside her"
    )

    spans = _attrs_for(otel_capture, PERSON_RECONCILED_SPAN)
    assert spans, f"a {PERSON_RECONCILED_SPAN} span must fire (GM-panel lie detector)"
    assert spans[-1].get("reconciled_to") == "The Good Witch of the North"
    assert spans[-1].get("signal") == "scene_guard"


def test_genuinely_new_arrival_still_mints(otel_capture) -> None:
    """The new-arrival guard: a mention flagged ``is_new=True`` must still mint a
    new person even with an NPC active in scene — the narrator can introduce
    arrivals (Living World), and the scene-guard must never silently swallow one.
    """
    witch = _active_witch()
    snapshot = GameSnapshot(genre_slug="wry_whimsy", world_slug="oz", npcs=[witch])
    snapshot.character_locations["Dorothy"] = "Munchkin Country"

    _apply_npc_mentions(
        snapshot=snapshot,
        mentions=[_mention("Boq", role="munchkin farmer", pronouns="he/him", is_new=True)],
        turn_num=4,
        acting_character_name="Dorothy",
    )

    minted = [m for m in snapshot.npc_pool if m.name == "Boq"]
    assert len(minted) == 1, (
        "is_new=True is the new-arrival cue — it must mint a fresh person, never "
        "reconcile onto the active NPC"
    )
    assert _attrs_for(otel_capture, PERSON_RECONCILED_SPAN) == [], (
        "the scene-guard must not fire for a flagged new arrival"
    )
