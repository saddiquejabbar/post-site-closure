from __future__ import annotations

import hashlib
import json
import os
import posixpath
import shutil
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
XML = "http://www.w3.org/XML/1998/namespace"
ET.register_namespace("", MAIN)
ET.register_namespace("r", DOC_REL)


class SafeWriteError(RuntimeError):
    pass


class LayoutError(SafeWriteError):
    pass


class PartialCommitDetected(SafeWriteError):
    pass


@dataclass(frozen=True)
class Mapping:
    sheet_name: str
    header_row: int
    columns: dict[str, str]
    expected_headers: dict[str, str]
    idempotency_field: str = "row_id"
    require_vba_project: bool = True
    fail_if_sheet_has_tables: bool = True
    fail_if_sheet_protected: bool = True
    fail_if_excel_lock_exists: bool = True
    copy_style_from_previous_row: bool = True
    max_rows_per_write: int = 50

    @classmethod
    def load(cls, path: str | Path) -> "Mapping":
        value = cls(**json.loads(Path(path).read_text(encoding="utf-8")))
        value.validate()
        return value

    def validate(self) -> None:
        if not self.sheet_name or self.header_row < 1:
            raise LayoutError("sheet_name and a positive header_row are required")
        if self.idempotency_field not in self.columns:
            raise LayoutError(f"missing idempotency column: {self.idempotency_field}")
        mapped = [_column(v) for v in self.columns.values()]
        if len(mapped) != len(set(mapped)):
            raise LayoutError("two fields cannot map to the same column")
        for value in self.expected_headers:
            _column(value)


@dataclass(frozen=True)
class Receipt:
    request_id: str
    workbook_path: str
    sheet_name: str
    rows_requested: int
    rows_appended: int
    first_row: int | None
    last_row: int | None
    source_sha256: str
    output_sha256: str
    backup_path: str | None
    duplicate_replay: bool
    committed_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FileLock:
    """Process-level advisory lock using only the Python standard library."""

    def __init__(self, path: Path, timeout: float) -> None:
        self.path, self.timeout, self.handle = path, max(0.0, timeout), None

    def __enter__(self) -> "FileLock":
        self.handle = self.path.open("a+b")
        if os.name == "nt":
            self.handle.seek(0, os.SEEK_END)
            if self.handle.tell() == 0:
                self.handle.write(b"\0")
                self.handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    raise SafeWriteError("another inventory write is in progress")
                time.sleep(0.1)

    def __exit__(self, *_: Any) -> None:
        if not self.handle:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()


