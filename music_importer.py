#!/usr/bin/env python3
"""Music importer — runs on the media host, where the disk2 NFS mount with both
the qBittorrent downloads (/mnt/nas/disk2/Torrents) and the Navidrome library
(/mnt/nas/disk2/Music) is visible.

Pipeline (see docs/music-pipeline-plan.md, mirror of book-importer, Режим A):
  music_grab (media-mcp on the bot host) → Prowlarr grab → qB category [Music], tagged
  mus:<base64url(artist/album/chat)>  →  THIS importer polls qB, and for each
  COMPLETED [Music] torrent that carries a mus: tag and is not yet mus_done:
    1. map qB container path (/downloads/...) → host path (/mnt/nas/disk2/Torrents/...)
    2. recursive per-file hardlink into  Music/Артист/Альбом/  (cp -al, copy on EXDEV)
    3. Subsonic startScan on Navidrome (its NFS watcher misses hardlinks)
    4. tag the torrent mus_done (idempotent — already-done torrents are skipped)

Режим A = passthrough: tags are NOT edited (T1 spike confirmed lossless RU rips ship
clean UTF-8 tags; Navidrome groups by tags natively). No cross-host DB — state lives
in qB tags. The torrent keeps seeding from [Music]/ (hardlink shares the inode).

If a future MP3 rip lands with CP1251/mojibake tags, that is the trigger for Режим B
(copy + mutagen/beets) — see docs/music-pipeline-plan.md T8. Not implemented here.

Run as a loop (systemd) or once for testing:  python3 music_importer.py --once
"""

import base64
import hashlib
import os
import subprocess
import sys
import time

import requests

QBIT_URL = os.environ.get("QBIT_URL", "http://localhost:8081")
QBIT_USER = os.environ.get("QBIT_USER", "")
QBIT_PASS = os.environ.get("QBIT_PASS", "")
NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "http://localhost:4533")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "")
NAVIDROME_PASS = os.environ.get("NAVIDROME_PASS", "")
CATEGORY = os.environ.get("MUSIC_CATEGORY", "[Music]")

LIB_MUSIC = os.environ.get("LIB_MUSIC", "/mnt/nas/disk2/Music")
# qB reports container paths; map them to the host NFS mount this importer sees.
DL_CONTAINER = os.environ.get("QBIT_DL_CONTAINER", "/downloads")
DL_HOST = os.environ.get("QBIT_DL_HOST", "/mnt/nas/disk2/Torrents")
POLL_SEC = int(os.environ.get("IMPORTER_POLL_SEC", "60"))
# Optional Telegram "music ready" notification (best-effort; silent if unset)
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
TIMEOUT = 15

_cookies: dict = {}


def _log(msg: str) -> None:
    print(f"[music-importer] {msg}", flush=True)


# ── qBittorrent ──────────────────────────────────────────────

def _qlogin() -> bool:
    """qBittorrent 5.x: HTTP 204 + cookie QBT_SID_<port>; older: 200 "Ok." + SID.
    Keep whatever cookie(s) the server set (version-agnostic)."""
    global _cookies
    r = requests.post(f"{QBIT_URL}/api/v2/auth/login",
                      data={"username": QBIT_USER, "password": QBIT_PASS}, timeout=TIMEOUT)
    if r.status_code in (200, 204) and r.cookies:
        _cookies = r.cookies.get_dict()
        return bool(_cookies)
    return False


def _qget(path: str, **params):
    r = requests.get(f"{QBIT_URL}/api/v2{path}", cookies=_cookies, params=params, timeout=TIMEOUT)
    if r.status_code == 403:
        _qlogin()
        r = requests.get(f"{QBIT_URL}/api/v2{path}", cookies=_cookies, params=params, timeout=TIMEOUT)
    return r


def _qpost(path: str, **data):
    r = requests.post(f"{QBIT_URL}/api/v2{path}", cookies=_cookies, data=data, timeout=TIMEOUT)
    if r.status_code == 403:
        _qlogin()
        r = requests.post(f"{QBIT_URL}/api/v2{path}", cookies=_cookies, data=data, timeout=TIMEOUT)
    return r


# ── Navidrome (Subsonic) ─────────────────────────────────────

def _navidrome_scan() -> bool:
    """Trigger a Navidrome library scan via Subsonic startScan (salted-token auth).
    The NFS watcher misses hardlinks, so an explicit scan is required (T1 spike)."""
    salt = "musimp"
    token = hashlib.md5((NAVIDROME_PASS + salt).encode()).hexdigest()
    params = {"u": NAVIDROME_USER, "t": token, "s": salt,
              "v": "1.16.1", "c": "music-importer", "f": "json"}
    try:
        r = requests.get(f"{NAVIDROME_URL}/rest/startScan", params=params, timeout=30)
        ok = r.status_code == 200 and r.json().get("subsonic-response", {}).get("status") == "ok"
        if not ok:
            _log(f"Navidrome startScan not ok: HTTP {r.status_code}")
        return ok
    except Exception as e:
        _log(f"Navidrome startScan error: {e}")
        return False


