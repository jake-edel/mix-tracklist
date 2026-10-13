# mix-tracklist

Build a tracklist for a DJ mix by querying Shazam with short clips.

Every `--step` seconds, a `--clip`-second excerpt is cut with ffmpeg and sent
to Shazam. Hits are grouped by track, and each track's start time is estimated
from the clip position minus Shazam's reported offset into the track. Hits that
disagree on the start (a shared sample or loop heard minutes apart) are dropped.

The mix file is only read, never modified.

## Requirements

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- `ffmpeg` and `ffprobe` on `PATH` 

## Install

Run from the checkout (uv creates `.venv` from `uv.lock` on first run):

```sh
uv run mix-tracklist --help
```

Or install it as a standalone command on your `PATH`:

```sh
uv tool install .
# or: pip install .
```

## Usage

```sh
mix-tracklist "path/to/mix.opus" -o out/
```

Writes to the output directory:

| File | Contents |
| --- | --- |
| `<mix>.tracklist.txt` | `H:MM:SS  Artist - Title  [label, year]  (N hits)`, plus single-hit matches as comments |
| `<mix>.cue` | Cue sheet pointing at the mix file, one track per entry |
| `<mix>.shazam.json` | Raw per-clip hits, saved after every query |

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--clip` | 12 | clip length in seconds |
| `--step` | 30 | seconds between clips |
| `--workers` | 1 | parallel queries |
| `--delay` | 4 | minimum seconds between queries |
| `--min-hits` | 2 | hits needed to list a track (fewer go in the single-hit section) |

### Rate limits and resuming

Shazam returns HTTP 429 after a hundred or so quick queries. On a 429 all
queries pause, the wait doubling each time (30s, 60s, … up to 15 min). A clip
that still fails after 8 attempts is marked as an error.

The `.shazam.json` cache is reused on re-runs: cached hits are skipped and
failed clips are queried again, so an interrupted or partly failed scan picks
up where it stopped. Re-running on a fully cached mix just rebuilds the
tracklist, which makes it cheap to try a different `--min-hits`.

## Development

Dependencies are declared in `pyproject.toml` and pinned in `uv.lock`.

```sh
uv sync              # create/update .venv
uv add <package>     # add a dependency
uv lock --upgrade    # refresh pinned versions
```

`shazamio` is pinned exactly because the script relies on its `HTTPClient` and
`retry_options` internals to surface 429s instead of shazamio's silent retries.
