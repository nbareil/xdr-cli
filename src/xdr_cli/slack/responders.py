from __future__ import annotations

import json
from typing import Any


def format_json_block(obj: Any, max_chars: int = 3000) -> str:
    text = json.dumps(obj, indent=2, sort_keys=True, default=str)
    if len(text) > max_chars:
        text = text[: max_chars - 20] + "\n... (truncated)"
    return "```%s```" % text


def format_list_summary(items, item_label: str = "items") -> str:
    return "Found %d %s" % (len(items), item_label)
