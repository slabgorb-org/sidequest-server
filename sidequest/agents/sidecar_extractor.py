"""Post-narration Haiku sidecar extractor — ADR-150 step 2 (Story 151-2).

The foundation of the sidecar-accounting epic. A post-narration Haiku
``emit_tool`` pass (AsideResolver / IntentRouter-shaped) reads the narrator's
emitted prose and derives the eleven *bucket-B* sidecar fields. It ships in
SHADOW mode: it computes the fields and emits OTEL, but applies nothing — the
lie-detector watches from day one before any field cuts over (151-4 / 151-5).

Two layers mirror the live ADR-113 lineage:

* :class:`SidecarExtractor` — the core pass (the analogue of
  ``IntentRouter.decompose``): a single forced-``emit_tool`` Haiku call
  (ADR-102, structured input back, no JSON parsing), one bounded retry, and an
  explicit :class:`SidecarExtractionFailure` raise on persistent failure
  (No Silent Fallbacks). Emits ``sidecar_extraction.run`` + per-field spans.
* :func:`run_sidecar_extraction_watcher` — the shadow runner (the analogue of
  ``run_dispatch_engagement_watcher``): a NON-FATAL post-narration wrapper that
  runs the core pass, emits the ``sidecar_extraction.mismatch`` lie-detector
  span, and never raises into the WS turn pipeline (the per-field catch-loops
  remain the loud net).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field, ValidationError

from sidequest.agents.model_routing import CallType, resolve_model
from sidequest.telemetry.spans.sidecar_extraction import (
    sidecar_extraction_failed_span,
    sidecar_extraction_field_span,
    sidecar_extraction_mismatch_span,
    sidecar_extraction_run_span,
)

logger = logging.getLogger(__name__)

# The eleven bucket-B prose-readout fields (ADR-150 §Decision). Verified against
# NarrationTurnResult field names (orchestrator.py:474-587); ``scene_mood`` is
# the field the ADR refers to as "mood". This tuple is the SINGLE source of
# truth the 151-4 / 151-5 cutover stories reference — do not drift a parallel
# string list.
BUCKET_B_FIELDS: tuple[str, ...] = (
    "items_gained",
    "items_lost",
    "items_discarded",
    "items_consumed",
    "gold_change",
    "companions_added",
    "companions_dismissed",
    "npcs_present",
    "scene_mood",
    "visual_scene",
    "footnotes",
)

# One attempt plus one bounded retry (ADR-150 no-fallbacks discipline, mirroring
# the IntentRouter producer).
_MAX_TOTAL_ATTEMPTS = 2
_RAW_PREVIEW_LIMIT = 200

_TOOL_NAME = "emit_sidecar_fields"
_TOOL_DESCRIPTION = (
    "Emit the structured bucket-B sidecar fields you READ from the narration "
    "prose: items gained/lost/discarded/consumed, gold change, companions "
    "added/dismissed, NPCs present, scene mood, visual scene, and footnotes. "
    "Report only what the prose states; never invent. An empty field is correct "
    "when the prose says nothing about it."
)
_SYSTEM_PROMPT = (
    "You are a post-narration READER at a tabletop session. The narrator already "
    "wrote the prose; your only job is to extract the structured bucket-B sidecar "
    "fields the prose states, as a single tool call. You do not narrate and you "
    "do not invent — an empty field is the correct answer when the prose is "
    "silent about it."
)


class SidecarExtraction(BaseModel):
    """The eleven bucket-B fields as validated structured output (ADR-102).

    Shadow-mode skeleton: the list fields stay raw ``dict`` payloads (the shape
    ``emit_tool`` returns) because no field is applied this story — the 151-4 /
    151-5 cutover stories type them as they migrate into ``narration_apply``.
    """

    items_gained: list[dict[str, Any]] = Field(default_factory=list)
    items_lost: list[dict[str, Any]] = Field(default_factory=list)
    items_discarded: list[dict[str, Any]] = Field(default_factory=list)
    items_consumed: list[dict[str, Any]] = Field(default_factory=list)
    gold_change: int | None = None
    companions_added: list[dict[str, Any]] = Field(default_factory=list)
    companions_dismissed: list[str] = Field(default_factory=list)
    npcs_present: list[dict[str, Any]] = Field(default_factory=list)
    scene_mood: str | None = None
    visual_scene: dict[str, Any] | None = None
    footnotes: list[dict[str, Any]] = Field(default_factory=list)


class SidecarExtractionFailure(Exception):
    """The extractor failed after the bounded retry (ADR-150 No Silent Fallbacks).

    Raised explicitly so the shadow runner surfaces a loud GM-panel error rather
    than degrading silently to a narrator-only continuation. Carries the
    human-readable reason of the last failed attempt.
    """


class SidecarExtractorLLM(Protocol):
    """Single-shot forced-tool-use LLM contract (ADR-102), the same shape as
    ``IntentRouterLLM``: the adapter forces one tool call and returns the
    ``tool_use`` block's already-structured ``input`` dict — there is no
    free-text JSON to parse. Tests inject an ``AsyncMock`` returning a dict;
    ``llm_factory.build_sidecar_extractor_llm`` is the live implementation.
    """

    async def emit_tool(
        self,
        *,
        system: str,
        user: str,
        tool_name: str,
        tool_description: str,
        tool_schema: dict[str, Any],
    ) -> dict[str, Any]: ...


def _extraction_tool_schema() -> dict[str, Any]:
    """The forced tool's input_schema IS the bucket-B field shape, so Haiku
    returns structured input instead of fenced JSON (ADR-102)."""
    return SidecarExtraction.model_json_schema()


def _build_user_prompt(narration: str) -> str:
    return f"NARRATION:\n{narration}\n\nEmit the bucket-B sidecar fields the prose states."


def _is_emitted(value: Any) -> bool:
    """A field is 'emitted' when it carries content: a non-None scalar, or a
    non-empty collection/string. Distinguishes 'extractor looked, found nothing'
    (``emitted=False``) from 'extractor never ran'."""
    if value is None:
        return False
    if isinstance(value, (list, tuple, str, dict)):
        return len(value) > 0
    return True


class SidecarExtractor:
    """Core post-narration extraction pass (ADR-150 step 2)."""

    def __init__(self, *, llm: SidecarExtractorLLM) -> None:
        self._llm = llm

    async def extract(self, *, narration: str, snapshot: Any) -> SidecarExtraction:
        """Read the prose into the bucket-B fields via one forced ``emit_tool`` call.

        Raises :class:`SidecarExtractionFailure` if the initial attempt and the
        bounded retry both fail (timeout / transport / schema-invalid). There is
        no degraded-shape return — failure surfaces as an exception (ADR-150 No
        Silent Fallbacks). ``snapshot`` is read-only and is reserved for future
        grounding; the shadow skeleton derives fields from prose alone and
        deliberately mutates nothing.
        """
        tool_schema = _extraction_tool_schema()
        user_prompt = _build_user_prompt(narration)
        start_ns = time.perf_counter_ns()
        last_failure: tuple[str, str] | None = None
        for attempt_index in range(_MAX_TOTAL_ATTEMPTS):
            retry_count = attempt_index  # 0 on first try, 1 on retry.
            try:
                tool_input = await self._llm.emit_tool(
                    system=_SYSTEM_PROMPT,
                    user=user_prompt,
                    tool_name=_TOOL_NAME,
                    tool_description=_TOOL_DESCRIPTION,
                    tool_schema=tool_schema,
                )
            except TimeoutError as exc:
                last_failure = ("timeout", str(exc))
                self._emit_failed(reason="timeout", preview=str(exc), retry_count=retry_count)
                continue
            except Exception as exc:  # noqa: BLE001 — transport boundary (ADR-113 §5 parity)
                last_failure = ("transport", str(exc))
                self._emit_failed(reason="transport", preview=str(exc), retry_count=retry_count)
                continue

            try:
                extraction = SidecarExtraction.model_validate(tool_input)
            except ValidationError as exc:
                last_failure = ("schema_invalid", type(exc).__name__)
                self._emit_failed(
                    reason="schema_invalid",
                    preview=str(tool_input),
                    retry_count=retry_count,
                )
                logger.warning(
                    "sidecar_extraction.failed reason=schema_invalid attempt=%d exc=%s",
                    retry_count,
                    exc,
                )
                continue

            self._emit_run_and_fields(extraction=extraction, narration=narration, start_ns=start_ns)
            return extraction

        assert last_failure is not None  # _MAX_TOTAL_ATTEMPTS >= 1
        reason, detail = last_failure
        raise SidecarExtractionFailure(f"sidecar extractor failed after retry: {reason} ({detail})")

    def _emit_failed(self, *, reason: str, preview: str, retry_count: int) -> None:
        with sidecar_extraction_failed_span(
            reason=reason,
            raw_preview=preview[:_RAW_PREVIEW_LIMIT],
            retry_count=retry_count,
        ):
            pass
        logger.warning(
            "sidecar_extraction.failed reason=%s attempt=%d preview=%r",
            reason,
            retry_count,
            preview[:_RAW_PREVIEW_LIMIT],
        )

    def _emit_run_and_fields(
        self, *, extraction: SidecarExtraction, narration: str, start_ns: int
    ) -> None:
        emitted_flags = {
            field: _is_emitted(getattr(extraction, field)) for field in BUCKET_B_FIELDS
        }
        latency_ms = max(0, (time.perf_counter_ns() - start_ns) // 1_000_000)
        with sidecar_extraction_run_span(
            model=resolve_model(CallType.CLASSIFICATION),
            prose_length=len(narration),
            field_count=sum(1 for emitted in emitted_flags.values() if emitted),
            latency_ms=int(latency_ms),
        ):
            pass
        for field_name, emitted in emitted_flags.items():
            with sidecar_extraction_field_span(field=field_name, emitted=emitted):
                pass


@dataclass(frozen=True)
class SidecarMismatch:
    """One divergence between the extractor's output and engine-owned state."""

    field: str
    evidence: str


