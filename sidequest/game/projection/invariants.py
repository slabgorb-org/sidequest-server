"""Core invariants — structural guarantees genre packs cannot weaken.

Runs before GenreRuleStage in the ComposedFilter. Can short-circuit with
a terminal decision (include=True with canonical payload, or include=False).

Invariants shipped in this stage:
    - GM sees canonical (Task 5).
    - Targeted-by-field — DICE_REQUEST / etc.'s `to` field restricts
      recipients (Task 6).
    - Visibility-gated — SECRET_NOTE / NARRATION_SEGMENT carry their
      recipient set in ``_visibility.visible_to`` and the exclusion
      decision is structural here, not a genre rule (ADR-105 B1).
    - Self-authored — PLAYER_ACTION / DICE_THROW echo to author + GM
      (Task 7).
    - GM-only kind — THINKING is never routed to players (Task 8).

ADR-105 B1 — why secret-routing is a CoreInvariant, not a genre rule:
a security boundary must not depend on a pack remembering to add
``visibility_tag`` to its ``projection.yaml``. A missing rule today
*silently* passes the secret through (a No-Silent-Fallbacks violation),
and the SECRET_NOTE channel was in fact dead for players because the
old ``TARGETED_KINDS["SECRET_NOTE"]="to"`` read a ``to`` field that
``SecretNotePayload`` never carries. ``GenreRuleStage``'s
``VisibilityTagRule`` remains for *fidelity* shaping; the *exclusion*
decision for these kinds is structural and lives here. Genre packs may
tighten, never weaken.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.view import GameStateView
from sidequest.game.projection_filter import FilterDecision

logger = logging.getLogger(__name__)

# Kinds whose canonical payload carries a `to` field naming the recipient(s).
# The `to` value may be a single player_id string OR a list[str] of player_ids.
# GM is always an implicit recipient (added by the GM invariant above).
#
# NOTE (ADR-105 B1): SECRET_NOTE was removed from this map — it has no
# ``to`` field by design (``SecretNotePayload`` carries
# ``_visibility.visible_to``). It is now handled by the visibility-gated
# branch below.
TARGETED_KINDS: dict[str, str] = {
    "DICE_REQUEST": "to",
    "JOURNAL_RESPONSE": "to",
    "VOICE_TEXT": "to",
}

# ADR-105 B1: kinds whose recipient set lives in ``_visibility.visible_to``
# (not a top-level ``to`` field). The exclusion decision for these is a
# structural CoreInvariant — a genre pack cannot weaken it. ``visible_to``
# is either the sentinel string ``"all"`` or a ``list[str]`` of player_ids.
# ``NARRATION_SEGMENT`` is the ADR-105 B3 per-PC private-prose channel.
VISIBILITY_GATED_KINDS: frozenset[str] = frozenset(
    {
        "SECRET_NOTE",
        "NARRATION_SEGMENT",
    }
)

# Kinds that echo back to the player who authored them (via
# payload.author_player_id). GM is implicit recipient. Non-author,
# non-GM players do not see these.
SELF_AUTHORED_KINDS: frozenset[str] = frozenset(
    {
        "PLAYER_ACTION",
        "DICE_THROW",
        "BEAT_SELECTION",
        "CHARACTER_CREATION",
    }
)

# Kinds never routed to non-GM players. GM gets them via the GM invariant.
GM_ONLY_KINDS: frozenset[str] = frozenset({"THINKING"})


@dataclass(frozen=True)
class InvariantOutcome:
    """Result of running CoreInvariantStage on one envelope.

    ``terminal=True`` means a core invariant decided the outcome — the
    ``decision`` and ``source`` fields are both populated and the
    GenreRuleStage must not run. ``source`` is one of the
    ``invariant:*`` strings emitted as the OTEL ``rule.source`` attribute.

    ``terminal=False`` means no invariant applied; ``decision`` and
    ``source`` are both ``None`` and GenreRuleStage should be consulted.

    The stage itself owns the source string (I7): it is the only place
    that knows *which* invariant fired. The composed filter does not
    redundantly re-check the envelope kind — that avoided the previous
    silent ``"invariant:unknown"`` fallback when invariants drift.
    """

    terminal: bool
    decision: FilterDecision | None
    source: str | None = None


class CoreInvariantStage:
    """Hardcoded structural filters. No configuration."""

    def evaluate(
        self,
        *,
        envelope: MessageEnvelope,
        view: GameStateView,
        player_id: str,
    ) -> InvariantOutcome:
        # 1. GM sees canonical — always.
        if view.is_gm(player_id):
            return InvariantOutcome(
                terminal=True,
                decision=FilterDecision(include=True, payload_json=envelope.payload_json),
                source="invariant:gm_sees_all",
            )

        # 2. Targeted-by-field: kinds that declare a recipient in their payload.
        if envelope.kind in TARGETED_KINDS:
            field_name = TARGETED_KINDS[envelope.kind]
            payload = json.loads(envelope.payload_json)
            to_value = payload.get(field_name)
            included = _match_to_field(to_value, player_id)
            return InvariantOutcome(
                terminal=True,
                decision=FilterDecision(
                    include=included,
                    payload_json=envelope.payload_json if included else "",
                ),
                source="invariant:targeted",
            )

        # 2b. Visibility-gated (ADR-105 B1): SECRET_NOTE / NARRATION_SEGMENT
        #     carry their recipient set in ``_visibility.visible_to``. The
        #     *exclusion* decision is structural here so a pack cannot
        #     silently weaken the firewall by omitting a projection.yaml
        #     rule. GM already short-circuited at branch 1 (canonical).
        #     A secret kind with no/malformed visibility info FAILS CLOSED
        #     (include=False) — leaking is catastrophic, dropping a note is
        #     recoverable — and the watcher event flags it loudly so the
        #     GM panel sees a malformed secret rather than a silent leak.
        if envelope.kind in VISIBILITY_GATED_KINDS:
            payload = json.loads(envelope.payload_json)
            viz = payload.get("_visibility")
            malformed = not isinstance(viz, dict) or "visible_to" not in viz
            visible_to = viz.get("visible_to") if isinstance(viz, dict) else None
            included = (visible_to == "all") or (
                isinstance(visible_to, list) and player_id in visible_to
            )
            _publish_secret_routed(
                kind=envelope.kind,
                player_id=player_id,
                included=included,
                malformed=malformed,
            )
            return InvariantOutcome(
                terminal=True,
                decision=FilterDecision(
                    include=included,
                    payload_json=envelope.payload_json if included else "",
                ),
                source="invariant:visibility_gated",
            )

        # 3. Self-authored: echo to author + GM only.
        if envelope.kind in SELF_AUTHORED_KINDS:
            payload = json.loads(envelope.payload_json)
            author = payload.get("author_player_id")
            included = isinstance(author, str) and author == player_id
            return InvariantOutcome(
                terminal=True,
                decision=FilterDecision(
                    include=included,
                    payload_json=envelope.payload_json if included else "",
                ),
                source="invariant:self_echo",
            )

        # 4. GM-only kinds: never route to players.
        if envelope.kind in GM_ONLY_KINDS:
            return InvariantOutcome(
                terminal=True,
                decision=FilterDecision(include=False, payload_json=""),
                source="invariant:gm_only_kind",
            )

        return InvariantOutcome(terminal=False, decision=None, source=None)


def _match_to_field(to_value: object, player_id: str) -> bool:
    """Return True if player_id is named by a `to` field (scalar or list)."""
    if isinstance(to_value, str):
        return to_value == player_id
    if isinstance(to_value, list):
        return player_id in to_value
    return False


def _publish_secret_routed(
    *,
    kind: str,
    player_id: str,
    included: bool,
    malformed: bool,
) -> None:
    """Emit the ``invariant.secret_routed`` watcher event (ADR-105 B1).

    This is the firewall's lie-detector (CLAUDE.md OTEL mandate): without
    a per-recipient decision event the GM panel cannot prove a player was
    excluded from a secret kind. Fires once per recipient evaluated.
    ``malformed=True`` means the secret carried no usable
    ``_visibility.visible_to`` and was failed closed — a loud signal of
    an upstream B2/B3 bug, never a silent passthrough. Telemetry must
    never crash a projection fan-out.
    """
    try:
        from sidequest.telemetry.watcher_hub import publish_event as _watcher_publish

        _watcher_publish(
            "state_transition",
            {
                "field": "invariant.secret_routed",
                "kind": kind,
                "player_id": player_id,
                "included": included,
                "malformed": malformed,
                "source": "invariant:visibility_gated",
            },
            component="projection",
            severity="warning" if malformed else "info",
        )
    except Exception:  # noqa: BLE001 — telemetry must never crash a turn
        logger.warning(
            "invariant.secret_routed watcher publish failed kind=%s player_id=%s",
            kind,
            player_id,
        )
