"""Story 84-2 (WI-5) — alias-aware mention resolution + accretion helper (RED phase).

ADR-118 §A4: ``mention`` (the DOMINANT pertinence signal, 84-1) must resolve through
each entity's **aliases/epithets**, not raw player tokens — ADR-048's own example,
"the Ember Throne" ≡ "the seat of the fire king". 84-1 left mention NAME-MATCH ONLY
(``retrieval_orchestration.py:300-303`` carries the explicit WI-5 TODO). WI-5 fills it.

THE CONTRACT THIS SUITE PINS (the test IS the spec) — net-new pure helpers:

    # sidequest.game.alias_resolution  (NET-NEW module — pure, no I/O)

    def resolve_mention(
        action_text: str,
        *,
        names: set[str],
        aliases_by_name: dict[str, list[str]],
    ) -> set[str]
        # Return the canonical names the action references through EITHER the
        # canonical name OR any alias/epithet. Word-bounded (``\b``), case-
        # insensitive; multi-word epithets ("the old man") match as a PHRASE.

    def accrete_aliases(
        existing: list[str],
        new_epithets: list[str],
    ) -> list[str]
        # Idempotent, case-folded-dedup merge for the 75-1-shaped accretion path:
        # append only genuinely-new epithets, never a blank, never a case-dup.

PURE: no daemon, no span, no store. Synthetic fixtures only (project rule). Symbols
imported inside each test so collection survives and each fails crisply until Dev
implements them.
"""

from __future__ import annotations

# ===========================================================================
# AC-1 — mention resolves through name OR alias
# ===========================================================================


class TestResolveMention:
    def test_alias_reference_resolves_to_npc(self) -> None:
        """The headline (§A4): "the old man" → the NPC whose alias is "old man",
        even though the canonical name (Thorn) never appears in the action."""
        from sidequest.game.alias_resolution import resolve_mention

        matched = resolve_mention(
            "I greet the old man by the fire",
            names={"Thorn", "Borin"},
            aliases_by_name={"Thorn": ["old man"], "Borin": []},
        )
        assert matched == {"Thorn"}, "an alias reference must resolve to its NPC"

    def test_canonical_name_still_resolves(self) -> None:
        """No regression: a canonical-name reference still resolves (84-1 behavior
        preserved — WI-5 only WIDENS the matcher)."""
        from sidequest.game.alias_resolution import resolve_mention

        matched = resolve_mention(
            "I attack Borin",
            names={"Thorn", "Borin"},
            aliases_by_name={"Thorn": ["old man"], "Borin": []},
        )
        assert matched == {"Borin"}

    def test_unrelated_action_matches_nobody(self) -> None:
        """An action that names neither a name nor an alias raises mention for
        nobody — the dominant signal must not fire on noise."""
        from sidequest.game.alias_resolution import resolve_mention

        matched = resolve_mention(
            "I stare at the blank stone wall",
            names={"Thorn", "Borin"},
            aliases_by_name={"Thorn": ["old man"], "Borin": ["the smith"]},
        )
        assert matched == set()

    def test_multiword_epithet_matches_as_phrase(self) -> None:
        """ADR-048's example: a multi-word epithet ("the seat of the fire king")
        must match as a PHRASE, not require every token to appear separately."""
        from sidequest.game.alias_resolution import resolve_mention

        matched = resolve_mention(
            "I approach the seat of the fire king",
            names={"Ember Throne"},
            aliases_by_name={"Ember Throne": ["the seat of the fire king"]},
        )
        assert matched == {"Ember Throne"}

    def test_word_boundary_respected_for_aliases(self) -> None:
        """The ``\\b`` discipline of ``player_referenced_npcs_from_action`` must
        carry to aliases: alias "art" must NOT match inside "start"."""
        from sidequest.game.alias_resolution import resolve_mention

        matched = resolve_mention(
            "I start walking",
            names={"Arthur"},
            aliases_by_name={"Arthur": ["art"]},
        )
        assert matched == set(), "an alias must not match inside a larger word"

    def test_resolution_is_case_insensitive(self) -> None:
        """Player typing is messy — "The Old Man" matches alias "old man"."""
        from sidequest.game.alias_resolution import resolve_mention

        matched = resolve_mention(
            "I bow to The Old Man",
            names={"Thorn"},
            aliases_by_name={"Thorn": ["old man"]},
        )
        assert matched == {"Thorn"}

    def test_multiple_npcs_can_match(self) -> None:
        """If the action references two entities (one by name, one by alias),
        BOTH surface — the resolver is not single-winner."""
        from sidequest.game.alias_resolution import resolve_mention

        matched = resolve_mention(
            "I ask Borin about the old man",
            names={"Thorn", "Borin"},
            aliases_by_name={"Thorn": ["old man"], "Borin": []},
        )
        assert matched == {"Thorn", "Borin"}


# ===========================================================================
# AC-3 — accretion merge helper (idempotent, case-folded dedup, no blank)
# ===========================================================================


class TestAccreteAliases:
    def test_accretion_appends_new_epithet(self) -> None:
        from sidequest.game.alias_resolution import accrete_aliases

        assert accrete_aliases(["old man"], ["the grey wanderer"]) == [
            "old man",
            "the grey wanderer",
        ]

    def test_accretion_is_idempotent_no_duplicate(self) -> None:
        """The same epithet on a later turn must not duplicate — the 75-1 path is
        idempotent (a fact already accreted collides and is skipped)."""
        from sidequest.game.alias_resolution import accrete_aliases

        assert accrete_aliases(["old man"], ["old man"]) == ["old man"]

    def test_accretion_dedup_is_case_folded(self) -> None:
        """ "Old Man" must not be appended alongside "old man" — case-folded dedup,
        so the alias set doesn't bloat with casing variants."""
        from sidequest.game.alias_resolution import accrete_aliases

        result = accrete_aliases(["old man"], ["Old Man"])
        assert result == ["old man"], "a case-variant of an existing alias is a duplicate"

    def test_accretion_skips_blank_epithet(self) -> None:
        """A blank/whitespace epithet is never appended (No Silent Fallbacks: a
        blank alias would degrade the matcher / embed degenerate)."""
        from sidequest.game.alias_resolution import accrete_aliases

        assert accrete_aliases(["old man"], ["", "   "]) == ["old man"]

    def test_accretion_preserves_order_and_returns_new_list(self) -> None:
        """Deterministic for 75-6 reproject: existing aliases keep their order,
        new ones append in order; the input list is not mutated in place."""
        from sidequest.game.alias_resolution import accrete_aliases

        existing = ["a", "b"]
        result = accrete_aliases(existing, ["c"])
        assert result == ["a", "b", "c"]
        assert existing == ["a", "b"], "accrete must not mutate the input list"
