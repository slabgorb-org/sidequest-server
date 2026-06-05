"""Story 84-2 (WI-5) — Npc.aliases field + card projection + persistence (RED phase).

ADR-118 §A4: an entity's aliases/epithets feed the mention resolver. WI-5 stores
them on the stateful ``Npc`` (the promotion target). Investigation (RED phase)
confirmed the storage decision:

  * ``Npc`` (sidequest/game/session.py) is a Pydantic BaseModel; ``GameSnapshot``
    (which holds ``npcs: list[Npc]``) persists as ONE JSON blob via
    ``model_dump_json()`` → ``game_state.snapshot_json`` (pg/snapshot.py). NPCs are
    NOT columnar. So ``aliases: list[str] = Field(default_factory=list)`` rides the
    blob — **NO Alembic migration**. ``default_factory=list`` means pre-84-2 saves
    (no ``aliases`` key) load to an empty list.

THE CONTRACT THIS SUITE PINS:
  * ``Npc.aliases: list[str]`` defaulting to ``[]``.
  * ``project_npc_card`` carries the aliases into ``EntityCard.metadata["aliases"]``
    deterministically (same aliases → same metadata, for the 75-6 reproject).
  * aliases survive the snapshot JSON round-trip; a legacy dict without the key
    defaults empty (No Silent Fallbacks — old saves don't crash).

Synthetic fixtures only — no migration, no DB. Symbols imported inside each test.
"""

from __future__ import annotations

import json

from sidequest.game.creature_core import CreatureCore


def _npc(name: str, *, aliases: list[str] | None = None):
    """A minimal stateful Npc. ``aliases`` passed only when the field exists —
    constructed via kwargs so a missing field surfaces as the failure under test."""
    from sidequest.game.session import Npc

    kwargs = {"core": CreatureCore(name=name, description=f"{name} desc", personality="stoic")}
    if aliases is not None:
        kwargs["aliases"] = aliases
    return Npc(**kwargs)


# ===========================================================================
# AC-2 — the field
# ===========================================================================


class TestNpcAliasesField:
    def test_npc_has_aliases_field_defaulting_empty(self) -> None:
        """A fresh Npc carries an empty ``aliases`` list — never ``None`` (a None
        would force every alias consumer to guard)."""
        from sidequest.game.session import Npc

        fields = Npc.model_fields
        assert "aliases" in fields, "Npc must declare an `aliases` field (WI-5)"
        npc = _npc("Thorn")
        assert npc.aliases == [], "aliases must default to an empty list"

    def test_aliases_accept_a_list_of_strings(self) -> None:
        npc = _npc("Thorn", aliases=["old man", "the grey wanderer"])
        assert npc.aliases == ["old man", "the grey wanderer"]


# ===========================================================================
# AC-2 — projection into card metadata
# ===========================================================================


class TestProjectNpcCardAliases:
    def test_project_npc_card_carries_aliases_in_metadata(self) -> None:
        """The mention resolver reads aliases off the card; ``project_npc_card``
        must surface ``Npc.aliases`` into ``EntityCard.metadata["aliases"]``."""
        from sidequest.game.entity_card import project_npc_card

        npc = _npc("Thorn", aliases=["old man", "the grey wanderer"])
        card = project_npc_card(npc)
        assert "aliases" in card.metadata, (
            "the projected card must carry aliases in metadata for the resolver"
        )
        # Stored as a JSON list or a separator-joined string — either way the
        # epithets must be recoverable.
        raw = card.metadata["aliases"]
        recovered = json.loads(raw) if raw.strip().startswith("[") else raw.split(",")
        recovered = [s.strip() for s in recovered]
        assert "old man" in recovered and "the grey wanderer" in recovered

    def test_projection_is_deterministic_for_same_aliases(self) -> None:
        """75-6 reproject relies on determinism: the same aliases yield the same
        metadata every time, regardless of input ordering (sort before serialize)."""
        from sidequest.game.entity_card import project_npc_card

        a = project_npc_card(_npc("Thorn", aliases=["old man", "grey wanderer"]))
        b = project_npc_card(_npc("Thorn", aliases=["grey wanderer", "old man"]))
        assert a.metadata["aliases"] == b.metadata["aliases"], (
            "same alias SET must project to identical metadata (deterministic reproject)"
        )

    def test_empty_vs_populated_aliases_projection_differs(self) -> None:
        """Contrast the two: an NPC WITH aliases must project a recoverable
        ``aliases`` value, and an NPC WITHOUT must NOT project a bogus non-empty
        one. Asserting BOTH halves pins the projection behavior so this cannot
        pass vacuously against today's no-key-at-all projector."""
        from sidequest.game.entity_card import project_npc_card

        populated = project_npc_card(_npc("Thorn", aliases=["old man"]))
        empty = project_npc_card(_npc("Borin"))

        # Populated side: the alias is recoverable from metadata.
        assert "aliases" in populated.metadata, (
            "an NPC with aliases must project an aliases metadata key (WI-5)"
        )
        praw = populated.metadata["aliases"]
        recovered = json.loads(praw) if praw.strip().startswith("[") else praw.split(",")
        assert "old man" in [s.strip() for s in recovered]

        # Empty side: no bogus value (absent key, or an empty collection).
        if "aliases" in empty.metadata:
            assert empty.metadata["aliases"].strip() in ("", "[]"), (
                "empty aliases must not project a bogus value"
            )


# ===========================================================================
# AC-4 — persistence (JSON blob round-trip, no migration)
# ===========================================================================


class TestAliasPersistence:
    def test_aliases_survive_snapshot_json_roundtrip(self) -> None:
        """The whole snapshot serializes via ``model_dump_json`` (pg/snapshot.py);
        aliases must survive that round-trip with no migration."""
        from sidequest.game.session import GameSnapshot
        from sidequest.game.turn import TurnManager

        snap = GameSnapshot(
            genre_slug="caverns_and_claudes",
            turn_manager=TurnManager(interaction=3),
            npcs=[_npc("Thorn", aliases=["old man"])],
        )
        wire = snap.model_dump_json()
        restored = GameSnapshot.model_validate_json(wire)
        assert restored.npcs[0].aliases == ["old man"], (
            "accreted aliases must round-trip through the snapshot JSON blob"
        )

    def test_legacy_snapshot_without_aliases_defaults_empty(self) -> None:
        """A pre-84-2 Npc dict has NO ``aliases`` key. Loading it must default the
        field to ``[]`` — old saves load clean, they do not crash (No Silent
        Fallbacks: the missing key is an honest default, not a swallowed error)."""
        from sidequest.game.session import Npc

        legacy = {
            "core": {"name": "Thorn", "description": "old", "personality": "stoic"},
        }
        npc = Npc.model_validate(legacy)
        assert npc.aliases == [], "a legacy NPC without aliases must default to []"
