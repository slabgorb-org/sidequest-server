"""Slash commands — /status, /inventory, /quests, /map, /save, /gm.

Dispatch via a ``CommandHandler`` ABC plus a dataclass-style
``CommandResult`` union.

Phase 1 covers all six commands (status, inventory, quests, map, save,
gm) since they are pure read-only or produce ``WorldStatePatch``
mutations — no combat/dice/chase dependency.

Deferred (Phase 3+): commands that depend on ``StructuredEncounter`` or
dice resolution (e.g., ``/roll``) are not implemented here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sidequest.game.session import GameSnapshot, QuestEntry, WorldStatePatch


# ---------------------------------------------------------------------------
# CommandResult — sum type for handler return values
# ---------------------------------------------------------------------------


@dataclass
class DisplayResult:
    """A formatted string to display to the player."""

    text: str


@dataclass
class ErrorResult:
    """An error message."""

    message: str


@dataclass
class StateMutationResult:
    """A state patch to apply."""

    patch: WorldStatePatch


# Sum type for command handler return values.
CommandResult = DisplayResult | ErrorResult | StateMutationResult


# ---------------------------------------------------------------------------
# CommandHandler base class
# ---------------------------------------------------------------------------


class CommandHandler(ABC):
    """Base class for slash command handlers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """The command name (without the leading slash)."""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """One-line description shown by /help."""
        ...

    @abstractmethod
    def handle(self, state: GameSnapshot, args: str) -> CommandResult:
        """Handle the command and return a result."""
        ...


# ---------------------------------------------------------------------------
# /status — character HP, level, class, race, location, stats
# ---------------------------------------------------------------------------


class StatusCommand(CommandHandler):
    """Show the character's current condition."""

    @property
    def name(self) -> str:
        return "status"

    @property
    def description(self) -> str:
        return "Show your character's current condition"

    def handle(self, state: GameSnapshot, args: str) -> CommandResult:
        if not state.characters:
            return ErrorResult("No character found")

        ch = state.characters[0]
        location = state.party_location(perspective=ch.core.name) or "(unknown location)"
        output = (
            f"{ch.core.name} — Level {ch.core.level} {ch.race} {ch.char_class}\n"
            f"HP: {ch.core.hp.current}/{ch.core.hp.max}\n"
            f"Location: {location} ({state.current_region})"
        )

        if ch.stats:
            output += "\n\n"
            for stat, value in sorted(ch.stats.items()):
                output += f"  {stat:<12} {value}\n"

        if ch.abilities:
            output += "\nAbilities:\n"
            for ability in ch.abilities:
                output += f"  • {ability.genre_description} — {ability.mechanical_effect}\n"

        if ch.narrative_state:
            output += f"\n{ch.narrative_state}"

        return DisplayResult(output)


# ---------------------------------------------------------------------------
# /inventory — items, gold
# ---------------------------------------------------------------------------


class InventoryCommand(CommandHandler):
    """List carried items."""

    @property
    def name(self) -> str:
        return "inventory"

    @property
    def description(self) -> str:
        return "List your carried items"

    def handle(self, state: GameSnapshot, args: str) -> CommandResult:
        if not state.characters:
            return ErrorResult("No character found")

        ch = state.characters[0]
        inv = ch.core.inventory

        # items is list[dict] — filter by state
        carried = [
            i for i in inv.items if i.get("state", "Carried") in ("Carried", {"kind": "Carried"})
        ]

        if not carried and inv.gold == 0:
            return DisplayResult("You carry nothing of note. Your pockets are empty.")

        output = ""
        equipped = [i for i in carried if i.get("equipped", False)]
        output += "EQUIPPED:\n"
        if not equipped:
            output += "  (nothing equipped)\n"
        else:
            for item in equipped:
                output += f"  {item.get('name', '?')} — {item.get('description', '')}\n"

        pack = [i for i in carried if not i.get("equipped", False)]
        output += "\nPACK:\n"
        if not pack:
            output += "  (empty)\n"
        else:
            for item in pack:
                qty = item.get("quantity", 1)
                if qty > 1:
                    output += f"  {item.get('name', '?')} x{qty}\n"
                else:
                    output += f"  {item.get('name', '?')}\n"

        output += f"\nGold: {inv.gold}"
        return DisplayResult(output)


# ---------------------------------------------------------------------------
# /quests — active, completed, failed quests
# ---------------------------------------------------------------------------


class QuestsCommand(CommandHandler):
    """Show the quest log."""

    @property
    def name(self) -> str:
        return "quests"

    @property
    def description(self) -> str:
        return "Show your quest log"

    def handle(self, state: GameSnapshot, args: str) -> CommandResult:
        if not state.quest_log:
            return DisplayResult("No quests recorded yet. The story is just beginning.")

        active = []
        completed = []
        failed = []

        # Story 77-2: quest_log values are structured QuestEntry now. Classify
        # on the status field; display the quest's objective when it carries
        # one, else the raw status.
        for name, entry in state.quest_log.items():
            if entry.status.startswith("completed"):
                completed.append((name, entry))
            elif entry.status.startswith("failed"):
                failed.append((name, entry))
            else:
                active.append((name, entry))

        def _line(name: str, entry: QuestEntry) -> str:
            label = entry.title or name
            detail = entry.objective or entry.status
            return f"  {label} — {detail}\n"

        output = ""
        if active:
            output += "ACTIVE QUESTS:\n"
            for name, entry in active:
                output += _line(name, entry)
        if completed:
            output += "\nCOMPLETED:\n"
            for name, entry in completed:
                output += _line(name, entry)
        if failed:
            output += "\nFAILED:\n"
            for name, entry in failed:
                output += _line(name, entry)

        return DisplayResult(output)


