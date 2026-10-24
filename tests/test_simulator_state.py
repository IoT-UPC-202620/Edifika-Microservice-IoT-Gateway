from simulator.node_state import NodeState


def test_mode_reserved_and_rgb_updates_state():
    state = NodeState()
    assert state.mode == "FREE_ACCESS"
    assert state.led == "status"

    ack1 = state.apply({"type": "mode", "params": {"mode": "RESERVED"}})
    assert ack1.status == "OK"
    assert state.mode == "RESERVED"

    ack2 = state.apply({"type": "rgb", "params": {"color": "#00FF88"}})
    assert ack2.status == "OK"
    assert (state.mode, state.led) == ("RESERVED", "#00FF88")


def test_mode_free_access_resets_led():
    state = NodeState(mode="RESERVED", led="#00FF88")
    ack = state.apply({"type": "mode", "params": {"mode": "FREE_ACCESS"}})
    assert ack.status == "OK"
    assert state.mode == "FREE_ACCESS"
    assert state.led == "status"


def test_rgb_in_free_access_fails_with_detail():
    state = NodeState(mode="FREE_ACCESS")
    ack = state.apply({"type": "rgb", "params": {"color": "#00FF88"}})
    assert ack.status == "FAILED"
    assert ack.detail == "not reserved"
    assert state.led == "status"


def test_other_commands_ack_ok():
    state = NodeState()
    for cmd_type in ("unlock", "display", "buzzer", "time_sync", "maintenance", "access_result"):
        ack = state.apply({"type": cmd_type, "params": {}})
        assert ack.status == "OK"
