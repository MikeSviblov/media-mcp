"""Tests for the book MCP tools in server.py. Skipped if the mcp package is absent."""
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


# ── book_search_releases ─────────────────────────────────────

def test_book_search_filters_category_and_sorts_by_seeders(monkeypatch):
    payload = [
        {"guid": "g1", "indexerId": 1, "indexer": "RuTracker",
         "title": "Этвуд - Рассказ служанки [Елена Греб]", "size": 100 * 10**6,
         "seeders": 5, "categories": _cat(3030)},
        {"guid": "g2", "indexerId": 1, "indexer": "RuTracker",
         "title": "Этвуд - Заветы [Елена Греб]", "size": 200 * 10**6,
         "seeders": 20, "categories": _cat(3030)},
        {"guid": "gx", "indexerId": 2, "indexer": "X", "title": "movie",
         "size": 0, "seeders": 99, "categories": _cat(2000)},  # wrong category
    ]
    monkeypatch.setattr(server.requests, "get", lambda *a, **k: _resp(200, payload))
    out = json.loads(server.book_search_releases("этвуд", "audiobook"))
    assert [r["guid"] for r in out] == ["g2", "g1"]   # seeders desc, movie filtered out
    assert out[0]["size_mb"] == 200
    assert "Елена Греб" in out[0]["title"]


def test_book_search_empty_query():
    assert "пустой запрос" in server.book_search_releases("  ", "audiobook")


def test_book_search_no_results(monkeypatch):
    monkeypatch.setattr(server.requests, "get", lambda *a, **k: _resp(200, []))
    assert "Ничего не найдено" in server.book_search_releases("zzz", "audiobook")


# ── book_grab ────────────────────────────────────────────────

def test_book_grab_dry_run_sanitizes_placement():
    # input title carries a slash (path-injection attempt); must collapse to one component
    msg = server.book_grab("g", 1, "Фрэнк Герберт", "Дюна/злой", "audiobook", confirm=False)
    assert "DRY-RUN" in msg
    placement = msg.split("«")[1].split("»")[0]
    assert placement.count("/") == 1            # only the author/title separator survives
    assert placement.startswith("Фрэнк Герберт/")


def test_book_grab_tags_new_torrent(monkeypatch):
    state = {"tags": None, "phase": "before"}

    def fake_get(url, **kw):
        if "torrents/info" in url:
            # before grab -> empty; after grab -> one new torrent
            return _resp(200, [] if state["phase"] == "before" else [{"hash": "NEWHASH"}])
        return _resp(200, [])

    def fake_post(url, **kw):
        if "auth/login" in url:
            r = _resp(204)                       # qB 5.x returns 204 + QBT_SID cookie
            r.cookies = MagicMock()
            r.cookies.get_dict = lambda: {"QBT_SID_8081": "sid"}
            return r
        if "/api/v1/search" in url:        # Prowlarr grab succeeds, flips phase
            state["phase"] = "after"
            return _resp(200, {})
        if "addTags" in url:
            state["tags"] = kw.get("data", {}).get("tags")
            return _resp(200, text="")
        return _resp(200, text="Ok.")

    monkeypatch.setattr(server.requests, "get", fake_get)
    monkeypatch.setattr(server.requests, "post", fake_post)
    import time
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    out = json.loads(server.book_grab("guid-1", 1, "Фрэнк Герберт", "Дюна",
                                      "audiobook", confirm=True))
    assert out["ok"] is True
    assert out["hash"] == "NEWHASH"[:12]
    # tag carries base64url(author/title/type)
    assert state["tags"].startswith("abk:")
    decoded = base64.urlsafe_b64decode(state["tags"][4:].encode()).decode()
    # payload is author/title/type[/notify_chat]; chat empty here (no NOTIFY_CHAT_ID)
    assert decoded.split("/")[:3] == ["Фрэнк Герберт", "Дюна", "audiobook"]


def test_book_grab_prowlarr_error(monkeypatch):
    monkeypatch.setattr(server.requests, "get", lambda *a, **k: _resp(200, []))
    monkeypatch.setattr(server.requests, "post", lambda url, **k: _resp(500, text="boom"))
    msg = server.book_grab("g", 1, "A", "B", "audiobook", confirm=True)
    assert "недоступен" in msg or "Ошибка" in msg
