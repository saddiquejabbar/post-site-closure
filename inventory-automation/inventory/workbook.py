from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import tempfile
import time
import zipfile
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import BinaryIO

from lxml import etree

from .models import WorkbookCheckout, WorkbookWriteResult

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
XML_NS = "http://www.w3.org/XML/1998/namespace"
NS = {"m": MAIN_NS, "r": DOC_REL_NS, "pr": PKG_REL_NS}
CELL_REF_RE = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*)$")
FORMULA_REF_RE = re.compile(
    r"(?<![A-Z0-9_])(?P<col>\$?[A-Z]{1,3})(?P<row_abs>\$?)(?P<row>[1-9][0-9]*)(?![A-Z0-9_]|\s*\()"
)
INVALID_XML_CHARS_RE = re.compile("[\x00-\x08\x0B\x0C\x0E-\x1F\uD800-\uDFFF\uFFFE\uFFFF]")


class WorkbookError(RuntimeError):
    pass


class WorkbookContractError(WorkbookError):
    pass


class WorkbookBusyError(WorkbookError):
    pass


class ConcurrentEditError(WorkbookError):
    pass


class PartialRowError(WorkbookError):
    pass


class VerificationError(WorkbookError):
    pass


@dataclass(slots=True, frozen=True)
class WorkbookContract:
    sheet_name: str = "Log"
    header_row: int = 3
    data_start_row: int = 4
    data_end_row: int = 9956
    sku_start_column: str = "J"
    sku_end_column: str = "HF"
    status_column: str = "HG"

    @property
    def metadata_headers(self) -> dict[str, str]:
        return {
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


@dataclass(slots=True, frozen=True)
class WorkbookInspection:
    sheet_path: str
    sku_headers: tuple[str, ...]
    first_empty_row: int
    date_1904: bool
    status_formula_template_row: int
    vba_members: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class _CellValue:
    text: str | None
    formula: str | None


@dataclass(slots=True, frozen=True)
class _PatchedSheet:
    data: bytes
    row: int
    duplicate: bool


def column_to_index(column: str) -> int:
    value = 0
    for character in column.upper():
        if not "A" <= character <= "Z":
            raise ValueError(f"Invalid Excel column: {column}")
        value = value * 26 + ord(character) - 64
    return value


def index_to_column(index: int) -> str:
    if index < 1:
        raise ValueError("Excel columns are 1-based")
    output: list[str] = []
    while index:
        index, remainder = divmod(index - 1, 26)
        output.append(chr(65 + remainder))
    return "".join(reversed(output))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_stream(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _sanitize_text(value: str) -> str:
    return INVALID_XML_CHARS_RE.sub("", value).strip()


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise WorkbookContractError("Quantity must be finite")
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _excel_serial(value: datetime, *, date_1904: bool) -> Decimal:
    if value.tzinfo is not None:
        value = value.replace(tzinfo=None)
    base = datetime(1904, 1, 1) if date_1904 else datetime(1899, 12, 30)
    delta = value - base
    seconds = Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / Decimal(1_000_000)
    return seconds / Decimal(86400)


def translate_formula_rows(formula: str, row_delta: int) -> str:
    """Translate relative A1 row references without touching quoted strings."""
    pieces: list[str] = []
    current: list[str] = []
    inside_string = False
    index = 0
    while index < len(formula):
        character = formula[index]
        if character == '"':
            if inside_string and index + 1 < len(formula) and formula[index + 1] == '"':
                current.extend(['"', '"'])
                index += 2
                continue
            if current:
                segment = "".join(current)
                pieces.append(segment if inside_string else _translate_formula_segment(segment, row_delta))
                current = []
            inside_string = not inside_string
        current.append(character)
        index += 1
    if current:
        segment = "".join(current)
        pieces.append(segment if inside_string else _translate_formula_segment(segment, row_delta))
    return "".join(pieces)


def _translate_formula_segment(segment: str, row_delta: int) -> str:
    def replace(match: re.Match[str]) -> str:
        if match.group("row_abs") == "$":
            return match.group(0)
        row = int(match.group("row")) + row_delta
        if row < 1:
            raise WorkbookContractError("Formula translation produced an invalid row")
        return f"{match.group('col')}{row}"

    return FORMULA_REF_RE.sub(replace, segment)


class ExclusiveFileLock(AbstractContextManager["ExclusiveFileLock"]):
    def __init__(self, path: Path, request_id: str) -> None:
        self.path = path
        self.request_id = request_id
        self._descriptor: int | None = None

    def __enter__(self) -> "ExclusiveFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"request_id": self.request_id, "pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()},
            separators=(",", ":"),
        ).encode()
        try:
            self._descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise WorkbookBusyError("Inventory writer lock already exists") from exc
        os.write(self._descriptor, payload)
        os.fsync(self._descriptor)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class XlsmLogWorkbook:
    def __init__(self, path: Path, contract: WorkbookContract, *, require_vba_project: bool = True) -> None:
        self.path = path
        self.contract = contract
        self.require_vba_project = require_vba_project

    def inspect(self) -> WorkbookInspection:
        with self._open_zip() as archive:
            sheet_path = self._resolve_sheet_path(archive)
            shared = self._read_shared_strings(archive)
            root = self._parse_xml(archive.read(sheet_path), sheet_path)
            sku_headers = self._validate_headers(root, shared)
            first_empty = self._find_first_empty_row(root, shared)
            formula_row = self._find_status_formula_template(root, first_empty)
            vba = tuple(name for name in archive.namelist() if name.casefold().endswith("vbaproject.bin"))
            if self.require_vba_project and not vba:
                raise WorkbookContractError("Macro-enabled workbook has no vbaProject.bin member")
            return WorkbookInspection(
                sheet_path=sheet_path,
                sku_headers=sku_headers,
                first_empty_row=first_empty,
                date_1904=self._read_date_system(archive),
                status_formula_template_row=formula_row,
                vba_members=vba,
            )

    def archive_manifest(self) -> dict[str, str]:
        with self._open_zip() as archive:
            return {info.filename: _sha256_stream(archive.open(info)) for info in archive.infolist()}

    def find_request_marker(self, request_id: str) -> int | None:
        marker = f"INVREQ:{request_id}"
        with self._open_zip() as archive:
            sheet = self._resolve_sheet_path(archive)
            shared = self._read_shared_strings(archive)
            root = self._parse_xml(archive.read(sheet), sheet)
            for row in root.xpath(".//m:sheetData/m:row", namespaces=NS):
                row_number = int(row.get("r", "0"))
                if row_number >= self.contract.data_start_row:
                    note = self._cell_value(self._cell_in_row(row, "B"), shared).text or ""
                    if marker in note:
                        return row_number
        return None

    def create_patched_copy(self, destination: Path, checkout: WorkbookCheckout) -> WorkbookWriteResult:
        before_sha = sha256_file(self.path)
        before_manifest = self.archive_manifest()
        with self._open_zip() as source:
            sheet_path = self._resolve_sheet_path(source)
            shared = self._read_shared_strings(source)
            patched = self._patch_sheet(
                source.read(sheet_path),
                shared_strings=shared,
                date_1904=self._read_date_system(source),
                checkout=checkout,
            )
            if patched.duplicate:
                return WorkbookWriteResult(patched.row, before_sha, before_sha, "", True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._write_archive_with_replacement(source, destination, sheet_path, patched.data)
        verifier = XlsmLogWorkbook(destination, self.contract, require_vba_project=self.require_vba_project)
        after_manifest = verifier.archive_manifest()
        self._verify_manifests(before_manifest, after_manifest, sheet_path)
        verifier._verify_written_row(checkout, patched.row)
        return WorkbookWriteResult(patched.row, before_sha, sha256_file(destination), "", False)

    def _open_zip(self) -> zipfile.ZipFile:
        if not self.path.is_file():
            raise WorkbookContractError(f"Workbook does not exist: {self.path}")
        try:
            archive = zipfile.ZipFile(self.path, "r")
        except zipfile.BadZipFile as exc:
            raise WorkbookContractError("Workbook is not a valid XLSM ZIP package") from exc
        names = archive.namelist()
        if len(names) != len(set(names)):
            archive.close()
            raise WorkbookContractError("Workbook ZIP contains duplicate member names")
        bad = archive.testzip()
        if bad:
            archive.close()
            raise WorkbookContractError(f"Workbook ZIP CRC failure: {bad}")
        return archive

    @staticmethod
    def _parse_xml(data: bytes, member: str) -> etree._Element:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=False, huge_tree=True)
        try:
            return etree.fromstring(data, parser=parser)
        except etree.XMLSyntaxError as exc:
            raise WorkbookContractError(f"Invalid XML in {member}") from exc

    def _resolve_sheet_path(self, archive: zipfile.ZipFile) -> str:
        workbook = self._parse_xml(archive.read("xl/workbook.xml"), "xl/workbook.xml")
        sheets = workbook.xpath(".//m:sheets/m:sheet[@name=$name]", namespaces=NS, name=self.contract.sheet_name)
        if len(sheets) != 1:
            raise WorkbookContractError(f"Expected exactly one sheet named {self.contract.sheet_name!r}")
        relationship_id = sheets[0].get(f"{{{DOC_REL_NS}}}id")
        relationships = self._parse_xml(archive.read("xl/_rels/workbook.xml.rels"), "xl/_rels/workbook.xml.rels")
        targets = relationships.xpath(".//pr:Relationship[@Id=$id]", namespaces=NS, id=relationship_id)
        if len(targets) != 1:
            raise WorkbookContractError("Could not resolve Log worksheet relationship")
        target = targets[0].get("Target", "")
        normalized = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        if normalized not in archive.namelist():
            raise WorkbookContractError(f"Resolved worksheet member is missing: {normalized}")
        return normalized

    def _read_date_system(self, archive: zipfile.ZipFile) -> bool:
        workbook = self._parse_xml(archive.read("xl/workbook.xml"), "xl/workbook.xml")
        nodes = workbook.xpath("./m:workbookPr", namespaces=NS)
        return bool(nodes and nodes[0].get("date1904", "0") in {"1", "true", "True"})

    def _read_shared_strings(self, archive: zipfile.ZipFile) -> tuple[str, ...]:
        if "xl/sharedStrings.xml" not in archive.namelist():
            return ()
        root = self._parse_xml(archive.read("xl/sharedStrings.xml"), "xl/sharedStrings.xml")
        return tuple("".join(item.xpath(".//m:t/text()", namespaces=NS)) for item in root.xpath("./m:si", namespaces=NS))

    def _validate_headers(self, root: etree._Element, shared: tuple[str, ...]) -> tuple[str, ...]:
        header = self._row(root, self.contract.header_row)
        if header is None:
            raise WorkbookContractError(f"Header row {self.contract.header_row} is missing")
        for column, expected in self.contract.metadata_headers.items():
            actual = self._text_in_column(header, column, shared)
            if actual != expected:
                raise WorkbookContractError(f"{column}{self.contract.header_row} must be {expected!r}; found {actual!r}")
        status = self._text_in_column(header, self.contract.status_column, shared)
        if status != "Status":
            raise WorkbookContractError(f"{self.contract.status_column}{self.contract.header_row} must be 'Status'; found {status!r}")
        headers: list[str] = []
        for index in range(column_to_index(self.contract.sku_start_column), column_to_index(self.contract.sku_end_column) + 1):
            column = index_to_column(index)
            value = self._text_in_column(header, column, shared)
            if not value:
                raise WorkbookContractError(f"SKU header {column}{self.contract.header_row} is blank")
            headers.append(value)
        if len(headers) != len(set(headers)):
            raise WorkbookContractError("Duplicate SKU headers in J:HF")
        return tuple(headers)

    def _find_first_empty_row(self, root: etree._Element, shared: tuple[str, ...]) -> int:
        last_scan_column = column_to_index(self.contract.sku_end_column)
        for row_number in range(self.contract.data_start_row, self.contract.data_end_row + 1):
            row = self._row(root, row_number)
            timestamp = self._cell_value(self._cell_in_row(row, "C"), shared)
            if timestamp.text is not None or timestamp.formula:
                continue
            if row is not None:
                for index in range(1, last_scan_column + 1):
                    column = index_to_column(index)
                    value = self._cell_value(self._cell_in_row(row, column), shared)
                    if value.text is not None or value.formula:
                        raise PartialRowError(f"Row {row_number} has a blank Timestamp but data/formula in {column}")
            return row_number
        raise WorkbookContractError("No empty Timestamp row remains within the configured data range")

    def _find_status_formula_template(self, root: etree._Element, target_row: int) -> int:
        for row_number in range(target_row, self.contract.data_start_row - 1, -1):
            cell = self._cell_in_row(self._row(root, row_number), self.contract.status_column)
            if cell is not None and cell.find(f"{{{MAIN_NS}}}f") is not None:
                return row_number
        for row_number in range(target_row + 1, self.contract.data_end_row + 1):
            cell = self._cell_in_row(self._row(root, row_number), self.contract.status_column)
            if cell is not None and cell.find(f"{{{MAIN_NS}}}f") is not None:
                return row_number
        raise WorkbookContractError("No status formula template found in HG")

    def _patch_sheet(
        self,
        source_sheet: bytes,
        *,
        shared_strings: tuple[str, ...],
        date_1904: bool,
        checkout: WorkbookCheckout,
    ) -> _PatchedSheet:
        root = self._parse_xml(source_sheet, "Log worksheet")
        headers = self._validate_headers(root, shared_strings)
        start = column_to_index(self.contract.sku_start_column)
        sku_columns = {sku: index_to_column(start + offset) for offset, sku in enumerate(headers)}
        unknown = [line.sku for line in checkout.lines if line.sku not in sku_columns]
        if unknown:
            raise WorkbookContractError(f"Canonical SKU not present in current J:HF headers: {unknown}")
        marker = f"INVREQ:{checkout.request_id}"
        for row in root.xpath(".//m:sheetData/m:row", namespaces=NS):
            if int(row.get("r", "0")) >= self.contract.data_start_row:
                note = self._cell_value(self._cell_in_row(row, "B"), shared_strings).text or ""
                if marker in note:
                    return _PatchedSheet(source_sheet, int(row.get("r")), True)

        target_number = self._find_first_empty_row(root, shared_strings)
        template_number = self._find_status_formula_template(root, target_number)
        target = self._ensure_row(root, target_number, template_number)
        template = self._row(root, template_number)
        if template is None:
            raise WorkbookContractError("Status template row disappeared")

        def set_text(column: str, value: str) -> None:
            self._set_inline_text(self._ensure_cell(target, column, target_number, template), _sanitize_text(value))

        def set_number(column: str, value: Decimal) -> None:
            self._set_number(self._ensure_cell(target, column, target_number, template), value)

        self._clear_cell_value(self._ensure_cell(target, "A", target_number, template))
        note = _sanitize_text(checkout.note)
        set_text("B", f"{note} | {marker}" if note else marker)
        set_number("C", _excel_serial(checkout.timestamp, date_1904=date_1904))
        set_text("D", checkout.checkout_by)
        set_text("E", checkout.received_by)
        set_text("F", checkout.quote)
        set_text("G", checkout.name)
        set_text("H", checkout.address)
        set_text("I", checkout.purpose)
        for line in checkout.lines:
            set_number(sku_columns[line.sku], line.quantity)

        status = self._ensure_cell(target, self.contract.status_column, target_number, template)
        formula = status.find(f"{{{MAIN_NS}}}f")
        if formula is None or not (formula.text or "").strip():
            template_status = self._cell_in_row(template, self.contract.status_column)
            template_formula = template_status.find(f"{{{MAIN_NS}}}f") if template_status is not None else None
            if template_formula is None or not (template_formula.text or "").strip():
                raise WorkbookContractError("HG status formula is missing")
            self._clear_cell_value(status)
            formula = etree.SubElement(status, f"{{{MAIN_NS}}}f")
            formula.text = translate_formula_rows(template_formula.text or "", target_number - template_number)
        cached = status.find(f"{{{MAIN_NS}}}v")
        if cached is not None:
            status.remove(cached)
        self._sort_cells(target)
        self._update_dimension(root, target_number)
        return _PatchedSheet(etree.tostring(root, xml_declaration=True, encoding="UTF-8"), target_number, False)

    @staticmethod
    def _write_archive_with_replacement(source: zipfile.ZipFile, destination: Path, target: str, replacement: bytes) -> None:
        with zipfile.ZipFile(destination, "w", allowZip64=True) as output:
            for info in source.infolist():
                data = replacement if info.filename == target else source.read(info.filename)
                output.writestr(info, data)

    def _verify_manifests(self, before: dict[str, str], after: dict[str, str], target: str) -> None:
        if set(before) != set(after):
            raise VerificationError("XLSM member list changed during patch")
        changed = [name for name in before if name != target and before[name] != after[name]]
        if changed:
            raise VerificationError(f"Non-target XLSM members changed: {changed[:5]}")
        if self.require_vba_project:
            vba = [name for name in before if name.casefold().endswith("vbaproject.bin")]
            if not vba or any(before[name] != after[name] for name in vba):
                raise VerificationError("VBA project missing or changed")

    def _verify_written_row(self, checkout: WorkbookCheckout, expected_row: int) -> None:
        inspection = self.inspect()
        with self._open_zip() as archive:
            shared = self._read_shared_strings(archive)
            root = self._parse_xml(archive.read(inspection.sheet_path), inspection.sheet_path)
            row = self._row(root, expected_row)
            if row is None:
                raise VerificationError(f"Written row {expected_row} is missing")
            expected_text = {
                "D": checkout.checkout_by,
                "E": checkout.received_by,
                "F": checkout.quote,
                "G": checkout.name,
                "H": checkout.address,
                "I": checkout.purpose,
            }
            for column, expected in expected_text.items():
                actual = self._text_in_column(row, column, shared) or ""
                if actual != _sanitize_text(expected):
                    raise VerificationError(f"Read-back mismatch at {column}{expected_row}")
            if f"INVREQ:{checkout.request_id}" not in (self._text_in_column(row, "B", shared) or ""):
                raise VerificationError("Request idempotency marker was not written")
            header = self._row(root, self.contract.header_row)
            if header is None:
                raise VerificationError("Header row disappeared")
            sku_columns: dict[str, str] = {}
            for index in range(column_to_index(self.contract.sku_start_column), column_to_index(self.contract.sku_end_column) + 1):
                column = index_to_column(index)
                sku = self._text_in_column(header, column, shared)
                if sku:
                    sku_columns[sku] = column
            for line in checkout.lines:
                raw = self._cell_value(self._cell_in_row(row, sku_columns[line.sku]), shared).text
                if Decimal(raw or "0") != line.quantity:
                    raise VerificationError(f"Quantity read-back mismatch for {line.sku}")
            status = self._cell_in_row(row, self.contract.status_column)
            formula = status.find(f"{{{MAIN_NS}}}f") if status is not None else None
            if formula is None or not (formula.text or "").strip():
                raise VerificationError("HG status formula missing after write")

    @staticmethod
    def _row(root: etree._Element, row_number: int) -> etree._Element | None:
        matches = root.xpath(".//m:sheetData/m:row[@r=$row]", namespaces=NS, row=str(row_number))
        if len(matches) > 1:
            raise WorkbookContractError(f"Duplicate worksheet row {row_number}")
        return matches[0] if matches else None

    @staticmethod
    def _cell_in_row(row: etree._Element | None, column: str) -> etree._Element | None:
        if row is None:
            return None
        coordinate = f"{column}{row.get('r')}"
        matches = [cell for cell in row.findall(f"{{{MAIN_NS}}}c") if cell.get("r") == coordinate]
        if len(matches) > 1:
            raise WorkbookContractError(f"Duplicate worksheet cell {coordinate}")
        return matches[0] if matches else None

    @staticmethod
    def _cell_value(cell: etree._Element | None, shared: tuple[str, ...]) -> _CellValue:
        if cell is None:
            return _CellValue(None, None)
        formula_node = cell.find(f"{{{MAIN_NS}}}f")
        formula = formula_node.text if formula_node is not None else None
        if cell.get("t") == "inlineStr":
            text = "".join(cell.xpath("./m:is//m:t/text()", namespaces=NS))
            return _CellValue(text or None, formula)
        value = cell.find(f"{{{MAIN_NS}}}v")
        if value is None or value.text in {None, ""}:
            return _CellValue(None, formula)
        if cell.get("t") == "s":
            try:
                return _CellValue(shared[int(value.text)], formula)
            except (ValueError, IndexError) as exc:
                raise WorkbookContractError("Invalid shared-string index") from exc
        return _CellValue(value.text, formula)

    def _text_in_column(self, row: etree._Element, column: str, shared: tuple[str, ...]) -> str | None:
        return self._cell_value(self._cell_in_row(row, column), shared).text

    def _ensure_row(self, root: etree._Element, row_number: int, template_number: int) -> etree._Element:
        existing = self._row(root, row_number)
        if existing is not None:
            return existing
        sheet_data = root.xpath(".//m:sheetData", namespaces=NS)
        if len(sheet_data) != 1:
            raise WorkbookContractError("Worksheet must contain exactly one sheetData element")
        template = self._row(root, template_number)
        attributes = dict(template.attrib) if template is not None else {}
        attributes["r"] = str(row_number)
        new_row = etree.Element(f"{{{MAIN_NS}}}row", attrib=attributes)
        for index, row in enumerate(sheet_data[0].findall(f"{{{MAIN_NS}}}row")):
            if int(row.get("r", "0")) > row_number:
                sheet_data[0].insert(index, new_row)
                return new_row
        sheet_data[0].append(new_row)
        return new_row

    def _ensure_cell(self, row: etree._Element, column: str, row_number: int, template: etree._Element) -> etree._Element:
        existing = self._cell_in_row(row, column)
        if existing is not None:
            return existing
        attributes = {"r": f"{column}{row_number}"}
        template_cell = self._cell_in_row(template, column)
        if template_cell is not None:
            for attribute in ("s", "cm", "vm", "ph"):
                if template_cell.get(attribute) is not None:
                    attributes[attribute] = template_cell.get(attribute)
        cell = etree.Element(f"{{{MAIN_NS}}}c", attrib=attributes)
        row.append(cell)
        return cell

    @staticmethod
    def _clear_cell_value(cell: etree._Element) -> None:
        for child in list(cell):
            cell.remove(child)
        cell.attrib.pop("t", None)

    def _set_inline_text(self, cell: etree._Element, value: str) -> None:
        self._clear_cell_value(cell)
        cell.set("t", "inlineStr")
        inline = etree.SubElement(cell, f"{{{MAIN_NS}}}is")
        text = etree.SubElement(inline, f"{{{MAIN_NS}}}t")
        if value != value.strip() or "  " in value:
            text.set(f"{{{XML_NS}}}space", "preserve")
        text.text = value

    def _set_number(self, cell: etree._Element, value: Decimal) -> None:
        self._clear_cell_value(cell)
        etree.SubElement(cell, f"{{{MAIN_NS}}}v").text = _decimal_text(value)

    @staticmethod
    def _sort_cells(row: etree._Element) -> None:
        cells = list(row.findall(f"{{{MAIN_NS}}}c"))
        for cell in cells:
            row.remove(cell)
        def key(cell: etree._Element) -> int:
            match = CELL_REF_RE.match(cell.get("r", ""))
            if match is None:
                raise WorkbookContractError("Invalid cell reference")
            return column_to_index(match.group(1))
        for cell in sorted(cells, key=key):
            row.append(cell)

    @staticmethod
    def _update_dimension(root: etree._Element, target_row: int) -> None:
        nodes = root.xpath("./m:dimension", namespaces=NS)
        if not nodes or ":" not in nodes[0].get("ref", ""):
            return
        start, end = nodes[0].get("ref").split(":", 1)
        match = CELL_REF_RE.match(end)
        if match and int(match.group(2)) < target_row:
            nodes[0].set("ref", f"{start}:{match.group(1)}{target_row}")


