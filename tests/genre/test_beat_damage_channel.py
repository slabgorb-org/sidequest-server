from sidequest.genre.models.rules import BeatDef, DamageChannel


def _beat(**kw):
    base = dict(id="b", label="L", kind="strike", stat_check="MIGHT")
    base.update(kw)
    return BeatDef(**base)


def test_damage_channel_defaults_to_none():
    assert _beat().damage_channel is DamageChannel.none


def test_strike_and_brace_channels_parse():
    assert _beat(damage_channel="strike").damage_channel is DamageChannel.strike
    assert _beat(damage_channel="brace").damage_channel is DamageChannel.brace


def test_creature_natural_attack_override():
    b = _beat(damage_channel="strike", damage_override={"dice": "1d8", "bonus": 0})
    assert b.damage_override.dice == "1d8"
    assert _beat(damage_channel="brace", mitigation_override=1).mitigation_override == 1
