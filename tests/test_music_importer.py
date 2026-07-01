"""Tests for the music importer (no mcp dependency — pure logic + filesystem).

Mirror of test_importer.py for the Режим A (passthrough) music pipeline:
qB [Music] + mus: tag → hardlink into Music/Артист/Альбом/ → Navidrome startScan →
mus_done. No tag editing, no metadata-fix (Navidrome groups by tags natively)."""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import music_importer as mi  # noqa: E402


# ── sanitize: path-traversal / cyrillic / edge cases ─────────

def test_sanitize_strips_separators_and_traversal():
    assert "/" not in mi.sanitize("a/b/c")
    assert "\\" not in mi.sanitize("a\\b")
    out = mi.sanitize("../../etc/passwd")
    assert "/" not in out
    assert out != ".."


def test_sanitize_drops_control_and_null():
    assert "\x00" not in mi.sanitize("a\x00b")
    assert mi.sanitize("a\nb\tc") == "abc"


def test_sanitize_empty_and_dots_become_unknown():
    assert mi.sanitize("") == "Unknown"
    assert mi.sanitize("..") == "Unknown"
    assert mi.sanitize("   ") == "Unknown"


def test_sanitize_keeps_cyrillic_and_trims():
    assert mi.sanitize("  Кино  ") == "Кино"


def test_sanitize_caps_length():
    assert len(mi.sanitize("x" * 500)) == 120


# ── container→host path mapping ──────────────────────────────

def test_to_host_path_maps_prefix(monkeypatch):
    monkeypatch.setattr(mi, "DL_CONTAINER", "/downloads")
    monkeypatch.setattr(mi, "DL_HOST", "/mnt/nas/disk2/Torrents")
    assert mi.to_host_path("/downloads/[Music]/X") == "/mnt/nas/disk2/Torrents/[Music]/X"
    assert mi.to_host_path("/elsewhere/p") == "/elsewhere/p"


# ── import_torrent: hardlink + scan + idempotency ────────────

def _tag(artist, album, chat="", kind=None):
    if kind is not None:
        payload = f"{artist}/{album}/{chat}/{kind}"
    elif chat:
        payload = f"{artist}/{album}/{chat}"
    else:
        payload = f"{artist}/{album}"
    return "mus:" + base64.urlsafe_b64encode(payload.encode()).decode()


def test_import_torrent_hardlinks_and_scans(tmp_path, monkeypatch):
    src = tmp_path / "torrents" / "Кино - Молнии Индры"
    src.mkdir(parents=True)
    (src / "01.flac").write_text("audio1")
    (src / "02.flac").write_text("audio2")
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(mi, "LIB_MUSIC", str(lib))
    monkeypatch.setattr(mi, "DL_CONTAINER", str(tmp_path / "torrents"))
    monkeypatch.setattr(mi, "DL_HOST", str(tmp_path / "torrents"))
    tagged, scanned = {}, []
    monkeypatch.setattr(mi, "_qpost", lambda p, **kw: tagged.update(kw))
    monkeypatch.setattr(mi, "_navidrome_scan", lambda: scanned.append(True) or True)

    t = {"hash": "h1", "name": "rel", "tags": _tag("Кино", "Молнии Индры"),
         "content_path": str(src), "progress": 1.0}
    assert mi.import_torrent(t) is True

    dst = lib / "Кино" / "Молнии Индры"
    assert (dst / "01.flac").exists() and (dst / "02.flac").exists()
    # hardlink — same inode, link count 2 (passthrough, no byte editing)
    assert os.stat(dst / "01.flac").st_nlink == 2
    assert scanned == [True]
    assert tagged.get("tags") == "mus_done"


def test_import_torrent_single_file_lands_in_album_folder(tmp_path, monkeypatch):
    # a bare single-file release must land inside Артист/Альбом/, not at the root
    src = tmp_path / "t" / "single.flac"
    src.parent.mkdir(parents=True)
    src.write_text("a")
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(mi, "LIB_MUSIC", str(lib))
    monkeypatch.setattr(mi, "DL_CONTAINER", str(tmp_path / "t"))
    monkeypatch.setattr(mi, "DL_HOST", str(tmp_path / "t"))
    monkeypatch.setattr(mi, "_qpost", lambda *a, **k: None)
    monkeypatch.setattr(mi, "_navidrome_scan", lambda: True)
    t = {"hash": "h", "tags": _tag("Аквариум", "Синий альбом"),
         "content_path": str(src), "progress": 1.0}
    assert mi.import_torrent(t) is True
    assert (lib / "Аквариум" / "Синий альбом" / "single.flac").exists()


