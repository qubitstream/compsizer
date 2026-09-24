# Windows and NTFS support

**Status:** Design proposal

## Purpose

Add Windows support to Compsizer. On NTFS volumes, the app should report useful
file-compression size data while it browses directories. Keep the existing
Btrfs workflow available on Linux.

This is a feature design, not a statement about current behavior. The current
app supports Btrfs scans through `compsize` and GNU `du` estimates on Linux.

## Goals

- Run the TUI on Windows 10 or newer under the normal user account.
- Support launch and interactive use from the standard Windows Command Prompt
  (`cmd.exe`).
- Browse directories without waiting for size scans.
- Measure logical and stored file sizes on NTFS and show compression results
  with clear labels.
- Keep scans bounded and asynchronous so a large directory does not block
  navigation.
- Prefer native Windows tools or filesystem APIs for size checks when they
  provide the required metrics. Avoid reading file contents to calculate sizes.
- Preserve the Btrfs scanner, its result semantics, and its consent-based
  elevation flow.
- Keep the single-script launch workflow and PEP 723 dependency metadata.
- Do not add a runtime dependency for Windows support without approval.

## Proposed behavior

Select a scanner for the filesystem that contains each path being scanned.
Do not select a scanner only from the initial path; a browsed directory can be
on a different mounted volume.

- On Linux Btrfs, use the existing `compsize` backend.
- On Windows NTFS, use read-only native Windows facilities to collect file
  compression and size data, then aggregate results for each directory row.
- On other Windows filesystems, allow directory browsing without size
  measurements. Do not display NTFS compression statistics for them.
- Do not change file compression state. Do not require an elevated Windows
  process. Report paths that the current user cannot read.

The NTFS UI should distinguish logical size from stored size. It may show a
compression ratio or savings when the selected size values support that
calculation. It should identify compressed files as NTFS-compressed, without
claiming an algorithm breakdown that the scanner does not provide.

## Measurement requirements

Windows exposes more than one relevant size. The scanner must define the
meaning of each value before it displays directory totals:

- Logical file size is the size visible to applications.
- Stored or allocated size is the disk-space measure used for the NTFS view.
- Compression state and sparse-file state must be considered separately.

`GetCompressedFileSizeW` is a candidate API. Microsoft documents that it
returns the compressed size for compressed files and the sparse size for
sparse files. The scanner must not treat a sparse file as compressed based
only on a smaller stored-size value. It must also validate how to measure
uncompressed files so that directory totals use consistent semantics.

Count a hard-linked file once within each scanned tree. Do not recurse through
directory reparse points. This prevents cycles and scans outside the selected
tree. Separate directory scans can overlap if the same file is linked into
more than one tree.

Prefer an OS-native recursive command when it provides the required size and
compression data with clear semantics. If no suitable read-only command does
this, use Windows filesystem APIs in a metadata-only traversal; do not read
file contents to calculate sizes. Benchmark the chosen approach. `compact` can
report compression state, but its compression and uncompression options change
files; do not use those options for scanning.

## Design constraints

- Keep the TUI, cache, navigation, and scan scheduler shared where their
  behavior is platform-independent.
- Keep filesystem-specific measurement and filesystem detection explicit.
- Keep the Btrfs parser and Btrfs-specific metrics separate in meaning from
  NTFS measurements. Do not label NTFS results as Btrfs extent statistics.
- A Windows-compatible build must not call Linux-only APIs such as
  `os.geteuid()` or read `/proc/self/mountinfo` during startup.
- GNU `du` and `sudo` are not Windows fallback mechanisms.
- This work does not include ZFS, ReFS deduplication, F2FS, APFS, or compressed
  read-only image filesystems.
- This work does not require a multi-file package or a change to the
  single-script project structure.

## Acceptance criteria

- `uv run --script compsizer.py` starts on a supported Windows system.
- Windows 10 and newer are supported.
- The app works interactively from the standard Windows Command Prompt
  (`cmd.exe`); Windows Terminal is not required.
- The user can browse an NTFS directory tree while scans run in the
  background.
- Other Windows filesystems remain browsable without size measurements.
- NTFS rows show logical and stored-size metrics with clear labels and valid
  ratio or savings values.
- Sparse files are not reported as NTFS-compressed only because their stored
  size is smaller than their logical size.
- Inaccessible paths produce a warning or error without stopping navigation.
- No scan changes file compression state or launches the TUI as Administrator.
- Linux Btrfs behavior and its existing tests remain intact.
- Tests cover NTFS compressed, uncompressed, sparse, inaccessible, hard-link,
  and reparse-point cases. Run the Windows-specific checks on Windows.

## Work plan

1. Audit Windows startup, terminal, path, and subprocess assumptions. Confirm
   the size APIs and accounting rules with a small NTFS prototype.
2. Implement the NTFS scanner and focused tests.
3. Select the scanner by the filesystem of each scanned path and update the
   UI labels and help text.
4. Run the existing Linux checks and the Windows-specific test suite.

## Remaining implementation questions

- Which native Windows tool or API gives the best combination of performance
  and the required per-file compression and size metrics?
- What are the consistent stored-size semantics for compressed,
  uncompressed, and sparse files?

## References

- [GetCompressedFileSizeW function](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getcompressedfilesizew)
- [File attribute constants](https://learn.microsoft.com/en-us/windows/win32/fileio/file-attribute-constants)
- [The `compact` command](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/compact)
