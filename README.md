# mix-tracklist

List the tracks in a DJ mix by querying Shazam with short clips.

Every `--step` seconds, a `--clip`-second excerpt is cut with ffmpeg and sent
to Shazam. Each track it recognises is listed once, in the order first heard,
as leads for finding new music. Misses and false positives (e.g. a sample
recognised as its source track) are left in rather than filtered out.

The mix file is only read, never modified.

## Requirements

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/) or pip
- `ffmpeg` and `ffprobe`

## Install

Run from the checkout (uv creates `.venv` from `uv.lock` on first run):

```sh
uv run mix-tracklist --help
```

Or install it as a standalone command on your `PATH` (`--editable` picks up
edits to the script without reinstalling):

```sh
uv tool install --editable .
# or: pip install .
```

## Usage

```sh
mix-tracklist "path/to/mix.opus"           # writes to the current directory
mix-tracklist "path/to/mix.opus" -o out/
```

Writes:

| File | Contents |
| --- | --- |
| `<mix>.tracklist.txt` | Plain `H:MM:SS Artist - Title` lines, for embedding in file metadata |
| `<mix>.tracklist.md` | Markdown list: time first heard, artist - title, label and year, number of matching clips, Shazam and YouTube Music search links |
| `<mix>.shazam.json` | Raw per-clip hits, saved after every query |

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| `-o`, `--out` | current directory | output directory |
| `--clip` | 12 | clip length in seconds |
| `--step` | 30 | seconds between clips |
| `--delay` | 4 | seconds between queries |

### Rate limits and resuming

Shazam returns HTTP 429 after a hundred or so quick queries. On a 429 the scan
pauses, the wait doubling each time (30s, 60s, … up to 15 min). A clip that
still fails after 8 attempts is marked as an error.

The `.shazam.json` cache is reused on re-runs: cached hits are skipped and
failed clips are queried again, so an interrupted or partly failed scan picks
up where it stopped. Re-running on a fully cached mix just rebuilds the
tracklist.

## Development

Dependencies are declared in `pyproject.toml` and pinned in `uv.lock`.

```sh
uv sync              # create/update .venv
uv add <package>     # add a dependency
uv lock --upgrade    # refresh pinned versions
```

`shazamio` is pinned exactly because the script relies on its `HTTPClient` and
`retry_options` internals to surface 429s instead of shazamio's silent retries.
