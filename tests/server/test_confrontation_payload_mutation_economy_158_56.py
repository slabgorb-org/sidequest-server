"""Story 158-56 (server half) — the CONFRONTATION payload projects the
recipient's owned-mutation economy so the overlay can render the mutation
picker (the spell-picker twin, story 102-2).

158-54 landed the dice-path mutation route: a ``mutation_resolution``-marked
beat commit carrying ``DiceThrowPayload.mutation_id`` routes the AWN use spine
(``awn.mutation.used`` + Strain/usage economy). But no UI can send that id —
the confrontation overlay has no mutation picker, and (this story's finding)
the owned-mutation list is not projected to the client AT ALL. ``spellcasting``
(102-2) is the precedent: the server DERIVES the caster economy inside
``build_confrontation_payload`` and projects it as a block on the CONFRONTATION
payload; the overlay's "Work a Spell" picker reads that block. There is no
mutation twin — this suite pins it.

Contract:

  - A recipient who OWNS positive mutations gets a ``mutation_economy`` block:
    ``{"owned": [{"id", "name", "strain_cost"}, ...]}`` — ``id`` rides
    ``mutation_id`` on the commit, ``name`` is the picker label, and
    ``strain_cost`` is the player-visible spend math (Sebastien/Jade
    legibility, the ``casts_remaining`` analog).
  - The block is SCOPED to the recipient: a different mutant seated in the same
    ``MutationState`` must not leak into this recipient's picker (the 162-10
    lesson — resolve the entity, not grab-all).
  - A recipient with no owned positives — or a non-AWN pack with no
    ``mutation_state``/catalog — gets ``None``, never a fabricated empty
    economy (No Silent Fallbacks; mirrors spellcasting's non-caster ``None``).
  - ``ConfrontationPayload`` (``extra="forbid"``) accepts the
    ``mutation_economy`` key, so the block actually reaches the client instead
    of crashing the broadcast.

Uses the REAL mutant_wasteland pack (AWN; its "Wasteland Brawl" carries the
``mutant_ability`` / ``mutation_resolution: true`` beat and a mutations.yaml
catalog); skips when sidequest-content is not on disk.
"""

from __future__ import annotations

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, PackNotFound, find_pack_path

pytestmark = pytest.mark.skipif(
    not GENRE_PACKS_DIR.is_dir(), reason="sidequest-content not on disk"
)


def _load_mutant_wasteland():
    from sidequest.genre.loader import load_genre_pack

    try:
        return load_genre_pack(find_pack_path("mutant_wasteland"))
    except PackNotFound:  # pragma: no cover - environment guard
        pytest.skip("sidequest-content not on disk in this checkout")


def _combat_cdef(pack):
    cdef = next(
        (c for c in pack.rules.confrontations if c.confrontation_type == "combat"),
        None,
    )
    assert cdef is not None, "mutant_wasteland must expose a 'combat' confrontation"
    return cdef


def _catalog(pack):
    cat = pack.mutations
    assert cat is not None and len(cat.positives) >= 2, (
        "mutant_wasteland must ship a mutations.yaml catalog with >=2 positives"
    )
    return cat


def _make_encounter(player: str, opponent: str):
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )

    return StructuredEncounter(
        encounter_type="combat",
        player_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        opponent_metric=EncounterMetric(name="momentum", current=0, starting=0, threshold=7),
        beat=0,
        structured_phase=EncounterPhase.Setup,
        secondary_stats=None,
        actors=[
            EncounterActor(name=player, role="combatant", side="player"),
            EncounterActor(name=opponent, role="combatant", side="opponent"),
        ],
        outcome=None,
        resolved=False,
        mood_override=None,
        narrator_hints=[],
    )


def _state(owned: dict[str, list[str]]):
    from sidequest.mutation.state import CharacterMutationState, MutationState

    return MutationState(
        characters={
            actor: CharacterMutationState(mp_remaining=0, positive_ids=list(ids))
            for actor, ids in owned.items()
        }
    )


