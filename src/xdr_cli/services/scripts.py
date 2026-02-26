from __future__ import annotations

import base64
import json
from typing import Dict, List, Optional

from ..errors import XdrValidationError
from .base import BaseService


_PY_YARA_BASE_SCRIPT = r'''
import base64
import json
import logging
import os

import yara
import psutil

yara_rules = base64.b64decode('XXX_XDR_CLI_INSERT_YARA_CONTENT_XXX').decode("utf-8")

# Do not edit past this line
# ------------------------------------------------------------------

logger = logging.getLogger("xdr_yara_process_scan")
logging.basicConfig(level=logging.ERROR)

def is_expected_yara_error(err: yara.Error):
    for arg in err.args:
        if "access denied" in arg:
            return True
        elif "internal error: 47" in arg:
            return True
    return False

class BytesEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, bytes):
            return base64.b64encode(o).decode("ascii")
        return super().default(o)

allow_list_exe = [
    "C:\\Program Files\\Palo Alto Networks\\Traps\\tlaworker.exe",
    "C:\\Program Files\\Palo Alto Networks\\Traps\\cysandbox.exe",
    "C:\\Program Files (x86)\\ossec-agent\\wazuh-agent.exe",
]

def yara_process_scan():
    rules = yara.compile(source=yara_rules)
    for process in psutil.process_iter():
        if process.pid == os.getpid():
            continue
        try:
            matches = rules.match(pid=process.pid)
        except yara.Error:
            continue
        if not matches:
            continue
        if process.exe() in allow_list_exe:
            continue
        obj = {
            "process": {
                "name": process.name(),
                "pid": process.pid,
                "ppid": process.ppid(),
                "cmdline": process.cmdline(),
                "exe": process.exe(),
            }
        }
        for m in matches:
            obj[m.rule] = {}
            for (offset, str_id, matched_data) in m.strings:
                obj[m.rule][str_id] = matched_data
        print(json.dumps(obj, cls=BytesEncoder))

if __name__ == "__main__":
    yara_process_scan()
'''


def build_py_script_from_yara_bytes(yara_content: bytes) -> str:
    blob = base64.b64encode(yara_content).decode("utf-8")
    return _PY_YARA_BASE_SCRIPT.replace("XXX_XDR_CLI_INSERT_YARA_CONTENT_XXX", blob)


class ScriptsService(BaseService):
    async def run_snippet(self, script_content: str, endpoint_ids: List[str], timeout: int = 600, name: Optional[str] = None) -> Dict:
        if not endpoint_ids:
            raise XdrValidationError("endpoint_ids must not be empty")
        params = {
            "request_data": {
                "timeout": timeout,
                "snippet_code": script_content,
                "filters": [
                    {"field": "endpoint_id_list", "operator": "in", "value": list(endpoint_ids)}
                ],
            }
        }
        obj = await self.transport.post_json("scripts/run_snippet_code_script", params)
        return obj.get("reply", {})

    async def execution_results(self, action_id: int) -> Dict:
        obj = await self.transport.post_json("scripts/get_script_execution_results", {"request_data": {"action_id": action_id}})
        return obj.get("reply", {})

    async def execution_status(self, action_id: int) -> Dict:
        obj = await self.transport.post_json("scripts/get_script_execution_status", {"request_data": {"action_id": action_id}})
        return obj.get("reply", {})

    async def result_files(self, action_id: int, endpoint_id: str) -> Dict:
        obj = await self.transport.post_json(
            "scripts/get_script_execution_results_files",
            {"request_data": {"action_id": action_id, "endpoint_id": endpoint_id}},
        )
        return obj.get("reply", {})

    async def yara_memory_scan(self, yara_ruleset_bytes: bytes, endpoint_ids: Optional[List[str]] = None, all_platforms: Optional[List[str]] = None, timeout: int = 600, batch_size: int = 1000) -> Dict:
        endpoint_ids = list(endpoint_ids or [])
        all_platforms = list(all_platforms or [])
        if endpoint_ids and all_platforms:
            raise XdrValidationError("endpoint_ids and all_platforms are mutually exclusive")
        if not endpoint_ids and not all_platforms:
            raise XdrValidationError("Provide endpoint_ids or all_platforms")
        if all_platforms:
            endpoint_ids = []
            for platform in all_platforms:
                endpoints = await self.client.endpoints.list(platform=platform)
                endpoint_ids.extend([e.get("endpoint_id") for e in endpoints if e.get("endpoint_id")])
        script_content = build_py_script_from_yara_bytes(yara_ruleset_bytes)
        actions = []
        for index in range(0, len(endpoint_ids), batch_size):
            batch = endpoint_ids[index:index + batch_size]
            reply = await self.run_snippet(script_content=script_content, endpoint_ids=batch, timeout=timeout, name="yara_memory_scan")
            actions.append(reply.get("action_id"))
        return {"actions_id": actions}

    @staticmethod
    def parse_json_lines(lines: List[str]) -> List[Dict]:
        out = []
        for line in lines:
            out.append(json.loads(line))
        return out
