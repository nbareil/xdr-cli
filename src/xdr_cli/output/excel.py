from __future__ import annotations

import json
from typing import Any, Dict, Iterable

import xlsxwriter


def _cell_value(value: Any) -> Any:
    """Coerce a value into something xlsxwriter can write to a cell.

    XQL rows routinely contain nested objects and arrays; xlsxwriter only
    accepts scalars, so anything else is JSON-encoded into a single cell.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)):
        return value
    return json.dumps(value, default=str, ensure_ascii=False)


def write_xql_results_excel(results: Iterable[Dict], output_path: str) -> None:
    rows = list(results)
    workbook = xlsxwriter.Workbook(output_path)
    worksheet = workbook.add_worksheet()

    if not rows:
        workbook.close()
        return

    # Union of keys across every row, preserving first-seen order, so columns
    # introduced by later rows are not silently dropped.
    columns = []
    seen = set()
    for item in rows:
        for key in item.keys():
            if key not in seen:
                seen.add(key)
                columns.append(key)

    for i, col in enumerate(columns):
        worksheet.write(0, i, col)

    for r, item in enumerate(rows, start=1):
        for i, col in enumerate(columns):
            worksheet.write(r, i, _cell_value(item.get(col, "")))

    workbook.close()
