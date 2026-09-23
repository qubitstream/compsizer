# compsizer

`compsizer` is a terminal browser for exploring Btrfs compression. It shows
the direct child directories of the current directory and measures each child
with the external `compsize` command.

Filesystem navigation does not wait for compression measurements. Directory
names appear after direct enumeration, and scan results update rows as the
bounded background scan manager receives them.

## Requirements

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Textual (installed from the script's PEP 723 metadata)
- `compsize` available in `PATH`
- A Btrfs filesystem for compression statistics

`compsize` uses Btrfs ioctls. Depending on the system, those operations may
require elevated privileges. Compsizer does not invoke `sudo`; it reports
permission and filesystem errors in the interface.

## Run

Use the current directory as the initial location:

```console
uv run --script compsizer.py
```

Pass a different initial directory when needed:

```console
uv run --script compsizer.py /some/btrfs/path
```

The script also has a `uv` shebang, so an executable checkout may be started
with `./compsizer.py`.

## Controls

- `Up`/`Down` or `j`/`k`: move the selection
- `Home`/`End`: select the first or last row
- `Enter` or `l`: enter the selected directory
- `Backspace` or `h`: open the parent directory
- `Tab`: change pane focus
- `s`: cycle between size, compression-ratio, and savings sorting
- `c`: toggle the in-memory result cache
- `r`: refresh the current directory and rescan its children
- `?`: show help
- `q`: quit

Size sorting uses uncompressed bytes, with the largest directory first.
Ratio sorting uses `disk usage / uncompressed size`, with the lowest ratio
first. Savings sorting uses `uncompressed size - disk usage`, with the
largest difference first. Pending and error rows remain below rows with
known values.

## Data and limitations

- Version 1 lists directories only.
- The visible size is `compsize`'s uncompressed extent size. It is not the
  apparent size reported by the `Referenced` column.
- The bar uses a solid glyph for disk usage and a separate glyph for the
  difference from uncompressed extent usage.
- Independent child scans are not additive. Btrfs reflinks, deduplication,
  shared extents, and extent waste can make sibling measurements overlap.
- The in-memory cache lasts for one process. It is not persistent and has no
  automatic filesystem-change invalidation.
- Refresh is explicit. It invalidates current child results and starts new
  measurements.
- Partial `SIGUSR1` progress is not enabled yet. Rows update when their
  individual scans finish.
- `compsize -x` prevents scans from crossing filesystem boundaries.
- All direct child directories remain queued for scanning. The selected and
  visible rows are prioritized so useful results appear sooner without making
  the sorted view incomplete.

## Development checks

Run the tests with the same runtime dependency used by the script:

```console
uv run --with textual python -m unittest discover -s tests -v
```

The project instructions also require Ruff and `ty` checks for
`compsizer.py`.
