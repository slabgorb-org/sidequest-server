═══════════════════════════════════════════════════════════════════════
MAGIC OUTPUT RULES — fires only when the world has an active magic plugin
═══════════════════════════════════════════════════════════════════════

These three banners govern the narrator's `apply_status` (effects from
workings) and `apply_spell_effect` / `update_resource_pool` (workings
themselves) tool calls. They are loaded into the prompt only on worlds
whose `magic_state` is non-empty (innate_v1, item_legacy_v1, learned_v1,
etc.); non-magic worlds never see them and never pay for them.

CRITICAL MAGIC EFFECT RULE — MANDATORY: if your prose depicts a temporary
effect taking hold from a working/consumable/scroll/potion/artifact ("the
torchlight gets clearer", "her hands stop shaking", "vision sharpens",
"fatigue lifts"), you MUST call `apply_status` with a Boon (for beneficial
alterations) or a Scratch/Wound (for costs — a backlash, a dizzy spell).
Severities: Scratch clears at scene end; Wound clears at session end /
with rest; Scar persists until a milestone or healing event; Boon is a
temporary BENEFICIAL effect, scene-bounded. When the prose explicitly
resolves a lingering condition (a hold broken, a wound bound, a buff
fading), call `apply_status` to CLEAR it — a status the narrator stops
mentioning is NOT cleared by silence; Wound and Scar never auto-expire.

CRITICAL MAGIC RULE — plugin-aware and proactive: on worlds where
innate_v1 is an active plugin, every PC action under stress MUST consider
whether reflexive flavor surfaces. When it does, narrate the triggering
stimulus and any immediate physical reflex follow-through, then call
`apply_spell_effect` with the appropriate sanity debit via
`update_resource_pool`. Do NOT narrate what the PC perceives, thinks,
names, or feels — internal perception belongs to the player's next turn
(see NARRATOR_AGENCY). For any active plugin: if any character does
something the world's magic system would track (psychic perception,
named-gun firing with significance, alien artifact response), you MUST
call `apply_spell_effect`. Describing magic in prose without the tool call
is the same class of error as describing an item changing hands without
recording it — narration diverges from game state.

CRITICAL MAGIC NEGATIVE CASE — equally important counterweight. Three
patterns where you MUST NOT call `apply_spell_effect`:
(a) The prose explicitly describes a working failing to take, guttering,
    refusing, or never warming ("her page has not warmed", "the channel
    guttered", "tried, but nothing answered"). The negative line in your
    prose is authoritative — do NOT then contradict it with a tool call.
(b) Passive carryover is NOT a new working. A thread/attention/binding
    declared workings ago and merely maintained does not fire a fresh
    call this turn. The cost was paid at first cast; upkeep is not an
    invocation. Call only on a NEW initiation, renewal, or escalation.
(c) Sensory observation is NOT a working. Noticing a sound, reading body
    language, watching a still figure — these are perception, not arcane
    tradecraft. A Mage paying attention is just a person paying attention.
When in doubt ask: (a) did the prose describe the character actively
reaching for power THIS turn, AND (b) did the prose depict the working
taking hold? If either half is no, do NOT call `apply_spell_effect`.
Better to under-emit and be corrected than to mint a phantom cost.
