"""Per-expansion quest lifecycle (ADR-137 × ADR-106): select signature beat,
seed a ledger thread, project into quest_log, resolve on the beat.
Deterministic — no LLM (Amendment C)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from sidequest.dungeon.persistence import ComplicationThread
from sidequest.dungeon.region_graph.model import Expansion, RegionNode
from sidequest.dungeon.themes import ExpansionQuestTemplate
from sidequest.game.cookbook.models import RegionContentManifest
from sidequest.game.session import GameSnapshot, QuestEntry
from sidequest.genre.names.generator import sanitize_display_name
from sidequest.telemetry.spans.dungeon_quest import (
    quest_bound_span,
    quest_minted_span,
    quest_resolved_span,
)


class ThreadLedger(Protocol):
    """Minimal ledger surface the expansion-quest functions need — satisfied by
    both DungeonStore (sqlite, in-tests) and PgDungeonRepository (production)."""

    def open_thread(self, thread: ComplicationThread) -> None: ...
    def open_threads(self) -> list[ComplicationThread]: ...
    def resolve_thread(self, thread_id: str) -> None: ...


@dataclass(frozen=True)
class SignatureBinding:
    kind: str  # "big_bad" | "set_piece" | "reach_deep" (effective, post-degrade)
    ref_id: str  # bound element id: region id, or big_bad name, or set_piece id
    anchor_region: str  # the region id the quest anchors to
    title: str
    objective: str
    degraded: bool
    theme: str  # the deepest region's theme id the quest bound (AC-5: GM-panel verifiable)


def _deepest(expansion: Expansion) -> RegionNode:
    """Return the node with the highest depth_score; None scores as -inf so
    attached (scored) nodes always win over unattached ones."""
    return max(
        expansion.new_nodes,
        key=lambda n: n.depth_score if n.depth_score is not None else float("-inf"),
    )


def _fill(text: str, *, theme: str, big_bad: str, anchor: str, depth: str = "") -> str:
    return (
        text.replace("{theme}", theme)
        .replace("{big_bad}", big_bad)
        .replace("{anchor}", anchor)
        .replace("{depth}", depth)
    )


def _depth_token(node: RegionNode) -> str:
    """AC-4 distinguisher: a deterministic, per-expansion marker so two
    same-theme quests do not read as byte-identical duplicates in the Quests
    tab.  The anchor region's depth_score is the natural "how deep does this
    one go" marker and differs per descent; fall back to the region id when a
    node carries no score so the token is never blank (No Silent Fallbacks)."""
    if node.depth_score is not None:
        return f"Depth {int(node.depth_score)}"
    return node.id


def _distinguished_title(
    template_title: str, *, theme: str, big_bad: str, anchor_node: RegionNode
) -> str:
    """Fill the quest title and guarantee same-theme expansions diverge (AC-4).

    Supports both distinguisher paths TEA flagged: a YAML title carrying an
    explicit ``{depth}`` slot fills it; a slotless title (every shipped
    beneath_sunden title today) gets the token appended at seed time.  The
    token is a pure function of the anchor region, so determinism holds —
    two seeds of the same expansion still produce identical titles."""
    token = _depth_token(anchor_node)
    title = _fill(template_title, theme=theme, big_bad=big_bad, anchor=anchor_node.id, depth=token)
    if "{depth}" not in template_title and token:
        title = f"{title} — {token}"
    return title


def select_signature(
    *,
    expansion: Expansion,
    manifests_by_region: dict[str, RegionContentManifest],
    template: ExpansionQuestTemplate,
) -> SignatureBinding:
    """Pick the expansion's signature beat from a theme quest_template and the
    per-region content manifests.  Pure logic — no I/O, no randomness."""
    deepest = _deepest(expansion)
    theme = deepest.theme

    if template.signature == "big_bad":
        # Deepest region (by depth_score) that rolled a big_bad.
        candidates = sorted(
            (
                n
                for n in expansion.new_nodes
                if (manifests_by_region.get(n.id) or _empty_manifest()).big_bad
            ),
            key=lambda n: n.depth_score if n.depth_score is not None else float("-inf"),
            reverse=True,
        )
        if candidates:
            node = candidates[0]
            bb = manifests_by_region[node.id].big_bad or {}
            # Sanitize identically to the Monster-Manual inject boundary
            # (``monster_manual_inject._sanitize_patch_names`` →
            # ``sanitize_display_name``): the minted encounter actor enters
            # ``snapshot.npcs`` cleaned, so the seeded quest ref_id MUST match
            # that cleaned name or ``ref in defeated_npc_names`` never fires.
            name = sanitize_display_name(str(bb.get("name", ""))) or "the master of this place"
            return SignatureBinding(
                kind="big_bad",
                ref_id=name,
                anchor_region=node.id,
                title=_distinguished_title(
                    template.title, theme=theme, big_bad=name, anchor_node=node
                ),
                objective=_fill(template.objective, theme=theme, big_bad=name, anchor=node.id),
                degraded=False,
                theme=theme,
            )
        # No big_bad rolled — loud degrade to reach_deep (caller emits the span).
        return _bind_reach_deep(template, deepest, theme, degraded=True)

    if template.signature == "set_piece":
        sp = (template.set_piece_id or "").strip()
        return SignatureBinding(
            kind="set_piece",
            ref_id=sp,
            anchor_region=deepest.id,
            title=_distinguished_title(
                template.title, theme=theme, big_bad="", anchor_node=deepest
            ),
            objective=_fill(template.objective, theme=theme, big_bad="", anchor=deepest.id),
            degraded=False,
            theme=theme,
        )

    return _bind_reach_deep(template, deepest, theme, degraded=False)


def _bind_reach_deep(
    template: ExpansionQuestTemplate,
    deepest: RegionNode,
    theme: str,
    *,
    degraded: bool,
) -> SignatureBinding:
    return SignatureBinding(
        kind="reach_deep",
        ref_id=deepest.id,
        anchor_region=deepest.id,
        title=_distinguished_title(template.title, theme=theme, big_bad="", anchor_node=deepest),
        objective=_fill(template.objective, theme=theme, big_bad="", anchor=deepest.id),
        degraded=degraded,
        theme=theme,
    )


def _empty_manifest() -> RegionContentManifest:
    """Null object for regions not yet in the manifest dict."""
    return RegionContentManifest(
        race="",
        cr_band="",
        size_budget={},
        wandering_table=[],
        loot_table=[],
        special_rooms=[],
        big_bad=None,
    )


def _expansion_quest_thread_id(campaign_seed: int, expansion_id: int) -> str:
    h = hashlib.blake2b(f"{campaign_seed}:{expansion_id}:expansion_quest".encode(), digest_size=8)
    return f"q.exp{expansion_id}.{h.hexdigest()}"


def seed_expansion_quest(
    *,
    campaign_seed: int,
    expansion: Expansion,
    manifests_by_region: dict[str, RegionContentManifest],
    template: ExpansionQuestTemplate,
    store: ThreadLedger,
    started_at_depth_score: float,
) -> str:
    """Open one expansion-scoped ComplicationThread in the ledger.

    Calls ``select_signature`` to pick the quest's signature beat, then
    writes a single ``ComplicationThread(kind="quest")`` to ``store`` via
    ``DungeonStore.open_thread``.  Emits ``quest_bound_span`` so the GM
    panel can verify the quest engine engaged rather than the narrator
    improvising quest outcomes.

    Returns the thread_id (deterministic: same campaign_seed + expansion_id
    always yields the same id regardless of store state).
    """
    b = select_signature(
        expansion=expansion,
        manifests_by_region=manifests_by_region,
        template=template,
    )
    thread_id = _expansion_quest_thread_id(campaign_seed, expansion.expansion_id)
    with quest_bound_span(
        expansion_id=expansion.expansion_id,
        signature_kind=b.kind,
        ref_id=b.ref_id,
        degraded=b.degraded,
        theme=b.theme,
    ):
        store.open_thread(
            ComplicationThread(
                thread_id=thread_id,
                origin_region_id=b.anchor_region,
                kind="quest",
                status="open",
                started_at_depth_score=started_at_depth_score,
                payload={
                    "scope": "expansion",
                    "expansion_id": expansion.expansion_id,
                    "signature_kind": b.kind,
                    "ref_id": b.ref_id,
                    "anchor_region": b.anchor_region,
                    "title": b.title,
                    "objective": b.objective,
                },
            )
        )
    return thread_id


# ---------------------------------------------------------------------------
# Projection: reconcile open expansion-quest threads into snapshot.quest_log
# ---------------------------------------------------------------------------

_DUNGEON_QUEST_PREFIX = "dungeon:exp"


def reconcile_dungeon_quests_into_log(
    *,
    snapshot: GameSnapshot,
    store: ThreadLedger,
    reached_expansion_ids: set[int],
) -> int:
    """Write/update namespaced QuestEntry rows (id ``dungeon:expN``) into
    ``snapshot.quest_log`` for every open expansion-quest thread whose
    expansion_id is in ``reached_expansion_ids``.

    - NEVER touches non-``dungeon:`` quest_log entries.
    - Idempotent: already-active entries are not duplicated; already-resolved
      entries are not reopened.
    - Returns the count of entries newly projected (0 on a no-op re-run).
    """
    projected = 0
    for thread in store.open_threads():
        if thread.kind != "quest" or thread.payload.get("scope") != "expansion":
            continue
        exp_id = thread.payload.get("expansion_id")
        if exp_id not in reached_expansion_ids:
            continue
        qid = f"{_DUNGEON_QUEST_PREFIX}{exp_id}"
        existing = snapshot.quest_log.get(qid)
        title = thread.payload.get("title", "")
        objective = thread.payload.get("objective", "")
        anchor = thread.payload.get("anchor_region")
        if existing is None:
            # Emit the mint span around the projection so the GM panel sees the
            # quest become player-visible — bound (seed) and resolved (complete)
            # already emit; this closes the silent-mint gap (158-42).
            with quest_minted_span(
                expansion_id=exp_id,
                quest_id=qid,
                signature_kind=thread.payload.get("signature_kind", ""),
            ):
                snapshot.quest_log[qid] = QuestEntry(
                    title=title,
                    objective=objective,
                    status="active",
                    anchor_id=anchor,
                )
                if anchor and anchor not in snapshot.quest_anchors:
                    snapshot.quest_anchors.append(anchor)
            projected += 1
        elif existing.status != "active":
            # Entry was externally resolved (e.g. "completed", "failed") — preserve it.
            continue
    return projected


# ---------------------------------------------------------------------------
# Resolution: check each open expansion-quest thread against fired beats
# ---------------------------------------------------------------------------


def _beat_fired(
    payload: dict,
    *,
    reached_region_ids: set[str],
    resolved_trope_ids: list[str],
    defeated_npc_names: set[str],
) -> str | None:
    """Return the resolving_event name if the thread's signature beat fired, else None."""
    kind = payload.get("signature_kind")
    ref = payload.get("ref_id", "")
    if kind == "reach_deep" and payload.get("anchor_region") in reached_region_ids:
        return "reach_deep"
    if kind == "set_piece" and ref in resolved_trope_ids:
        return "set_piece"
    if kind == "big_bad" and ref in defeated_npc_names:
        return "hp_depletion"
    return None


