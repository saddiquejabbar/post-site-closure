# Safe XLSM Write Protocol

## Objective

Append a small number of approved checkout rows while minimizing the chance of damaging a legacy macro-enabled workbook.

The writer is fail-closed. Any mismatch, concurrent change or uncertain replay stops the transaction.

## Preconditions

- Target file extension is `.xlsm`.
- Target path exists locally.
- The configured worksheet exists.
- Required headers match their exact approved columns.
- `xl/vbaProject.bin` exists when `require_vba_project=true`.
- The target sheet is not protected.
- The target sheet does not contain an Excel structured table.
- Every outgoing row has a unique durable row ID.
- Row count does not exceed the configured maximum.
- Excel's `~$Workbook.xlsm` owner file is absent.

## Transaction sequence

1. Acquire a process-level advisory lock beside the workbook.
2. Hash the live workbook with SHA-256.
3. Record the source file metadata.
4. Create a timestamped backup outside the Google Drive folder.
5. Open the workbook as an OPC/ZIP package and run ZIP integrity validation.
6. Resolve the approved worksheet through `workbook.xml` relationships, not a guessed file name.
7. Validate the header fingerprint.
8. Reject unsupported target-sheet features.
9. Read the Automation Row ID column.
10. Apply idempotency rules:
    - all incoming IDs already present → return a duplicate-replay receipt;
    - some present → stop for manual investigation;
    - none present → continue.
11. Append one row per approved SKU to `sheetData`.
12. Copy cell style indexes from the previous row where available.
13. Update the worksheet dimension.
14. Write a candidate `.xlsm` file in the same filesystem.
15. Validate the candidate ZIP.
16. Compare SHA-256 of every non-target package member with the source.
17. Confirm `vbaProject.bin` is unchanged.
18. Parse the modified worksheet and confirm every incoming row ID exists.
19. Re-hash the live workbook. If it differs from step 2, stop; another actor changed it.
20. Flush the candidate to disk.
21. Atomically replace the live path with `os.replace`.
22. Flush the parent directory where supported.
23. Hash the committed workbook and return a receipt.

## What is and is not preserved

The writer rewrites the selected worksheet XML part because new rows must be added. Every other uncompressed package member is required to match its source SHA-256 exactly. This includes the VBA project and unrelated binary parts.

The writer does not execute, decompile, edit or validate VBA behavior. Production acceptance still requires opening the copied output in desktop Excel and running the existing macros.

## Write receipt

A successful receipt contains:

- request ID
- workbook path
- worksheet name
- requested and appended row counts
- first and last inserted row
- source and output SHA-256
- backup path
- duplicate-replay indicator
- UTC commit timestamp

SQLite stores the receipt, and Telegram shows a shortened output hash.

## Recovery cases

| Condition | Result | Operator action |
|---|---|---|
| Workbook missing | No write | Correct path or restore file |
| Excel lock file exists | No write | Close Excel and retry |
| Header mismatch | No write | Review mapping and workbook version |
| Macro project missing | No write | Confirm correct `.xlsm` source |
| Target sheet has a table | No write | Use a plain log sheet or implement table-aware logic separately |
| Another process holds writer lock | No write | Let the active transaction finish |
| Live source hash changed | Candidate discarded | Review the concurrent edit and retry |
| All row IDs already exist | No append; replay receipt | Treat as recovered success |
| Some row IDs already exist | No append | Investigate partial/manual changes |
| Candidate validation fails | Live file untouched | Keep backup and inspect error |
| Process dies before replace | Live file untouched; temp may remain | Remove stale hidden temp after inspection |
| Process dies after replace but before SQLite update | Request may recover as failed | Retry; row IDs prevent duplication |
| Google Drive has not synced | Local commit only | Check Drive client and conflict status |

## Unsupported operations

This writer is not intended to:

- update or resize Excel structured tables;
- modify PivotTables, Power Query or named-range logic;
- insert formulas generated from user text;
- edit VBA or ActiveX controls;
- perform high-volume stock migrations;
- merge concurrent changes from multiple computers;
- prove cloud synchronization.

Those needs require a different integration boundary, preferably an inventory transaction database with Excel as a compatibility output.
