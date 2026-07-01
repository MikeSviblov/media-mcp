"""Tests for the Bandcamp importer (no network — pure logic)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import bandcamp_import as bc  # noqa: E402


def test_sanitize_traversal_and_cyrillic():
    assert "/" not in bc.sanitize("a/b")
    assert bc.sanitize("../../etc") != ".."
    assert bc.sanitize("  Da'Ba  ") == "Da'Ba"
    assert bc.sanitize("") == "Unknown"


def test_is_bandcamp_accepts_only_bandcamp():
    assert bc.is_bandcamp("https://dababand.bandcamp.com/")
    assert bc.is_bandcamp("https://dababand.bandcamp.com/album/iii")
    assert bc.is_bandcamp("http://x.bandcamp.com/track/y")
    assert not bc.is_bandcamp("https://evil.com/album/x")
    assert not bc.is_bandcamp("https://notbandcamp.com.evil.com/")
    assert not bc.is_bandcamp("ftp://dababand.bandcamp.com/")
    assert not bc.is_bandcamp("file:///etc/passwd")


def test_build_cmd_outputs_per_album_under_artist(monkeypatch):
    monkeypatch.setattr(bc, "LIB_MUSIC", "/music")
    monkeypatch.setattr(bc, "YTDLP", "/usr/bin/yt-dlp")
    cmd = bc.build_cmd("https://dababand.bandcamp.com/album/iii", "Da'Ba")
    assert cmd[0] == "/usr/bin/yt-dlp"
    assert "--embed-metadata" in cmd and "--embed-thumbnail" in cmd
    out = cmd[cmd.index("-o") + 1]
    # album folder under the canonical artist; yt-dlp fills %(album)s per track
    assert out.startswith("/music/Da'Ba/%(album)s/")
    assert cmd[-1] == "https://dababand.bandcamp.com/album/iii"


def test_main_rejects_non_bandcamp(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bandcamp_import.py", "https://evil.com/x", "Da'Ba"])
    calls = []
    monkeypatch.setattr(bc.subprocess, "run", lambda *a, **k: calls.append(a))
    try:
        bc.main()
    except SystemExit as e:
        assert e.code == 2
    assert calls == []  # never invoked yt-dlp on a non-bandcamp url
