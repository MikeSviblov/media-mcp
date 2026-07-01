#!/usr/bin/env python3
"""Jellyseerr webhook receiver — pushes media notifications to Telegram.

Listens on HTTP :8097 for Jellyseerr webhook events (Media Available,
Media Approved) and sends Telegram messages to the appropriate user.

Run: python3 webhook_receiver.py
Env: WEBHOOK_SECRET, TELEGRAM_BOT_TOKEN_PRIMARY, TELEGRAM_CHAT_ID_PRIMARY,
     TELEGRAM_BOT_TOKEN_SECONDARY, TELEGRAM_CHAT_ID_SECONDARY
"""

import hmac
import json
import logging
import os
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("media-webhook")

PORT = int(os.environ.get("WEBHOOK_PORT", "8097"))
SECRET = os.environ.get("WEBHOOK_SECRET", "")

# Telegram targets
TG_PRIMARY_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN_PRIMARY", "")
TG_PRIMARY_CHAT = os.environ.get("TELEGRAM_CHAT_ID_PRIMARY", "")
TG_SECONDARY_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN_SECONDARY", "")
TG_SECONDARY_CHAT = os.environ.get("TELEGRAM_CHAT_ID_SECONDARY", "")

TIMEOUT = 10


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    """Send a message via Telegram Bot API."""
    if not token or not chat_id:
        log.warning("Telegram credentials missing, skipping send")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            log.info("Telegram sent to %s", chat_id)
            return True
        log.error("Telegram error %d: %s", resp.status_code, resp.text[:200])
        return False
    except requests.RequestException as e:
        log.error("Telegram send failed: %s", e)
        return False


def format_notification(data: dict) -> str | None:
    """Format Jellyseerr webhook payload into a human-readable message."""
    notif_type = data.get("notification_type", "")
    media = data.get("media", {})
    subject = data.get("subject", "")
    message = data.get("message", "")

    title = media.get("tmdbTitle") or subject or "Unknown"
    media_type = media.get("media_type", "")
    year = media.get("tmdbYear", "")

    type_label = "Фильм" if media_type == "movie" else "Сериал" if media_type == "tv" else ""

    if notif_type == "MEDIA_AVAILABLE":
        return (
            f"🎬 <b>{title}</b> ({year}) готов!\n"
            f"{type_label} скачался — приятного просмотра!"
        )
    elif notif_type == "MEDIA_APPROVED":
        return (
            f"✅ <b>{title}</b> ({year}) одобрен\n"
            f"Заказ принят, скачивание начнётся скоро."
        )
    elif notif_type == "MEDIA_PENDING":
        return (
            f"⏳ <b>{title}</b> ({year}) ожидает одобрения\n"
            f"{message}"
        )
    elif notif_type == "MEDIA_DECLINED":
        return f"❌ <b>{title}</b> ({year}) отклонён\n{message}"
    elif notif_type == "MEDIA_FAILED":
        return f"⚠️ <b>{title}</b> ({year}) — ошибка загрузки\n{message}"
    elif notif_type == "TEST_NOTIFICATION":
        return "🔔 Тест webhook — всё работает!"
    else:
        log.info("Unknown notification type: %s", notif_type)
        return None


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Auth check
        if SECRET:
            auth = self.headers.get("Authorization", "")
            expected = f"Bearer {SECRET}"
            # constant-time compare to avoid leaking the secret via timing
            if not hmac.compare_digest(auth, expected):
                log.warning("Unauthorized request from %s", self.client_address[0])
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Forbidden")
                return

        # Read body
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Empty body")
            return

        try:
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except (json.JSONDecodeError, Exception) as e:
            log.error("Invalid JSON: %s", e)
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid JSON")
            return

        log.info("Received: %s", data.get("notification_type", "unknown"))

        text = format_notification(data)
        if text:
            # Send to both users
            if TG_PRIMARY_TOKEN and TG_PRIMARY_CHAT:
                send_telegram(TG_PRIMARY_TOKEN, TG_PRIMARY_CHAT, text)
            if TG_SECONDARY_TOKEN and TG_SECONDARY_CHAT:
                send_telegram(TG_SECONDARY_TOKEN, TG_SECONDARY_CHAT, text)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        """Health check endpoint."""
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"media-mcp webhook receiver")

    def log_message(self, format, *args):
        """Suppress default HTTP log — we use our own logger."""
        pass


def main():
    if not SECRET:
        log.warning("WEBHOOK_SECRET not set — accepting all requests")
    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    log.info("Listening on :%d", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
