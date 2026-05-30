"""RED tests for Story 72-2 leg 2 — reconcile ``npcs`` vs ``npc_pool`` on load.

Epic 72 DEEP-DIVE #1: the two NPC stores are never reconciled on load.
``migrate_legacy_snapshot`` (``sidequest/game/migrations.py``) runs S1–S4
sub-migrations but has **no pass that reconciles ``npcs`` against
``npc_pool``** for the same logical (case-folded) name. A save can carry an
``Npc`` ``Mara`` (disposition +18, friendly) *and* a separate
``NpcPoolMember(name="Mara")`` whose state has drifted, with nothing enforcing
single-source-of-truth. On load the divergent state survives and the next
case-folding call site gets whichever record it hits first.

This story adds a **reconcile pass** as a new sub-function in the
``migrate_legacy_snapshot`` tuple (mirroring ``_migrate_s2_npc_registry_split``:
operates on the deep-copied raw ``out`` dict *before* pydantic re-hydration,
returns either ``None`` (no-op) or a dict of OTEL attributes folded into the
shared ``snapshot.canonicalize`` span). Disposition in raw-dict space is a bare
int. ``Npc`` is **authoritative** for disposition in a conflict (it is the
record ADR-020 deltas land on); the conflict is **counted in OTEL**, never
resolved by a silent first-wins pick (SOUL: No Silent Fallbacks).

**Proposed OTEL contract** (mirrors the ``s2_*`` count style; Dev may rename
per the TEA deviation log in ``.session/72-2-session.md`` — the assertion then
moves to the chosen names / a dedicated reconcile span):
- ``s5_pool_shadowed_removed`` — pool members removed because a same-name
  ``Npc`` exists (single source of truth).
- ``s5_disposition_conflicts`` — same-name pairs whose pool-side disposition
  diverged from the authoritative ``Npc`` value (resolved to the ``Npc``,
  divergence recorded — not silently dropped).

Wiring proof (CLAUDE.md "No Source-Text Wiring Tests"): every test drives the
**public** ``migrate_legacy_snapshot`` entrypoint and asserts behaviour + spans,
so it fails if the reconcile sub-function is never wired into the live tuple
(an unwired helper is dead code — No Stubbing).
"""

from __future__ import annotations

from typing import Any

from sidequest.game.migrations import migrate_legacy_snapshot

_SPAN = "snapshot.canonicalize"


def _npc(name: str, disposition: int, *, pool_origin: str | None = None) -> dict[str, Any]:
    """Minimal Npc-shaped raw dict carrying the two fields reconcile reads:
    ``core.name`` and top-level integer ``disposition``."""
    return {
        "core": {
            "name": name,
            "description": "x",
            "personality": "x",
            "level": 1,
            "xp": 0,
            "inventory": {"items": [], "max_slots": 10},
            "statuses": [],
            "edge": {
                "current": 10,
                "max": 10,
                "base_max": 10,
                "recovery_triggers": [{"kind": "OnResolution"}],
                "thresholds": [],
            },
            "acquired_advancements": [],
        },
        "disposition": disposition,
        "pool_origin": pool_origin,
    }


def _member(name: str, **extra: Any) -> dict[str, Any]:
    """Minimal NpcPoolMember-shaped raw dict. ``extra`` lets a test stamp a
    ``disposition`` to model the divergent-scaffold case."""
    base: dict[str, Any] = {
        "name": name,
        "role": None,
        "pronouns": None,
        "appearance": None,
        "archetype_id": None,
        "drawn_from": "world_authored",
    }
    base.update(extra)
    return base


def _canonicalize_spans(otel_capture) -> list[Any]:
    return [s for s in otel_capture.get_finished_spans() if s.name == _SPAN]


# ---------------------------------------------------------------------------
# AC2 — same case-folded name in both stores reconciles to single source of
# truth: the shadowing pool member is removed, the Npc (authoritative) stays.
# ---------------------------------------------------------------------------


def test_shadowed_pool_member_removed_on_load() -> None:
    """A name present in both ``npcs`` and ``npc_pool`` leaves the pool member
    removed (shadowed by the mechanical ``Npc``) so a later case-folding lookup
    cannot resolve to a divergent scaffold. RED: no reconcile pass exists, so
    the duplicate ``Mara`` survives in ``npc_pool``."""
    snapshot = {
        "genre_slug": "caverns_and_claudes",
        "world_slug": "beneath_sunden",
        "npcs": [_npc("Mara", 18, pool_origin="Mara")],
        "npc_pool": [_member("mara")],  # case-folded duplicate
    }

    out = migrate_legacy_snapshot(snapshot)

    pool_names = {m["name"].casefold() for m in out["npc_pool"]}
    assert "mara" not in pool_names, (
        "shadowing pool member was not removed; npcs and npc_pool both still "
        "hold 'Mara' — no single source of truth"
    )
    # The authoritative Npc is untouched.
    assert len(out["npcs"]) == 1
    assert out["npcs"][0]["disposition"] == 18