# ── Telegram notification (best-effort) ──────────────────────

def _notify_telegram(text: str, chat: str = "") -> None:
    """Tell the requester music is ready. Routes per-requester: if the mus tag
    carried a chat and NOTIFY_TOKEN_<chat> is set, notify via THAT bot/chat. Falls
    back to TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID. Best-effort, never raises. Tokens
    live only in this importer's env (not in qB tags)."""
    token, target = "", ""
    if chat:
        token = os.environ.get(f"NOTIFY_TOKEN_{chat}", "")
        target = chat
    if not token and TG_TOKEN and TG_CHAT:
        token, target = TG_TOKEN, TG_CHAT
    if not (token and target):
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": target, "text": text}, timeout=10)
        code = getattr(r, "status_code", 200)
        _log(f"notified chat {target}" if code == 200
             else f"telegram notify HTTP {code} for chat {target}")
    except Exception as e:
        _log(f"telegram notify failed: {e}")


# ── Import logic ─────────────────────────────────────────────

def sanitize(name: str) -> str:
    """Mirror media-mcp _sanitize_component — defend against path traversal."""
    name = (name or "").replace("\x00", "").replace("/", " ").replace("\\", " ")
    name = "".join(c for c in name if c >= " ").strip().strip(".").strip()
    name = " ".join(name.split())
    return (name or "Unknown")[:120]


def to_host_path(p: str) -> str:
    if p and p.startswith(DL_CONTAINER):
        return DL_HOST + p[len(DL_CONTAINER):]
    return p


def _hardlink_tree(src: str, dst: str) -> None:
    """Recursive per-file hardlink (directories cannot be hardlinked). Fallback to
    copy if the filesystem refuses the link (EXDEV/EMLINK). dst is the album folder,
    so a single-file release lands inside Артист/Альбом/ (not bare at the root)."""
    os.makedirs(dst, exist_ok=True)
    if os.path.isdir(src):
        rc = subprocess.run(["cp", "-al", src + "/.", dst + "/"]).returncode
        if rc != 0:
            _log(f"hardlink failed (rc={rc}), copying instead: {src}")
            subprocess.run(["cp", "-a", src + "/.", dst + "/"])
    else:
        target = os.path.join(dst, os.path.basename(src))
        try:
            os.link(src, target)
        except OSError:
            subprocess.run(["cp", "-a", src, target])


def import_torrent(t: dict) -> bool:
    tags = [x.strip() for x in (t.get("tags") or "").split(",") if x.strip()]
    mus = next((x for x in tags if x.startswith("mus:")), None)
    if not mus or "mus_done" in tags:
        return False
    try:
        decoded = base64.urlsafe_b64decode(mus[4:].encode()).decode()
        parts = decoded.split("/", 3)
        artist = parts[0]
        album = parts[1] if len(parts) > 1 else ""
        notify_chat = parts[2] if len(parts) > 2 else ""
        kind = parts[3] if len(parts) > 3 else "album"
    except Exception as e:
        _log(f"bad mus tag {mus!r}: {e}")
        return False
    artist, album = sanitize(artist), sanitize(album)
    src = to_host_path(t.get("content_path") or t.get("save_path") or "")
    if not src or not os.path.exists(src):
        _log(f"source path missing: {src!r} (torrent {t.get('name')})")
        return False
    # T4: discography = one torrent with per-album subfolders → place its contents
    # directly under the artist (no single "album" wrapper); Navidrome groups by
    # tags, so each album shows separately. Single albums nest as Артист/Альбом/.
    if kind == "discography":
        dst = os.path.join(LIB_MUSIC, artist)
    else:
        dst = os.path.join(LIB_MUSIC, artist, album)
    _hardlink_tree(src, dst)
    _navidrome_scan()
    _qpost("/torrents/addTags", hashes=t.get("hash"), tags="mus_done")
    _log(f"IMPORTED ({kind}) «{artist}/{album}» <- {os.path.basename(src)}")
    if notify_chat != "silent":
        if kind == "discography":
            _notify_telegram(f"🎵 discography “{artist}” is ready in Navidrome", notify_chat)
        else:
            _notify_telegram(f"🎵 “{album}” — {artist} is ready in Navidrome", notify_chat)
    return True


def tick() -> int:
    if not _cookies and not _qlogin():
        _log("qB login failed")
        return 0
    r = _qget("/torrents/info", category=CATEGORY)
    if r.status_code != 200:
        _log(f"qB /torrents/info -> {r.status_code}")
        return 0
    done = 0
    for t in r.json():
        if t.get("progress", 0) >= 1.0:
            try:
                if import_torrent(t):
                    done += 1
            except Exception as e:
                _log(f"import error {t.get('name')!r}: {e}")
    return done


def main() -> None:
    once = "--once" in sys.argv
    _log(f"start (poll={POLL_SEC}s, once={once}, category={CATEGORY})")
    while True:
        try:
            tick()
        except Exception as e:
            _log(f"tick error: {e}")
        if once:
            break
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
