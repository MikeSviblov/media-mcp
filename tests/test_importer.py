"""Tests for the audiobook importer (no mcp dependency — pure logic + filesystem)."""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import importer  # noqa: E402


# ── sanitize: path-traversal / cyrillic / edge cases ─────────

def test_sanitize_strips_separators_and_traversal():
    # the defense is "no path separators" → result is a single, harmless component
    assert "/" not in importer.sanitize("a/b/c")
    assert "\\" not in importer.sanitize("a\\b")
    out = importer.sanitize("../../etc/passwd")
    assert "/" not in out          # cannot escape the target directory
    assert out != ".."


def test_sanitize_drops_control_and_null():
    # control chars (incl. NUL, newline, tab) are dropped, not kept
    assert "\x00" not in importer.sanitize("a\x00b")
    assert importer.sanitize("a\nb\tc") == "abc"


def test_sanitize_empty_and_dots_become_unknown():
    assert importer.sanitize("") == "Unknown"
    assert importer.sanitize("..") == "Unknown"
    assert importer.sanitize("   ") == "Unknown"


def test_sanitize_keeps_cyrillic_and_trims():
    assert importer.sanitize("  Маргарет Этвуд  ") == "Маргарет Этвуд"


def test_sanitize_caps_length():
    assert len(importer.sanitize("x" * 500)) == 120


# ── container→host path mapping ──────────────────────────────

def test_to_host_path_maps_prefix(monkeypatch):
    monkeypatch.setattr(importer, "DL_CONTAINER", "/downloads")
    monkeypatch.setattr(importer, "DL_HOST", "/mnt/nas/disk2/Torrents")
    assert importer.to_host_path("/downloads/[Abooks]/X") == "/mnt/nas/disk2/Torrents/[Abooks]/X"
    assert importer.to_host_path("/elsewhere/p") == "/elsewhere/p"


# ── import_torrent: hardlink + scan + idempotency ────────────

def _tag(author, title, btype="audiobook", chat=""):
    payload = f"{author}/{title}/{btype}/{chat}" if chat else f"{author}/{title}/{btype}"
    return "abk:" + base64.urlsafe_b64encode(payload.encode()).decode()


def test_import_torrent_hardlinks_and_scans(tmp_path, monkeypatch):
    src = tmp_path / "torrents" / "Дюна - Чонишвили"
    src.mkdir(parents=True)
    (src / "01.mp3").write_text("audio1")
    (src / "02.mp3").write_text("audio2")
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(importer, "LIB_AUDIOBOOKS", str(lib))
    monkeypatch.setattr(importer, "DL_CONTAINER", str(tmp_path / "torrents"))
    monkeypatch.setattr(importer, "DL_HOST", str(tmp_path / "torrents"))
    tagged, scanned = {}, []
    monkeypatch.setattr(importer, "_qpost", lambda p, **kw: tagged.update(kw))
    monkeypatch.setattr(importer, "_abs_scan", lambda lid: scanned.append(lid))
    monkeypatch.setattr(importer, "_abs_fix_metadata", lambda *a, **k: True)

    t = {"hash": "h1", "name": "rel", "tags": _tag("Фрэнк Герберт", "Дюна"),
         "content_path": str(src), "progress": 1.0}
    assert importer.import_torrent(t, ["lib-id-1"]) is True

    dst = lib / "Фрэнк Герберт" / "Дюна"
    assert (dst / "01.mp3").exists() and (dst / "02.mp3").exists()
    # hardlink — same inode, link count 2
    assert os.stat(dst / "01.mp3").st_nlink == 2
    assert scanned == ["lib-id-1"]
    assert tagged.get("tags") == "abk_done"


def test_import_torrent_silent_suppresses_notification(tmp_path, monkeypatch):
    src = tmp_path / "t" / "rel"
    src.mkdir(parents=True)
    (src / "01.mp3").write_text("a")
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(importer, "LIB_AUDIOBOOKS", str(lib))
    monkeypatch.setattr(importer, "DL_CONTAINER", str(tmp_path / "t"))
    monkeypatch.setattr(importer, "DL_HOST", str(tmp_path / "t"))
    monkeypatch.setattr(importer, "_qpost", lambda *a, **k: None)
    monkeypatch.setattr(importer, "_abs_scan", lambda lid: None)
    monkeypatch.setattr(importer, "_abs_fix_metadata", lambda *a, **k: True)
    monkeypatch.setattr(importer, "TG_TOKEN", "tok")
    monkeypatch.setattr(importer, "TG_CHAT", "123")
    posts = []
    monkeypatch.setattr(importer.requests, "post", lambda *a, **k: posts.append(1))
    t = {"hash": "h", "tags": _tag("Стивен Кинг", "Институт", chat="silent"),
         "content_path": str(src), "progress": 1.0}
    assert importer.import_torrent(t, ["lib1"]) is True
    assert posts == []          # silent → no Telegram notification


