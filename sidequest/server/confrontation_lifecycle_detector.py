"""Confrontation lifecycle lie-detector for the GM panel.

sq-playtest 2026-05-12 [BUG] Confrontation panel doesn't clear when the
encounter ends — Chalk Moth narrated dead, panel stays open, next turn
the narrator un-kills the moth. Root cause is broader (narrator
hallucinates kills not backed by metric saturation; rolls are failing
so the engine never resolves the encounter mechanically) — fixing the
prompt is out of scope here, but the disconnect must be VISIBLE in the
GM panel per CLAUDE.md OTEL principle. This module classifies each
post-narration confrontation state and emits a watcher event with the
disagreement surface so Sebastien's panel can flag the lie.

Wired into the CONFRONTATION emit in websocket_session_handler.py — fires
once per emit, parallel to the existing `confrontation_peer_projection_
broadcast` watcher event.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# High-confidence English kill / death prose. Restrict to vocabulary that
# is unambiguous about opponent defeat — common false-positives like
# "the dead end of the tunnel" are mostly defended by word boundaries and
# the corpus is small enough that further calibration can land via
# playtest data without rewriting the matcher. Patterns intentionally
# match on the lemma rather than narrator-specific phrasing.
_KILL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bkill(?:ed|s)?\b", re.IGNORECASE),
    re.compile(r"\bslain\b", re.IGNORECASE),
    re.compile(r"\bdead\b", re.IGNORECASE),
    re.compile(r"\bdies\b", re.IGNORECASE),
    re.compile(r"\blifeless\b", re.IGNORECASE),
    re.compile(r"\bcorpse[ds]?\b", re.IGNORECASE),
    # "the legs go slack" / "go slack" — physiological collapse phrasing
    # the playtest narration used verbatim for the Chalk Moth kill.
    re.compile(r"\bgo(?:es)?\s+slack\b", re.IGNORECASE),
    # "fell still" — narrator's "Silence." continuation phrasing.
    re.compile(r"\bfell\s+still\b", re.IGNORECASE),
    re.compile(r"\bbreath(?:s|ed)?\s+(?:their|its|his|her)\s+last\b", re.IGNORECASE),
]


@dataclass(frozen=True)
class ConfrontationLifecycleSnapshot:
    """Post-narration confrontation state + narration kill-claim analysis.

    All attributes are JSON-safe and meant to feed a watcher event payload
    1:1 — see ``to_watcher_attrs``. Comparing
    ``narration_claims_kill`` against ``encounter_active_post_apply`` is
    the lie-detector core: kill claim + still active = narrator outran
    the engine.
    """

    encounter_type: str | None
    encounter_active_pre_apply: bool
    encounter_active_post_apply: bool
    encounter_resolved_this_turn: bool
    player_metric_current: int | None
    player_metric_threshold: int | None
    opponent_metric_current: int | None
    opponent_metric_threshold: int | None
    opponent_alive_count: int
    narration_claims_kill: bool
    narration_kill_keywords: list[str] = field(default_factory=list)

    @property
    def narrator_kill_unbacked(self) -> bool:
        """Narrator claimed a kill in prose but the encounter is still active.

        High-signal regression detector: post-fix this should be 0 on the
        dashboard except in genuine multi-opponent fights where one
        opponent dies but others remain. The lifecycle snapshot's
        ``opponent_alive_count`` lets the panel disambiguate — a kill
        claim WITH alive opponents remaining is not a lie.
        """
        return (
            self.narration_claims_kill
            and self.encounter_active_post_apply
            and self.opponent_alive_count > 0
        )

    def to_watcher_attrs(self) -> dict[str, object]:
        """Render the snapshot as a watcher event attribute dict."""
        return {
            "encounter_type": self.encounter_type or "",
            "encounter_active_pre_apply": self.encounter_active_pre_apply,
            "encounter_active_post_apply": self.encounter_active_post_apply,
            "encounter_resolved_this_turn": self.encounter_resolved_this_turn,
            "player_metric_current": self.player_metric_current,
            "player_metric_threshold": self.player_metric_threshold,
            "opponent_metric_current": self.opponent_metric_current,
            "opponent_metric_threshold": self.opponent_metric_threshold,
            "opponent_alive_count": self.opponent_alive_count,
            "narration_claims_kill": self.narration_claims_kill,
            "narration_kill_keywords": self.narration_kill_keywords,
            "narrator_kill_unbacked": self.narrator_kill_unbacked,
        }


def detect_kill_keywords(narration: str) -> list[str]:
    """Return the list of high-confidence kill keywords matched in narration.

    Each pattern contributes at most one match string (the literal text
    in the narration that matched, lowercased). Empty list when narration
    is empty or no patterns match.
    """
    if not narration:
        return []
    matches: list[str] = []
    for pat in _KILL_PATTERNS:
        m = pat.search(narration)
        if m is not None:
            matches.append(m.group(0).lower())
    return matches


def build_lifecycle_snapshot(
    *,
    narration: str,
    encounter_active_pre_apply: bool,
    encounter,  # type: ignore[no-untyped-def] — sidequest.game.encounter.Encounter | None
    encounter_resolved_this_turn: bool,
) -> ConfrontationLifecycleSnapshot:
    """Build the post-narration lifecycle snapshot.

    ``encounter`` is the snapshot's encounter AFTER narration apply
    (typed loosely to keep this module standalone-importable for tests
    without dragging the whole game-state package). When ``encounter``
    is None, the post-apply branch indicates the encounter is gone
    (resolved or never existed).
    """
    kill_keywords = detect_kill_keywords(narration)
    narration_claims_kill = len(kill_keywords) > 0

    encounter_type: str | None = None
    encounter_active_post_apply = False
    player_metric_current: int | None = None
    player_metric_threshold: int | None = None
    opponent_metric_current: int | None = None
    opponent_metric_threshold: int | None = None
    opponent_alive_count = 0

    if encounter is not None:
        encounter_type = encounter.encounter_type
        encounter_active_post_apply = not encounter.resolved
        pm = encounter.player_metric
        om = encounter.opponent_metric
        player_metric_current = pm.current
        player_metric_threshold = pm.threshold
        opponent_metric_current = om.current
        opponent_metric_threshold = om.threshold
        # Count opponent-side actors that have not withdrawn. The
        # EncounterActor model has a `withdrawn` flag flipped on yield
        # (sidequest/game/encounter.py:119); withdrawn actors are
        # skipped by _apply_beat and do not contribute to ongoing
        # combat. Non-withdrawn opponents are "still in the fight".
        opponent_alive_count = sum(
            1
            for a in encounter.actors
            if a.side == "opponent" and not a.withdrawn
        )

    return ConfrontationLifecycleSnapshot(
        encounter_type=encounter_type,
        encounter_active_pre_apply=encounter_active_pre_apply,
        encounter_active_post_apply=encounter_active_post_apply,
        encounter_resolved_this_turn=encounter_resolved_this_turn,
        player_metric_current=player_metric_current,
        player_metric_threshold=player_metric_threshold,
        opponent_metric_current=opponent_metric_current,
        opponent_metric_threshold=opponent_metric_threshold,
        opponent_alive_count=opponent_alive_count,
        narration_claims_kill=narration_claims_kill,
        narration_kill_keywords=kill_keywords,
    )
