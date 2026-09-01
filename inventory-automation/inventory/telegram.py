from __future__ import annotations

from typing import Any

import httpx


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str, *, timeout_seconds: float = 20.0) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> int:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result = await self._call("sendMessage", payload)
        try:
            return int(result["message_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TelegramError("Telegram did not return a message id") from exc

    async def edit_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self._call(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "disable_web_page_preview": True,
                    "reply_markup": reply_markup or {"inline_keyboard": []},
                },
            )
        except TelegramError as exc:
            if "message is not modified" not in str(exc).casefold():
                raise

    async def answer_callback(self, *, callback_query_id: str, text: str, alert: bool = False) -> None:
        await self._call(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text[:200], "show_alert": alert},
        )

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(f"{self._base_url}/{method}", json=payload)
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramError(f"Telegram {method} request failed") from exc
        if response.status_code >= 400 or not data.get("ok"):
            raise TelegramError(f"Telegram {method}: {data.get('description') or response.status_code}")
        result = data.get("result")
        if isinstance(result, dict):
            return result
        if result is True:
            return {}
        raise TelegramError(f"Telegram {method} returned an unexpected result")
