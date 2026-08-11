"""Benachrichtigungen über einen Telegram-Bot."""

from __future__ import annotations

import html
import time

import requests

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 20
RETRIES = 3
MAX_LEN = 4096  # Telegram-Limit pro Nachricht


class NotifyError(RuntimeError):
    """Telegram hat die Nachricht nicht angenommen."""


class Telegram:
    def __init__(self, token: str, chat_id: str):
        if not token or not chat_id:
            raise NotifyError(
                "TELEGRAM_TOKEN oder TELEGRAM_CHAT_ID ist nicht gesetzt (siehe .env.example)."
            )
        self.token = token
        self.chat_id = str(chat_id)

    def _call(self, method: str, data: dict, files: dict | None = None) -> dict:
        last_error: Exception | None = None
        for attempt in range(RETRIES):
            try:
                response = requests.post(
                    API.format(token=self.token, method=method),
                    data={**data, "chat_id": self.chat_id},
                    files=files,
                    timeout=TIMEOUT,
                )
                payload = response.json()
                if payload.get("ok"):
                    return payload
                raise NotifyError(f"Telegram-Fehler: {payload.get('description', payload)}")
            except requests.RequestException as exc:
                last_error = exc
                if attempt < RETRIES - 1:
                    time.sleep(2**attempt)
        raise NotifyError(f"Telegram nicht erreichbar: {last_error}") from last_error

    def send(self, text: str) -> None:
        for chunk in _split(text, MAX_LEN):
            self._call(
                "sendMessage",
                {"text": chunk, "parse_mode": "HTML", "disable_web_page_preview": "true"},
            )

    def send_photo(self, image: bytes, caption: str = "", filename: str = "plan.png") -> None:
        self._call(
            "sendPhoto",
            {"caption": caption[:1024], "parse_mode": "HTML"},
            files={"photo": (filename, image)},
        )


def _get(token: str, method: str) -> dict:
    response = requests.get(API.format(token=token, method=method), timeout=TIMEOUT)
    payload = response.json()
    if not payload.get("ok"):
        raise NotifyError(f"Telegram-Fehler bei {method}: {payload.get('description', payload)}")
    return payload.get("result") or {}


def get_me(token: str) -> dict:
    """Bot-Infos — bestätigt, dass das Token gültig ist, und liefert den @Namen."""
    if not token:
        raise NotifyError("TELEGRAM_TOKEN ist nicht gesetzt (siehe .env.example).")
    return _get(token, "getMe")


def get_webhook_info(token: str) -> dict:
    """Ist ein Webhook gesetzt, gehen Updates dorthin und getUpdates bleibt leer."""
    return _get(token, "getWebhookInfo")


def find_chats(token: str) -> list[tuple[str, str]]:
    """Liest die Chats, die dem Bot geschrieben haben: [(Chat-ID, Anzeigename)].

    Spart den Umweg über getUpdates im Browser. Voraussetzung: dem Bot wurde
    vorher mindestens einmal geschrieben — vorher kennt er niemanden.
    """
    if not token:
        raise NotifyError("TELEGRAM_TOKEN ist nicht gesetzt (siehe .env.example).")

    response = requests.get(API.format(token=token, method="getUpdates"), timeout=TIMEOUT)
    payload = response.json()
    if not payload.get("ok"):
        raise NotifyError(f"Telegram-Fehler: {payload.get('description', payload)}")

    chats: dict[str, str] = {}
    for update in payload.get("result", []):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat")
            if not chat:
                continue
            name = chat.get("title") or " ".join(
                filter(None, [chat.get("first_name"), chat.get("last_name")])
            ) or chat.get("username") or chat.get("type", "")
            chats[str(chat["id"])] = name
    return sorted(chats.items())


def _split(text: str, limit: int) -> list[str]:
    """Teilt an Zeilenumbrüchen, damit kein HTML-Tag zerrissen wird."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:  # Notbremse für eine einzelne überlange Zeile
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit and current:
            chunks.append(current.rstrip("\n"))
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip("\n"))
    return chunks


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def format_changes(groups: dict[str, list[tuple[str, str]]]) -> str:
    """Baut die Nachricht. groups: Plandatum -> [(Kurs-Label, Zeilentext)]."""
    total = sum(len(rows) for rows in groups.values())
    plural = "Änderung" if total == 1 else "Änderungen"
    lines = [f"<b>Vertretungsplan — {total} {plural}</b>"]
    for plan_date, rows in groups.items():
        lines.append(f"\n📅 <b>{esc(plan_date or 'ohne Datum')}</b>")
        for label, summary in rows:
            prefix = f"<b>{esc(label)}</b> · " if label else ""
            lines.append(f"• {prefix}{esc(summary)}")
    return "\n".join(lines)


def format_notices(datum: str, notices) -> str:
    """Meldungen des Infoblatts, die einen selbst betreffen."""
    kopf = f"📎 <b>Infoblatt</b>{' — ' + esc(datum) if datum else ''}"
    zeilen = [kopf]
    for notice in notices:
        prefix = f"<b>{esc(notice.addressee)}:</b> " if notice.addressee else ""
        zeilen.append(f"• {prefix}{esc(notice.text)}")
    return "\n".join(zeilen)


def format_failure(message: str, count: int) -> str:
    return (
        f"⚠️ <b>DSB-Watcher hängt</b>\n"
        f"{count} Fehlläufe in Folge.\n\n"
        f"<code>{esc(message)[:600]}</code>\n\n"
        "Bis das behoben ist, kommen <b>keine</b> Vertretungs-Meldungen mehr."
    )