# ---------------------------------------------------------------------------
# /map — discovered regions and routes
# ---------------------------------------------------------------------------


class MapCommand(CommandHandler):
    """Show discovered regions and routes."""

    @property
    def name(self) -> str:
        return "map"

    @property
    def description(self) -> str:
        return "Show discovered regions and routes"

    def handle(self, state: GameSnapshot, args: str) -> CommandResult:
        output = "REGIONS:\n"
        if not state.discovered_regions:
            output += "  No regions discovered yet.\n"
        else:
            for region in state.discovered_regions:
                if region == state.current_region:
                    output += f"  * {region} (current)\n"
                else:
                    output += f"    {region}\n"

        output += "\nROUTES:\n"
        if not state.discovered_routes:
            output += "  No routes discovered yet.\n"
        else:
            for route in state.discovered_routes:
                output += f"  {route}\n"

        return DisplayResult(output)


# ---------------------------------------------------------------------------
# /save — triggers persistence
# ---------------------------------------------------------------------------


class SaveCommand(CommandHandler):
    """Save the game."""

    @property
    def name(self) -> str:
        return "save"

    @property
    def description(self) -> str:
        return "Save your game"

    def handle(self, state: GameSnapshot, args: str) -> CommandResult:
        name = state.characters[0].core.name if state.characters else "Unknown"
        return DisplayResult(f"Game saved for {name}.")


# ---------------------------------------------------------------------------
# /gm — operator commands (set, teleport, spawn, dmg)
# ---------------------------------------------------------------------------


class GmCommand(CommandHandler):
    """Game master commands (operator only)."""

    @property
    def name(self) -> str:
        return "gm"

    @property
    def description(self) -> str:
        return "Game master commands (operator only)"

    def handle(self, state: GameSnapshot, args: str) -> CommandResult:

        parts = args.split(" ", 1)
        sub = parts[0]
        sub_args = parts[1].strip() if len(parts) > 1 else ""

        if sub == "set":
            return self._handle_set(sub_args)
        elif sub == "teleport":
            return self._handle_teleport(sub_args)
        elif sub == "spawn":
            return self._handle_spawn(sub_args)
        elif sub == "dmg":
            return self._handle_dmg(sub_args)
        elif sub == "":
            return ErrorResult("Usage: /gm <set|teleport|spawn|dmg> [args]")
        else:
            return ErrorResult(f"Unknown GM subcommand: {sub}")

    def _handle_set(self, args: str) -> CommandResult:
        from sidequest.game.session import WorldStatePatch

        parts = args.split(" ", 1)
        if len(parts) < 2:
            return ErrorResult("Usage: /gm set <field> <value>. Missing value.")

        field_name, value = parts[0], parts[1]
        patch = WorldStatePatch()

        if field_name == "location":
            patch.location = value
        elif field_name == "time_of_day":
            patch.time_of_day = value
        elif field_name == "atmosphere":
            patch.atmosphere = value
        elif field_name == "current_region":
            patch.current_region = value
        elif field_name == "active_stakes":
            patch.active_stakes = value
        else:
            return ErrorResult(
                f"Unknown field: '{field_name}'. Valid fields: location, time_of_day, atmosphere, current_region, active_stakes"
            )
        return StateMutationResult(patch)

    def _handle_teleport(self, args: str) -> CommandResult:
        from sidequest.game.session import WorldStatePatch

        parts = args.split(" ", 1)
        if len(parts) < 2:
            return ErrorResult("Usage: /gm teleport <region> <location>")

        region, location = parts[0], parts[1]
        patch = WorldStatePatch(
            location=location,
            current_region=region,
            discover_regions=[region],
        )
        return StateMutationResult(patch)

    def _handle_spawn(self, args: str) -> CommandResult:
        from sidequest.game.session import NpcPatch, WorldStatePatch

        if not args:
            return ErrorResult("Usage: /gm spawn <name> [role] [personality]")

        parts = args.split(" ", 2)
        npc_name = parts[0]
        role = parts[1] if len(parts) > 1 else None
        personality = parts[2] if len(parts) > 2 else None

        npc = NpcPatch(name=npc_name, role=role, personality=personality)
        patch = WorldStatePatch(npcs_present=[npc])
        return StateMutationResult(patch)

    def _handle_dmg(self, args: str) -> CommandResult:
        from sidequest.game.session import WorldStatePatch

        args = args.strip()
        if not args:
            return ErrorResult("Usage: /gm dmg <target> <amount>")

        rsplit = args.rsplit(" ", 1)
        if len(rsplit) < 2:
            return ErrorResult("Usage: /gm dmg <target> <amount>")

        target, amount_str = rsplit[0], rsplit[1]
        try:
            amount = int(amount_str)
        except ValueError:
            return ErrorResult(f"Invalid number '{amount_str}'. Amount must be a valid integer.")

        patch = WorldStatePatch(hp_changes={target: -amount})
        return StateMutationResult(patch)


# ---------------------------------------------------------------------------
# Built-in command registry
# ---------------------------------------------------------------------------

BUILTIN_COMMANDS: list[CommandHandler] = [
    StatusCommand(),
    InventoryCommand(),
    QuestsCommand(),
    MapCommand(),
    SaveCommand(),
    GmCommand(),
]
