"""Epithet mint guard — descriptive epithets must not become phantom identities.

sq-playtest 2026-06-07 (five_points-2 turns 4-6): the narrator emitted the
mention "The Heavy Man in Broadcloth" — a DESCRIPTION of roster NPC Isaiah
Rynders, already referenced, present, and explicitly bound in the fiction
("That's Rynders. Isaiah Rynders.") — and the Step-3 novel branch routed it
through the culture-bound person namer, minting phantom "Deacon Rutherford
Lacy". The standoff then seated the phantom as the Other: disposition,
recurrence, and relationship ledgers tracked a ghost while the world's
principal authored antagonist's ledger never moved, and the narrator retconned
the opponent's identity mid-scene to obey state.

Two defects pinned here (ping-pong entry defects a + b):

  (a) **Descriptive epithets are not mintable names.** A mention whose name is
      article-led ("The Heavy Man in Broadcloth", "A Hooded Stranger") is a
      descriptor, not an identity — the culture-bound person namer must
      decline, exactly as it declines creatures (``npc.creature_preserved``
      precedent). The epithet is preserved verbatim so the registry shows an
      honest descriptor instead of a fake culture-minted full name with fake
      provenance.

  (b) **Coreference against existing state before inventing an identity.** An
      epithet whose meaningful tokens overlap an existing person's identity
      (name/role/appearance) is a RE-DESCRIPTION of that person, not a new
      figure — reconcile to the existing identity instead of forking it
      (``npc.creature_reconciled`` precedent, story 83-3). Conservative: a
      clear, unique token overlap (>=2 shared meaningful tokens) is required —
      never a false merge on a single generic word.

Span contract (referenced as string literals so RED failures are behavioral):
``npc.epithet_preserved`` for the declined-namer leg, ``npc.epithet_reconciled``
(incoming / reconciled_to / signal / target_store) for the coreference leg.
Tests drive the REAL production seam ``_apply_npc_mentions``.
"""

from __future__ import annotations

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.creature_core import CreatureCore
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot, Npc
from sidequest.genre.names.generator import NameGenerator
from sidequest.server.narration_apply import (
    _apply_npc_mentions,  # noqa: PLC2701 — driving the real production seam
)

EPITHET_PRESERVED_SPAN = "npc.epithet_preserved"
EPITHET_RECONCILED_SPAN = "npc.epithet_reconciled"
ROUTED_SPAN = "npc.invented_name_routed"


class _SeqNameGenerator(NameGenerator):
    """Deterministic stand-in counting ``generate_person`` consultations
    (mirrors tests/server/test_npc_invented_namegen_routing.py)."""

    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self._iter = iter(names)
        self.person_calls = 0

    def generate_person(self, pattern: str | None = None) -> str:  # noqa: ARG002
        self.person_calls += 1
        return next(self._iter)


def _core(name: str) -> CreatureCore:
    return CreatureCore(name=name, description="X.", personality="Y.")


def _rynders() -> Npc:
    """The five_points repro shape: the authored machine boss, already on the
    roster, whose look the narrator re-described as an epithet."""
    return Npc(
        core=_core("Isaiah Rynders"),
        pronouns="he/him",
        appearance="a heavy man in immaculate broadcloth, gold watch chain",
    )


def _epithet_mention(
    name: str = "The Heavy Man in Broadcloth",
    *,
    is_new: bool = True,
    role: str = "",
    appearance: str = "",
    pronouns: str = "",
) -> NpcMention:
    return NpcMention(
        name=name,
        role=role,
        appearance=appearance,
        pronouns=pronouns,
        is_new=is_new,
    )


def _attrs_for(otel_capture, span_name: str) -> list[dict]:
    return [
        dict(s.attributes or {}) for s in otel_capture.get_finished_spans() if s.name == span_name
    ]


# ===========================================================================
# Defect (a) — an epithet never reaches the person namer.
# ===========================================================================


