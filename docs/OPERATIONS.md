# Daily operations runbook

## Morning setup

1. Confirm the DMR date and that each site requiring a timed check-in has a trailing appointment time.
2. Send `run post-site` in the configured Telegram operations group.
3. Paste the DMR and tag the bot when required by Telegram privacy mode.
4. Verify the preview against the DMR: site, time, location, and assigned staff.
5. Approve only when the preview is correct. Use **Make Changes** for any mismatch.

## During the day

- OpenClaw delivers one check-in at each approved appointment time.
- Technicians reply directly to the relevant check-in message.
- Operations can see pending or incomplete work in the same Telegram group.
- Do not answer on behalf of a technician or infer that work was completed.

## Good technician replies

Installation completed:

```text
YES
```

Servicing completed:

```text
Found loose neutral at the switch. Re-terminated and tested; all controls working.
```

Incomplete visit:

```text
Two switches pending. Returning Friday after the electrician fixes the wiring; owner informed.
```

## End-of-day review

- Review every `CRM NOTE — DRAFT ONLY` for factual accuracy.
- Follow up on entries marked outstanding or unresolved.
- Confirm payment status only for visits whose DMR required collection.
- Keep CRM writes disabled until the team has approved the note quality and field mapping.

## Recovery rules

- If scheduling fails before approval completes, the draft remains pending and may be approved again.
- If Telegram button cleanup fails after approval, the stored approval and exact job IDs remain authoritative.
- Never start a second Telegram poller with the same bot token as the OpenClaw gateway.
- Before restarting the gateway, verify that approved one-shot jobs are present in OpenClaw's scheduler.