def _known_npc_names(snapshot: Any) -> set[str]:
    """The engine-owned cast: identity-channel ``npc_pool`` members plus any
    promoted ``Npc`` rows. Read defensively so a minimal snapshot is fine."""
    names: set[str] = set()
    for member in getattr(snapshot, "npc_pool", None) or []:
        name = getattr(member, "name", "")
        if name:
            names.add(name)
    for npc in getattr(snapshot, "npcs", None) or []:
        name = getattr(npc, "name", "")
        if name:
            names.add(name)
    return names


def detect_sidecar_extraction_mismatch(
    *, extraction: SidecarExtraction, snapshot: Any
) -> list[SidecarMismatch]:
    """Lie-detector witness (seed, Story 151-2).

    Membership is engine-owned (ADR-150 — the IntentRouter seats opponents
    pre-narrator). An ``npcs_present`` name the engine never seated (absent from
    the snapshot cast) is the canonical "two readers of one prose disagree". The
    later cutover stories (151-4 items, 151-5 npcs/cosmetic) add per-field
    witnesses; the skeleton seeds exactly this one.
    """
    known = _known_npc_names(snapshot)
    mismatches: list[SidecarMismatch] = []
    for mention in extraction.npcs_present:
        name = (mention.get("name", "") if isinstance(mention, dict) else "") or ""
        if name and name not in known:
            mismatches.append(
                SidecarMismatch(
                    field="npcs_present",
                    evidence=f"extractor reported npcs_present {name!r} not seated by the engine",
                )
            )
    return mismatches


