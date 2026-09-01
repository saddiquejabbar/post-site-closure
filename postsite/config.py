from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_STAFF_NAMES = {
    1: "Staff 1",
    2: "Staff 2",
    3: "Staff 3",
    4: "Staff 4",
    5: "Staff 5",
    6: "Staff 6",
}


def staff_name(staff_no: int) -> str:
    fallback = DEFAULT_STAFF_NAMES.get(staff_no, f"Staff {staff_no}")
    return os.getenv(f"STAFF_{staff_no}_NAME", fallback)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_group_chat_id: str = os.getenv("TELEGRAM_GROUP_CHAT_ID", "")
    post_site_time: str = os.getenv("POST_SITE_TIME", "18:00")
    schedule_retry_seconds: int = int(os.getenv("POST_SITE_RETRY_SECONDS", "60"))
    timezone: str = os.getenv("TIMEZONE", "Asia/Singapore")
    crm_webhook_url: str = os.getenv("CRM_WEBHOOK_URL", "")
    db_path: str = os.getenv("POSTSITE_DB", "data/postsite.db")

    def mention(self, staff_no: int) -> str:
        return os.getenv(f"STAFF_{staff_no}_MENTION", self.staff_name(staff_no))

    def staff_name(self, staff_no: int) -> str:
        return staff_name(staff_no)

    def ensure_data_dir(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
