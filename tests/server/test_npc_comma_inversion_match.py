"""Playtest 2026-06-01 (the_real_mccoy): the invented-name router minted a
phantom pool duplicate of an already-rostered NPC.

Server log: ``npc.invented_name_routed original='Denis Gilligan'
minted='Colonel Phill'`` — the roster carried ``Gilligan, Denis`` (comma-
inverted, the old Markov dossier register) accreting real disposition state,
while the narrator's natural-order mention ``Denis Gilligan`` failed the
exact-casefold match in Steps 1/2 of ``_apply_npc_mentions`` and fell through to
Step 3, minting a fresh ``Colonel Phill`` pool member. One played identity, two
stored entities (ADR-072 split-identity-store).

The comma-inverted register is still live and possibly intentional
(``space_opera/coyote_star`` Hegemonic ``"{family_name}, {given_name}"``), so
the matcher must reconcile ``"Gilligan, Denis"`` ⇄ ``"Denis Gilligan"`` rather
than mint a phantom. These tests pin comma-inversion-aware matching on both the
roster (Step 1) and pool (Step 2) lookups, and guard against false positives.
"""

from __future__ import annotations

from sidequest.agents.orchestrator import NpcMention
from sidequest.game.creature_core import CreatureCore
from sidequest.game.npc_pool import NpcPoolMember
from sidequest.game.session import GameSnapshot, Npc
from sidequest.server.narration_apply import _apply_npc_mentions

REFERENCED_SPAN = "npc.referenced"


def _core(name: str) -> CreatureCore:
    return CreatureCore(name=name, description="X.", personality="Y.")


def _mention(name: str, *, role: str = "", pronouns: str = "") -> NpcMention:
    return NpcMention(name=name, role=role, pronouns=pronouns, appearance="")


def _attrs_for(otel_capture, span_name: str) -> list[dict]:
    return [
        dict(s.attributes or {}) for s in otel_capture.get_finished_spans() if s.name == span_name
    ]


def test_roster_comma_name_matches_natural_mention() -> None:
    """``Gilligan, Denis`` on the roster ⇄ ``Denis Gilligan`` mention → npcs_hit,
    no phantom pool mint."""
    snap = GameSnapshot()
    snap.npcs = [Npc(core=_core("Gilligan, Denis"))]

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_mention("Denis Gilligan")],
        turn_num=7,
    )

    # The rostered identity was matched, not duplicated.
    assert snap.npcs[0].last_seen_turn == 7
    assert snap.npc_pool == [], (
        "natural-order mention of a comma-inverted rostered NPC must reconcile to "
        f"the roster, not mint a phantom pool member; got {snap.npc_pool}"
    )


def test_pool_comma_name_matches_natural_mention() -> None:
    """``Vance, Konstantin`` in the pool ⇄ ``Konstantin Vance`` mention →
    pool_hit upsert, no second pool member."""
    snap = GameSnapshot()
    snap.npc_pool = [NpcPoolMember(name="Vance, Konstantin", drawn_from="narrator_invented")]

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_mention("Konstantin Vance", role="hegemon")],
        turn_num=3,
    )

    assert len(snap.npc_pool) == 1, (
        f"comma-inverted pool member must be matched, not duplicated; got {snap.npc_pool}"
    )
    assert snap.npc_pool[0].role == "hegemon", "upsert must land on the existing member"


def test_roster_natural_name_matches_comma_mention() -> None:
    """Symmetry: natural-order roster ⇄ comma-order mention also reconciles."""
    snap = GameSnapshot()
    snap.npcs = [Npc(core=_core("Denis Gilligan"))]

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_mention("Gilligan, Denis")],
        turn_num=4,
    )

    assert snap.npcs[0].last_seen_turn == 4
    assert snap.npc_pool == []


def test_distinct_names_do_not_falsely_match() -> None:
    """No false positive: a genuinely new name still mints (the matcher only
    reconciles a comma-flip of the SAME tokens, not unrelated names)."""
    snap = GameSnapshot()
    snap.npcs = [Npc(core=_core("Denis Gilligan"))]

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_mention("Colonel Phill")],
        turn_num=2,
    )

    # Unmatched → minted into the pool as before (no generator supplied → raw).
    assert any(m.name == "Colonel Phill" for m in snap.npc_pool), (
        "an unrelated name must still mint — comma-normalization must not over-match"
    )
    assert snap.npcs[0].last_seen_turn != 2, "the rostered NPC must not be touched"


def test_comma_match_records_match_form_on_referenced_span(otel_capture) -> None:
    """OTEL lie-detector: a comma-normalized reconciliation is observable — the
    ``npc.referenced`` span carries ``match_form="comma_normalized"`` so the GM
    panel can see a phantom mint was prevented."""
    snap = GameSnapshot()
    snap.npcs = [Npc(core=_core("Gilligan, Denis"))]

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_mention("Denis Gilligan")],
        turn_num=9,
    )

    refs = _attrs_for(otel_capture, REFERENCED_SPAN)
    npcs_hits = [a for a in refs if a.get("match_strategy") == "npcs_hit"]
    assert npcs_hits, "a comma-normalized roster match must still fire npc.referenced/npcs_hit"
    assert any(a.get("match_form") == "comma_normalized" for a in npcs_hits), (
        f"the reconciliation must be span-visible as comma_normalized; got {npcs_hits}"
    )


def test_exact_match_unaffected_records_exact_form(otel_capture) -> None:
    """Regression guard: an exact match still works and is tagged
    ``match_form="exact"`` (the normalization didn't change the happy path)."""
    snap = GameSnapshot()
    snap.npcs = [Npc(core=_core("Denis Gilligan"))]

    _apply_npc_mentions(
        snapshot=snap,
        mentions=[_mention("Denis Gilligan")],
        turn_num=1,
    )

    refs = _attrs_for(otel_capture, REFERENCED_SPAN)
    npcs_hits = [a for a in refs if a.get("match_strategy") == "npcs_hit"]
    assert npcs_hits
    assert any(a.get("match_form") == "exact" for a in npcs_hits)