def _build(pack, *, actor, mutation_state, mutation_catalog):
    from sidequest.server.dispatch.confrontation import build_confrontation_payload

    return build_confrontation_payload(
        encounter=_make_encounter(actor, "Feral Raider"),
        cdef=_combat_cdef(pack),
        genre_slug="mutant_wasteland",
        recipient_actor_name=actor,
        mutation_state=mutation_state,
        mutation_catalog=mutation_catalog,
    )


def test_payload_projects_owned_mutations_for_a_mutant():
    """A mutant with owned positives must see them on the wire — the picker
    cannot render what the server hides."""
    pack = _load_mutant_wasteland()
    cat = _catalog(pack)
    owned = cat.positives[0]
    payload = _build(
        pack,
        actor="Rust",
        mutation_state=_state({"Rust": [owned.id]}),
        mutation_catalog=cat,
    )

    block = payload.get("mutation_economy")
    assert block is not None, (
        "a mutant recipient's CONFRONTATION payload must carry a "
        "'mutation_economy' block — the overlay picker has no other source "
        f"for owned mutations; payload keys: {sorted(payload.keys())}"
    )
    assert block["owned"] == [
        {"id": owned.id, "name": owned.name, "strain_cost": owned.strain_cost}
    ]


def test_mutation_economy_is_scoped_to_the_recipient_not_the_whole_state():
    """A second mutant in the same MutationState must not leak into this
    recipient's picker (the 162-10 lesson: resolve the entity, not grab-[0]/all)."""
    pack = _load_mutant_wasteland()
    cat = _catalog(pack)
    mine, theirs = cat.positives[0], cat.positives[1]
    payload = _build(
        pack,
        actor="Rust",
        mutation_state=_state({"Rust": [mine.id], "Chrome": [theirs.id]}),
        mutation_catalog=cat,
    )

    ids = [m["id"] for m in payload["mutation_economy"]["owned"]]
    assert ids == [mine.id], (
        f"only the recipient's owned mutations may project; got {ids} "
        f"(the decoy {theirs.id!r} leaked in from actor 'Chrome')"
    )


def test_payload_omits_mutation_economy_for_a_non_mutant():
    """A recipient with no owned positives must get None — a fabricated empty
    economy would make the overlay render a dead picker (No Silent Fallbacks)."""
    pack = _load_mutant_wasteland()
    cat = _catalog(pack)
    payload = _build(
        pack,
        actor="Rust",
        mutation_state=_state({"Rust": []}),
        mutation_catalog=cat,
    )
    assert payload.get("mutation_economy") is None, (
        "a mutant who owns nothing must not receive a mutation_economy block; "
        f"got {payload.get('mutation_economy')!r}"
    )


def test_payload_omits_mutation_economy_without_state_or_catalog():
    """No mutation_state / no catalog (a non-AWN pack) keeps the block absent —
    the picker gates on the value, never on key presence."""
    pack = _load_mutant_wasteland()
    payload = _build(pack, actor="Rust", mutation_state=None, mutation_catalog=None)
    assert payload.get("mutation_economy") is None


def test_confrontation_payload_model_accepts_mutation_economy():
    """The block must survive ``ConfrontationPayload(**payload)`` — the model is
    ``extra="forbid"``, so an unpermitted key would raise at construction and
    crash the CONFRONTATION broadcast before the client ever sees the picker
    data. RED today: ``mutation_economy`` is an unpermitted extra key."""
    from sidequest.protocol.messages import ConfrontationPayload

    block = {"owned": [{"id": "structure/iron_hide", "name": "Iron Hide", "strain_cost": 1}]}
    model = ConfrontationPayload(
        type="combat",
        label="Wasteland Brawl",
        category="combat",
        genre_slug="mutant_wasteland",
        mutation_economy=block,
    )
    assert model.mutation_economy == block
