# Inventory Checkout Runbook

## Purpose

Turn one natural-language Telegram message into a controlled inventory checkout written to a legacy macro-enabled Excel workbook in a locally synced Google Drive folder.

The bot does **not** write directly from an unverified message. Every request moves through validation, confirmation and a single controlled writer.

## Normal operator flow

Send a message such as:

```text
Checkout for Tan Residence, installer Amir: 4 x ZB-SW2 and 2 x HUB-PRO. Site tomorrow. Use van stock.
```

The workflow:

1. validates the Telegram webhook secret, chat and sender;
2. extracts customer/site, installer, items, quantities and notes;
3. resolves each item against the approved SKU catalogue;
4. shows the structured checkout for confirmation;
5. accepts **Approve** or **Reject**;
6. queues the approved request behind the single workbook writer;
7. rechecks the source workbook before writing;
8. creates and verifies a backup;
9. writes only the mapped checkout fields;
10. validates the saved `.xlsm` and records the result;
11. returns a Telegram receipt.

## Flagged requests

The bot blocks automatic approval when an item, quantity, customer/site or workbook condition is uncertain.

Examples:

- an item description matches more than one SKU;
- a quantity is missing or invalid;
- two fields conflict;
- the workbook changed after the preview was generated;
- a previous request appears to be a duplicate;
- the backup or saved workbook cannot be verified.

For a resolvable item ambiguity, Telegram displays selection buttons such as:

```text
Which 2-gang switch did you mean?

[ ZB-SW2-WH ]  [ ZB-SW2-BK ]
[ Reject request ]
```

The button stores a short server-side choice token. It does not place workbook paths, customer details or credentials inside Telegram callback data.

After all flags are resolved, the bot regenerates the final preview. A user must approve that exact version before any workbook write.

## Controlled write sequence

The writer follows this order:

1. acquire the process and workbook lock;
2. reload the approved job from SQLite;
3. verify the job has not already completed;
4. calculate the current source-workbook hash;
5. block the write if it differs from the approved source hash;
6. copy the workbook to a timestamped backup;
7. verify the backup exists and matches the source hash;
8. copy the source to a temporary working file;
9. open the working copy with VBA preservation enabled;
10. change only approved cells/rows from the workbook mapping;
11. save the working copy;
12. verify it is a readable macro-enabled workbook and contains the expected values;
13. atomically replace the source where the host filesystem supports it;
14. calculate and journal the final hash;
15. mark the transaction complete;
16. release the lock and send the receipt.

A failure before replacement leaves the source unchanged. A failure after replacement remains visible in the transaction journal and can be reconciled from the verified backup.

## Duplicate and retry behaviour

Telegram and HTTP clients may retry updates. The workflow stores Telegram `update_id`, message identity, callback identity and its own transaction key.

A repeated request therefore returns the existing state or receipt instead of writing a second checkout.

## Production setup

Provide these outside Git:

- Telegram bot token;
- Telegram webhook secret;
- allowed chat and user IDs;
- local path to the Google Drive-synced `.xlsm` file;
- production SKU catalogue;
- approved workbook sheet/column mapping;
- backup and SQLite state directories;
- HTTPS ingress for the webhook.

Use the example environment and mapping files as templates. Never commit the live workbook, customer data, production paths, credentials or inventory catalogue.

## Host boundary

For the strongest compatibility with a complex legacy workbook, run the controlled writer on a Windows host with desktop Excel available and keep Telegram/web processing separate from the one-writer queue.

`openpyxl` with `keep_vba=True` can retain the VBA package for many `.xlsm` files, but it does not guarantee preservation of every Excel feature, digital signature, external connection or embedded control. Validate a representative copy of the real workbook before production. Use Excel COM automation when exact Excel fidelity is required.

## Recovery

When a job is blocked or fails:

1. do not resend the original message repeatedly;
2. inspect the job state and failure reason;
3. confirm the source workbook is closed and fully synced;
4. compare the current, approved and backup hashes;
5. resolve the workbook or catalogue issue;
6. resume or create a corrected request through the bot;
7. restore only from a verified backup when reconciliation requires it.

Never bypass the queue by manually running multiple writers against the shared workbook.

## Production acceptance checks

Before enabling live writes, confirm:

- unauthorized webhook requests are rejected;
- unauthorized chats and users are rejected;
- ambiguous SKUs produce buttons and no write;
- approve and reject callbacks are one-time and version-bound;
- duplicate Telegram updates do not duplicate rows;
- a changed source hash blocks the write;
- a backup is created and verified before each write;
- VBA remains present after a representative checkout;
- expected formulas, pivots, controls and external links still work;
- Google Drive sync does not expose partial files;
- failed Telegram receipts do not cause a second workbook write;
- recovery from the latest verified backup has been rehearsed.
