"""Story 84-7 — §A4 diacritic-fold in alias matching + epithet POS hardening (RED).

ADR-118 §A4. Two deliverables, pinned as one suite (the test IS the spec):

PRIMARY (load-bearing AC) — the diacritic split-brain fix. ``resolve_mention`` (the
§A4 alias matcher in :mod:`sidequest.game.alias_resolution`) and the live mention
seam ``player_referenced_npcs_from_action`` (:mod:`sidequest.agents.npc_context`)
must normalize BOTH the candidate name/alias AND the action text through the SAME
NFKD fold the slug surfaces adopted in Story 101-8
(:func:`sidequest.foundation.slug_fold.fold_to_ascii`). A diacritic-named entity
("Évropi") must resolve a reference written without the accent ("evropi"), and a
plain-ASCII entity must resolve an accented reference — so the fold has to apply to
BOTH sides, not one.

  * The fold must NOT loosen the ``\\b`` word-boundary discipline ("Evropi" must not
    match inside "Evropiville").
  * The fold must be the SHARED helper's NFKD rule, NOT a re-implemented one. True
    NFKD folds ``é → e``; a naive ``str.encode("ascii", "ignore")`` would DROP the
    ``é`` entirely ("café" → "caf"), so a "cafe" reference would never match. The
    positive fold tests below FAIL under that wrong rule — that is the behavioral
    fingerprint of the shared helper (AC-4) without grepping source.

SECONDARY — epithet POS hardening. The structural "-s ending ⇒ finite verb"
heuristic in ``extract_epithets_for_npc`` wrongly rejects PLURAL-NOUN epithets
("the silver spurs", "the twin moons" — the plural noun trips the ``-s`` clause
guard). Harden the POS handling so a plural-noun appositive MINTS while a real scene
clause ("the torch sputters") is STILL rejected (no over-loosening).

PURE resolver tests + a fixture-driven live-seam wiring test (no source grep,
project rule "No Source-Text Wiring Tests"). Symbols imported inside each test so
collection survives RED and each fails crisply until Dev wires the fold.
"""

from __future__ import annotations

from typing import Any

from sidequest.game.creature_core import CreatureCore
from sidequest.game.session import GameSnapshot, Npc
from sidequest.game.turn import TurnManager


def _npc(name: str, *, aliases: list[str] | None = None) -> Npc:
    kwargs: dict[str, Any] = {
        "core": CreatureCore(name=name, description=f"{name} is a test NPC.", personality="stoic"),
        "last_seen_turn": 3,
    }
    if aliases is not None:
        kwargs["aliases"] = aliases
    return Npc(**kwargs)


def _snap(*, npcs: list[Npc]) -> GameSnapshot:
    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="mawdeep",
        turn_manager=TurnManager(interaction=10),
        npcs=npcs,
        npc_pool=[],
    )


# ===========================================================================
# PRIMARY / AC-1,2 — resolve_mention folds BOTH sides (the §A4 matcher)
# ===========================================================================


class TestResolveMentionDiacriticFold:
    def test_diacritic_name_resolves_accent_stripped_reference(self) -> None:
        """A roster name carrying a diacritic ("Évropi") must resolve a reference the
        player typed WITHOUT the accent ("evropi"). Today the ``\\b...\\b``
        IGNORECASE regex treats ``É`` and ``e`` as distinct (IGNORECASE folds case,
        not accents), so the entity is invisible to an unaccented reference."""
        from sidequest.game.alias_resolution import resolve_mention

        matched = resolve_mention(
            "I travel to evropi",
            names={"Évropi"},
            aliases_by_name={"Évropi": []},
        )
        assert matched == {"Évropi"}, (
            "a diacritic-named entity must resolve an accent-stripped reference (§A4 fold)"
        )

    def test_plain_name_resolves_accented_reference(self) -> None:
        """The mirror direction: a plain-ASCII roster name ("Evropi") must resolve a
        reference the player typed WITH an accent ("Évropi"). Only folding the
        action text too makes this match — proving the fold applies to BOTH sides."""
        from sidequest.game.alias_resolution import resolve_mention

        matched = resolve_mention(
            "I travel to Évropi",
            names={"Evropi"},
            aliases_by_name={"Evropi": []},
        )
        assert matched == {"Evropi"}, (
            "the action text must be folded too, not just the candidate name"
        )

    def test_diacritic_multiword_alias_resolves(self) -> None:
        """An alias with a diacritic ("Faërie Realm") must resolve an accent-stripped,
        case-different phrase reference ("the faerie realm") — the alias path and the
        multi-word phrase discipline both survive the fold."""
        from sidequest.game.alias_resolution import resolve_mention

        matched = resolve_mention(
            "I step into the faerie realm at dusk",
            names={"Twilight Court"},
            aliases_by_name={"Twilight Court": ["Faërie Realm"]},
        )
        assert matched == {"Twilight Court"}

    def test_fold_uses_nfkd_not_ascii_drop(self) -> None:
        """AC-4 behavioral fingerprint: the normalization is the SHARED NFKD helper,
        not a re-implemented ``encode('ascii','ignore')`` rule. True NFKD folds
        ``é → e`` so "café" matches "cafe"; the ascii-drop rule yields "caf" and would
        NOT match. This test passes ONLY when the real fold (é→e) is applied."""
        from sidequest.game.alias_resolution import resolve_mention

        matched = resolve_mention(
            "I wait at the cafe on the corner",
            names={"Café Métropole"},
            aliases_by_name={"Café Métropole": ["the café"]},
        )
        assert matched == {"Café Métropole"}, (
            "fold must map é→e (NFKD), not drop it (ascii-ignore) — reuse the 101-8 helper"
        )