class SafeXlsmWriter:
    """Append rows by changing one worksheet XML part, never re-saving the workbook."""

    def __init__(
        self,
        workbook: str | Path,
        mapping: Mapping,
        backup_dir: str | Path,
        lock_timeout: float = 5.0,
    ) -> None:
        self.workbook = Path(workbook).expanduser().resolve()
        self.mapping = mapping
        self.backup_dir = Path(backup_dir).expanduser().resolve()
        self.lock_timeout = lock_timeout
        mapping.validate()

    def append(self, request_id: str, rows: Iterable[dict[str, Any]]) -> Receipt:
        rows = list(rows)
        if not rows or len(rows) > self.mapping.max_rows_per_write:
            raise SafeWriteError("row count is empty or exceeds the configured limit")
        if self.workbook.suffix.lower() != ".xlsm" or not self.workbook.is_file():
            raise SafeWriteError("target must be an existing .xlsm file")
        ids = [str(row.get(self.mapping.idempotency_field, "")).strip() for row in rows]
        if any(not value for value in ids) or len(ids) != len(set(ids)):
            raise SafeWriteError("every row needs a unique, non-empty row ID")

        lock = self.workbook.with_name(f".{self.workbook.name}.inventory.lock")
        with FileLock(lock, self.lock_timeout):
            return self._append_locked(request_id, rows, ids)

    def _append_locked(self, request_id: str, rows: list[dict[str, Any]], ids: list[str]) -> Receipt:
        excel_lock = self.workbook.with_name(f"~${self.workbook.name}")
        if self.mapping.fail_if_excel_lock_exists and excel_lock.exists():
            raise SafeWriteError("Excel appears to have the workbook open")

        source_hash = _file_hash(self.workbook)
        source_mode = self.workbook.stat().st_mode
        backup = self._backup(source_hash)
        candidate: Path | None = None

        try:
            with zipfile.ZipFile(self.workbook) as source:
                if source.testzip():
                    raise SafeWriteError("source workbook ZIP is corrupt")
                if self.mapping.require_vba_project and "xl/vbaProject.bin" not in source.namelist():
                    raise LayoutError("VBA project is missing")
                sheet_part = _sheet_part(source, self.mapping.sheet_name)
                shared = _shared_strings(source)
                root = ET.fromstring(source.read(sheet_part))
                self._validate_sheet(root, shared)

                id_col = _column(self.mapping.columns[self.mapping.idempotency_field])
                existing = _column_values(root, id_col, shared)
                present = set(ids) & existing
                if present == set(ids):
                    return self._receipt(request_id, len(rows), 0, None, None, source_hash, source_hash, backup, True)
                if present:
                    raise PartialCommitDetected("only some incoming row IDs already exist")

                payload, first, last = self._add_rows(root, rows)
                untouched = {
                    info.filename: hashlib.sha256(source.read(info.filename)).hexdigest()
                    for info in source.infolist()
                    if info.filename != sheet_part
                }
                candidate = self._candidate(source, sheet_part, payload)

            self._validate_candidate(candidate, sheet_part, untouched, set(ids))
            if _file_hash(self.workbook) != source_hash:
                raise SafeWriteError("live workbook changed during the transaction")
            os.chmod(candidate, source_mode)
            _fsync(candidate)
            os.replace(candidate, self.workbook)
            candidate = None
            _fsync_dir(self.workbook.parent)
            output_hash = _file_hash(self.workbook)
            return self._receipt(request_id, len(rows), len(rows), first, last, source_hash, output_hash, backup, False)
        finally:
            if candidate and candidate.exists():
                candidate.unlink(missing_ok=True)

    def _validate_sheet(self, root: ET.Element, shared: list[str]) -> None:
        if self.mapping.fail_if_sheet_has_tables and root.find(_q("tableParts")) is not None:
            raise LayoutError("target sheet contains an Excel table")
        if self.mapping.fail_if_sheet_protected and root.find(_q("sheetProtection")) is not None:
            raise LayoutError("target sheet is protected")
        row = _rows(root).get(self.mapping.header_row)
        if row is None:
            raise LayoutError("configured header row does not exist")
        cells = {_cell_column(cell): cell for cell in row.findall(_q("c"))}
        for col, expected in self.mapping.expected_headers.items():
            actual = _cell_text(cells.get(_column(col)), shared).strip()
            if actual.casefold() != expected.strip().casefold():
                raise LayoutError(f"header mismatch at {col}{self.mapping.header_row}: {actual!r}")

    def _add_rows(self, root: ET.Element, values: list[dict[str, Any]]) -> tuple[bytes, int, int]:
        sheet_data = root.find(_q("sheetData"))
        if sheet_data is None:
            raise LayoutError("sheetData is missing")
        existing = _rows(root)
        maximum = max(existing, default=self.mapping.header_row)
        styles: dict[str, str] = {}
        if self.mapping.copy_style_from_previous_row and maximum in existing:
            for cell in existing[maximum].findall(_q("c")):
                if cell.get("s") is not None:
                    styles[_cell_column(cell)] = str(cell.get("s"))

        first = maximum + 1
        for offset, row_values in enumerate(values):
            number = first + offset
            row = ET.Element(_q("row"), {"r": str(number)})
            cells = []
            for field, col in self.mapping.columns.items():
                value = row_values.get(field)
                if value is None:
                    continue
                col = _column(col)
                cells.append((_column_number(col), _new_cell(f"{col}{number}", value, styles.get(col))))
            for _, cell in sorted(cells):
                row.append(cell)
            sheet_data.append(row)
        last = first + len(values) - 1
        _update_dimension(root, last, self.mapping.columns.values())
        return ET.tostring(root, encoding="utf-8", xml_declaration=True), first, last

    def _candidate(self, source: zipfile.ZipFile, sheet_part: str, sheet_bytes: bytes) -> Path:
        fd, name = tempfile.mkstemp(prefix=f".{self.workbook.stem}.inventory-", suffix=".xlsm", dir=self.workbook.parent)
        os.close(fd)
        candidate = Path(name)
        try:
            with zipfile.ZipFile(candidate, "w") as target:
                target.comment = source.comment
                for info in source.infolist():
                    target.writestr(info, sheet_bytes if info.filename == sheet_part else source.read(info.filename))
            return candidate
        except Exception:
            candidate.unlink(missing_ok=True)
            raise

    def _validate_candidate(
        self, candidate: Path, sheet_part: str, untouched: dict[str, str], expected_ids: set[str]
    ) -> None:
        with zipfile.ZipFile(candidate) as result:
            if result.testzip():
                raise SafeWriteError("candidate workbook ZIP is corrupt")
            for name, expected in untouched.items():
                if hashlib.sha256(result.read(name)).hexdigest() != expected:
                    raise SafeWriteError(f"non-target workbook member changed: {name}")
            root = ET.fromstring(result.read(sheet_part))
            actual = _column_values(
                root,
                _column(self.mapping.columns[self.mapping.idempotency_field]),
                _shared_strings(result),
            )
            if not expected_ids <= actual:
                raise SafeWriteError("candidate is missing one or more row IDs")

    def _backup(self, source_hash: str) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = self.backup_dir / f"{self.workbook.stem}.{stamp}.{source_hash[:12]}.xlsm"
        counter = 1
        while target.exists():
            target = self.backup_dir / f"{self.workbook.stem}.{stamp}.{source_hash[:12]}.{counter}.xlsm"
            counter += 1
        shutil.copy2(self.workbook, target)
        _fsync(target)
        return target

    def _receipt(
        self,
        request_id: str,
        requested: int,
        appended: int,
        first: int | None,
        last: int | None,
        source_hash: str,
        output_hash: str,
        backup: Path,
        replay: bool,
    ) -> Receipt:
        return Receipt(
            request_id,
            str(self.workbook),
            self.mapping.sheet_name,
            requested,
            appended,
            first,
            last,
            source_hash,
            output_hash,
            str(backup),
            replay,
            datetime.now(timezone.utc).isoformat(),
        )


