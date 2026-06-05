"""Story 84-2 (WI-5) — alias accretion on promotion + OTEL observability (RED phase).

ADR-118 §A4: promoted/yes-and entities **accrete epithets** via the 75-1 accretion
path so mention-matching "gets smarter the longer the campaign runs, with no new
pipeline." Investigation (RED phase) located the hook:

  * ``_promote_pool_member_to_npc`` (narration_apply.py:1075) builds the stateful
    ``Npc``; the call site (~1240-1269, inside ``resolve_status_target``) appends it
    to ``snapshot.npcs`` and fires a ``promoted_from_pool`` watcher event.
  * The 75-1 shape to mirror: ``lore_accretion.accrete_facts_to_lore`` (idempotent
    mint → dedup → result struct → OTEL).

THE CONTRACT THIS SUITE PINS — a net-new game-tier accretion helper:

    # sidequest.game.alias_accretion  (NET-NEW)

    @dataclass
    class AliasAccretionResult:
        accreted: list[str]          # epithets newly appended this call
        skipped_duplicate: int
        skipped_blank: int

    def accrete_npc_aliases(
        npc: Npc,
        epithets: list[str],
        *,
        turn: int,
    ) -> AliasAccretionResult
        # Append genuinely-new epithets to ``npc.aliases`` (idempotent, case-folded
        # dedup, no blank), and EMIT an OTEL ``entity.alias_accreted`` span/event per
        # accretion so the GM panel sees aliases are engine-written. NO event when
        # nothing accreted (don't spam the lie-detector with no-ops).

The OTEL span name is pinned as ``SPAN_ALIAS_ACCRETED``. Span-count tests run ``-n0``.
Synthetic fixtures only; symbols imported inside each test.
"""

from __future__ import annotations

from typing import Any

from sidequest.game.creature_core import CreatureCore

_ALIAS_ACCRETED_SPAN = "entity.alias_accreted"


def _npc(name: str, *, aliases: list[str] | None = None):
    from sidequest.game.session import Npc

    kwargs: dict[str, Any] = {
        "core": CreatureCore(name=name, description=f"{name} desc", personality="stoic")
    }
    if aliases is not None:
        kwargs["aliases"] = aliases
    return Npc(**kwargs)


# ===========================================================================
# AC-3 — accretion mutates aliases idempotently
# ===========================================================================


class TestAccreteNpcAliases:
    # NOTE: these are UNIT tests of the ``accrete_npc_aliases`` helper in
    # isolation — they call it directly and do NOT exercise the production
    # promotion path. The end-to-end wiring (resolve_status_target →
    # extract_epithets_for_npc → accrete_npc_aliases, with narration_text
    # threaded and the mutated NPC persisted into snapshot.npcs) is guarded by
    # ``TestRealPromotionAccretionWiring`` below.
    def test_accrete_helper_appends_epithet_into_aliases(self) -> None:
        """An epithet handed to the accreter lands in ``npc.aliases`` (the §A4
        accretion). Helper-level unit test — not the promotion wiring."""
        from sidequest.game.alias_accretion import accrete_npc_aliases

        npc = _npc("Thorn")
        result = accrete_npc_aliases(npc, ["the old man"], turn=4)
        assert "the old man" in npc.aliases
        assert result.accreted == ["the old man"]

    def test_accretion_is_idempotent_no_duplicate_aliases(self) -> None:
        """Re-accreting the same epithet next turn does not duplicate it (the
        75-1 path is idempotent — a fact already accreted collides and is skipped)."""
        from sidequest.game.alias_accretion import accrete_npc_aliases

        npc = _npc("Thorn", aliases=["the old man"])
        result = accrete_npc_aliases(npc, ["the old man"], turn=5)
        assert npc.aliases == ["the old man"]
        assert result.accreted == []
        assert result.skipped_duplicate == 1

    def test_accretion_skips_blank_epithet(self) -> None:
        """A blank/whitespace epithet is never appended (No Silent Fallbacks)."""
        from sidequest.game.alias_accretion import accrete_npc_aliases

        npc = _npc("Thorn")
        result = accrete_npc_aliases(npc, ["", "   "], turn=6)
        assert npc.aliases == []
        assert result.skipped_blank == 2


