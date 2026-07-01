#!/usr/bin/env python3
"""Bandcamp importer — runs on the media host, where /mnt/nas/disk2/Music (the
Navidrome library) is mounted. Downloads a Bandcamp album or whole-artist
discography via yt-dlp into Music/<artist>/<album>/, embeds metadata + cover,
then triggers a Navidrome scan and notifies the requester.

A SEPARATE acquisition path from torrents (music_grab): Bandcamp gives ~128 kbps
MP3 on the public stream (no purchase). Tags from Bandcamp are clean, so Navidrome
groups by them. yt-dlp + ffmpeg required on this host.

Invoked fire-and-forget by media-mcp `music_bandcamp` over ssh:
  bandcamp_import.py <url> <artist> [notify_chat]
"""
import hashlib
import os
import subprocess
import sys
import urllib.parse
import urllib.request

LIB_MUSIC = os.environ.get("LIB_MUSIC", "/mnt/nas/disk2/Music")
NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "http://localhost:4533")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "")
NAVIDROME_PASS = os.environ.get("NAVIDROME_PASS", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
YTDLP = os.environ.get("YTDLP_BIN", os.path.expanduser("~/.local/bin/yt-dlp"))
TIMEOUT = 15


def log(m: str) -> None:
    print("[bandcamp] " + m, flush=True)


def sanitize(name: str) -> str:
    """Mirror media-mcp _sanitize_component — defend against path traversal."""
    name = (name or "").replace("\x00", "").replace("/", " ").replace("\\", " ")
    name = "".join(c for c in name if c >= " ").strip().strip(".").strip()
    name = " ".join(name.split())
    return (name or "Unknown")[:120]


def is_bandcamp(url: str) -> bool:
    p = urllib.parse.urlparse(url)
    return p.scheme in ("http", "https") and p.netloc.endswith("bandcamp.com")


def navidrome_scan() -> None:
    """Subsonic startScan (salted-token). NFS watcher misses new files."""
    if not (NAVIDROME_USER and NAVIDROME_PASS):
        log("navidrome creds missing, skip scan")
        return
    salt = "bcimp"
    tok = hashlib.md5((NAVIDROME_PASS + salt).encode()).hexdigest()
    p = {"u": NAVIDROME_USER, "t": tok, "s": salt, "v": "1.16.1", "c": "bandcamp", "f": "json"}
    try:
        urllib.request.urlopen(NAVIDROME_URL + "/rest/startScan?" + urllib.parse.urlencode(p), timeout=30).read()
    except Exception as e:
        log("navidrome scan error: " + str(e))


def notify(text: str, chat: str = "") -> None:
    token, target = "", ""
    if chat:
        token = os.environ.get("NOTIFY_TOKEN_" + chat, "")
        target = chat
    if not token and TG_TOKEN and TG_CHAT:
        token, target = TG_TOKEN, TG_CHAT
    if not (token and target):
        return
    try:
        urllib.request.urlopen("https://api.telegram.org/bot" + token + "/sendMessage",
                               urllib.parse.urlencode({"chat_id": target, "text": text}).encode(), timeout=10)
    except Exception:
        pass


def build_cmd(url: str, artist: str) -> list:
    out = os.path.join(LIB_MUSIC, artist, "%(album)s", "%(track_number)02d %(title)s.%(ext)s")
    return [YTDLP, "-x", "--audio-format", "mp3", "--audio-quality", "0",
            "--embed-metadata", "--embed-thumbnail", "--no-warnings", "--ignore-errors",
            "-o", out, url]


def main() -> None:
    if len(sys.argv) < 3:
        log("usage: bandcamp_import.py <url> <artist> [notify_chat]")
        sys.exit(2)
    url = sys.argv[1]
    artist = sanitize(sys.argv[2])
    chat = sys.argv[3] if len(sys.argv) > 3 else ""
    if not is_bandcamp(url):
        log("refusing non-bandcamp url: " + url[:80])
        sys.exit(2)
    log("yt-dlp " + url + " -> " + os.path.join(LIB_MUSIC, artist))
    rc = subprocess.run(build_cmd(url, artist)).returncode
    log("yt-dlp rc=" + str(rc))
    navidrome_scan()
    if chat != "silent":
        notify("\U0001F3B5 «" + artist + "» (Bandcamp) готов в Navidrome", chat)


if __name__ == "__main__":
    main()
