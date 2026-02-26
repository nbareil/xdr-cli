from __future__ import annotations

from typing import Dict, Iterable, List

import xlsxwriter


def write_xql_results_excel(results: Iterable[Dict], output_path: str) -> None:
    iterator = iter(results)
    try:
        first = next(iterator)
    except StopIteration:
        workbook = xlsxwriter.Workbook(output_path)
        workbook.add_worksheet()
        workbook.close()
        return

    columns = list(first.keys())
    workbook = xlsxwriter.Workbook(output_path)
    worksheet = workbook.add_worksheet()
    for i, col in enumerate(columns):
        worksheet.write(0, i, col)
        worksheet.write(1, i, first.get(col, ""))

    row = 2
    for item in iterator:
        for i, col in enumerate(columns):
            worksheet.write(row, i, item.get(col, ""))
        row += 1
    workbook.close()
