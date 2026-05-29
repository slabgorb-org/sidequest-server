from sidequest.telemetry.spans import (
    table_accuse_span,
    table_cheat_span,
    table_commit_span,
    table_dealt_span,
    table_fold_span,
    table_npc_commit_span,
    table_read_span,
    table_showdown_span,
)
from sidequest.telemetry.spans._core import SPAN_ROUTES


def test_all_table_spans_registered_in_routes():
    for name in (
        "table.dealt",
        "table.commit",
        "table.npc_commit",
        "table.cheat",
        "table.read",
        "table.accuse",
        "table.fold",
        "table.showdown",
    ):
        assert name in SPAN_ROUTES, f"{name} missing from SPAN_ROUTES"
        assert SPAN_ROUTES[name].component == "table"


def test_dealt_span_opens_and_routes():
    with table_dealt_span(seat_count=3, game_kind="poker", stake_kind="money"):
        pass
    route = SPAN_ROUTES["table.dealt"]
    # extract() must read attributes without raising even on a bare span shape
    extracted = route.extract(
        type("S", (), {"attributes": {"seat_count": 3, "game_kind": "poker"}})()
    )
    assert extracted["seat_count"] == 3
    assert extracted["op"] == "dealt"


def test_showdown_span_carries_winner():
    with table_showdown_span(winner="seat_1", forfeits=["seat_2"], pot_awarded="seat_1"):
        pass
    assert SPAN_ROUTES["table.showdown"].component == "table"


def test_accuse_extract_reads_attributes():
    span = type(
        "S",
        (),
        {
            "attributes": {
                "accuser": "seat_1",
                "target": "seat_2",
                "accuser_total": 18,
                "dc": 14,
                "landed": True,
            }
        },
    )()
    extracted = SPAN_ROUTES["table.accuse"].extract(span)
    assert extracted["landed"] is True
    assert extracted["accuser_total"] == 18
    assert extracted["op"] == "accuse"


def test_showdown_extract_reads_revealed_strengths():
    span = type(
        "S",
        (),
        {
            "attributes": {
                "winner": "seat_1",
                "forfeits": "seat_2",
                "pot_awarded": "seat_1",
                "revealed_strengths": "seat_1:pair,seat_2:high",
            }
        },
    )()
    extracted = SPAN_ROUTES["table.showdown"].extract(span)
    assert extracted["winner"] == "seat_1"
    assert extracted["forfeits"] == "seat_2"
    assert extracted["revealed_strengths"] == "seat_1:pair,seat_2:high"
    assert extracted["op"] == "showdown"


def test_commit_and_npc_commit_spans_open():
    with table_commit_span(seat="seat_1", beat_id="raise", amount=2, decision_point=0):
        pass
    with table_npc_commit_span(seat="seat_2", strength_band="strong", pot=4, chosen_beat="call"):
        pass


def test_cheat_read_accuse_spans_open():
    with table_cheat_span(seat="seat_1", strength_before=12, strength_after=20, new_trace=0.4):
        pass
    with table_read_span(reader="seat_1", target="seat_2", info_returned="strength_band=weak"):
        pass
    with table_accuse_span(accuser="seat_1", target="seat_2", accuser_total=18, dc=14, landed=True):
        pass


def test_fold_span_opens():
    with table_fold_span(seat="seat_3", decision_point=1):
        pass