def test_epithet_is_never_routed_through_person_namer(otel_capture) -> None:
    """The repro's worst symptom: "The Heavy Man in Broadcloth" must not mint
    "Deacon Rutherford Lacy". With no reconcile target, the epithet is
    preserved VERBATIM as the pool member's name, the namer is never
    consulted, and ``npc.epithet_preserved`` records the decline."""
    gen = _SeqNameGenerator(["Deacon Rutherford Lacy"])
    snap = GameSnapshot()

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_epithet_mention()],
        turn_num=4,
        name_generator=gen,
        culture_name="Irish Catholic",
        culture_source="world",
    )

    assert gen.person_calls == 0, (
        "the culture-bound person namer must DECLINE a descriptive epithet "
        f"(consulted {gen.person_calls}x)"
    )
    assert [m.name for m in snap.npc_pool] == ["The Heavy Man in Broadcloth"], (
        "the epithet must be preserved verbatim — an honest descriptor in the "
        f"registry, not a phantom full name; got {[m.name for m in snap.npc_pool]}"
    )
    assert _attrs_for(otel_capture, ROUTED_SPAN) == [], (
        "no invented-name reroute span may fire for an epithet"
    )
    preserved = _attrs_for(otel_capture, EPITHET_PRESERVED_SPAN)
    assert len(preserved) == 1
    assert preserved[0]["npc_name"] == "The Heavy Man in Broadcloth"


def test_real_person_names_still_route_through_namer(otel_capture) -> None:
    """Guard the guard: a genuine novel person name (no leading article) keeps
    the ADR-091 culture-bound mint exactly as before."""
    gen = _SeqNameGenerator(["Veyra Solnë"])
    snap = GameSnapshot()

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_epithet_mention("Bob Hegemonic")],
        turn_num=3,
        name_generator=gen,
        culture_name="Spacer",
        culture_source="world",
    )

    assert gen.person_calls == 1
    assert [m.name for m in snap.npc_pool] == ["Veyra Solnë"]
    assert _attrs_for(otel_capture, EPITHET_PRESERVED_SPAN) == []


# ===========================================================================
# Defect (b) — coreference: an epithet overlapping an existing person's
# identity reconciles to it instead of forking a phantom.
# ===========================================================================


def test_epithet_reconciles_to_described_roster_npc(otel_capture) -> None:
    """The five_points-2 repro: Rynders is on the roster with the broadcloth
    look; the narrator re-describes him as "The Heavy Man in Broadcloth". The
    epithet must collapse onto Rynders — no new pool member, last_seen
    stamped, and ``npc.epithet_reconciled`` proving the coreference."""
    snap = GameSnapshot()
    rynders = _rynders()
    snap.npcs.append(rynders)

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_epithet_mention()],
        turn_num=4,
    )

    assert snap.npc_pool == [], (
        "the epithet describes an existing roster NPC — it must NOT fork a new "
        f"pool identity; got {[m.name for m in snap.npc_pool]}"
    )
    assert rynders.last_seen_turn == 4, "the reconciled cite stamps presence on the real NPC"
    reconciled = _attrs_for(otel_capture, EPITHET_RECONCILED_SPAN)
    assert len(reconciled) == 1
    attrs = reconciled[0]
    assert attrs["incoming"] == "The Heavy Man in Broadcloth"
    assert attrs["reconciled_to"] == "Isaiah Rynders"
    assert attrs["target_store"] == "npcs"


def test_epithet_reconciles_via_accreted_alias(otel_capture) -> None:
    """ADR-118 §A4 aliases are coreference evidence: an NPC promoted with the
    accreted epithet-alias "the heavy man" reconciles a later "The Heavy Man
    in Broadcloth" mention even when the appearance field carries no
    overlapping tokens — the fiction's own binding, made actionable."""
    snap = GameSnapshot()
    rynders = Npc(
        core=_core("Isaiah Rynders"),
        pronouns="he/him",
        appearance="immaculate tailoring, gold watch chain",
        aliases=["the heavy man", "the boss of the points"],
    )
    snap.npcs.append(rynders)

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_epithet_mention()],
        turn_num=7,
    )

    assert snap.npc_pool == [], "the alias-bound epithet must not fork a phantom"
    reconciled = _attrs_for(otel_capture, EPITHET_RECONCILED_SPAN)
    assert len(reconciled) == 1
    assert reconciled[0]["reconciled_to"] == "Isaiah Rynders"