def resolve_expansion_quests(
    *,
    snapshot: GameSnapshot,
    store: ThreadLedger,
    reached_region_ids: set[str],
    resolved_trope_ids: list[str],
    defeated_npc_names: set[str],
    skip_expansion_ids: set[int] | None = None,
) -> int:
    """For each open expansion-quest thread whose signature beat has fired,
    resolve the ledger thread, flip the projected QuestEntry to "completed",
    and emit a quest_resolved_span.  Returns the count of quests resolved.

    ``skip_expansion_ids`` names expansions whose quest was *minted this same
    transition* — NO beat may resolve a quest on the step that first projects it
    into the log; the guard skips the whole thread regardless of signature kind.
    This fixes 158-42: a region-anchor ``reach_deep`` quest whose anchor is the
    entry region (a single-region expansion, or one where the entry scored
    deepest) otherwise mints-and-completes in one move, with its "way down past
    it" objective unmet. In practice only ``reach_deep`` is affected —
    ``big_bad``/``set_piece`` cannot resolve on the mint transition anyway (the
    antagonist isn't dead yet and the observer passes no resolved tropes) — but
    the skip is unconditional for safety. The player must descend to the anchor
    on a later transition. Defaults to an empty set, so the per-turn handshake
    and direct callers are unaffected.
    """
    skip = skip_expansion_ids or set()
    resolved = 0
    for thread in store.open_threads():
        if thread.kind != "quest" or thread.payload.get("scope") != "expansion":
            continue
        if thread.payload.get("expansion_id") in skip:
            continue
        event = _beat_fired(
            thread.payload,
            reached_region_ids=reached_region_ids,
            resolved_trope_ids=resolved_trope_ids,
            defeated_npc_names=defeated_npc_names,
        )
        if event is None:
            continue
        exp_id = thread.payload.get("expansion_id")
        if exp_id is None:
            raise ValueError(
                f"expansion-quest thread {thread.thread_id!r} missing expansion_id in payload"
            )
        with quest_resolved_span(
            expansion_id=exp_id,
            signature_kind=thread.payload.get("signature_kind", ""),
            resolving_event=event,
            ref_id=thread.payload.get("ref_id", ""),
        ):
            store.resolve_thread(thread.thread_id)
            entry = snapshot.quest_log.get(f"dungeon:exp{exp_id}")
            if entry is not None:
                entry.status = "completed"
        resolved += 1
    return resolved