def test_import_torrent_skips_already_done(monkeypatch):
    monkeypatch.setattr(importer, "_qpost", lambda *a, **k: None)
    t = {"hash": "h", "tags": _tag("A", "B") + ",abk_done", "content_path": "/x", "progress": 1.0}
    assert importer.import_torrent(t, []) is False


def test_import_torrent_skips_untagged():
    t = {"hash": "h", "tags": "", "content_path": "/x", "progress": 1.0}
    assert importer.import_torrent(t, []) is False


def test_import_torrent_missing_source_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(importer, "DL_CONTAINER", "/downloads")
    monkeypatch.setattr(importer, "DL_HOST", str(tmp_path))
    t = {"hash": "h", "tags": _tag("A", "B"), "content_path": "/downloads/nope", "progress": 1.0}
    assert importer.import_torrent(t, []) is False


def test_import_torrent_sends_telegram_when_configured(tmp_path, monkeypatch):
    src = tmp_path / "t" / "rel"
    src.mkdir(parents=True)
    (src / "01.mp3").write_text("a")
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(importer, "LIB_AUDIOBOOKS", str(lib))
    monkeypatch.setattr(importer, "DL_CONTAINER", str(tmp_path / "t"))
    monkeypatch.setattr(importer, "DL_HOST", str(tmp_path / "t"))
    monkeypatch.setattr(importer, "_qpost", lambda *a, **k: None)
    monkeypatch.setattr(importer, "_abs_scan", lambda lid: None)
    monkeypatch.setattr(importer, "_abs_fix_metadata", lambda *a, **k: True)
    monkeypatch.setattr(importer, "TG_TOKEN", "tok")
    monkeypatch.setattr(importer, "TG_CHAT", "123")
    sent = {}
    monkeypatch.setattr(importer.requests, "post",
                        lambda url, **kw: sent.update(url=url, data=kw.get("data")))
    t = {"hash": "h", "tags": _tag("Дэн Симмонс", "Гиперион"),
         "content_path": str(src), "progress": 1.0}
    assert importer.import_torrent(t, ["lib1"]) is True
    assert "api.telegram.org" in sent["url"]
    assert sent["data"]["chat_id"] == "123"
    assert "Гиперион" in sent["data"]["text"]


def test_notify_telegram_silent_when_unconfigured(monkeypatch):
    monkeypatch.setattr(importer, "TG_TOKEN", "")
    monkeypatch.setattr(importer, "TG_CHAT", "")
    called = []
    monkeypatch.setattr(importer.requests, "post", lambda *a, **k: called.append(1))
    importer._notify_telegram("hi")
    assert called == []          # no HTTP call when unconfigured


def test_notify_telegram_routes_to_requester_bot(monkeypatch):
    # the requester's chat has a dedicated bot token → notify via THAT bot/chat
    monkeypatch.setattr(importer, "TG_TOKEN", "default")
    monkeypatch.setattr(importer, "TG_CHAT", "111")
    monkeypatch.setenv("NOTIFY_TOKEN_222", "maxtok")
    sent = {}
    monkeypatch.setattr(importer.requests, "post",
                        lambda url, **kw: sent.update(url=url, data=kw.get("data")))
    importer._notify_telegram("книга готова", "222")
    assert "/botmaxtok/" in sent["url"]
    assert sent["data"]["chat_id"] == "222"


def test_notify_telegram_falls_back_to_default(monkeypatch):
    # chat without a dedicated token → fall back to the default bot/chat
    monkeypatch.setattr(importer, "TG_TOKEN", "default")
    monkeypatch.setattr(importer, "TG_CHAT", "111")
    monkeypatch.delenv("NOTIFY_TOKEN_999", raising=False)
    sent = {}
    monkeypatch.setattr(importer.requests, "post",
                        lambda url, **kw: sent.update(url=url, data=kw.get("data")))
    importer._notify_telegram("hi", "999")
    assert "/botdefault/" in sent["url"]
    assert sent["data"]["chat_id"] == "111"


# ── ABS metadata fix (CP1251 / inconsistent author override) ──

class _FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self._p = payload

    def json(self):
        return self._p


def test_abs_fix_metadata_patches_item_by_relpath(monkeypatch):
    monkeypatch.setattr(importer, "METADATA_FIX_DELAY", 0)
    monkeypatch.setattr(importer, "METADATA_FIX_RETRIES", 1)
    monkeypatch.setattr(importer, "ABS_API_KEY", "x")
    monkeypatch.setattr(importer.requests, "get",
                        lambda *a, **k: _FakeResp({"results": [
                            {"id": "OTHER", "relPath": "X/Y"},
                            {"id": "IT", "relPath": "Дэн Симмонс/Гиперион"}]}))
    patched = {}
    monkeypatch.setattr(importer.requests, "patch",
                        lambda url, **k: patched.update(url=url, body=k.get("json")))
    assert importer._abs_fix_metadata(["lib1"], "Дэн Симмонс", "Гиперион") is True
    assert "/api/items/IT/media" in patched["url"]
    assert patched["body"]["metadata"]["title"] == "Гиперион"
    assert patched["body"]["metadata"]["authors"] == [{"name": "Дэн Симмонс"}]


