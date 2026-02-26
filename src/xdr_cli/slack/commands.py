from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..errors import XdrValidationError


@dataclass
class ParsedCommand:
    group: str
    action: str
    positional: List[str] = field(default_factory=list)
    options: Dict[str, str] = field(default_factory=dict)
    flags: List[str] = field(default_factory=list)
    tenant: Optional[str] = None


def parse_slack_command(text: str) -> ParsedCommand:
    tokens = shlex.split(text or "")
    if len(tokens) < 2:
        raise XdrValidationError("Expected at least '<group> <action>'")

    group = tokens[0]
    action = tokens[1]
    positional = []
    options = {}
    flags = []
    i = 2
    tenant = None
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("--"):
            key = token[2:]
            if key in ("all", "raw", "excel"):
                flags.append(key)
                i += 1
                continue
            if i + 1 >= len(tokens):
                raise XdrValidationError("Missing value for --%s" % key)
            value = tokens[i + 1]
            if key == "tenant":
                tenant = value
            else:
                options[key] = value
            i += 2
            continue
        positional.append(token)
        i += 1
    return ParsedCommand(group=group, action=action, positional=positional, options=options, flags=flags, tenant=tenant)
