from sidequest.server.dispatch.region_population import (  # noqa: F401
    RegionCreature,
    load_region_population,
)


class _FakeMutation:
    def __init__(self, region_id, kind, payload):
        self.region_id, self.kind, self.payload = region_id, kind, payload


class _FakeRepo:
    def __init__(self, muts):
        self._muts = muts

    def load_mutations(self):
        return self._muts


def _pop_payload(region_id):
    return {
        "region_id": region_id,
        "creatures": [
            {
                "name": "Gnaw-Swarm",
                "creature_type": "swarm",
                "telegraph": "chittering",
                "hp": {"current": 6, "max": 6, "base_max": 6},
                "threat_level": 1,
            }
        ],
        "big_bad": {
            "name": "Pale Mother",
            "creature_type": "big_bad",
            "telegraph": "deep band",
            "hp": {"current": 24, "max": 24, "base_max": 24},
            "threat_level": 2,
        },
    }


def test_load_region_population_parses_roster_and_big_bad():
    repo = _FakeRepo(
        [
            _FakeMutation("exp002.r3", "setpiece_state", {"x": 1}),
            _FakeMutation("exp002.r3", "region_population", _pop_payload("exp002.r3")),
        ]
    )
    roster, big_bad = load_region_population(repo, "exp002.r3")
    assert [c.name for c in roster] == ["Gnaw-Swarm"]
    assert roster[0].hp == 6 and roster[0].threat_level == 1
    assert big_bad is not None and big_bad.name == "Pale Mother" and big_bad.threat_level == 2


def test_load_region_population_empty_for_unknown_region():
    repo = _FakeRepo([_FakeMutation("exp002.r3", "region_population", _pop_payload("exp002.r3"))])
    roster, big_bad = load_region_population(repo, "exp999.r9")
    assert roster == [] and big_bad is None
