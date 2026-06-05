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
    def test_promotion_accretes_epithet_into_aliases(self) -> None:
        """An epithet the narration carried for the promoted NPC lands in
        ``npc.aliases`` (the §A4 accretion)."""
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
