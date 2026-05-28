"""RED tests for Story 60-2 — per-system-block cache attribution on the
``prompt_assembled`` watcher event (the GM-panel Prompt-tab "eyes").

Epic 60 ORIGINAL hypothesis (2026-05-22): the wasted ``cache_write`` was
believed to be ``system_blocks[0]`` (Primacy+Early, ``cache=True``) being
re-written every turn because three ``state``-category sections
(``narrator_available_confrontations``, ``trope_beat_directives``,
``npc_roster``) were mis-zoned into the cached Early zone.

CORRECTION (Story 60-3, measured later the same day): that hypothesis is
WRONG. Those three sections are User-bucket → ride the *uncached* user
message → never touch ``system_blocks[0]``. The cached prefix is byte-stable
(no drift). The real cause is the tool-use loop: continuation calls re-mint
the prefix at 5m because the growing conversation has no cache breakpoint
(60-4 fixes). These eyes (built here in 60-2) are still correct and were what
*enabled* 60-3 to disprove the hypothesis — but note the ``mis_zoned`` flag
below is zone-only/bucket-blind and false-positives on those User-bucket
sections; trust the per-section ``cached`` field. See 60-3 session.

The original Prompt-tab Zone Breakdown hid the cost because it was built from
a separate char/4 estimate path, decoupled from the real ``system_blocks`` and
the real API ``cache_read/cache_write``. So the lie-detector itself was lying;
60-2 (these tests) rebuilt it from the actual blocks.

These tests build the eyes. They assert that the ``prompt_assembled`` event,
emitted on a *real* narration turn, carries:

* AC-1 — a per-zone cache-boundary flag (Primacy/Early cached; Valley/Late/
  Recency uncached), derived from the SAME zone partition the SDK path uses.
* AC-2 — the real per-turn API cache usage (read/write, 5m/1h split, cost,
  ttl) from the SDK response — and an explicit ``None`` ("n/a") when no SDK
  usage is available (No Silent Fallbacks; never a char/4 estimate styled as
  an actual).
* AC-3 — a per-cacheable-block content digest (``sha256[:8]``) so the UI can
  show drift vs the previous turn.
* AC-4 — a ``mis_zoned`` flag on any ``state``-category section that landed
  in a cached *zone*. (Per 60-3: this flag is zone-only/bucket-blind — it
  does NOT prove the section rides the cached block; it false-positives on
  User-bucket state sections. It was NOT the real block-0 cost driver.)
* AC-5 — ACCURACY: the emitted partition + digests match the ACTUAL
  ``system_blocks`` the SDK client received on the same turn. The display
  cannot claim "stable" while the real prompt drifted.
* AC-6 — WIRING: the fields are emitted on the live ``prompt_assembled``
  path during a real ``run_narration_turn`` (behavior, not a source grep).

Contract (new ``prompt_assembled.fields`` keys these tests pin down):

    zones: [{zone, total_tokens, cached: bool,
             sections: [{name, token_estimate, category, mis_zoned: bool}]}]
    cache_blocks: [{label, digest, cached: bool}]   # label in {stable,valley,recency,tools}
    cache_usage: {cache_read, cache_write, cache_write_5m, cache_write_1h,
                  cost_usd, cache_ttl} | None
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from typing import Any

import pytest

# Importing the tools package wires the 26 adapters onto default_registry,
# matching the production SDK path's expectations (see
# test_cache_ttl_prefix_and_otel.py).
import sidequest.agents.tools  # noqa: F401
from sidequest.agents.claude_client import ClaudeResponse
from sidequest.agents.orchestrator import Orchestrator, TurnContext
from sidequest.telemetry.watcher_hub import WatcherHub, watcher_hub
from tests.agents.fakes.fake_anthropic_sdk_client import (
    FakeAnthropicSdkClient,
    ScriptedResponse,
)

CACHED_ZONES = {"Primacy", "Early"}
UNCACHED_ZONES = {"Valley", "Late", "Recency"}


class _FakeSocket:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.events.append(data)


class _CannedClient:
    """Non-tooling LlmClient — exercises the build-time path WITHOUT real
    SDK usage, so the 'n/a loudly' contract (AC-2) can be checked."""

    async def send(self, prompt: str, **_: Any) -> ClaudeResponse:
        return ClaudeResponse(text="ok", duration_ms=0)


def _scripted(
    text: str = "ok",
    *,
    cache_read: int = 0,
    cache_write: int = 0,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
) -> ScriptedResponse:
    return ScriptedResponse(
        text=text,
        stop_reason="end_turn",
        input_tokens=500,
        output_tokens=80,
        cached_input_read_tokens=cache_read,
        cached_input_write_tokens=cache_write,
        cached_input_write_5m_tokens=cache_write_5m,
        cached_input_write_1h_tokens=cache_write_1h,
        model="claude-sonnet-4-6",
    )


@pytest.fixture
async def bound_hub() -> WatcherHub:
    watcher_hub.bind_loop(asyncio.get_running_loop())
    async with watcher_hub._lock:  # noqa: SLF001
        watcher_hub._subscribers.clear()  # noqa: SLF001
    return watcher_hub


def _enriched_event(sock: _FakeSocket) -> dict[str, Any]:
    """Return the ``prompt_assembled`` event carrying the new cache
    attribution (``cache_blocks``). Robust to a one- or two-event emission
    design — picks the enriched one, fails loudly if absent."""
    candidates = [
        e
        for e in sock.events
        if e.get("event_type") == "prompt_assembled" and "cache_blocks" in e.get("fields", {})
    ]
    assert candidates, (
        "no prompt_assembled event carried `cache_blocks` — the cache "
        "attribution fields are not emitted on the live turn path. Events "
        f"seen: {[e.get('event_type') for e in sock.events]}"
    )
    return candidates[-1]


async def _run_turn(
    sock: _FakeSocket,
    bound_hub: WatcherHub,
    fake: FakeAnthropicSdkClient,
    context: TurnContext,
    action: str = "look around",
) -> None:
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]
    orch = Orchestrator(client=fake)
    await orch.run_narration_turn(action, context)
    # watcher_hub.publish_event dispatches to subscribers via
    # run_coroutine_threadsafe on the bound loop; yield briefly so the
    # _FakeSocket.send_json callbacks land before we read sock.events
    # (same settle pattern as tests/agents/test_prompt_zones_dashboard.py).
    await asyncio.sleep(0.05)


# --- AC-1: cache boundary on zones ----------------------------------------


@pytest.mark.asyncio
async def test_zones_carry_cache_boundary_flag(
    bound_hub: WatcherHub, simple_turn_context: TurnContext
) -> None:
    """Every zone row indicates whether it rides the cached ``system_blocks[0]``
    (Primacy/Early) or an uncached follow-on block (Valley/Late/Recency)."""
    sock = _FakeSocket()
    fake = FakeAnthropicSdkClient(responses=[_scripted()])
    await _run_turn(sock, bound_hub, fake, simple_turn_context)

    zones = _enriched_event(sock)["fields"]["zones"]
    assert zones, "zones must be non-empty for a real prompt build"

    sections_by_name: dict[str, dict[str, Any]] = {}
    seen_cached = False
    for z in zones:
        assert "cached" in z, f"zone {z.get('zone')!r} missing `cached` flag"
        assert isinstance(z["cached"], bool)
        if z["zone"] in CACHED_ZONES:
            assert z["cached"] is True, (
                f"{z['zone']} feeds the cached block region and MUST be marked cached"
            )
            seen_cached = True
        elif z["zone"] in UNCACHED_ZONES:
            assert z["cached"] is False, (
                f"{z['zone']} rides an uncached follow-on block and MUST be "
                f"marked uncached; got cached=True"
            )
        for s in z["sections"]:
            assert "cached" in s, f"section {s['name']!r} missing per-section `cached`"
            sections_by_name[s["name"]] = s
    assert seen_cached, "expected at least one cached zone (Primacy/Early)"

    # Per-section accuracy: the zone-level flag is a rollup, but a section only
    # rides system_blocks[0] when its bucket is System (in STABLE_SECTION_NAMES)
    # AND its zone is Primacy/Early. Both narrator_identity and the
    # narrator_constraints guardrail prose are System-bucket sections that sit in
    # Primacy — Story 61-10 promoted narrator_constraints into STABLE_SECTION_NAMES
    # (byte-static .md prose, no per-turn interpolation), so it now rides the same
    # cached system_blocks[0] as narrator_identity.
    assert sections_by_name["narrator_identity"]["cached"] is True, (
        "narrator_identity is System-bucket in Primacy — it rides the cached block"
    )
    assert sections_by_name["narrator_constraints"]["cached"] is True, (
        "narrator_constraints is System-bucket (STABLE_SECTION_NAMES, per Story 61-10) — "
        "it rides the cached system_blocks[0], not the per-turn user message"
    )


# --- AC-2: real usage joined + n/a loudly ---------------------------------


@pytest.mark.asyncio
async def test_cache_usage_carries_real_sdk_numbers_not_estimates(
    bound_hub: WatcherHub, simple_turn_context: TurnContext
) -> None:
    """The turn's event shows the ACTUAL ``cache_read``/``cache_write`` (with
    5m/1h split), ``cost_usd`` and ``cache_ttl`` from the SDK response — not a
    char/4 estimate. Scripted distinct values so an estimate cannot
    accidentally match."""
    sock = _FakeSocket()
    fake = FakeAnthropicSdkClient(
        responses=[
            _scripted(
                cache_read=11168,
                cache_write=12281,
                cache_write_5m=0,
                cache_write_1h=12281,
            )
        ]
    )
    await _run_turn(sock, bound_hub, fake, simple_turn_context)

    usage = _enriched_event(sock)["fields"]["cache_usage"]
    assert isinstance(usage, dict), (
        f"cache_usage must be a dict of real SDK numbers on an SDK turn; got {type(usage).__name__}"
    )
    assert usage["cache_read"] == 11168, f"got {usage['cache_read']!r}"
    assert usage["cache_write"] == 12281, f"got {usage['cache_write']!r}"
    assert usage["cache_write_5m"] == 0, f"got {usage['cache_write_5m']!r}"
    assert usage["cache_write_1h"] == 12281, f"got {usage['cache_write_1h']!r}"
    # Real spend, computed from the pricing table — strictly positive when
    # tokens were consumed; never a fabricated/estimated stand-in.
    assert isinstance(usage["cost_usd"], float) and usage["cost_usd"] > 0.0, (
        f"cost_usd must be a positive float from the SDK cost rollup; got {usage['cost_usd']!r}"
    )
    # cache_ttl reflects the client's configured TTL (FakeAnthropicSdkClient
    # has no configurable TTL → the orchestrator's getattr fallback yields
    # "n/a"); the point is it is a string, never silently dropped.
    assert isinstance(usage["cache_ttl"], str) and usage["cache_ttl"], (
        f"cache_ttl must be a non-empty string; got {usage['cache_ttl']!r}"
    )
    # Per-turn health band rides the same live event so the GM panel can color
    # $/turn. WIRING: must be emitted on the real run_narration_turn path, not
    # just computable in isolation.
    assert usage["cost_band"] in {"all_systems_go", "needs_work", "stop_everything"}, (
        f"cost_band must be a valid band string on the live turn; got {usage['cost_band']!r}"
    )


@pytest.mark.asyncio
async def test_cache_usage_is_explicit_na_when_sdk_usage_unavailable(
    bound_hub: WatcherHub, simple_turn_context: TurnContext
) -> None:
    """No Silent Fallbacks (CLAUDE.md): on a non-SDK build path there is no
    real API usage. ``cache_usage`` MUST be an explicit ``None`` ("n/a") — never
    a fabricated number styled as an actual. This is the rule-enforcement test
    for python-review check #1 / the No-Silent-Fallbacks principle.

    Uses the non-tooling ``_CannedClient`` and drives ``build_narrator_prompt``
    directly (the only path that has no SDK usage in hand)."""
    sock = _FakeSocket()
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]
    orch = Orchestrator(client=_CannedClient())
    await orch.build_narrator_prompt("look around", simple_turn_context)
    await asyncio.sleep(0.05)  # let the watcher fan-out settle (see _run_turn)

    events = [e for e in sock.events if e.get("event_type") == "prompt_assembled"]
    assert events, "no prompt_assembled event published on the build path"
    fields = events[-1]["fields"]
    # The field must be present (so the UI shows an explicit "n/a"), and it
    # must be None — not a number derived from prompt length.
    assert "cache_usage" in fields, (
        "cache_usage key must exist even with no SDK usage, so the panel can "
        "render an explicit 'n/a' rather than a blank that hides the gap"
    )
    assert fields["cache_usage"] is None, (
        f"cache_usage MUST be None ('n/a') when no real SDK usage exists — a "
        f"fabricated estimate here is exactly the lie this story kills; got "
        f"{fields['cache_usage']!r}"
    )


# --- AC-3: per-block content digest ---------------------------------------


@pytest.mark.asyncio
async def test_cache_blocks_carry_content_digest(
    bound_hub: WatcherHub, simple_turn_context: TurnContext
) -> None:
    """Each cacheable block emits a short content digest so the UI can detect
    drift turn-to-turn. The 'stable' block (and the tools array) are cached;
    valley/recency are not."""
    sock = _FakeSocket()
    fake = FakeAnthropicSdkClient(responses=[_scripted()])
    await _run_turn(sock, bound_hub, fake, simple_turn_context)

    blocks = _enriched_event(sock)["fields"]["cache_blocks"]
    assert isinstance(blocks, list) and blocks, "cache_blocks must be a non-empty list"

    by_label = {b["label"]: b for b in blocks}
    # stable + tools are always present and cache-marked on a real turn.
    assert "stable" in by_label, f"missing 'stable' block; labels={sorted(by_label)}"
    assert "tools" in by_label, f"missing 'tools' block; labels={sorted(by_label)}"
    assert by_label["stable"]["cached"] is True
    assert by_label["tools"]["cached"] is True

    for label, b in by_label.items():
        digest = b["digest"]
        assert isinstance(digest, str) and len(digest) == 8, (
            f"{label} digest must be an 8-char sha256 prefix; got {digest!r}"
        )
        assert all(c in "0123456789abcdef" for c in digest), (
            f"{label} digest must be lowercase hex; got {digest!r}"
        )
    # Uncached blocks, when present, must be marked uncached.
    for label in ("valley", "recency"):
        if label in by_label:
            assert by_label[label]["cached"] is False, (
                f"{label} block must be uncached; got cached=True"
            )


@pytest.mark.asyncio
async def test_stable_block_digest_stable_across_unchanging_turns(
    bound_hub: WatcherHub,
    simple_turn_context: TurnContext,
    simple_turn_context_turn_three: TurnContext,
) -> None:
    """Two turns of one fixed game whose stable prefix does not move must emit
    the SAME 'stable' digest — the property the UI's drift indicator relies on.
    (If this ever differs, that IS the bug — but with the *current* fixture the
    Early-zone state sections are unset, so the stable prefix holds.)"""
    sock = _FakeSocket()
    fake = FakeAnthropicSdkClient(responses=[_scripted(), _scripted()])
    await bound_hub.subscribe(sock)  # type: ignore[arg-type]
    orch = Orchestrator(client=fake)
    await orch.run_narration_turn("turn one action", simple_turn_context)
    await orch.run_narration_turn("turn two action", simple_turn_context_turn_three)
    await asyncio.sleep(0.05)  # let the watcher fan-out settle (see _run_turn)

    enriched = [
        e
        for e in sock.events
        if e.get("event_type") == "prompt_assembled" and "cache_blocks" in e.get("fields", {})
    ]
    assert len(enriched) == 2, f"expected 2 enriched events; got {len(enriched)}"
    digests = []
    for ev in enriched:
        by_label = {b["label"]: b for b in ev["fields"]["cache_blocks"]}
        digests.append(by_label["stable"]["digest"])
    assert digests[0] == digests[1], (
        "the stable block digest moved across two turns whose cached prefix "
        f"should be byte-identical: {digests}. The drift indicator would "
        "fire a false positive (or the prefix really churned)."
    )


# --- AC-4: mis-zoned state flag -------------------------------------------


@pytest.mark.asyncio
async def test_user_bucket_state_in_cached_zone_is_not_miszoned(
    bound_hub: WatcherHub, simple_turn_context: TurnContext
) -> None:
    """Story 60-4: ``mis_zoned`` ANDs with bucket. A ``state``-category
    section that lands in a cached *zone* (Early) but is User-bucket — i.e.,
    routes to the uncached user message, NOT ``system_blocks[0]`` — must
    return ``mis_zoned=False``. ``narrator_available_confrontations`` is the
    canonical example: it's State + Early + User-bucket, and 60-3 measured
    that it never touches the cached block.

    This test enforces the corrected semantics. The pre-60-4 zone-only
    bucket-blind shape returned ``True`` here, false-flagging the section as
    the cache-churn culprit (it isn't). See
    ``sprint/archive/60-3-session.md`` and ``sprint/archive/60-4-session.md``.
    """
    ctx = replace(
        simple_turn_context,
        available_confrontations=[("negotiation", "Parley with the Sheriff", "social")],
        in_combat=False,
        in_chase=False,
        in_encounter=False,
    )
    sock = _FakeSocket()
    fake = FakeAnthropicSdkClient(responses=[_scripted()])
    await _run_turn(sock, bound_hub, fake, ctx)

    zones = _enriched_event(sock)["fields"]["zones"]
    sections_by_name: dict[str, dict[str, Any]] = {}
    for z in zones:
        for s in z["sections"]:
            assert "mis_zoned" in s, (
                f"section {s['name']!r} missing `mis_zoned` flag — every "
                f"section must carry it so the panel can flag the bug signature"
            )
            sections_by_name[s["name"]] = {**s, "_zone": z["zone"]}

    assert "narrator_available_confrontations" in sections_by_name, (
        "expected the state-category confrontation menu to register in Early"
    )
    flagged = sections_by_name["narrator_available_confrontations"]
    assert flagged["category"] == "state"
    assert flagged["_zone"] in CACHED_ZONES
    # The section IS in a cached zone AND is state-category, but bucket=User
    # so it doesn't actually ride the cached block. Per the 60-4 AND-correction,
    # mis_zoned must be False here.
    assert flagged["cached"] is False, (
        "narrator_available_confrontations is User-bucket — the per-section "
        "cached flag (bucket-aware) must report False even though the zone "
        "itself is cached"
    )
    assert flagged["mis_zoned"] is False, (
        "Story 60-4: mis_zoned now ANDs zone-cached with bucket. A User-bucket "
        "state section routes to the uncached user message and CANNOT churn "
        "system_blocks[0] — so flagging it would be the false positive 60-3 "
        "disproved. The corrected positive case (System-bucket + State + cached "
        "zone) is covered by tests/agents/test_60_4_mis_zoned_bucket_correction.py."
    )

    # Negative case (unchanged across 60-4): a non-state section in the SAME
    # cached zone must NOT be flagged — mis_zoned is specifically about
    # volatile state, not all cached content.
    non_state_cached = [
        s
        for s in sections_by_name.values()
        if s["_zone"] in CACHED_ZONES and s["category"] != "state"
    ]
    assert non_state_cached, "expected at least one non-state section in a cached zone"
    for s in non_state_cached:
        assert s["mis_zoned"] is False, (
            f"non-state section {s['name']!r} (category={s['category']}) in a "
            f"cached zone must NOT be flagged mis_zoned — only volatile state is"
        )


@pytest.mark.asyncio
async def test_section_in_uncached_zone_is_never_miszoned(
    bound_hub: WatcherHub, simple_turn_context: TurnContext
) -> None:
    """``mis_zoned`` is about *cached* zones. Any section in Valley/Late/Recency
    — even a ``state``-category one — must never be flagged, because an
    uncached block is free to mutate per turn without cache cost."""
    sock = _FakeSocket()
    fake = FakeAnthropicSdkClient(responses=[_scripted()])
    await _run_turn(sock, bound_hub, fake, simple_turn_context)

    zones = _enriched_event(sock)["fields"]["zones"]
    for z in zones:
        if z["zone"] in UNCACHED_ZONES:
            for s in z["sections"]:
                assert s["mis_zoned"] is False, (
                    f"section {s['name']!r} in uncached zone {z['zone']} must "
                    f"never be mis_zoned (uncached blocks may mutate freely)"
                )


# --- AC-5: ACCURACY — emitted partition matches real system_blocks ---------


@pytest.mark.asyncio
async def test_emitted_partition_matches_real_system_blocks(
    bound_hub: WatcherHub, simple_turn_context: TurnContext
) -> None:
    """The keystone. The emitted 'stable' block digest MUST equal
    ``sha256(actual system_blocks[0].text)[:8]`` — the block the SDK client
    really received — and every cached-zone section's content must live inside
    that block while uncached-zone content must not. The display cannot claim
    'stable' while the real prompt drifted.
    """
    sock = _FakeSocket()
    fake = FakeAnthropicSdkClient(responses=[_scripted()])
    await _run_turn(sock, bound_hub, fake, simple_turn_context)

    assert len(fake.recorded_requests) == 1, (
        f"expected one recorded SDK request; got {len(fake.recorded_requests)}"
    )
    real_blocks = fake.recorded_requests[0].system_blocks
    real_stable_text = real_blocks[0].text
    expected_stable_digest = hashlib.sha256(real_stable_text.encode("utf-8")).hexdigest()[:8]

    fields = _enriched_event(sock)["fields"]
    by_label = {b["label"]: b for b in fields["cache_blocks"]}
    assert by_label["stable"]["digest"] == expected_stable_digest, (
        "emitted 'stable' digest does not match sha256 of the ACTUAL "
        "system_blocks[0] sent to the SDK — the panel would be reporting a "
        "digest computed from a different string than what was cached, which "
        "is precisely the decoupled-estimate bug this story kills. "
        f"emitted={by_label['stable']['digest']!r} "
        f"expected={expected_stable_digest!r}"
    )

    # Partition accuracy (per-section): a section marked cached MUST have its
    # content inside the REAL cached block; a section marked uncached MUST NOT.
    # The check is per-section (not per-zone) because a cached zone mixes
    # System-bucket content (→ system_blocks[0]) with User-bucket guardrails
    # (→ the per-turn user message). The user message is not a system block, so
    # we assert only the cached-prefix membership — that is the property the
    # "stable vs drifted" claim depends on.
    for z in fields["zones"]:
        for s in z["sections"]:
            content = s.get("content")
            if not content:
                continue
            if s["cached"]:
                assert content in real_stable_text, (
                    f"section {s['name']!r} is marked cached but its content "
                    f"is NOT inside the real cached block — the partition is a "
                    f"lie. (content head: {content[:40]!r})"
                )
            else:
                assert content not in real_stable_text, (
                    f"section {s['name']!r} is marked uncached but its content "
                    f"IS inside the real cached block — the partition is a lie."
                )


# --- AC-6: WIRING — emitted on the live run_narration_turn path ------------


@pytest.mark.asyncio
async def test_cache_attribution_wired_on_live_turn(
    bound_hub: WatcherHub, simple_turn_context: TurnContext
) -> None:
    """Wiring test (CLAUDE.md: every suite needs one; no source-text grep).
    Drive the production ``run_narration_turn`` end to end and assert the
    enriched ``prompt_assembled`` event actually fires with all four new field
    groups present — proving the attribution is reachable from the real path,
    not just constructible in isolation."""
    sock = _FakeSocket()
    fake = FakeAnthropicSdkClient(responses=[_scripted(cache_read=11168, cache_write=12281)])
    await _run_turn(sock, bound_hub, fake, simple_turn_context)

    fields = _enriched_event(sock)["fields"]
    # turn_number must survive (the UI dropdown joins usage by turn).
    assert fields.get("turn_number") == 0
    assert isinstance(fields["zones"], list) and fields["zones"]
    assert isinstance(fields["cache_blocks"], list) and fields["cache_blocks"]
    assert isinstance(fields["cache_usage"], dict)
    # Every zone carries the boundary flag and every section the mis_zoned flag
    # — the full contract is live, not partially wired.
    for z in fields["zones"]:
        assert "cached" in z
        for s in z["sections"]:
            assert "mis_zoned" in s
