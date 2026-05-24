"""Narration visibility classifier (Story 49-8).

Produces the ``_visibility`` sidecar dict that drives per-recipient
narration routing in :mod:`sidequest.server.emitters`. The classifier
runs at the post-narration emit site after the narrator subprocess has
returned — no extra LLM calls.

Found in the 2026-05-12 caverns_sunden playtest: the live
``WebSocketSessionHandler`` ships ``visibility_sidecar=None`` so every
player receives every per-PC narration card identically in third
person. This module fills the gap with the v2 sidecar shape:

    {
        "visible_to": "all" | [player_id, ...],
        "fidelity":   {entity_id: fidelity_level},
        "anchor_pc":  "Carl" | None,
        "pov_strategy": "pc_anchored" | "atmospheric" | "private",
        # ADR-105 B2 — present ONLY when the turn carried redacted
        # routes; absent for solo/atmospheric (byte-unchanged):
        "private_segments": [
            {"visible_to": [player_id, ...] | "all",
             "fidelity": {...}, "subsystem": str, "idempotency_key": str},
            ...
        ],
    }

The v2 keys (``anchor_pc``, ``pov_strategy``) and the ADR-105 B2
``private_segments`` key are purely additive to the v1 shape; existing
consumers (:class:`sidequest.game.projection.rules.VisibilityTagRule`,
:func:`sidequest.server.session_helpers.aggregate_visibility`,
:func:`sidequest.server.emitters._apply_pov_swap`) ignore keys they do
not know about. ``private_segments`` is the structured private-route
map B3 partitions prose into and B4 POV-swaps per recipient.

Anchor inference order:
  1. ``result.action_rewrite.named`` — the structured field the
     narrator emits per ADR-039. Validated against the snapshot's PC
     roster; NPC names are NOT accepted as anchors.
  2. First-sentence scan of ``result.narration`` for a PC name from the
     roster.
  3. No match → atmospheric (``anchor_pc=None``,
     ``pov_strategy="atmospheric"``).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

if TYPE_CHECKING:
    from sidequest.agents.orchestrator import NarrationTurnResult
    from sidequest.game.session import GameSnapshot

_tracer = trace.get_tracer("sidequest.visibility_classifier")


def _pc_names(snapshot: GameSnapshot) -> list[str]:
    """Return the list of PC names from the snapshot's character roster."""
    out: list[str] = []
    for c in snapshot.characters:
        name = (c.core.name or "").strip()
        if name:
            out.append(name)
    return out


