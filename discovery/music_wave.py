#!/usr/bin/env python3
"""Wave runner: finish the targets that the first pass left ungrabbed, waiting
out the MUSIC_MAX_INFLIGHT cap as in-flight torrents drain. Runs on the bot host.
Run after music_discovery.py, same incantation (see discovery/README.md)."""
import os, sys, json, time
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                   # music_discovery
sys.path.insert(0, os.path.dirname(_HERE))  # media-mcp root, for server
import server, music_discovery as md

TARGETS = json.load(open(md.TARGETS_FILE))
prev = json.load(open(md.RESULTS_FILE))
done_t = {x["t"] for x in prev if x["status"] in ("grab", "skip_have")}
dead_t = {x["t"] for x in prev if x["status"] == "no_seeders"}  # 0-seed, skip

pending = [t for t in TARGETS
           if f"{t['artist']} {t['album']}" not in done_t
           and f"{t['artist']} {t['album']}" not in dead_t]
print(f"pending={len(pending)} (done={len(done_t)} dead={len(dead_t)})", flush=True)

results = list(prev)
INFLIGHT = ("active music download", "too many active")
DISK = ("low disk space", "disk space")
MAX_WAIT_MIN = 90
start = time.time()
stalls = 0

while pending and (time.time() - start) < MAX_WAIT_MIN * 60:
    progressed = False
    for t in list(pending):
        g = ""
        try:
            g = server._music_guards()
        except Exception:
            g = ""
        gl = g.lower()
        if any(d in gl for d in DISK):
            print("DISK LOW — aborting wave:", g, flush=True)
            pending = []
            break
        if any(s in gl for s in INFLIGHT):
            break  # cap full → drop to drain-sleep
        r = md.grab_one(t)
        r["cluster"] = t.get("cluster")
        key = f"{t['artist']} {t['album']}"
        raw = json.dumps(r.get("detail") or {}, ensure_ascii=False).lower()
        if r["status"] in ("grab", "skip_have"):
            pending.remove(t); progressed = True
            print(f"  + {r['status']:<10} {key[:34]} => {(r.get('pick') or '')[:46]}", flush=True)
        elif r["status"] in ("no_seeders", "no_results", "noparse", "search_err", "search_err2"):
            pending.remove(t)  # won't succeed on retry
            print(f"  - {r['status']:<10} {key[:50]}", flush=True)
        elif any(s in raw for s in INFLIGHT):
            print(f"  . inflight-cap, requeue {key[:40]}", flush=True)
            break  # cap hit mid-loop → drain
        else:
            pending.remove(t)
            print(f"  ? {r['status']:<10} {key[:50]} {raw[:80]}", flush=True)
        results[:] = [x for x in results if x.get("t") != r["t"]] + [r]
        json.dump(results, open(md.RESULTS_FILE, "w"), ensure_ascii=False, indent=1)
        time.sleep(4)
    if pending:
        if not progressed:
            stalls += 1
        else:
            stalls = 0
        if stalls >= 12:  # ~9 min no progress → give up gracefully
            print(f"STALLED — {len(pending)} still pending, exiting", flush=True)
            break
        print(f"… inflight full, draining (pending={len(pending)}, stall={stalls})", flush=True)
        time.sleep(45)

grabbed = sum(1 for x in results if x["status"] == "grab")
print(f"\n=== WAVE DONE: total grabbed={grabbed}, still pending={len(pending)} ===", flush=True)
