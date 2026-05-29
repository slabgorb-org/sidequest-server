"""Single source of truth for the four Recency-zone narrator guardrails.

ADR-111 §Implementation Notes mandates that the prose constants governing
the narrator's `npcs_present` / `confrontation` / `npcs_present`
(extraction) / `location` emission rules live in one module, so every
consumer references the same string — no duplication, no silent drift.

SDK-path consumers (per guardrail — the default `anthropic_sdk` backend):
  - `npc_intro_visual_constraint`  → `narrator_prompts/output_only.md`
    (`NARRATOR_OUTPUT_ONLY`, Primacy/Stable cached sidecar).
  - `npc_extraction_constraint`    → `NARRATOR_OUTPUT_ONLY`.
  - `location_patch_constraint`    → `apply_world_patch` tool description.
  - `confrontation_trigger_constraint` → its framing-neutral core
    (`CONFRONTATION_TRIGGER_CORE`) is composed into the IntentRouter
    `_SYSTEM_PROMPT` (`sidequest/agents/intent_router.py`). On the SDK path
    the narrator no longer emits the `confrontation` patch field — the
    IntentRouter (ADR-113) decides the trigger pre-narrator — so the
    recognition steering lives where the decision is made (story 61-18).
    The pre-61-18 claim that this guardrail rode a `generate_encounter`
    tool description was stale: that tool's `begin_confrontation` lift was
    retired in 59-4.

The legacy `claude -p` / Ollama path (opt-in, non-default) still emits a
`game_patch`, so it consumes the full narrator-framed constants via
`_maybe_register_legacy_guardrail` (`orchestrator.py`). The constants are
kept byte-identical to the prior inline strings so that path stays
un-drifted from pre-111 behavior (ADR-111 §Decision: legacy path
byte-identical) — `CONFRONTATION_TRIGGER_CONSTRAINT` is now composed from
`CONFRONTATION_TRIGGER_CORE` but reproduces the original bytes exactly.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Guardrail 1 — npc_intro_visual_constraint
# ---------------------------------------------------------------------------
# Playtest 2026-05-03 [BUG] — render policy fired NPC_INTRO for two newly
# auto-registered NPCs but the narrator emitted no visual_scene. ADR-014
# (Diamonds and Coal) treats first-introduction prose as a diamond; the
# visual is part of that diamond, not optional.
NPC_INTRO_VISUAL_CONSTRAINT: str = (
    "<npc-intro-visual>\n"
    "When you introduce a NEW named NPC for the first time "
    "this session — i.e. you set ``is_new: true`` on their "
    "entry in ``npcs_present`` — your game_patch MUST also "
    "include a ``visual_scene`` whose ``subject`` describes "
    "that NPC (their appearance, posture, and the moment "
    'the player is meeting them). Use tier ``"portrait"`` '
    'for a single character close-up, or ``"landscape"`` '
    "when the introduction is inseparable from the place "
    "(a foreman silhouetted against the rig, a customs "
    "officer at the freight stair). If multiple NPCs are "
    "introduced in the same turn, pick the one whose "
    "introduction carries the most narrative weight — the "
    "visual is the diamond on that introduction. Recurring "
    "NPCs (``is_new: false``) do NOT require a fresh "
    "visual_scene; this rule fires only on the first reveal.\n"
    "</npc-intro-visual>"
)

# ---------------------------------------------------------------------------
# Guardrail 2 — confrontation_trigger_constraint
# ---------------------------------------------------------------------------
# Pingpong 2026-05-03 [BUG] — narrator wrote a textbook chase-firing beat
# but the game_patch carried ``confrontation=None``. This prompt guardrail
# steers the narrator to emit ``confrontation`` proactively. The validator
# (confrontation_intent_validator) catches post-hoc mismatches and emits
# confrontation.intent_mismatch spans to the GM panel. Together they close
# the gap without server-side auto-firing (which would be a silent fallback).
#
# Story 61-18: the trigger-recognition body is extracted into
# ``CONFRONTATION_TRIGGER_CORE`` below. On the default SDK path the narrator no
# longer emits the ``confrontation`` patch field — the IntentRouter (ADR-113)
# decides the trigger pre-narrator — so the recognition steering moved to the
# router's ``_SYSTEM_PROMPT`` (``sidequest/agents/intent_router.py``), which
# composes the SAME core under its DispatchPackage framing. This constant keeps
# the narrator/``game_patch`` framing for the opt-in legacy ``claude -p`` /
# Ollama path, which DOES still emit a game_patch. One source, two framings, no
# prose duplication (ADR-111 §Implementation Notes).

# Framing-neutral trigger-recognition core (single source of truth, story
# 61-18). Describes WHAT fictional beat counts as a confrontation trigger and
# that the mechanical commit lands on the turn the trigger appears — with no
# producer-contract framing (no ``game_patch``, no ``beat_selections``), so
# both the legacy narrator path (wrapped below) and the SDK-path IntentRouter
# consume identical prose. The load-bearing regression fingerprints
# ("Do NOT defer it to the next turn", "exactly as mechanically binding as a
# weapon drawn") live here — ADR-111 §Alternatives B kept the concrete
# examples deliberately; they are the regression detector, not flavor.
CONFRONTATION_TRIGGER_CORE: str = (
    "Pick the MOST SPECIFIC type the genre offers; "
    "never default to a generic ``combat`` when "
    "``ship_combat``, ``dogfight``, ``social_duel``, or "
    "another specialized type applies. Spell the type "
    "exactly as it appears in the available list "
    "(lowercase, snake_case where compound).\n"
    "Combat / pursuit triggers (``combat``, "
    "``ship_combat``, ``dogfight``, ``chase``): a hostile "
    "chassis spinning its reactor up, a patrol or pursuer "
    "requesting permission to engage, weapons drawn / "
    "charged / going hot, an intercept order, a boarding "
    "action, an antagonist drawing a weapon, opening "
    "fire, or otherwise making a hostile commit against "
    "the party.\n"
    "Social triggers (``negotiation``, ``trial``, "
    "``auction``, ``social_duel``, ``scandal``): a price "
    "named and a counter-offer expected (``negotiation``); "
    "a summons served, the docket called, a witness "
    "sworn before the magistrate (``trial``); an "
    "auctioneer calling the lot, paddles raised, "
    '"going once" (``auction``); a card declined, the '
    "cut direct, seconds appointed, a formal challenge "
    "issued (``social_duel``); a rumour reaching print, "
    "exposure in the society pages, a blackmail letter "
    "on the salver (``scandal``). Social-pack triggers "
    "are NOT optional — a scandal breaking in print is "
    "exactly as mechanically binding as a weapon drawn.\n"
    "The mechanical commit belongs to the turn the "
    "trigger appears in fiction. Do NOT defer it to the "
    "next turn — there is no retroactive crediting. If "
    "the cutter spins up THIS turn, fire ``chase`` THIS "
    "turn. If a hostile draws a weapon THIS turn, fire "
    "``combat`` THIS turn. If the auctioneer calls the "
    "lot THIS turn, fire ``auction`` THIS turn. The "
    "system handles de-escalation gracefully if the "
    "resolution swerves; an unfired encounter cannot "
    "be created later.\n"
    "Edge cases: if the engagement is described as the "
    "uniform / pursuer ASKING someone else (a tower, a "
    "command channel) for permission — fire the "
    "encounter NOW. The asking IS the trigger. Waiting "
    'for the explicit "go" produces a turn of prose '
    "with no mechanical track, and the Diamonds-and-Coal "
    "promise is broken (ADR-014). Same rule on the "
    "social side: when the writ is served, fire "
    "``trial`` now — do not wait for the court to "
    "convene.\n"
)

# Legacy narrator-path guardrail: the framing-neutral core wrapped in the
# narrator's ``game_patch`` contract. Kept byte-identical to the pre-61-18
# inline string (ADR-111 §Decision: legacy path byte-identical).
CONFRONTATION_TRIGGER_CONSTRAINT: str = (
    "<confrontation-trigger>\n"
    "If your prose this turn describes any stake-binding "
    "engagement — physical, social, or reputational — "
    "your ``game_patch`` MUST populate ``confrontation`` "
    "with the matching type from AVAILABLE ENCOUNTER "
    "TYPES. " + CONFRONTATION_TRIGGER_CORE + "Only emit ``confrontation`` on the turn the "
    "encounter STARTS; once it is active, use "
    "``beat_selections`` for subsequent rounds."
    "\n</confrontation-trigger>"
)

# ---------------------------------------------------------------------------
# Guardrail 3 — npc_extraction_constraint
# ---------------------------------------------------------------------------
# 2026-05-11 Glenross [BUG]: narrator wrote dialogue about Father in detail
# but emitted ``npcs_present`` covering only Reverend Murchison + the
# pinafore girl. Father lived only in prose. Turn 6 then invented "the
# wee one's mother / her" with no roster constraint to refuse.
NPC_EXTRACTION_CONSTRAINT: str = (
    "<npc-extraction>\n"
    "Any person named or role-named in this turn's "
    "prose — including patients, parents, children, "
    "siblings, and recurring townsfolk — MUST appear "
    "in ``npcs_present``. If your prose names "
    "``Father``, ``Mother``, ``the doctor``, ``the "
    "Reverend``, ``Mrs. <Name>``, ``Mr. <Name>``, "
    "``Dr. <Name>``, or any other role-named or "
    "honorific-named individual, they MUST be emitted "
    "with a ``name``, ``role``, and ``pronouns`` in "
    "``npcs_present`` — even if they don't speak this "
    "turn, even if they're only mentioned in passing.\n"
    "Patients on a sickbed count. Parents at a hearth "
    "count. Children at a doorway count. Siblings in "
    "the next room count. The grieving widow, the "
    "stable-boy holding the lantern, the apothecary's "
    "apprentice — all count.\n"
    "This is how the roster stays consistent across "
    "turns. A name or role mentioned only in prose, "
    "never emitted in ``npcs_present``, is invisible "
    "to the next turn's reasoning — and the gap "
    "invites a slip (gender flip, role flip, name "
    "drift). The server runs a catch-loop that auto-"
    "mints prose-only first-mentions, but the catch-"
    "loop is a safety net, not the source of truth — "
    "you are."
    "\n</npc-extraction>"
)

# ---------------------------------------------------------------------------
# Guardrail 4 — location_patch_constraint
# ---------------------------------------------------------------------------
# 2026-05-11 Glenross [BUG]: across five turns the narrator wrote bold room
# headers while ``character_locations[Ziggy]='the_manse'`` lagged the prose
# because ``game_patch.location`` was empty on turns 2-5. SOUL.md
# "Illusionism": narrator and state on different tracks, GM panel blind.
LOCATION_PATCH_CONSTRAINT: str = (
    "<location-patch>\n"
    "If your prose this turn opens a new scene with a "
    "bold room header (``**Title**`` or ``## **Title**``) "
    "OR your prose moves the party into a different named "
    "space, your ``game_patch.location`` MUST be set to "
    "the new room.\n"
    "State must not lag prose. A bold title with no "
    "matching ``location`` field leaves the GM panel "
    "and the canonical ``character_locations`` map "
    "pointing at the prior room while the players are "
    "reading the new one — the same Illusionism failure "
    "mode SOUL.md warns against.\n"
    "If the scene has NOT changed and you are continuing "
    "in the same room, omit ``location`` (or set it to "
    "the current value). The server runs a drift-repair "
    "backstop that auto-promotes leading bold titles "
    "into ``character_locations`` and emits a WARNING-"
    "level ``narrator.location_drift_repaired`` span — "
    "but the backstop is a safety net, not the source "
    "of truth. You are."
    "\n</location-patch>"
)


# Convenience tuple — every consumer that needs to iterate (the
# `narrator.recency_guardrails_skipped` span attrs, the bytes_saved
# computation) reads this so adding a fifth guardrail doesn't require
# editing every call site.
ALL_GUARDRAILS: tuple[tuple[str, str], ...] = (
    ("npc_intro_visual_constraint", NPC_INTRO_VISUAL_CONSTRAINT),
    ("confrontation_trigger_constraint", CONFRONTATION_TRIGGER_CONSTRAINT),
    ("npc_extraction_constraint", NPC_EXTRACTION_CONSTRAINT),
    ("location_patch_constraint", LOCATION_PATCH_CONSTRAINT),
)

# Precomputed views of ``ALL_GUARDRAILS`` for hot-path consumers.
# ``Orchestrator.build_narrator_prompt`` emits the
# ``narrator.recency_guardrails_skipped`` span on every turn; computing
# these from the tuple every time is wasted work since the values are
# static after module load.
GUARDRAIL_NAMES: tuple[str, ...] = tuple(name for name, _ in ALL_GUARDRAILS)
TOTAL_PROSE_BYTES: int = sum(len(prose) for _, prose in ALL_GUARDRAILS)
