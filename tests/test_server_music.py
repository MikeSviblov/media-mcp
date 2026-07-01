"""Tests for the music MCP tools in server.py. Skipped if the mcp package is absent."""
import base64
import json
import os

import pytest

pytest.importorskip("mcp")
os.environ.setdefault("MEDIA_MCP_MODE", "full")

import server  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402


def _resp(status=200, payload=None, text=""):
    m = MagicMock()
    m.status_code = status
    m.json = lambda: payload
    m.text = text
    return m


def _cat(*ids):
    return [{"id": i} for i in ids]


# ── music_search_releases ────────────────────────────────────

def test_music_search_filters_category_and_sorts_by_seeders(monkeypatch):
    payload = [
        {"guid": "g1", "indexerId": 1, "indexer": "RuTracker",
         "title": "Кино - Группа крови, FLAC", "size": 360 * 10**6,
         "seeders": 5, "categories": _cat(3000)},
        {"guid": "g2", "indexerId": 1, "indexer": "NoNaMe",
         "title": "Кино - Молнии Индры, FLAC", "size": 380 * 10**6,
         "seeders": 23, "categories": _cat(3010)},
        {"guid": "gx", "indexerId": 2, "indexer": "X", "title": "movie",
         "size": 0, "seeders": 99, "categories": _cat(2000)},  # wrong category
    ]
    monkeypatch.setattr(server.requests, "get", lambda *a, **k: _resp(200, payload))
    out = json.loads(server.music_search_releases("кино"))
    assert [r["guid"] for r in out] == ["g2", "g1"]   # seeders desc, movie filtered out
    assert out[0]["size_mb"] == 380
    assert "Молнии Индры" in out[0]["title"]


def test_music_search_empty_query():
    assert "пустой запрос" in server.music_search_releases("  ")


def test_music_search_no_results(monkeypatch):
    monkeypatch.setattr(server.requests, "get", lambda *a, **k: _resp(200, []))
    assert "Ничего не найдено" in server.music_search_releases("zzz")


# ── music_grab ───────────────────────────────────────────────

def test_music_grab_dry_run_sanitizes_placement():
    # album carries a slash (path-injection attempt); must collapse to one component
    msg = server.music_grab("g", 1, "Кино", "Группа крови/evil", confirm=False)
    assert "DRY-RUN" in msg
    placement = msg.split("«")[1].split("»")[0]
    assert placement.count("/") == 1            # only the artist/album separator survives
    assert placement.startswith("Кино/")


def test_music_grab_tags_and_routes_new_torrent(monkeypatch):
    state = {"tags": None, "category": None, "phase": "before"}

    def fake_get(url, **kw):
        if "torrents/info" in url:
            return _resp(200, [] if state["phase"] == "before" else [{"hash": "NEWHASH"}])
        return _resp(200, [])

    def fake_post(url, **kw):
        if "auth/login" in url:
            r = _resp(204)
            r.cookies = MagicMock()
            r.cookies.get_dict = lambda: {"QBT_SID_8081": "sid"}
            return r
        if "/api/v1/search" in url:        # Prowlarr grab succeeds, flips phase
            state["phase"] = "after"
            return _resp(200, {})
        if "setCategory" in url:
            state["category"] = kw.get("data", {}).get("category")
            return _resp(200, text="")
        if "addTags" in url:
            state["tags"] = kw.get("data", {}).get("tags")
            return _resp(200, text="")
        return _resp(200, text="Ok.")

    monkeypatch.setattr(server.requests, "get", fake_get)
    monkeypatch.setattr(server.requests, "post", fake_post)
    import time
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    out = json.loads(server.music_grab("guid-1", 1, "Кино", "Молнии Индры", confirm=True))
    assert out["ok"] is True
    assert out["hash"] == "NEWHASH"[:12]
    assert state["category"] == server.MUSIC_CATEGORY        # routed to [Music]
    assert state["tags"].startswith("mus:")
    decoded = base64.urlsafe_b64decode(state["tags"][4:].encode()).decode()
    # payload is artist/album[/notify_chat]; chat empty here (no NOTIFY_CHAT_ID)
    assert decoded.split("/")[:2] == ["Кино", "Молнии Индры"]


