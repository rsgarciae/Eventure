"""Telegram bot over long polling only.

No webhook, no inbound port, no public hostname. Everything is an outbound
request to api.telegram.org.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from .http import urlopen

API = "https://api.telegram.org/bot{token}/{method}"


def _retry_after(detail: str) -> float:
    """Telegram reports flood control as 429 with parameters.retry_after."""
    try:
        parsed = json.loads(detail)
        return min(float(parsed.get("parameters", {}).get("retry_after", 5)), 30.0)
    except Exception:  # noqa: BLE001 - malformed body, use a safe default
        return 5.0


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, timeout: float = 35.0):
        if not token:
            raise TelegramError("missing bot token (set TELEGRAM_BOT_TOKEN)")
        self.token = token
        self.timeout = timeout

    def call(self, method: str, payload: dict | None = None, timeout: float | None = None) -> dict:
        url = API.format(token=self.token, method=method)
        body = json.dumps(payload or {}).encode("utf-8")
        for attempt in (1, 2):
            request = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}
            )
            try:
                with urlopen(request, timeout=timeout or self.timeout) as response:
                    parsed = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code == 429 and attempt == 1:
                    # Sending many cards at once trips flood control; wait it out.
                    time.sleep(_retry_after(detail))
                    continue
                raise TelegramError(f"{method}: HTTP {exc.code} {detail[:300]}") from exc
            except Exception as exc:  # noqa: BLE001
                raise TelegramError(f"{method}: {type(exc).__name__}: {exc}") from exc
        else:  # pragma: no cover - loop always breaks or raises
            raise TelegramError(f"{method}: gave up after retry")
        if not parsed.get("ok"):
            raise TelegramError(f"{method}: {parsed.get('description', 'not ok')}")
        return parsed.get("result", {})

    def send(self, chat_id: int, text: str, keyboard: list[list[dict]] | None = None) -> dict:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return self.call("sendMessage", payload)

    def answer(self, callback_id: str, text: str) -> None:
        self.call(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text[:180]},
        )

    def send_photo(
        self,
        chat_id: int,
        photo: str,
        caption: str,
        keyboard: list[list[dict]] | None = None,
    ) -> dict:
        """Send by URL: Telegram fetches the image, nothing is uploaded from the Pi."""
        payload = {
            "chat_id": chat_id,
            "photo": photo,
            "caption": caption,
            "parse_mode": "HTML",
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return self.call("sendPhoto", payload)

    def replace_buttons(self, chat_id: int, message_id: int, keyboard: list[list[dict]]) -> None:
        self.call(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )

    def poll(self, offset: int | None, timeout: int = 25, retry_delay: float = 5.0):
        """Yield whole updates forever. Long-polls between them."""
        backoff = retry_delay
        while True:
            payload = {"timeout": timeout, "allowed_updates": ["callback_query", "message"]}
            if offset is not None:
                payload["offset"] = offset
            try:
                updates = self.call("getUpdates", payload, timeout=timeout + 10)
                backoff = retry_delay
            except TelegramError:
                # Transient network/Telegram errors must not kill the service.
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                yield update
