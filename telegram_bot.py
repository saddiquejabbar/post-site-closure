from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from postsite.config import settings
from postsite.crm import post_to_webhook
from postsite.dmr import parse_dmr
from postsite.models import Site
from postsite.scheduling import build_schedule_plan
from postsite.service import ClosureService
from postsite.store import Store
from postsite.telegram_flow import is_run_post_site, looks_like_dmr, schedule_preview_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("postsite.telegram")

settings.ensure_data_dir()
store = Store(settings.db_path)
service = ClosureService(store)
SCHEDULE_CHANGED_KEY = "post_site_schedule_changed"
ARMED_CHATS_KEY = "post_site_armed_chats"


def wake_scheduler(app: Application) -> None:
    """Wake the scheduler when pending work may have changed."""
    changed = app.bot_data.get(SCHEDULE_CHANGED_KEY)
    if isinstance(changed, asyncio.Event):
        changed.set()


def armed_chats(app: Application) -> dict[int, int]:
    return app.bot_data.setdefault(ARMED_CHATS_KEY, {})


def approval_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Approve Schedule", callback_data=f"postsite:approve:{draft_id}")],
            [
                InlineKeyboardButton("✏️ Make Changes", callback_data=f"postsite:revise:{draft_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"postsite:cancel:{draft_id}"),
            ],
        ]
    )


async def create_draft_preview(
    msg,
    context: ContextTypes.DEFAULT_TYPE,
    source: str,
    requested_by: int,
) -> None:
    sites = parse_dmr(source)
    if not sites:
        await msg.reply_text("I could not find any DMR site entries in that message.")
        return
    draft = store.create_draft(msg.chat_id, requested_by, source, sites)
    armed_chats(context.application).pop(msg.chat_id, None)
    await msg.reply_text(
        schedule_preview_text(draft.sites, settings.post_site_time, settings.staff_name),
        parse_mode=ParseMode.HTML,
        reply_markup=approval_keyboard(draft.id),
    )


async def send_site(bot, chat_id: int | str, site: Site) -> None:
    msg = await bot.send_message(
        chat_id=chat_id,
        text=service.prompt_text(site),
        parse_mode=ParseMode.HTML,
    )
    store.update(site.id, prompt_message_id=msg.message_id, status="awaiting", stage="primary")


async def send_sites(bot, chat_id: int | str, date: str | None = None) -> int:
    """Manual/override send: pushes every pending site for a date right now."""
    sites = [s for s in store.list(dmr_date=date) if s.status == "pending"]
    for site in sites:
        await send_site(bot, chat_id, site)
    return len(sites)


async def close_sites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    date = context.args[0] if context.args else None
    sent = await send_sites(context.bot, update.effective_chat.id, date)
    wake_scheduler(context.application)
    if sent == 0:
        await update.effective_message.reply_text("No unsent sites found.")


async def ingest_dmr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    source = " ".join(context.args).strip()
    if msg.reply_to_message and msg.reply_to_message.text:
        source = msg.reply_to_message.text
    if not source:
        await msg.reply_text("Reply /ingest_dmr to a message containing the Daily Meeting Report.")
        return
    requested_by = update.effective_user.id if update.effective_user else 0
    await create_draft_preview(msg, context, source, requested_by)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sites = store.list()
    if not sites:
        await update.effective_message.reply_text("No sites loaded.")
        return
    recent = sites[-20:]
    lines = [
        f"{s.id}. {s.deal_name} @ {s.location} — {s.status}/{s.stage} "
        f"(sends {s.scheduled_time or settings.post_site_time + ' fallback'})"
        for s in recent
    ]
    await update.effective_message.reply_text("\n".join(lines))


