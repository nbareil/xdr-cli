from xdr_cli.slack.responders import (
    SlackMessage,
    format_endpoints_compact,
    format_json_block,
    format_list_summary,
)


def test_format_list_summary():
    assert format_list_summary([1, 2, 3], "alerts") == "Found 3 alerts"


def test_format_json_block_truncates():
    block = format_json_block({"k": "v" * 5000}, max_chars=100)
    assert block.startswith("```") and block.endswith("```")
    assert "truncated" in block
    assert len(block) < 200


def test_format_endpoints_compact_empty_returns_string():
    out = format_endpoints_compact([], title="Hosts")
    assert out == "Hosts: none found"


def test_format_endpoints_compact_builds_table_message():
    items = [
        {"endpoint_name": "h1", "endpoint_status": "CONNECTED", "ip": ["10.0.0.1"], "is_isolated": "AGENT_ISOLATED"},
    ]
    msg = format_endpoints_compact(items, title="Hosts")
    assert isinstance(msg, SlackMessage)
    assert "Hosts (1)" in msg.text
    table = msg.attachments[0]["blocks"][0]
    assert table["type"] == "table"
    header = [c["text"] for c in table["rows"][0]]
    assert header == ["Host", "OS", "IPs", "Status", "Last Seen", "Isolated"]
    data_row = [c["text"] for c in table["rows"][1]]
    assert data_row[0] == "h1"
    assert "🔒" in data_row[5]  # isolated marker


def test_format_endpoints_compact_sorts_connected_first():
    items = [
        {"endpoint_name": "lost-host", "endpoint_status": "DISCONNECTED", "last_seen": 1000},
        {"endpoint_name": "live-host", "endpoint_status": "CONNECTED", "last_seen": 2000},
    ]
    msg = format_endpoints_compact(items)
    hostnames = [row[0]["text"] for row in msg.attachments[0]["blocks"][0]["rows"][1:]]
    assert hostnames == ["live-host", "lost-host"]


def test_format_endpoints_compact_sorts_recent_first_within_status():
    items = [
        {"endpoint_name": "old", "endpoint_status": "CONNECTED", "last_seen": 1000},
        {"endpoint_name": "new", "endpoint_status": "CONNECTED", "last_seen": 9000},
    ]
    msg = format_endpoints_compact(items)
    hostnames = [row[0]["text"] for row in msg.attachments[0]["blocks"][0]["rows"][1:]]
    assert hostnames == ["new", "old"]
