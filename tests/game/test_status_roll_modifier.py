from sidequest.game.status import Status, StatusSeverity, status_roll_modifier


def _status(text, mod):
    return Status(text=text, severity=StatusSeverity.Wound, roll_modifier=mod)


def test_status_defaults_to_zero_modifier():
    s = Status(text="bruised", severity=StatusSeverity.Scratch)
    assert s.roll_modifier == 0


def test_status_roll_modifier_sums_active_statuses():
    class _Core:
        statuses = [_status("in the dark", -2), _status("blessed", 1)]

    assert status_roll_modifier(_Core()) == -1


def test_status_roll_modifier_none_and_empty():
    assert status_roll_modifier(None) == 0

    class _Empty:
        statuses = []

    assert status_roll_modifier(_Empty()) == 0
