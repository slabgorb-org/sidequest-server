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

from pydantic import model_validator

from sidequest.protocol.base import ProtocolBase
from sidequest.protocol.dice import ThrowParams


class FateActionPayload(ProtocolBase):
    """Client -> server: a Fate action to seal (or a concession).

    - ``action``: the three proactive actions plus ``concede`` and the two compel
      verbs ``compel_accept`` / ``compel_refuse`` (ADR-144 F3e). ``concede`` and the
      compel verbs are pre-roll and non-committing — they route to
      ``concede_in_conflict`` / ``resolve_compel`` respectively, never to the commit
      ledger. A compel verb names the compelled aspect in ``aspect_text``. Defend is
      reactive (engine-rolled), never submitted — there is no full_defense (not in
      the Fate SRD).
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
    action: Literal[
        "overcome", "create_advantage", "attack", "concede", "compel_accept", "compel_refuse"
    ]
    skill: str = ""
    target: str | None = None
    difficulty: int = 0
    invoke_aspect: str = ""
    invoke_mode: Literal["bonus", "reroll"] = "bonus"
    aspect_text: str = ""
    player_action: str = ""


class FateThrowPayload(ProtocolBase):
    """Player-thrown PROACTIVE Fate action (ADR-148, Story 126-7).

    The Fate analog of ``DiceThrowPayload``: the four settled dF faces ARE the
    roll. The server resolves the action from ``face`` and NEVER calls ``roll_4df``
    on this path; ``throw_params`` is the gesture echoed on ``FATE_ROLL`` so every
    seat replays the same tumble (animation only). Faces are authoritative at the
    wire — exactly four, each in {-1, 0, 1}, ``extra='forbid'`` (inherited). A
    distinct, faces-required message (not an optional ``face`` on ``FATE_ACTION``)
    keeps the player-thrown contract unforgeable: an empty/absent faces field
    cannot re-open the server-rolls-for-players backdoor (No Silent Fallbacks).

    ``action`` is restricted to the three ROLL verbs; the non-roll verbs
    (``concede`` / ``compel_*``) never throw and stay on ``FateActionPayload``.
    The remaining fields mirror ``FateActionPayload``'s intent surface so the
    handler can build the dispatch from a throw 1:1.
    """

    request_id: str
    action: Literal["overcome", "create_advantage", "attack"]
    skill: str = ""
    target: str | None = None
    difficulty: int = 0
    invoke_aspect: str = ""
    invoke_mode: Literal["bonus", "reroll"] = "bonus"
    aspect_text: str = ""
    player_action: str = ""
    throw_params: ThrowParams
    face: tuple[int, int, int, int]

    @model_validator(mode="after")
    def _validate_faces(self) -> FateThrowPayload:
        # ``tuple[int, int, int, int]`` already enforces exactly-4 at the pydantic
        # layer; this adds the value-range check with a clear message (defense in
        # depth — the engine re-validates in resolve_action_from_faces).
        for f in self.face:
            if f not in (-1, 0, 1):
                raise ValueError("each dF face must be -1, 0, or +1")
        return self