class WorkbookWriter:
    def __init__(
        self,
        *,
        workbook_path: Path,
        contract: WorkbookContract,
        lock_path: Path,
        staging_dir: Path,
        backup_dir: Path,
        require_vba_project: bool,
        source_stability_seconds: float,
        post_write_stability_seconds: float,
    ) -> None:
        self.workbook_path = workbook_path
        self.contract = contract
        self.lock_path = lock_path
        self.staging_dir = staging_dir
        self.backup_dir = backup_dir
        self.require_vba_project = require_vba_project
        self.source_stability_seconds = max(0.0, source_stability_seconds)
        self.post_write_stability_seconds = max(0.0, post_write_stability_seconds)

    def inspect(self) -> WorkbookInspection:
        return XlsmLogWorkbook(self.workbook_path, self.contract, require_vba_project=self.require_vba_project).inspect()

    def apply(self, checkout: WorkbookCheckout) -> WorkbookWriteResult:
        with ExclusiveFileLock(self.lock_path, checkout.request_id):
            self._wait_for_stable_source()
            source_sha = sha256_file(self.workbook_path)
            self.staging_dir.mkdir(parents=True, exist_ok=True)
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            stage_dir = Path(tempfile.mkdtemp(prefix=f"{checkout.request_id}-", dir=self.staging_dir))
            source_copy = stage_dir / "source.xlsm"
            patched_copy = stage_dir / "patched.xlsm"
            target_temp: Path | None = None
            backup_path: Path | None = None
            replaced = False
            output_sha = ""
            try:
                shutil.copy2(self.workbook_path, source_copy)
                if sha256_file(source_copy) != source_sha:
                    raise ConcurrentEditError("Source changed while being copied")
                staged = XlsmLogWorkbook(source_copy, self.contract, require_vba_project=self.require_vba_project)
                existing = staged.find_request_marker(checkout.request_id)
                if existing is not None:
                    return WorkbookWriteResult(existing, source_sha, source_sha, "", True)
                result = staged.create_patched_copy(patched_copy, checkout)
                output_sha = sha256_file(patched_copy)
                if sha256_file(self.workbook_path) != source_sha:
                    raise ConcurrentEditError("Production workbook changed after staging")
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                backup_path = self.backup_dir / f"Inventory2.{timestamp}.{checkout.request_id}.{source_sha[:12]}.xlsm"
                shutil.copy2(self.workbook_path, backup_path)
                if sha256_file(backup_path) != source_sha:
                    raise VerificationError("Backup hash mismatch")
                target_temp = self.workbook_path.with_name(f".{self.workbook_path.name}.{checkout.request_id}.pending")
                with patched_copy.open("rb") as source, target_temp.open("wb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                if sha256_file(target_temp) != output_sha:
                    raise VerificationError("Pending replacement hash mismatch")
                if sha256_file(self.workbook_path) != source_sha:
                    raise ConcurrentEditError("Production workbook changed before replace")
                os.replace(target_temp, self.workbook_path)
                target_temp = None
                replaced = True
                self._fsync_directory(self.workbook_path.parent)
                if self.post_write_stability_seconds:
                    time.sleep(self.post_write_stability_seconds)
                if sha256_file(self.workbook_path) != output_sha:
                    raise VerificationError("Production hash differs after replace")
                XlsmLogWorkbook(self.workbook_path, self.contract, require_vba_project=self.require_vba_project)._verify_written_row(checkout, result.row)
                return WorkbookWriteResult(result.row, source_sha, output_sha, str(backup_path), False)
            except Exception:
                if replaced and backup_path is not None and output_sha:
                    self._restore_if_owned(backup_path, output_sha)
                raise
            finally:
                if target_temp is not None:
                    try:
                        target_temp.unlink()
                    except FileNotFoundError:
                        pass
                shutil.rmtree(stage_dir, ignore_errors=True)

    def _wait_for_stable_source(self) -> None:
        if not self.workbook_path.exists():
            raise WorkbookContractError(f"Workbook does not exist: {self.workbook_path}")
        first = self.workbook_path.stat()
        if self.source_stability_seconds:
            time.sleep(self.source_stability_seconds)
        second = self.workbook_path.stat()
        if (first.st_size, first.st_mtime_ns, first.st_ino) != (second.st_size, second.st_mtime_ns, second.st_ino):
            raise ConcurrentEditError("Workbook is not stable; it may still be saving or syncing")

    def _restore_if_owned(self, backup_path: Path, expected_current_sha: str) -> None:
        if sha256_file(self.workbook_path) != expected_current_sha:
            raise VerificationError("Automatic restore refused because workbook changed again")
        restore = self.workbook_path.with_name(f".{self.workbook_path.name}.restore")
        shutil.copy2(backup_path, restore)
        os.replace(restore, self.workbook_path)
        self._fsync_directory(self.workbook_path.parent)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)
