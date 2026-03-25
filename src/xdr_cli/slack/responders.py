from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Union


@dataclass
class SlackMessage:
    text: str
    attachments: list


def format_json_block(obj: Any, max_chars: int = 3000) -> str:
    text = json.dumps(obj, indent=2, sort_keys=True, default=str)
    if len(text) > max_chars:
        text = text[: max_chars - 20] + "\n... (truncated)"
    return "```%s```" % text


def format_list_summary(items, item_label: str = "items") -> str:
    return "Found %d %s" % (len(items), item_label)


def _iso_from_epoch_ms(value: Any) -> str:
    if value in (None, "", 0):
        return "N/A"
    try:
        ts_ms = int(value)
    except (TypeError, ValueError):
        return str(value)
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).isoformat()


def _humanize_epoch_ms(value: Any) -> str:
    if value in (None, "", 0):
        return "N/A"
    try:
        ts_ms = int(value)
    except (TypeError, ValueError):
        return str(value)
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - dt
    if delta.total_seconds() < 0:
        return dt.isoformat()
    days = delta.days
    if days == 0:
        return "Today"
    if days == 1:
        return "1 day ago"
    if days < 30:
        return f"{days} days ago"
    return dt.strftime("%Y-%m-%d")


def format_endpoints_compact(items, title: str = "Endpoints") -> Union[str, SlackMessage]:
    items = list(items or [])
    if not items:
        return f"{title}: none found"

    rows = []
    for item in items:
        hostname = item.get("endpoint_name") or item.get("hostname") or "N/A"
        os_name = item.get("operating_system") or item.get("os_version") or "N/A"
        ips = item.get("ip") or []
        if not isinstance(ips, list):
            ips = [str(ips)]
        status = item.get("endpoint_status") or "N/A"
        status_prefix = ""
        if status == "CONNECTED":
            status_prefix = "🟢 "
        elif status in {"LOST", "DISCONNECTED"}:
            status_prefix = "⚠️ "
        elif status != "N/A":
            status_prefix = "ℹ️ "
        isolation_raw = str(item.get("is_isolated") or "")
        isolated = isolation_raw == "AGENT_ISOLATED"
        isolation_label = "🔒" if isolated else "-"
        last_seen = _humanize_epoch_ms(item.get("last_seen"))
        rows.append(
            {
                "hostname": str(hostname),
                "os": str(os_name),
                "ips": ", ".join(map(str, ips)) or "N/A",
                "status": f"{status_prefix}{status}",
                "_status_raw": str(status),
                "iso": isolation_label,
                "last_seen": last_seen,
                "_last_seen_raw": int(item.get("last_seen") or 0) if str(item.get("last_seen") or "").isdigit() else 0,
            }
        )

    def _status_rank(status: str) -> int:
        if status == "CONNECTED":
            return 0
        if status in {"LOST", "DISCONNECTED"}:
            return 2
        return 1

    rows.sort(
        key=lambda r: (
            _status_rank(r.get("_status_raw", "")),
            -int(r.get("_last_seen_raw", 0)),
            r.get("hostname", ""),
        )
    )

    columns = [
        ("Host", "hostname"),
        ("OS", "os"),
        ("IPs", "ips"),
        ("Status", "status"),
        ("Last Seen", "last_seen"),
        ("Isolated", "iso"),
    ]

    table_rows = [
        [{"type": "raw_text", "text": label} for label, _ in columns]
    ]
    for row in rows[:99]:
        table_rows.append(
            [{"type": "raw_text", "text": str(row[key])} for _, key in columns]
        )

    shown = min(len(rows), 99)
    title_text = f"{title} ({len(rows)})"
    if shown < len(rows):
        title_text += f" — showing first {shown}"

    return SlackMessage(
        text=title_text,
        attachments=[
            {
                "fallback": title_text,
                "blocks": [
                    {
                        "type": "table",
                        "column_settings": [
                            {"is_wrapped": True},
                            {"is_wrapped": True},
                            {"is_wrapped": True},
                            {"is_wrapped": True},
                            {"is_wrapped": True},
                            {"align": "center"},
                        ],
                        "rows": table_rows,
                    }
                ]
            }
        ],
    )
