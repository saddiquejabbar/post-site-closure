from __future__ import annotations

import argparse
import json
from pathlib import Path

from postsite.config import settings
from postsite.dmr import parse_dmr
from postsite.service import ClosureService
from postsite.store import Store


def get_store() -> Store:
    settings.ensure_data_dir()
    return Store(settings.db_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Post-Site Closure Agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    ingest = sub.add_parser("ingest", help="Parse and save a DMR text file")
    ingest.add_argument("file")

    prompts = sub.add_parser("prompts", help="Print closure prompts")
    prompts.add_argument("--date")

    reply = sub.add_parser("reply", help="Simulate a staff reply locally")
    reply.add_argument("site_id", type=int)
    reply.add_argument("text")

    payload = sub.add_parser("payload", help="Print CRM/n8n payload")
    payload.add_argument("site_id", type=int)

    listing = sub.add_parser("list", help="List sites")
    listing.add_argument("--date")

    args = parser.parse_args()
    store = get_store()
    service = ClosureService(store)

    if args.cmd == "ingest":
        text = Path(args.file).read_text(encoding="utf-8")
        sites = store.upsert_sites(parse_dmr(text))
        print(f"Saved {len(sites)} site(s).")
        for s in sites:
            print(f"[{s.id}] {s.deal_name} @ {s.location} | {s.activity_raw} | staff {s.staff_numbers}")
        return

    if args.cmd == "prompts":
        for s in store.list(dmr_date=args.date):
            print(f"\n--- SITE {s.id} ---")
            print(service.prompt_text(s).replace("<b>", "").replace("</b>", ""))
        return

    if args.cmd == "reply":
        site, question = service.record_reply(args.site_id, args.text)
        if question:
            print(question)
        else:
            print(service.ready_text(site).replace("<b>", "").replace("</b>", ""))
        return

    if args.cmd == "payload":
        site = store.get(args.site_id)
        if not site:
            raise SystemExit(f"Site {args.site_id} not found")
        print(json.dumps(service.payload(site), indent=2, ensure_ascii=False))
        return

    if args.cmd == "list":
        for s in store.list(dmr_date=args.date):
            print(f"[{s.id}] {s.dmr_date} | {s.status:8} | {s.deal_name} @ {s.location} | {s.activity_raw}")


if __name__ == "__main__":
    main()
