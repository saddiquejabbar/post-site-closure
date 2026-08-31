from __future__ import annotations

import hashlib
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from safe_xlsm import MAIN, LayoutError, Mapping, SafeXlsmWriter


def cell(ref: str, value: str, style: str | None = None) -> str:
    style_attr = f' s="{style}"' if style else ""
    return f'<c r="{ref}" t="inlineStr"{style_attr}><is><t>{value}</t></is></c>'


def demo_xlsm(path: Path, wrong_header: bool = False) -> bytes:
    customer = "Wrong" if wrong_header else "Customer"
    sheet = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="{MAIN}">
  <dimension ref="A1:D2"/>
  <sheetData>
    <row r="1">{cell("A1", "Timestamp")}{cell("B1", customer)}{cell("C1", "SKU")}{cell("D1", "Automation Row ID")}</row>
    <row r="2">{cell("A2", "2026-01-01", "1")}{cell("B2", "Example", "1")}{cell("C2", "OLD-SKU", "1")}{cell("D2", "MANUAL-ROW", "1")}</row>
  </sheetData>
</worksheet>'''.encode()
    members = {
        "[Content_Types].xml": b'''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.ms-excel.sheet.macroEnabled.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/vbaProject.bin" ContentType="application/vnd.ms-office.vbaProject"/>
</Types>''',
        "_rels/.rels": b'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>''',
        "xl/workbook.xml": b'''<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Checkout Log" sheetId="1" r:id="rId1"/></sheets></workbook>''',
        "xl/_rels/workbook.xml.rels": b'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.microsoft.com/office/2006/relationships/vbaProject" Target="vbaProject.bin"/>
</Relationships>''',
        "xl/worksheets/sheet1.xml": sheet,
        "xl/vbaProject.bin": b"FAKE-VBA-MUST-NOT-CHANGE\x00\x01",
        "xl/other.bin": b"OTHER-BINARY-MUST-NOT-CHANGE\x02\x03",
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as book:
        for name, payload in members.items():
            book.writestr(name, payload)
    return members["xl/vbaProject.bin"]


def mapping() -> Mapping:
    return Mapping(
        "Checkout Log",
        1,
        {"timestamp": "A", "customer": "B", "sku": "C", "row_id": "D"},
        {"A": "Timestamp", "B": "Customer", "C": "SKU", "D": "Automation Row ID"},
    )


def inline_text(element: ET.Element) -> str:
    return "".join(node.text or "" for node in element.iter(f"{{{MAIN}}}t"))


def test_safe_writer_preserves_vba_and_replays_idempotently(tmp_path: Path) -> None:
    workbook = tmp_path / "Inventory2.xlsm"
    original_vba = demo_xlsm(workbook)
    with zipfile.ZipFile(workbook) as before:
        other_hash = hashlib.sha256(before.read("xl/other.bin")).hexdigest()
    writer = SafeXlsmWriter(workbook, mapping(), tmp_path / "backups")
    row = {"timestamp": "2026-09-01T08:00:00+08:00", "customer": "Tan", "sku": "HUB-1", "row_id": "INV-ABCDEF12:01"}

    first = writer.append("INV-ABCDEF12", [row])
    second = writer.append("INV-ABCDEF12", [row])

    assert first.rows_appended == 1
    assert first.first_row == 3
    assert second.rows_appended == 0 and second.duplicate_replay
    assert second.output_sha256 == first.output_sha256
    assert Path(first.backup_path or "").exists()
    with zipfile.ZipFile(workbook) as after:
        assert after.read("xl/vbaProject.bin") == original_vba
        assert hashlib.sha256(after.read("xl/other.bin")).hexdigest() == other_hash
        root = ET.fromstring(after.read("xl/worksheets/sheet1.xml"))
        rows = root.find(f"{{{MAIN}}}sheetData").findall(f"{{{MAIN}}}row")
        values = {item.get("r"): inline_text(item) for item in rows[-1]}
        assert values["B3"] == "Tan"
        assert values["D3"] == "INV-ABCDEF12:01"
        assert rows[-1].find(f"{{{MAIN}}}c").get("s") == "1"


def test_header_mismatch_never_replaces_live_file(tmp_path: Path) -> None:
    workbook = tmp_path / "Inventory2.xlsm"
    demo_xlsm(workbook, wrong_header=True)
    before = hashlib.sha256(workbook.read_bytes()).hexdigest()
    writer = SafeXlsmWriter(workbook, mapping(), tmp_path / "backups")
    row = {"timestamp": "2026-09-01", "customer": "Tan", "sku": "HUB-1", "row_id": "INV-ABCDEF12:01"}
    try:
        writer.append("INV-ABCDEF12", [row])
    except LayoutError:
        pass
    else:
        raise AssertionError("expected a layout error")
    assert hashlib.sha256(workbook.read_bytes()).hexdigest() == before