# ===========================================================================
# AC-5 — OTEL: accretion is observable (GM-panel lie-detector)
# ===========================================================================


class TestAliasAccretionOtel:
    def test_alias_accretion_emits_otel_event(self, otel_capture: Any) -> None:
        """A real accretion emits an ``entity.alias_accreted`` span carrying the
        npc name + the accreted alias, so the GM panel verifies aliases are
        engine-written, not narrator-improvised (CLAUDE.md OTEL principle)."""
        from sidequest.game.alias_accretion import accrete_npc_aliases

        npc = _npc("Thorn")
        accrete_npc_aliases(npc, ["the old man"], turn=4)

        spans = [s for s in otel_capture.get_finished_spans() if s.name == _ALIAS_ACCRETED_SPAN]
        assert spans, f"a real accretion must emit a {_ALIAS_ACCRETED_SPAN!r} span"
        attrs = dict(spans[0].attributes or {})
        # The span must identify WHICH npc accreted WHICH alias.
        assert "Thorn" in str(attrs.get("npc_name", "")) or "Thorn" in str(attrs.values())
        assert "the old man" in str(attrs.values())

    def test_no_accretion_event_when_nothing_accreted(self, otel_capture: Any) -> None:
        """Re-accreting an existing alias accretes nothing — and must NOT emit an
        accretion span (don't spam the lie-detector with no-op turns)."""
        from sidequest.game.alias_accretion import accrete_npc_aliases

        npc = _npc("Thorn", aliases=["the old man"])
        accrete_npc_aliases(npc, ["the old man"], turn=5)

        spans = [s for s in otel_capture.get_finished_spans() if s.name == _ALIAS_ACCRETED_SPAN]
        assert not spans, "a no-op accretion must not emit an alias-accreted span"

    def test_span_name_pinned(self) -> None:
        """Pin the span-name constant so the emitter and the GM-panel reader agree."""
        from sidequest.game.alias_accretion import SPAN_ALIAS_ACCRETED

        assert SPAN_ALIAS_ACCRETED == _ALIAS_ACCRETED_SPAN


# ===========================================================================
# AC-3 / AC-5 WIRING (mandatory) — the REAL promotion path accretes + observes
# ===========================================================================


def _snapshot_with_pool_member(name: str):
    """A minimal snapshot carrying an unpromoted pool member ``name``. Promotion
    happens inside ``resolve_status_target`` when that name is resolved as a
    status actor — the live production seam (no auto-mint, no LLM)."""
    from sidequest.game.npc_pool import NpcPoolMember
    from sidequest.game.session import GameSnapshot
    from sidequest.game.turn import TurnManager

    return GameSnapshot(
        genre_slug="caverns_and_claudes",
        world_slug="beneath_sunden",
        turn_manager=TurnManager(interaction=7),
        npc_pool=[NpcPoolMember(name=name, drawn_from="world_authored")],
    )


