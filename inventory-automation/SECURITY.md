# Security and publishing policy

## Never commit

- the production `.xlsm`, `.xlsx`, `.xlsb`, or exported workbook ZIP;
- Telegram bot tokens or webhook secrets;
- real chat/user IDs;
- customer names, addresses, quote numbers, or messages;
- Google Drive paths, folder IDs, or credentials;
- production SQLite databases, lock files, backups, or logs;
- the private SKU/alias catalogue unless publication is explicitly authorised;
- employee mappings or operational schedules.

The supplied examples are synthetic.

## Access controls

Use separate allowlists for requesters and approvers. Approvers must be a subset of authorised users. Keep the bot in the minimum required Telegram chat and rotate its token if exposed.

The webhook secret validates Telegram's request origin at the application boundary. It does not replace HTTPS, host firewall rules, or network access control.

## Callback controls

Telegram callback data contains only an opaque random token. The server stores the action and payload in SQLite. Tokens expire, are single-use, and are invalidated when a preview changes. Authorisation is checked before consumption so an unauthorised user cannot burn an approver's button.

## Data minimisation

The service stores the original Telegram instruction and parsed fields because they are required for audit and recovery. The database therefore contains operational/customer data and must be protected like the workbook. Restrict filesystem permissions, encrypt the device, and define retention.

Application logs deliberately avoid message bodies, tokens, workbook paths, and customer fields. Error messages shown in Telegram redact the full workbook path.

## Writer privilege

Run the process under a dedicated local account that can access only:

- the one production workbook;
- its staging, lock, state, and backup directories;
- outbound Telegram HTTPS.

Do not give the workflow broad Drive or home-directory access. Keep backups outside the Drive sync folder to avoid conflict amplification.

## Public-repository warning

This implementation may describe an employer workflow. Publication authority is separate from technical sanitisation. Confirm ownership and permission before making an implementation public or associating it with a company name.

## Reporting vulnerabilities

Do not open a public issue containing production files, secrets, personal data, or exploitable deployment details. Revoke exposed credentials first, preserve relevant logs privately, and use the repository owner's private security contact.
