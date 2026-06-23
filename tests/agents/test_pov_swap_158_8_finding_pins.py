"""Story 158-8 — regression pins for Facet 1 (agreement break), GREEN.

The 2026-06-22 playtest finding reported a person-AGREEMENT break in the POV
localizer: a PC name swapped to "you" left the dependent gendered pronouns
(he/him/his) 3rd-person, producing "You ... his ... him". It reported the same
break in SOLO, where the narrator converts a 3rd-person-authored action to 2nd
person within one passage.

Measured 2026-06-23: this facet is ALREADY FIXED by story 153-29 (commit
8dec6bcd, in develop). The finding's repro was a stale-tree artifact (driver was
on d19afd32, several commits behind). Per the 158-8 scope decision ("Facet 2
only + pin Facet 1"), these tests LOCK IN that fix against the *exact* repro
strings from the finding so a future change to the localizer cannot silently
re-open the agreement break. They are expected to PASS.

The genuine, unimplemented gap (Facet 2 — per-recipient re-anchor) is pinned by
the RED tests in tests/server/test_narration_pov_emission.py (Story 158-8
section). Do NOT move Facet-1 work here; it is done.
"""

from __future__ import annotations

import pytest

from sidequest.agents.pov_swap import swap_to_second_person


def test_158_8_facet1_mp_repro_carries_object_and_possessive_pronouns() -> None:
    """Finding MP repro (turn 2): 'Groucho grabs the rope ... through his
    gloves ... past him' must localize his->your and him->you when Groucho is
    swapped to 'you' — no 'You ... his ... him' disagreement."""
    text = (
        "Groucho grabs the rope a beat later and follows, the wet hemp cold "
        "through his gloves, the draught pushing up past him."
    )
    out, swaps = swap_to_second_person(text, target_name="Groucho", pronouns="he/him")
    assert "through your gloves" in out, f"possessive his->your must carry; got: {out!r}"
    assert "past you" in out, f"object him->you must carry; got: {out!r}"
    assert "his gloves" not in out and "past him" not in out, (
        f"no residual 3rd-person pronoun for the swapped PC; got: {out!r}"
    )
    assert swaps >= 3


def test_158_8_facet1_solo_repro_carries_reflexive_and_subject_pronouns() -> None:
    """Finding SOLO repro (697cbc14): a 3rd-person-authored solo action
    converted to 2nd person must carry the reflexive (himself->yourself) and
    subsequent subject/object pronouns — same localizer path as MP, so the
    solo 'reconnect/replay variant' is covered by the same fix."""
    text = "Groucho lowers himself, swinging his legs over the collar as he takes the rope."
    out, swaps = swap_to_second_person(text, target_name="Groucho", pronouns="he/him")
    assert "lower yourself" in out, f"reflexive himself->yourself must carry; got: {out!r}"
    assert "your legs" in out, f"possessive his->your must carry; got: {out!r}"
    assert "as you take" in out, f"subject he->you (+verb) must carry; got: {out!r}"
    assert "himself" not in out and "his legs" not in out, (
        f"no residual 3rd-person pronoun for the swapped PC; got: {out!r}"
    )
    assert swaps >= 4


def test_158_8_facet1_solo_mid_sentence_conversion_after_npc_clause() -> None:
    """Finding's exact broken solo string shape: 'Tork watches ... as Groucho
    swings his legs ... against him as he takes the rope.' The NPC (Tork) stays
    3rd-person while Groucho and his dependent pronouns all become 2nd person."""
    text = (
        "Tork watches without expression as Groucho swings his legs over the collar, "
        "the shaft breathing up against him as he takes the rope."
    )
    out, _ = swap_to_second_person(text, target_name="Groucho", pronouns="he/him")
    assert out.startswith("Tork watches without expression as you swing"), (
        f"NPC subject (Tork) must stay 3rd person; PC must swap; got: {out!r}"
    )
    assert "your legs" in out and "against you" in out and "as you take" in out, (
        f"all of Groucho's dependent pronouns must carry to 2nd person; got: {out!r}"
    )
    assert "his legs" not in out and "against him" not in out, (
        f"no residual 3rd-person pronoun for Groucho; got: {out!r}"
    )


def test_158_8_facet1_female_pc_agreement() -> None:
    """she/her variant (the non-anchor recipient in the Facet 2 tests is
    she/her): 'Harpo ... her hands ... as she leans' -> full 2nd-person."""
    text = (
        "Harpo moves to the winch without a word, her hands finding the crank as she leans into it."
    )
    out, _ = swap_to_second_person(text, target_name="Harpo", pronouns="she/her")
    assert "your hands" in out, f"possessive her->your must carry; got: {out!r}"
    assert "as you lean" in out, f"subject she->you (+verb) must carry; got: {out!r}"
    assert "Harpo" not in out and "her hands" not in out, (
        f"no residual 3rd-person reference to Harpo; got: {out!r}"
    )


@pytest.mark.parametrize(
    ("pronouns", "npc_subject"),
    [("he/him", "he"), ("she/her", "she"), ("they/them", "they")],
)
def test_158_8_facet1_clause_local_gate_does_not_bleed_npc_pronouns(
    pronouns: str, npc_subject: str
) -> None:
    """Belt-and-suspenders for the 153-29 clause-local gate that the Facet 2
    fix must not disturb: a subject pronoun in a clause with NO swapped PC name
    (an NPC referent) stays 3rd-person. The NPC word matches the PC's pronoun
    set so the gate is genuinely exercised (a non-matching word would pass
    vacuously)."""
    text = f"Vesna lifts the lantern; {npc_subject} does not look up from the ledger."
    out, _ = swap_to_second_person(text, target_name="Vesna", pronouns=pronouns)
    assert out.startswith("You lift the lantern;"), f"PC clause must swap; got: {out!r}"
    # The post-';' clause has no PC name -> its subject pronoun is an NPC
    # referent and must NOT be rewritten (the deliberate NPC-bleed guard).
    assert f"{npc_subject} does not look up" in out, (
        f"NPC pronoun in a name-less sibling clause must stay 3rd person; got: {out!r}"
    )
