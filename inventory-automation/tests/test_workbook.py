from __future__ import annotations

import hashlib
import zipfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from lxml import etree

from inventory.models import CanonicalLine, WorkbookCheckout
from inventory.workbook import (
    MAIN_NS,
    NS,
    PartialRowError,
    WorkbookContract,
    XlsmLogWorkbook,
    column_to_index,
    index_to_column,
)


def inline_cell(column: str, row: int, value: str, *, style: str | None = None) -> etree._Element:
    attributes = {"r": f"{column}{row}", "t": "inlineStr"}
    if style is not None:
        attributes["s"] = style
    cell = etree.Element(f"{{{MAIN_NS}}}c", attrib=attributes)
    inline = etree.SubElement(cell, f"{{{MAIN_NS}}}is")
    text = etree.SubElement(inline, f"{{{MAIN_NS}}}t")
    text.text = value
    return cell


def number_cell(column: str, row: int, value: str, *, style: str | None = None) -> etree._Element:
    attributes = {"r": f"{column}{row}"}
    if style is not None:
        attributes["s"] = style
    cell = etree.Element(f"{{{MAIN_NS}}}c", attrib=attributes)
    etree.SubElement(cell, f"{{{MAIN_NS}}}v").text = value
    return cell


def formula_cell(column: str, row: int, formula: str) -> etree._Element:
    cell = etree.Element(f"{{{MAIN_NS}}}c", attrib={"r": f"{column}{row}", "s": "1"})
    etree.SubElement(cell, f"{{{MAIN_NS}}}f").text = formula
    etree.SubElement(cell, f"{{{MAIN_NS}}}v").text = ""
    return cell


def build_xlsm(path: Path, *, partial_row: bool = False) -> None:
    worksheet = etree.Element(f"{{{MAIN_NS}}}worksheet", nsmap={None: MAIN_NS})
    etree.SubElement(worksheet, f"{{{MAIN_NS}}}dimension", ref="A1:HG9956")
    sheet_data = etree.SubElement(worksheet, f"{{{MAIN_NS}}}sheetData")

    row3 = etree.SubElement(sheet_data, f"{{{MAIN_NS}}}row", r="3")
    metadata = {
        "A": "Reconciled?",
        "B": "Note",
        "C": "Timestamp",
        "D": "Checkout By",
        "E": "Received By",
        "F": "Quote",
        "G": "Name",
        "H": "Address",
        "I": "Purpose",
    }
    for column, value in metadata.items():
        row3.append(inline_cell(column, 3, value))
    sku_names = {
        "J": "SKU-SWITCH-1G",
        "K": "SKU-HUB-ZB",
        "L": "SKU-LED-CCT",
        "M": "SKU-IR-RF",
    }
    for index in range(column_to_index("J"), column_to_index("HF") + 1):
        column = index_to_column(index)
        row3.append(inline_cell(column, 3, sku_names.get(column, f"SKU-{column}")))
    row3.append(inline_cell("HG", 3, "Status"))

    row4 = etree.SubElement(sheet_data, f"{{{MAIN_NS}}}row", r="4")
    row4.append(number_cell("C", 4, "45000", style="2"))
    row4.append(inline_cell("D", 4, "Existing", style="1"))
    row4.append(formula_cell("HG", 4, 'IF(C4<>"","Logged","")'))

    row5 = etree.SubElement(sheet_data, f"{{{MAIN_NS}}}row", r="5")
    if partial_row:
        row5.append(inline_cell("G", 5, "Orphan data"))
    row5.append(formula_cell("HG", 5, 'IF(C5<>"","Logged","")'))

    workbook = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{MAIN_NS}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <workbookPr date1904="0"/>
  <sheets><sheet name="Log" sheetId="1" r:id="rId1"/></sheets>
</workbook>'''.encode()
    rels = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>'''
    root_rels = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''
    content_types = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.ms-excel.sheet.macroEnabled.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>'''

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            etree.tostring(worksheet, xml_declaration=True, encoding="UTF-8"),
        )
        archive.writestr("xl/vbaProject.bin", b"FAKE-VBA-PROJECT-BYTES")
        archive.writestr("docProps/custom.xml", b"<custom>unchanged</custom>")


def member_hashes(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        return {
            name: hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist()
        }


def checkout(request_id: str = "inv_test") -> WorkbookCheckout:
    return WorkbookCheckout(
        request_id=request_id,
        timestamp=datetime(2026, 9, 1, 9, 30),
        checkout_by="Alex",
        received_by="Sam",
        quote="Q-1001",
        name="Demo Home",
        address="10 Example Road",
        purpose="Install",
        note="Approved Telegram checkout",
        lines=(
            CanonicalLine("SKU-SWITCH-1G", Decimal("2")),
            CanonicalLine("SKU-HUB-ZB", Decimal("-1")),
        ),
    )


def test_patch_preserves_macros_and_non_target_members(tmp_path) -> None:
    source = tmp_path / "Inventory2.xlsm"
    output = tmp_path / "patched.xlsm"
    build_xlsm(source)
    before = member_hashes(source)

    workbook = XlsmLogWorkbook(source, WorkbookContract(), require_vba_project=True)
    inspection = workbook.inspect()
    assert inspection.first_empty_row == 5
    assert len(inspection.sku_headers) == 205

    result = workbook.create_patched_copy(output, checkout())
    after = member_hashes(output)

    assert result.row == 5
    assert before.keys() == after.keys()
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {"xl/worksheets/sheet1.xml"}
    assert before["xl/vbaProject.bin"] == after["xl/vbaProject.bin"]

    reopened = XlsmLogWorkbook(output, WorkbookContract(), require_vba_project=True)
    assert reopened.inspect().first_empty_row == 6
    assert reopened.find_request_marker("inv_test") == 5

    with zipfile.ZipFile(output) as archive:
        root = etree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    row = root.xpath('.//m:sheetData/m:row[@r="5"]', namespaces=NS)[0]
    cells = {cell.get("r"): cell for cell in row.findall(f"{{{MAIN_NS}}}c")}
    assert cells["A5"].find(f"{{{MAIN_NS}}}v") is None
    assert "INVREQ:inv_test" in "".join(cells["B5"].xpath("./m:is//m:t/text()", namespaces=NS))
    assert Decimal(cells["J5"].findtext(f"{{{MAIN_NS}}}v")) == Decimal("2")
    assert Decimal(cells["K5"].findtext(f"{{{MAIN_NS}}}v")) == Decimal("-1")
    assert cells["HG5"].findtext(f"{{{MAIN_NS}}}f") == 'IF(C5<>"","Logged","")'
    assert cells["HG5"].find(f"{{{MAIN_NS}}}v") is None


def test_duplicate_request_returns_existing_row_without_new_file(tmp_path) -> None:
    source = tmp_path / "Inventory2.xlsm"
    once = tmp_path / "once.xlsm"
    twice = tmp_path / "twice.xlsm"
    build_xlsm(source)
    XlsmLogWorkbook(source, WorkbookContract()).create_patched_copy(once, checkout())

    result = XlsmLogWorkbook(once, WorkbookContract()).create_patched_copy(twice, checkout())
    assert result.duplicate
    assert result.row == 5
    assert not twice.exists()


def test_partial_blank_timestamp_row_is_blocked(tmp_path) -> None:
    source = tmp_path / "Inventory2.xlsm"
    build_xlsm(source, partial_row=True)
    workbook = XlsmLogWorkbook(source, WorkbookContract())
    with pytest.raises(PartialRowError):
        workbook.inspect()
