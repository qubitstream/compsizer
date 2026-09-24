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
- GNU `du` from coreutils available in `PATH` for fallback size estimates
- A Btrfs filesystem for compression statistics
- `sudo` available in `PATH` if elevated scan access is needed

`compsize` uses Btrfs ioctls. Depending on the system, those operations may
require elevated privileges. Compsizer first tries scans as the current user.
If a scan fails because of permissions, it asks before enabling elevated scans.
It may ask again if sudo credentials expire. Only `compsize` runs with `sudo`.
For elevated scans, Compsizer resolves `sudo` and `compsize` from standard
system directories, not from user-controlled `PATH` entries.
Your account must be allowed to run `compsize` with sudo. Sudo handles any
password prompt while the interface temporarily releases the terminal.
Compsizer does not read or store the password, and the interface itself does
not run as root. If authorization is declined or unavailable, the browser
stays open and shows GNU `du` estimates for apparent size and allocated space.
These estimates do not include Btrfs extent statistics. If `compsize` is
missing, the browser stays open and reports scan errors.

If the starting path is on another filesystem, Compsizer shows a warning but
continues. A child directory can be a Btrfs mount.

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
- `s`: cycle between size, compression-ratio, savings, and name sorting
- `c`: toggle the in-memory result cache
- `r`: refresh the current directory and rescan its children
- `?`: show help
- `q`: quit

Size sorting lists exact `compsize` results first, ordered by uncompressed
extent size. It then lists `du` estimates, ordered by apparent size. The two
groups are not compared with each other. Ratio sorting uses exact `compsize`
results, with the lowest ratio first. Savings sorting uses exact `compsize`
results, with the largest difference first. Fallback rows sort after exact
results for ratio and savings modes. Name sorting uses case-insensitive
directory names. Pending and error rows remain below rows with known values
for numeric sorts; name sorting includes every row in name order.

## Data and limitations

- Version 1 lists directories only.
- With `compsize`, the visible size is the uncompressed extent size. It is not
  the apparent size reported by the `Referenced` column. After sudo is
  declined, the visible size is `du`'s apparent size.
- The bar uses a solid glyph for allocated space and a separate glyph for the
  difference to the size baseline. Exact results and estimates use separate
  bar scales. A `~` prefix marks both numeric values for fallback rows: the
  allocated-space estimate in Ratio/Used and the apparent-size estimate in
  Size.
- `du` estimates are not Btrfs extent statistics. Shared extents can make
  allocated-space totals differ from unique physical usage.
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
