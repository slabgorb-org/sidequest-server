"""Tool adapters — Phase C populates this package.

Each adapter module calls @tool at import time. This barrel imports each
adapter so the registry is loaded by importing this package.
"""

# Phase C will add lines like:
#   from sidequest.agents.tools import lookup_monster  # noqa: F401
# one per adapter, here.
from sidequest.agents.tools import (
    advance_confrontation,  # noqa: F401
    advance_encounter_beat,  # noqa: F401
    advance_scene_clue,  # noqa: F401
    apply_damage,  # noqa: F401
    apply_spell_effect,  # noqa: F401
    apply_status,  # noqa: F401
    apply_world_patch,  # noqa: F401
    begin_confrontation,  # noqa: F401
    commit_known_fact,  # noqa: F401
    generate_encounter,  # noqa: F401
    generate_loadout,  # noqa: F401
    generate_name,  # noqa: F401
    get_world_grounding,  # noqa: F401
    list_npcs_in_scene,  # noqa: F401
    lookup_monster,  # noqa: F401
    query_character,  # noqa: F401
    query_encounter,  # noqa: F401
    query_gossip,  # noqa: F401
    query_known_facts,  # noqa: F401
    query_lore,  # noqa: F401
    query_magic_state,  # noqa: F401
    query_npc,  # noqa: F401
    query_scenario_clues,  # noqa: F401
    query_scene_state,  # noqa: F401
    resolve_location_entity,  # noqa: F401
    roll_dice,  # noqa: F401
    tick_tropes,  # noqa: F401
    update_npc_disposition,  # noqa: F401
    update_resource_pool,  # noqa: F401
)
