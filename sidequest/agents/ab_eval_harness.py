"""A/B evaluation harness — Claude vs local Qwen on identical prompts.

Story 48-4 (epic 48, Local-LLM Workstream). Delivers the A/B eval plan that
Group E explicitly deferred (Group F territory).

Two-layer design (see ``.session/48-4-session.md``):

- **This module (CI-safe core):** ``AbEvalHarness`` drives both backends
  through the ``LlmClient.send_stateless`` boundary so the unit layer runs
  fully mocked — no live Ollama required. Mirrors story 48-2's
  ``ollama_latency_check.py`` operator-evidence pattern.
- **Operator layer:** ``scripts/ab_eval_harness_cli.py`` constructs the real
  backends and runs the live A/B on Keith's M3 Ultra (the only host where
  Ollama serves the local model).

Both clients MUST satisfy the ``LlmClient`` protocol (``send_stateless``).
The default ``anthropic_sdk`` backend is a ``ToolingLlmClient`` with no
``send_stateless`` — it is rejected loudly at construction (No Silent
Fallbacks), exactly as ``ollama_latency_check.py`` guards its client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Any

from sidequest.agents.claude_client import LlmClient
from sidequest.agents.intent_router import (
    IntentRouter,
    IntentRouterFailure,
    IntentRouterLLM,
    _serialize_state_summary,
)
from sidequest.agents.model_routing import LOCAL_CLASSIFIER_MODEL
from sidequest.agents.ollama_client import OllamaClientError
from sidequest.corpus.router_corpus import (
    ROUTER_CORPUS_SCHEMA_VERSION,
    RouterCapture,
)
from sidequest.corpus.schema import MineProvenance, TrainingPair
from sidequest.protocol.dispatch import DispatchPackage, SubsystemDispatch

logger = logging.getLogger(__name__)

# Model hints handed to send_stateless. The Claude hint is not load-bearing;
# OLLAMA_MODEL is — story 92-2's production rung serves exactly the model this
# harness evaluated, so the id is imported from the ladder (single source of
# truth: ``model_routing.LOCAL_CLASSIFIER_MODEL``) rather than re-spelled here.
CLAUDE_MODEL = "sonnet"
OLLAMA_MODEL = LOCAL_CLASSIFIER_MODEL


def _split_narration_and_patch(text: str) -> tuple[str, str | None]:
    """Split a backend response into (narration, raw_patch_json_or_None).

    The narrator emits prose followed by a JSON object. We take the prose as
    everything before the first ``{`` and the candidate patch as the substring
    from the first ``{`` onward. No parsing here — validity is decided by the
    caller so a malformed patch is *recorded*, never raised through.
    """
    brace = text.find("{")
    if brace == -1:
        return text.strip(), None
    return text[:brace].strip(), text[brace:]


def _validate_patch(text: str) -> tuple[bool, list[str], list[tuple[str, float]]]:
    """Return (valid, errors, declared_keys).

    A patch is valid when the JSON tail parses to a dict. ``declared_keys`` is
    a shallow, honest signal of what the model declared (top-level patch keys)
    — full trope/beat coverage needs a GameSnapshot the offline harness does
    not have (session note: "flag for future expansion").
    """
    _, raw = _split_narration_and_patch(text)
    if raw is None:
        return False, ["no JSON patch found in response"], []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        # Recorded, not swallowed (rule #1 / #8): untrusted model output must
        # never be trusted and must explain why it was rejected.
        return False, [f"patch JSON decode error: {exc}"], []
    if not isinstance(parsed, dict):
        return False, [f"patch is {type(parsed).__name__}, expected object"], []
    beats = [(str(k), 1.0) for k in parsed]
    return True, [], beats


def _similarity(a: str, b: str) -> float:
    """Narration similarity in [0.0, 1.0] via SequenceMatcher on the prose."""
    na, _ = _split_narration_and_patch(a)
    nb, _ = _split_narration_and_patch(b)
    return SequenceMatcher(None, na, nb).ratio()


def _beats_overlap(a: list[tuple[str, float]], b: list[tuple[str, float]]) -> float:
    """Jaccard overlap (%) of the two declared-key sets."""
    sa = {k for k, _ in a}
    sb = {k for k, _ in b}
    if not sa and not sb:
        return 0.0
    return 100.0 * len(sa & sb) / len(sa | sb)


@dataclass
class _Side:
    """One backend's outcome for a single pair (internal)."""

    text: str
    duration_ms: int
    valid: bool
    errors: list[str]
    beats: list[tuple[str, float]]


@dataclass
class AbEvalResult:
    """One pair's A/B evaluation. Every list is per-instance (no shared
    mutable default — rule #2)."""

    user_prompt: str
    claude_response: str
    ollama_response: str
    claude_duration_ms: int
    ollama_duration_ms: int
    latency_ratio: float
    claude_patch_valid: bool
    ollama_patch_valid: bool
    claude_patch_errors: list[str] = field(default_factory=list)
    ollama_patch_errors: list[str] = field(default_factory=list)
    claude_beats: list[tuple[str, float]] = field(default_factory=list)
    ollama_beats: list[tuple[str, float]] = field(default_factory=list)
    beats_match_pct: float = 0.0
    narration_similarity: float = 0.0
    notes: str = ""

    def to_markdown(self) -> str:
        """Single-pair markdown (used by the CLI's single-prompt mode)."""
        return (
            f"# A/B Evaluation — single pair\n\n"
            f"**Prompt:** {self.user_prompt}\n\n"
            f"| Metric | Claude | Ollama |\n"
            f"|--------|--------|--------|\n"
            f"| patch valid | {self.claude_patch_valid} | {self.ollama_patch_valid} |\n"
            f"| duration ms | {self.claude_duration_ms} | {self.ollama_duration_ms} |\n"
            f"| latency ratio (ollama/claude) | — | {self.latency_ratio:.2f} |\n"
            f"| narration similarity | {self.narration_similarity:.3f} |\n"
            f"| beats match % | {self.beats_match_pct:.1f} |\n\n"
            f"**Claude errors:** {self.claude_patch_errors or 'none'}\n\n"
            f"**Ollama errors:** {self.ollama_patch_errors or 'none'}\n\n"
            f"**Notes:** {self.notes or 'none'}\n"
        )


@dataclass
class AbEvalReport:
    """Aggregated A/B report over a batch of pairs."""

    genre: str
    sample_size: int
    timestamp: str
    claude_avg_duration_ms: float
    ollama_avg_duration_ms: float
    avg_latency_ratio: float
    claude_patch_valid_pct: float
    ollama_patch_valid_pct: float
    avg_beats_match_pct: float
    avg_narration_similarity: float
    results: list[AbEvalResult] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            f"# A/B Evaluation Report — {self.genre}",
            "",
            f"- Generated: {self.timestamp}",
            f"- Sample size: {self.sample_size}",
            "",
            "| Metric | Claude | Ollama |",
            "|--------|--------|--------|",
            f"| patch valid % | {self.claude_patch_valid_pct:.1f} "
            f"| {self.ollama_patch_valid_pct:.1f} |",
            f"| avg duration ms | {self.claude_avg_duration_ms:.0f} "
            f"| {self.ollama_avg_duration_ms:.0f} |",
            f"| avg latency ratio (ollama/claude) | — | {self.avg_latency_ratio:.2f} |",
            f"| avg narration similarity | {self.avg_narration_similarity:.3f} |",
            f"| avg beats match % | {self.avg_beats_match_pct:.1f} |",
            "",
            "## Per-pair",
            "",
            "| # | Claude valid | Ollama valid | similarity |",
            "|---|--------------|--------------|------------|",
        ]
        for i, r in enumerate(self.results):
            lines.append(
                f"| {i} | {r.claude_patch_valid} | {r.ollama_patch_valid} "
                f"| {r.narration_similarity:.3f} |"
            )
        return "\n".join(lines) + "\n"


class AbEvalHarness:
    """Run identical prompts through a Claude-family and an Ollama backend
    and compare patch validity, narration similarity, declared beats, and
    latency.

    Both clients must satisfy ``LlmClient`` (``send_stateless``). A client
    lacking it (the default ``anthropic_sdk`` ``ToolingLlmClient``) is
    rejected with ``TypeError`` here — a clear domain error, not a raw
    ``AttributeError`` surfacing mid-run (mirrors ``ollama_latency_check.py``).
    """

    def __init__(
        self,
        claude_client: LlmClient,
        ollama_client: LlmClient,
        system_prompt: str,
        genre: str,
    ) -> None:
        for label, client in (("claude", claude_client), ("ollama", ollama_client)):
            if not isinstance(client, LlmClient):
                raise TypeError(
                    f"{label}_client does not satisfy the LlmClient protocol "
                    f"(no send_stateless). The A/B harness requires "
                    f"send_stateless-capable backends; the anthropic_sdk "
                    f"ToolingLlmClient is not supported here. "
                    f"Got {type(client).__name__}."
                )
        self.claude = claude_client
        self.ollama = ollama_client
        self.system_prompt = system_prompt
        self.genre = genre

    async def _run_backend(
        self, client: LlmClient, model: str, user_prompt: str, label: str
    ) -> _Side:
        start = time.perf_counter()
        try:
            resp = await client.send_stateless(
                system_prompt=self.system_prompt,
                user_message=user_prompt,
                model=model,
            )
        except OllamaClientError:
            # Infrastructure failure (daemon down / HTTP 000 transport): the
            # local model is absent, so there is no meaningful A/B to record.
            # Propagate so the CLI emits the AC4 operator-evidence no-op
            # (exit 4 + note). This is deliberately NOT folded into rule-#9
            # per-side isolation — "Ollama unreachable" and "Ollama produced
            # a bad patch" are different signals and must not be conflated.
            logger.warning("ab_eval.%s_unreachable", label)
            raise
        except Exception as exc:  # noqa: BLE001 — per-side isolation (rule #9): a
            # single backend's API/output failure must not lose the other side.
            dur = int((time.perf_counter() - start) * 1000)
            logger.warning("ab_eval.%s_backend_error error=%s", label, exc)
            return _Side(
                text=f"<{label} backend error: {exc}>",
                duration_ms=dur,
                valid=False,
                errors=[f"{label} backend error: {exc}"],
                beats=[],
            )
        dur = int((time.perf_counter() - start) * 1000)
        valid, errors, beats = _validate_patch(resp.text)
        if not valid:
            logger.info("ab_eval.%s_patch_invalid errors=%s", label, errors)
        return _Side(text=resp.text, duration_ms=dur, valid=valid, errors=errors, beats=beats)

    async def eval_pair(
        self, user_prompt: str, expected_response: str | None = None
    ) -> AbEvalResult:
        """Run one prompt through both backends concurrently and compare.

        One backend failing never loses the other's result (rule #9): each
        side is isolated inside ``_run_backend`` and gathered independently.
        """
        # return_exceptions=True so an Ollama-unreachable raise from one side
        # does not cancel the sibling mid-flight; we then re-raise the
        # infrastructure failure for the CLI's AC4 no-op. Non-infrastructure
        # failures never reach here — _run_backend records them per-side.
        outcomes = await asyncio.gather(
            self._run_backend(self.claude, CLAUDE_MODEL, user_prompt, "claude"),
            self._run_backend(self.ollama, OLLAMA_MODEL, user_prompt, "ollama"),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
        claude_side, ollama_side = outcomes
        ratio = (
            ollama_side.duration_ms / claude_side.duration_ms
            if claude_side.duration_ms > 0
            else 0.0
        )
        note = ""
        if expected_response is not None:
            note = (
                f"reference len={len(expected_response)} "
                f"(semantic grading deferred — needs GameSnapshot)"
            )
        return AbEvalResult(
            user_prompt=user_prompt,
            claude_response=claude_side.text,
            ollama_response=ollama_side.text,
            claude_duration_ms=claude_side.duration_ms,
            ollama_duration_ms=ollama_side.duration_ms,
            latency_ratio=ratio,
            claude_patch_valid=claude_side.valid,
            ollama_patch_valid=ollama_side.valid,
            claude_patch_errors=list(claude_side.errors),
            ollama_patch_errors=list(ollama_side.errors),
            claude_beats=list(claude_side.beats),
            ollama_beats=list(ollama_side.beats),
            beats_match_pct=_beats_overlap(claude_side.beats, ollama_side.beats),
            narration_similarity=_similarity(claude_side.text, ollama_side.text),
            notes=note,
        )

    async def eval_batch(
        self, pairs: list[TrainingPair], sample_size: int | None = None
    ) -> AbEvalReport:
        """Evaluate a batch of real ``TrainingPair`` rows and aggregate.

        Binds to the real corpus schema: ``pair.input_text`` is the prompt,
        ``pair.output_text`` the reference (the session pseudocode's
        ``user_prompt``/``expected_response`` fields do not exist — TEA
        Conflict finding).
        """
        chosen = pairs[:sample_size] if sample_size is not None else list(pairs)
        results: list[AbEvalResult] = []
        for pair in chosen:
            results.append(
                await self.eval_pair(
                    user_prompt=pair.input_text,
                    expected_response=pair.output_text,
                )
            )

        n = len(results)

        def _avg(values: list[float]) -> float:
            return sum(values) / n if n else 0.0

        def _pct(flags: list[bool]) -> float:
            return 100.0 * sum(1 for f in flags if f) / n if n else 0.0

        return AbEvalReport(
            genre=self.genre,
            sample_size=n,
            timestamp=datetime.now(UTC).isoformat(),
            claude_avg_duration_ms=_avg([float(r.claude_duration_ms) for r in results]),
            ollama_avg_duration_ms=_avg([float(r.ollama_duration_ms) for r in results]),
            avg_latency_ratio=_avg([r.latency_ratio for r in results]),
            claude_patch_valid_pct=_pct([r.claude_patch_valid for r in results]),
            ollama_patch_valid_pct=_pct([r.ollama_patch_valid for r in results]),
            avg_beats_match_pct=_avg([r.beats_match_pct for r in results]),
            avg_narration_similarity=_avg([r.narration_similarity for r in results]),
            results=results,
        )


# =========================================================================== #
# Story 92-1 — router A/B layer (extension of the 48-4 harness, not a fork).
#
# The 48-4 classes above are narration-shaped: send_stateless free text,
# game-patch validity, prose similarity. The Intent Router (ADR-113) speaks a
# different protocol — a forced emit_tool call returning a structured
# ``DispatchPackage`` — and the 92-2 routing flip gates on three router-shaped
# measurements over a REAL captured corpus:
#   1. dispatch-selection agreement (per dispatch type),
#   2. qwen schema-validity rate against DispatchPackage (extra='forbid'),
#   3. latency percentiles (p50/p95) per backend.
# Everything below drives the PRODUCTION ``IntentRouter.decompose`` path so
# what we measure is the real call shape, not a parallel reimplementation.
# =========================================================================== #


class RouterAdjudication(StrEnum):
    """Manual adjudication of a Haiku/qwen disagreement (AC4).

    Haiku is the comparison anchor, not gospel: where qwen is *right* and
    Haiku was *wrong*, the disagreement counts FOR the local model. A
    both-defensible split is not a local-model failure either.
    """

    QWEN_WRONG = "qwen_wrong"
    HAIKU_WRONG = "haiku_wrong"
    BOTH_DEFENSIBLE = "both_defensible"


@dataclass
class TypeAgreement:
    """Per-dispatch-type agreement tally. ``total`` counts every evaluated
    capture where EITHER backend emitted this subsystem; ``agreed`` counts
    those where the whole turn's dispatch selection agreed."""

    agreed: int
    total: int


def latency_percentiles(values: Sequence[float]) -> tuple[float, float]:
    """(p50, p95) by the nearest-rank method on sorted ``values``.

    Nearest-rank is deterministic, dependency-free, and conservative — it
    never reports a latency nobody experienced. An empty input is operator
    nonsense and raises (No Silent Fallbacks), never a fabricated 0.0.
    """
    if not values:
        raise ValueError("latency_percentiles: empty input — nothing was measured")
    ordered = sorted(values)
    n = len(ordered)

    def _rank(q: float) -> float:
        return float(ordered[max(0, math.ceil(q * n) - 1)])

    return _rank(0.50), _rank(0.95)


def _iter_dispatches(pkg: DispatchPackage) -> Iterator[SubsystemDispatch]:
    for pd in pkg.per_player:
        yield from pd.dispatch
    for ca in pkg.cross_player:
        yield from ca.dispatch


def _selection_signature(pkg: DispatchPackage) -> tuple[frozenset[str], frozenset[str]]:
    """The comparable dispatch selection: the set of subsystem keys plus the
    set of confrontation ``type`` params (the load-bearing classification —
    a melee vs parley split seats the wrong engine even when both sides said
    'confrontation')."""
    subsystems = frozenset(d.subsystem for d in _iter_dispatches(pkg))
    confrontation_types = frozenset(
        str(d.params.get("type")) for d in _iter_dispatches(pkg) if d.subsystem == "confrontation"
    )
    return subsystems, confrontation_types


def dispatch_selection_agreement(a: DispatchPackage, b: DispatchPackage) -> bool:
    """True when both packages select the same mechanical engagement: equal
    subsystem-key sets and equal confrontation-type sets. Both backends
    deciding 'no mechanical engagement' is agreement — a pure-narration turn
    must not count against the gate metric."""
    return _selection_signature(a) == _selection_signature(b)


class _InfraSensingLlm:
    """Pass-through ``IntentRouterLLM`` that remembers an ``OllamaClientError``.

    ``IntentRouter.decompose`` classifies every emit_tool exception as a
    retryable transport failure and ultimately raises ``IntentRouterFailure``
    — which would conflate 'Ollama daemon is down' (infrastructure: no
    meaningful A/B exists) with 'qwen produced a bad package' (the
    schema-validity signal this story measures). The sensor lets the harness
    re-raise the infrastructure failure out of the per-side fold, mirroring
    the 48-4 OllamaClientError-propagation contract."""

    def __init__(self, inner: IntentRouterLLM) -> None:
        self._inner = inner
        self.infra_error: OllamaClientError | None = None

    async def emit_tool(
        self,
        *,
        system: str,
        user: str,
        tool_name: str,
        tool_description: str,
        tool_schema: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return await self._inner.emit_tool(
                system=system,
                user=user,
                tool_name=tool_name,
                tool_description=tool_description,
                tool_schema=tool_schema,
            )
        except OllamaClientError as exc:
            self.infra_error = exc
            raise


@dataclass
class _RouterSide:
    """One backend's outcome for a single capture (internal)."""

    package: DispatchPackage | None
    valid: bool
    errors: list[str]
    latency_ms: int


@dataclass
class RouterAbEvalResult:
    """One capture's router A/B evaluation."""

    action: str
    haiku_package: DispatchPackage | None
    qwen_package: DispatchPackage | None
    haiku_valid: bool
    qwen_valid: bool
    haiku_latency_ms: int
    qwen_latency_ms: int
    agreement: bool
    haiku_errors: list[str] = field(default_factory=list)
    qwen_errors: list[str] = field(default_factory=list)


@dataclass
class RouterGoNoGo:
    """The 92-2 gate verdict: go iff every reason list stays empty."""

    go: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class RouterAbEvalReport:
    """Aggregated router A/B report — the artifact the 92-2 flip gates on."""

    sample_size: int
    timestamp: str
    agreement_pct: float
    haiku_schema_validity_pct: float
    qwen_schema_validity_pct: float
    haiku_latency_p50_ms: float
    haiku_latency_p95_ms: float
    qwen_latency_p50_ms: float
    qwen_latency_p95_ms: float
    per_type_agreement: dict[str, TypeAgreement] = field(default_factory=dict)
    results: list[RouterAbEvalResult] = field(default_factory=list)

    def adjudicated_agreement_pct(self, adjudications: dict[int, RouterAdjudication]) -> float:
        """Agreement after manual adjudication (AC4). A disagreement at
        result index ``i`` adjudicated ``HAIKU_WRONG`` or ``BOTH_DEFENSIBLE``
        counts FOR the local model; ``QWEN_WRONG`` keeps the disagreement."""
        if not self.results:
            raise ValueError("adjudicated_agreement_pct: report has no results")
        agreed = 0
        for i, result in enumerate(self.results):
            if result.agreement or adjudications.get(i) in (
                RouterAdjudication.HAIKU_WRONG,
                RouterAdjudication.BOTH_DEFENSIBLE,
            ):
                agreed += 1
        return 100.0 * agreed / len(self.results)

    def go_no_go(
        self,
        *,
        agreement_threshold_pct: float,
        p95_budget_ms: float,
        schema_validity_floor_pct: float,
        adjudications: dict[int, RouterAdjudication] | None = None,
    ) -> RouterGoNoGo:
        """Evaluate the flip gate: adjudicated agreement >= threshold, qwen
        schema validity >= floor (below it the flip is unsafe regardless of
        agreement), and qwen latency p95 within the per-turn budget."""
        agreement = (
            self.adjudicated_agreement_pct(adjudications) if adjudications else self.agreement_pct
        )
        reasons: list[str] = []
        if agreement < agreement_threshold_pct:
            reasons.append(
                f"dispatch-selection agreement {agreement:.1f}% is below the "
                f"{agreement_threshold_pct:.1f}% threshold"
            )
        if self.qwen_schema_validity_pct < schema_validity_floor_pct:
            reasons.append(
                f"qwen schema validity {self.qwen_schema_validity_pct:.1f}% is "
                f"below the {schema_validity_floor_pct:.1f}% floor — the flip "
                f"is unsafe regardless of agreement"
            )
        if self.qwen_latency_p95_ms > p95_budget_ms:
            reasons.append(
                f"qwen latency p95 {self.qwen_latency_p95_ms:.0f}ms exceeds "
                f"the {p95_budget_ms:.0f}ms budget"
            )
        return RouterGoNoGo(go=not reasons, reasons=reasons)

    def to_markdown(self) -> str:
        lines = [
            "# Router A/B Evaluation Report — Haiku vs local qwen",
            "",
            f"- Generated: {self.timestamp}",
            f"- Sample size: {self.sample_size}",
            "",
            "| Metric | Haiku | qwen |",
            "|--------|-------|------|",
            f"| schema validity % | {self.haiku_schema_validity_pct:.1f} "
            f"| {self.qwen_schema_validity_pct:.1f} |",
            f"| latency p50 ms | {self.haiku_latency_p50_ms:.0f} "
            f"| {self.qwen_latency_p50_ms:.0f} |",
            f"| latency p95 ms | {self.haiku_latency_p95_ms:.0f} "
            f"| {self.qwen_latency_p95_ms:.0f} |",
            "",
            f"**Dispatch-selection agreement:** {self.agreement_pct:.1f}%",
            "",
            "## Per-dispatch-type agreement",
            "",
            "| Dispatch type | Agreed | Total |",
            "|---------------|--------|-------|",
        ]
        for name in sorted(self.per_type_agreement):
            stat = self.per_type_agreement[name]
            lines.append(f"| {name} | {stat.agreed} | {stat.total} |")
        lines += [
            "",
            "## Per-capture",
            "",
            "| # | Haiku valid | qwen valid | agreement |",
            "|---|-------------|------------|-----------|",
        ]
        for i, r in enumerate(self.results):
            lines.append(f"| {i} | {r.haiku_valid} | {r.qwen_valid} | {r.agreement} |")
        return "\n".join(lines) + "\n"


def _require_router_llm(label: str, llm: IntentRouterLLM) -> None:
    if not callable(getattr(llm, "emit_tool", None)):
        raise TypeError(
            f"{label} does not satisfy the IntentRouterLLM protocol (no "
            f"emit_tool). The router A/B harness drives the production "
            f"forced-tool call shape; a send_stateless-only client cannot "
            f"be measured here. Got {type(llm).__name__}."
        )


class RouterCorpusCapturer:
    """Capture Haiku-baseline corpus rows through the PRODUCTION router.

    The baseline ``DispatchPackage`` comes from the real
    ``IntentRouter.decompose`` — identical system prompt, tool schema, and
    prompt envelope to a live turn — so the corpus ground truth is exactly
    what production Haiku would have emitted for the captured
    ``(action, state_summary)``.
    """

    def __init__(self, *, llm: IntentRouterLLM) -> None:
        _require_router_llm("llm", llm)
        self._llm = llm

    async def capture(
        self,
        *,
        action: str,
        state_summary: Any,
        genre: str,
        world: str,
        round_number: int,
        provenance: MineProvenance,
    ) -> RouterCapture:
        router = IntentRouter(llm=self._llm)
        package = await router.decompose(action=action, state_summary=state_summary)
        return RouterCapture(
            schema_version=ROUTER_CORPUS_SCHEMA_VERSION,
            genre=genre,
            world=world,
            round_number=round_number,
            action=action,
            # Stored via the SAME serialization the prompt used so replay is
            # byte-identical to the captured turn (intent_router is the single
            # source of truth for this encoding).
            state_summary=_serialize_state_summary(state_summary),
            baseline_package=package,
            provenance=provenance,
        )


class QwenRouterLlm:
    """Measurement-only ``IntentRouterLLM`` adapter over an Ollama backend.

    qwen has no native forced-tool path (``OllamaClient.capabilities()``
    reports ``supports_tools=False``), so the production reality this story
    measures is prompt-coerced JSON: the tool schema is embedded in the
    system prompt and the raw completion is parsed as a single JSON object.
    The rate at which that JSON survives ``DispatchPackage.model_validate``
    IS the schema-validity metric. Building the production adapter is 92-2's
    job — this one lives in the harness, not the live router (story scope).
    """

    def __init__(self, client: LlmClient, *, model: str = OLLAMA_MODEL) -> None:
        if not isinstance(client, LlmClient):
            raise TypeError(
                f"QwenRouterLlm requires a send_stateless-capable LlmClient; "
                f"got {type(client).__name__}."
            )
        self._client = client
        self._model = model

    async def emit_tool(
        self,
        *,
        system: str,
        user: str,
        tool_name: str,
        tool_description: str,
        tool_schema: dict[str, Any],
    ) -> dict[str, Any]:
        coercion = (
            f"\n\nYou cannot call tools. Instead, respond with ONLY a single "
            f"JSON object that is a valid input for the `{tool_name}` tool "
            f"({tool_description}). The JSON Schema is:\n"
            f"{json.dumps(tool_schema)}\n"
            f"No prose, no markdown fences — the JSON object only."
        )
        resp = await self._client.send_stateless(
            system_prompt=system + coercion,
            user_message=user,
            model=self._model,
        )
        return _extract_json_object(resp.text)


def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse the single JSON object out of a prompt-coerced completion.

    A missing/unparseable object raises — the router's retry/failure
    taxonomy records it, and the capture is counted qwen-invalid. Never
    silently substitute an empty package."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"no JSON object in qwen response: {text[:160]!r}")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError(f"qwen JSON is {type(parsed).__name__}, expected object")
    return parsed


class RouterAbEvalHarness:
    """Run identical captured router prompts through a Haiku-shaped and a
    qwen-shaped ``IntentRouterLLM`` via the production ``IntentRouter`` and
    compare dispatch selection, schema validity, and latency."""

    def __init__(self, *, haiku_llm: IntentRouterLLM, qwen_llm: IntentRouterLLM) -> None:
        _require_router_llm("haiku_llm", haiku_llm)
        _require_router_llm("qwen_llm", qwen_llm)
        self._haiku_llm = haiku_llm
        self._qwen_llm = qwen_llm

    async def _run_side(
        self, llm: IntentRouterLLM, capture: RouterCapture, label: str
    ) -> _RouterSide:
        sensor = _InfraSensingLlm(llm)
        router = IntentRouter(llm=sensor)
        start = time.perf_counter()
        try:
            package = await router.decompose(
                action=capture.action,
                state_summary=capture.state_summary,
            )
        except IntentRouterFailure as exc:
            if sensor.infra_error is not None:
                # Infrastructure failure ≠ bad output: with the local daemon
                # down there is no meaningful A/B to record. Propagate so the
                # CLI emits the operator-evidence no-op (48-4 contract).
                raise sensor.infra_error from exc
            dur = int((time.perf_counter() - start) * 1000)
            logger.info("router_ab_eval.%s_invalid error=%s", label, exc)
            return _RouterSide(
                package=None,
                valid=False,
                errors=[f"{label}: {exc}"],
                latency_ms=dur,
            )
        dur = int((time.perf_counter() - start) * 1000)
        return _RouterSide(package=package, valid=True, errors=[], latency_ms=dur)

    async def eval_capture(self, capture: RouterCapture) -> RouterAbEvalResult:
        """Evaluate one capture on both backends. Per-side isolation: one
        backend's API/schema failure never loses the other side's result —
        only an ``OllamaClientError`` (infrastructure) propagates."""
        haiku_side = await self._run_side(self._haiku_llm, capture, "haiku")
        qwen_side = await self._run_side(self._qwen_llm, capture, "qwen")
        agreement = (
            haiku_side.valid
            and qwen_side.valid
            and haiku_side.package is not None
            and qwen_side.package is not None
            and dispatch_selection_agreement(haiku_side.package, qwen_side.package)
        )
        return RouterAbEvalResult(
            action=capture.action,
            haiku_package=haiku_side.package,
            qwen_package=qwen_side.package,
            haiku_valid=haiku_side.valid,
            qwen_valid=qwen_side.valid,
            haiku_latency_ms=haiku_side.latency_ms,
            qwen_latency_ms=qwen_side.latency_ms,
            agreement=agreement,
            haiku_errors=list(haiku_side.errors),
            qwen_errors=list(qwen_side.errors),
        )

    async def eval_corpus(
        self,
        captures: Sequence[RouterCapture],
        sample_size: int | None = None,
    ) -> RouterAbEvalReport:
        """Evaluate a corpus and aggregate the three gate metrics."""
        chosen = list(captures[:sample_size] if sample_size is not None else captures)
        if not chosen:
            raise ValueError(
                "eval_corpus: empty corpus — an agreement metric over nothing "
                "would be a fabricated gate signal"
            )
        results: list[RouterAbEvalResult] = []
        for capture in chosen:
            results.append(await self.eval_capture(capture))

        n = len(results)
        per_type: dict[str, TypeAgreement] = {}
        for result in results:
            types: set[str] = set()
            for package in (result.haiku_package, result.qwen_package):
                if package is not None:
                    types |= {d.subsystem for d in _iter_dispatches(package)}
            for name in types:
                stat = per_type.setdefault(name, TypeAgreement(agreed=0, total=0))
                stat.total += 1
                if result.agreement:
                    stat.agreed += 1

        haiku_p50, haiku_p95 = latency_percentiles([float(r.haiku_latency_ms) for r in results])
        qwen_p50, qwen_p95 = latency_percentiles([float(r.qwen_latency_ms) for r in results])
        return RouterAbEvalReport(
            sample_size=n,
            timestamp=datetime.now(UTC).isoformat(),
            agreement_pct=100.0 * sum(1 for r in results if r.agreement) / n,
            haiku_schema_validity_pct=100.0 * sum(1 for r in results if r.haiku_valid) / n,
            qwen_schema_validity_pct=100.0 * sum(1 for r in results if r.qwen_valid) / n,
            haiku_latency_p50_ms=haiku_p50,
            haiku_latency_p95_ms=haiku_p95,
            qwen_latency_p50_ms=qwen_p50,
            qwen_latency_p95_ms=qwen_p95,
            per_type_agreement=per_type,
            results=results,
        )
