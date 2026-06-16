"""Fate Core wire payloads (ADR-144 F1d).

``FateActionPayload`` is the player's submission on the Fate channel — one of the
three proactive Fate actions (or a concession), the skill used, and the opposition
shape (active ``target`` or passive ``difficulty``). The 4dF roll is the server's
job (``FateRulesetModule.resolve_action``); the client submits the INTENT, not
faces (unlike DICE_THROW's physics-is-the-roll). The F3 UI and F2 narrator both
emit this message.
"""

from __future__ import annotations

from typing import Literal

from sidequest.protocol.base import ProtocolBase


class FateActionPayload(ProtocolBase):
    """Client -> server: a Fate action to seal (or a concession).

    - ``action``: the three proactive actions plus ``concede`` (concede is
      pre-roll and routes to ``concede_in_conflict``, never to the commit ledger).
      Defend is reactive (engine-rolled), never submitted — there is no
      full_defense (not in the Fate SRD).
    - ``skill``: the skill name used (empty for ``concede``).
    - ``target``: the opposed participant for an ACTIVE action (the engine rolls
      their defense); ``None`` for a passive action.
    - ``difficulty``: the passive opposition value when ``target`` is ``None``.
    - ``invoke_aspect``: an aspect text to invoke for +2 before the roll (spends a
      free invoke or a fate point — server-side via the F1b economy).
    - ``invoke_mode``: which KIND of invoke — ``'bonus'`` (flat +2) or ``'reroll'``
      (reroll the 4dF). Only meaningful when ``invoke_aspect`` is set. Defaults to
      ``'bonus'``: an omitting client keeps the +2 behavior the dispatch hardcoded
      before this field existed (Story 118-10). The Literal guard rejects an
      out-of-band mode loudly (No Silent Fallbacks) rather than coercing it.
    - ``aspect_text``: the situation aspect a ``create_advantage`` intends to place.
    - ``player_action``: freeform RP text the player typed alongside the Fate action
      tile (the "I swing from the chandelier and fire" flavor rider). Mirrors
      ``DiceThrowPayload.player_action`` (Story 108-5): it rides as narrator color
      ONLY and NEVER feeds the 4dF roll a bonus or a difficulty (Story 118-10).
      Defaults to ``''`` — no rider.
    """

    request_id: str
    action: Literal["overcome", "create_advantage", "attack", "concede"]
    skill: str = ""
    target: str | None = None
    difficulty: int = 0
    invoke_aspect: str = ""
    invoke_mode: Literal["bonus", "reroll"] = "bonus"
    aspect_text: str = ""
    player_action: str = ""
