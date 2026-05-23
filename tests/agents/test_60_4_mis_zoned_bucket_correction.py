"""Story 60-4 — mis_zoned flag must AND zone-cached with bucket.

60-3 disproved the original "mis-zoned state sections churn block 0"
hypothesis by measuring: the three suspect sections
(``narrator_available_confrontations``, ``trope_beat_directives``,
``npc_roster``) are User-bucket (absent from ``STABLE_SECTION_NAMES``), so
``compose_split_by_zone`` routes them into the uncached user message — they
never touch ``system_blocks[0]``. The ``mis_zoned`` flag was zone-only
(``zone_cached AND category == "state"``) and produced false positives that
misled the entire epic framing.

60-4 corrects the flag so it only fires on sections that ACTUALLY ride the
cached block — i.e., ``zone_cached AND bucket == System`` AND
``category == "state"``. (The bucket gate is the new condition; zone and
category gates stay.)

These tests pin the corrected semantics. They drive ``_compute_zones_payload``
directly with synthetic ``PromptSection`` inputs — no orchestrator
integration — so the contract is independent of which sections the live
narrator pipeline happens to register.

See: ``sprint/archive/60-3-session.md`` → "Dev Diagnosis (60-3 — FINAL)".
"""

from __future__ import annotations

from sidequest.agents.orchestrator import _compute_zones_payload
from sidequest.agents.prompt_framework.bucket import STABLE_SECTION_NAMES
from sidequest.agents.prompt_framework.types import (
    AttentionZone,
    PromptSection,
    SectionCategory,
)


def _section(
    *,
    name: str,
    category: SectionCategory,
    zone: AttentionZone,
    content: str = "x",
) -> PromptSection:
    return PromptSection(name=name, category=category, zone=zone, content=content)


def _find_section_payload(
    payload: list[dict], section_name: str
) -> dict:
    for zone in payload:
        for s in zone["sections"]:
            if s["name"] == section_name:
                return s
    raise AssertionError(
        f"section {section_name!r} not found in payload {payload!r}"
    )


# --- AC-4: User-bucket state section in cached zone is NOT mis_zoned --------


def test_user_bucket_state_section_in_cached_zone_is_not_miszoned() -> None:
    """The three live "suspect" sections are State + Early but User-bucket.
    60-3 measured them as never touching the cached prefix. mis_zoned MUST
    return False for them — flagging them is the false positive that misled
    the original epic.
    """
    # All three suspect names from the 60-3 evidence chain. None are in
    # STABLE_SECTION_NAMES; all resolve to SectionBucket.User by default.
    for name in (
        "narrator_available_confrontations",
        "trope_beat_directives",
        "npc_roster",
    ):
        assert name not in STABLE_SECTION_NAMES, (
            f"precondition broke — {name!r} migrated into STABLE_SECTION_NAMES; "
            f"that changes the bucket calculus and this test needs review"
        )

        payload = _compute_zones_payload(
            [
                _section(
                    name=name,
                    category=SectionCategory.State,
                    zone=AttentionZone.Early,
                )
            ]
        )
        section_row = _find_section_payload(payload, name)

        # Sanity: the zone is cached and the section IS state-category.
        early_zone = next(z for z in payload if z["zone"] == "Early")
        assert early_zone["cached"] is True, (
            "Early zone must report cached=True (zone-level attribution)"
        )
        assert section_row["category"] == "state"

        # The new contract: cached=False (bucket-aware) so mis_zoned MUST be
        # False (the AND of zone-cached + bucket-stable + state).
        assert section_row["cached"] is False, (
            f"{name!r} resolves to SectionBucket.User and must report "
            f"cached=False in the zones payload — this is the existing "
            f"bucket-aware per-section ``cached`` field (60-2). If THIS "
            f"flips, the entire AND-correction premise breaks."
        )
        assert section_row["mis_zoned"] is False, (
            f"{name!r} is User-bucket and CANNOT ride system_blocks[0]; "
            f"flagging it mis_zoned=True is the false positive 60-3 disproved. "
            f"Got mis_zoned={section_row['mis_zoned']!r} — the flag is still "
            f"zone-only / bucket-blind. Fix: AND with the per-section bucket "
            f"check (use the existing _section_rides_cache helper)."
        )


def test_state_section_in_uncached_zone_is_not_miszoned() -> None:
    """State + Valley + any bucket: mis_zoned MUST be False. An uncached
    block may mutate freely without cache cost, so flagging is meaningless.
    """
    payload = _compute_zones_payload(
        [
            _section(
                name="npc_roster",
                category=SectionCategory.State,
                zone=AttentionZone.Valley,
            )
        ]
    )
    row = _find_section_payload(payload, "npc_roster")
    assert row["mis_zoned"] is False, (
        f"state section in Valley (uncached) must never be mis_zoned; "
        f"got {row['mis_zoned']!r}"
    )


