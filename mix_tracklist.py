"""List the tracks in a DJ mix by querying Shazam with short clips.

Every --step seconds, a --clip second excerpt is cut with ffmpeg and sent to
Shazam. Each track it recognises is listed once, in the order first heard.

Shazam rate-limits (HTTP 429) after a hundred or so quick queries. On a 429
the scan pauses, the wait doubling each time (30s, 60s, ... 15min).

Read-only on the mix. Writes <mix name>.tracklist.md (with links, for
digging), .tracklist.txt (plain lines, for embedding in tags) and .shazam.json
(raw hits, saved after every query and reused on re-runs, so an interrupted
scan resumes where it stopped) to --out, default the current directory.

    mix-tracklist "/mnt/hdd/Music/Mixes/HÖR Berlin/HÖR Berlin/02 - Tigerhead (2022-11-18).opus"
"""
import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote_plus

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
        "label": meta.get("Label"),
        "released": meta.get("Released"),
        "url": track.get("url"),
    }


async def scan(path, clip, step, delay, raw, retries=8):
    cache = json.loads(raw.read_text()) if raw.exists() else {}
    hits = {int(t): h for t, h in cache.items() if not (h or {}).get("error")}
    todo = [t for t in range(0, int(duration(path) - clip), step) if t not in hits]
    print(f"{path.name}: {len(hits)} clips cached, {len(todo)} to query", file=sys.stderr)
    # shazamio's default client silently retries 429s 20 times; surface them instead
    shazam = Shazam(segment_duration_seconds=clip,
                    http_client=HTTPClient(retry_options=ExponentialRetry(attempts=1)))
    backoff, start = 0, time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "clip.wav"
        for n, t in enumerate(todo, 1):
            cut(path, t, clip, dest)
            for attempt in range(1, retries + 1):
                try:
                    hits[t] = parse(await shazam.recognize(str(dest)))
                    backoff = 0
                    break
                except Exception as e:  # 429s arrive as a non-JSON body
                    backoff = min(900, backoff * 2 or 30)
                    print(f"  {fmt(t)}: {type(e).__name__} (attempt {attempt}/{retries}); "
                          f"pausing {backoff}s until {clock(backoff)}", file=sys.stderr)
                    await asyncio.sleep(backoff)
            else:
                hits[t] = {"error": True}  # re-queried on the next run
            raw.write_text(json.dumps(dict(sorted(hits.items())), indent=1, ensure_ascii=False))
            hit = hits[t]
            label = f"{hit['artist']} - {hit['title']}" if hit and "key" in hit else "-"
            eta = (time.monotonic() - start) / n * (len(todo) - n)
            print(f"  [{n}/{len(todo)} eta {fmt(eta)}] {fmt(t)}  {label}", file=sys.stderr)
            await asyncio.sleep(delay)
    return hits


def tracklist(hits):
    """One entry per track, in the order first heard, with how many clips matched it."""
    tracks = {}
    for t, hit in sorted(hits.items()):
        if hit and "key" in hit:
            tracks.setdefault(hit["key"], {**hit, "first": t, "hits": 0})["hits"] += 1
    return list(tracks.values())


def clock(in_sec):
    """Wall-clock time in_sec seconds from now, e.g. 14:05:30."""
    return time.strftime("%H:%M:%S", time.localtime(time.time() + in_sec))


def fmt(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def write_txt(path, tracks):
    path.write_text("".join(f"{fmt(x['first'])} {x['artist']} - {x['title']}\n" for x in tracks))


def write_md(path, mix, tracks):
    lines = [f"# {mix.stem}", ""]
    for x in tracks:
        name = f"{x['artist']} - {x['title']}"
        extra = ", ".join(v for v in (x["label"], x["released"]) if v)
        links = ([f"[Shazam]({x['url']})"] if x["url"] else []) + \
                [f"[YouTube Music](https://music.youtube.com/search?q={quote_plus(x['artist'] + ' ' + x['title'])})"]
        lines.append(" · ".join([f"- {fmt(x['first'])} **{name}**"] + ([extra] if extra else [])
                                + [f"{x['hits']}×"] + links))
    path.write_text("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mix", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path.cwd(), help="output directory (default: current directory)")
    ap.add_argument("--clip", type=int, default=12, help="clip length in seconds (default 12)")
    ap.add_argument("--step", type=int, default=30, help="seconds between clips (default 30)")
    ap.add_argument("--delay", type=float, default=4, help="seconds between queries (default 4)")
    args = ap.parse_args()
    if not args.mix.is_file():
        sys.exit(f"mix not found: {args.mix}")

    args.out.mkdir(parents=True, exist_ok=True)
    raw = args.out / f"{args.mix.stem}.shazam.json"
    hits = asyncio.run(scan(args.mix, args.clip, args.step, args.delay, raw))

    tracks = tracklist(hits)
    stem = args.out / f"{args.mix.stem}.tracklist"
    write_md(Path(f"{stem}.md"), args.mix, tracks)
    write_txt(Path(f"{stem}.txt"), tracks)
    matched = sum(1 for h in hits.values() if h and "key" in h)
    print(f"\n{len(tracks)} tracks from {matched}/{len(hits)} matched clips -> {stem}.{{md,txt}}", file=sys.stderr)


if __name__ == "__main__":
    main()
