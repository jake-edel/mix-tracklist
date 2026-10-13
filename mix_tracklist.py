"""Build a tracklist for a DJ mix by querying Shazam with short clips.

Every --step seconds, a --clip second excerpt is cut with ffmpeg and sent to
Shazam. Hits are grouped by track, and each track's start is estimated from
the clip time minus Shazam's reported offset into the track.

Shazam rate-limits (HTTP 429) after a hundred or so quick queries. On a 429
all queries pause, the wait doubling each time (30s, 60s, ... 15min).

Read-only on the mix. Writes <out>/<mix name>.tracklist.txt, .cue and
.shazam.json (raw hits, saved after every query and reused on re-runs, so an
interrupted scan resumes where it stopped).

    mix-tracklist "/mnt/hdd/Music/Mixes/HÖR Berlin/HÖR Berlin/02 - Tigerhead (2022-11-18).opus" -o /tmp/out
"""
import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from aiohttp_retry import ExponentialRetry
from shazamio import HTTPClient, Shazam


def duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(out)


def cut(path, start, length, dest):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(length), "-i", str(path),
         "-ac", "1", "-ar", "16000", str(dest)],
        check=True,
    )


def parse(resp):
    """Reduce a Shazam response to the fields we keep, or None for no match."""
    track = resp.get("track")
    if not track or not resp.get("matches"):
        return None
    meta = {}
    for section in track.get("sections", []):
        for m in section.get("metadata", []) or []:
            meta[m.get("title")] = m.get("text")
    return {
        "key": track.get("key"),
        "artist": track.get("subtitle"),
        "title": track.get("title"),
        "album": meta.get("Album"),
        "label": meta.get("Label"),
        "released": meta.get("Released"),
        "url": track.get("url"),
        "offset": resp["matches"][0].get("offset"),
    }


class Throttle:
    """Shared pacing: a fixed gap between queries, and a global pause on 429s."""

    def __init__(self, delay):
        self.delay, self.backoff, self.resume = delay, 0, 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            pause = self.resume - time.monotonic()
            if pause > self.delay + 1:
                print(f"  rate-limited: sleeping {pause:.0f}s (until {clock(pause)})", file=sys.stderr)
            await asyncio.sleep(max(0, pause))
            self.resume = time.monotonic() + self.delay

    def limited(self):
        self.backoff = min(900, self.backoff * 2 or 30)
        self.resume = max(self.resume, time.monotonic() + self.backoff)
        return self.backoff

    def ok(self):
        self.backoff = 0


async def query(shazam, throttle, sem, path, t, clip, tmp, retries=8):
    async with sem:
        dest = tmp / f"{t}.wav"
        await asyncio.to_thread(cut, path, t, clip, dest)
        try:
            for attempt in range(1, retries + 1):
                await throttle.wait()
                try:
                    hit = parse(await shazam.recognize(str(dest)))
                    if throttle.backoff:
                        print(f"  {fmt(t)}: query succeeded again; rate-limit cleared", file=sys.stderr)
                    throttle.ok()
                    return t, hit
                except Exception as e:  # 429s arrive as a non-JSON body
                    wait = throttle.limited()
                    print(f"  {fmt(t)}: {type(e).__name__} (attempt {attempt}/{retries}); "
                          f"pausing {wait}s, resuming ~{clock(wait)}", file=sys.stderr)
            print(f"  {fmt(t)}: giving up after {retries} attempts; will retry on next run", file=sys.stderr)
            return t, {"error": True}
        finally:
            dest.unlink(missing_ok=True)


async def scan(path, clip, step, workers, delay, raw):
    cache = json.loads(raw.read_text()) if raw.exists() else {}
    hits = {int(k): v for k, v in cache.items() if not (v or {}).get("error")}
    total = duration(path)
    todo = [t for t in range(0, int(total - clip), step) if t not in hits]
    print(f"{path.name}: {fmt(total)}, {len(hits)} cached, {len(todo)} to query", file=sys.stderr)
    if raw.exists():
        errors = sum(1 for v in cache.values() if (v or {}).get("error"))
        print(f"  resumed from {raw}" + (f" ({errors} failed clips re-queued)" if errors else ""), file=sys.stderr)
    if todo:
        est = len(todo) * delay / workers
        print(f"  clip {clip}s every {step}s, {workers} worker(s), {delay}s gap; "
              f"best case ~{fmt(est)} (finish ~{clock(est)})", file=sys.stderr)
    else:
        print("  everything cached; skipping queries", file=sys.stderr)
    # shazamio's default client silently retries 429s 20 times; surface them instead
    client = HTTPClient(retry_options=ExponentialRetry(attempts=1))
    shazam = Shazam(segment_duration_seconds=clip, http_client=client)
    throttle, sem = Throttle(delay), asyncio.Semaphore(workers)
    start, matched = time.monotonic(), 0
    with tempfile.TemporaryDirectory() as tmp:
        tasks = [asyncio.create_task(query(shazam, throttle, sem, path, t, clip, Path(tmp))) for t in todo]
        for done, fut in enumerate(asyncio.as_completed(tasks), 1):
            t, hit = await fut
            hits[t] = hit
            raw.write_text(json.dumps(dict(sorted(hits.items())), indent=1, ensure_ascii=False))
            label = f"{hit['artist']} - {hit['title']}" if hit and "key" in hit else "-"
            matched += bool(hit and "key" in hit)
            elapsed = time.monotonic() - start
            eta = elapsed / done * (len(todo) - done)
            print(f"  [{done}/{len(todo)} {done * 100 // len(todo)}%  {matched} matched  "
                  f"elapsed {fmt(elapsed)}  eta {fmt(eta)}] {fmt(t)}  {label}", file=sys.stderr)
    if todo:
        print(f"scan done in {fmt(time.monotonic() - start)}", file=sys.stderr)
    return dict(sorted(hits.items()))


