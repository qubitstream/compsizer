# compsizer

`compsizer` is a terminal directory browser with filesystem-specific size
statistics. On Linux, it reports Btrfs compression through `compsize`. On
Windows 10 or newer, it reports NTFS logical and allocated sizes.

Filesystem navigation does not wait for compression measurements. Directory
names appear after direct enumeration, and scan results update rows as the
bounded background scan manager receives them.

## Requirements

- Python 3.11 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Textual (installed from the script's PEP 723 metadata)

On Linux, `compsize` and a Btrfs filesystem are needed for Btrfs compression
statistics. GNU `du` from coreutils provides fallback size estimates. `sudo` is
needed only if a Btrfs scan requires elevated access.

On Windows 10 or newer, NTFS volumes provide logical and allocated-size
statistics. Other Windows filesystems remain browsable, but do not provide size
statistics. The app works from the standard Command Prompt (`cmd.exe`); Windows
Terminal is not required.

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

On Linux, if the starting path is on another filesystem, Compsizer shows a
warning but continues. A child directory can be a Btrfs mount. On Windows,
Compsizer selects the scanner from the volume for each directory scan.

## Run

Use the current directory as the initial location:

```console
uv run --script compsizer.py
```

Pass a different initial directory when needed:

```console
uv run --script compsizer.py /some/btrfs/path
```

On Windows, run the same command from `cmd.exe` and pass a Windows path when
needed:

```console
uv run --script compsizer.py C:\Users\name\Documents
```

On Unix-like systems, the script's `uv` shebang also allows an executable
checkout to start with `./compsizer.py`.

## Controls

- `Up`/`Down` or `j`/`k`: move the selection
- `Home`/`End`: select the first or last row on the current page
- `PageUp`/`PageDown`: show the previous or next directory page
- `Enter` or `l`: enter the selected directory
- `Backspace` or `h`: open the parent directory
- `Tab`: change pane focus
- `s`: cycle between size, compression-ratio, savings, and name sorting
- `c`: toggle the in-memory result cache
- `r`: refresh the current directory and rescan its children
- `g`: go to an absolute path or a path relative to the current directory;
  suggestions search child directory names by case-insensitive substring.
  The first match is selected. Use Up/Down to choose, Tab to complete, Enter to
  open the typed path, or Esc to cancel.
  The prompt shows at most 100 matches; refine the substring to narrow larger
  result sets.
  For UNC paths, add a divider after the share to suggest its child directories;
  server and share names are not suggested.
- `i`: show filesystem, scan method, byte sizes, available file counts,
  compression details, and diagnostics
- `?`: show help
- `q`: quit

Size sorting orders `compsize`, NTFS, and `du` results in separate groups. It
does not compare values across groups. Ratio and savings sorting use available
exact results. On NTFS, these values compare allocated bytes with logical
bytes; sparse files can affect them. Fallback rows sort after exact results.
Name sorting uses case-insensitive directory names. Pending, unavailable, and
error rows remain below rows with known values for numeric sorts; name sorting
includes every row in name order.

## Data and limitations

- Version 1 lists directories only.
- With `compsize`, the visible size is the uncompressed extent size. It is not
  the apparent size reported by the `Referenced` column. After sudo is
  declined, the visible size is `du`'s apparent size.
- The Flags column shows `C` when a scan finds a compressed extent on Btrfs or
  a compressed file on NTFS, `S` when an NTFS scan finds sparse files, and
  `?` when compression status is unknown. If neither `C` nor `?` appears, a
  complete scan found no compressed data.
- The `i` details view shows disk-usage and uncompressed bytes by Btrfs
  compression type when `compsize` reports them. It shows file counts when the scanner
  provides them. NTFS counts unique file identities, so hard links count once;
  `du` does not report a file count.
- On NTFS, the size column shows logical file bytes and the `Stored/Logical`
  column compares allocated bytes with logical bytes. NTFS counts appear in the
  selected-row status and tooltip. Sparse allocation affects the ratio, so it
  is not a compression-only measurement.
- NTFS scans read file metadata only and count a hard-linked file once per
  scanned tree. Automatic scans skip directory reparse points, including a row
  whose root is a junction or mount point. The rows remain browsable; after
  entering one, child directories are scanned according to their volume.
  Inaccessible paths produce a partial result or an error. Windows does not
  need an elevated process.
- A filesystem label appears after a directory name when its filesystem differs
  from the current location or does not support the platform's exact metrics.
  `[link]` marks a directory reparse point that the scanner skipped.
- Non-NTFS Windows filesystems can be browsed without size statistics.
- The bar uses a text-colored glyph (`▓`) for allocated space and the theme's
  success color for savings (`▒`) to the size baseline. Btrfs, NTFS, and estimate
  results use separate bar scales. A `~` prefix marks both numeric values on
  fallback rows: the allocated-space estimate in Ratio/Used and the apparent-size
  estimate in Size.
- `du` estimates are not Btrfs extent statistics. Shared extents can make
  allocated-space totals differ from unique physical usage.
- Independent child scans are not additive. Btrfs reflinks, deduplication,
  shared extents, and extent waste can make sibling measurements overlap.
- The in-memory cache lasts for one process. It is not persistent and has no
  automatic filesystem-change invalidation.
- Refresh is explicit. It invalidates the current directory and its child
  results, refreshes the corresponding tree entries, and starts new
  measurements.
- Partial `SIGUSR1` progress is not enabled yet. Rows update when their
  individual scans finish.
- `compsize -x` prevents scans from crossing filesystem boundaries.
- The right pane shows up to 100 child directories per page. Use `PageUp` and
  `PageDown` to browse every row. The tree shows up to 100 children per
  expanded node; use the right pane to browse additional directories.
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