def test_abs_fix_metadata_no_match_does_not_patch(monkeypatch):
    monkeypatch.setattr(importer, "METADATA_FIX_DELAY", 0)
    monkeypatch.setattr(importer, "METADATA_FIX_RETRIES", 1)
    monkeypatch.setattr(importer, "ABS_API_KEY", "x")
    monkeypatch.setattr(importer.requests, "get",
                        lambda *a, **k: _FakeResp({"results": [{"id": "X", "relPath": "Other/Book"}]}))
    called = []
    monkeypatch.setattr(importer.requests, "patch", lambda *a, **k: called.append(1))
    assert importer._abs_fix_metadata(["lib1"], "A", "B") is False
    assert called == []


# ── ABS dedup: collapse duplicate items for the same folder ───

def test_abs_fix_metadata_dedups_duplicate_items(monkeypatch):
    # ABS races our scan and creates two items for one folder: a raw+isMissing twin
    # and a canonical one. Survivor = the non-missing one; the raw twin is removed.
    monkeypatch.setattr(importer, "METADATA_FIX_DELAY", 0)
    monkeypatch.setattr(importer, "METADATA_FIX_RETRIES", 1)
    monkeypatch.setattr(importer, "ABS_API_KEY", "x")
    payload = {"results": [
        {"id": "RAW", "relPath": "Стивен Кинг/Стрелок", "isMissing": True},
        {"id": "CANON", "relPath": "Стивен Кинг/Стрелок", "isMissing": False},
    ]}
    monkeypatch.setattr(importer.requests, "get", lambda *a, **k: _FakeResp(payload))
    patched, deleted = {}, []
    monkeypatch.setattr(importer.requests, "patch",
                        lambda url, **k: patched.update(url=url, body=k.get("json")))
    monkeypatch.setattr(importer.requests, "delete",
                        lambda url, **k: deleted.append((url, k)) or _FakeResp({}))
    assert importer._abs_fix_metadata(["lib1"], "Стивен Кинг", "Стрелок") is True
    # canonical (non-missing) survivor gets the metadata patch
    assert "/api/items/CANON/media" in patched["url"]
    assert patched["body"]["metadata"]["authors"] == [{"name": "Стивен Кинг"}]
    # exactly the raw twin is removed, and strictly db-only (NO hard flag anywhere)
    assert len(deleted) == 1
    durl, dkw = deleted[0]
    assert "/api/items/RAW" in durl
    assert "hard" not in durl
    assert "hard" not in (dkw.get("params") or {})


def test_abs_fix_metadata_single_item_no_delete(monkeypatch):
    # the common case (one item) must PATCH but never delete the sole record
    monkeypatch.setattr(importer, "METADATA_FIX_DELAY", 0)
    monkeypatch.setattr(importer, "METADATA_FIX_RETRIES", 1)
    monkeypatch.setattr(importer, "ABS_API_KEY", "x")
    monkeypatch.setattr(importer.requests, "get", lambda *a, **k: _FakeResp(
        {"results": [{"id": "ONLY", "relPath": "A/B", "isMissing": False}]}))
    patched, deleted, posted = {}, [], []
    monkeypatch.setattr(importer.requests, "patch", lambda url, **k: patched.update(url=url))
    monkeypatch.setattr(importer.requests, "delete", lambda *a, **k: deleted.append(1))
    monkeypatch.setattr(importer.requests, "post", lambda url, **k: posted.append(url))
    assert importer._abs_fix_metadata(["lib1"], "A", "B") is True
    assert "/api/items/ONLY/media" in patched["url"]
    assert deleted == []
    assert posted == []          # non-missing item → no rescan


def test_abs_fix_metadata_rescans_lone_missing_item(monkeypatch):
    # a single item flagged isMissing (no duplicate) must be rescanned to clear it
    monkeypatch.setattr(importer, "METADATA_FIX_DELAY", 0)
    monkeypatch.setattr(importer, "METADATA_FIX_RETRIES", 1)
    monkeypatch.setattr(importer, "ABS_API_KEY", "x")
    monkeypatch.setattr(importer.requests, "get", lambda *a, **k: _FakeResp(
        {"results": [{"id": "LONE", "relPath": "A/B", "isMissing": True}]}))
    deleted, posted = [], []
    monkeypatch.setattr(importer.requests, "patch", lambda *a, **k: None)
    monkeypatch.setattr(importer.requests, "delete", lambda *a, **k: deleted.append(1))
    monkeypatch.setattr(importer.requests, "post", lambda url, **k: posted.append(url))
    assert importer._abs_fix_metadata(["lib1"], "A", "B") is True
    assert deleted == []                         # nothing to dedup
    assert any("/api/items/LONE/scan" in u for u in posted)   # rescan to clear missing