def tracklist(hits, min_hits, step, tolerance=45):
    by_key = {}
    for t, hit in hits.items():
        if hit and "key" in hit:
            by_key.setdefault(hit["key"], []).append((t, hit))
    tracks = []
    for seen in by_key.values():
        # Hits from one continuous play agree on where the track started. Drop
        # those that don't: a shared sample or loop matches at the same offset
        # minutes apart.
        starts = [t - (h["offset"] or 0) for t, h in seen]
        mid = statistics.median(starts)
        seen = [(t, h) for (t, h), s in zip(seen, starts) if abs(s - mid) <= tolerance]
        if len(seen) < min_hits:
            continue
        # A track mixed in mid-way has a large offset; it can't have started
        # more than one step before it was first heard.
        first = seen[0][0]
        tracks.append({
            **seen[0][1],
            "start": max(0, mid, first - step),
            "first_hit": first,
            "hits": len(seen),
        })
    return sorted(tracks, key=lambda x: x["start"])


def clock(in_sec):
    """Wall-clock time in_sec seconds from now, e.g. 14:05:30."""
    return time.strftime("%H:%M:%S", time.localtime(time.time() + in_sec))


def fmt(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def write_txt(path, tracks, singles):
    lines = []
    for x in tracks:
        extra = ", ".join(v for v in (x["label"], x["released"]) if v)
        lines.append(f"{fmt(x['start'])}  {x['artist']} - {x['title']}"
                     + (f"  [{extra}]" if extra else "") + f"  ({x['hits']} hits)")
    if singles:
        lines += ["", "# single-hit matches (less certain):"]
        lines += [f"# {fmt(x['start'])}  {x['artist']} - {x['title']}" for x in singles]
    path.write_text("\n".join(lines) + "\n")


def write_cue(path, mix, tracks):
    q = lambda s: (s or "").replace('"', "'")
    out = [f'TITLE "{q(mix.stem)}"', f'FILE "{q(mix.name)}" WAVE']
    for n, x in enumerate(tracks, 1):
        s = int(x["start"])
        out += [f"  TRACK {n:02d} AUDIO", f'    TITLE "{q(x["title"])}"',
                f'    PERFORMER "{q(x["artist"])}"', f"    INDEX 01 {s // 60:02d}:{s % 60:02d}:00"]
    path.write_text("\n".join(out) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mix", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True, help="output directory")
    ap.add_argument("--clip", type=int, default=12, help="clip length in seconds (default 12)")
    ap.add_argument("--step", type=int, default=30, help="seconds between clips (default 30)")
    ap.add_argument("--workers", type=int, default=1, help="parallel queries (default 1)")
    ap.add_argument("--delay", type=float, default=4, help="min seconds between queries (default 4)")
    ap.add_argument("--min-hits", type=int, default=2, help="hits needed to list a track (default 2)")
    args = ap.parse_args()
    if not args.mix.is_file():
        sys.exit(f"mix not found: {args.mix}")

    args.out.mkdir(parents=True, exist_ok=True)
    raw = args.out / f"{args.mix.stem}.shazam.json"
    hits = asyncio.run(scan(args.mix, args.clip, args.step, args.workers, args.delay, raw))

    print("building tracklist...", file=sys.stderr)
    tracks = tracklist(hits, args.min_hits, args.step)
    singles = [x for x in tracklist(hits, 1, args.step) if x["hits"] < args.min_hits]
    txt, cue = args.out / f"{args.mix.stem}.tracklist.txt", args.out / f"{args.mix.stem}.cue"
    write_txt(txt, tracks, singles)
    print(f"  wrote {txt}", file=sys.stderr)
    write_cue(cue, args.mix, tracks)
    print(f"  wrote {cue}", file=sys.stderr)

    matched = sum(1 for h in hits.values() if h and "key" in h)
    print(f"\n{matched}/{len(hits)} clips matched; {len(tracks)} tracks "
          f"(+{len(singles)} single-hit) -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
