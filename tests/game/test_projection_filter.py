from sidequest.game.projection.envelope import MessageEnvelope
from sidequest.game.projection.view import SessionGameStateView
from sidequest.game.projection_filter import (
    FilterDecision,
    PassThroughFilter,
    ProjectionFilter,
)


def _env(kind: str = "NARRATION", payload: str = '{"text":"hi"}', seq: int = 1) -> MessageEnvelope:
    return MessageEnvelope(kind=kind, payload_json=payload, origin_seq=seq)


def _view() -> SessionGameStateView:
    return SessionGameStateView(gm_player_id="gm", player_id_to_character={"alice": "alice_char"})


def test_pass_through_includes_everything_for_everyone():
    f = PassThroughFilter()
    dec = f.project(envelope=_env(), view=_view(), player_id="alice")
    assert dec.include is True
    assert dec.payload_json == '{"text":"hi"}'


def test_filter_protocol_allows_redaction():
    class RedactHP:
        def project(self, *, envelope: MessageEnvelope, view, player_id):
            if envelope.kind == "STATE_UPDATE" and player_id != "gm":
                return FilterDecision(include=True, payload_json="{}")
            return FilterDecision(include=True, payload_json=envelope.payload_json)

    f: ProjectionFilter = RedactHP()
    dec = f.project(
        envelope=_env(kind="STATE_UPDATE", payload='{"hp":10}'), view=_view(), player_id="alice"
    )
    assert dec.payload_json == "{}"
    dec_gm = f.project(
        envelope=_env(kind="STATE_UPDATE", payload='{"hp":10}'), view=_view(), player_id="gm"
    )
    assert dec_gm.payload_json == '{"hp":10}'


def test_filter_can_omit():
    class OmitSecrets:
        def project(self, *, envelope: MessageEnvelope, view, player_id):
            if envelope.kind == "SECRET_NOTE" and player_id != "alice":
                return FilterDecision(include=False, payload_json="")
            return FilterDecision(include=True, payload_json=envelope.payload_json)

    f: ProjectionFilter = OmitSecrets()
    assert (
        f.project(envelope=_env(kind="SECRET_NOTE"), view=_view(), player_id="bob").include is False
    )
    assert (
        f.project(envelope=_env(kind="SECRET_NOTE"), view=_view(), player_id="alice").include
        is True
    )