# --- AC-4: System-bucket state section in cached zone IS mis_zoned ---------


def test_system_bucket_state_section_in_cached_zone_is_miszoned() -> None:
    """The corrected positive case: a hypothetical State-category section
    that IS System-bucket (i.e., its name is in STABLE_SECTION_NAMES) AND
    sits in a cached zone — that one truly would churn block 0 every turn
    and is the genuine bug signature mis_zoned exists to surface.

    Uses ``narrator_identity`` (in STABLE_SECTION_NAMES) but force-tagged as
    State category to construct the corrected positive case without
    depending on a section that doesn't currently exist.
    """
    assert "narrator_identity" in STABLE_SECTION_NAMES, (
        "precondition broke — narrator_identity left STABLE_SECTION_NAMES; "
        "pick another System-bucket name for this test"
    )

    payload = _compute_zones_payload(
        [
            _section(
                name="narrator_identity",
                category=SectionCategory.State,
                zone=AttentionZone.Early,
            )
        ]
    )
    row = _find_section_payload(payload, "narrator_identity")

    assert row["cached"] is True, (
        "narrator_identity is System-bucket + Early zone — it really does "
        "ride system_blocks[0] (existing _section_rides_cache returns True)"
    )
    assert row["mis_zoned"] is True, (
        "this is the genuine bug signature: a state-category section that "
        "ACTUALLY rides the cached prefix would invalidate block 0 every "
        "turn (state mutates per-turn). mis_zoned MUST fire here — if the "
        "AND-correction over-corrected and disabled the flag entirely, this "
        "test catches that regression."
    )


# --- AC-4: Non-state section in cached zone is not mis_zoned ---------------


def test_non_state_system_bucket_section_in_cached_zone_is_not_miszoned() -> None:
    """``mis_zoned`` is specifically about VOLATILE STATE sitting in cached
    storage. A stable identity section in the same cached zone is fine —
    that's exactly what the cached block is for. The category gate must
    survive the AND-correction.
    """
    payload = _compute_zones_payload(
        [
            _section(
                name="narrator_identity",
                category=SectionCategory.Identity,
                zone=AttentionZone.Early,
            )
        ]
    )
    row = _find_section_payload(payload, "narrator_identity")

    assert row["cached"] is True
    assert row["mis_zoned"] is False, (
        "an Identity-category section in the cached block is correct — "
        "identity is byte-stable across turns and is the prefix's reason "
        "to exist. mis_zoned must NOT fire on stable categories; got "
        f"{row['mis_zoned']!r}"
    )


# --- AC-4: All three suspect sections from 60-3 together --------------------


def test_three_60_3_suspect_sections_all_clear_mis_zoned() -> None:
    """The integration shape: when all three User-bucket state sections
    from 60-3's evidence chain are present together in Early, NONE of them
    are mis_zoned after the fix. This is the failure mode the original
    epic framing was built on; it must produce zero false positives.
    """
    sections = [
        _section(
            name="narrator_available_confrontations",
            category=SectionCategory.State,
            zone=AttentionZone.Early,
        ),
        _section(
            name="trope_beat_directives",
            category=SectionCategory.State,
            zone=AttentionZone.Early,
        ),
        _section(
            name="npc_roster",
            category=SectionCategory.State,
            zone=AttentionZone.Early,
        ),
    ]
    payload = _compute_zones_payload(sections)

    flagged = []
    for zone in payload:
        for s in zone["sections"]:
            if s["mis_zoned"]:
                flagged.append(s["name"])

    assert flagged == [], (
        f"after the AND-correction, ZERO of the 60-3 suspect sections "
        f"should be mis_zoned (all three are User-bucket and never touch "
        f"system_blocks[0]); got flagged={flagged!r}. Each one of these "
        f"would be a false-positive cache-churn warning on the GM panel."
    )


# --- AC-4 regression: the bucket-blind shape must NOT survive ---------------


def test_mis_zoned_is_not_pure_zone_and_category_only() -> None:
    """Direct anti-shape regression: if mis_zoned == (zone_cached AND
    category=="state") with no bucket gate, this test fires. It locks the
    AND-correction in place.

    A User-bucket state section in Early would return True under the OLD
    shape and False under the corrected shape. We assert False.
    """
    payload = _compute_zones_payload(
        [
            _section(
                # A name guaranteed to be User-bucket (not in STABLE_SECTION_NAMES).
                name="totally_made_up_user_bucket_state_section_xyz",
                category=SectionCategory.State,
                zone=AttentionZone.Early,
            )
        ]
    )
    row = _find_section_payload(
        payload, "totally_made_up_user_bucket_state_section_xyz"
    )
    assert row["mis_zoned"] is False, (
        "the OLD shape (zone_cached AND category=='state') returns True "
        "here; the corrected shape returns False because bucket is User. "
        "If this assertion fails, the AND-with-bucket gate was not added."
    )
