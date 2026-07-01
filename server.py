#!/usr/bin/env python3
"""Media MCP Server — shared media stack management.

Provides media tools for Sebastian (full), Max (safe), and Claude Code (full).
Mode controlled by MEDIA_MCP_MODE env: "safe" (search/request/status only)
or "full" (+ direct Radarr/Sonarr/qBit/Prowlarr/Jellyfin control).

Run: uvx --with mcp --with requests python server.py
"""

import functools
import json
import os

import requests
from mcp.server.fastmcp import FastMCP

# ── Config from env ──────────────────────────────────────────

MODE = os.environ.get("MEDIA_MCP_MODE", "safe")

JELLYSEERR_URL = os.environ.get("JELLYSEERR_URL", "http://localhost:5055")
JELLYSEERR_API_KEY = os.environ.get("JELLYSEERR_API_KEY", "")

RADARR_URL = os.environ.get("RADARR_URL", "http://localhost:7878")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")

SONARR_URL = os.environ.get("SONARR_URL", "http://localhost:8989")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")

PROWLARR_URL = os.environ.get("PROWLARR_URL", "http://localhost:9696")
PROWLARR_API_KEY = os.environ.get("PROWLARR_API_KEY", "")

QBIT_URL = os.environ.get("QBIT_URL", "http://localhost:8081")
QBIT_USER = os.environ.get("QBIT_USER", "")
QBIT_PASS = os.environ.get("QBIT_PASS", "")

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://localhost:8096")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")

# Audiobookshelf (books/audiobooks pipeline)
ABS_URL = os.environ.get("ABS_URL", "http://localhost:13378")
ABS_API_KEY = os.environ.get("ABS_API_KEY", "") or os.environ.get("AUDIOBOOKSHELF_TOKEN", "")
# qB category that the book download client / importer use (literal, with brackets)
ABOOKS_CATEGORY = os.environ.get("ABOOKS_CATEGORY", "[Abooks]")
# Torznab categories: 3030 = Audio/Audiobook, 7000 = Books (ebooks)
BOOK_CATEGORIES = {"audiobook": 3030, "ebook": 7000}

# Navidrome (music pipeline) — Subsonic API, salted-token auth (user/pass, not bearer)
NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "http://localhost:4533")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "")
NAVIDROME_PASS = os.environ.get("NAVIDROME_PASS", "")
# qB category that the music importer polls (literal, with brackets)
MUSIC_CATEGORY = os.environ.get("MUSIC_CATEGORY", "[Music]")
# Torznab category 3000 = Audio (music). State lives in qB tags (mus:/mus_done), no DB.
MUSIC_CATEGORY_ID = 3000
# Grab guards (#12): refuse if disk low or too many music downloads in flight
MUSIC_MIN_FREE_GB = int(os.environ.get("MUSIC_MIN_FREE_GB", "50"))
MUSIC_MAX_INFLIGHT = int(os.environ.get("MUSIC_MAX_INFLIGHT", "20"))
# Bandcamp path: yt-dlp runs on the NAS host (disk2 mount); media-mcp triggers it over ssh
BANDCAMP_SSH_HOST = os.environ.get("MEDIA_NAS_SSH", "localhost")
BANDCAMP_REMOTE = "/opt/appdata/music-importer/bandcamp_import.py"
BANDCAMP_ENV = "/opt/appdata/music-importer/bandcamp.env"

TIMEOUT = 15

mcp = FastMCP("media-mcp")


# ── Helpers ──────────────────────────────────────────────────

