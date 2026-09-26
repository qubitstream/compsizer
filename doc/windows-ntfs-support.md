# Windows and NTFS support

**Status:** Implemented; Windows validation and benchmarking pending

## Purpose

Add Windows support to Compsizer. On NTFS volumes, the app reports logical and
allocated sizes while it browses directories. The existing Btrfs workflow
remains available on Linux.

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

## Behavior

On Windows, select a scanner for the filesystem that contains each path being
scanned. Do not select a scanner only from the initial path; a browsed directory
can be on a different mounted volume.

- On Linux Btrfs, use the existing `compsize` backend.
- On Windows NTFS, use read-only Windows APIs to collect file attributes,
  identities, and size data, then aggregate results for each directory row.
- Keep directory reparse points browsable, but do not scan them as row roots or
  cross them during a scan of their parent. After a user opens one, scan its
  child directories using the volume that contains each child.
- On other Windows filesystems, allow directory browsing without size
  measurements. Do not display NTFS compression statistics for them.
- Do not change file compression state. Do not require an elevated Windows
  process. Report paths that the current user cannot read.

The NTFS UI should distinguish logical size from stored size. It may show a
compression ratio or savings when the selected size values support that
calculation. It should identify compressed files as NTFS-compressed, without
claiming an algorithm breakdown that the scanner does not provide.

## Measurement requirements

Windows exposes more than one relevant size. The scanner uses these values:

- `FILE_STANDARD_INFO.EndOfFile` is the logical size visible to applications.
- `FILE_STANDARD_INFO.AllocationSize` is the allocated-size measure shown for
  NTFS. It provides one metric for compressed, uncompressed, and sparse files.
- `FILE_BASIC_INFO.FileAttributes` identifies compressed, sparse, and reparse
  points. The UI keeps compression and sparsity distinct.
- `FILE_ID_INFO` identifies hard-linked files so each file is counted once in
  a scanned tree.

The implementation uses `GetFileInformationByHandleEx` with read-attributes
access. It does not open or read file contents. `GetCompressedFileSizeW` is
not used because its documented result is the compressed or sparse size for
those files, but the logical file size for ordinary uncompressed files. Those
values would not give consistent allocated-size totals.

Count a hard-linked file once within each scanned tree. Skip a directory
reparse point when it is the root of an automatic row scan. Do not recurse
through directory reparse points or follow file reparse points found below the
scan root. Keep directory reparse points navigable; after the user opens one,
scan its child directories using their own volumes. Separate directory scans
can overlap if the same file is linked into more than one tree.

No suitable read-only Windows command has been identified that provides the
required combined per-file compression, sparse, identity, and allocated-size
data as a recursive report. The implementation therefore uses a metadata-only
API traversal. `compact` can report compression state, but its compression and
uncompression options change files; do not use those options for scanning.
Benchmark the selected traversal on NTFS before treating its performance as
validated.

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
- Benchmark the Windows traversal on a large NTFS directory tree.

## Work plan

1. Audit Windows startup and select the scanner for each scanned path. Done in
   the implementation.
2. Implement a metadata-only NTFS scanner and focused tests. Done.
3. Add filesystem-specific UI labels and help text. Done.
4. Run Linux checks. Done. Run Windows integration checks from `cmd.exe` and
   benchmark the scanner on NTFS before release.

## Validation still needed

- Confirm the API results for compressed, uncompressed, sparse, and
  hard-linked files on Windows 10 or newer.
- Confirm interactive launch and navigation from standard `cmd.exe`.
- Record NTFS scan timing and memory use for a large directory tree.

## References

- [GetCompressedFileSizeW function](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getcompressedfilesizew)
- [FILE_STANDARD_INFO structure](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_standard_info)
- [FILE_ID_INFO structure](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info)
- [GetFileInformationByHandleEx function](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex)
- [File attribute constants](https://learn.microsoft.com/en-us/windows/win32/fileio/file-attribute-constants)
- [The `compact` command](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/compact)