class TestRealPromotionAccretionWiring:
    """server CLAUDE.md "Every Test Suite Needs a Wiring Test": the helper unit
    tests above call ``accrete_npc_aliases`` directly. This class drives the REAL
    production seam — ``resolve_status_target`` (narration_apply.py:1208), which
    threads ``narration_text`` → ``extract_epithets_for_npc`` →
    ``accrete_npc_aliases`` on the same ``Npc`` object it appends to
    ``snapshot.npcs``. It is a regression guard on already-correct wiring: it
    passes today and FAILS if someone later drops the ``narration_text=`` arg at
    the call site (narration_apply.py:4629) or breaks the
    extract→accrete→persist seam.

    Behaviour + span only (No Source-Text Wiring Tests): assertions read the
    promoted ``Npc`` on ``snapshot.npcs`` and the emitted span, never source text.
    """

    def test_real_promotion_path_accretes_epithet_and_emits_span(
        self, otel_capture: Any
    ) -> None:
        """Drive ``resolve_status_target`` with a pool member being promoted and an
        appositive promotion narration. The epithet must land on the NPC that is
        appended to ``snapshot.npcs`` (proving ``narration_text`` threads through
        the real seam and the mutated NPC is the persisted one), and the
        ``entity.alias_accreted`` span must fire."""
        from sidequest.server.narration_apply import resolve_status_target

        snapshot = _snapshot_with_pool_member("Borin")
        promoted = resolve_status_target(
            snapshot,
            actor_name="Borin",
            turn_num=7,
            trigger="status_change",
            narration_text="Borin, the old smith, steps forward to greet you.",
        )

        # (1) The promotion happened on the live path and the epithet was accreted.
        assert promoted is not None
        assert "the old smith" in promoted.aliases, (
            "the promotion narration's appositive epithet must accrete onto the "
            "promoted NPC via the real resolve_status_target seam"
        )

        # (2) The mutated NPC IS the one persisted in snapshot.npcs (no dual-rep):
        #     the alias lives on the snapshot object, not a detached copy.
        in_snapshot = next((n for n in snapshot.npcs if n.core.name == "Borin"), None)
        assert in_snapshot is not None, "the promoted NPC must be appended to snapshot.npcs"
        assert "the old smith" in in_snapshot.aliases, (
            "the accreted alias must be on the snapshot NPC (so it persists)"
        )

        # (3) The accretion is observable: the entity.alias_accreted span fired.
        spans = [s for s in otel_capture.get_finished_spans() if s.name == _ALIAS_ACCRETED_SPAN]
        assert spans, (
            "a real promotion-path accretion must emit an entity.alias_accreted span"
        )
        attrs = dict(spans[-1].attributes or {})
        assert "Borin" in str(attrs.values())
        assert "the old smith" in str(attrs.values())

    def test_real_promotion_aliases_survive_snapshot_json_roundtrip(self) -> None:
        """The accreted alias rides the GameSnapshot JSON blob (no migration): a
        promotion-then-serialize round-trip preserves it on the snapshot NPC."""
        from sidequest.game.session import GameSnapshot
        from sidequest.server.narration_apply import resolve_status_target

        snapshot = _snapshot_with_pool_member("Borin")
        resolve_status_target(
            snapshot,
            actor_name="Borin",
            turn_num=7,
            trigger="status_change",
            narration_text="Borin, the old smith, steps forward.",
        )

        restored = GameSnapshot.model_validate_json(snapshot.model_dump_json())
        borin = next((n for n in restored.npcs if n.core.name == "Borin"), None)
        assert borin is not None
        assert "the old smith" in borin.aliases, (
            "accreted aliases must survive the snapshot JSON round-trip (no migration)"
        )

    def test_non_epithet_promotion_accretes_nothing_and_emits_no_span(
        self, otel_capture: Any
    ) -> None:
        """Regression guard on no-op honesty at the REAL seam: a promotion whose
        narration carries no appositive epithet for the NPC accretes nothing AND
        fires no span — the production path must not spam the lie-detector or mint
        a phantom alias."""
        from sidequest.server.narration_apply import resolve_status_target

        snapshot = _snapshot_with_pool_member("Vex")
        promoted = resolve_status_target(
            snapshot,
            actor_name="Vex",
            turn_num=8,
            trigger="status_change",
            narration_text="Vex draws a blade and lunges at the hero.",
        )

        assert promoted is not None
        assert promoted.aliases == [], (
            "a promotion narration with no appositive epithet must accrete nothing "
            "(conservative extraction — §A4 alias correctness is load-bearing)"
        )
        spans = [s for s in otel_capture.get_finished_spans() if s.name == _ALIAS_ACCRETED_SPAN]
        assert not spans, "a no-op promotion must not emit an alias-accreted span"
