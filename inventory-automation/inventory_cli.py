from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from inventory.catalog import SkuCatalog
from inventory.config import ConfigurationError, Settings
from inventory.parser import NaturalCheckoutParser
from inventory.validation import validate_checkout
from inventory.workbook import WorkbookContract, WorkbookWriter


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_writer(settings: Settings) -> WorkbookWriter:
    return WorkbookWriter(
        workbook_path=settings.workbook_path,
        contract=WorkbookContract(
            sheet_name=settings.log_sheet_name,
            header_row=settings.header_row,
            data_start_row=settings.data_start_row,
            data_end_row=settings.data_end_row,
            sku_start_column=settings.sku_start_column,
            sku_end_column=settings.sku_end_column,
            status_column=settings.status_column,
        ),
        lock_path=settings.lock_file_path,
        staging_dir=settings.staging_dir,
        backup_dir=settings.backup_dir,
        require_vba_project=settings.require_vba_project,
        source_stability_seconds=settings.source_stability_seconds,
        post_write_stability_seconds=settings.post_write_stability_seconds,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Inventory2 safe-writer diagnostics")
    parser.add_argument("--env", default=".env")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-workbook")
    preview = commands.add_parser("preview")
    preview.add_argument("--text", required=True)
    args = parser.parse_args()
    load_env_file(Path(args.env))
    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    writer = build_writer(settings)
    try:
        inspection = writer.inspect()
    except Exception as exc:
        print(f"Workbook validation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.command == "validate-workbook":
        print(json.dumps({
            "workbook": settings.workbook_path.name,
            "sheet_member": inspection.sheet_path,
            "sku_count": len(inspection.sku_headers),
            "first_empty_row": inspection.first_empty_row,
            "date_system": "1904" if inspection.date_1904 else "1900",
            "status_formula_template_row": inspection.status_formula_template_row,
            "vba_member_count": len(inspection.vba_members),
            "write_enabled": settings.write_enabled,
        }, indent=2))
        return 0
    catalog = SkuCatalog.from_json_file(inspection.sku_headers, settings.sku_aliases_path)
    parsed = NaturalCheckoutParser(catalog).parse(args.text)
    validation = validate_checkout(parsed, max_abs_quantity=settings.max_abs_quantity, max_items=settings.max_items_per_request)
    print(json.dumps({"parsed": parsed.to_dict(), "validation": validation.to_dict()}, indent=2))
    return 0 if validation.ready else 3


if __name__ == "__main__":
    raise SystemExit(main())