def test_import_torrent_discography_places_albums_under_artist(tmp_path, monkeypatch):
    # one torrent with per-album subfolders → contents land directly under the artist
    # (no single "album" wrapper); Navidrome groups by tags
    src = tmp_path / "torrents" / "Pink Floyd Discography"
    (src / "1973 - Dark Side").mkdir(parents=True)
    (src / "1973 - Dark Side" / "01.flac").write_text("a")
    (src / "1979 - The Wall").mkdir(parents=True)
    (src / "1979 - The Wall" / "01.flac").write_text("b")
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(mi, "LIB_MUSIC", str(lib))
    monkeypatch.setattr(mi, "DL_CONTAINER", str(tmp_path / "torrents"))
    monkeypatch.setattr(mi, "DL_HOST", str(tmp_path / "torrents"))
    monkeypatch.setattr(mi, "_qpost", lambda *a, **k: None)
    monkeypatch.setattr(mi, "_navidrome_scan", lambda: True)
    t = {"hash": "h", "tags": _tag("Pink Floyd", "Discography", kind="discography"),
         "content_path": str(src), "progress": 1.0}
    assert mi.import_torrent(t) is True
    # album subfolders sit directly under the artist, NOT under Pink Floyd/Discography/
    assert (lib / "Pink Floyd" / "1973 - Dark Side" / "01.flac").exists()
    assert (lib / "Pink Floyd" / "1979 - The Wall" / "01.flac").exists()
    assert not (lib / "Pink Floyd" / "Discography").exists()


def test_import_torrent_silent_suppresses_notification(tmp_path, monkeypatch):
    src = tmp_path / "t" / "rel"
    src.mkdir(parents=True)
    (src / "01.flac").write_text("a")
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(mi, "LIB_MUSIC", str(lib))
    monkeypatch.setattr(mi, "DL_CONTAINER", str(tmp_path / "t"))
    monkeypatch.setattr(mi, "DL_HOST", str(tmp_path / "t"))
    monkeypatch.setattr(mi, "_qpost", lambda *a, **k: None)
    monkeypatch.setattr(mi, "_navidrome_scan", lambda: True)
    monkeypatch.setattr(mi, "TG_TOKEN", "tok")
    monkeypatch.setattr(mi, "TG_CHAT", "123")
    posts = []
    monkeypatch.setattr(mi.requests, "post", lambda *a, **k: posts.append(1))
    t = {"hash": "h", "tags": _tag("Кино", "Группа крови", chat="silent"),
         "content_path": str(src), "progress": 1.0}
    assert mi.import_torrent(t) is True
    assert posts == []          # silent → no Telegram notification


def test_import_torrent_skips_already_done(monkeypatch):
    monkeypatch.setattr(mi, "_qpost", lambda *a, **k: None)
    t = {"hash": "h", "tags": _tag("A", "B") + ",mus_done", "content_path": "/x", "progress": 1.0}
    assert mi.import_torrent(t) is False


def test_import_torrent_skips_untagged():
    t = {"hash": "h", "tags": "", "content_path": "/x", "progress": 1.0}
    assert mi.import_torrent(t) is False


def test_import_torrent_bad_tag_returns_false(monkeypatch):
    # malformed base64url payload → logged + skipped, never raises
    t = {"hash": "h", "tags": "mus:!!!notbase64!!!", "content_path": "/x", "progress": 1.0}
    assert mi.import_torrent(t) is False


def test_import_torrent_missing_source_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(mi, "DL_CONTAINER", "/downloads")
    monkeypatch.setattr(mi, "DL_HOST", str(tmp_path))
    t = {"hash": "h", "tags": _tag("A", "B"), "content_path": "/downloads/nope", "progress": 1.0}
    assert mi.import_torrent(t) is False


def test_import_torrent_sends_telegram_when_configured(tmp_path, monkeypatch):
    src = tmp_path / "t" / "rel"
    src.mkdir(parents=True)
    (src / "01.flac").write_text("a")
    lib = tmp_path / "lib"
    lib.mkdir()
    monkeypatch.setattr(mi, "LIB_MUSIC", str(lib))
    monkeypatch.setattr(mi, "DL_CONTAINER", str(tmp_path / "t"))
    monkeypatch.setattr(mi, "DL_HOST", str(tmp_path / "t"))
    monkeypatch.setattr(mi, "_qpost", lambda *a, **k: None)
    monkeypatch.setattr(mi, "_navidrome_scan", lambda: True)
    monkeypatch.setattr(mi, "TG_TOKEN", "tok")
    monkeypatch.setattr(mi, "TG_CHAT", "123")
    sent = {}
    monkeypatch.setattr(mi.requests, "post",
                        lambda url, **kw: sent.update(url=url, data=kw.get("data")))
    t = {"hash": "h", "tags": _tag("Кино", "Звезда по имени Солнце"),
         "content_path": str(src), "progress": 1.0}
    assert mi.import_torrent(t) is True
    assert "api.telegram.org" in sent["url"]
    assert sent["data"]["chat_id"] == "123"
    assert "Звезда по имени Солнце" in sent["data"]["text"]


def test_notify_telegram_silent_when_unconfigured(monkeypatch):
    monkeypatch.setattr(mi, "TG_TOKEN", "")
    monkeypatch.setattr(mi, "TG_CHAT", "")
    called = []
    monkeypatch.setattr(mi.requests, "post", lambda *a, **k: called.append(1))
    mi._notify_telegram("hi")
    assert called == []


def test_notify_telegram_routes_to_requester_bot(monkeypatch):
    monkeypatch.setattr(mi, "TG_TOKEN", "default")
    monkeypatch.setattr(mi, "TG_CHAT", "111")
    monkeypatch.setenv("NOTIFY_TOKEN_333", "sebtok")
    sent = {}
    monkeypatch.setattr(mi.requests, "post",
                        lambda url, **kw: sent.update(url=url, data=kw.get("data")))
    mi._notify_telegram("музыка готова", "333")
    assert "/botsebtok/" in sent["url"]
    assert sent["data"]["chat_id"] == "333"
