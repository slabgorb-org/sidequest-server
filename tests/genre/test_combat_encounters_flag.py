"""Playtest 2026-06-01 (blackthorn_moor shakedown) — combat-encounters capability.

The Monster Manual pre-generates *combat* encounters (B/X-style enemies with
HP, Strike/Power-Strike abilities, hostile disposition) and injects them into
``snapshot.npcs`` every turn. In a **social, Composure-only pack** like
``tea_and_murder`` there is no combat — yet encountergen falls back to humanoid
"enemies" built from the pack's social ``allowed_classes`` (Governess, Detective,
Society…), seeding drawing-room guests as ``disposition=-20`` combatants with
"The Retired Colonel's Instinct" abilities. That is the blocking defect the
DRIVER filed: combat stats + default hostility in a pack with no combat.

The fix is an explicit pack capability flag, ``combat_encounters`` (No Silent
Fallbacks — declared, not inferred): ``True`` by default so every combat pack is
unchanged; ``False`` for ``tea_and_murder`` so the Manual neither seeds nor
injects combat encounters.

Because ``RulesConfig`` is ``extra="forbid"``, the field is a paired
content+server change — the model must declare it or the YAML fails to load.
The pack-loading tests drive real content through ``load_genre_pack`` (the
wiring/integration tests).
"""

from __future__ import annotations

from sidequest.genre.loader import load_genre_pack
from sidequest.genre.models.rules import RulesConfig
from tests._helpers.genre_paths import find_pack_path

# --- model: the paired server field ----------------------------------------


def test_rules_config_defaults_combat_encounters_true():
    """Omitting the flag yields True — combat packs keep seeding encounters."""
    rules = RulesConfig.model_validate({})
    assert rules.combat_encounters is True


def test_rules_config_accepts_combat_encounters_false():
    """The model round-trips an explicit opt-out."""
    rules = RulesConfig.model_validate({"combat_encounters": False})
    assert rules.combat_encounters is False


# --- content (real loader) --------------------------------------------------


def test_tea_and_murder_disables_combat_encounters():
    """tea_and_murder (social, Composure-only) opts out of combat encounters.

    Loads the real pack through the production loader — the integration guard
    against the YAML key being dropped or the model field being removed.
    """
    pack = load_genre_pack(find_pack_path("tea_and_murder"))
    assert pack.rules.combat_encounters is False, (
        "tea_and_murder must disable combat_encounters — it is a social, "
        "Composure-only pack with no combat (survivability_pool_label=Composure)"
    )


def test_caverns_and_claudes_keeps_combat_encounters_enabled():
    """A mechanical fantasy pack keeps combat encounters (the default)."""
    pack = load_genre_pack(find_pack_path("caverns_and_claudes"))
    assert pack.rules.combat_encounters is True
