"""Story 84-5 (WI-2) — project_quest_card + project_trope_card (RED phase).

ADR-118 §A2: a DORMANT quest/trope is projected into the index as a SUMMARY-style
embeddable card so it can be recalled by pertinence ("what was that quest about the
smuggler?"). These are the net-new projectors (siblings of project_npc_card /
project_relationship_card).

THE CONTRACT (net-new, entity_card.py):

    def project_quest_card(quest_id: str, entry: QuestEntry) -> EntityCard
        # id "quest:<id>"; content = title + objective + status; deterministic;
        # never blank (a sparse quest still yields non-blank content).

    def project_trope_card(state: TropeState, definition: TropeDefinition) -> EntityCard
        # id "trope:<id>"; content = definition.name + description (+ progress);
        # the NAME comes from the TropeDefinition (TropeState carries only id);
        # deterministic; never blank.

Synthetic fixtures only. Symbols imported inside each test.
"""

from __future__ import annotations

from sidequest.game.session import QuestEntry, TropeState
from sidequest.genre.models.tropes import TropeDefinition

# ===========================================================================
# AC-2 — quest card
# ===========================================================================


class TestProjectQuestCard:
    def test_project_quest_card_content(self) -> None:
        from sidequest.game.entity_card import project_quest_card

        entry = QuestEntry(
            title="The Smuggler's Debt", objective="Recover the stolen ledger", status="completed"
        )
        card = project_quest_card("q_smuggler", entry)
        assert "Smuggler" in card.content
        assert "stolen ledger" in card.content

    def test_quest_card_id_namespace(self) -> None:
        """The quest card id is namespaced ``quest:<id>`` so it does not collide
        with other card types."""
        from sidequest.game.entity_card import project_quest_card

        card = project_quest_card("q_smuggler", QuestEntry(title="t", objective="o"))
        assert card.id == "quest:q_smuggler", f"expected quest:q_smuggler, got {card.id!r}"

    def test_quest_card_deterministic(self) -> None:
        """Same entry → byte-identical content + metadata (75-6 reproject)."""
        from sidequest.game.entity_card import project_quest_card

        e = QuestEntry(title="A", objective="b", status="completed")
        a = project_quest_card("q1", e)
        b = project_quest_card("q1", QuestEntry(title="A", objective="b", status="completed"))
        assert a.content == b.content
        assert a.metadata == b.metadata
        assert a.id == b.id

    def test_quest_card_not_blank_when_sparse(self) -> None:
        """No Silent Fallbacks: EntityCard rejects blank content, so a title-less,
        objective-less quest must still project non-blank content (e.g. the id /
        status), never crash or produce an empty card."""
        from sidequest.game.entity_card import project_quest_card

        card = project_quest_card("q_sparse", QuestEntry())  # all defaults, empty title
        assert card.content.strip(), "a sparse quest must still yield non-blank content"


# ===========================================================================
# AC-3 — trope card
# ===========================================================================


class TestProjectTropeCard:
    def test_project_trope_card_content(self) -> None:
        from sidequest.game.entity_card import project_trope_card

        state = TropeState(id="redemption_arc", status="resolved", progress=1.0, beats_fired=3)
        definition = TropeDefinition(
            id="redemption_arc",
            name="Redemption Arc",
            description="A fallen figure earns a second chance",
        )
        card = project_trope_card(state, definition)
        assert "Redemption Arc" in card.content
        assert "second chance" in card.content

    def test_trope_card_uses_definition_name(self) -> None:
        """TropeState carries only ``id`` — the human name MUST come from the
        TropeDefinition, never fabricated from the id slug."""
        from sidequest.game.entity_card import project_trope_card

        state = TropeState(id="t_xyz", status="dormant")
        definition = TropeDefinition(id="t_xyz", name="The Locked Room", description="A mystery")
        card = project_trope_card(state, definition)
        assert "The Locked Room" in card.content

    def test_trope_card_id_namespace(self) -> None:
        from sidequest.game.entity_card import project_trope_card

        state = TropeState(id="redemption_arc", status="dormant")
        definition = TropeDefinition(id="redemption_arc", name="Redemption Arc")
        card = project_trope_card(state, definition)
        assert card.id == "trope:redemption_arc", f"expected trope:redemption_arc, got {card.id!r}"

    def test_trope_card_deterministic(self) -> None:
        from sidequest.game.entity_card import project_trope_card

        state = TropeState(id="t1", status="resolved", progress=1.0)
        definition = TropeDefinition(id="t1", name="N", description="d")
        a = project_trope_card(state, definition)
        b = project_trope_card(
            TropeState(id="t1", status="resolved", progress=1.0),
            TropeDefinition(id="t1", name="N", description="d"),
        )
        assert a.content == b.content
        assert a.metadata == b.metadata
        assert a.id == b.id

    def test_trope_card_not_blank_when_description_absent(self) -> None:
        """A definition with only a name (no description) still yields non-blank
        content (the name)."""
        from sidequest.game.entity_card import project_trope_card

        card = project_trope_card(
            TropeState(id="t1", status="dormant"), TropeDefinition(id="t1", name="Name Only")
        )
        assert card.content.strip()