async def handle_installer_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    site: Site,
) -> None:
    msg = update.effective_message
    if not msg or not msg.text:
        return

    site, question = service.record_reply(site.id, msg.text)
    if question:
        follow = await msg.reply_text(question)
        store.update(site.id, prompt_message_id=follow.message_id, status="awaiting")
        return

    ready = await msg.reply_text(service.ready_text(site), parse_mode=ParseMode.HTML)
    store.update(site.id, prompt_message_id=ready.message_id)

    if settings.crm_webhook_url:
        ok, detail = post_to_webhook(site, settings.crm_webhook_url)
        if ok:
            store.update(site.id, status="sent")
            await msg.reply_text("✅ Closure sent to CRM workflow.")
        else:
            log.warning("CRM webhook failed for site %s: %s", site.id, detail)
            await msg.reply_text("⚠️ CRM note is ready, but the CRM webhook failed. It remains saved locally.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route the natural trigger, DMR capture, and threaded installer replies."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not msg.text or not chat:
        return

    bot_username = context.bot.username or ""
    if is_run_post_site(msg.text, bot_username):
        armed_chats(context.application)[chat.id] = user.id if user else 0
        await msg.reply_text(
            "Ready for the Daily Meeting Report. Paste it next. If I do not see the "
            "pasted message, reply to that DMR and tag me."
        )
        return

    if msg.reply_to_message:
        site = store.by_prompt_message(msg.reply_to_message.message_id)
        if site:
            await handle_installer_reply(update, context, site)
            return

    requested_by = armed_chats(context.application).get(chat.id)
    if requested_by is None:
        return

    candidates = [msg.text]
    if msg.reply_to_message and msg.reply_to_message.text:
        candidates.insert(0, msg.reply_to_message.text)
    source = next((candidate for candidate in candidates if looks_like_dmr(candidate)), "")
    if source:
        await create_draft_preview(msg, context, source, requested_by)
        return

    if bot_username and f"@{bot_username}".casefold() in msg.text.casefold():
        await msg.reply_text("Reply to the DMR message when you tag me, so I can read it.")


async def handle_schedule_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.message:
        return
    try:
        _prefix, action, draft_id_text = query.data.split(":", 2)
        draft_id = int(draft_id_text)
    except (TypeError, ValueError):
        await query.answer("Invalid schedule action.", show_alert=True)
        return

    draft = store.get_draft(draft_id)
    if not draft or draft.chat_id != str(query.message.chat_id):
        await query.answer("This schedule draft is no longer available.", show_alert=True)
        return
    if draft.requested_by and query.from_user.id != draft.requested_by:
        await query.answer(
            "Only the person who started this post-site run can approve or change it.",
            show_alert=True,
        )
        return
    if draft.status != "pending":
        await query.answer(f"This schedule is already {draft.status}.", show_alert=True)
        return

    await query.answer()
    preview = schedule_preview_text(
        draft.sites,
        settings.post_site_time,
        settings.staff_name,
        include_instruction=False,
    )
    if action == "approve":
        result = store.approve_draft(draft.id)
        if not result:
            await query.edit_message_reply_markup(reply_markup=None)
            return
        _approved, live_sites = result
        wake_scheduler(context.application)
        await query.edit_message_text(
            f"{preview}\n\n✅ <b>APPROVED</b> — {len(live_sites)} installer check-in(s) scheduled.",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "revise":
        if not store.update_draft_status(draft.id, "revising"):
            return
        armed_chats(context.application)[query.message.chat_id] = query.from_user.id
        await query.edit_message_text(
            f"{preview}\n\n✏️ <b>REVISION REQUESTED</b> — paste the corrected DMR.",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "cancel":
        if not store.update_draft_status(draft.id, "cancelled"):
            return
        await query.edit_message_text(
            f"{preview}\n\n❌ <b>CANCELLED</b> — no check-ins were scheduled.",
            parse_mode=ParseMode.HTML,
        )


async def scheduled_send_loop(app: Application, schedule_changed: asyncio.Event) -> None:
    """Sleep until the next site is due, waking early when the DMR changes."""
    if not settings.telegram_group_chat_id:
        log.info("TELEGRAM_GROUP_CHAT_ID not set; automatic post-site scheduling disabled.")
        return
    tz = ZoneInfo(settings.timezone)
    chat_id = int(settings.telegram_group_chat_id)
    retry_seconds = max(1, settings.schedule_retry_seconds)

    while True:
        # Clearing before reading the store prevents a newly ingested DMR from
        # being lost between calculating the next target and starting the wait.
        schedule_changed.clear()
        now = datetime.now(tz)
        plan = build_schedule_plan(
            store.list(status="pending"), now, tz, settings.post_site_time
        )
        for site in plan.due:
            try:
                await send_site(app.bot, chat_id, site)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Could not send scheduled prompt for site %s; retrying in at most %ss",
                    site.id,
                    retry_seconds,
                )
            else:
                log.info(
                    "Sent scheduled post-site prompt for site %s (%s) at %s",
                    site.id,
                    site.deal_name,
                    site.scheduled_time or f"{settings.post_site_time} (fallback)",
                )

        # Successful sends changed pending sites to awaiting. Re-read once to
        # calculate one precise timer for the next remaining appointment.
        now = datetime.now(tz)
        plan = build_schedule_plan(
            store.list(status="pending"), now, tz, settings.post_site_time
        )
        delays: list[float] = []
        if plan.due:
            # A due site remains pending only when its Telegram send failed.
            delays.append(float(retry_seconds))
        if plan.next_at:
            delays.append(max(0.0, (plan.next_at - now).total_seconds()))

        if not delays:
            log.debug("No pending sites; scheduler sleeping until the DMR changes.")
            await schedule_changed.wait()
            continue

        delay = min(delays)
        log.debug("Next scheduler wake-up in %.1f seconds.", delay)
        try:
            await asyncio.wait_for(schedule_changed.wait(), timeout=delay)
        except TimeoutError:
            pass


async def post_init(app: Application) -> None:
    schedule_changed = asyncio.Event()
    app.bot_data[SCHEDULE_CHANGED_KEY] = schedule_changed
    app.create_task(
        scheduled_send_loop(app, schedule_changed),
        name="post-site-scheduled-send",
    )


def main() -> None:
    if not settings.telegram_bot_token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env first.")
    app = Application.builder().token(settings.telegram_bot_token).post_init(post_init).build()
    app.add_handler(CommandHandler("ingest_dmr", ingest_dmr))
    app.add_handler(CommandHandler("close_sites", close_sites))
    app.add_handler(CommandHandler("postsite_status", status))
    app.add_handler(
        CallbackQueryHandler(handle_schedule_action, pattern=r"^postsite:(approve|revise|cancel):\d+$")
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print(
        "Post-Site Closure Agent running. Say 'run post-site' or use /ingest_dmr. Commands: "
        "/close_sites [YYYY-MM-DD], /postsite_status"
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
