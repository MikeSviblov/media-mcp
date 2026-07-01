#!/usr/bin/env python3
"""Discovery batch grabber. Runs on the bot host with the media-mcp .env sourced.
Reuses media-mcp server.py functions: music_search_releases, music_grab.
Dedups against Navidrome, prefers FLAC, throttles, respects disk guard.

Run from media-mcp root (see discovery/README.md):
  set -a; . .env; set +a; export MEDIA_MCP_MODE=full
  ~/.local/bin/uv run --with mcp --with requests python discovery/music_discovery.py

Edit discovery/music_targets.json first; results land in discovery/music_results.json
(both overridable via MUSIC_TARGETS / MUSIC_RESULTS env)."""
import os, sys, json, time, hashlib, re
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))  # media-mcp root, for `import server`
import requests
import server  # media-mcp

TARGETS_FILE = os.environ.get("MUSIC_TARGETS", os.path.join(_HERE, "music_targets.json"))
RESULTS_FILE = os.environ.get("MUSIC_RESULTS", os.path.join(_HERE, "music_results.json"))

NAV = os.environ.get("NAVIDROME_URL", "http://localhost:4533")
NUSER = os.environ["NAVIDROME_USER"]; NPASS = os.environ["NAVIDROME_PASS"]

def nav(ep, **kw):
    salt = "discovery"
    tok = hashlib.md5((NPASS + salt).encode()).hexdigest()
    p = dict(u=NUSER, t=tok, s=salt, v="1.16.1", c="disc", f="json"); p.update(kw)
    r = requests.get(f"{NAV}/rest/{ep}", params=p, timeout=30)
    return r.json()["subsonic-response"]

def already_have(artist):
    """True if Navidrome already has an artist whose name ~matches (case/space-insensitive)."""
    try:
        res = nav("search3", query=artist, artistCount=20, albumCount=0, songCount=0)
    except Exception:
        return False
    norm = lambda s: re.sub(r"[^a-zа-я0-9]", "", s.lower())
    a = norm(artist)
    for art in res.get("searchResult3", {}).get("artist", []):
        if norm(art.get("name", "")) == a:
            return True
    return False

LOSSLESS = re.compile(r"\b(flac|lossless|ape|wavpack|wv|alac)\b", re.I)
LOWQ = re.compile(r"\b(128|192|m4a)\b", re.I)
IMAGECUE = re.compile(r"image\s*\+\s*\.?cue|\(image|\.cue|\bimage\b", re.I)

def pick(releases, kind):
    """Choose best release. Preference: track-based FLAC > FLAC image+cue >
    non-low-quality (mp3 320 etc) > anything; ties broken by seeders."""
    rs = [r for r in releases if (r.get("seeders") or 0) >= 1]
    if not rs:
        return None
    def size_ok(r):
        mb = r.get("size_mb") or 0
        if kind == "discography":
            return mb >= 80
        return 25 <= mb <= 2500
    rs = [r for r in rs if size_ok(r)] or rs
    flac = [r for r in rs if LOSSLESS.search(r.get("title", ""))]
    flac_tracks = [r for r in flac if not IMAGECUE.search(r.get("title", ""))]
    flac_image = [r for r in flac if IMAGECUE.search(r.get("title", ""))]
    nonlowq = [r for r in rs if not LOWQ.search(r.get("title", ""))]
    for pool in (flac_tracks, flac_image, nonlowq, rs):
        if pool:
            pool.sort(key=lambda r: (r.get("seeders") or 0), reverse=True)
            return pool[0]
    return None

def grab_one(t):
    artist, album, kind = t["artist"], t["album"], t.get("kind", "album")
    if already_have(artist):
        return {"t": artist + " - " + album, "status": "skip_have"}
    q = f"{artist} {album}"
    try:
        raw = server.music_search_releases(q)
    except Exception as e:
        return {"t": q, "status": "search_err", "err": str(e)[:120]}
    if isinstance(raw, str) and not raw.strip().startswith("["):
        # fallback: search by artist only
        try:
            raw = server.music_search_releases(artist)
        except Exception as e:
            return {"t": q, "status": "search_err2", "err": str(e)[:120]}
    try:
        rels = json.loads(raw)
    except Exception:
        return {"t": q, "status": "noparse", "raw": str(raw)[:160]}
    if not rels:
        return {"t": q, "status": "no_results"}
    best = pick(rels, kind)
    if not best:
        return {"t": q, "status": "no_seeders"}
    out = server.music_grab(best["guid"], best.get("indexerId") or best.get("indexer_id"),
                            artist, album, kind, confirm=True)
    try:
        od = json.loads(out)
    except Exception:
        od = {"raw": str(out)[:200]}
    return {"t": q, "status": "grab" if od.get("ok") else "grab_fail",
            "pick": best.get("title", "")[:80], "seeders": best.get("seeders"),
            "size_mb": best.get("size_mb"), "hash": od.get("hash"), "detail": od if not od.get("ok") else None}

def main():
    targets = json.load(open(TARGETS_FILE))
    results = []
    grabbed = 0
    for i, t in enumerate(targets, 1):
        r = grab_one(t)
        r["cluster"] = t.get("cluster")
        results.append(r)
        if r["status"] == "grab":
            grabbed += 1
        print(f"[{i:>2}/{len(targets)}] {r['status']:<11} {r['t']}"
              + (f"  «{r.get('pick','')[:60]}» S={r.get('seeders')} {r.get('size_mb')}MB" if r.get("pick") else "")
              + (f"  ERR={r.get('err') or r.get('detail') or r.get('raw','')}" if r["status"] not in ("grab","skip_have") else ""),
              flush=True)
        json.dump(results, open(RESULTS_FILE, "w"), ensure_ascii=False, indent=1)
        time.sleep(4)
    print(f"\n=== DONE: grabbed={grabbed} / {len(targets)} ===")

if __name__ == "__main__":
    main()