async def run_sidecar_extraction_watcher(
    *, narration: str, snapshot: Any, llm: SidecarExtractorLLM
) -> SidecarExtraction | None:
    """Shadow-mode post-narration runner (ADR-150 step 2).

    Runs the extractor, emits the ``sidecar_extraction.mismatch`` lie-detector
    span on divergence, and APPLIES NOTHING (cutover is 151-4 / 151-5). NON-FATAL
    by contract (mirrors ``run_dispatch_engagement_watcher``): a crash here would
    tear down WS turn delivery *after* the prose already broadcast, so every
    failure is caught and surfaced as a loud span — never raised, never silent.
    Returns the extraction (shadow only) or ``None`` on failure / empty prose.
    """
    # Cost Scales with Drama: a turn with no prose has nothing to read — skip the
    # live Haiku call rather than extract from nothing (matches the sibling
    # post-narration classifier's non-empty-narration gating).
    if not narration.strip():
        return None
    try:
        try:
            extraction = await SidecarExtractor(llm=llm).extract(
                narration=narration, snapshot=snapshot
            )
        except SidecarExtractionFailure as exc:
            # extract() already fired the loud ERROR sidecar_extraction.failed
            # span per attempt; stay non-fatal so the turn pipeline continues —
            # the per-field catch-loops are the net (ADR-150 No Silent Fallbacks).
            logger.error(
                "sidecar_extraction.failed reason=extract_failed exc=%s "
                "(turn pipeline continues; catch-loops are the net)",
                exc,
            )
            return None
        for mismatch in detect_sidecar_extraction_mismatch(
            extraction=extraction, snapshot=snapshot
        ):
            with sidecar_extraction_mismatch_span(field=mismatch.field, evidence=mismatch.evidence):
                pass
        return extraction
    except Exception as exc:  # noqa: BLE001 — observability must never abort the turn
        logger.error(
            "sidecar_extraction.watcher_crashed error_type=%s error=%s (turn pipeline continues)",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        return None


__all__ = [
    "BUCKET_B_FIELDS",
    "SidecarExtraction",
    "SidecarExtractionFailure",
    "SidecarExtractor",
    "SidecarExtractorLLM",
    "SidecarMismatch",
    "detect_sidecar_extraction_mismatch",
    "run_sidecar_extraction_watcher",
]
