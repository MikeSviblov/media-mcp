#!/usr/bin/env python3
"""Split image+.cue FLAC albums into per-track FLAC using ffmpeg. Runs on the media host.
Keeps originals (moved to /mnt/nas/disk2/_music_orig/). Usage: split_cue.py <artist> ..."""
import os, sys, re, subprocess, glob, shutil

MUSIC = "/mnt/nas/disk2/Music"
BACKUP = "/mnt/nas/disk2/_music_orig"


def readcue(p):
    for enc in ("utf-8", "cp1251", "latin1"):
        try:
            return open(p, encoding=enc).read()
        except Exception:
            pass
    return open(p, encoding="latin1", errors="replace").read()


def t2s(m, s, f):
    return int(m) * 60 + int(s) + int(f) / 75.0


def parse(cue):
    g = {"PERFORMER": "", "TITLE": "", "FILE": ""}
    tracks = []
    cur = None
    for ln in cue.splitlines():
        l = ln.strip()
        mf = re.match(r'FILE\s+"?(.*?)"?\s+\w+$', l)
        if mf and cur is None:
            g["FILE"] = mf.group(1)
        mp = re.match(r'(PERFORMER|TITLE)\s+"?(.*?)"?$', l)
        if mp and cur is None:
            g[mp.group(1)] = mp.group(2)
        if l.startswith("TRACK"):
            cur = {"no": int(re.search(r"TRACK\s+(\d+)", l).group(1)),
                   "title": "", "perf": g["PERFORMER"], "start": 0.0}
            tracks.append(cur)
        elif cur is not None:
            mt = re.match(r'TITLE\s+"?(.*?)"?$', l)
            mpf = re.match(r'PERFORMER\s+"?(.*?)"?$', l)
            mi = re.match(r"INDEX\s+01\s+(\d+):(\d+):(\d+)", l)
            if mt:
                cur["title"] = mt.group(1)
            elif mpf:
                cur["perf"] = mpf.group(1)
            elif mi:
                cur["start"] = t2s(*mi.groups())
    return g, tracks


def sanit(s):
    return (re.sub(r"[/\x00]", " ", s).strip() or "track")[:120]


def process(artist):
    base = os.path.join(MUSIC, artist)
    cues = glob.glob(base + "/**/*.cue", recursive=True)
    done = 0
    for cue in cues:
        d = os.path.dirname(cue)
        g, tr = parse(readcue(cue))
        # locate the image audio file: prefer the cue's FILE reference, else any single audio
        flac = None
        if g.get("FILE"):
            cand = os.path.join(d, g["FILE"])
            if os.path.exists(cand):
                flac = cand
        if not flac:
            # same basename as the .cue, audio extension (CD1.cue -> CD1.ape)
            stem = cue[:-4]
            for ext in (".flac", ".ape", ".wv", ".wav"):
                if os.path.exists(stem + ext):
                    flac = stem + ext
                    break
        if not flac:
            auds = [f for ext in ("*.flac", "*.ape", "*.wv", "*.wav")
                    for f in glob.glob(d + "/" + ext)]
            if len(auds) == 1:
                flac = auds[0]
        if not flac:
            print("  skip (cannot resolve audio):", d)
            continue
        if not tr:
            print("  no tracks parsed:", cue)
            continue
        album = g["TITLE"] or os.path.basename(d)
        aartist = g["PERFORMER"] or artist
        errs = 0
        for i, t in enumerate(tr):
            start = t["start"]
            end = tr[i + 1]["start"] if i + 1 < len(tr) else None
            out = os.path.join(d, "%02d %s.flac" % (t["no"], sanit(t["title"])))
            cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", flac, "-ss", "%.3f" % start]
            if end:
                cmd += ["-to", "%.3f" % end]
            cmd += ["-c:a", "flac",
                    "-metadata", "title=%s" % t["title"],
                    "-metadata", "artist=%s" % t["perf"],
                    "-metadata", "album_artist=%s" % aartist,
                    "-metadata", "album=%s" % album,
                    "-metadata", "track=%d" % t["no"], out]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode:
                errs += 1
                print("  ffmpeg err tr%d: %s" % (t["no"], r.stderr[:90]))
        bdir = os.path.join(BACKUP, artist, os.path.basename(d))
        os.makedirs(bdir, exist_ok=True)
        for f in [flac, cue] + glob.glob(d + "/*.log"):
            try:
                shutil.move(f, os.path.join(bdir, os.path.basename(f)))
            except Exception as e:
                print("  mv warn:", e)
        print("  OK %s: %d tracks, %d errs (album=%s | aartist=%s)" %
              (os.path.basename(d), len(tr), errs, album, aartist))
        done += 1
    return done


if __name__ == "__main__":
    for a in sys.argv[1:]:
        print("==", a, "==")
        process(a)