def test_reconcile_counts_shadowed_removal_in_otel(otel_capture) -> None:
    """The reconcile pass must fold a shadowed-removal count into the
    ``snapshot.canonicalize`` span so the GM panel can see the pass fired and
    how many duplicates it collapsed. RED: no span / no attribute today."""
    snapshot = {
        "genre_slug": "caverns_and_claudes",
        "world_slug": "beneath_sunden",
        "npcs": [_npc("Mara", 18, pool_origin="Mara")],
        "npc_pool": [_member("Mara")],
    }

    migrate_legacy_snapshot(snapshot)

    spans = _canonicalize_spans(otel_capture)
    assert len(spans) == 1, (
        "reconcile must emit (fold into) the snapshot.canonicalize span when it "
        f"collapses a duplicate; got {len(spans)} spans"
    )
    attrs = dict(spans[0].attributes or {})
    assert attrs.get("s5_pool_shadowed_removed") == 1, (
        "reconcile must count shadowed pool-member removals via "
        f"s5_pool_shadowed_removed; got {attrs!r}"
    )


# ---------------------------------------------------------------------------
# AC4 — divergent disposition resolves to the Npc (authoritative) and the
# divergence is COUNTED (No Silent Fallbacks), never silently first-wins.
# ---------------------------------------------------------------------------


def test_divergent_disposition_resolves_to_npc_and_is_counted(otel_capture) -> None:
    """When a pool member carries a disposition that diverges from the
    same-name ``Npc``, reconcile keeps the ``Npc`` value (authoritative) AND
    records the conflict in OTEL. RED: no reconcile pass, so the divergence
    survives unreconciled and uncounted."""
    snapshot = {
        "genre_slug": "caverns_and_claudes",
        "world_slug": "beneath_sunden",
        # Npc says friendly (+18); scaffold drifted to +5. Npc wins.
        "npcs": [_npc("Mara", 18, pool_origin="Mara")],
        "npc_pool": [_member("Mara", disposition=5)],
    }

    out = migrate_legacy_snapshot(snapshot)

    # Authoritative value preserved on the Npc.
    assert out["npcs"][0]["disposition"] == 18
    # No surviving record carries the divergent +5.
    pool_names = {m["name"].casefold() for m in out["npc_pool"]}
    assert "mara" not in pool_names

    attrs = dict(_canonicalize_spans(otel_capture)[0].attributes or {})
    assert attrs.get("s5_disposition_conflicts") == 1, (
        "a divergent pool/Npc disposition must be counted as a resolved "
        f"conflict (No Silent Fallbacks); got {attrs!r}"
    )


# ---------------------------------------------------------------------------
# AC3 — pool-only name: no matching Npc → left intact, no spurious Npc, no
# silent drop, and (nothing to reconcile) a clean no-op (no span).
# ---------------------------------------------------------------------------


def test_pool_only_member_is_left_intact(otel_capture) -> None:
    """A name in ``npc_pool`` with no matching ``Npc`` is a valid re-citable
    scaffold with no mechanical state to reconcile — it must survive untouched,
    no ``Npc`` is fabricated, and the pass is a clean no-op (no canonicalize
    span fired for it)."""
    snapshot = {
        "genre_slug": "caverns_and_claudes",
        "world_slug": "beneath_sunden",
        "npcs": [],
        "npc_pool": [_member("Solo")],
    }

    out = migrate_legacy_snapshot(snapshot)

    pool_names = {m["name"] for m in out["npc_pool"]}
    assert "Solo" in pool_names, "pool-only member was silently dropped"
    assert len(out["npcs"]) == 0, "reconcile fabricated a spurious Npc"
    assert _canonicalize_spans(otel_capture) == [], (
        "nothing to reconcile — reconcile must be a no-op (no canonicalize "
        "span) for a pool-only member"
    )


# ---------------------------------------------------------------------------
# AC4 (first clause) — npcs-only name: authoritative, untouched, no spurious
# pool member, no conflict counted.
# ---------------------------------------------------------------------------


def test_npcs_only_name_unaffected(otel_capture) -> None:
    """A name present only in ``npcs`` (no scaffold) is already
    single-source-of-truth: its disposition is preserved, no pool member is
    fabricated, and no conflict is recorded."""
    snapshot = {
        "genre_slug": "caverns_and_claudes",
        "world_slug": "beneath_sunden",
        "npcs": [_npc("Solo", 12)],
        "npc_pool": [],
    }

    out = migrate_legacy_snapshot(snapshot)

    assert out["npcs"][0]["disposition"] == 12
    assert out["npc_pool"] == [], "reconcile fabricated a spurious pool member"
    assert _canonicalize_spans(otel_capture) == [], (
        "npcs-only name needs no reconcile — must be a clean no-op"
    )


# ---------------------------------------------------------------------------
# Input-not-mutated invariant (mirrors test_migrations.py): reconcile operates
# on the deep-copied `out`, never the caller's dict.
# ---------------------------------------------------------------------------


def test_reconcile_does_not_mutate_input() -> None:
    """``migrate_legacy_snapshot`` is documented pure-ish (deep-copies its
    input). The reconcile sub must honour that — the caller's snapshot dict is
    unchanged even when a duplicate is collapsed in the output."""
    import copy

    snapshot = {
        "genre_slug": "caverns_and_claudes",
        "world_slug": "beneath_sunden",
        "npcs": [_npc("Mara", 18, pool_origin="Mara")],
        "npc_pool": [_member("Mara")],
    }
    before = copy.deepcopy(snapshot)

    migrate_legacy_snapshot(snapshot)

    assert snapshot == before, "reconcile mutated the caller's input snapshot"