def test_epithet_reconciles_to_pool_member(otel_capture) -> None:
    """Same coreference against the pool tier: a prior pool member with the
    overlapping look absorbs the re-description (fill-empty upsert, story
    72-7 additive precedent) instead of a duplicate mint."""
    snap = GameSnapshot()
    snap.npc_pool.append(
        NpcPoolMember(
            name="Silas Crane",
            role="dock foreman",
            appearance="a heavy man in patched broadcloth",
            drawn_from="narrator_invented",
        )
    )

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_epithet_mention(pronouns="he/him")],
        turn_num=6,
    )

    assert [m.name for m in snap.npc_pool] == ["Silas Crane"], (
        f"no duplicate mint; got {[m.name for m in snap.npc_pool]}"
    )
    assert snap.npc_pool[0].pronouns == "he/him", (
        "the re-description's pronouns fill the empty field (additive upsert)"
    )
    reconciled = _attrs_for(otel_capture, EPITHET_RECONCILED_SPAN)
    assert len(reconciled) == 1
    assert reconciled[0]["reconciled_to"] == "Silas Crane"
    assert reconciled[0]["target_store"] == "pool"


def test_dissimilar_epithet_stays_a_distinct_preserved_member(otel_capture) -> None:
    """Conservative guard: an epithet with NO meaningful overlap against any
    existing person must not merge — it lands as its own verbatim-preserved
    member (a genuinely new described figure stays new)."""
    snap = GameSnapshot()
    snap.npcs.append(
        Npc(
            core=_core("Mother Demus"),
            pronouns="she/her",
            appearance="a stooped washerwoman with knotted grey hair",
        )
    )

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_epithet_mention("The Hooded Stranger")],
        turn_num=2,
    )

    assert [m.name for m in snap.npc_pool] == ["The Hooded Stranger"]
    assert _attrs_for(otel_capture, EPITHET_RECONCILED_SPAN) == [], (
        "zero-overlap epithets must never false-merge"
    )
    assert len(_attrs_for(otel_capture, EPITHET_PRESERVED_SPAN)) == 1


def test_single_generic_token_overlap_does_not_merge(otel_capture) -> None:
    """One shared generic token ("man") is not coreference evidence — persons
    require a CLEAR overlap (>=2 meaningful tokens), stricter than the
    creature guard, because a false person-merge misattributes ledgers."""
    snap = GameSnapshot()
    snap.npcs.append(
        Npc(
            core=_core("Joachim Beckert"),
            pronouns="he/him",
            appearance="a wiry man with ink-stained fingers",
        )
    )

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_epithet_mention()],  # "The Heavy Man in Broadcloth" — only "man" overlaps
        turn_num=5,
    )

    assert [m.name for m in snap.npc_pool] == ["The Heavy Man in Broadcloth"], (
        "a single generic shared token must not collapse two different people"
    )
    assert _attrs_for(otel_capture, EPITHET_RECONCILED_SPAN) == []


def test_creature_epithets_keep_the_creature_path(otel_capture) -> None:
    """A creature mention with an article-led descriptive name stays on the
    ping-pong-#74 creature path (``npc.creature_preserved``) — the epithet
    guard is a person-branch concern and must not shadow it."""
    snap = GameSnapshot()

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[
            NpcMention(name="The Forest Lions", is_creature=True, is_new=True, side="opponent")
        ],
        turn_num=2,
    )

    assert [m.name for m in snap.npc_pool] == ["The Forest Lions"]
    assert snap.npc_pool[0].is_creature is True
    assert _attrs_for(otel_capture, EPITHET_PRESERVED_SPAN) == [], (
        "creature mentions are handled by npc.creature_preserved, not the epithet guard"
    )
