from __future__ import annotations

import html

from .config import settings
from .crm import build_crm_payload, build_closure_note
from .models import Site
from .questions import next_question, primary_question, work_summary
from .store import Store


class ClosureService:
    def __init__(self, store: Store):
        self.store = store

    def prompt_text(self, site: Site) -> str:
        mentions = " ".join(settings.mention(n) for n in site.staff_numbers)
        title = f"{site.deal_name} @ {site.location}" if site.location else site.deal_name
        planned = work_summary(site)
        todo = site.activity_raw or site.visit_type.title()
        if planned:
            todo = f"{todo} — {planned}"
        if site.payment_required:
            todo += "; collect payment"
        return (
            f"{mentions} — <b>{html.escape(title)}</b>\n"
            f"<b>Things to be done:</b> {html.escape(todo)}\n\n"
            f"{html.escape(primary_question(site))}"
        )

    def record_reply(self, site_id: int, text: str) -> tuple[Site, str | None]:
        site = self.store.get(site_id)
        if not site:
            raise KeyError(site_id)
        question, changes = next_question(site, text)
        site = self.store.update(site_id, **changes)
        return site, question

    @staticmethod
    def ready_text(site: Site) -> str:
        return (
            f"✅ <b>CRM note ready — {html.escape(site.deal_name)} @ {html.escape(site.location)}</b>\n"
            f"{html.escape(build_closure_note(site))}"
        )

    @staticmethod
    def payload(site: Site) -> dict:
        return build_crm_payload(site)
