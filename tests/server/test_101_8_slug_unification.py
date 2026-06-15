"""RED — Story 101-8: unify slug derivation onto one NFKD-fold rule.

Canonical decision (session 101-8, ratified by Keith): the shared non-ASCII
normalization is ``unicodedata.normalize("NFKD", …)`` + strip combining marks.
Each surface keeps its own separator — portraits/daemon ``_``, reference ``-`` —
so ASCII output is UNCHANGED (AC3). Only non-ASCII names change: a diacritic
folds to its base letter instead of being dropped (rule 2 ``slugify_player_name``)
or turned into a separator (rule 3 ``reference_slug.slugify``).

This is a CONSOLIDATION refactor, not a live-404 fix: each surface is internally
consistent today (see session Design Deviations). The externally-observable
change is exactly the fold. These tests assert that BEHAVIOR through the existing
public entry points every call site already uses — they do NOT grep source
(server CLAUDE.md: no source-text wiring tests). When Dev introduces the shared
NFKD-fold helper and re-points ``slugify_player_name`` + ``reference_slug.slugify``
to it, every RED test below turns green and the ASCII guards stay green.

Golden vector ``"Srárný Fyzioloniązka"`` is shared verbatim with the daemon
(``tests/test_101_8_slug_fold.py``) and orchestrator
(``scripts/tests/test_101_8_render_common_slug.py``) suites so the three repos
cannot drift on the fold contract.
"""

from __future__ import annotations

import pytest

from sidequest.foundation.reference_slug import slugify
from sidequest.server.utils import slugify_player_name

# The canonical diacritic golden case — identical input across all three repos.
_DIACRITIC = "Srárný Fyzioloniązka"
_DIACRITIC_FOLDED_BASE = "srarnyfyzioloniazka"  # separator-free fold core


# ---------------------------------------------------------------------------
# AC3 — ASCII output is UNCHANGED (regression armor). Expected values are the
# MEASURED current outputs (incl. the warts: "cosh__run" double underscore,
# "historyyaml" dot-dropped) so the refactor cannot silently alter ASCII slugs.
# These pass today AND after the fold lands.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Jane Doe", "jane_doe"),
        ("Old Sten", "old_sten"),
        ("  Mara  Quill  ", "mara_quill"),
        ("Cosh & Run", "cosh__run"),
        ("Lady Of The Hall", "lady_of_the_hall"),
        ("history.yaml", "historyyaml"),
        ("The Wicked Witch of the West", "the_wicked_witch_of_the_west"),
        ("", ""),
    ],
)
def test_slugify_player_name_ascii_unchanged(raw: str, expected: str) -> None:
    assert slugify_player_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Burglar", "burglar"),
        ("Cosh & Run", "cosh-run"),
        ("Lady Of The Hall", "lady-of-the-hall"),
        ("history.yaml", "history-yaml"),
        ("a   b---c__d", "a-b-c-d"),
        ("", ""),
    ],
)
def test_reference_slugify_ascii_unchanged(raw: str, expected: str) -> None:
    assert slugify(raw) == expected


# ---------------------------------------------------------------------------
# AC1 — non-ASCII folds to base letters (RED today). Today rule 2 DROPS the
# diacritic ("srrn_fyziolonizka") and rule 3 SPLITS on it ("sr-rn-fyzioloni-zka");
# the unified fold preserves the base letter.
# ---------------------------------------------------------------------------


def test_player_name_folds_diacritics() -> None:
    # today: "srrn_fyziolonizka"  →  fold: base letters preserved.
    assert slugify_player_name(_DIACRITIC) == "srarny_fyzioloniazka"


def test_reference_slug_folds_diacritics() -> None:
    # today: "sr-rn-fyzioloni-zka"  →  fold: "srarny-fyzioloniazka".
    assert slugify(_DIACRITIC) == "srarny-fyzioloniazka"


@pytest.mark.parametrize(
    ("raw", "ref_expected", "player_expected"),
    [
        ("café", "cafe", "cafe"),
        ("naïve", "naive", "naive"),
        ("Zoë", "zoe", "zoe"),
        ("Núñez", "nunez", "nunez"),
        ("Smörgåsbord", "smorgasbord", "smorgasbord"),
    ],
)
def test_common_diacritics_fold_on_both_surfaces(
    raw: str, ref_expected: str, player_expected: str
) -> None:
    assert slugify(raw) == ref_expected
    assert slugify_player_name(raw) == player_expected


def test_nfkd_nondecomposing_letter_limitation_is_documented() -> None:
    # NFKD does NOT decompose stand-alone letters like 'ł' (U+0142); they carry
    # no combining mark to strip, so they still drop. This is the DOCUMENTED
    # boundary of the stdlib choice (no anyascii/unidecode dep). "Łódź" → ó/ź
    # fold to o/z, but Ł drops → "odz". Pinned so a future reader sees this is
    # intended, not a regression — and so a later switch to full transliteration
    # is a deliberate, test-visible change.
    assert slugify("Łódź") == "odz"
    assert slugify_player_name("Łódź") == "odz"


# ---------------------------------------------------------------------------
# AC1 — single source of truth: both surfaces apply the SAME fold core and
# differ ONLY in separator. This is the behavioral contract of "one shared
# function" — the alphanumeric content cannot diverge between surfaces.
# ---------------------------------------------------------------------------


def test_surfaces_share_one_fold_core_differing_only_in_separator() -> None:
    player = slugify_player_name(_DIACRITIC)
    ref = slugify(_DIACRITIC)
    assert player.replace("_", "") == _DIACRITIC_FOLDED_BASE
    assert ref.replace("-", "") == _DIACRITIC_FOLDED_BASE
    assert player.replace("_", "") == ref.replace("-", ""), (
        "portrait and reference surfaces must apply the SAME non-ASCII fold; "
        "only the separator may differ"
    )
