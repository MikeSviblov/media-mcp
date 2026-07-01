"""Verify how many discovery targets actually landed in Navidrome, grouped by
cluster. Read-only, no server import (MEDIA_MCP_MODE not needed). Run after a
discovery batch with the .env sourced. See discovery/README.md."""
import os, json, hashlib, re, unicodedata, collections, requests
_HERE = os.path.dirname(os.path.abspath(__file__))
U = os.environ["NAVIDROME_USER"]; P = os.environ["NAVIDROME_PASS"]; NAV = "http://localhost:4533"


def nav(ep, **kw):
    import urllib.parse
    salt = "fv"; tok = hashlib.md5((P + salt).encode()).hexdigest()
    p = dict(u=U, t=tok, s=salt, v="1.16.1", c="fv", f="json"); p.update(kw)
    return requests.get(NAV + "/rest/" + ep, params=p, timeout=30).json()["subsonic-response"]


def fold(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-zа-я0-9]", "", s.lower())


targets = json.load(open(os.environ.get("MUSIC_TARGETS", os.path.join(_HERE, "music_targets.json"))))
byc = collections.defaultdict(lambda: [0, 0, []])
for t in targets:
    a = t["artist"]; c = t["cluster"]
    res = nav("search3", query=a, artistCount=15, albumCount=0, songCount=0)
    arts = res.get("searchResult3", {}).get("artist", [])
    fa = fold(a)
    ok = any(fold(x.get("name", "")) == fa or fa in fold(x.get("name", "")) for x in arts)
    byc[c][1] += 1; byc[c][0] += 1 if ok else 0
    if not ok:
        byc[c][2].append(a)
names = {"neofolk": "Неофолк/нордик", "artpop": "Арт-поп/сонграйтер",
         "darkelectro": "Тёмн.электроника", "metal": "Метал"}
tot = 0
for c in ["neofolk", "artpop", "darkelectro", "metal"]:
    g, n, miss = byc[c]; tot += g
    print("%-20s %2d/%d" % (names[c], g, n) + ("  нет: " + ", ".join(miss) if miss else "  ✓"))
print("\nИТОГО: %d/%d" % (tot, len(targets)))
