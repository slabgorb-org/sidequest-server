"""Structured statuses with severity tier — replaces bare-string statuses.

Spec: docs/superpowers/specs/2026-04-25-dual-track-momentum-design.md §Statuses.

Severity tiers drive recovery cadence and (v2) drive the absorption budget
when an encounter dial is about to cross threshold:

  - Scratch: clears at scene end (graze, lost composure, momentary shake).
  - Wound:   clears at session end or with rest (real injury, notable shake).
  - Scar:    persists until milestone or healing event (permanent mark).
  - Boon:    temporary BENEFICIAL effect from a working/consumable/scroll/
             potion (heightened senses, vigor, courage, the air sharpening
             after a draught). Clears at scene end alongside Scratch — Boons
             are scene-bounded by design so a single buff doesn't trail a
             party between encounters. Added 2026-04-30 to give the narrator
             a tool for prose-described magical effects from item use that
             previously had no schema slot (Mira / dungeon_survivor playtest).

v1 tracks shape only; the absorption mechanic ships in story 5. Boons do
NOT participate in absorption — they're flavor for the player, not mitigation.

Migration: existing saves carry ``CreatureCore.statuses`` as ``list[str]``.
``migrate_legacy_statuses`` converts a bare string to
``Status(text=<s>, severity=Scratch, absorbed_shifts=0, created_turn=0,
created_in_encounter=None)`` so loaders can call it during
``model_validator(mode="before")`` on CreatureCore.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class StatusSeverity(str, Enum):  # noqa: UP042 — matches project convention (see protocol/enums.py)
    """Status severity tier — drives recovery cadence and (v2) absorption budget."""

    Scratch = "Scratch"
    Wound = "Wound"
    Scar = "Scar"
    Boon = "Boon"


class Status(BaseModel):
    """An actor-level lingering cost.

    ``absorbed_shifts`` is 0 in v1; story 5 sets it from the severity's
    absorption budget when the status absorbs a would-be threshold cross.
    """

    model_config = {"extra": "forbid"}

    text: str
    severity: StatusSeverity
    absorbed_shifts: int = 0
    created_turn: int = 0
    created_in_encounter: str | None = None
    incapacitating: bool = False
    """True when this status takes the actor OUT of play — a dead/dying PC.

    sq-playtest 2026-06-07 (barsoom-3, blocking): a PC the lethality policy
    ruled ``dead`` kept full turn agency for four rounds because nothing
    durable said "this actor is out." String-matching the ``text`` is fragile
    (CLAUDE.md: structured markers, not source/text scraping); this is the
    structured signal the turn-intake gate and the death-surface message both
    key on. Set by ``post_resolution_lethality`` for LETHAL verdicts only — a
    recoverable ``Recovering`` setback leaves it False (the PC keeps agency).
    Additive default (False) → existing saves migrate cleanly."""

    source: str | None = None
    """Machine identity for reconcilable / system-applied statuses.

    Distinct from the human-readable ``text`` (which is for display and the
    narrator). System subsystems that re-assert a status against current state
    each tick (e.g. the light & darkness ``environment_clock``) tag their own
    status with a stable ``source`` so reconcile can find and remove *exactly*
    its own status without string-matching ``text`` — which is fragile under
    wording/i18n changes (CLAUDE.md: structured markers, not text scraping; cf.
    the ``incapacitating`` field added for the same reason). ``None`` (default)
    = an ordinary status with no machine owner, back-compat for every existing
    construction and save under ``extra='forbid'``."""

    roll_modifier: int = 0
    """-N/+N applied to ALL rolls while this status is active.

    The single generic "status modifies rolls" lever (Phase 2 of the
    light & darkness survival-clock spec). Penalties (e.g. fighting in the
    dark) are negative, boons (blessed, heightened senses) positive. Every
    roll site reads the aggregate via ``status_roll_modifier`` so a
    status-driven modifier can never silently no-op at one call site.
    Additive default (0) → existing saves migrate cleanly."""


def status_roll_modifier(core: object | None) -> int:
    """Sum ``roll_modifier`` across all statuses on a creature core.

    0 when ``core`` is None or carries no statuses. Bonuses and penalties
    stack additively. This is the single aggregation point every roll site
    calls so status-driven modifiers cannot silently no-op at one site.
    """
    if core is None:
        return 0
    statuses = getattr(core, "statuses", None) or []
    return sum(int(getattr(s, "roll_modifier", 0)) for s in statuses)


def migrate_legacy_statuses(raw: list[object]) -> list[Status]:
    """Forward-migrate a save's ``statuses`` field to structured Status list.

    Accepts a list whose entries are either bare ``str`` (legacy save) or
    already-structured ``Status`` instances (post-migration save). A list
    that contains anything else raises ``TypeError`` per CLAUDE.md
    "no silent fallbacks".
    """
    out: list[Status] = []
    for entry in raw:
        if isinstance(entry, Status):
            out.append(entry)
            continue
        if isinstance(entry, str):
            out.append(
                Status(
                    text=entry,
                    severity=StatusSeverity.Scratch,
                    absorbed_shifts=0,
                    created_turn=0,
                    created_in_encounter=None,
                )
            )
            continue
        if isinstance(entry, dict):
            out.append(Status.model_validate(entry))
            continue
        raise TypeError(
            f"unexpected entry in statuses list: {entry!r} "
            f"(type={type(entry).__name__}); "
            f"expected str, dict, or Status"
        )
    return out
