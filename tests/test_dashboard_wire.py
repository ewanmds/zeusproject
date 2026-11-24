"""REST and WebSocket payloads must not leak internal session bookkeeping."""
import json

from zeus.dashboard import DashboardBroker

_INTERNAL = ("cloids", "owned_oids", "pending_cloids", "funds_root", "stats_file",
             "stop_file", "safety_file", "account_kind", "account", "symbols")


def _row():
    row = {field: field + "-value" for field in _INTERNAL}
    row.update(session_id="s1", market="#101", running=True, cleanup_confirmed=False,
               selling_phase=None, phase="running", started=1, volume_usd=10.0,
               net_pnl=-0.1, fees=0.01, fills=2)
    return row


def _broker():
    def source():
        return {"bots": [_row()], "display_bots": [_row()], "active_metrics": {}}
    return DashboardBroker({"outcome": source}, ttl=0)


def test_rest_read_strips_internal_session_fields():
    data = _broker().read("outcome")
    for row in data["bots"] + data["display_bots"]:
        for field in _INTERNAL:
            assert field not in row
        assert row["session_id"] == "s1"
        assert row["market"] == "#101"


def test_websocket_payload_drops_internal_fields_and_legacy_bots():
    payload = json.loads(_broker().payload("outcome"))
    assert "bots" not in payload
    for row in payload["display_bots"]:
        for field in _INTERNAL:
            assert field not in row
        assert row["session_id"] == "s1"