# ===========================================================================
# PRIMARY / regression guards — the fold must not loosen \b or over-match
# ===========================================================================


class TestFoldPreservesDiscipline:
    def test_word_boundary_survives_fold(self) -> None:
        """The ``\\b`` discipline must hold AFTER folding: a folded name ("Evropi")
        must NOT match inside a larger word ("Evropiville"). Folding changes
        characters, not boundaries — this guards against a regex rewrite that drops
        ``\\b`` while adding the fold."""
        from sidequest.game.alias_resolution import resolve_mention

        matched = resolve_mention(
            "I ride out to Évropiville",
            names={"Evropi"},
            aliases_by_name={"Evropi": []},
        )
        assert matched == set(), "a folded name must not match inside a larger word"

    def test_unrelated_action_still_matches_nobody(self) -> None:
        """The fold must not manufacture matches: an action naming neither the name
        nor an alias resolves nobody, even with diacritic entities in the roster."""
        from sidequest.game.alias_resolution import resolve_mention

        matched = resolve_mention(
            "I stare at the blank stone wall",
            names={"Évropi", "Coyote Star"},
            aliases_by_name={"Évropi": [], "Coyote Star": []},
        )
        assert matched == set()


# ===========================================================================
# PRIMARY / AC-3 (WIRING) — the LIVE mention seam honors the fold
# ===========================================================================


class TestLiveSeamDiacriticFold:
    def test_player_referenced_resolves_diacritic_npc(self) -> None:
        """WIRING (fixture, not grep): drive the real seam
        ``player_referenced_npcs_from_action`` with a roster ``Npc`` named with a
        diacritic and an accent-stripped player reference. The fold must be applied
        on the LIVE path, not only in the pure resolver — so the diacritic NPC
        registers as referenced."""
        from sidequest.agents.npc_context import player_referenced_npcs_from_action

        snap = _snap(npcs=[_npc("Évropi")])
        referenced = player_referenced_npcs_from_action(snap, "what is happening in evropi?")
        assert "Évropi" in referenced, (
            "the live mention seam must fold diacritics — an accent-stripped "
            "reference must register the diacritic-named NPC (§A4 wiring)"
        )

    def test_live_seam_resolves_diacritic_alias(self) -> None:
        """The live seam must also fold on the ALIAS path: an NPC whose alias carries
        a diacritic ("the Café owner") resolves an accent-stripped reference."""
        from sidequest.agents.npc_context import player_referenced_npcs_from_action

        snap = _snap(npcs=[_npc("Marguerite", aliases=["the café owner"])])
        referenced = player_referenced_npcs_from_action(snap, "I ask the cafe owner about the rent")
        assert "Marguerite" in referenced


# ===========================================================================
# SECONDARY — epithet POS hardening (replace the -s finite-verb heuristic)
# ===========================================================================


class TestEpithetPosHardening:
    def test_plural_noun_epithet_is_minted(self) -> None:
        """A plural-NOUN appositive ("the silver spurs") is a valid epithet and must
        be minted. Today the structural "-s ⇒ finite verb" guard flags "spurs" as a
        verb and rejects the whole epithet — the bug this story hardens."""
        from sidequest.game.alias_accretion import extract_epithets_for_npc

        minted = extract_epithets_for_npc("Borin, the silver spurs, tipped his hat.", "Borin")
        assert "the silver spurs" in minted, (
            "a plural-noun epithet must mint — '-s' is not a finite-verb tell for nouns"
        )

    def test_second_plural_noun_epithet_is_minted(self) -> None:
        """A second plural-noun case ("the twin moons") — the hardening must
        generalize, not special-case one word."""
        from sidequest.game.alias_accretion import extract_epithets_for_npc

        minted = extract_epithets_for_npc("the twin moons, Borin, watched the gate.", "Borin")
        assert "the twin moons" in minted

    def test_finite_verb_clause_still_rejected(self) -> None:
        """GUARD (must stay green): hardening the noun side must NOT loosen the verb
        side — a real scene clause ("the torch sputters") still mints nothing, so the
        §A4 'miss before mint-garbage' invariant holds."""
        from sidequest.game.alias_accretion import extract_epithets_for_npc

        minted = extract_epithets_for_npc("Borin, the torch sputters and dies.", "Borin")
        assert minted == [], "a finite-verb clause must never mint an epithet"

    def test_valid_singular_noun_epithet_unregressed(self) -> None:
        """GUARD (must stay green): the canonical valid case ("the old smith") keeps
        minting — the POS change is additive for nouns, not a rewrite that drops
        existing behavior."""
        from sidequest.game.alias_accretion import extract_epithets_for_npc

        minted = extract_epithets_for_npc("Borin, the old smith, worked the forge.", "Borin")
        assert "the old smith" in minted
