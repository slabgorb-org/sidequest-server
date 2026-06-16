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
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher

from sidequest.agents.claude_client import LlmClient
from sidequest.agents.ollama_client import OllamaClientError
from sidequest.corpus.schema import TrainingPair

logger = logging.getLogger(__name__)

# Model hints handed to send_stateless. The harness compares whatever each
# backend's factory/config resolves these to; the strings themselves are not
# load-bearing for the comparison.
CLAUDE_MODEL = "sonnet"
OLLAMA_MODEL = "qwen2.5:7b-instruct"


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
