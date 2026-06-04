"""Story 75-15 (AC2/AC5) — make the embedding model + similarity-vs-floor
legible so the GM panel can tell *why* retrieval returned nothing.

Gulliver 2026-06-02: of 36 ``lore_retrieval`` events, 18 returned
``selected_count=0`` and the highest cosine recorded all session was 0.1499 —
*below* the ``min_similarity=0.15`` floor. The OTEL principle's worst failure
class: telemetry says "retrieval ran" while nothing was retrieved, and the panel
cannot distinguish:

- store empty (nothing to retrieve), from
- everything below the floor (a model/calibration starve), from
- a degenerate / hash-fallback embedding (No Silent Fallbacks).

These are unit tests against ``retrieve_lore_context`` / ``embed_pending_fragments``
with a fake ``DaemonClient`` (no socket, no daemon), capturing the watcher event
the GM panel consumes (``sidequest.telemetry.watcher_hub.publish_event``, imported
at call-time inside ``retrieve_lore_context``).

NOTE on AC3 (relevant lore clears the floor): the empirical fix — whether by
model fix, floor calibration, or fragment granularity — is a measurement against
the LIVE daemon and is verified by Dev per AC6's PR cross-check, not unit-tested
here (the daemon is absent in the unit suite). These tests deliver the telemetry
that makes that measurement legible; see the TEA assessment's Design Deviations.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from sidequest.game.lore_embedding import (
    DEFAULT_RETRIEVAL_MIN_SIMILARITY,
    embed_pending_fragments,
    retrieve_lore_context,
)
from sidequest.game.lore_store import (
    LoreCategory,
    LoreFragment,
    LoreSource,
    LoreStore,
)

_REAL_MODEL = "all-MiniLM-L6-v2"


class _FakeClient:
    """Duck-typed DaemonClient: returns a fixed embedding + model for every
    embed() call. Only ``embed`` / ``is_available`` are used by the helpers.
    """

    def __init__(self, *, embedding: list[float], model: str = _REAL_MODEL) -> None:
        self._embedding = embedding
        self._model = model
        self.calls: list[str] = []
        self.socket_path = "/tmp/fake-sock"

    def is_available(self) -> bool:
        return True

    async def embed(self, text: str) -> dict[str, Any]:
        self.calls.append(text)
        return {"embedding": list(self._embedding), "model": self._model, "latency_ms": 1}


def _embedded_frag(id_: str, embedding: list[float], content: str = "lore body text") -> LoreFragment:
    frag = LoreFragment.new(
        id=id_,
        category=LoreCategory.History,
        content=content,
        source=LoreSource.GenrePack,
    )
    frag.embedding = list(embedding)
    frag.embedding_pending = False
    return frag


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """Capture every watcher event the lore path publishes.

    ``retrieve_lore_context`` does ``from sidequest.telemetry.watcher_hub import
    publish_event`` at call-time, so patching the module attribute is sufficient.
    """
    import sidequest.telemetry.watcher_hub as watcher_hub

    events: list[tuple[str, dict]] = []

    def _capture(event_type, fields, **kwargs):  # noqa: ANN001
        events.append((event_type, dict(fields)))

    monkeypatch.setattr(watcher_hub, "publish_event", _capture)
    return events


def _lore_events(captured: list[tuple[str, dict]]) -> list[dict]:
    return [f for et, f in captured if et == "lore_retrieval"]


# ---------------------------------------------------------------------------
# AC5 — distinguish "store empty" from "nothing cleared the floor".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_empty_store_emits_panel_event(
    captured_events: list[tuple[str, dict]],
) -> None:
    """An empty store must still emit a panel-visible retrieval event so the GM
    panel can tell "store empty" from "retrieval never ran".

    Pre-fix: ``retrieve_lore_context`` returns ``None`` for an empty store BEFORE
    the ``_watcher_publish`` call — so NO ``lore_retrieval`` event fires and the
    panel is blind to the empty-store case.
    """
    store = LoreStore()  # empty
    fake = _FakeClient(embedding=[1.0, 0.0])

    result = await retrieve_lore_context(store, "what do I know?", client=fake)
    assert result is None

    events = _lore_events(captured_events)
    assert events, (
        "empty store must still emit a lore_retrieval watcher event (with an "
        "empty-store outcome) so the panel distinguishes it from 'never ran'; "
        f"captured: {[et for et, _ in captured_events]}"
    )
    outcomes = {f.get("outcome") for f in events}
    assert outcomes & {"empty_query_or_store", "store_empty", "empty_store"}, (
        f"empty-store retrieval event must carry an empty-store outcome; got {outcomes}"
    )


@pytest.mark.asyncio
async def test_retrieve_below_floor_emits_outcome_and_peak_similarity(
    captured_events: list[tuple[str, dict]],
) -> None:
    """When every candidate scores below the floor, the retrieval event must
    record BOTH a distinct "nothing cleared the floor" outcome AND the peak
    similarity — exactly the two numbers the gulliver bug needed (peak 0.1499 vs
    floor 0.15).

    Pre-fix: the watcher event has no ``outcome`` field and no explicit
    ``peak_similarity`` — the panel cannot answer "did anything come close?".
    """
    query = [1.0, 0.0, 0.0]
    # Near-orthogonal fragment: cosine = 0.1 / sqrt(1.01) ~= 0.0995 (< 0.15 floor).
    frag_vec = [0.1, 1.0, 0.0]
    expected_peak = 0.1 / (math.sqrt(1.0) * math.sqrt(0.1 * 0.1 + 1.0))
    assert expected_peak < DEFAULT_RETRIEVAL_MIN_SIMILARITY  # sanity: genuinely sub-floor

    store = LoreStore()
    store.add(_embedded_frag("frag_below_floor", frag_vec))
    fake = _FakeClient(embedding=query)

    result = await retrieve_lore_context(store, "a query about something else", client=fake)
    assert result is None, "sub-floor candidate must not be selected"

    events = _lore_events(captured_events)
    assert events, "below-floor retrieval must still emit a lore_retrieval event"
    ev = events[-1]
    assert ev.get("outcome") in {"no_hits_above_threshold", "below_floor"}, (
        f"below-floor event must carry a distinct outcome; got {ev.get('outcome')!r}"
    )
    peak = ev.get("peak_similarity")
    assert peak is not None, (
        "below-floor event must record the peak similarity so the panel can see "
        "how close the best candidate came to the floor"
    )
    assert peak == pytest.approx(expected_peak, abs=1e-3), (
        f"peak_similarity must equal the best candidate's cosine; "
        f"got {peak}, expected ~{expected_peak:.4f}"
    )
    assert float(ev.get("min_similarity", -1)) == pytest.approx(DEFAULT_RETRIEVAL_MIN_SIMILARITY)


# ---------------------------------------------------------------------------
# AC2 — record which model produced the embedding (no silent fallback).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_records_embedding_model(
    captured_events: list[tuple[str, dict]],
) -> None:
    """A successful retrieval must surface the embedding model name so a
    degenerate / hash-fallback model is detectable on the panel.

    Pre-fix: ``retrieve_lore_context`` reads ``response["embedding"]`` but throws
    away ``response["model"]`` — the panel can't tell MiniLM from a fallback.
    """
    query = [1.0, 0.0]
    store = LoreStore()
    store.add(_embedded_frag("frag_hit", query))  # identical vector -> cosine 1.0
    fake = _FakeClient(embedding=query, model=_REAL_MODEL)

    result = await retrieve_lore_context(store, "find the matching lore", client=fake)
    assert result is not None, "an identical-vector fragment must clear the floor"

    events = _lore_events(captured_events)
    assert events, "successful retrieval must emit a lore_retrieval event"
    models = {f.get("embedding_model") for f in events}
    assert _REAL_MODEL in models, (
        "retrieval event must record the embedding model name reported by the "
        f"daemon; got embedding_model values {models}"
    )


@pytest.mark.asyncio
async def test_retrieve_flags_nan_embedding_as_degenerate(
    captured_events: list[tuple[str, dict]],
) -> None:
    """REWORK (Reviewer [EDGE]): a NaN/Inf query embedding must also surface the
    degenerate outcome. The zero-magnitude guard (`== 0.0`) does NOT catch NaN
    (NaN != 0.0) or Inf, so a corrupt/truncated daemon reply would sail past it
    and poison the cosine math + `peak_similarity` with NaN.
    """
    store = LoreStore()
    store.add(_embedded_frag("frag_any", [1.0, 0.0]))
    fake = _FakeClient(embedding=[float("nan"), 0.0], model="hash-fallback")

    result = await retrieve_lore_context(store, "a real query", client=fake)
    assert result is None

    events = _lore_events(captured_events)
    assert events, "NaN-embedding retrieval must still emit an event"
    outcomes = {f.get("outcome") for f in events}
    assert outcomes & {"degenerate_embedding", "zero_magnitude_embedding", "non_finite_embedding"}, (
        "a non-finite (NaN/Inf) query embedding must surface a distinct degenerate "
        f"outcome, not a silent no-hits with a NaN peak; got outcomes {outcomes}"
    )
    peaks = [f.get("peak_similarity") for f in events]
    assert all(p is None or (isinstance(p, float) and math.isfinite(p)) for p in peaks), (
        f"peak_similarity must never be NaN/Inf; got {peaks}"
    )


@pytest.mark.asyncio
async def test_retrieve_flags_degenerate_zero_magnitude_embedding(
    captured_events: list[tuple[str, dict]],
) -> None:
    """A zero-magnitude query embedding (the degenerate / hash-fallback smell)
    must surface a LOUD, distinct outcome — not a silent "no hits".

    ``cosine_similarity`` returns 0.0 for a zero-magnitude vector, so today a
    degenerate embedding looks identical to "nothing relevant" — masking a model
    failure (No Silent Fallbacks).
    """
    store = LoreStore()
    store.add(_embedded_frag("frag_any", [1.0, 0.0]))
    fake = _FakeClient(embedding=[0.0, 0.0], model="hash-fallback")

    result = await retrieve_lore_context(store, "a real query", client=fake)
    assert result is None

    events = _lore_events(captured_events)
    assert events, "degenerate-embedding retrieval must still emit an event"
    outcomes = {f.get("outcome") for f in events}
    assert outcomes & {"degenerate_embedding", "zero_magnitude_embedding"}, (
        "a zero-magnitude query embedding must surface a distinct degenerate "
        f"outcome (not a silent no-hits); got outcomes {outcomes}"
    )


# ---------------------------------------------------------------------------
# AC2 — the embed worker telemetry must also surface the model name.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_worker_surfaces_model_in_telemetry() -> None:
    """``embed_pending_fragments`` writes embeddings back from the daemon's
    reply, which carries ``model``. The worker's telemetry payload
    (``EmbedWorkerResult.as_dict``) must surface that model name so the panel can
    confirm the SAME real model embedded the corpus that retrieval queries with.

    Pre-fix: ``EmbedWorkerResult`` has no model field and ``as_dict`` never
    reports it.
    """
    store = LoreStore()
    store.add(
        LoreFragment.new(
            id="pending_frag",
            category=LoreCategory.History,
            content="awaiting embedding",
            source=LoreSource.GenrePack,
        )
    )
    fake = _FakeClient(embedding=[1.0, 0.0], model=_REAL_MODEL)

    result = await embed_pending_fragments(store, client=fake)
    assert result.embedded == 1, "the pending fragment must be embedded by the fake"

    payload = result.as_dict()
    assert "embedding_model" in payload, (
        "EmbedWorkerResult.as_dict must surface the embedding model name so the "
        f"panel can verify the real model embedded the corpus; keys: {sorted(payload)}"
    )
    assert payload["embedding_model"] == _REAL_MODEL