def _safe_request(func):
    """Catch network errors so a single down service doesn't crash MCP."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except requests.RequestException as e:
            return f"Error: {type(e).__name__} — {e}"
    return wrapper


def _jellyseerr_headers() -> dict:
    return {"X-Api-Key": JELLYSEERR_API_KEY, "Accept": "application/json"}


def _arr_headers(api_key: str) -> dict:
    return {"X-Api-Key": api_key, "Accept": "application/json"}


def _jellyfin_params() -> dict:
    return {"api_key": JELLYFIN_API_KEY}


def _status_message(resp: requests.Response, context: str = "") -> str:
    """Convert HTTP status codes to user-friendly messages."""
    code = resp.status_code
    if code == 409:
        return f"This {context} is already requested."
    if code == 404:
        return f"{context} not found (ID does not exist)."
    if code == 429:
        return "Too many requests — wait a minute."
    if code == 401:
        return "Authorization error — check the API key."
    if code >= 500:
        return f"Service temporarily unavailable (HTTP {code})."
    return f"Error: HTTP {code} — {resp.text[:200]}"


def _abs_headers() -> dict:
    return {"Authorization": f"Bearer {ABS_API_KEY}"}


def _sanitize_component(name: str) -> str:
    """Sanitize an author/title into a single safe path component.
    Strips path separators, traversal, control chars; caps length. Defends
    against path traversal since these strings become filesystem paths.
    """
    name = (name or "").replace("\x00", "")
    name = name.replace("/", " ").replace("\\", " ")
    name = "".join(ch for ch in name if ch >= " ")  # drop control chars
    name = name.strip().strip(".").strip()           # no leading/trailing dots/space
    name = " ".join(name.split())                    # collapse whitespace
    if name in ("", ".", ".."):
        name = "Unknown"
    return name[:120]


# ── qBittorrent auth (module level — used by SAFE book_status too) ──

_qbit_cookie_jar: dict = {}


def _qbit_login() -> bool:
    """Login to qBittorrent and cache the session cookie.
    qBittorrent 5.x returns HTTP 204 and a cookie named QBT_SID_<port>; older
    versions returned 200 "Ok." with a SID cookie. Accept both and keep whatever
    cookie(s) the server set, so the helper is version-agnostic.
    """
    global _qbit_cookie_jar
    resp = requests.post(f"{QBIT_URL}/api/v2/auth/login",
                         data={"username": QBIT_USER, "password": QBIT_PASS},
                         timeout=TIMEOUT)
    if resp.status_code in (200, 204) and resp.cookies:
        _qbit_cookie_jar = resp.cookies.get_dict()
        return bool(_qbit_cookie_jar)
    return False


def _qbit_cookies() -> dict:
    if not _qbit_cookie_jar:
        _qbit_login()
    return _qbit_cookie_jar


# ── SAFE tools (both modes) ──────────────────────────────────

@mcp.tool()
@_safe_request
def media_search(query: str, type: str = "any") -> str:
    """Search for movies and TV shows by name. Uses Jellyseerr (TMDb).
    Args: query — search string (Russian or English), type — 'movie', 'tv', or 'any'.
    Returns top results with tmdbId, title, year, overview, and availability status.
    """
    if not query.strip():
        return "Error: empty query. Provide a movie or series title."
    params = {"query": query, "page": 1, "language": "ru"}
    resp = requests.get(f"{JELLYSEERR_URL}/api/v1/search",
                        headers=_jellyseerr_headers(), params=params, timeout=TIMEOUT)
    if resp.status_code != 200:
        return _status_message(resp, "search")
    results = []
    for item in resp.json().get("results", [])[:10]:
        media_type = item.get("mediaType", "")
        if type != "any" and media_type != type:
            continue
        entry = {
            "tmdbId": item.get("id"),
            "type": media_type,
            "title": item.get("title") or item.get("name", ""),
            "originalTitle": item.get("originalTitle") or item.get("originalName", ""),
            "year": (item.get("releaseDate") or item.get("firstAirDate") or "")[:4],
            "overview": (item.get("overview") or "")[:200],
        }
        ms = item.get("mediaInfo", {})
        if ms:
            status_map = {1: "unknown", 2: "pending", 3: "processing",
                          4: "partially_available", 5: "available"}
            entry["status"] = status_map.get(ms.get("status"), "unknown")
        else:
            entry["status"] = "not_requested"
        results.append(entry)
    if not results:
        return f"Nothing found for “{query}”."
    return json.dumps(results, ensure_ascii=False, indent=2)


@mcp.tool()
@_safe_request
def media_availability(tmdb_id: int, type: str = "movie") -> str:
    """Check if a movie/show is available in Jellyfin or being downloaded.
    Args: tmdb_id — TMDb ID, type — 'movie' or 'tv'.
    """
    endpoint = "movie" if type == "movie" else "tv"
    resp = requests.get(f"{JELLYSEERR_URL}/api/v1/{endpoint}/{tmdb_id}",
                        headers=_jellyseerr_headers(), timeout=TIMEOUT)
    if resp.status_code == 404:
        return f"Not found in TMDb (ID {tmdb_id})."
    if resp.status_code != 200:
        return _status_message(resp, "check")
    data = resp.json()
    title = data.get("title") or data.get("name", "")
    ms = data.get("mediaInfo", {})
    if not ms:
        return json.dumps({"title": title, "status": "not_in_library",
                           "message": "Not in library and not requested."}, ensure_ascii=False)
    status_map = {1: "unknown", 2: "pending", 3: "processing",
                  4: "partially_available", 5: "available"}
    status = status_map.get(ms.get("status"), "unknown")
    return json.dumps({"title": title, "status": status,
                       "tmdbId": tmdb_id, "type": type}, ensure_ascii=False, indent=2)


@mcp.tool()
@_safe_request
def media_request(tmdb_id: int, type: str = "movie", seasons: str = "all") -> str:
    """Request a movie or TV show via Jellyseerr. Uses pre-configured quality profile
    (HD-1080p, Russian audio).
    Args: tmdb_id — TMDb ID, type — 'movie' or 'tv', seasons — 'all' or comma-separated
    season numbers like '1,2,3' (for TV only).
    """
    if type == "movie":
        payload = {"mediaType": "movie", "mediaId": tmdb_id}
    else:
        payload = {"mediaType": "tv", "mediaId": tmdb_id}
        if seasons == "all":
            payload["seasons"] = "all"
        else:
            payload["seasons"] = [int(s.strip()) for s in seasons.split(",")]
    resp = requests.post(f"{JELLYSEERR_URL}/api/v1/request",
                         headers={**_jellyseerr_headers(), "Content-Type": "application/json"},
                         json=payload, timeout=TIMEOUT)
    if resp.status_code in (200, 201):
        req = resp.json()
        return json.dumps({
            "ok": True,
            "requestId": req.get("id"),
            "message": f"Request created! Quality: HD-1080p, Russian audio. Ready in a few hours.",
        }, ensure_ascii=False)
    if resp.status_code == 409:
        return "This movie/series is already requested."
    return _status_message(resp, "movie/series")


@mcp.tool()
@_safe_request
def media_request_status(limit: int = 10) -> str:
    """Show recent media requests and their status.
    Args: limit — number of requests to show (default 10).
    """
    params = {"take": limit, "sort": "added", "sortDirection": "desc"}
    resp = requests.get(f"{JELLYSEERR_URL}/api/v1/request",
                        headers=_jellyseerr_headers(), params=params, timeout=TIMEOUT)
    if resp.status_code != 200:
        return _status_message(resp, "requests")
    results = []
    status_map = {1: "pending_approval", 2: "approved", 3: "declined"}
    media_status_map = {1: "unknown", 2: "pending", 3: "processing",
                        4: "partially_available", 5: "available"}
    for req in resp.json().get("results", []):
        media = req.get("media", {})
        entry = {
            "requestId": req.get("id"),
            "type": req.get("type", ""),
            "status": status_map.get(req.get("status"), "unknown"),
            "mediaStatus": media_status_map.get(media.get("status"), "unknown"),
            "title": (media.get("title") or media.get("name")
                      or req.get("media", {}).get("tmdbId", "")),
            "createdAt": req.get("createdAt", "")[:10],
        }
        results.append(entry)
    if not results:
        return "No active requests."
    return json.dumps(results, ensure_ascii=False, indent=2)


@mcp.tool()
@_safe_request
def library_recently_added(limit: int = 10) -> str:
    """Show recently added movies and shows in Jellyfin library.
    Args: limit — number of items (default 10).
    """
    params = {
        **_jellyfin_params(),
        "Limit": limit,
        "SortBy": "DateCreated",
        "SortOrder": "Descending",
        "IncludeItemTypes": "Movie,Series",
        "Recursive": "true",
        "Fields": "DateCreated,Overview",
    }
    resp = requests.get(f"{JELLYFIN_URL}/Items",
                        params=params, timeout=TIMEOUT)
    if resp.status_code != 200:
        return _status_message(resp, "library")
    results = []
    for item in resp.json().get("Items", [])[:limit]:
        results.append({
            "name": item.get("Name", ""),
            "type": item.get("Type", ""),
            "year": item.get("ProductionYear", ""),
            "added": (item.get("DateCreated") or "")[:10],
        })
    if not results:
        return "Library is empty."
    return json.dumps(results, ensure_ascii=False, indent=2)


@mcp.tool()
@_safe_request
def media_progress() -> str:
    """Show download progress for active media requests.
    Uses Radarr and Sonarr queue APIs (read-only).
    """
    items = []
    # Radarr queue
    resp = requests.get(f"{RADARR_URL}/api/v3/queue",
                        headers=_arr_headers(RADARR_API_KEY),
                        params={"pageSize": 20}, timeout=TIMEOUT)
    if resp.status_code == 200:
        for rec in resp.json().get("records", []):
            title = rec.get("title", "")
            size = rec.get("size", 0)
            sizeleft = rec.get("sizeleft", 0)
            pct = round((1 - sizeleft / size) * 100, 1) if size > 0 else 0
            eta = rec.get("timeleft", "unknown")
            items.append({
                "title": title, "type": "movie",
                "progress": f"{pct}%", "eta": eta,
                "status": rec.get("status", ""),
            })
    # Sonarr queue
    resp = requests.get(f"{SONARR_URL}/api/v3/queue",
                        headers=_arr_headers(SONARR_API_KEY),
                        params={"pageSize": 20}, timeout=TIMEOUT)
    if resp.status_code == 200:
        for rec in resp.json().get("records", []):
            title = rec.get("title", "")
            size = rec.get("size", 0)
            sizeleft = rec.get("sizeleft", 0)
            pct = round((1 - sizeleft / size) * 100, 1) if size > 0 else 0
            eta = rec.get("timeleft", "unknown")
            items.append({
                "title": title, "type": "tv",
                "progress": f"{pct}%", "eta": eta,
                "status": rec.get("status", ""),
            })
    if not items:
        return "No active downloads."
    return json.dumps(items, ensure_ascii=False, indent=2)


@mcp.tool()
@_safe_request
def media_similar(tmdb_id: int, type: str = "movie", limit: int = 5) -> str:
    """Get similar movies/shows based on a title. Uses TMDb via Jellyseerr.
    Args: tmdb_id — TMDb ID, type — 'movie' or 'tv', limit — max results.
    """
    endpoint = "movie" if type == "movie" else "tv"
    resp = requests.get(f"{JELLYSEERR_URL}/api/v1/{endpoint}/{tmdb_id}/similar",
                        headers=_jellyseerr_headers(),
                        params={"page": 1, "language": "ru"}, timeout=TIMEOUT)
    if resp.status_code == 404:
        return f"Not found in TMDb (ID {tmdb_id})."
    if resp.status_code != 200:
        return _status_message(resp, "recommendations")
    results = []
    for item in resp.json().get("results", [])[:limit]:
        results.append({
            "tmdbId": item.get("id"),
            "title": item.get("title") or item.get("name", ""),
            "year": (item.get("releaseDate") or item.get("firstAirDate") or "")[:4],
            "overview": (item.get("overview") or "")[:150],
        })
    if not results:
        return "No similar titles found."
    return json.dumps(results, ensure_ascii=False, indent=2)


# ── SAFE book/audiobook tools (both modes) ───────────────────

@mcp.tool()
@_safe_request
def book_search_releases(query: str, type: str = "audiobook") -> str:
    """Search audiobook/ebook releases via Prowlarr (Russian trackers: RuTracker,
    Kinozal, NoNaMe, RuTor). Narrator/format are visible in the release title.
    Args: query — book title + author in Russian, type — 'audiobook' or 'ebook'.
    Returns top releases (guid, indexerId, title, size_mb, seeders). The agent picks
    one (e.g. by narrator) and passes guid+indexerId to book_grab.
    """
    if not query.strip():
        return "Error: empty query. Provide the book author and title."
    cat = BOOK_CATEGORIES.get(type, 3030)
    resp = requests.get(f"{PROWLARR_URL}/api/v1/search",
                        headers=_arr_headers(PROWLARR_API_KEY),
                        params={"query": query, "categories": cat,
                                "type": "search", "limit": 50}, timeout=TIMEOUT)
    if resp.status_code != 200:
        return _status_message(resp, "book search")
    releases = []
    for rel in resp.json():
        if cat not in [c.get("id") for c in (rel.get("categories") or [])]:
            continue
        releases.append({
            "guid": rel.get("guid", ""),
            "indexerId": rel.get("indexerId"),
            "indexer": rel.get("indexer", ""),
            "title": rel.get("title", ""),
            "size_mb": round(rel.get("size", 0) / 1e6),
            "seeders": rel.get("seeders", 0),
        })
    releases.sort(key=lambda r: -(r["seeders"] or 0))
    releases = releases[:20]
    if not releases:
        return f"Nothing found for “{query}” ({type})."
    return json.dumps(releases, ensure_ascii=False, indent=2)


@mcp.tool()
@_safe_request
def book_status() -> str:
    """Show book/audiobook download status (qB category [Abooks]) and whether each is
    already imported into Audiobookshelf (qB tag abk_done set by the importer)."""
    def _get():
        return requests.get(f"{QBIT_URL}/api/v2/torrents/info",
                            cookies=_qbit_cookies(),
                            params={"category": ABOOKS_CATEGORY}, timeout=TIMEOUT)
    resp = _get()
    if resp.status_code == 403:
        _qbit_login()
        resp = _get()
    if resp.status_code != 200:
        return _status_message(resp, "qBittorrent")
    items = []
    for t in resp.json():
        tags = [x.strip() for x in (t.get("tags") or "").split(",") if x.strip()]
        items.append({
            "hash": t.get("hash", "")[:12],
            "name": t.get("name", ""),
            "progress": f"{round(t.get('progress', 0) * 100)}%",
            "state": t.get("state", ""),
            "imported": "abk_done" in tags,
        })
    items.sort(key=lambda x: x["imported"])
    if not items:
        return "No active book downloads."
    return json.dumps(items, ensure_ascii=False, indent=2)


@mcp.tool()
@_safe_request
def book_library_recent(limit: int = 10) -> str:
    """Show recently added books/audiobooks in Audiobookshelf.
    Args: limit — number of items (default 10).
    """
    libs = requests.get(f"{ABS_URL}/api/libraries", headers=_abs_headers(), timeout=TIMEOUT)
    if libs.status_code != 200:
        return _status_message(libs, "Audiobookshelf")
    book_libs = [l for l in libs.json().get("libraries", []) if l.get("mediaType") == "book"]
    if not book_libs:
        return "No book libraries found in Audiobookshelf."
    results = []
    for lib in book_libs:
        r = requests.get(f"{ABS_URL}/api/libraries/{lib['id']}/items",
                         headers=_abs_headers(),
                         params={"sort": "addedAt", "desc": 1, "limit": limit}, timeout=TIMEOUT)
        if r.status_code != 200:
            continue
        for it in r.json().get("results", []):
            m = it.get("media", {}).get("metadata", {})
            results.append({"title": m.get("title", ""),
                            "author": m.get("authorName", ""),
                            "library": lib.get("name", "")})
    results = results[:limit]
    if not results:
        return "Book library is empty."
    return json.dumps(results, ensure_ascii=False, indent=2)


def _abooks_hashes() -> set:
    """Current torrent hashes in the [Abooks] category."""
    def _get():
        return requests.get(f"{QBIT_URL}/api/v2/torrents/info",
                            cookies=_qbit_cookies(),
                            params={"category": ABOOKS_CATEGORY}, timeout=TIMEOUT)
    r = _get()
    if r.status_code == 403:
        _qbit_login()
        r = _get()
    return {t.get("hash") for t in r.json()} if r.status_code == 200 else set()


def _all_hashes() -> set:
    """All current torrent hashes (any category). Prowlarr grabs land in the
    default download-client category; music_grab snapshots the full list, finds
    the new hash, then routes it to [Music] — so this is category-agnostic."""
    def _get():
        return requests.get(f"{QBIT_URL}/api/v2/torrents/info",
                            cookies=_qbit_cookies(), timeout=TIMEOUT)
    r = _get()
    if r.status_code == 403:
        _qbit_login()
        r = _get()
    return {t.get("hash") for t in r.json()} if r.status_code == 200 else set()


def _music_guards() -> str:
    """#12: refuse a music grab if the download disk is low on space or too many
    music downloads are already in flight. Returns an error message if a guard
    trips, else ''. Best-effort: a probe failure never blocks the grab."""
    def _get(path, **params):
        r = requests.get(f"{QBIT_URL}/api/v2{path}", cookies=_qbit_cookies(),
                         params=params, timeout=TIMEOUT)
        if r.status_code == 403:
            _qbit_login()
            r = requests.get(f"{QBIT_URL}/api/v2{path}", cookies=_qbit_cookies(),
                             params=params, timeout=TIMEOUT)
        return r
    try:
        free = _get("/sync/maindata").json().get("server_state", {}).get("free_space_on_disk", 0)
        if free and free < MUSIC_MIN_FREE_GB * 1024 ** 3:
            return (f"Low disk space ({round(free / 1024 ** 3)} GB free, "
                    f"need ≥{MUSIC_MIN_FREE_GB}). Grab cancelled.")
    except Exception:
        pass
    try:
        r = _get("/torrents/info", category=MUSIC_CATEGORY)
        inflight = sum(1 for t in r.json() if t.get("progress", 0) < 1.0) if r.status_code == 200 else 0
        if inflight >= MUSIC_MAX_INFLIGHT:
            return (f"Too many active music downloads ({inflight}). "
                    "Wait for the current ones to finish.")
    except Exception:
        pass
    return ""


def _subsonic_get(endpoint: str, **extra):
    """Call a Navidrome Subsonic endpoint with salted-token auth
    (t=md5(password+salt)). Navidrome has no bearer token; this is the
    confirmed auth method (spike T1)."""
    import hashlib as _hl
    salt = "mediamcp"
    token = _hl.md5((NAVIDROME_PASS + salt).encode()).hexdigest()
    params = {"u": NAVIDROME_USER, "t": token, "s": salt,
              "v": "1.16.1", "c": "media-mcp", "f": "json", **extra}
    return requests.get(f"{NAVIDROME_URL}/rest/{endpoint}", params=params, timeout=TIMEOUT)


@mcp.tool()
@_safe_request
def book_grab(guid: str, indexer_id: int, author: str, title: str,
              type: str = "audiobook", confirm: bool = False) -> str:
    """Order a chosen book/audiobook release: grab it into qBittorrent and tag it so
    the importer hardlinks it into Audiobookshelf under “Author/Title”. Available in
    safe mode too (Max can order audiobooks, like he requests movies).
    Args: guid + indexer_id from book_search_releases; author, title — canonical
    Russian names for the library folder; type — 'audiobook' or 'ebook';
    confirm — False for dry-run, True to execute.
    """
    import base64 as _b64
    import time as _time
    author_s = _sanitize_component(author)
    title_s = _sanitize_component(title)
    if not confirm:
        return (f"DRY-RUN: would download release guid={guid} (indexer {indexer_id}) and place "
                f"it as “{author_s}/{title_s}” ({type}). Call with confirm=True.")
    before = _abooks_hashes()
    resp = requests.post(f"{PROWLARR_URL}/api/v1/search",
                         headers={**_arr_headers(PROWLARR_API_KEY), "Content-Type": "application/json"},
                         json={"guid": guid, "indexerId": indexer_id}, timeout=30)
    if resp.status_code not in (200, 201):
        return _status_message(resp, "Prowlarr grab")
    new_hash = ""
    for _ in range(10):
        _time.sleep(1.5)
        diff = _abooks_hashes() - before
        if diff:
            new_hash = sorted(diff)[0]
            break
    if not new_hash:
        return ("OK: release sent to qBittorrent, but the torrent is not visible in the queue yet. "
                "The importer tag was not set — check book_status and retry if needed.")
    # carry the requester's chat (this bot's NOTIFY_CHAT_ID) so the importer can
    # send the "book ready" notification to whoever ordered (primary user via
    # Sebastian, secondary user via Max), not a single hard-coded chat.
    notify_chat = os.environ.get("NOTIFY_CHAT_ID", "")
    payload = f"{author_s}/{title_s}/{type}/{notify_chat}"
    tag = "abk:" + _b64.urlsafe_b64encode(payload.encode()).decode()
    requests.post(f"{QBIT_URL}/api/v2/torrents/addTags", cookies=_qbit_cookies(),
                  data={"hashes": new_hash, "tags": tag}, timeout=TIMEOUT)
    return json.dumps({
        "ok": True, "hash": new_hash[:12], "placement": f"{author_s}/{title_s}",
        "message": f"Ordered: “{title_s}” — {author_s}. Will appear in Audiobookshelf after download.",
    }, ensure_ascii=False)


# ── FULL tools (only when MODE == "full") ────────────────────

if MODE == "full":

    # ── Radarr ───────────────────────────────────────────────

    @mcp.tool()
    @_safe_request
    def radarr_queue() -> str:
        """Show Radarr download queue — active downloads, status, progress."""
        resp = requests.get(f"{RADARR_URL}/api/v3/queue",
                            headers=_arr_headers(RADARR_API_KEY),
                            params={"pageSize": 50}, timeout=TIMEOUT)
        if resp.status_code != 200:
            return _status_message(resp, "Radarr queue")
        records = []
        for rec in resp.json().get("records", []):
            size = rec.get("size", 0)
            sizeleft = rec.get("sizeleft", 0)
            pct = round((1 - sizeleft / size) * 100, 1) if size > 0 else 0
            records.append({
                "id": rec.get("id"),
                "title": rec.get("title", ""),
                "progress": f"{pct}%",
                "eta": rec.get("timeleft", ""),
                "status": rec.get("status", ""),
                "trackedDownloadStatus": rec.get("trackedDownloadStatus", ""),
            })
        return json.dumps(records, ensure_ascii=False, indent=2) if records else "Radarr queue is empty."

    @mcp.tool()
    @_safe_request
    def radarr_search_releases(movie_id: int) -> str:
        """Search for available releases for a Radarr movie.
        Args: movie_id — Radarr internal movie ID (not tmdbId).
        """
        resp = requests.get(f"{RADARR_URL}/api/v3/release",
                            headers=_arr_headers(RADARR_API_KEY),
                            params={"movieId": movie_id}, timeout=TIMEOUT)
        if resp.status_code != 200:
            return _status_message(resp, "Radarr releases")
        releases = []
        for rel in resp.json()[:20]:
            releases.append({
                "guid": rel.get("guid", ""),
                "title": rel.get("title", ""),
                "size_gb": round(rel.get("size", 0) / 1e9, 2),
                "quality": rel.get("quality", {}).get("quality", {}).get("name", ""),
                "indexer": rel.get("indexer", ""),
                "seeders": rel.get("seeders", 0),
                "languages": [l.get("name", "") for l in rel.get("languages", [])],
            })
        return json.dumps(releases, ensure_ascii=False, indent=2) if releases else "No releases found."

    @mcp.tool()
    @_safe_request
    def radarr_grab_release(guid: str, indexer_id: int, confirm: bool = False) -> str:
        """Grab a specific release in Radarr.
        Args: guid — release guid from radarr_search_releases, indexer_id — indexer ID,
        confirm — False for dry-run preview, True to execute.
        """
        if not confirm:
            return f"DRY-RUN: would download release guid={guid} via indexer {indexer_id}. Call with confirm=True to execute."
        payload = {"guid": guid, "indexerId": indexer_id}
        resp = requests.post(f"{RADARR_URL}/api/v3/release",
                             headers={**_arr_headers(RADARR_API_KEY), "Content-Type": "application/json"},
                             json=payload, timeout=TIMEOUT)
        if resp.status_code in (200, 201):
            return "OK: release added to the download queue."
        return _status_message(resp, "Radarr grab")

    @mcp.tool()
    @_safe_request
    def radarr_delete_movie(movie_id: int, delete_files: bool = False, confirm: bool = False) -> str:
        """Delete a movie from Radarr.
        Args: movie_id — Radarr internal ID, delete_files — also delete downloaded files,
        confirm — False for dry-run, True to execute.
        """
        if not confirm:
            action = "removed from Radarr + files deleted" if delete_files else "removed from Radarr (files kept)"
            return f"DRY-RUN: movie ID {movie_id} will be {action}. Call with confirm=True."
        resp = requests.delete(f"{RADARR_URL}/api/v3/movie/{movie_id}",
                               headers=_arr_headers(RADARR_API_KEY),
                               params={"deleteFiles": str(delete_files).lower()},
                               timeout=TIMEOUT)
        if resp.status_code in (200, 204):
            return f"OK: movie {movie_id} deleted."
        return _status_message(resp, "Radarr delete")

    # ── Sonarr ───────────────────────────────────────────────

    @mcp.tool()
    @_safe_request
    def sonarr_queue() -> str:
        """Show Sonarr download queue — active TV downloads, status, progress."""
        resp = requests.get(f"{SONARR_URL}/api/v3/queue",
                            headers=_arr_headers(SONARR_API_KEY),
                            params={"pageSize": 50}, timeout=TIMEOUT)
        if resp.status_code != 200:
            return _status_message(resp, "Sonarr queue")
        records = []
        for rec in resp.json().get("records", []):
            size = rec.get("size", 0)
            sizeleft = rec.get("sizeleft", 0)
            pct = round((1 - sizeleft / size) * 100, 1) if size > 0 else 0
            records.append({
                "id": rec.get("id"),
                "title": rec.get("title", ""),
                "progress": f"{pct}%",
                "eta": rec.get("timeleft", ""),
                "status": rec.get("status", ""),
            })
        return json.dumps(records, ensure_ascii=False, indent=2) if records else "Sonarr queue is empty."

    @mcp.tool()
    @_safe_request
    def sonarr_search_releases(series_id: int) -> str:
        """Search for available releases for a Sonarr series.
        Args: series_id — Sonarr internal series ID.
        """
        resp = requests.get(f"{SONARR_URL}/api/v3/release",
                            headers=_arr_headers(SONARR_API_KEY),
                            params={"seriesId": series_id}, timeout=TIMEOUT)
        if resp.status_code != 200:
            return _status_message(resp, "Sonarr releases")
        releases = []
        for rel in resp.json()[:20]:
            releases.append({
                "guid": rel.get("guid", ""),
                "title": rel.get("title", ""),
                "size_gb": round(rel.get("size", 0) / 1e9, 2),
                "quality": rel.get("quality", {}).get("quality", {}).get("name", ""),
                "indexer": rel.get("indexer", ""),
                "seeders": rel.get("seeders", 0),
                "languages": [l.get("name", "") for l in rel.get("languages", [])],
            })
        return json.dumps(releases, ensure_ascii=False, indent=2) if releases else "No releases found."

    @mcp.tool()
    @_safe_request
    def sonarr_grab_release(guid: str, indexer_id: int, confirm: bool = False) -> str:
        """Grab a specific release in Sonarr.
        Args: guid — release guid, indexer_id — indexer ID,
        confirm — False for dry-run, True to execute.
        """
        if not confirm:
            return f"DRY-RUN: would download release guid={guid}. Call with confirm=True."
        payload = {"guid": guid, "indexerId": indexer_id}
        resp = requests.post(f"{SONARR_URL}/api/v3/release",
                             headers={**_arr_headers(SONARR_API_KEY), "Content-Type": "application/json"},
                             json=payload, timeout=TIMEOUT)
        if resp.status_code in (200, 201):
            return "OK: release added to the queue."
        return _status_message(resp, "Sonarr grab")

    @mcp.tool()
    @_safe_request
    def sonarr_delete_series(series_id: int, delete_files: bool = False, confirm: bool = False) -> str:
        """Delete a series from Sonarr.
        Args: series_id — Sonarr internal ID, delete_files — also delete files,
        confirm — False for dry-run, True to execute.
        """
        if not confirm:
            action = "removed from Sonarr + files" if delete_files else "removed from Sonarr (files kept)"
            return f"DRY-RUN: series ID {series_id} will be {action}. Call with confirm=True."
        resp = requests.delete(f"{SONARR_URL}/api/v3/series/{series_id}",
                               headers=_arr_headers(SONARR_API_KEY),
                               params={"deleteFiles": str(delete_files).lower()},
                               timeout=TIMEOUT)
        if resp.status_code in (200, 204):
            return f"OK: series {series_id} deleted."
        return _status_message(resp, "Sonarr delete")

    # ── qBittorrent ──────────────────────────────────────────

    @mcp.tool()
    @_safe_request
    def qbit_list_torrents(category: str = "") -> str:
        """List active torrents in qBittorrent.
        Args: category — filter by category (optional).
        """
        params = {}
        if category:
            params["category"] = category
        resp = requests.get(f"{QBIT_URL}/api/v2/torrents/info",
                            cookies=_qbit_cookies(), params=params, timeout=TIMEOUT)
        if resp.status_code == 403:
            _qbit_login()
            resp = requests.get(f"{QBIT_URL}/api/v2/torrents/info",
                                cookies=_qbit_cookies(), params=params, timeout=TIMEOUT)
        if resp.status_code != 200:
            return _status_message(resp, "qBittorrent")
        torrents = []
        for t in resp.json()[:30]:
            torrents.append({
                "hash": t.get("hash", "")[:12],
                "name": t.get("name", ""),
                "progress": f"{round(t.get('progress', 0) * 100, 1)}%",
                "size_gb": round(t.get("size", 0) / 1e9, 2),
                "state": t.get("state", ""),
                "category": t.get("category", ""),
                "eta": t.get("eta", 0),
            })
        return json.dumps(torrents, ensure_ascii=False, indent=2) if torrents else "No torrents."

    @mcp.tool()
    @_safe_request
    def qbit_pause(hash: str) -> str:
        """Pause a torrent. Args: hash — torrent hash (full or partial from qbit_list)."""
        resp = requests.post(f"{QBIT_URL}/api/v2/torrents/pause",
                             cookies=_qbit_cookies(), data={"hashes": hash}, timeout=TIMEOUT)
        if resp.status_code == 200:
            return f"OK: torrent {hash} paused."
        return _status_message(resp, "qBit pause")

    @mcp.tool()
    @_safe_request
    def qbit_resume(hash: str) -> str:
        """Resume a paused torrent. Args: hash — torrent hash."""
        resp = requests.post(f"{QBIT_URL}/api/v2/torrents/resume",
                             cookies=_qbit_cookies(), data={"hashes": hash}, timeout=TIMEOUT)
        if resp.status_code == 200:
            return f"OK: torrent {hash} resumed."
        return _status_message(resp, "qBit resume")

    @mcp.tool()
    @_safe_request
    def qbit_delete(hash: str, delete_files: bool = False, confirm: bool = False) -> str:
        """Delete a torrent from qBittorrent.
        Args: hash — torrent hash, delete_files — also delete downloaded data,
        confirm — False for dry-run, True to execute.
        """
        if not confirm:
            action = "removed + files deleted" if delete_files else "removed (files kept)"
            return f"DRY-RUN: torrent {hash} will be {action}. Call with confirm=True."
        resp = requests.post(f"{QBIT_URL}/api/v2/torrents/delete",
                             cookies=_qbit_cookies(),
                             data={"hashes": hash, "deleteFiles": str(delete_files).lower()},
                             timeout=TIMEOUT)
        if resp.status_code == 200:
            return f"OK: torrent {hash} deleted."
        return _status_message(resp, "qBit delete")

    # ── Prowlarr ─────────────────────────────────────────────

    @mcp.tool()
    @_safe_request
    def prowlarr_indexer_status() -> str:
        """Show status of all Prowlarr indexers."""
        resp = requests.get(f"{PROWLARR_URL}/api/v1/indexer",
                            headers=_arr_headers(PROWLARR_API_KEY), timeout=TIMEOUT)
        if resp.status_code != 200:
            return _status_message(resp, "Prowlarr")
        indexers = []
        for idx in resp.json():
            indexers.append({
                "id": idx.get("id"),
                "name": idx.get("name", ""),
                "protocol": idx.get("protocol", ""),
                "enable": idx.get("enable", False),
            })
        return json.dumps(indexers, ensure_ascii=False, indent=2) if indexers else "No indexers."

    @mcp.tool()
    @_safe_request
    def prowlarr_test_indexer(indexer_id: int) -> str:
        """Test a Prowlarr indexer connection. Args: indexer_id — indexer ID."""
        resp = requests.post(f"{PROWLARR_URL}/api/v1/indexer/test",
                             headers={**_arr_headers(PROWLARR_API_KEY), "Content-Type": "application/json"},
                             json={"id": indexer_id}, timeout=TIMEOUT)
        if resp.status_code == 200:
            return f"OK: indexer {indexer_id} is working."
        return f"FAIL: indexer {indexer_id} — {resp.text[:200]}"

    # ── Jellyfin (admin) ─────────────────────────────────────

    @mcp.tool()
    @_safe_request
    def jellyfin_scan_library(library_name: str = "") -> str:
        """Trigger a Jellyfin library scan. Args: library_name — specific library or all."""
        resp = requests.post(f"{JELLYFIN_URL}/Library/Refresh",
                             params=_jellyfin_params(), timeout=30)
        if resp.status_code == 204:
            return "OK: library scan started."
        return _status_message(resp, "Jellyfin scan")

    @mcp.tool()
    @_safe_request
    def jellyfin_refresh_item(item_id: str) -> str:
        """Refresh metadata for a specific Jellyfin item. Args: item_id — Jellyfin item ID."""
        resp = requests.post(f"{JELLYFIN_URL}/Items/{item_id}/Refresh",
                             params={**_jellyfin_params(), "Recursive": "true",
                                     "MetadataRefreshMode": "FullRefresh"},
                             timeout=30)
        if resp.status_code == 204:
            return f"OK: metadata for {item_id} is refreshing."
        return _status_message(resp, "Jellyfin refresh")

    # ── Books / audiobooks (cancel — destructive, full only) ──

    @mcp.tool()
    @_safe_request
    def book_cancel(hash: str, delete_files: bool = True, confirm: bool = False) -> str:
        """Cancel a book/audiobook download (qB category [Abooks]).
        Args: hash — from book_status, delete_files — also remove downloaded data,
        confirm — False for dry-run, True to execute.
        """
        if not confirm:
            extra = " + files deleted" if delete_files else ""
            return f"DRY-RUN: torrent {hash} will be cancelled{extra}. Call with confirm=True."
        def _del():
            return requests.post(f"{QBIT_URL}/api/v2/torrents/delete", cookies=_qbit_cookies(),
                                 data={"hashes": hash, "deleteFiles": str(delete_files).lower()},
                                 timeout=TIMEOUT)
        resp = _del()
        if resp.status_code == 403:
            _qbit_login()
            resp = _del()
        if resp.status_code == 200:
            return f"OK: download {hash} cancelled."
        return _status_message(resp, "book cancel")

    # ── Music (Navidrome pipeline, mirror of book_*; full-only — not for Max) ──

    @mcp.tool()
    @_safe_request
    def music_search_releases(query: str) -> str:
        """Search music releases via Prowlarr (Russian trackers: RuTracker, Kinozal,
        NoNaMe). Format/quality (FLAC/MP3) are visible in the release title.
        Args: query — artist + album in Russian (or any language).
        Returns top releases (guid, indexerId, title, size_mb, seeders). The agent
        picks one and passes guid+indexerId to music_grab.
        """
        if not query.strip():
            return "Error: empty query. Provide an artist and album."
        resp = requests.get(f"{PROWLARR_URL}/api/v1/search",
                            headers=_arr_headers(PROWLARR_API_KEY),
                            params={"query": query, "categories": MUSIC_CATEGORY_ID,
                                    "type": "search", "limit": 50}, timeout=TIMEOUT)
        if resp.status_code != 200:
            return _status_message(resp, "music search")
        releases = []
        for rel in resp.json():
            cats = [c.get("id") for c in (rel.get("categories") or [])]
            if not any(str(c).startswith("30") for c in cats):
                continue
            releases.append({
                "guid": rel.get("guid", ""),
                "indexerId": rel.get("indexerId"),
                "indexer": rel.get("indexer", ""),
                "title": rel.get("title", ""),
                "size_mb": round(rel.get("size", 0) / 1e6),
                "seeders": rel.get("seeders", 0),
            })
        releases.sort(key=lambda r: -(r["seeders"] or 0))
        releases = releases[:20]
        if not releases:
            return f"Nothing found for “{query}”."
        return json.dumps(releases, ensure_ascii=False, indent=2)

    @mcp.tool()
    @_safe_request
    def music_status() -> str:
        """Show music download status (qB category [Music]) and whether each is
        already imported into Navidrome (qB tag mus_done set by the importer)."""
        def _get():
            return requests.get(f"{QBIT_URL}/api/v2/torrents/info",
                                cookies=_qbit_cookies(),
                                params={"category": MUSIC_CATEGORY}, timeout=TIMEOUT)
        resp = _get()
        if resp.status_code == 403:
            _qbit_login()
            resp = _get()
        if resp.status_code != 200:
            return _status_message(resp, "qBittorrent")
        items = []
        for t in resp.json():
            tags = [x.strip() for x in (t.get("tags") or "").split(",") if x.strip()]
            items.append({
                "hash": t.get("hash", "")[:12],
                "name": t.get("name", ""),
                "progress": f"{round(t.get('progress', 0) * 100)}%",
                "state": t.get("state", ""),
                "imported": "mus_done" in tags,
            })
        items.sort(key=lambda x: x["imported"])
        if not items:
            return "No active music downloads."
        return json.dumps(items, ensure_ascii=False, indent=2)

    @mcp.tool()
    @_safe_request
    def music_library_recent(limit: int = 10) -> str:
        """Show recently added albums in Navidrome (Subsonic getAlbumList2 newest).
        Args: limit — number of albums (default 10).
        """
        resp = _subsonic_get("getAlbumList2", type="newest", size=limit)
        if resp.status_code != 200:
            return _status_message(resp, "Navidrome")
        body = resp.json().get("subsonic-response", {})
        if body.get("status") != "ok":
            return f"Navidrome error: {body.get('error', {}).get('message', 'unknown')}"
        albums = body.get("albumList2", {}).get("album", [])
        results = [{"album": a.get("name", ""), "artist": a.get("artist", ""),
                    "year": a.get("year", ""), "songs": a.get("songCount", 0)}
                   for a in albums[:limit]]
        if not results:
            return "Navidrome library is empty."
        return json.dumps(results, ensure_ascii=False, indent=2)

    @mcp.tool()
    @_safe_request
    def music_grab(guid: str, indexer_id: int, artist: str, album: str,
                   kind: str = "album", confirm: bool = False) -> str:
        """Order a chosen music release: grab it into qBittorrent, route it to the
        [Music] category, and tag it so the importer hardlinks it into Navidrome.
        State lives entirely in the qB tag (no DB).
        Args: guid + indexer_id from music_search_releases; artist, album — canonical
        names for the library folder; kind — 'album' (single, → Artist/Album/) or
        'discography' (one torrent with many albums → contents go under Artist/, since
        Navidrome groups by tags); confirm — False for dry-run, True to execute.
        """
        import base64 as _b64
        import time as _time
        artist_s = _sanitize_component(artist)
        album_s = _sanitize_component(album)
        kind = kind if kind in ("album", "discography") else "album"
        placement = artist_s if kind == "discography" else f"{artist_s}/{album_s}"
        if not confirm:
            note = " (discography — can be tens of GB)" if kind == "discography" else ""
            return (f"DRY-RUN: would download release guid={guid} (indexer {indexer_id}) and place "
                    f"it as “{placement}”{note}. Call with confirm=True.")
        guard = _music_guards()
        if guard:
            return guard
        before = _all_hashes()
        resp = requests.post(f"{PROWLARR_URL}/api/v1/search",
                             headers={**_arr_headers(PROWLARR_API_KEY), "Content-Type": "application/json"},
                             json={"guid": guid, "indexerId": indexer_id}, timeout=30)
        if resp.status_code not in (200, 201):
            return _status_message(resp, "Prowlarr grab")
        new_hash = ""
        for _ in range(10):
            _time.sleep(1.5)
            diff = _all_hashes() - before
            if diff:
                new_hash = sorted(diff)[0]
                break
        if not new_hash:
            return ("OK: release sent to qBittorrent, but the torrent is not visible yet. The tag/category "
                    "were not set — check music_status and retry if needed.")
        # route to [Music] (set before download finishes → lands in [Music] save path)
        requests.post(f"{QBIT_URL}/api/v2/torrents/setCategory", cookies=_qbit_cookies(),
                      data={"hashes": new_hash, "category": MUSIC_CATEGORY}, timeout=TIMEOUT)
        # carry the requester's chat so the importer notifies whoever ordered + the kind
        notify_chat = os.environ.get("NOTIFY_CHAT_ID", "")
        payload = f"{artist_s}/{album_s}/{notify_chat}/{kind}"
        tag = "mus:" + _b64.urlsafe_b64encode(payload.encode()).decode()
        requests.post(f"{QBIT_URL}/api/v2/torrents/addTags", cookies=_qbit_cookies(),
                      data={"hashes": new_hash, "tags": tag}, timeout=TIMEOUT)
        what = f"discography “{artist_s}”" if kind == "discography" else f"“{album_s}” — {artist_s}"
        return json.dumps({
            "ok": True, "hash": new_hash[:12], "placement": placement, "kind": kind,
            "message": f"Ordered: {what}. Will appear in Navidrome after download.",
        }, ensure_ascii=False)

    @mcp.tool()
    @_safe_request
    def music_cancel(hash: str, delete_files: bool = True, confirm: bool = False) -> str:
        """Cancel a music download (qB category [Music]).
        Args: hash — from music_status, delete_files — also remove downloaded data,
        confirm — False for dry-run, True to execute.
        """
        if not confirm:
            extra = " + files deleted" if delete_files else ""
            return f"DRY-RUN: torrent {hash} will be cancelled{extra}. Call with confirm=True."
        def _del():
            return requests.post(f"{QBIT_URL}/api/v2/torrents/delete", cookies=_qbit_cookies(),
                                 data={"hashes": hash, "deleteFiles": str(delete_files).lower()},
                                 timeout=TIMEOUT)
        resp = _del()
        if resp.status_code == 403:
            _qbit_login()
            resp = _del()
        if resp.status_code == 200:
            return f"OK: download {hash} cancelled."
        return _status_message(resp, "music cancel")

    @mcp.tool()
    def music_bandcamp(url: str, artist: str, confirm: bool = False) -> str:
        """Download a Bandcamp album or whole-artist discography into Navidrome via
        yt-dlp (a SEPARATE path from torrents — for artists not on the trackers).
        Bandcamp's public stream is ~128 kbps MP3 (no purchase); tags are clean so
        Navidrome groups by them. Fire-and-forget: returns immediately, the music
        appears in Navidrome a few minutes later.
        Args: url — a https://...bandcamp.com album or artist URL; artist — canonical
        name for the library folder; confirm — False for dry-run, True to execute.
        """
        import shlex as _sh
        import subprocess as _sp
        import urllib.parse as _up
        u = (url or "").strip()
        p = _up.urlparse(u)
        if not (p.scheme in ("http", "https") and p.netloc.endswith("bandcamp.com")):
            return "Error: need a URL like https://…bandcamp.com (album or artist)."
        artist_s = _sanitize_component(artist)
        if not confirm:
            return (f"DRY-RUN: would download from Bandcamp {u} as “{artist_s}” (~128 kbps MP3, "
                    "stream without purchase). Call with confirm=True.")
        chat = os.environ.get("NOTIFY_CHAT_ID", "") or "silent"
        remote = ("set -a; . " + BANDCAMP_ENV + " 2>/dev/null; set +a; "
                  "nohup python3 " + BANDCAMP_REMOTE + " "
                  + _sh.quote(u) + " " + _sh.quote(artist_s) + " " + _sh.quote(chat)
                  + " >> /tmp/bandcamp-import.log 2>&1 &")
        ssh_cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new",
                   "-o", "ConnectTimeout=8", BANDCAMP_SSH_HOST, remote]
        try:
            _sp.run(ssh_cmd, timeout=20, check=False)
        except Exception as e:
            return f"Error starting the download on media-nas: {type(e).__name__} — {e}"
        return json.dumps({
            "ok": True, "artist": artist_s, "source": "bandcamp", "quality": "~128k mp3",
            "message": f"Started downloading “{artist_s}” from Bandcamp (~128k). Will appear in Navidrome in a few minutes.",
        }, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
