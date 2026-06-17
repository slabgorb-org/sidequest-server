"""Story 123-1 — the baseline-failure manifest is a committed, structured deliverable.

Story 123-1 triages the ~171 pre-existing full-suite failures and commits a
*categorized baseline-failure manifest* so future stories get honest CI signal:
a subagent can no longer mislabel a real regression as "pre-existing" by hand,
because the baseline is now an enumerated, dispositioned artifact in the repo.

**Why a test, not just a doc.** AC-1 requires the manifest to carry, per bucket,
a category, the affected test name(s), a root-cause summary, and a disposition.
A freeform prose doc cannot be checked for completeness and silently rots. This
test pins the manifest to a machine-checkable JSON shape so that (a) the
deliberate out-of-scope bucket (WWN beat-pool → epic-108) is *documented as
deferred*, never silently dropped, and (b) the dominant SDK-hermeticity bucket
and the in-scope snapshot-governance fix are both characterized.

**Format decision (TEA test-design deviation).** The story allows "markdown or
JSON". This test requires the JSON to be the source of truth because the ACs are
structural (per-bucket fields + a constrained disposition vocabulary) and only a
parsed object can be checked for those without brittle text-grepping. Dev MAY
additionally commit a human-readable rendering; this test governs the JSON.

The manifest does not exist yet — this whole module is RED until Dev (GREEN)
produces ``docs/test-baseline-manifest.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# tests/server/test_123_1_baseline_manifest.py -> parents[2] == server repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_PATH = _REPO_ROOT / "docs" / "test-baseline-manifest.json"

# AC-1: disposition is a *constrained* vocabulary so every bucket lands an
# explicit decision rather than vague prose. Allowed values:
#   fixed        — the failures in this bucket are repaired by this story
#   quarantined  — marked xfail/skip with a documented reason, kept visible
#   deferred     — deliberately out of scope, owned by another epic/story
#   investigated — root-caused as env/stale-tree (no code change needed),
#                  with the reproduction recorded
_ALLOWED_DISPOSITIONS = {"fixed", "quarantined", "deferred", "investigated"}

# Required per-bucket keys, mirroring AC-1's enumeration.
_REQUIRED_BUCKET_KEYS = {"category", "tests", "root_cause", "disposition"}


def _load_manifest() -> dict:
    assert _MANIFEST_PATH.exists(), (
        f"Story 123-1 deliverable missing: expected the categorized "
        f"baseline-failure manifest at {_MANIFEST_PATH.relative_to(_REPO_ROOT)} "
        "(AC-1). Dev must commit a JSON object with a non-empty `buckets` list, "
        "each bucket carrying category / tests / root_cause / disposition."
    )
    raw = _MANIFEST_PATH.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - failure path
        pytest.fail(f"Baseline manifest is not valid JSON: {exc}")
    assert isinstance(data, dict), (
        f"Baseline manifest top-level must be a JSON object, got {type(data).__name__}."
    )
    return data


def _buckets() -> list[dict]:
    data = _load_manifest()
    buckets = data.get("buckets")
    assert isinstance(buckets, list) and buckets, (
        "Baseline manifest must have a non-empty `buckets` list — the triage "
        "produced zero categories, which means the ~171-failure baseline was "
        "not actually characterized."
    )
    for i, bucket in enumerate(buckets):
        assert isinstance(bucket, dict), f"buckets[{i}] must be an object."
    return buckets


def _bucket_blob(bucket: dict) -> str:
    """Lower-cased haystack of a bucket's category + root_cause for keyword match.

    Keyword matching (not exact category strings) keeps the test from coupling
    to Dev's exact naming while still enforcing that the substantive bucket is
    present and dispositioned.
    """
    return f"{bucket.get('category', '')} {bucket.get('root_cause', '')}".lower()


def test_manifest_exists_and_parses() -> None:
    """AC-1: the manifest is a committed, parseable JSON object."""
    data = _load_manifest()
    assert "buckets" in data, "Manifest object must carry a top-level `buckets` key."


def test_every_bucket_has_required_fields() -> None:
    """AC-1: each bucket records category, affected tests, root cause, disposition."""
    for i, bucket in enumerate(_buckets()):
        missing = _REQUIRED_BUCKET_KEYS - set(bucket.keys())
        assert not missing, (
            f"buckets[{i}] ({bucket.get('category', '?')!r}) is missing required "
            f"field(s): {sorted(missing)}. AC-1 requires every bucket to carry "
            f"{sorted(_REQUIRED_BUCKET_KEYS)}."
        )
        # tests must be a non-empty list of strings — a bucket with no named
        # tests is not a characterization, it is a hand-wave.
        tests = bucket["tests"]
        assert isinstance(tests, list) and tests, (
            f"buckets[{i}] ({bucket['category']!r}) must list at least one "
            "affected test id/pattern."
        )
        assert all(isinstance(t, str) and t.strip() for t in tests), (
            f"buckets[{i}] ({bucket['category']!r}) `tests` must be non-empty strings."
        )
        # root_cause must be a real summary, not an empty placeholder.
        root_cause = bucket["root_cause"]
        assert isinstance(root_cause, str) and root_cause.strip(), (
            f"buckets[{i}] ({bucket['category']!r}) must carry a non-empty root_cause summary."
        )


def test_every_disposition_is_in_the_allowed_vocabulary() -> None:
    """AC-1/AC-3/AC-4: each bucket lands an explicit, constrained disposition."""
    for i, bucket in enumerate(_buckets()):
        disposition = bucket.get("disposition")
        assert disposition in _ALLOWED_DISPOSITIONS, (
            f"buckets[{i}] ({bucket.get('category', '?')!r}) has disposition "
            f"{disposition!r}; must be one of {sorted(_ALLOWED_DISPOSITIONS)}."
        )


def test_snapshot_governance_bucket_is_present_and_fixed() -> None:
    """Step 2 is the in-scope fix: the snapshot-field governance miss.

    The pre-existing ``test_every_snapshot_field_is_categorized`` failure
    (uncategorized ``pending_quest_offers``) is the dominant *fixable* bucket;
    the manifest must record it and mark it ``fixed``.
    """
    matches = [
        b
        for b in _buckets()
        if ("snapshot" in _bucket_blob(b) and "governance" in _bucket_blob(b))
        or "pending_quest_offers" in _bucket_blob(b)
    ]
    assert matches, (
        "Manifest has no bucket for the snapshot-field governance miss "
        "(pending_quest_offers / test_every_snapshot_field_is_categorized). "
        "This is the in-scope Step 2 fix and must be characterized."
    )
    assert any(b.get("disposition") == "fixed" for b in matches), (
        "The snapshot-governance bucket must be disposition=fixed — Step 2 is "
        "an in-scope one-liner, not a deferral."
    )


def test_wwn_beatpool_bucket_is_present_and_deferred() -> None:
    """AC-3: the deliberate out-of-scope WWN bucket is documented, not dropped.

    The WWN class beat-pool loader PackErrors belong to epic-108 de-nativization
    (resolves when 108-3 lands). They must appear in the manifest with a
    deferred/quarantined disposition that references the owning epic, so a
    future reader knows the failure is *known and owned*, not new breakage.
    """
    matches = [
        b
        for b in _buckets()
        if "wwn" in _bucket_blob(b) or "beat" in _bucket_blob(b) or "108" in _bucket_blob(b)
    ]
    assert matches, (
        "Manifest has no bucket for the deliberate out-of-scope WWN class "
        "beat-pool failures (epic-108 / 108-3). The deferral must be documented "
        "so it is never mistaken for a fresh regression."
    )
    assert any(b.get("disposition") in {"deferred", "quarantined"} for b in matches), (
        "The WWN beat-pool bucket must be disposition=deferred (or quarantined) "
        "— it is owned by epic-108, not fixed here."
    )
    # The deferral must name its owning epic/story so it is traceable.
    assert any("108" in _bucket_blob(b) for b in matches), (
        "The WWN beat-pool bucket must reference its owning epic (108 / 108-3) "
        "so the deferral is traceable."
    )


def test_sdk_hermeticity_bucket_is_present() -> None:
    """Step 1: the dominant bucket (~174 LlmClientError) must be characterized.

    Whatever the root-cause verdict (env/stale-tree artifact vs real develop
    regression), the manifest must record the SDK-hermeticity bucket — it is the
    largest single category and the reason the baseline looked alarming.
    """
    matches = [
        b
        for b in _buckets()
        if "anthropic" in _bucket_blob(b)
        or "hermetic" in _bucket_blob(b)
        or "llmclienterror" in _bucket_blob(b)
        or "build_async_anthropic" in _bucket_blob(b)
        or "sdk" in _bucket_blob(b)
    ]
    assert matches, (
        "Manifest has no bucket for the dominant SDK-hermeticity failures "
        "(~174 LlmClientError from build_async_anthropic). Step 1's root-cause "
        "verdict must be recorded as a bucket with a disposition."
    )
