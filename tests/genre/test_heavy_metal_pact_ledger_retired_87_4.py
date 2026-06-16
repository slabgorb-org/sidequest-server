"""RED (story 87-4): the bespoke pact/ledger magic framing is retired.

Epic 87 binds heavy_metal to the ``wwn`` ruleset. Stories 87-1/87-2 landed the
binding, the five WWN Callings, and the High Magic spell catalog — but they
deliberately LEFT the over-flavored bespoke magic in place (content PR #358:
"Story-4 baggage (ledger/pact confrontations) left untouched").

Story 87-4 is the final integration gate. Per design decision D5
(``docs/superpowers/specs/2026-06-04-heavy-metal-wwn-port-design.md`` §3) the
doom-cost *feeling* re-homes into WWN Effort + System Strain + grim spell
descriptions; the bespoke *mechanics* are cut:

    - the ``pact_working`` ("Working the Rite") confrontation  → replaced by the
      live ``cast_spell`` beat
    - the ``debt_collection`` ("The Collector at the Door") confrontation → cut
    - the ``ledger_tracking`` / ``pact_cost_attribution`` custom_rules → dropped

These tests FAIL until that retirement happens (the four artifacts are present
in the live pack as of 2026-06-05: rules.yaml custom_rules block + the two
confrontations at ~L255 / ~L309).

NOTE ON SCOPE (the category-2 trap, per the story context three-category sweep
doctrine): the word "ledger" saturates ``prompts.yaml`` as PROSE IMAGERY (the
blood-and-years metaphor, match-cut candle imagery). That voice is the doom-cost
identity D5 explicitly PRESERVES — it is not retired. These tests therefore pin
only the *mechanical* artifacts (confrontation types + custom_rules keys), never
the prose. Do not extend them into a ``grep ledger`` sweep — that would gut the
pack's voice and contradict the spec's flavor-preservation mandate.
"""

from __future__ import annotations

import pytest

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.pack import GenrePack
from tests._helpers.genre_paths import GENRE_PACKS_DIR, find_pack_path

HEAVY_METAL_DIR = GENRE_PACKS_DIR / "heavy_metal"

# The two bespoke confrontation types D5 retires.
RETIRED_CONFRONTATION_TYPES = {"pact_working", "debt_collection"}
# The two bespoke custom_rules keys D5 drops.
RETIRED_CUSTOM_RULE_KEYS = {"ledger_tracking", "pact_cost_attribution"}
# The confrontation types that legitimately SURVIVE the port (spec §6.1):
#   - combat ("Blade-work")  → hp_depletion, WWN-resolved
#   - negotiation ("Cold Negotiation") → dial (generic, kept like EH)
#   - chase ("Pursuit")      → dial (generic, kept like EH)
EXPECTED_SURVIVING_TYPES = {"combat", "negotiation", "chase"}


def _has_real_content() -> bool:
    return HEAVY_METAL_DIR.is_dir()


def _load_heavy_metal() -> GenrePack:
    pack = load_genre_pack(find_pack_path("heavy_metal"))
    assert pack.rules is not None, "heavy_metal must declare a rules block"
    return pack


def _confrontation_types(pack: GenrePack) -> set[str]:
    return {c.confrontation_type for c in pack.rules.confrontations}


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_pact_working_confrontation_is_retired():
    """D5: the ``pact_working`` ("Working the Rite") confrontation is cut —
    its runtime role is taken by the WWN ``cast_spell`` beat."""
    types = _confrontation_types(_load_heavy_metal())
    assert "pact_working" not in types, (
        "pact_working confrontation must be retired (story 87-4 D5); the "
        "cast_spell beat replaces it"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_debt_collection_confrontation_is_retired():
    """D5: the ``debt_collection`` ("The Collector at the Door") confrontation
    is cut outright — there is no WWN replacement."""
    types = _confrontation_types(_load_heavy_metal())
    assert "debt_collection" not in types, (
        "debt_collection confrontation must be retired (story 87-4 D5)"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_ledger_custom_rules_are_retired():
    """D5: the bespoke ``ledger_tracking`` / ``pact_cost_attribution`` custom
    rules drop. The doom-cost toll is carried by WWN Effort + System Strain,
    not a ledger rule."""
    custom_rules = _load_heavy_metal().rules.custom_rules
    leftover = RETIRED_CUSTOM_RULE_KEYS & set(custom_rules)
    assert not leftover, (
        f"custom_rules must not carry the retired ledger keys, found: {sorted(leftover)} "
        "(story 87-4 D5)"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_no_retired_confrontation_types_remain():
    """Belt-and-braces: neither retired type survives under any label."""
    types = _confrontation_types(_load_heavy_metal())
    leftover = RETIRED_CONFRONTATION_TYPES & types
    assert not leftover, (
        f"retired confrontation types still present: {sorted(leftover)} (story 87-4 D5)"
    )


@pytest.mark.skipif(not _has_real_content(), reason="sidequest-content not on disk")
def test_surviving_confrontations_are_exactly_the_ported_set():
    """The post-retirement confrontation set is exactly combat + negotiation +
    chase. This pins BOTH directions: the bespoke pair is gone AND the generic
    ported confrontations survive (so the retirement did not over-reach and
    delete negotiation/chase). Fails now because pact_working/debt_collection
    inflate the set."""
    types = _confrontation_types(_load_heavy_metal())
    assert types == EXPECTED_SURVIVING_TYPES, (
        f"expected surviving confrontations {sorted(EXPECTED_SURVIVING_TYPES)}, "
        f"got {sorted(types)} (story 87-4: retire the bespoke pair, keep the generic dials)"
    )