def collect_defeated_npc_names(snapshot: GameSnapshot) -> set[str]:
    """Return the sanitized names of NPCs that have been defeated (HP at 0).

    This is the input the turn handshake feeds to ``resolve_expansion_quests``
    so a ``big_bad``-signature quest can resolve when its antagonist falls. An
    NPC at ``core.hp.current == 0`` is the durable defeat signal: the per-turn
    Monster-Manual re-inject deliberately does NOT heal a slain NPC back to a
    full pool (``green_room.admit()`` structurally never touches live
    ``core.hp`` on a merge — ADR-139 Inv-2, formerly the ``_merge_npc_patch``
    BUG-2b guard), so a killed big_bad stays pinned at 0/N across turns.

    Names are normalized through ``sanitize_display_name`` — the SAME transform
    ``select_signature`` applies when it binds the quest ref_id. The Monster-
    Manual inject sanitizes most names at the boundary, but the procedural
    region_population / room_binding inject branches
    (``monster_manual_inject``) append patches AFTER ``_sanitize_patch_names``
    runs, so a cache-sourced bracket-bearing big_bad can reach ``snapshot.npcs``
    raw. Normalizing here makes the ``ref in defeated_npc_names`` comparison
    symmetric regardless of mint path, so the kill never silently fails to
    resolve the quest.
    """
    return {
        sanitize_display_name(npc.core.name) for npc in snapshot.npcs if npc.core.hp.current == 0
    }


