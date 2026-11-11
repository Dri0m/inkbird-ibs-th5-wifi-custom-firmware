"""The fallback screen's message (protocol.md 5.5) from how far the CBU got."""
from tools.th5link import proto as P
from tools.th5link.mcumodel import (STATUS_HA_NO_ANSWER, STATUS_HA_UNREACH, STATUS_LINK_ERROR,
                              STATUS_WIFI, McuModel)


def status_after(net_state, result=P.RES_NET_FAIL):
    m = McuModel()
    m.start_session(0.0, P.TRIG_BUTTON)
    if net_state is not None:
        m.net = P.Net(net_state)
    m.end_session(1.0, result)
    return m.status


def test_net_states_map_to_messages():
    assert status_after(None) == STATUS_WIFI
    assert status_after(P.NET_WIFI_CONNECTING) == STATUS_WIFI
    assert status_after(P.NET_FAIL_WIFI) == STATUS_WIFI
    assert status_after(P.NET_FAIL_NO_CREDENTIALS) == STATUS_WIFI
    assert status_after(P.NET_WIFI_UP) == STATUS_HA_UNREACH
    assert status_after(P.NET_FAIL_API) == STATUS_HA_UNREACH
    assert status_after(P.NET_API_UP) == STATUS_HA_NO_ANSWER
    assert status_after(P.NET_FAIL_NO_HA) == STATUS_HA_NO_ANSWER
    assert status_after(P.NET_API_UP, P.RES_LINK) == STATUS_LINK_ERROR
    assert status_after(None, P.RES_NO_HELLO) == STATUS_LINK_ERROR
