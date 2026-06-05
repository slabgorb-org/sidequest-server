"""Story 84-3 (WI-4) — relationship card projector + beat selection (RED phase).

ADR-118 §A2/§A4: ``relationship`` becomes an index-side card type, born at SUMMARY
tier — current attitude band + the two or three *load-bearing* beats. The
``disposition_log`` (ADR-136, cap 10) is a time series that cannot be embedded
whole, so the card ABBREVIATES it; the player-facing panel
(``projection/relationships.py``) shows the full log and is orthogonal.

THE CONTRACT THIS SUITE PINS (the test IS the spec) — net-new in entity_card.py:

    def select_load_bearing_beats(
        log: list[DispositionBeat], *, limit: int = 3
    ) -> list[DispositionBeat]
        # the most-recent NON-ZERO-delta beats, capped at ``limit`` (most-recent
        # first). Zero-delta beats excluded; empty / all-zero log → [].

    def project_relationship_card(npc: Npc) -> EntityCard
        # SUMMARY-tier card: content carries the attitude BAND + the selected
        # 2-3 beat reasons; NEVER the full log. Aliases → metadata["aliases"]
        # (sorted JSON, like project_npc_card). Deterministic (75-6 reproject).
        # Empty/all-zero log → attitude-band-only card, never blank content.

Synthetic ``Npc`` + ``disposition_log`` fixtures only. Symbols imported inside
each test so collection survives and each fails crisply until Dev implements them.
"""

from __future__ import annotations

import json

from sidequest.game.creature_core import CreatureCore
from sidequest.game.disposition import Disposition, DispositionBeat
from sidequest.game.session import Npc


def _npc(
    name: str = "Borin",
    *,
    disposition: int = 0,
    log: list[DispositionBeat] | None = None,
    aliases: list[str] | None = None,
) -> Npc:
    kwargs = {
        "core": CreatureCore(name=name, description=f"{name} desc", personality="stoic"),
        "disposition": Disposition(disposition),
    }
    if log is not None:
        kwargs["disposition_log"] = log
    if aliases is not None:
        kwargs["aliases"] = aliases
    return Npc(**kwargs)


def _beat(turn: int, delta: int, reason: str) -> DispositionBeat:
    return DispositionBeat(turn=turn, delta=delta, reason=reason, location=None)


# ===========================================================================
# AC-2 — load-bearing beat selection (recency + non-zero delta)
# ===========================================================================


class TestSelectLoadBearingBeats:
    def test_select_beats_drops_zero_delta(self) -> None:
        """A zero-delta beat is not load-bearing (nothing moved) — excluded."""
        from sidequest.game.entity_card import select_load_bearing_beats

        log = [_beat(1, 5, "saved his life"), _beat(2, 0, "small talk"), _beat(3, -3, "lied")]
        selected = select_load_bearing_beats(log)
        reasons = [b.reason for b in selected]
        assert "small talk" not in reasons, "zero-delta beats must be excluded"
        assert "saved his life" in reasons and "lied" in reasons

    def test_select_beats_most_recent_first(self) -> None:
        """Selection favors recency — most-recent non-zero beats first."""
        from sidequest.game.entity_card import select_load_bearing_beats

        log = [_beat(1, 5, "oldest"), _beat(2, 5, "middle"), _beat(3, 5, "newest")]
        selected = select_load_bearing_beats(log, limit=2)
        reasons = [b.reason for b in selected]
        assert reasons[0] == "newest", "most-recent beat must come first"
        assert "oldest" not in reasons, "the oldest beat is dropped past the limit"

    def test_select_beats_caps_at_limit(self) -> None:
        """Never more than ``limit`` beats (default 3) — the SUMMARY abbreviation."""
        from sidequest.game.entity_card import select_load_bearing_beats

        log = [_beat(i, 5, f"beat{i}") for i in range(1, 8)]  # 7 non-zero beats
        assert len(select_load_bearing_beats(log)) == 3
        assert len(select_load_bearing_beats(log, limit=2)) == 2

    def test_select_beats_empty_log_returns_empty(self) -> None:
        from sidequest.game.entity_card import select_load_bearing_beats

        assert select_load_bearing_beats([]) == []

    def test_select_beats_all_zero_log_returns_empty(self) -> None:
        from sidequest.game.entity_card import select_load_bearing_beats

        log = [_beat(1, 0, "a"), _beat(2, 0, "b")]
        assert select_load_bearing_beats(log) == []


# ===========================================================================
# AC-1 — summary-tier card content (attitude band + beats, NEVER full log)
# ===========================================================================