def _first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` (up to first .!? or end)."""
    m = re.match(r"[^.!?]+", text)
    return m.group(0) if m else text


def _find_pc_in_text(text: str, pc_names: list[str]) -> str | None:
    """Return the first PC name found in ``text`` (case-sensitive,
    word-boundary match), or None if no PC is mentioned.
    """
    for name in pc_names:
        if re.search(rf"\b{re.escape(name)}\b", text):
            return name
    return None


def classify_narration_visibility(
    *,
    result: NarrationTurnResult,
    snapshot: GameSnapshot,
    connected_player_ids: list[str],
    player_id_to_character: dict[str, str],
) -> dict[str, Any]:
    """Build the v2 ``_visibility`` sidecar for a narration emit.

    Args:
        result: The orchestrator's ``NarrationTurnResult`` (carries the
            narration prose and the structured ``ActionRewrite`` field).
        snapshot: The current ``GameSnapshot`` — used only for its PC
            roster (``snapshot.characters``).
        connected_player_ids: Player IDs currently attached to sockets;
            reserved for future "visible_to" filtering (this story
            broadcasts to all).
        player_id_to_character: Mapping from player_id to PC name. Used
            by downstream consumers to map ``anchor_pc`` back to a
            player_id at swap-time; not used here directly.

    Returns:
        Dict with keys ``visible_to``, ``fidelity``, ``anchor_pc``,
        ``pov_strategy``. The dict is dropped onto
        ``NarrationPayload.visibility_sidecar`` (wire name
        ``_visibility``) at the emit site.

    Raises:
        ValueError: If ``result.narration`` is empty or whitespace-only.
            An empty narration is unrenderable upstream — fail loud
            rather than emit an ambiguous sidecar.
    """
    narration = (result.narration or "").strip()
    if not narration:
        raise ValueError("narration text cannot be empty")

    pc_roster = _pc_names(snapshot)

    # ------------------------------------------------------------------
    # Step 1: try action_rewrite.named. Must validate against PC roster.
    # ------------------------------------------------------------------
    anchor: str | None = None
    action_rewrite = getattr(result, "action_rewrite", None)
    if action_rewrite is not None:
        named = (getattr(action_rewrite, "named", "") or "").strip()
        if named:
            candidate = _find_pc_in_text(named, pc_roster)
            if candidate is not None:
                anchor = candidate

    # ------------------------------------------------------------------
    # Step 2: fallback to the first sentence of the prose.
    # ------------------------------------------------------------------
    if anchor is None:
        first = _first_sentence(narration)
        anchor = _find_pc_in_text(first, pc_roster)

    pov_strategy = "pc_anchored" if anchor is not None else "atmospheric"

    # ------------------------------------------------------------------
    # ADR-105 B2: derive the real per-recipient private-route map from
    # the narrator's structured private-routing signal
    # (``result.secret_routes`` — the redacted SubsystemDispatches). The
    # hardcoded ``"all"`` + its never-done ADR-028 deferral comment (D1,
    # the welded-open valve) is removed here.
    #
    # The SHARED narration ``text`` stays ``visible_to: "all"`` *by
    # contract*, NOT by hardcode: ADR-105 B3 makes the shared blob
    # public-safe, so every connected PC may see it. The partition is
    # the per-PC SEGMENT, not the public blob — gating the blob by
    # visible_to would drop the public scene for someone. Each redacted
    # route becomes a private-segment entry carrying its own recipient
    # set (normalized through the shared ``union_visible_to`` stop-word
    # rule so this path and ``aggregate_visibility`` cannot drift).
    # ------------------------------------------------------------------
    from sidequest.protocol.dispatch import SubsystemDispatch
    from sidequest.server.session_helpers import union_visible_to

    private_segments: list[dict[str, Any]] = []
    for entry in result.secret_routes or []:
        # Mirror build_secret_note_events' skip rule exactly: only
        # SubsystemDispatch entries carry a routable recipient set.
        if not isinstance(entry, SubsystemDispatch):
            continue
        vis = entry.visibility
        private_segments.append(
            {
                "visible_to": union_visible_to([vis.visible_to]),
                "fidelity": dict(vis.perception_fidelity),
                "subsystem": entry.subsystem,
                "idempotency_key": entry.idempotency_key,
            }
        )

    sidecar: dict[str, Any] = {
        # Shared public-safe prose — visible to every connected PC by the
        # ADR-105 B3 output contract (NOT a never-tightened deferral).
        "visible_to": "all",
        # Fidelity untouched — perception_rewriter already consumes this
        # shape and the POV swap layer is orthogonal to fidelity.
        "fidelity": {},
        "anchor_pc": anchor,
        "pov_strategy": pov_strategy,
    }
    # Additive + conditional: only present when there are real private
    # routes. Solo / atmospheric / no-secret turns carry the exact v2
    # sidecar shape byte-for-byte (ADR-105 regression obligation).
    if private_segments:
        sidecar["private_segments"] = private_segments

    # OTEL lie-detector — the GM panel needs to see what anchor the
    # classifier resolved on every turn so a "narrator said Carl but
    # the swap fired on Donut" regression surfaces immediately.
    with _tracer.start_as_current_span("narration.visibility_classified") as span:
        # OTEL forbids None values; encode "no anchor" as empty string.
        span.set_attribute("anchor_pc", anchor or "")
        span.set_attribute("pov_strategy", pov_strategy)
        # visible_to either "all" (single sentinel string) or a
        # comma-joined player-id list. Joined form keeps the span
        # attribute scalar so it indexes cleanly.
        visible_to_val = sidecar["visible_to"]
        if isinstance(visible_to_val, list):
            span.set_attribute("visible_to", ",".join(visible_to_val))
        else:
            span.set_attribute("visible_to", str(visible_to_val))
        # ADR-105 B2 lie-detector: the GM panel must see the DERIVED
        # private-route partition, not just the welded-open constant.
        # ``private_segment_count`` > 0 with a convincing public blob is
        # exactly the signal that a turn carried withheld perception —
        # without it the firewall is unobservable (the original leak
        # survived 5 turns of fluent prose).
        span.set_attribute("private_segment_count", len(private_segments))
        private_union = union_visible_to([seg["visible_to"] for seg in private_segments])
        span.set_attribute(
            "private_visible_to",
            ",".join(private_union) if isinstance(private_union, list) else str(private_union),
        )
        # oq-1 VERIFY-FAIL 2026-05-16: ``private_segment_count`` counts
        # only secret_routes-derived (structured-dispatch) segments and
        # read 0 while a NARRATION_SEGMENT from the narrator's PROSE
        # partition (result.private_prose_segments) was in fact emitted +
        # gated — making the firewall look like it had nothing to
        # partition. Surface the prose-partition count distinctly so the
        # GM panel sees a turn DID carry a private prose segment.
        _prose_segs = getattr(result, "private_prose_segments", None) or []
        span.set_attribute(
            "private_prose_segment_count",
            len(_prose_segs) if isinstance(_prose_segs, list) else 0,
        )

    return sidecar
