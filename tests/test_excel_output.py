import tempfile
import zipfile
import xml.etree.ElementTree as ET

from xdr_cli.output.excel import write_xql_results_excel


NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _read_sheet_cells(path):
    with zipfile.ZipFile(path) as zf:
        shared = []
        if 'xl/sharedStrings.xml' in zf.namelist():
            root = ET.fromstring(zf.read('xl/sharedStrings.xml'))
            for si in root.findall('s:si', NS):
                texts = [t.text or '' for t in si.findall('.//s:t', NS)]
                shared.append(''.join(texts))
        sheet = ET.fromstring(zf.read('xl/worksheets/sheet1.xml'))
        cells = {}
        for cell in sheet.findall('.//s:c', NS):
            ref = cell.attrib['r']
            value_node = cell.find('s:v', NS)
            if value_node is None:
                continue
            value = value_node.text
            if cell.attrib.get('t') == 's':
                value = shared[int(value)]
            cells[ref] = value
        return cells


def test_write_xql_results_excel_keeps_first_row():
    rows = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    with tempfile.NamedTemporaryFile(suffix='.xlsx') as tmp:
        write_xql_results_excel(rows, tmp.name)
        cells = _read_sheet_cells(tmp.name)
    assert cells['A1'] == 'a'
    assert cells['A2'] == '1'
    assert cells['A3'] == '2'
