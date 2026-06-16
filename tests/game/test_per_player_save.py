"""Peer save receives only the filtered event stream.

Canonical save on the narrator-host holds the union; peer save holds the
filtered subset per MP spec 2026-04-22. These tests assert the session
handler's write-split helper is correct.

The helper we test is pure:
  inputs:  event_log (accepts append), filter (project), envelope, players, view
  outputs: (canonical appended once) + (list of sent frames per include=True)
"""

import pytest

# Import the per-turn write-split under test. Exact name depends on where
# you place the helper — put it wherever feels natural and import from there.
from sidequest.server.session_handler import apply_turn_writes_for_test


class _FakeEventLog:
    def __init__(self):
        self.canonical = []
        self.next_seq = 0

    def append(self, envelope):
        self.canonical.append(envelope)
        self.next_seq += 1


class _FakeFilterAllowAll:
    def project(self, *, envelope, view, player_id):
        from sidequest.game.projection_filter import FilterDecision

        return FilterDecision(include=True, payload_json=envelope.payload_json)


class _FakeFilterP1Only:
    def project(self, *, envelope, view, player_id):
        from sidequest.game.projection_filter import FilterDecision

        if player_id == "p1":
            return FilterDecision(include=True, payload_json=envelope.payload_json)
        return FilterDecision(include=False, payload_json="")


@pytest.fixture
def fake_event_log():
    return _FakeEventLog()


@pytest.fixture
def fake_filter_allow_all():
    return _FakeFilterAllowAll()


@pytest.fixture
def fake_filter_p1_only():
    return _FakeFilterP1Only()


def test_canonical_save_gets_unfiltered_event(fake_event_log, fake_filter_allow_all):
    apply_turn_writes_for_test(
        event_log=fake_event_log,
        filter=fake_filter_allow_all,
        envelope={"kind": "NARRATION", "payload": {"text": "X"}},
        connected_players=["p1", "p2"],
    )
    assert len(fake_event_log.canonical) == 1


def test_peer_frames_sent_only_when_filter_includes(fake_event_log, fake_filter_p1_only):
    sent = apply_turn_writes_for_test(
        event_log=fake_event_log,
        filter=fake_filter_p1_only,
        envelope={
            "kind": "NARRATION",
            "payload": {"text": "X", "_visibility": {"visible_to": ["p1"]}},
        },
        connected_players=["p1", "p2"],
    )
    assert [f.player_id for f in sent] == ["p1"]


def test_allow_all_filter_sends_to_every_connected_player(fake_event_log, fake_filter_allow_all):
    sent = apply_turn_writes_for_test(
        event_log=fake_event_log,
        filter=fake_filter_allow_all,
        envelope={"kind": "NARRATION", "payload": {"text": "Dawn"}},
        connected_players=["p1", "p2", "p3"],
    )
    assert {f.player_id for f in sent} == {"p1", "p2", "p3"}


def test_empty_connected_player_list_produces_no_frames(fake_event_log, fake_filter_allow_all):
    sent = apply_turn_writes_for_test(
        event_log=fake_event_log,
        filter=fake_filter_allow_all,
        envelope={"kind": "NARRATION", "payload": {"text": "X"}},
        connected_players=[],
    )
    assert sent == []
    assert len(fake_event_log.canonical) == 1  # canonical still written
