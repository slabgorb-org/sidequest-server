"""Wire-first integration tests for namegen min-pool guard (Story 45-28).

The unit lane (``test_namegen_thresholds.py``) pins the threshold
constants and the stem-collision predicate in isolation. *This* file
pins the **wiring** — that the guards are actually invoked from the
production code paths and that the OTEL spans actually reach the
watcher hub through ``SPAN_ROUTES``.

The story's wire-first contract (``sprint/context/context-story-45-28.md``,
"The wire-first seam") names two seams:

1. ``sidequest/genre/names/generator.py:build_from_culture`` — corpus
   load + threshold check. Tests:
   ``test_thin_corpus_emits_thin_corpus_span``,
   ``test_sub_fail_corpus_raises_and_emits_fail_loud_span``.

2. ``sidequest/cli/namegen/namegen.py:generate_npc`` — sample-output
   stem-collision rejection loop. Tests:
   ``test_generate_npc_rejects_collision_and_emits_collision_span``,
   ``test_generate_npc_exhausts_collision_loop_and_emits_fail_loud_span``.

Plus three structural-wiring assertions that fail loud if the spans are
declared but never registered as routes (the GM-panel-blind regression):
``test_thin_corpus_span_routed_to_namegen_component``,
``test_fail_loud_span_routed_to_namegen_component``,
``test_stem_collision_span_routed_to_namegen_component``.

Per CLAUDE.md "Verify Wiring, Not Just Existence" and the OTEL
Observability Principle.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import random
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

# ---------------------------------------------------------------------------
# Synthetic corpus + Culture fixtures
# ---------------------------------------------------------------------------


def _write_corpus(path: Path, word_count: int) -> None:
    """Write a synthetic corpus of ``word_count`` distinct lowercase words.

    The Markov chain doesn't care about content; it just consumes
    whitespace-split tokens. We write distinct strings so the chain
    actually has training material rather than a single repeated word
    (which would make the warn-but-not-fail tests degenerate into
    fail-on-empty-output tests instead).
    """
    # 26-letter base × 26 = 676 unique two-letter prefixes; we always
    # have headroom for word_count <= 1500.
    words: list[str] = []
    for i in range(word_count):
        a = chr(ord("a") + (i // 26) % 26)
        b = chr(ord("a") + i % 26)
        c = chr(ord("a") + (i // 676) % 26)
        words.append(f"{a}{b}{c}xyz{i}")
    path.write_text(" ".join(words), encoding="utf-8")


@pytest.fixture
def synthetic_corpus_dir(tmp_path: Path) -> Path:
    """Lay out a corpus directory mirroring genre_packs/<genre>/corpus/.

    Three files:
      - ``ample.txt`` — 1500 words, comfortably above WARN.
      - ``thin.txt`` — 300 words, below WARN, above FAIL.
      - ``floor.txt`` — 50 words, below FAIL (raises).
    """
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_corpus(corpus / "ample.txt", 1500)
    _write_corpus(corpus / "thin.txt", 300)
    _write_corpus(corpus / "floor.txt", 50)
    return corpus


def _make_culture(
    name: str,
    *,
    corpus_filename: str,
    person_pattern: str = "{given_name}",
) -> Any:  # type: ignore[misc]
    """Build a minimal Culture pointing at one corpus file.

    Imported lazily so this module loads even when Culture moves; the
    architect context pins the YAML schema as unchanged for this story.
    """
    from sidequest.genre.models.culture import (
        CorpusRef,
        Culture,
        CultureSlot,
    )

    return Culture(
        name=name,
        summary="synthetic test culture",
        description="synthetic test culture",
        slots={
            "given_name": CultureSlot(
                corpora=[CorpusRef(corpus=corpus_filename, weight=1.0)],
                lookback=2,
            ),
        },
        person_patterns=[person_pattern],
    )


# ---------------------------------------------------------------------------
# OTEL capture fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_spans() -> Iterator[InMemorySpanExporter]:
    """Install an in-memory exporter on the live tracer provider.

    Mirrors ``tests/agents/conftest.py::otel_capture`` — patching the
    ``tracer()`` helper alone is insufficient because production code
    paths close over the global provider through OTEL's tracer-lookup
    indirection. Installing a ``SimpleSpanProcessor`` on the live
    provider is the reliable observation path.
    """
    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


def _span_names(exporter: InMemorySpanExporter) -> list[str]:
    return [span.name for span in exporter.get_finished_spans()]


def _spans_named(exporter: InMemorySpanExporter, name: str) -> list[Any]:
    return [span for span in exporter.get_finished_spans() if span.name == name]


# ---------------------------------------------------------------------------
# build_from_culture seam — corpus-load threshold
# ---------------------------------------------------------------------------


def test_thin_corpus_emits_thin_corpus_span(
    synthetic_corpus_dir: Path,
    captured_spans: InMemorySpanExporter,
) -> None:
    """Loading a 300-word corpus fires ``namegen.thin_corpus`` exactly once.

    The chain must still produce output (300 > FAIL_BELOW_WORDS=200) —
    silent degradation is the bug we're fixing. The span is the GM
    panel's signal that the warning fired; without it, Sebastien
    cannot tell the difference between "namegen worked and the names
    are good" and "namegen worked and the corpus is paper-thin and we
    got lucky on this draw."
    """
    from sidequest.genre.names.generator import build_from_culture
    from sidequest.telemetry.spans import SPAN_NAMEGEN_THIN_CORPUS

    culture = _make_culture("Thin Test Culture", corpus_filename="thin.txt")
    rng = random.Random(45028)

    generator = build_from_culture(culture, synthetic_corpus_dir, rng)

    # Generator still works — chain is trained and produces a real name.
    # Silent degradation (empty string from a thin chain) is the bug we
    # came here to prevent; verify positively.
    name = generator.generate_person()
    assert isinstance(name, str) and len(name) >= 2, (
        f"thin-but-usable corpus must still produce a real name; got {name!r}"
    )

    thin_spans = _spans_named(captured_spans, SPAN_NAMEGEN_THIN_CORPUS)
    assert len(thin_spans) == 1, (
        f"expected exactly one {SPAN_NAMEGEN_THIN_CORPUS} span; "
        f"got {len(thin_spans)} (all spans: {_span_names(captured_spans)})"
    )

    attrs = thin_spans[0].attributes or {}
    assert attrs.get("corpus_name") == "thin.txt"
    assert attrs.get("word_count") == 300
    assert attrs.get("culture") == "Thin Test Culture"
    assert attrs.get("slot_name") == "given_name"
    # The threshold value travels in the span so the GM panel can show
    # "300 < 1000" rather than "300 < ?".
    assert attrs.get("threshold") == 1000


def test_ample_corpus_emits_no_threshold_spans(
    synthetic_corpus_dir: Path,
    captured_spans: InMemorySpanExporter,
) -> None:
    """A 1500-word corpus must NOT trigger any warn or fail span.

    Without this negative-case test, a future "warn always" regression
    would silently pass — every wire-first story gets at least one
    "the guard does not over-fire" assertion to keep the GM panel
    free of noise.
    """
    from sidequest.genre.names.generator import build_from_culture
    from sidequest.telemetry.spans import (
        SPAN_NAMEGEN_FAIL_LOUD,
        SPAN_NAMEGEN_THIN_CORPUS,
    )

    culture = _make_culture("Ample Test Culture", corpus_filename="ample.txt")
    rng = random.Random(45028)
    build_from_culture(culture, synthetic_corpus_dir, rng)

    assert _spans_named(captured_spans, SPAN_NAMEGEN_THIN_CORPUS) == []
    assert _spans_named(captured_spans, SPAN_NAMEGEN_FAIL_LOUD) == []


def test_sub_fail_corpus_raises_and_emits_fail_loud_span(
    synthetic_corpus_dir: Path,
    captured_spans: InMemorySpanExporter,
) -> None:
    """A 50-word corpus raises ``ValueError`` AND emits the fail-loud span.

    Both signals are load-bearing:

    - The raise propagates to the caller (CLI exits non-zero, narrator
      subprocess refuses to substitute a degenerate name silently).
    - The span fires *before* the raise so the GM panel sees the failure
      even when the caller swallows the exception (defense in depth
      against a future regression where someone wraps the call in
      ``try: ... except ValueError: pass``).
    """
    from sidequest.genre.names.generator import build_from_culture
    from sidequest.telemetry.spans import SPAN_NAMEGEN_FAIL_LOUD

    culture = _make_culture("Floor Test Culture", corpus_filename="floor.txt")
    rng = random.Random(45028)

    with pytest.raises(ValueError, match="floor.txt"):
        build_from_culture(culture, synthetic_corpus_dir, rng)

    fail_spans = _spans_named(captured_spans, SPAN_NAMEGEN_FAIL_LOUD)
    assert len(fail_spans) == 1, (
        f"fail-loud span must fire even when a raise follows; "
        f"got {len(fail_spans)} (all spans: {_span_names(captured_spans)})"
    )
    attrs = fail_spans[0].attributes or {}
    assert attrs.get("corpus_name") == "floor.txt"
    assert attrs.get("word_count") == 50
    assert attrs.get("culture") == "Floor Test Culture"
    assert attrs.get("reason") == "below_floor"


def test_names_file_resolves_in_corpus_dir(tmp_path: Path) -> None:
    """``CultureSlot.names_file`` resolves directly under ``corpus_dir``.

    Regression: an earlier resolver looked in ``corpus_dir.parent / 'names'``
    and silently produced zero names for every culture that used
    ``names_file:`` (the Tea & Murder pack — 12 slots — and a Monster Manual
    pregen that booted with ``npcs_after=0``). Pack authors put the
    curated name lists in ``corpus/`` alongside Markov sources; the
    resolver must follow the content.
    """
    from sidequest.genre.models.culture import Culture, CultureSlot
    from sidequest.genre.names.generator import build_from_culture

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "people.txt").write_text(
        "\n".join(["Alasdair", "Catriona", "Effie", "Hamish", "Iain"]),
        encoding="utf-8",
    )

    culture = Culture(
        name="Names File Culture",
        summary="curated list culture",
        description="curated list culture",
        slots={
            "given_name": CultureSlot(names_file="people.txt"),
        },
        person_patterns=["{given_name}"],
    )

    generator = build_from_culture(culture, corpus, random.Random(45028))
    name = generator.generate_person()
    assert name in {"Alasdair", "Catriona", "Effie", "Hamish", "Iain"}, (
        f"names_file slot must draw from the curated list under corpus/; got {name!r}"
    )


def test_thin_corpus_logs_warning(
    synthetic_corpus_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Thin-corpus path must call ``logger.warning`` (python.md rule #4).

    OTEL is the canonical lie-detector signal, but CLI users running
    ``namegen`` interactively don't tail OTEL. ``logger.warning`` is
    the stdout-visible companion. Both must fire — the OTEL span tests
    above pin OTEL; this test pins the logger emission.
    """
    import logging

    from sidequest.genre.names.generator import build_from_culture

    culture = _make_culture("Thin Test Culture", corpus_filename="thin.txt")
    rng = random.Random(45028)

    with caplog.at_level(logging.WARNING):
        build_from_culture(culture, synthetic_corpus_dir, rng)

    matches = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "thin.txt" in record.getMessage()
    ]
    assert matches, (
        f"expected a logger.warning mentioning 'thin.txt'; got "
        f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# generate_npc seam — sample-output stem-collision rejection loop
# ---------------------------------------------------------------------------


def _stub_namespace(genre: str = "space_opera") -> argparse.Namespace:
    """Minimal argparse.Namespace that ``generate_npc`` reads from.

    Mirrors the surface ``cli/namegen/namegen.py:generate_npc`` consults
    — anything it doesn't read can stay None.
    """
    return argparse.Namespace(
        genre=genre,
        world=None,
        culture=None,
        archetype=None,
        gender=None,
        role=None,
        description=None,
        jungian=None,
        rpg_role=None,
        npc_role=None,
    )


def test_generate_npc_rejects_collision_and_emits_collision_span(
    monkeypatch: pytest.MonkeyPatch,
    captured_spans: InMemorySpanExporter,
) -> None:
    """Each rejected stem-collision attempt fires a ``namegen.stem_collision`` span.

    We mock ``NameGenerator.generate_person`` to return a fixed sequence:
    three collision names followed by a clean name. ``generate_npc``
    must:

    1. Reject the three collisions (emitting one stem_collision span
       each, with ``attempt_index`` 0..2).
    2. Accept the fourth (clean) name.
    3. Return the clean name in the resulting NpcBlock.
    """
    from sidequest.cli.namegen import namegen as namegen_cli
    from sidequest.genre.names.generator import NameGenerator
    from sidequest.telemetry.spans import SPAN_NAMEGEN_STEM_COLLISION

    candidates = iter(
        [
            "Frandrew Andrew",  # collision #0
            "Andrewson Andrew",  # collision #1
            "Pranderil Anderil",  # collision #2
            "Solenne Veradaine",  # clean
        ]
    )

    def fake_generate_person(self: NameGenerator, pattern: str | None = None) -> str:  # noqa: ARG001
        return next(candidates)

    monkeypatch.setattr(NameGenerator, "generate_person", fake_generate_person)

    pack, genre_dir = _load_test_pack()
    args = _stub_namespace()
    rng = random.Random(45028)

    npc = namegen_cli.generate_npc(pack, genre_dir, args, rng)

    assert npc.name == "Solenne Veradaine", (
        f"generate_npc must return the first non-collision candidate; got {npc.name!r}"
    )

    collision_spans = _spans_named(captured_spans, SPAN_NAMEGEN_STEM_COLLISION)
    assert len(collision_spans) == 3, (
        f"three rejected candidates must each emit one stem_collision "
        f"span; got {len(collision_spans)} (names: "
        f"{[s.attributes.get('candidate') for s in collision_spans]})"
    )
    rejected_names = [(s.attributes or {}).get("candidate") for s in collision_spans]
    assert rejected_names == [
        "Frandrew Andrew",
        "Andrewson Andrew",
        "Pranderil Anderil",
    ]
    # attempt_index ascends 0..2 so the GM panel can render a chronological
    # rejection trail rather than three identical-looking events.
    indices = sorted((s.attributes or {}).get("attempt_index") for s in collision_spans)
    assert indices == [0, 1, 2]


def test_generate_npc_exhausts_collision_loop_and_emits_fail_loud_span(
    monkeypatch: pytest.MonkeyPatch,
    captured_spans: InMemorySpanExporter,
) -> None:
    """When every attempt collides, ``generate_npc`` emits ``fail_loud``.

    The architect context calls out the fallback path: the loop runs
    up to its budget; if every candidate is a collision, the final
    span must carry ``reason="stem_collision_exhausted"`` so the GM
    panel can distinguish "thin corpus" from "totally degenerate
    output" without the operator squinting at attribute values.
    """
    from sidequest.cli.namegen import namegen as namegen_cli
    from sidequest.genre.names.generator import NameGenerator
    from sidequest.telemetry.spans import (
        SPAN_NAMEGEN_FAIL_LOUD,
        SPAN_NAMEGEN_STEM_COLLISION,
    )

    def always_collide(self: NameGenerator, pattern: str | None = None) -> str:  # noqa: ARG001
        return "Frandrew Andrew"

    monkeypatch.setattr(NameGenerator, "generate_person", always_collide)

    pack, genre_dir = _load_test_pack()
    args = _stub_namespace()
    rng = random.Random(45028)

    # Whether the function returns a degenerate name or raises is a
    # design decision left to Dev (logged as a Delivery Finding).
    # Either way: the fail_loud span MUST fire.
    with contextlib.suppress(ValueError, RuntimeError):
        namegen_cli.generate_npc(pack, genre_dir, args, rng)

    fail_spans = _spans_named(captured_spans, SPAN_NAMEGEN_FAIL_LOUD)
    assert len(fail_spans) == 1, (
        f"loop exhaustion must fire exactly one fail_loud span; "
        f"got {len(fail_spans)} (all spans: {_span_names(captured_spans)})"
    )
    attrs = fail_spans[0].attributes or {}
    assert attrs.get("reason") == "stem_collision_exhausted"

    # The intermediate rejections also fire (one per attempt). The
    # number is the loop budget — pinned at ≥ 5 so a future budget cut
    # to e.g. 3 surfaces here as a deviation rather than silently
    # under-reporting rejections.
    rejection_spans = _spans_named(captured_spans, SPAN_NAMEGEN_STEM_COLLISION)
    assert len(rejection_spans) >= 5, (
        f"loop exhaustion must rack up at least 5 rejection spans "
        f"before failing loud; got {len(rejection_spans)}"
    )


# ---------------------------------------------------------------------------
# Structural-wiring assertions — SPAN_ROUTES registration + state_transition
# ---------------------------------------------------------------------------


def test_thin_corpus_span_routed_to_namegen_component() -> None:
    """``SPAN_NAMEGEN_THIN_CORPUS`` is registered in SPAN_ROUTES.

    Without a route, the span name lands in the dashboard's flat
    "agent_span_close" feed and the GM panel cannot render a typed
    namegen tab — Sebastien's mechanical visibility is silently lost.
    """
    from sidequest.telemetry.spans import (
        SPAN_NAMEGEN_THIN_CORPUS,
        SPAN_ROUTES,
    )

    assert SPAN_NAMEGEN_THIN_CORPUS in SPAN_ROUTES, (
        "thin_corpus span must be routed (component=namegen, "
        "event_type=state_transition) — see context-story-45-28.md "
        "'OTEL spans (LOAD-BEARING)'."
    )
    route = SPAN_ROUTES[SPAN_NAMEGEN_THIN_CORPUS]
    assert route.event_type == "state_transition"
    assert route.component == "namegen"


def test_fail_loud_span_routed_to_namegen_component() -> None:
    """``SPAN_NAMEGEN_FAIL_LOUD`` is registered in SPAN_ROUTES."""
    from sidequest.telemetry.spans import (
        SPAN_NAMEGEN_FAIL_LOUD,
        SPAN_ROUTES,
    )

    assert SPAN_NAMEGEN_FAIL_LOUD in SPAN_ROUTES
    route = SPAN_ROUTES[SPAN_NAMEGEN_FAIL_LOUD]
    assert route.event_type == "state_transition"
    assert route.component == "namegen"


def test_stem_collision_span_routed_to_namegen_component() -> None:
    """``SPAN_NAMEGEN_STEM_COLLISION`` is registered in SPAN_ROUTES."""
    from sidequest.telemetry.spans import (
        SPAN_NAMEGEN_STEM_COLLISION,
        SPAN_ROUTES,
    )

    assert SPAN_NAMEGEN_STEM_COLLISION in SPAN_ROUTES
    route = SPAN_ROUTES[SPAN_NAMEGEN_STEM_COLLISION]
    assert route.event_type == "state_transition"
    assert route.component == "namegen"


@pytest.mark.asyncio
async def test_thin_corpus_state_transition_reaches_watcher_hub(
    monkeypatch: pytest.MonkeyPatch,
    synthetic_corpus_dir: Path,
) -> None:
    """End-to-end: span → SPAN_ROUTES → WatcherSpanProcessor → hub subscriber.

    This is the GM-panel proof-test. It mirrors the npc_wiring pattern
    in ``tests/integration/test_npc_wiring.py``. If this passes, the
    dashboard renders a typed ``state_transition`` event for
    ``component=namegen``; if it fails, the span exists but is invisible
    to anyone watching the panel — which is exactly the silent-failure
    mode CLAUDE.md OTEL principle exists to catch.
    """
    from sidequest.genre.names.generator import build_from_culture
    from sidequest.server.watcher import WatcherSpanProcessor
    from sidequest.telemetry import spans as spans_module
    from sidequest.telemetry.watcher_hub import watcher_hub

    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001

    captured: list[dict] = []

    class _Sock:
        async def send_json(self, data: dict) -> None:
            captured.append(data)

    await watcher_hub.subscribe(_Sock())  # type: ignore[arg-type]

    provider = TracerProvider()
    provider.add_span_processor(WatcherSpanProcessor(watcher_hub))
    local_tracer = provider.get_tracer("test-namegen-thin-corpus-wiring")
    monkeypatch.setattr(spans_module, "tracer", lambda: local_tracer)

    culture = _make_culture("Thin Test Culture", corpus_filename="thin.txt")
    rng = random.Random(45028)
    build_from_culture(culture, synthetic_corpus_dir, rng)

    await asyncio.sleep(0.05)

    typed = [
        e
        for e in captured
        if e["event_type"] == "state_transition"
        and e["component"] == "namegen"
        and e["fields"].get("op") == "thin_corpus"
    ]
    assert len(typed) == 1, (
        "expected exactly one routed thin_corpus state_transition reaching "
        f"the hub (got {len(typed)} typed; {len(captured)} total events)"
    )
    fields = typed[0]["fields"]
    assert fields["corpus_name"] == "thin.txt"
    assert fields["word_count"] == 300
    assert fields["culture"] == "Thin Test Culture"
    assert fields["threshold"] == 1000


# ---------------------------------------------------------------------------
# Test pack loader — minimal in-tree pack so generate_npc has cultures
# ---------------------------------------------------------------------------


def _load_test_pack() -> tuple[Any, Path]:
    """Load a minimal genre pack from sidequest-content for end-to-end tests.

    The architect context says the wire-first scenario "calls through
    ``cli.namegen.namegen.generate_npc``, not through a stub name-only
    helper". We honor that by loading a real pack — ``space_opera`` is
    the playtest pack the bug surfaced in. The synthetic-corpus tests
    use tmp_path fixtures; the rejection-loop tests reuse the real pack
    because ``generate_npc`` consults archetypes / axes / OCEAN
    machinery that's not worth synthesising.
    """
    from sidequest.genre import load_genre_pack

    repo_root = Path(__file__).resolve().parents[2]
    content = repo_root.parent / "sidequest-content"
    pack_dir = content / "genre_packs" / "space_opera"
    pack = load_genre_pack(pack_dir)
    return pack, pack_dir
