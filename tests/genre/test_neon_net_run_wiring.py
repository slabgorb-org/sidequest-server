"""End-to-end wiring test: neon_dystopia net_run through dispatch (Task 7).

Proves the FULL chain with the REAL on-disk ``neon_dystopia`` pack — no synthetic
fixture pack:

    real neon YAML (cwn.hacking + net_run confrontation)
      → ``load_pack`` (validated CwnConfig / HackingConfig)
        → ``dispatch_dice_throw`` (net_run branch, 2d6 Program check)
          → ``cwn.hacking.security_check`` OTEL span (lie-detector)

Per CLAUDE.md "No Source-Text Wiring Tests": all assertions are CONFIG truth
(loaded pydantic models) or BEHAVIOR (OTEL span attributes + engine state after
driving real dispatch). No source-greps.

Content-on-disk guard mirrors ``tests/genre/test_neon_loads_cwn.py``:
- Skip ONLY when sidequest-content is genuinely absent.
- If ``load_pack`` RAISES that is a REAL content defect — the exception surfaces,
  it is NOT swallowed by a skip.

otel_capture: tests/genre has no shared conftest for this; the fixture is
defined locally, mirroring the one in ``test_neon_combat_lethality_wiring.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from tests._helpers.genre_paths import GENRE_PACKS_DIR, find_pack_path

# ---------------------------------------------------------------------------
# Content guard — checked at collection time, same as sibling wiring tests.
# ---------------------------------------------------------------------------
_HAS_CONTENT = GENRE_PACKS_DIR.is_dir()


def _load_neon():
    from sidequest.genre.loader import load_genre_pack

    return load_genre_pack(find_pack_path("neon_dystopia"))


# ---------------------------------------------------------------------------
# otel_capture fixture (local copy — tests/genre has no shared conftest for it).
# ---------------------------------------------------------------------------
@pytest.fixture
def otel_capture() -> Iterator:
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from sidequest.telemetry.setup import init_tracer

    init_tracer()
    provider = otel_trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    # Drop accumulated processors from prior invocations (mirrors server conftest).
    provider._active_span_processor._span_processors = ()  # type: ignore[attr-defined]

    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    try:
        yield exporter
    finally:
        processor.shutdown()


# ---------------------------------------------------------------------------
# Shared helpers — encounter + dispatch driver.
# ---------------------------------------------------------------------------
def _make_encounter(
    *,
    data_current: int = 0,
    alert_current: int = 0,
    security_tier: str = "black_site",
):
    from sidequest.game.encounter import (
        EncounterActor,
        EncounterMetric,
        EncounterPhase,
        StructuredEncounter,
    )

    return StructuredEncounter(
        encounter_type="net_run",
        player_metric=EncounterMetric(
            name="data", current=data_current, starting=data_current, threshold=10
        ),
        opponent_metric=EncounterMetric(
            name="alert", current=alert_current, starting=alert_current, threshold=10
        ),
        structured_phase=EncounterPhase.Setup,
        actors=[EncounterActor(name="Ghost", role="runner", side="player")],
        security_tier=security_tier,
        mood_override=None,
        narrator_hints=[],
    )


# character_stats: Tech=14 → INT mod +1 (SWN curve); run_program combat_skill=1
# → modifier = +1 + 1 = +2.
_STATS = {"Tech": 14}


def _drive_beat(
    *,
    pack,
    enc,
    faces: list[int],
    beat_id: str = "run_program",
):
    from sidequest.game.session import GameSnapshot
    from sidequest.protocol.dice import DiceThrowPayload, ThrowParams
    from sidequest.server.dispatch.dice import dispatch_dice_throw

    snap = GameSnapshot()
    snap.genre_slug = "neon_dystopia"

    return dispatch_dice_throw(
        payload=DiceThrowPayload(
            request_id=str(uuid.uuid4()),
            throw_params=ThrowParams(velocity=(0, 0, 0), angular=(0, 0, 0), position=(0, 0)),
            face=faces,
            beat_id=beat_id,
        ),
        rolling_player_id="player-ghost",
        character_name="Ghost",
        character_stats=dict(_STATS),
        encounter=enc,
        pack=pack,
        genre_slug="neon_dystopia",
        session_id="neon-net-run-wiring",
        round_number=1,
        room_broadcast=[].append,
        snapshot=snap,
    )


# ---------------------------------------------------------------------------
# 1: Config / content wiring — no database, no dispatch.  MUST run on disk.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _HAS_CONTENT, reason="sidequest-content not on disk")
def test_neon_net_run_config_and_confrontation() -> None:
    """Real neon pack carries the CWN hacking config and a net_run confrontation.

    If ``load_pack`` raises (a real validation failure from the content tasks),
    the exception surfaces here — it is NOT masked by the content guard.
    """
    pack = _load_neon()

    # CWN hacking config is present and the black_site tier is correct.
    assert pack.rules.cwn is not None
    assert pack.rules.cwn.hacking is not None
    assert pack.rules.cwn.hacking.security_tiers["black_site"] == 12

    # net_run confrontation exists; net_combat does not.
    types = [c.confrontation_type for c in pack.rules.confrontations]
    assert "net_run" in types, f"neon_dystopia must declare a net_run confrontation; got: {types}"
    assert "net_combat" not in types, (
        f"net_combat must have been retired in favour of net_run; got: {types}"
    )

    # net_run has the right shape.
    nr = next(c for c in pack.rules.confrontations if c.confrontation_type == "net_run")
    assert nr.category == "hacking"
    assert nr.player_metric is not None and nr.player_metric.name == "data"
    assert nr.opponent_metric is not None and nr.opponent_metric.name == "alert"

    # run_program beat is present.
    beat_ids = [b.id for b in nr.beats]
    assert "run_program" in beat_ids, f"net_run must have a run_program beat; got: {beat_ids}"


# ---------------------------------------------------------------------------
# 2: Dispatch-drive — cwn.hacking.security_check span fires through real pack.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _HAS_CONTENT, reason="sidequest-content not on disk")
def test_neon_net_run_fires_security_check_through_dispatch(otel_capture) -> None:
    """Driving run_program through dispatch on the real neon pack fires the span.

    This is the load-bearing wiring assertion: REAL pack → CwnRulesetModule →
    cwn.hacking config → dispatch seam → cwn.hacking.security_check OTEL span.

    - black_site DC=12, alert=0 → effective_dc=12.
    - Faces [6,6] + modifier(+2) = 14 > 12 → Success.
    - The span must carry tier="black_site" and effective_dc=12.
    """
    pack = _load_neon()
    enc = _make_encounter(security_tier="black_site")

    # Faces [6,6] → total 14, well above DC 12 (Success).
    _drive_beat(pack=pack, enc=enc, faces=[6, 6])

    span_names = [s.name for s in otel_capture.get_finished_spans()]
    assert "cwn.hacking.security_check" in span_names, (
        "cwn.hacking.security_check must fire when the real neon net_run "
        "dispatches through the CWN module — proves pack(hacking config) → "
        "registry → dispatch reachability; "
        f"got spans: {span_names}"
    )

    # Verify span attributes are correct (not a vacuous name check).
    check_span = next(
        s for s in otel_capture.get_finished_spans() if s.name == "cwn.hacking.security_check"
    )
    attrs = check_span.attributes or {}
    assert attrs.get("tier") == "black_site", (
        f"span must carry tier=black_site; got {attrs.get('tier')!r}"
    )
    assert attrs.get("effective_dc") == 12, (
        f"effective_dc must be 12 (black_site base=12, alert=0); got {attrs.get('effective_dc')!r}"
    )


# ---------------------------------------------------------------------------
# 3: Player-win path — data reaches threshold → player_victory.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _HAS_CONTENT, reason="sidequest-content not on disk")
def test_neon_net_run_player_win_path(otel_capture) -> None:
    """A run_program Success when data=8 pushes data to 10 → player_victory.

    run_program (strike, base=2): Success → own_delta = +2.
    Starting from data=8, one Success (faces [6,6] → total 14 > DC 12) reaches
    data=10 >= threshold=10 → encounter resolves as player_victory.
    """
    pack = _load_neon()
    enc = _make_encounter(data_current=8, alert_current=0, security_tier="black_site")

    outcome = _drive_beat(pack=pack, enc=enc, faces=[6, 6])

    assert outcome.encounter_resolved is True, (
        "encounter must resolve when player data reaches threshold; "
        f"encounter_resolved={outcome.encounter_resolved}"
    )
    assert enc.outcome == "player_victory", f"outcome must be player_victory; got {enc.outcome!r}"
    assert enc.player_metric.current >= enc.player_metric.threshold, (
        f"player data must be at or above threshold; "
        f"current={enc.player_metric.current} threshold={enc.player_metric.threshold}"
    )


# ---------------------------------------------------------------------------
# 4: Opponent-win path — a failed Program check spikes alert to threshold.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _HAS_CONTENT, reason="sidequest-content not on disk")
def test_neon_net_run_opponent_win_path(otel_capture) -> None:
    """A failed run_program spikes the alert via the fail delta → opponent_victory.

    This is the ORGANIC loss path: the network wins when a botched Program check
    trips the alarm. run_program carries a fail delta (own:-1, opponent:+2) — a
    2d6 Program check resolves to RollOutcome.Fail when it lands under the
    security DC (CritFail is a d20-nat-1 tier only, never a 2d6 outcome), so the
    alert spike rides ``fail``. The ``strike`` Fail default is empty ``{}``, so
    this override is what actually advances the alert.

    - black_site DC=12, alert starts at 8 (below threshold=10).
    - Faces [1,1] + modifier(+2) = 4, well under DC 12 → Fail.
    - The fail delta fires opponent:+2 → alert 8 → 10 >= threshold → the
      threshold check in apply_beat resolves the run as opponent_victory.
    """
    pack = _load_neon()
    enc = _make_encounter(data_current=0, alert_current=8, security_tier="black_site")

    alert_before = enc.opponent_metric.current
    outcome = _drive_beat(pack=pack, enc=enc, faces=[1, 1])

    # The failed Program check must have spiked the alert by +2 (fail delta fired).
    assert enc.opponent_metric.current == alert_before + 2, (
        "the fail delta (opponent:+2) must have spiked the alert; "
        f"before={alert_before} after={enc.opponent_metric.current}"
    )
    assert outcome.encounter_resolved is True, (
        "encounter must resolve when opponent alert reaches threshold; "
        f"encounter_resolved={outcome.encounter_resolved}"
    )
    assert enc.outcome == "opponent_victory", (
        f"outcome must be opponent_victory; got {enc.outcome!r}"
    )
    assert enc.opponent_metric.current >= enc.opponent_metric.threshold, (
        f"opponent alert must be at or above threshold; "
        f"current={enc.opponent_metric.current} threshold={enc.opponent_metric.threshold}"
    )
