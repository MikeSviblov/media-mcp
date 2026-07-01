#!/usr/bin/env python3
"""Audiobook/ebook importer — runs on the media host, where the disk2 NFS mount
with both the qBittorrent downloads (/mnt/nas/disk2/Torrents) and the Audiobookshelf
library (/mnt/nas/disk2/Abooks) is visible.

Pipeline (see docs/audiobook-pipeline-plan.md, Approach C):
  book_grab (media-mcp on the bot host) → Prowlarr grab → qB category [Abooks], tagged
  abk:<base64url(author/title/type)>  →  THIS importer polls qB, and for each
  COMPLETED [Abooks] torrent that carries an abk: tag and is not yet abk_done:
    1. map qB container path (/downloads/...) → host path (/mnt/nas/disk2/Torrents/...)
    2. recursive per-file hardlink into  Abooks/{Audiobooks|Ebooks}/Автор/Название/
       (fallback to copy on EXDEV/EMLINK)
    3. POST /api/libraries/{id}/scan to Audiobookshelf (its NFS watcher misses hardlinks)
    4. tag the torrent abk_done (idempotent — already-done torrents are skipped)

State lives entirely in qB tags, so no cross-host database is needed and the importer
survives restarts. The torrent keeps seeding from [Abooks]/ (hardlink shares the inode).

Run as a loop (systemd) or once for testing:  python3 importer.py --once
"""

import base64
import os
import subprocess
import sys
import time

import requests

QBIT_URL = os.environ.get("QBIT_URL", "http://localhost:8081")
QBIT_USER = os.environ.get("QBIT_USER", "")
QBIT_PASS = os.environ.get("QBIT_PASS", "")
ABS_URL = os.environ.get("ABS_URL", "http://localhost:13378")
ABS_API_KEY = os.environ.get("ABS_API_KEY", "") or os.environ.get("AUDIOBOOKSHELF_TOKEN", "")
CATEGORY = os.environ.get("ABOOKS_CATEGORY", "[Abooks]")

LIB_AUDIOBOOKS = os.environ.get("LIB_AUDIOBOOKS", "/mnt/nas/disk2/Abooks/Audiobooks")
LIB_EBOOKS = os.environ.get("LIB_EBOOKS", "/mnt/nas/disk2/Abooks/Ebooks")
# qB reports container paths; map them to the host NFS mount this importer sees.
DL_CONTAINER = os.environ.get("QBIT_DL_CONTAINER", "/downloads")
DL_HOST = os.environ.get("QBIT_DL_HOST", "/mnt/nas/disk2/Torrents")
POLL_SEC = int(os.environ.get("IMPORTER_POLL_SEC", "60"))
# Wait for the async ABS scan to register the new item before fixing its metadata
METADATA_FIX_RETRIES = int(os.environ.get("METADATA_FIX_RETRIES", "12"))
METADATA_FIX_DELAY = int(os.environ.get("METADATA_FIX_DELAY", "5"))
# Optional Telegram "book ready" notification (best-effort; silent if unset)
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
TIMEOUT = 15

_cookies: dict = {}


def _log(msg: str) -> None:
    print(f"[importer] {msg}", flush=True)


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


# ── Audiobookshelf ───────────────────────────────────────────

def _abs_book_library_ids():
    r = requests.get(f"{ABS_URL}/api/libraries",
                     headers={"Authorization": f"Bearer {ABS_API_KEY}"}, timeout=TIMEOUT)
    if r.status_code != 200:
        return []
    return [l["id"] for l in r.json().get("libraries", []) if l.get("mediaType") == "book"]


def _abs_scan(lib_id: str) -> None:
    requests.post(f"{ABS_URL}/api/libraries/{lib_id}/scan",
                  headers={"Authorization": f"Bearer {ABS_API_KEY}"}, timeout=30)


def _abs_delete_item(item_id: str, h: dict) -> None:
    """Remove a library-item record from ABS. NO hard delete (no `hard` flag):
    duplicate items point at the SAME files (shared hardlinks) as the survivor, so
    deleting files here would destroy the survivor's data too. db-only removal
    leaves the folder on disk untouched."""
    requests.delete(f"{ABS_URL}/api/items/{item_id}", headers=h, timeout=TIMEOUT)


def _abs_rescan_item(item_id: str, h: dict) -> None:
    """Re-scan a single item to clear a stale isMissing flag. ABS sometimes marks
    a freshly-imported large multi-file book as missing (folder-watcher saw it
    mid-hardlink); a per-item rescan re-validates the files on disk and clears it."""
    requests.post(f"{ABS_URL}/api/items/{item_id}/scan", headers=h, timeout=30)