class TestProjectRelationshipCardContent:
    def test_card_content_has_attitude_band(self) -> None:
        """The card content carries the attitude BAND so retrieval keys on the
        relationship standing. A friendly NPC's card reads as warm/friendly."""
        from sidequest.game.entity_card import project_relationship_card

        npc = _npc("Borin", disposition=60, log=[_beat(3, 20, "saved the party")])
        card = project_relationship_card(npc)
        content = card.content.lower()
        # The 5-level band_for(60) == "Devoted"; the 3-level attitude == "friendly".
        # Either band vocabulary is acceptable — assert SOME positive band word is present.
        assert any(w in content for w in ("devoted", "warm", "friendly")), (
            f"relationship card must carry the attitude band; got {card.content!r}"
        )

    def test_card_content_has_load_bearing_beats(self) -> None:
        """The selected beats' reasons appear in the card content (the 'why')."""
        from sidequest.game.entity_card import project_relationship_card

        npc = _npc("Borin", disposition=40, log=[_beat(5, 15, "saved the party from the ogre")])
        card = project_relationship_card(npc)
        assert "saved the party from the ogre" in card.content

    def test_card_never_embeds_full_log(self) -> None:
        """SUMMARY tier: a >3-beat log surfaces at MOST 3 beats — the card must
        NOT embed the full cap-10 time series."""
        from sidequest.game.entity_card import project_relationship_card

        log = [_beat(i, 5, f"distinct_beat_marker_{i}") for i in range(1, 8)]  # 7 beats
        card = project_relationship_card(_npc("Borin", disposition=30, log=log))
        present = [i for i in range(1, 8) if f"distinct_beat_marker_{i}" in card.content]
        assert len(present) <= 3, (
            f"the card must abbreviate (<=3 beats), not embed the full log; "
            f"found {len(present)} beat markers in {card.content!r}"
        )

    def test_card_is_summary_tier(self) -> None:
        """§A3: the relationship card is BORN at SUMMARY tier — never FULL.
        Tier rides ``metadata['tier']`` per the §A3 contract."""
        from sidequest.game.entity_card import project_relationship_card

        card = project_relationship_card(_npc("Borin", disposition=40, log=[_beat(3, 10, "x")]))
        assert card.metadata.get("tier", "").lower() == "summary", (
            "the relationship card must be born at SUMMARY tier (§A3)"
        )


# ===========================================================================
# AC-3 — attitude-only fallback (empty / all-zero log), never blank content
# ===========================================================================


class TestEmptyLogFallback:
    def test_empty_log_projects_attitude_only_card(self) -> None:
        """An NPC with no disposition history still projects a valid card — the
        attitude band alone, no beats (not a fail-loud, not a skip)."""
        from sidequest.game.entity_card import project_relationship_card

        card = project_relationship_card(_npc("Borin", disposition=0, log=[]))
        assert card is not None
        assert any(w in card.content.lower() for w in ("neutral", "friendly", "hostile")), (
            "an empty-log card must still carry the current attitude band"
        )

    def test_empty_log_card_content_not_blank(self) -> None:
        """No Silent Fallbacks: EntityCard rejects blank content, so the fallback
        must produce non-blank content, never an empty/whitespace card."""
        from sidequest.game.entity_card import project_relationship_card

        card = project_relationship_card(_npc("Borin", disposition=0, log=[]))
        assert card.content.strip(), "the fallback card content must not be blank"


# ===========================================================================
# AC-5 — deterministic projection (75-6 reproject)
# ===========================================================================


class TestDeterministicProjection:
    def test_projection_is_deterministic(self) -> None:
        """Same Npc state → byte-identical card content + metadata every time."""
        from sidequest.game.entity_card import project_relationship_card

        log = [_beat(1, 5, "alpha"), _beat(2, -3, "beta")]
        a = project_relationship_card(_npc("Borin", disposition=20, log=list(log)))
        b = project_relationship_card(_npc("Borin", disposition=20, log=list(log)))
        assert a.content == b.content
        assert a.metadata == b.metadata
        assert a.id == b.id

    def test_aliases_sorted_in_metadata(self) -> None:
        """Aliases are JSON-encoded and SORTED in metadata (like project_npc_card)
        so the same alias SET projects identically regardless of list order —
        75-6 reproject determinism."""
        from sidequest.game.entity_card import project_relationship_card

        a = project_relationship_card(
            _npc("Borin", disposition=10, log=[_beat(1, 5, "x")], aliases=["old man", "the smith"])
        )
        b = project_relationship_card(
            _npc("Borin", disposition=10, log=[_beat(1, 5, "x")], aliases=["the smith", "old man"])
        )
        assert a.metadata.get("aliases") == b.metadata.get("aliases")
        recovered = json.loads(a.metadata["aliases"])
        assert recovered == sorted(recovered), "aliases must be sorted in metadata"

    def test_card_id_uses_rel_namespace(self) -> None:
        """The relationship card id is namespaced ``rel:<slug>`` so it does not
        collide with the ``npc:<slug>`` card for the same NPC."""
        from sidequest.game.entity_card import project_relationship_card

        card = project_relationship_card(_npc("Borin", disposition=10, log=[_beat(1, 5, "x")]))
        assert card.id == "rel:borin", f"expected rel:borin, got {card.id!r}"