# ---------------------------------------------------------------------------
# Frontier observer factory — Task 8 wiring seam
# ---------------------------------------------------------------------------

# Type alias matching FrontierObserver (collections.abc.Callable[..., None]).
_FrontierObserver = Callable[..., None]


def _expansion_id_of(region_id: str) -> int | None:
    """Parse the expansion id from a region id (e.g. 'exp001.r3' → 1).

    Non-``exp`` ids (e.g. 'entrance') return ``None`` — they carry no
    expansion association and must not trigger quest projection.
    """
    if not region_id.startswith("exp"):
        return None
    try:
        return int(region_id.split(".", 1)[0][3:])
    except ValueError:
        return None


def make_expansion_quest_observer(store: ThreadLedger) -> _FrontierObserver:
    """Return a frontier observer that, on each region transition:

    1. Computes the expansion id from ``to_region`` (None for non-exp regions).
    2. Calls ``reconcile_dungeon_quests_into_log`` to project open expansion-quest
       threads for the reached expansion into ``snapshot.quest_log``.
    3. Calls ``resolve_expansion_quests`` to resolve any whose ``reach_deep``
       beat fired this transition.

    ``resolved_trope_ids`` is empty here — set_piece tropes resolve from the
    45-20 trope handshake in ``websocket_session_handler``, not from a region
    move. ``defeated_npc_names`` IS collected (158-17): a big_bad killed in a
    region we then walk out of should resolve on the move too, so we pass the
    live defeated set rather than a dead ``set()``. Resolution is idempotent
    (the ledger thread closes once), so the handshake + observer call sites
    cannot double-resolve.

    The returned callable matches the ``FrontierObserver`` signature:
    ``observer(*, snapshot, pc_name, from_region, to_region) -> None``.
    """

    def _observer(
        *,
        snapshot: GameSnapshot,
        pc_name: str,
        from_region: str | None,
        to_region: str,
    ) -> None:
        # AC-3: a multi-expansion-per-turn jump (e.g. exp1 -> exp5) descends
        # PAST the intermediate expansions; project every expansion traversed
        # this transition, not just the landed one, so their diverse quests
        # reach the quest_log.  When the origin is a non-exp region (entrance)
        # or absent, only the landed expansion is reachable this step.
        to_exp = _expansion_id_of(to_region)
        from_exp = _expansion_id_of(from_region) if from_region is not None else None
        reached_exps: set[int] = set()
        if to_exp is not None:
            if from_exp is not None:
                lo, hi = sorted((from_exp, to_exp))
                reached_exps = set(range(lo, hi + 1))
            else:
                reached_exps = {to_exp}
        before_ids = set(snapshot.quest_log.keys())
        reconcile_dungeon_quests_into_log(
            snapshot=snapshot,
            store=store,
            reached_expansion_ids=reached_exps,
        )
        # 158-42: a reach_deep quest minted THIS transition must not also resolve
        # on it — entering the anchor region is what made the quest visible, not
        # "the way down past it". Defer its completion to a later descent.
        newly_minted_exp_ids: set[int] = set()
        for qid in snapshot.quest_log.keys() - before_ids:
            if qid.startswith(_DUNGEON_QUEST_PREFIX):
                # Defensive parse-guard: dungeon: keys are produced ONLY by
                # reconcile_dungeon_quests_into_log as f"{_DUNGEON_QUEST_PREFIX}{exp_id}"
                # with an int exp_id, so the suffix always parses — the except is
                # structurally unreachable today and exists purely so a future
                # composite-id key shape can't crash the observer.
                try:
                    newly_minted_exp_ids.add(int(qid[len(_DUNGEON_QUEST_PREFIX) :]))
                except ValueError:
                    continue
        resolve_expansion_quests(
            snapshot=snapshot,
            store=store,
            reached_region_ids={to_region},
            resolved_trope_ids=[],
            defeated_npc_names=collect_defeated_npc_names(snapshot),
            skip_expansion_ids=newly_minted_exp_ids,
        )

    return _observer