def test_music_grab_dry_run_discography_note():
    msg = server.music_grab("g", 1, "Pink Floyd", "Discography", kind="discography", confirm=False)
    assert "DRY-RUN" in msg and "дискография" in msg
    # discography placement is the artist only (no album wrapper)
    placement = msg.split("«")[1].split("»")[0]
    assert placement == "Pink Floyd"


def test_music_grab_refuses_when_disk_low(monkeypatch):
    # qB reports very low free space → grab refused before any Prowlarr call
    def fake_get(url, **kw):
        if "sync/maindata" in url:
            return _resp(200, {"server_state": {"free_space_on_disk": 1 * 1024**3}})  # 1 GB
        return _resp(200, [])
    grabbed = []
    monkeypatch.setattr(server.requests, "get", fake_get)
    monkeypatch.setattr(server.requests, "post",
                        lambda url, **kw: grabbed.append(url) or _resp(200, {}))
    monkeypatch.setattr(server, "_qbit_cookie_jar", {"QBT_SID_8081": "sid"})
    msg = server.music_grab("g", 1, "A", "B", confirm=True)
    assert "Мало места" in msg
    assert not any("/api/v1/search" in u for u in grabbed)   # no Prowlarr grab happened


def test_music_grab_prowlarr_error(monkeypatch):
    monkeypatch.setattr(server.requests, "get", lambda *a, **k: _resp(200, []))
    monkeypatch.setattr(server.requests, "post", lambda url, **k: _resp(500, text="boom"))
    msg = server.music_grab("g", 1, "A", "B", confirm=True)
    assert "недоступен" in msg or "Ошибка" in msg


# ── music_status ─────────────────────────────────────────────

def test_music_bandcamp_rejects_non_bandcamp_url():
    msg = server.music_bandcamp("https://evil.com/album/x", "Da'Ba", confirm=True)
    assert "bandcamp.com" in msg


def test_music_bandcamp_dry_run():
    msg = server.music_bandcamp("https://dababand.bandcamp.com/", "Da'Ba", confirm=False)
    assert "DRY-RUN" in msg and "128" in msg


def test_music_bandcamp_launches_ssh_job(monkeypatch):
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args
        class R: returncode = 0
        return R()
    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    import shlex
    out = json.loads(server.music_bandcamp("https://dababand.bandcamp.com/album/iii", "Da'Ba", confirm=True))
    assert out["ok"] is True and out["source"] == "bandcamp"
    # ssh to the NAS host, launching the remote importer with the url + artist
    assert captured["args"][0] == "ssh"
    remote = captured["args"][-1]
    assert "bandcamp_import.py" in remote
    # url + artist are shell-quoted in the remote command (injection-safe)
    assert shlex.quote("https://dababand.bandcamp.com/album/iii") in remote
    assert shlex.quote("Da'Ba") in remote


def test_music_status_reports_imported_flag(monkeypatch):
    torrents = [
        {"hash": "h1", "name": "A", "progress": 1.0, "state": "uploading", "tags": "mus:x,mus_done"},
        {"hash": "h2", "name": "B", "progress": 0.5, "state": "downloading", "tags": "mus:y"},
    ]

    def fake_get(url, **kw):
        return _resp(200, torrents) if "torrents/info" in url else _resp(200, [])
    monkeypatch.setattr(server.requests, "get", fake_get)
    monkeypatch.setattr(server, "_qbit_cookie_jar", {"QBT_SID_8081": "sid"})
    out = json.loads(server.music_status())
    byhash = {x["name"]: x for x in out}
    assert byhash["A"]["imported"] is True
    assert byhash["B"]["imported"] is False