def _abs_fix_metadata(lib_ids: list, author: str, title: str) -> bool:
    """Override garbled/inconsistent embedded ID3 metadata with our canonical
    author/title, and collapse duplicate items ABS creates for the same folder.

    Russian torrents ship CP1251 ID3 tags (ABS misreads as Latin-1 → mojibake) or
    narrator-as-author spellings ("Дэн Симмонс" vs "Симмонс Дэн"). For large
    multi-file books ABS's folder-watcher races our explicit scan and creates a
    SECOND item for the same folder within milliseconds (one raw + isMissing, one
    canonical). We match ALL items by relPath (== the folder we created =
    «Автор/Название»), keep one survivor (prefer non-missing), PATCH it with our
    canonical metadata, and delete the rest (db-only — files are shared hardlinks).
    Durable across rescans. Best-effort: polls for the item (scan is async), never raises.
    """
    relpath = f"{author}/{title}"
    h = {"Authorization": f"Bearer {ABS_API_KEY}", "Content-Type": "application/json"}
    for _ in range(METADATA_FIX_RETRIES):
        time.sleep(METADATA_FIX_DELAY)
        matches: dict = {}
        for lib_id in lib_ids:
            try:
                r = requests.get(f"{ABS_URL}/api/libraries/{lib_id}/items",
                                 headers=h, params={"sort": "addedAt", "desc": 1, "limit": 25},
                                 timeout=TIMEOUT)
            except Exception as e:
                _log(f"ABS items fetch error: {e}")
                continue
            if r.status_code != 200:
                continue
            for it in r.json().get("results", []):
                if (it.get("relPath") or "") == relpath and it.get("id"):
                    matches[it["id"]] = it
        if not matches:
            continue
        # survivor: prefer a non-missing item; tie-break deterministically by id.
        # The duplicate twin (if any) is created in the same scan, so a single poll
        # that finds the folder sees both — no settle wait needed.
        items = sorted(matches.values(),
                       key=lambda it: (bool(it.get("isMissing")), it.get("id")))
        survivor, dupes = items[0], items[1:]
        try:
            requests.patch(f"{ABS_URL}/api/items/{survivor['id']}/media", headers=h,
                           json={"metadata": {"title": title,
                                              "authors": [{"name": author}]}},
                           timeout=TIMEOUT)
        except Exception as e:
            _log(f"ABS metadata patch error: {e}")
        for d in dupes:
            try:
                _abs_delete_item(d["id"], h)
            except Exception as e:
                _log(f"ABS dedup delete error: {e}")
        # a lone (or surviving) item can carry a stale isMissing flag — rescan clears it
        rescanned = False
        if survivor.get("isMissing"):
            try:
                _abs_rescan_item(survivor["id"], h)
                rescanned = True
            except Exception as e:
                _log(f"ABS rescan error: {e}")
        _log(f"ABS metadata set: «{relpath}»"
             + (f" + removed {len(dupes)} duplicate(s)" if dupes else "")
             + (" + cleared stale missing" if rescanned else ""))
        return True
    _log(f"ABS item not found for metadata fix: «{relpath}»")
    return False


# ── Telegram notification (best-effort) ──────────────────────

def _notify_telegram(text: str, chat: str = "") -> None:
    """Tell the requester a book is ready. Routes per-requester: if the abk tag
    carried a chat and NOTIFY_TOKEN_<chat> is set, notify via THAT bot/chat
    (primary user via Sebastian's bot, secondary user via Max's bot). Falls back to the default
    TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID for untagged/legacy grabs. Best-effort,
    never raises. Tokens live only in this importer's env (not in qB tags)."""
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
    copy if the filesystem refuses the link (EXDEV/EMLINK)."""
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


def import_torrent(t: dict, lib_ids: list) -> bool:
    tags = [x.strip() for x in (t.get("tags") or "").split(",") if x.strip()]
    abk = next((x for x in tags if x.startswith("abk:")), None)
    if not abk or "abk_done" in tags:
        return False
    try:
        decoded = base64.urlsafe_b64decode(abk[4:].encode()).decode()
        parts = decoded.split("/", 3)
        author = parts[0]
        title = parts[1] if len(parts) > 1 else ""
        btype = parts[2] if len(parts) > 2 else "audiobook"
        notify_chat = parts[3] if len(parts) > 3 else ""
    except Exception as e:
        _log(f"bad abk tag {abk!r}: {e}")
        return False
    author, title = sanitize(author), sanitize(title)
    root = LIB_EBOOKS if btype == "ebook" else LIB_AUDIOBOOKS
    src = to_host_path(t.get("content_path") or t.get("save_path") or "")
    if not src or not os.path.exists(src):
        _log(f"source path missing: {src!r} (torrent {t.get('name')})")
        return False
    dst = os.path.join(root, author, title)
    _hardlink_tree(src, dst)
    for lid in lib_ids:
        _abs_scan(lid)
    # override garbled/inconsistent embedded ID3 metadata with canonical names
    _abs_fix_metadata(lib_ids, author, title)
    _qpost("/torrents/addTags", hashes=t.get("hash"), tags="abk_done")
    _log(f"IMPORTED «{author}/{title}» ({btype}) <- {os.path.basename(src)}")
    icon = "📖" if btype == "ebook" else "📚"
    tail = "" if btype == "ebook" else " 🎧"
    # notify_chat == "silent" suppresses the notification (used for bulk orders)
    if notify_chat != "silent":
        _notify_telegram(f"{icon} “{title}” — {author} is ready in Audiobookshelf{tail}", notify_chat)
    return True


def tick() -> int:
    if not _cookies and not _qlogin():
        _log("qB login failed")
        return 0
    r = _qget("/torrents/info", category=CATEGORY)
    if r.status_code != 200:
        _log(f"qB /torrents/info -> {r.status_code}")
        return 0
    lib_ids = _abs_book_library_ids()
    if not lib_ids:
        _log("no Audiobookshelf book libraries found")
    done = 0
    for t in r.json():
        if t.get("progress", 0) >= 1.0:
            try:
                if import_torrent(t, lib_ids):
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