def _sheet_part(book: zipfile.ZipFile, sheet_name: str) -> str:
    workbook = ET.fromstring(book.read("xl/workbook.xml"))
    relation_id = None
    sheets = workbook.find(_q("sheets"))
    for sheet in sheets.findall(_q("sheet")) if sheets is not None else []:
        if sheet.get("name") == sheet_name:
            relation_id = sheet.get(f"{{{DOC_REL}}}id")
            break
    if not relation_id:
        raise LayoutError(f"worksheet not found: {sheet_name}")
    rels = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
    target = None
    for rel in rels.findall(f"{{{PKG_REL}}}Relationship"):
        if rel.get("Id") == relation_id:
            target = rel.get("Target")
            break
    if not target:
        raise LayoutError("worksheet relationship is missing")
    path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
    if path not in book.namelist():
        raise LayoutError("worksheet part is missing")
    return path


def _shared_strings(book: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in book.namelist():
        return []
    root = ET.fromstring(book.read("xl/sharedStrings.xml"))
    return ["".join(node.text or "" for node in item.iter(_q("t"))) for item in root.findall(_q("si"))]


def _rows(root: ET.Element) -> dict[int, ET.Element]:
    data = root.find(_q("sheetData"))
    if data is None:
        return {}
    result = {}
    for row in data.findall(_q("row")):
        if str(row.get("r", "")).isdigit():
            result[int(row.get("r"))] = row
    return result


def _column_values(root: ET.Element, col: str, shared: list[str]) -> set[str]:
    values = set()
    for row in _rows(root).values():
        for cell in row.findall(_q("c")):
            if _cell_column(cell) == col:
                value = _cell_text(cell, shared).strip()
                if value:
                    values.add(value)
                break
    return values


def _cell_text(cell: ET.Element | None, shared: list[str]) -> str:
    if cell is None:
        return ""
    if cell.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(_q("t")))
    value = cell.find(_q("v"))
    if value is None or value.text is None:
        return ""
    if cell.get("t") == "s":
        try:
            return shared[int(value.text)]
        except (ValueError, IndexError):
            return ""
    return value.text


def _new_cell(ref: str, value: Any, style: str | None) -> ET.Element:
    attrs = {"r": ref}
    if style is not None:
        attrs["s"] = style
    cell = ET.Element(_q("c"), attrs)
    if isinstance(value, bool):
        cell.set("t", "b")
        ET.SubElement(cell, _q("v")).text = "1" if value else "0"
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        cell.set("t", "n")
        ET.SubElement(cell, _q("v")).text = str(value)
    else:
        cell.set("t", "inlineStr")
        text = ET.SubElement(ET.SubElement(cell, _q("is")), _q("t"))
        rendered = str(value)
        if rendered[:1].isspace() or rendered[-1:].isspace() or "\n" in rendered:
            text.set(f"{{{XML}}}space", "preserve")
        text.text = rendered
    return cell


def _update_dimension(root: ET.Element, last_row: int, mapped: Iterable[str]) -> None:
    maximum = max((_column(v) for v in mapped), key=_column_number)
    dimension = root.find(_q("dimension"))
    if dimension is None:
        root.insert(0, ET.Element(_q("dimension"), {"ref": f"A1:{maximum}{last_row}"}))
        return
    current = dimension.get("ref", "A1")
    start, end = current.split(":", 1) if ":" in current else (current, current)
    end_col = "".join(char for char in end if char.isalpha()) or "A"
    max_col = max((end_col, maximum), key=_column_number)
    start_col = "".join(char for char in start if char.isalpha()) or "A"
    start_row = "".join(char for char in start if char.isdigit()) or "1"
    dimension.set("ref", f"{start_col}{start_row}:{max_col}{last_row}")


def _cell_column(cell: ET.Element) -> str:
    return _column("".join(char for char in cell.get("r", "") if char.isalpha()))


def _column(value: str) -> str:
    value = str(value).strip().upper()
    if not value or any(not ("A" <= char <= "Z") for char in value):
        raise LayoutError(f"invalid Excel column: {value!r}")
    _column_number(value)
    return value


def _column_number(value: str) -> int:
    number = 0
    for char in value.upper():
        number = number * 26 + ord(char) - 64
    if not 1 <= number <= 16384:
        raise LayoutError("Excel column is outside the supported range")
    return number


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _q(tag: str) -> str:
    return f"{{{MAIN}}}{tag}"
