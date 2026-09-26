#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "textual>=8.2.8",
# ]
# ///

"""Browse directories and filesystem size statistics in a terminal UI."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import heapq
import logging
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any, ClassVar, Protocol

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import MountError
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static, Tree
from textual.widgets.tree import TreeNode

LOGGER = logging.getLogger("compsizer")

DEFAULT_CONCURRENCY = 2
DIRECTORY_PAGE_SIZE = 100
TREE_CHILD_LIMIT = 100
MAX_DIAGNOSTIC_LENGTH = 500
ROW_REFRESH_DELAY = 0.02
# Do not resolve elevated commands through the user's environment PATH.
SYSTEM_EXECUTABLE_DIRECTORIES: tuple[str, ...] = (
    (
        "/usr/local/sbin",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/bin",
        "/sbin",
        "/bin",
    )
    if os.name != "nt"
    else ()
)
SYSTEM_EXECUTABLE_PATH = os.pathsep.join(SYSTEM_EXECUTABLE_DIRECTORIES)
BAR_STYLE = "green"
NAME_COLUMN_WIDTH = 26
RATIO_COLUMN_WIDTH = 14
SIZE_COLUMN_WIDTH = 13
FLAGS_COLUMN_WIDTH = 5
BTRFS_COMPRESSED_TYPES = frozenset(("zlib", "lzo", "zstd"))
BTRFS_UNCOMPRESSED_TYPES = frozenset(("none", "prealloc"))

FILE_ATTRIBUTE_DIRECTORY = 0x0010
FILE_ATTRIBUTE_SPARSE_FILE = 0x0200
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
FILE_ATTRIBUTE_COMPRESSED = 0x0800
FILE_READ_ATTRIBUTES = 0x0080
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_INFO_CLASS_BASIC = 0
FILE_INFO_CLASS_STANDARD = 1
FILE_INFO_CLASS_ID = 18


class InvalidInitialPathError(ValueError):
    """Report an invalid command-line directory path."""


class ScanState(Enum):
    """States reported for a directory compression scan."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    ERROR = "error"
    UNAVAILABLE = "unavailable"


class ScanMethod(Enum):
    """Backends that can provide size measurements."""

    COMPSIZE = "compsize"
    DU = "du"
    NTFS_METADATA = "NTFS metadata"


class CompressionStatus(Enum):
    """Describe whether a scan found compressed data."""

    PRESENT = "found"
    ABSENT = "not found"
    UNKNOWN = "unknown"


class SortMode(Enum):
    """Available directory row sort criteria."""

    SIZE = "size"
    RATIO = "ratio"
    SAVINGS = "savings"
    NAME = "name"

    def toggled(self) -> SortMode:
        """Return the next user-facing sort criterion."""

        modes = (SortMode.SIZE, SortMode.RATIO, SortMode.SAVINGS, SortMode.NAME)
        return modes[(modes.index(self) + 1) % len(modes)]


class EntryKind(Enum):
    """Kinds of filesystem entries supported by the browser model."""

    DIRECTORY = "directory"
    FILE = "file"


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    """Represent one navigable filesystem entry."""

    path: Path
    name: str
    kind: EntryKind = EntryKind.DIRECTORY


@dataclass(frozen=True, slots=True)
class DirectoryListing:
    """Represent the direct child directory listing for a path."""

    path: Path
    entries: tuple[DirectoryEntry, ...] = ()
    warning: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CompressionTypeStats:
    """Represent disk-usage and uncompressed bytes for one Btrfs data type."""

    type_name: str
    disk_usage_bytes: int
    uncompressed_bytes: int


@dataclass(frozen=True, slots=True)
class ParsedCompsizeReport:
    """Represent byte totals, type rows, and findings from a report."""

    disk_usage_bytes: int
    uncompressed_bytes: int
    referenced_bytes: int
    compression_type_stats: tuple[CompressionTypeStats, ...] = ()
    warning: str | None = None
    empty: bool = False
    compression_status: CompressionStatus = CompressionStatus.UNKNOWN
    files_scanned: int | None = None

    @property
    def compression_types(self) -> tuple[str, ...]:
        """Return the distinct type labels in this report."""

        return tuple(stats.type_name for stats in self.compression_type_stats)

    def status_for_scan(self, *, complete: bool) -> CompressionStatus:
        """Avoid reporting no compression when a scan did not finish."""

        if self.compression_status is CompressionStatus.PRESENT:
            return CompressionStatus.PRESENT
        if not complete:
            return CompressionStatus.UNKNOWN
        return self.compression_status


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Represent one path's statistics, scan source, and diagnostics."""

    path: Path
    state: ScanState
    disk_usage_bytes: int | None = None
    uncompressed_bytes: int | None = None
    referenced_bytes: int | None = None
    warning: str | None = None
    error: str | None = None
    exit_code: int | None = None
    is_estimate: bool = False
    is_ntfs: bool = False
    ntfs_compressed_files: int | None = None
    ntfs_sparse_files: int | None = None
    filesystem_type: str | None = None
    scan_method: ScanMethod | None = None
    is_reparse_point: bool = False
    compression_status: CompressionStatus = CompressionStatus.UNKNOWN
    compression_type_stats: tuple[CompressionTypeStats, ...] = ()
    files_scanned: int | None = None

    @property
    def ratio(self) -> float | None:
        """Return the stored-to-logical ratio for exact results."""

        if (
            self.is_estimate
            or self.disk_usage_bytes is None
            or self.uncompressed_bytes in (None, 0)
        ):
            return None
        return self.disk_usage_bytes / self.uncompressed_bytes

    @property
    def ratio_fraction(self) -> Fraction | None:
        """Return the exact stored-to-logical ratio used for sorting."""

        if (
            self.is_estimate
            or self.disk_usage_bytes is None
            or self.uncompressed_bytes in (None, 0)
        ):
            return None
        return Fraction(self.disk_usage_bytes, self.uncompressed_bytes)

    @property
    def savings_bytes(self) -> int | None:
        """Return the logical-minus-stored byte difference for exact results."""

        if (
            self.is_estimate
            or self.disk_usage_bytes is None
            or self.uncompressed_bytes is None
        ):
            return None
        return self.uncompressed_bytes - self.disk_usage_bytes

    @property
    def has_statistics(self) -> bool:
        """Return whether byte statistics are present on the result."""

        return (
            self.disk_usage_bytes is not None
            and self.uncompressed_bytes is not None
            and (self.is_ntfs or self.referenced_bytes is not None)
        )

    @property
    def ntfs_summary(self) -> str | None:
        """Describe NTFS-compressed and sparse files in this directory."""

        if (
            not self.is_ntfs
            or self.disk_usage_bytes is None
            or self.uncompressed_bytes is None
            or self.ntfs_compressed_files is None
            or self.ntfs_sparse_files is None
        ):
            return None
        return (
            f"NTFS-compressed files: {self.ntfs_compressed_files}; "
            f"sparse files: {self.ntfs_sparse_files}."
        )

    @property
    def compression_types(self) -> tuple[str, ...]:
        """Return the distinct compression type labels reported by the scan."""

        return tuple(stats.type_name for stats in self.compression_type_stats)

    @classmethod
    def pending(cls, path: Path) -> ScanResult:
        """Create a pending result for ``path``."""

        return cls(path=path, state=ScanState.PENDING)

    @classmethod
    def running(cls, path: Path) -> ScanResult:
        """Create a running result for ``path``."""

        return cls(path=path, state=ScanState.RUNNING)

    @classmethod
    def error_result(
        cls,
        path: Path,
        message: str,
        *,
        exit_code: int | None = None,
        scan_method: ScanMethod | None = None,
    ) -> ScanResult:
        """Create an error result without hiding the failing path."""

        return cls(
            path=path,
            state=ScanState.ERROR,
            error=message,
            exit_code=exit_code,
            scan_method=scan_method,
        )

    @classmethod
    def unavailable_result(cls, path: Path, message: str) -> ScanResult:
        """Create a terminal result when the filesystem has no supported metrics."""

        return cls(path=path, state=ScanState.UNAVAILABLE, warning=message)


@dataclass(frozen=True, slots=True)
class _CompsizeAttempt:
    """Represent a scan result with permission or authentication retry details."""

    result: ScanResult
    permission_required: bool = False
    sudo_authentication_failed: bool = False


@dataclass(frozen=True, slots=True)
class DirectoryRecord:
    """Pair a directory entry with its current scan state."""

    entry: DirectoryEntry
    result: ScanResult
    ordinal: int


@dataclass(frozen=True, slots=True)
class ScanRequest:
    """Describe a directory that the scan manager should measure."""

    path: Path
    view_id: int
    priority: int


@dataclass(frozen=True, slots=True)
class ScanJob:
    """Identify one scheduled scan and the view that requested it."""

    request_id: int
    path: Path
    view_id: int
    priority: int


class CompsizeParseError(ValueError):
    """Report output that cannot be interpreted as a compsize report."""


def path_key(path: Path) -> str:
    """Return a stable local identity for a normalized or absolute path."""

    return os.path.normcase(os.path.abspath(os.fspath(path)))


def normalize_initial_path(value: str | Path) -> Path:
    """Resolve and validate the initial directory path.

    :param value: User-supplied path.
    :returns: An absolute, resolved directory path.
    :raises InvalidInitialPathError: If the path cannot be resolved to a
        directory.
    """

    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InvalidInitialPathError(
            f"Cannot resolve initial path {candidate}: {exc}"
        ) from exc
    if not resolved.is_dir():
        raise InvalidInitialPathError(f"Initial path is not a directory: {candidate}")
    return resolved


def absolute_child_path(parent: Path, name: str) -> Path:
    """Build an absolute child path without resolving symlinks."""

    return Path(os.path.abspath(os.path.join(os.fspath(parent), name)))


class FilesystemDetector:
    """Read filesystem types from Linux mount information."""

    @staticmethod
    def read_mountinfo() -> str | None:
        """Read the current process mount table when it is available."""

        try:
            return Path("/proc/self/mountinfo").read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return None

    @staticmethod
    def filesystem_type_for_path(path: Path) -> str | None:
        """Return the mounted filesystem type for a path, when available."""

        mountinfo = FilesystemDetector.read_mountinfo()
        if mountinfo is None:
            return None
        return FilesystemDetector.filesystem_type(path, mountinfo)

    @staticmethod
    def filesystem_type(path: Path, mountinfo: str) -> str | None:
        """Return the filesystem type mounted at ``path``, if it is known."""

        best_mount: tuple[int, int, str] | None = None
        for line in mountinfo.splitlines():
            mount_fields, separator, filesystem_fields = line.partition(" - ")
            if not separator:
                continue
            mount_tokens = mount_fields.split()
            filesystem_tokens = filesystem_fields.split()
            if len(mount_tokens) < 5 or not filesystem_tokens:
                continue
            try:
                mount_id = int(mount_tokens[0])
            except ValueError:
                continue
            mountpoint_text = re.sub(
                r"\\([0-7]{3})",
                lambda match: chr(int(match.group(1), 8)),
                mount_tokens[4],
            )
            mountpoint = Path(mountpoint_text)
            try:
                path.relative_to(mountpoint)
            except ValueError:
                continue
            candidate = (len(mountpoint.parts), mount_id, filesystem_tokens[0])
            if best_mount is None or candidate[:2] > best_mount[:2]:
                best_mount = candidate
        return best_mount[2] if best_mount is not None else None


class _FileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("creation_time", ctypes.c_longlong),
        ("last_access_time", ctypes.c_longlong),
        ("last_write_time", ctypes.c_longlong),
        ("change_time", ctypes.c_longlong),
        ("attributes", ctypes.c_uint32),
    ]


class _FileStandardInfo(ctypes.Structure):
    _fields_ = [
        ("allocation_size", ctypes.c_longlong),
        ("end_of_file", ctypes.c_longlong),
        ("number_of_links", ctypes.c_uint32),
        ("delete_pending", ctypes.c_ubyte),
        ("directory", ctypes.c_ubyte),
    ]


class _FileIdInfo(ctypes.Structure):
    _fields_ = [
        ("volume_serial_number", ctypes.c_uint64),
        ("file_id", ctypes.c_ubyte * 16),
    ]


@dataclass(frozen=True, slots=True)
class WindowsFileMetadata:
    """Represent the read-only metadata needed for an NTFS scan."""

    file_identity: tuple[int, bytes] | None
    logical_size: int
    allocated_size: int
    is_directory: bool
    is_reparse_point: bool
    is_compressed: bool
    is_sparse: bool


class WindowsMetadataProvider(Protocol):
    """Provide filesystem and file metadata for the Windows scan runner."""

    def filesystem_type(self, path: Path) -> str:
        """Return the filesystem name for the volume that contains ``path``."""

    def inspect_path(self, path: Path) -> WindowsFileMetadata:
        """Read one path's identity, sizes, and file attributes."""


class WindowsFileApi:
    """Read volume and file metadata through Windows Kernel32 APIs."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows file APIs are only available on Windows.")
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise OSError("This Python runtime does not provide Windows APIs.")

        self._kernel32: Any = loader("kernel32", use_last_error=True)
        self._bind(
            "GetVolumePathNameW",
            (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32),
            ctypes.c_int,
        )
        self._bind(
            "GetVolumeInformationW",
            (
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.c_wchar_p,
                ctypes.c_uint32,
            ),
            ctypes.c_int,
        )
        self._bind(
            "CreateFileW",
            (
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_void_p,
            ),
            ctypes.c_void_p,
        )
        self._bind(
            "GetFileInformationByHandleEx",
            (ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32),
            ctypes.c_int,
        )
        self._bind("CloseHandle", (ctypes.c_void_p,), ctypes.c_int)
        self._bind("GetLastError", (), ctypes.c_uint32)

    def _bind(self, name: str, argtypes: tuple[Any, ...], restype: Any) -> None:
        """Set the ctypes signature for one Kernel32 function."""

        function = getattr(self._kernel32, name)
        function.argtypes = argtypes
        function.restype = restype

    def filesystem_type(self, path: Path) -> str:
        """Return the filesystem name for the volume that contains ``path``."""

        volume_path = ctypes.create_unicode_buffer(32768)
        if not self._kernel32.GetVolumePathNameW(
            self._extended_path(path), volume_path, len(volume_path)
        ):
            raise self._last_error(f"Cannot identify the volume for {path}")

        volume_label = ctypes.create_unicode_buffer(261)
        filesystem_name = ctypes.create_unicode_buffer(64)
        volume_serial = ctypes.c_uint32()
        maximum_component_length = ctypes.c_uint32()
        filesystem_flags = ctypes.c_uint32()
        if not self._kernel32.GetVolumeInformationW(
            volume_path.value,
            volume_label,
            len(volume_label),
            ctypes.byref(volume_serial),
            ctypes.byref(maximum_component_length),
            ctypes.byref(filesystem_flags),
            filesystem_name,
            len(filesystem_name),
        ):
            raise self._last_error(f"Cannot read filesystem information for {path}")
        return filesystem_name.value

    def inspect_path(self, path: Path) -> WindowsFileMetadata:
        """Read path metadata without opening file contents."""

        invalid_handle = ctypes.c_void_p(-1).value
        handle = self._kernel32.CreateFileW(
            self._extended_path(path),
            FILE_READ_ATTRIBUTES,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle is None or handle == invalid_handle:
            raise self._last_error(f"Cannot inspect {path}")

        try:
            basic_info = self._query_file_info(
                handle,
                FILE_INFO_CLASS_BASIC,
                _FileBasicInfo(),
            )
            attributes = basic_info.attributes
            is_directory = bool(attributes & FILE_ATTRIBUTE_DIRECTORY)
            is_reparse_point = bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)
            is_compressed = bool(attributes & FILE_ATTRIBUTE_COMPRESSED)
            is_sparse = bool(attributes & FILE_ATTRIBUTE_SPARSE_FILE)
            if is_directory or is_reparse_point:
                return WindowsFileMetadata(
                    file_identity=None,
                    logical_size=0,
                    allocated_size=0,
                    is_directory=is_directory,
                    is_reparse_point=is_reparse_point,
                    is_compressed=is_compressed,
                    is_sparse=is_sparse,
                )

            standard_info = self._query_file_info(
                handle,
                FILE_INFO_CLASS_STANDARD,
                _FileStandardInfo(),
            )
            id_info = self._query_file_info(
                handle,
                FILE_INFO_CLASS_ID,
                _FileIdInfo(),
            )
            if standard_info.end_of_file < 0 or standard_info.allocation_size < 0:
                raise OSError(f"Windows returned an invalid size for {path}")
            identity = (
                int(id_info.volume_serial_number),
                bytes(id_info.file_id),
            )
            return WindowsFileMetadata(
                file_identity=identity,
                logical_size=int(standard_info.end_of_file),
                allocated_size=int(standard_info.allocation_size),
                is_directory=False,
                is_reparse_point=False,
                is_compressed=is_compressed,
                is_sparse=is_sparse,
            )
        finally:
            self._kernel32.CloseHandle(handle)

    def _query_file_info(self, handle: int, info_class: int, info: Any) -> Any:
        """Read one documented file-information class from an open handle."""

        if not self._kernel32.GetFileInformationByHandleEx(
            handle, info_class, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise self._last_error("Cannot read file metadata")
        return info

    def _last_error(self, operation: str) -> OSError:
        """Create an error that includes the Windows API error code."""

        error_code = int(self._kernel32.GetLastError())
        return OSError(f"{operation} (Windows error {error_code})")

    @staticmethod
    def _extended_path(path: Path) -> str:
        """Prepare a path for Unicode Win32 calls, including long paths."""

        absolute_path = os.path.abspath(os.fspath(path))
        if absolute_path.startswith("\\\\?\\"):
            return absolute_path
        if absolute_path.startswith("\\\\"):
            return f"\\\\?\\UNC\\{absolute_path[2:]}"
        return f"\\\\?\\{absolute_path}"


def _scandir_path(path: Path) -> str:
    """Return a path that supports long-path directory enumeration on Windows."""

    if os.name == "nt":
        return WindowsFileApi._extended_path(path)
    return os.fspath(path)


def enumerate_directories(path: Path) -> DirectoryListing:
    """Enumerate direct child directories without following symlinks.

    The function does not recurse. It is suitable for running in a worker
    thread so a slow filesystem cannot block the Textual event loop.
    """

    entries: list[DirectoryEntry] = []
    warnings: list[str] = []
    try:
        with os.scandir(_scandir_path(path)) as directory:
            for entry in directory:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    entries.append(
                        DirectoryEntry(
                            path=absolute_child_path(path, entry.name),
                            name=entry.name,
                        )
                    )
                except OSError as exc:
                    warnings.append(f"Could not inspect {entry.name!r}: {exc}")
    except OSError as exc:
        return DirectoryListing(
            path=path,
            error=f"Cannot read directory {path}: {exc}",
        )

    entries.sort(
        key=lambda item: (item.name.casefold(), item.name, path_key(item.path))
    )
    return DirectoryListing(
        path=path,
        entries=tuple(entries),
        warning="; ".join(warnings) if warnings else None,
    )


def diagnostic_text(value: str, *, limit: int = MAX_DIAGNOSTIC_LENGTH) -> str:
    """Normalize and bound command diagnostics for display."""

    normalized = " ".join(line.strip() for line in value.splitlines() if line.strip())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1]}…"


def _is_empty_report_message(message: str) -> bool:
    """Return whether one compsize line reports an empty input."""

    normalized = message.strip().casefold()
    if normalized in {"no files", "no files."}:
        return True
    return re.match(r"processed\s+0\s+files\b", normalized) is not None


def _is_empty_compsize_report(stdout: str, stderr: str) -> bool:
    """Recognize compsize's normal empty-input response."""

    stdout_messages = [line.strip() for line in stdout.splitlines() if line.strip()]
    if stdout_messages:
        return (
            len(stdout_messages) == 1
            and _is_empty_report_message(stdout_messages[0])
            and not stderr.strip()
        )
    stderr_messages = [line.strip() for line in stderr.splitlines() if line.strip()]
    return bool(stderr_messages) and all(
        _is_empty_report_message(message) for message in stderr_messages
    )


def _parse_byte_columns(tokens: list[str], line: str) -> tuple[int, int, int]:
    """Parse the three byte columns from one compsize data row."""

    if len(tokens) < 5:
        raise CompsizeParseError(f"Incomplete compsize data row: {line.strip()!r}")
    try:
        values = (int(tokens[2]), int(tokens[3]), int(tokens[4]))
    except ValueError as exc:
        raise CompsizeParseError(
            f"Invalid byte value in compsize row: {line.strip()!r}"
        ) from exc
    if any(value < 0 for value in values):
        raise CompsizeParseError(
            f"Negative byte value in compsize row: {line.strip()!r}"
        )
    return values


def parse_compsize_output(stdout: str, stderr: str = "") -> ParsedCompsizeReport:
    """Parse byte-oriented ``compsize`` output.

    :param stdout: Standard output from ``compsize -b -x``.
    :param stderr: Standard error from the same process.
    :returns: The last complete report found in the output.
    :raises CompsizeParseError: If no complete total row is present.
    """

    if _is_empty_compsize_report(stdout, stderr):
        return ParsedCompsizeReport(
            disk_usage_bytes=0,
            uncompressed_bytes=0,
            referenced_bytes=0,
            warning=None,
            empty=True,
            compression_status=CompressionStatus.ABSENT,
            files_scanned=0,
        )

    total: tuple[int, int, int] | None = None
    type_totals: dict[str, list[int]] = {}
    files_scanned: int | None = None
    has_compressed_data = False
    has_unknown_type_data = False
    has_data_type_row = False
    for line in stdout.splitlines():
        tokens = line.split()
        if not tokens:
            continue
        if (
            len(tokens) >= 3
            and tokens[0].casefold() == "processed"
            and tokens[2].casefold().startswith("file")
        ):
            total = None
            type_totals = {}
            try:
                reported_file_count = int(tokens[1])
            except ValueError:
                files_scanned = None
            else:
                files_scanned = (
                    reported_file_count if reported_file_count >= 0 else None
                )
            has_compressed_data = False
            has_unknown_type_data = False
            has_data_type_row = False
            continue
        if tokens[0].upper() == "TOTAL":
            total = _parse_byte_columns(tokens, line)
            continue
        if len(tokens) < 5 or not tokens[1].endswith("%"):
            continue
        try:
            type_sizes = _parse_byte_columns(tokens, line)
        except CompsizeParseError:
            continue
        type_name = tokens[0]
        totals = type_totals.setdefault(type_name, [0, 0, 0])
        for index, size in enumerate(type_sizes):
            totals[index] += size
        if any(type_sizes):
            has_data_type_row = True
            normalized_type = type_name.casefold()
            if normalized_type in BTRFS_COMPRESSED_TYPES:
                has_compressed_data = True
            elif normalized_type not in BTRFS_UNCOMPRESSED_TYPES:
                has_unknown_type_data = True

    if total is None:
        processed_zero = any(
            _is_empty_report_message(line) for line in stdout.splitlines()
        )
        if processed_zero and not stderr.strip():
            return ParsedCompsizeReport(
                0,
                0,
                0,
                empty=True,
                compression_status=CompressionStatus.ABSENT,
                files_scanned=0,
            )
        detail = diagnostic_text(stderr) or "No TOTAL row was found."
        raise CompsizeParseError(f"Could not parse compsize output: {detail}")

    warning = diagnostic_text(stderr) or None
    if has_compressed_data:
        compression_status = CompressionStatus.PRESENT
    elif (
        warning is not None
        or has_unknown_type_data
        or (any(total) and not has_data_type_row)
    ):
        compression_status = CompressionStatus.UNKNOWN
    else:
        compression_status = CompressionStatus.ABSENT
    compression_type_stats = tuple(
        CompressionTypeStats(
            type_name=type_name,
            disk_usage_bytes=type_sizes[0],
            uncompressed_bytes=type_sizes[1],
        )
        for type_name, type_sizes in type_totals.items()
    )
    return ParsedCompsizeReport(
        disk_usage_bytes=total[0],
        uncompressed_bytes=total[1],
        referenced_bytes=total[2],
        compression_type_stats=compression_type_stats,
        warning=warning,
        compression_status=compression_status,
        files_scanned=files_scanned,
    )


class ScanRunner(Protocol):
    """Protocol implemented by asynchronous compression scanners."""

    async def scan(self, path: Path) -> ScanResult:
        """Scan one directory."""

    async def close(self) -> None:
        """Stop active scanner resources."""


@dataclass(frozen=True, slots=True)
class _DuMeasurement:
    """Keep one ``du`` size with any diagnostics from that traversal."""

    size_bytes: int | None
    warning: str | None = None
    error: str | None = None


class DuRunner:
    """Measure apparent and allocated directory sizes with GNU ``du``."""

    def __init__(self, executable: str = "du") -> None:
        self.executable = executable
        self._processes: set[asyncio.subprocess.Process] = set()

    async def scan(self, path: Path) -> ScanResult:
        """Return ``du`` size estimates for one directory."""

        allocated = await self._measure(path, apparent=False)
        apparent = await self._measure(path, apparent=True)
        messages: list[str] = []
        for label, measurement in (
            ("allocated space", allocated),
            ("apparent size", apparent),
        ):
            if measurement.error:
                messages.append(f"Could not measure {label}: {measurement.error}")
            if measurement.warning:
                messages.append(measurement.warning)

        if allocated.size_bytes is None and apparent.size_bytes is None:
            detail = "; ".join(messages) or "du did not return a size."
            return ScanResult.error_result(
                path,
                detail,
                scan_method=ScanMethod.DU,
            )

        return ScanResult(
            path=path,
            state=ScanState.COMPLETE,
            disk_usage_bytes=allocated.size_bytes,
            uncompressed_bytes=apparent.size_bytes,
            warning=" ".join(messages) or None,
            is_estimate=True,
            scan_method=ScanMethod.DU,
        )

    async def _measure(self, path: Path, *, apparent: bool) -> _DuMeasurement:
        """Run one allocated-space or apparent-size traversal."""

        command = [
            self.executable,
            "--summarize",
            "--block-size=1",
            "--one-file-system",
        ]
        if apparent:
            command.append("--apparent-size")
        command.extend(("--", os.fspath(path)))
        environment = os.environ.copy()
        environment["LC_ALL"] = "C"
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
        except FileNotFoundError:
            return _DuMeasurement(
                None,
                error=f"{self.executable!r} was not found in PATH.",
            )
        except OSError as exc:
            return _DuMeasurement(None, error=f"Unable to start du: {exc}")

        self._processes.add(process)
        try:
            stdout_bytes, stderr_bytes = await process.communicate()
        except asyncio.CancelledError:
            await self._terminate_process(process)
            raise
        finally:
            self._processes.discard(process)

        size_bytes = self._parse_size(stdout_bytes)
        diagnostic = diagnostic_text(stderr_bytes.decode(errors="replace")) or None
        exit_code = process.returncode
        if size_bytes is None:
            detail = diagnostic or "du returned no size."
            if exit_code not in (None, 0) and diagnostic is None:
                detail = f"du exited with status {exit_code} without a size."
            return _DuMeasurement(None, error=detail)
        if exit_code not in (None, 0) and diagnostic is None:
            diagnostic = f"du exited with status {exit_code}; totals may be partial."
        return _DuMeasurement(size_bytes, warning=diagnostic)

    @staticmethod
    def _parse_size(stdout: bytes) -> int | None:
        """Parse the byte count before ``du``'s tab-separated path."""

        first_line = stdout.partition(b"\n")[0]
        size_text, separator, _path = first_line.partition(b"\t")
        if not separator or not size_text.isdigit():
            return None
        return int(size_text)

    async def close(self) -> None:
        """Terminate active ``du`` processes."""

        processes = tuple(self._processes)
        if processes:
            await asyncio.gather(
                *(self._terminate_process(process) for process in processes),
                return_exceptions=True,
            )

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        """Terminate one ``du`` process and drain its pipes."""

        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.communicate(), timeout=1.0)
        except asyncio.TimeoutError:
            if process.returncode is None:
                process.kill()
            await process.communicate()


class WindowsScanRunner:
    """Measure NTFS directories with read-only metadata APIs on worker threads."""

    def __init__(
        self,
        file_api: WindowsMetadataProvider | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        if concurrency < 1:
            raise ValueError("Scan concurrency must be at least one.")
        self.file_api = file_api if file_api is not None else WindowsFileApi()
        self._scan_slots = asyncio.Semaphore(concurrency)
        self._cancellation_events: set[threading.Event] = set()
        self._worker_tasks: set[asyncio.Task[ScanResult]] = set()

    async def scan(self, path: Path) -> ScanResult:
        """Select metrics by volume and scan NTFS paths without reading file data."""

        await self._scan_slots.acquire()
        cancellation = threading.Event()
        self._cancellation_events.add(cancellation)
        worker = asyncio.create_task(asyncio.to_thread(self._scan, path, cancellation))
        self._worker_tasks.add(worker)
        worker.add_done_callback(
            lambda completed: self._scan_finished(completed, cancellation)
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancellation.set()
            raise

    def _scan_finished(
        self,
        worker: asyncio.Task[ScanResult],
        cancellation: threading.Event,
    ) -> None:
        """Release one scan slot when its worker thread has stopped."""

        self._worker_tasks.discard(worker)
        self._cancellation_events.discard(cancellation)
        self._scan_slots.release()

    def _scan(self, path: Path, cancellation: threading.Event) -> ScanResult:
        """Detect one volume and return its NTFS scan or unsupported status."""

        if cancellation.is_set():
            return ScanResult.error_result(path, "The scan was canceled.")

        try:
            root_metadata = self.file_api.inspect_path(path)
        except OSError as exc:
            return ScanResult.error_result(path, f"Could not inspect {path}: {exc}")
        if not root_metadata.is_directory:
            return ScanResult.error_result(
                path, f"Scan target is not a directory: {path}"
            )
        if cancellation.is_set():
            return ScanResult.error_result(path, "The scan was canceled.")
        if root_metadata.is_reparse_point:
            return ScanResult(
                path=path,
                state=ScanState.UNAVAILABLE,
                warning=(
                    "Skipped this directory reparse point. "
                    "Open it to browse its target."
                ),
                is_reparse_point=True,
            )

        try:
            filesystem_type = self.file_api.filesystem_type(path)
        except OSError as exc:
            return ScanResult.error_result(
                path,
                f"Could not identify the filesystem for {path}: {exc}",
            )
        if filesystem_type.casefold() != "ntfs":
            filesystem_label = filesystem_type or "an unknown filesystem"
            return ScanResult(
                path=path,
                state=ScanState.UNAVAILABLE,
                warning=f"Size measurements are unavailable on {filesystem_label}.",
                filesystem_type=filesystem_type,
            )
        if cancellation.is_set():
            return replace(
                ScanResult.error_result(path, "The scan was canceled."),
                filesystem_type=filesystem_type,
                scan_method=ScanMethod.NTFS_METADATA,
            )

        return replace(
            self._scan_ntfs(path, cancellation),
            filesystem_type=filesystem_type,
            scan_method=ScanMethod.NTFS_METADATA,
        )

    def _scan_ntfs(
        self,
        path: Path,
        cancellation: threading.Event,
    ) -> ScanResult:
        """Aggregate unique file sizes and attributes below one NTFS directory."""

        directories = [path]
        seen_files: set[tuple[int, bytes]] = set()
        logical_size = 0
        allocated_size = 0
        measured_files = 0
        compressed_files = 0
        sparse_files = 0
        skipped_reparse_points = 0
        failed_paths = 0
        diagnostics: list[str] = []

        while directories and not cancellation.is_set():
            directory_path = directories.pop()
            try:
                directory = os.scandir(_scandir_path(directory_path))
            except OSError as exc:
                failed_paths += 1
                self._add_diagnostic(diagnostics, directory_path, exc)
                continue

            with directory:
                try:
                    for entry in directory:
                        if cancellation.is_set():
                            break
                        entry_path = Path(entry.path)
                        try:
                            metadata = self.file_api.inspect_path(entry_path)
                        except OSError as exc:
                            failed_paths += 1
                            self._add_diagnostic(diagnostics, entry_path, exc)
                            continue

                        if metadata.is_reparse_point:
                            skipped_reparse_points += 1
                            continue
                        if metadata.is_directory:
                            directories.append(entry_path)
                            continue
                        if metadata.file_identity is None:
                            failed_paths += 1
                            self._add_diagnostic(
                                diagnostics,
                                entry_path,
                                OSError("Windows did not return a file identity"),
                            )
                            continue
                        if metadata.file_identity in seen_files:
                            continue

                        seen_files.add(metadata.file_identity)
                        measured_files += 1
                        logical_size += metadata.logical_size
                        allocated_size += metadata.allocated_size
                        if metadata.is_compressed:
                            compressed_files += 1
                        if metadata.is_sparse:
                            sparse_files += 1
                except OSError as exc:
                    failed_paths += 1
                    self._add_diagnostic(diagnostics, directory_path, exc)

        if cancellation.is_set():
            return ScanResult.error_result(path, "The NTFS scan was canceled.")

        warning_parts: list[str] = []
        if skipped_reparse_points:
            warning_parts.append(f"Skipped {skipped_reparse_points} reparse point(s).")
        error: str | None = None
        if failed_paths:
            error = (
                f"Could not inspect {failed_paths} path(s); NTFS totals are partial."
            )
            if diagnostics:
                error = diagnostic_text(f"{error} {'; '.join(diagnostics)}")

        statistics_available = failed_paths == 0 or measured_files > 0
        if compressed_files:
            compression_status = CompressionStatus.PRESENT
        elif failed_paths or skipped_reparse_points:
            compression_status = CompressionStatus.UNKNOWN
        else:
            compression_status = CompressionStatus.ABSENT
        return ScanResult(
            path=path,
            state=ScanState.ERROR if failed_paths else ScanState.COMPLETE,
            disk_usage_bytes=allocated_size if statistics_available else None,
            uncompressed_bytes=logical_size if statistics_available else None,
            warning=" ".join(warning_parts) or None,
            error=error,
            is_ntfs=True,
            ntfs_compressed_files=compressed_files if statistics_available else None,
            ntfs_sparse_files=sparse_files if statistics_available else None,
            compression_status=compression_status,
            files_scanned=measured_files,
        )

    @staticmethod
    def _add_diagnostic(
        diagnostics: list[str],
        path: Path,
        error: OSError,
    ) -> None:
        """Keep a bounded sample of failures from a partial tree scan."""

        if len(diagnostics) < 10:
            message = diagnostic_text(str(error)) or type(error).__name__
            diagnostics.append(diagnostic_text(f"{path}: {message}"))

    async def close(self) -> None:
        """Stop active filesystem walks and wait for their worker threads."""

        for cancellation in tuple(self._cancellation_events):
            cancellation.set()
        workers = tuple(self._worker_tasks)
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)


PrivilegeAuthorization = Callable[[Sequence[str]], Awaitable[bool]]


class CompsizeRunner:
    """Run ``compsize`` and request elevation only when a scan needs it."""

    def __init__(
        self,
        executable: str = "compsize",
        *,
        sudo_executable: str = "sudo",
        authorization_callback: PrivilegeAuthorization | None = None,
        fallback_runner: ScanRunner | None = None,
    ) -> None:
        self.executable = executable
        self.sudo_executable = sudo_executable
        self.authorization_callback = authorization_callback
        self.fallback_runner: ScanRunner = (
            fallback_runner if fallback_runner is not None else DuRunner()
        )
        self._processes: set[asyncio.subprocess.Process] = set()
        effective_uid = getattr(os, "geteuid", None)
        self._running_as_root: bool = callable(effective_uid) and effective_uid() == 0
        self._sudo_enabled: bool = False
        self._authorization_declined: bool = False
        self._authorization_lock: asyncio.Lock = asyncio.Lock()

    async def scan(self, path: Path) -> ScanResult:
        """Scan one path and attach its filesystem and measurement backend."""

        filesystem_type = await asyncio.to_thread(
            FilesystemDetector.filesystem_type_for_path,
            path,
        )
        result = await self._scan_with_privilege_policy(path)
        return replace(result, filesystem_type=filesystem_type)

    async def _scan_with_privilege_policy(self, path: Path) -> ScanResult:
        """Scan ``path``, offering one sudo prompt after a permission failure."""

        if self._running_as_root:
            return (await self._run_scan(path, elevated=False)).result

        if self._authorization_declined:
            return await self._fallback_scan(
                path,
                "Elevated scans were declined; showing du estimates.",
            )

        if self._sudo_enabled:
            attempt = await self._run_scan(path, elevated=True)
            if not attempt.sudo_authentication_failed:
                return attempt.result
            return await self._reauthorize(path)

        attempt = await self._run_scan(path, elevated=False)
        if not attempt.permission_required:
            return attempt.result
        return await self._enable_elevated_scans(path)

    def reset_privilege_decision(self) -> None:
        """Allow another authorization prompt after an explicit refresh."""

        self._authorization_declined = False

    async def _enable_elevated_scans(self, path: Path) -> ScanResult:
        """Retry one permission-denied scan as root and authorize if needed."""

        async with self._authorization_lock:
            if self._sudo_enabled:
                attempt = await self._run_scan(path, elevated=True)
                if not attempt.sudo_authentication_failed:
                    return attempt.result
                self._sudo_enabled = False
                self._authorization_declined = False
                return await self._request_authorization(path)

            if self._authorization_declined:
                return await self._fallback_scan(
                    path,
                    "Elevated scans were declined; showing du estimates.",
                )

            return await self._request_authorization(path)

    async def _reauthorize(self, path: Path) -> ScanResult:
        """Ask for authorization again when sudo credentials have expired."""

        async with self._authorization_lock:
            if self._sudo_enabled:
                attempt = await self._run_scan(path, elevated=True)
                if not attempt.sudo_authentication_failed:
                    return attempt.result
                self._sudo_enabled = False
                self._authorization_declined = False
            if self._authorization_declined:
                return await self._fallback_scan(
                    path,
                    "Sudo authorization was declined; showing du estimates.",
                )
            return await self._request_authorization(path)

    async def _request_authorization(self, path: Path) -> ScanResult:
        """Prompt once, then retry with sudo without an in-app password prompt."""

        command = self._authorization_command()
        if command is None:
            self._authorization_declined = True
            return await self._fallback_scan(
                path,
                "Sudo or compsize was not found in trusted system directories; "
                "showing du estimates.",
            )
        if self.authorization_callback is None:
            self._authorization_declined = True
            return await self._fallback_scan(
                path,
                "Elevated scans are unavailable; showing du estimates.",
            )

        if not await self.authorization_callback(command):
            self._authorization_declined = True
            return await self._fallback_scan(
                path,
                "Sudo authorization was declined or failed; showing du estimates.",
            )

        attempt = await self._run_scan(path, elevated=True)
        if attempt.sudo_authentication_failed:
            self._authorization_declined = True
            return await self._fallback_scan(
                path,
                "Sudo could not run compsize; showing du estimates.",
            )

        self._sudo_enabled = True
        return attempt.result

    async def _fallback_scan(self, path: Path, reason: str) -> ScanResult:
        """Return unprivileged ``du`` estimates after elevation is unavailable."""

        result = await self.fallback_runner.scan(path)
        if result.state is ScanState.ERROR:
            detail = result.error or "du could not measure this directory."
            return replace(result, error=f"{reason} {detail}")
        warning = " ".join(part for part in (reason, result.warning) if part)
        return replace(result, warning=warning)

    def _authorization_command(self) -> list[str] | None:
        """Build a sudo command that can authenticate without scanning data."""

        sudo_path = self._trusted_executable_path(self.sudo_executable)
        executable_path = self._trusted_executable_path(self.executable)
        if sudo_path is None or executable_path is None:
            return None
        return [sudo_path, "--", executable_path, "--help"]

    @staticmethod
    def _trusted_executable_path(executable: str) -> str | None:
        """Resolve an elevated command from system paths, not a user PATH."""

        if not SYSTEM_EXECUTABLE_PATH:
            return None
        candidate = shutil.which(executable, path=SYSTEM_EXECUTABLE_PATH)
        if candidate is None:
            return None
        try:
            resolved = Path(candidate).resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not resolved.is_file():
            return None
        for directory in SYSTEM_EXECUTABLE_PATH.split(os.pathsep):
            try:
                trusted_directory = Path(directory).resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if resolved.is_relative_to(trusted_directory):
                return os.fspath(resolved)
        return None

    async def _run_scan(self, path: Path, *, elevated: bool) -> _CompsizeAttempt:
        """Run one scan and classify permission and sudo failures."""

        command = [self.executable, "-b", "-x", "--", os.fspath(path)]
        if self._running_as_root and not elevated:
            executable_path = self._trusted_executable_path(self.executable)
            if executable_path is None:
                message = (
                    f"Unable to run {self.executable!r}: executable not found "
                    "in trusted system directories."
                )
                return _CompsizeAttempt(
                    ScanResult.error_result(
                        path,
                        message,
                        scan_method=ScanMethod.COMPSIZE,
                    )
                )
            command = [executable_path, "-b", "-x", "--", os.fspath(path)]
        if elevated:
            sudo_path = self._trusted_executable_path(self.sudo_executable)
            executable_path = self._trusted_executable_path(self.executable)
            if sudo_path is None or executable_path is None:
                missing = []
                if sudo_path is None:
                    missing.append(f"sudo executable {self.sudo_executable!r}")
                if executable_path is None:
                    missing.append(f"compsize executable {self.executable!r}")
                message = (
                    "Unable to start elevated scans; these trusted executables were "
                    f"not found: {', '.join(missing)}."
                )
                return _CompsizeAttempt(
                    ScanResult.error_result(
                        path,
                        message,
                        scan_method=ScanMethod.COMPSIZE,
                    )
                )
            command = [
                sudo_path,
                "-n",
                "--",
                executable_path,
                "-b",
                "-x",
                "--",
                os.fspath(path),
            ]
        environment = os.environ.copy()
        environment["LC_ALL"] = "C"
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
        except FileNotFoundError:
            executable_name = "sudo" if elevated else "compsize"
            message = (
                f"Unable to start {executable_name} executable {command[0]!r}: "
                "file not found."
            )
            return _CompsizeAttempt(
                ScanResult.error_result(
                    path,
                    message,
                    scan_method=ScanMethod.COMPSIZE,
                )
            )
        except OSError as exc:
            executable_name = "sudo" if elevated else "compsize"
            return _CompsizeAttempt(
                ScanResult.error_result(
                    path,
                    f"Unable to start {executable_name} executable "
                    f"{command[0]!r}: {exc}",
                    scan_method=ScanMethod.COMPSIZE,
                )
            )

        self._processes.add(process)
        try:
            stdout_bytes, stderr_bytes = await process.communicate()
        except asyncio.CancelledError:
            await self._terminate_process(process)
            raise
        finally:
            self._processes.discard(process)

        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")
        exit_code = process.returncode
        sudo_authentication_failed = (
            elevated and exit_code not in (None, 0) and self._is_sudo_failure(stderr)
        )
        if sudo_authentication_failed:
            return _CompsizeAttempt(
                ScanResult.error_result(
                    path,
                    diagnostic_text(stderr) or "Sudo could not run compsize.",
                    exit_code=exit_code,
                    scan_method=ScanMethod.COMPSIZE,
                ),
                sudo_authentication_failed=True,
            )

        permission_required = self._has_permission_error(stderr)
        try:
            report = parse_compsize_output(stdout, stderr)
        except CompsizeParseError as exc:
            detail = diagnostic_text(stderr)
            message = str(exc)
            if detail and detail not in message:
                message = f"{message} ({detail})"
            return _CompsizeAttempt(
                ScanResult.error_result(
                    path,
                    message,
                    exit_code=exit_code,
                    scan_method=ScanMethod.COMPSIZE,
                ),
                permission_required=permission_required,
            )

        if report.empty:
            return _CompsizeAttempt(
                ScanResult(
                    path=path,
                    state=ScanState.COMPLETE,
                    disk_usage_bytes=0,
                    uncompressed_bytes=0,
                    referenced_bytes=0,
                    warning=report.warning,
                    exit_code=exit_code,
                    scan_method=ScanMethod.COMPSIZE,
                    files_scanned=report.files_scanned,
                    compression_status=report.status_for_scan(
                        complete=not permission_required and exit_code in (None, 0)
                    ),
                ),
                permission_required=permission_required,
            )
        if permission_required:
            return _CompsizeAttempt(
                ScanResult(
                    path=path,
                    state=ScanState.ERROR,
                    disk_usage_bytes=report.disk_usage_bytes,
                    uncompressed_bytes=report.uncompressed_bytes,
                    referenced_bytes=report.referenced_bytes,
                    compression_type_stats=report.compression_type_stats,
                    warning=report.warning,
                    error=diagnostic_text(stderr),
                    exit_code=exit_code,
                    scan_method=ScanMethod.COMPSIZE,
                    files_scanned=report.files_scanned,
                    compression_status=report.status_for_scan(complete=False),
                ),
                permission_required=True,
            )
        if exit_code not in (None, 0):
            error = (
                diagnostic_text(stderr) or f"compsize exited with status {exit_code}."
            )
            return _CompsizeAttempt(
                ScanResult(
                    path=path,
                    state=ScanState.ERROR,
                    disk_usage_bytes=report.disk_usage_bytes,
                    uncompressed_bytes=report.uncompressed_bytes,
                    referenced_bytes=report.referenced_bytes,
                    compression_type_stats=report.compression_type_stats,
                    warning=report.warning,
                    error=error,
                    exit_code=exit_code,
                    scan_method=ScanMethod.COMPSIZE,
                    files_scanned=report.files_scanned,
                    compression_status=report.status_for_scan(complete=False),
                )
            )
        return _CompsizeAttempt(
            ScanResult(
                path=path,
                state=ScanState.COMPLETE,
                disk_usage_bytes=report.disk_usage_bytes,
                uncompressed_bytes=report.uncompressed_bytes,
                referenced_bytes=report.referenced_bytes,
                compression_type_stats=report.compression_type_stats,
                warning=report.warning,
                exit_code=exit_code,
                scan_method=ScanMethod.COMPSIZE,
                files_scanned=report.files_scanned,
                compression_status=report.status_for_scan(complete=True),
            )
        )

    @staticmethod
    def _has_permission_error(diagnostic: str) -> bool:
        """Return whether a compsize diagnostic reports a permission denial."""

        normalized = diagnostic.casefold()
        return (
            "operation not permitted" in normalized or "permission denied" in normalized
        )

    @staticmethod
    def _is_sudo_failure(diagnostic: str) -> bool:
        """Recognize messages emitted by sudo before it runs compsize."""

        normalized = diagnostic.lstrip().casefold()
        return normalized.startswith("sudo:") or "not allowed to execute" in normalized

    async def close(self) -> None:
        """Terminate all subprocesses owned by this runner."""

        processes = tuple(self._processes)
        if processes:
            await asyncio.gather(
                *(self._terminate_process(process) for process in processes),
                return_exceptions=True,
            )
        await self.fallback_runner.close()

    @staticmethod
    async def _terminate_process(process: asyncio.subprocess.Process) -> None:
        """Terminate a process and drain its pipes."""

        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.communicate(), timeout=1.0)
        except asyncio.TimeoutError:
            if process.returncode is None:
                process.kill()
            await process.communicate()


class ResultCache:
    """Keep successful scan results in memory for one application run."""

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self._entries: dict[str, ScanResult] = {}

    @property
    def enabled(self) -> bool:
        """Return whether reads and writes to the cache are enabled."""

        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Enable or bypass the cache without deleting existing entries."""

        self._enabled = enabled

    def get(self, path: Path) -> ScanResult | None:
        """Return a cached complete result when cache use is enabled."""

        if not self._enabled:
            return None
        return self._entries.get(path_key(path))

    def put(self, result: ScanResult) -> None:
        """Store a complete result when cache use is enabled."""

        if self._enabled and result.state is ScanState.COMPLETE:
            self._entries[path_key(result.path)] = result

    def invalidate(self, paths: Iterable[Path]) -> None:
        """Remove cached results for the supplied paths."""

        for path in paths:
            self._entries.pop(path_key(path), None)

    def clear(self) -> None:
        """Remove all cached results."""

        self._entries.clear()

    def __len__(self) -> int:
        """Return the number of retained cache entries."""

        return len(self._entries)


def sort_records(
    records: Iterable[DirectoryRecord],
    mode: SortMode,
) -> list[DirectoryRecord]:
    """Sort records with known values first and unknown rows stable below."""

    def key(record: DirectoryRecord) -> tuple[int, object, str, str]:
        result = record.result
        name_key = record.entry.name.casefold()
        identity = path_key(record.entry.path)
        if mode is SortMode.NAME:
            return (0, name_key, "", identity)
        if (
            mode is SortMode.SIZE
            and result.state is ScanState.COMPLETE
            and result.uncompressed_bytes is not None
        ):
            source_order = 2 if result.is_estimate else 1 if result.is_ntfs else 0
            return (source_order, -result.uncompressed_bytes, name_key, identity)
        if mode is SortMode.RATIO and result.state is ScanState.COMPLETE:
            ratio = result.ratio_fraction
            if ratio is not None:
                return (0, ratio, name_key, identity)
        if mode is SortMode.SAVINGS and result.state is ScanState.COMPLETE:
            savings = result.savings_bytes
            if savings is not None:
                return (0, -savings, name_key, identity)
        return (3, record.ordinal, name_key, identity)

    return sorted(records, key=key)


class BrowserModel:
    """Hold navigation, selection, cache, and current directory row state."""

    def __init__(self, initial_path: Path, cache: ResultCache) -> None:
        self.cache = cache
        self.current_path = initial_path
        self.sort_mode = SortMode.SIZE
        self.view_id = 0
        self.records: dict[str, DirectoryRecord] = {}
        self._state_counts: dict[ScanState, int] = {state: 0 for state in ScanState}
        self.listing_warning: str | None = None
        self.listing_error: str | None = None
        self.selected_path: Path | None = None
        self._selection_by_directory: dict[str, Path] = {}

    def begin_view(self, path: Path, *, refresh: bool = False) -> int:
        """Start a new view before any filesystem or subprocess work.

        A refresh invalidates cached statistics for ``path`` and its current
        direct child directories.
        """

        if self.selected_path is not None:
            self._selection_by_directory[path_key(self.current_path)] = (
                self.selected_path
            )
        if refresh:
            self.cache.invalidate(
                [path, *(record.entry.path for record in self.records.values())]
            )
        self.current_path = path
        self.view_id += 1
        self.records = {}
        self._state_counts = {state: 0 for state in ScanState}
        self.listing_warning = None
        self.listing_error = None
        self.selected_path = self._selection_by_directory.get(path_key(path))
        return self.view_id

    def set_listing(self, view_id: int, listing: DirectoryListing) -> list[ScanRequest]:
        """Apply a direct listing and return missing-statistics requests."""

        if view_id != self.view_id:
            return []
        self.records = {}
        self._state_counts = {state: 0 for state in ScanState}
        self.listing_warning = listing.warning
        self.listing_error = listing.error
        for ordinal, entry in enumerate(listing.entries):
            cached = self.cache.get(entry.path)
            result = cached if cached is not None else ScanResult.pending(entry.path)
            self.records[path_key(entry.path)] = DirectoryRecord(entry, result, ordinal)
            self._state_counts[result.state] += 1
        if (
            self.selected_path is None
            or path_key(self.selected_path) not in self.records
        ):
            ordered = self.sorted_records()
            self.selected_path = ordered[0].entry.path if ordered else None
        return self.requests_for_missing_results()

    def sorted_records(self) -> list[DirectoryRecord]:
        """Return current rows in the active sort order."""

        return sort_records(self.records.values(), self.sort_mode)

    def select(self, path: Path | None) -> None:
        """Select a current row by path identity."""

        if path is None or path_key(path) not in self.records:
            return
        self.selected_path = path
        self._selection_by_directory[path_key(self.current_path)] = path

    def move_selection(self, delta: int) -> None:
        """Move selection by ``delta`` rows while retaining path identity."""

        ordered = self.sorted_records()
        if not ordered:
            self.selected_path = None
            return
        if self.selected_path is None:
            self.select(ordered[0].entry.path)
            return
        current_key = path_key(self.selected_path)
        current_index = next(
            (
                index
                for index, record in enumerate(ordered)
                if path_key(record.entry.path) == current_key
            ),
            0,
        )
        target_index = max(0, min(len(ordered) - 1, current_index + delta))
        self.select(ordered[target_index].entry.path)

    def apply_scan_update(self, job: ScanJob, result: ScanResult) -> bool:
        """Cache a result and apply it only if it belongs to the current view."""

        self.cache.put(result)
        if job.view_id != self.view_id:
            return False
        key = path_key(result.path)
        record = self.records.get(key)
        if record is None:
            return False
        self._state_counts[record.result.state] -= 1
        self._state_counts[result.state] += 1
        self.records[key] = DirectoryRecord(record.entry, result, record.ordinal)
        return True

    @property
    def state_counts(self) -> dict[ScanState, int]:
        """Return counts of current rows in each scan state."""

        return self._state_counts.copy()

    def requests_for_missing_results(
        self,
        visible_paths: Iterable[Path] = (),
    ) -> list[ScanRequest]:
        """Return pending and running scans, prioritizing visible rows.

        Error rows remain visible until a refresh or navigation starts a new
        view. Running rows stay in the request set so view updates do not
        cancel their active work.
        """

        ordered = self.sorted_records()
        visible_keys = {path_key(path) for path in visible_paths}
        selected_key = path_key(self.selected_path) if self.selected_path else None
        offscreen_start = len(ordered)
        requests: list[ScanRequest] = []
        for index, record in enumerate(ordered):
            if record.result.state in {
                ScanState.COMPLETE,
                ScanState.ERROR,
                ScanState.UNAVAILABLE,
            }:
                continue
            key = path_key(record.entry.path)
            if key == selected_key:
                priority = -1
            elif key in visible_keys:
                priority = index
            else:
                priority = offscreen_start + index
            requests.append(ScanRequest(record.entry.path, self.view_id, priority))
        return requests


ScanCallback = Callable[[ScanJob, ScanResult], Awaitable[None]]


class ScanManager:
    """Schedule a bounded number of scans and discard obsolete queued work."""

    def __init__(
        self,
        runner: ScanRunner,
        callback: ScanCallback,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        if concurrency < 1:
            raise ValueError("Scan concurrency must be at least one.")
        self.runner = runner
        self.callback = callback
        self.concurrency = concurrency
        self._condition = asyncio.Condition()
        self._pending: dict[str, ScanJob] = {}
        self._pending_heap: list[tuple[int, int, str]] = []
        self._active: dict[int, tuple[ScanJob, asyncio.Task[ScanResult]]] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._next_request_id = 0
        self._view_id: int | None = None
        self._closed = False

    async def start(self) -> None:
        """Start the fixed worker pool."""

        if self._workers:
            return
        self._workers = [
            asyncio.create_task(self._worker(), name=f"compsizer-scan-{index}")
            for index in range(self.concurrency)
        ]

    async def set_view(self, view_id: int, requests: Iterable[ScanRequest]) -> None:
        """Replace or update the jobs associated with the current browser view."""

        request_map: dict[str, ScanRequest] = {}
        for request in requests:
            key = path_key(request.path)
            existing = request_map.get(key)
            if existing is None or request.priority < existing.priority:
                request_map[key] = request

        async with self._condition:
            if self._closed or (self._view_id is not None and view_id < self._view_id):
                return
            view_changed = self._view_id != view_id
            self._view_id = view_id
            desired_keys = set(request_map)
            for job, task in tuple(self._active.values()):
                if (
                    view_changed
                    or job.view_id != view_id
                    or path_key(job.path) not in desired_keys
                ):
                    task.cancel()
            if view_changed:
                self._pending.clear()
                self._pending_heap.clear()
            else:
                for key in tuple(self._pending):
                    if key not in desired_keys:
                        del self._pending[key]
            active_paths = {
                path_key(job.path)
                for job, _task in self._active.values()
                if job.view_id == view_id
            }
            new_heap_entries: list[tuple[int, int, str]] = []
            for key, request in request_map.items():
                if key in active_paths:
                    continue
                existing = self._pending.get(key)
                if existing is not None and existing.priority == request.priority:
                    continue
                self._next_request_id += 1
                job = ScanJob(
                    request_id=self._next_request_id,
                    path=request.path,
                    view_id=view_id,
                    priority=request.priority,
                )
                self._pending[key] = job
                new_heap_entries.append((job.priority, job.request_id, key))
            if view_changed:
                self._pending_heap = new_heap_entries
                heapq.heapify(self._pending_heap)
            else:
                for entry in new_heap_entries:
                    heapq.heappush(self._pending_heap, entry)
            self._compact_pending_heap()
            self._condition.notify_all()

    async def close(self) -> None:
        """Cancel queued work, active scans, and worker tasks."""

        async with self._condition:
            if self._closed:
                return
            self._closed = True
            self._pending.clear()
            self._pending_heap.clear()
            for _job, task in self._active.values():
                task.cancel()
            self._condition.notify_all()
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        await self.runner.close()

    async def _worker(self) -> None:
        """Run jobs until the manager is closed."""

        while True:
            async with self._condition:
                while not self._pending and not self._closed:
                    await self._condition.wait()
                if self._closed:
                    return
                job = self._pop_pending()
                if job is None:
                    continue

            scan_task = asyncio.create_task(self.runner.scan(job.path))
            self._active[job.request_id] = (job, scan_task)
            try:
                await self._emit(job, ScanResult.running(job.path))
                result = await scan_task
            except asyncio.CancelledError:
                current_task = asyncio.current_task()
                if current_task is not None and current_task.cancelling():
                    raise
                continue
            except Exception as exc:  # pragma: no cover - process boundary safeguard
                LOGGER.exception("Unexpected scan failure for %s", job.path)
                result = ScanResult.error_result(
                    job.path, f"Unexpected scan failure: {exc}"
                )
            finally:
                self._active.pop(job.request_id, None)
                if not scan_task.done():
                    scan_task.cancel()
                    await asyncio.gather(scan_task, return_exceptions=True)
            await self._emit(job, result)

    def _pop_pending(self) -> ScanJob | None:
        """Pop the next current job, ignoring stale heap entries."""

        while self._pending_heap:
            _priority, request_id, key = heapq.heappop(self._pending_heap)
            job = self._pending.get(key)
            if job is None or job.request_id != request_id:
                continue
            del self._pending[key]
            return job
        return None

    def _compact_pending_heap(self) -> None:
        """Rebuild the heap when stale entries exceed the live queue."""

        if len(self._pending_heap) <= max(64, 2 * len(self._pending)):
            return
        self._pending_heap = [
            (job.priority, job.request_id, key) for key, job in self._pending.items()
        ]
        heapq.heapify(self._pending_heap)

    async def _emit(self, job: ScanJob, result: ScanResult) -> None:
        """Send one update without allowing a UI callback to kill a worker."""

        try:
            await self.callback(job, result)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - application boundary safeguard
            LOGGER.exception("Unable to publish scan update for %s", job.path)


def _take_cells(value: str, width: int, *, from_end: bool = False) -> str:
    """Take a string prefix or suffix that fits a terminal cell width."""

    if width <= 0:
        return ""
    if not from_end:
        result = ""
        for character in value:
            candidate = result + character
            if cell_len(candidate) > width:
                break
            result = candidate
        return result
    result = ""
    for character in reversed(value):
        candidate = character + result
        if cell_len(candidate) > width:
            break
        result = candidate
    return result


def truncate_middle(value: str, width: int) -> str:
    """Truncate a display name while retaining its beginning and end."""

    if width <= 0:
        return ""
    if cell_len(value) <= width:
        return value
    if width == 1:
        return "…"
    content_width = width - 1
    left_width = (content_width + 1) // 2
    right_width = content_width // 2
    return f"{_take_cells(value, left_width)}…{_take_cells(value, right_width, from_end=True)}"


def pad_right(value: str, width: int) -> str:
    """Pad a terminal string on the right to a cell width."""

    return f"{value}{' ' * max(0, width - cell_len(value))}"


def pad_left(value: str, width: int) -> str:
    """Pad a terminal string on the left to a cell width."""

    return f"{' ' * max(0, width - cell_len(value))}{value}"


def format_bytes(value: int | None) -> str:
    """Format bytes with compact binary units for the size column."""

    if value is None:
        return "—"
    if value < 1024:
        return f"{value} B"
    units = ("KiB", "MiB", "GiB", "TiB", "PiB")
    amount = float(value)
    for unit in units:
        amount /= 1024
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
    return f"{value} B"


def format_ratio(ratio: float | None) -> str:
    """Format a disk-usage ratio as a percentage."""

    if ratio is None:
        return "—"
    return f"{ratio * 100:.1f}%"


@dataclass(frozen=True, slots=True)
class BarScales:
    """Keep bar normalization separate for each result metric source."""

    compsize_maximum: int
    estimate_maximum: int
    ntfs_maximum: int = 0

    @classmethod
    def from_records(cls, records: Iterable[DirectoryRecord]) -> BarScales:
        """Calculate independent maxima from complete rows."""

        compsize_maximum = 0
        estimate_maximum = 0
        ntfs_maximum = 0
        for record in records:
            result = record.result
            if result.state is not ScanState.COMPLETE:
                continue
            size = max(
                result.uncompressed_bytes or 0,
                result.disk_usage_bytes or 0,
            )
            if result.is_ntfs:
                ntfs_maximum = max(ntfs_maximum, size)
            elif result.is_estimate:
                estimate_maximum = max(estimate_maximum, size)
            else:
                compsize_maximum = max(compsize_maximum, size)
        return cls(compsize_maximum, estimate_maximum, ntfs_maximum)

    def maximum_for(self, result: ScanResult) -> int:
        """Return the scale for the result's metric source."""

        if result.is_ntfs:
            return self.ntfs_maximum
        return self.estimate_maximum if result.is_estimate else self.compsize_maximum

    def include(self, result: ScanResult) -> BarScales:
        """Return scales that include one complete result, if present."""

        if result.state is not ScanState.COMPLETE:
            return self
        size = max(
            result.uncompressed_bytes or 0,
            result.disk_usage_bytes or 0,
        )
        if result.is_ntfs:
            return BarScales(
                self.compsize_maximum,
                self.estimate_maximum,
                max(self.ntfs_maximum, size),
            )
        if result.is_estimate:
            return BarScales(
                self.compsize_maximum,
                max(self.estimate_maximum, size),
                self.ntfs_maximum,
            )
        return BarScales(
            max(self.compsize_maximum, size),
            self.estimate_maximum,
            self.ntfs_maximum,
        )


def render_bar(
    result: ScanResult,
    maximum_size: int,
    width: int,
) -> Text:
    """Render allocated usage and the difference to the size baseline."""

    bar = Text()
    if width <= 0:
        return bar
    if (
        result.state is ScanState.COMPLETE
        and result.uncompressed_bytes is not None
        and result.disk_usage_bytes is not None
    ):
        if maximum_size <= 0:
            bar.append(" " * width)
            return bar
        logical_fraction = max(0.0, result.uncompressed_bytes / maximum_size)
        disk_fraction = max(0.0, result.disk_usage_bytes / maximum_size)
        logical_cells = min(width, round(width * logical_fraction))
        disk_cells = min(width, round(width * disk_fraction))
        bar.append("█" * disk_cells, style=BAR_STYLE)
        bar.append("░" * max(0, logical_cells - disk_cells), style=BAR_STYLE)
        bar.append(" " * max(0, width - max(logical_cells, disk_cells)))
        return bar
    if result.state is ScanState.UNAVAILABLE:
        bar.append("·" * width, style="dim")
        return bar
    if result.state is ScanState.ERROR:
        bar.append("!" * width, style="red")
    else:
        bar.append("·" * width, style="dim")
    return bar


def _directory_graph_width(width: int) -> int:
    """Return the flexible graph width used by directory rows and headers."""

    column_widths = (
        NAME_COLUMN_WIDTH + RATIO_COLUMN_WIDTH + SIZE_COLUMN_WIDTH + FLAGS_COLUMN_WIDTH
    )
    minimum_width = column_widths + 6
    fixed_width = column_widths + 5
    return max(1, max(width, minimum_width) - fixed_width)


class DirectoryColumnHeader(Static):
    """Render column labels using the same flexible width as directory rows."""

    def __init__(self, ratio_label: str, size_label: str) -> None:
        super().__init__(markup=False, id="column-label")
        self.ratio_label: str = ratio_label
        self.size_label: str = size_label

    def text_for_width(self, width: int) -> Text:
        """Build labels aligned to a directory row of the given width."""

        graph_width = _directory_graph_width(width)
        bar_label = _take_cells("Bar", graph_width)

        header = Text()
        header.append(pad_right("Directory", NAME_COLUMN_WIDTH))
        header.append(" ")
        header.append(pad_right(bar_label, graph_width))
        header.append(" ")
        header.append(pad_left(self.ratio_label, RATIO_COLUMN_WIDTH))
        header.append(" ")
        header.append(pad_left(self.size_label, SIZE_COLUMN_WIDTH))
        header.append(" ")
        header.append(pad_right("Flags", FLAGS_COLUMN_WIDTH))
        return header

    def render(self) -> Text:
        """Render labels using the header's current allocated width."""

        return self.text_for_width(self.size.width)

    def on_resize(self, _event: events.Resize) -> None:
        """Refresh labels when the content pane changes width."""

        self.refresh()


class DirectoryRow(Static):
    """Render one directory record with responsive columns."""

    def __init__(
        self,
        record: DirectoryRecord,
        bar_scales: BarScales,
        filesystem_badge: str | None = None,
    ) -> None:
        super().__init__(markup=False, classes="directory-row")
        self.record: DirectoryRecord = record
        self.bar_scales: BarScales = bar_scales
        self.filesystem_badge: str | None = filesystem_badge
        self.tooltip = self._tooltip_text(record.result)

    @staticmethod
    def _tooltip_text(result: ScanResult) -> str | None:
        """Combine errors, warnings, and filesystem-specific row details."""

        details = [result.error] if result.error else []
        if result.warning and (result.is_ntfs or not result.error):
            details.append(result.warning)
        if result.ntfs_summary:
            details.append(result.ntfs_summary)
        return " ".join(item for item in details if item) or None

    def update_record(
        self,
        record: DirectoryRecord,
        bar_scales: BarScales,
        filesystem_badge: str | None = None,
    ) -> None:
        """Replace row data and refresh its display."""

        self.record = record
        self.bar_scales = bar_scales
        self.filesystem_badge = filesystem_badge
        self.tooltip = self._tooltip_text(record.result)
        self.refresh()

    def on_resize(self, _event: events.Resize) -> None:
        """Refresh the bar when the terminal width changes."""

        self.refresh()

    def render(self) -> Text:
        """Build a row using fixed data columns and a flexible graph."""

        graph_width = _directory_graph_width(self.size.width)
        result = self.record.result
        name = self.record.entry.name
        if self.filesystem_badge:
            name = f"{name} [{self.filesystem_badge}]"
        name = truncate_middle(name, NAME_COLUMN_WIDTH)
        ratio_or_used = (
            format_bytes(result.disk_usage_bytes)
            if result.is_estimate
            else format_ratio(result.ratio)
        )
        if result.is_estimate and result.disk_usage_bytes is not None:
            ratio_or_used = f"~{ratio_or_used}"
        size = format_bytes(result.uncompressed_bytes)
        if result.is_estimate and result.uncompressed_bytes is not None:
            size = f"~{size}"
        flags = self._flags_text(result)
        if result.state is ScanState.ERROR and not result.has_statistics:
            ratio_or_used = "error"
            size = "error"
        elif result.state is ScanState.UNAVAILABLE:
            ratio_or_used = "—"
            size = "—"

        line = Text()
        line.append(pad_right(name, NAME_COLUMN_WIDTH))
        line.append(" ")
        line.append(
            render_bar(result, self.bar_scales.maximum_for(result), graph_width)
        )
        line.append(" ")
        ratio_start = len(line.plain)
        line.append(pad_left(ratio_or_used, RATIO_COLUMN_WIDTH))
        line.append(" ")
        size_start = len(line.plain)
        line.append(pad_left(size, SIZE_COLUMN_WIDTH))
        line.append(" ")
        line.append(pad_right(flags, FLAGS_COLUMN_WIDTH))
        if result.state is ScanState.ERROR:
            line.stylize("red", ratio_start, len(line.plain))
        elif result.state in {ScanState.RUNNING, ScanState.UNAVAILABLE}:
            line.stylize("dim", ratio_start, len(line.plain))
        elif result.warning:
            line.stylize("yellow", ratio_start, size_start)
        return line

    @staticmethod
    def _flags_text(result: ScanResult) -> str:
        """Show observed compression and sparse data, or unknown compression."""

        flags: list[str] = []
        if result.compression_status is CompressionStatus.PRESENT:
            flags.append("C")
        elif result.compression_status is CompressionStatus.UNKNOWN:
            flags.append("?")
        if result.is_ntfs and (result.ntfs_sparse_files or 0) > 0:
            flags.append("S")
        return "".join(flags)


class DirectoryListView(ListView):
    """List view that reports viewport changes to the scan scheduler."""

    class VisibilityChanged(Message):
        """Indicate that a different set of rows is visible."""

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        """Keep the base scroll behavior and notify the application."""

        super().watch_scroll_y(old_value, new_value)
        if round(old_value) != round(new_value):
            self.post_message(self.VisibilityChanged())

    def on_resize(self, _event: events.Resize) -> None:
        """Notify the scheduler when resizing changes the visible rows."""

        self.post_message(self.VisibilityChanged())


class HelpScreen(ModalScreen[None]):
    """Show keyboard and data-semantics help over the browser."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Close", show=False),
        Binding("?", "close", "Close", show=False),
    ]

    CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-dialog {
        width: 76;
        max-width: 92%;
        height: auto;
        max-height: 85%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #help-text {
        width: 1fr;
        height: auto;
    }
    """

    def compose(self) -> ComposeResult:
        """Compose the help dialog."""

        text = (
            "Compsizer controls\n\n"
            "Up/Down, j/k   Move selection\n"
            "Home/End        Select first/last row on page\n"
            "PageUp/Down     Change directory page\n"
            "Enter, l        Open selected directory\n"
            "Backspace, h    Open parent directory\n"
            "Tab             Change pane\n"
            "s               Cycle size / ratio / savings / name sorting\n"
            "c               Toggle result cache\n"
            "r               Refresh current directory and rescan\n"
            "i               Show selected row details\n"
            "?               Show this help\n"
            "q               Quit\n\n"
            "Bars show allocated usage (█) and the difference to the size "
            "baseline (░). On Btrfs, the size column shows uncompressed "
            "extent bytes. On NTFS, it shows logical size; Stored/Logical "
            "compares allocated bytes with logical bytes. Flags use C for "
            "found compressed data, S for found NTFS sparse files, and ? when "
            "compression status is unknown. If neither C nor ? appears, a "
            "complete scan found no compressed data. NTFS counts appear in the "
            "selected-row status and tooltip. Sparse allocation can affect "
            "Stored/Logical, so that value is not a compression-only ratio. "
            "A filesystem label marks a different or unsupported filesystem; "
            "[link] marks an unmeasured directory reparse point.\n\n"
            "When elevated Btrfs scans are unavailable, du provides "
            "apparent-size and allocated-space estimates. A ~ marks estimated "
            "values in both numeric columns. Bars use separate scales for each "
            "source. These estimates are not Btrfs extent statistics. "
            "Independent Btrfs directory scans are not additive because "
            "reflinks and shared extents may overlap.\n\n"
            "Esc             Close help"
        )
        yield Container(Static(Text(text), id="help-text"), id="help-dialog")

    def action_close(self) -> None:
        """Close the help dialog."""

        self.dismiss(None)


class ScanDetailsScreen(ModalScreen[None]):
    """Show the selected directory's scan result and full diagnostics."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Close", show=False),
        Binding("i", "close", "Close", show=False),
    ]

    CSS = """
    ScanDetailsScreen {
        align: center middle;
    }
    #scan-details-dialog {
        width: 84;
        max-width: 92%;
        height: auto;
        max-height: 85%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #scan-details-text {
        width: 1fr;
        height: auto;
    }
    """

    def __init__(self, record: DirectoryRecord) -> None:
        super().__init__()
        self.record: DirectoryRecord = record

    @staticmethod
    def _size_with_bytes(value: int) -> str:
        """Show a readable size and its exact, copyable byte count."""

        return f"{format_bytes(value)} ({value} bytes)"

    def compose(self) -> ComposeResult:
        """Compose filesystem, compression, size, and diagnostic details."""

        result = self.record.result
        details = [
            f"Scan details: {self.record.entry.name}",
            f"Path: {self.record.entry.path}",
            f"Status: {result.state.value}",
        ]
        if result.is_reparse_point:
            details.append("Filesystem: not resolved (directory reparse point)")
            details.append("Scan method: skipped")
        else:
            details.append(f"Filesystem: {result.filesystem_type or 'unknown'}")
            if result.scan_method is not None:
                method = result.scan_method.value
                if result.is_estimate:
                    method = f"{method} estimate"
                details.append(f"Scan method: {method}")
            elif result.state is ScanState.PENDING:
                details.append("Scan method: not started")
            elif result.state is ScanState.RUNNING:
                details.append("Scan method: running")
            elif result.state is ScanState.UNAVAILABLE:
                details.append("Scan method: unavailable")
            else:
                details.append("Scan method: unknown")
        details.append(f"Compression: {result.compression_status.value}")
        if result.files_scanned is not None:
            file_count_label = (
                "Unique files measured" if result.is_ntfs else "Files processed"
            )
            details.append(f"{file_count_label}: {result.files_scanned}")
        if result.disk_usage_bytes is not None:
            if result.is_ntfs:
                disk_label = "Allocated size"
            elif result.is_estimate:
                disk_label = "Allocated estimate"
            else:
                disk_label = "Disk usage"
            details.append(
                f"{disk_label}: {self._size_with_bytes(result.disk_usage_bytes)}"
            )
        if result.uncompressed_bytes is not None:
            if result.is_ntfs:
                size_label = "Logical size"
            elif result.is_estimate:
                size_label = "Apparent size estimate"
            else:
                size_label = "Uncompressed size"
            details.append(
                f"{size_label}: {self._size_with_bytes(result.uncompressed_bytes)}"
            )
        if result.ratio is not None:
            ratio_label = "Stored/logical ratio" if result.is_ntfs else "Ratio"
            details.append(f"{ratio_label}: {format_ratio(result.ratio)}")
        nonempty_type_stats = tuple(
            stats
            for stats in result.compression_type_stats
            if stats.disk_usage_bytes or stats.uncompressed_bytes
        )
        if nonempty_type_stats:
            details.append("Btrfs size by compression type:")
            for stats in nonempty_type_stats:
                details.append(
                    f"  {stats.type_name}: "
                    f"Disk usage {self._size_with_bytes(stats.disk_usage_bytes)}; "
                    "uncompressed "
                    f"{self._size_with_bytes(stats.uncompressed_bytes)}"
                )
        if result.ntfs_summary:
            details.append(result.ntfs_summary)
        if result.error:
            details.extend(("", "Error:", result.error))
        if result.warning:
            details.extend(("", "Warning:", result.warning))
        if not result.error and not result.warning:
            details.extend(("", "No warning or error was reported."))
        details.extend(("", "Esc or i: close"))
        yield Container(
            Static(Text("\n".join(details)), id="scan-details-text"),
            id="scan-details-dialog",
        )

    def action_close(self) -> None:
        """Close the scan details dialog."""

        self.dismiss(None)


class ElevatedScanPrompt(ModalScreen[bool]):
    """Ask whether to authorize elevated ``compsize`` scans."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("y", "authorize", "Authorize", show=False),
        Binding("enter", "authorize", "Authorize", show=False, priority=True),
        Binding("n", "decline", "Continue without", show=False),
        Binding("escape", "decline", "Continue without", show=False),
    ]

    CSS = """
    ElevatedScanPrompt {
        align: center middle;
    }
    #elevated-dialog {
        width: 72;
        max-width: 92%;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #elevated-text {
        width: 1fr;
        height: auto;
    }
    """

    def compose(self) -> ComposeResult:
        """Compose the one-time elevated-scan consent message."""

        text = (
            "compsize needs permission to read Btrfs extent data.\n\n"
            "Allow Compsizer to run compsize as root? Sudo may ask for your "
            "password, and your account must be allowed to run this command. "
            "Compsizer does not read or store the password.\n\n"
            "Y or Enter: authorize    N or Esc: continue without statistics"
        )
        yield Container(Static(text, id="elevated-text"), id="elevated-dialog")

    def action_authorize(self) -> None:
        """Confirm elevated scans."""

        self.dismiss(True)

    def action_decline(self) -> None:
        """Continue without elevated scans."""

        self.dismiss(False)


APP_CSS = """
Screen {
    layout: vertical;
}
#main {
    height: 1fr;
    layout: horizontal;
}
#tree-pane {
    width: 30%;
    min-width: 24;
    border: solid $panel;
}
#content-pane {
    width: 70%;
    min-width: 42;
    border: solid $panel;
}
.pane-title {
    height: 1;
    padding: 0 1;
    text-style: bold;
}
#tree-host {
    height: 1fr;
}
#directory-tree {
    height: 1fr;
}
#path-label {
    height: 1;
    padding: 0 1;
    text-style: bold;
    overflow-x: hidden;
    text-overflow: ellipsis;
}
#column-label {
    height: 1;
    padding: 0;
    color: $text-muted;
}
#directory-list {
    height: 1fr;
    scrollbar-size: 1 1;
}
.directory-row {
    width: 1fr;
    height: 1;
}
#empty-label {
    height: auto;
    padding: 0 1;
    color: $text-muted;
}
#status {
    height: 2;
    padding: 0 1;
    color: $text-muted;
}
ListItem {
    height: 1;
    padding: 0;
}
ListItem.-highlight {
    background: $boost;
}
Footer {
    height: 1;
}
"""


class CompsizerApp(App[None]):
    """Textual application for immediate directory browsing."""

    TITLE = "compsizer"
    CSS = APP_CSS
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit_app", "Quit"),
        Binding("pageup", "previous_page", "Previous page", priority=True),
        Binding("pagedown", "next_page", "Next page", priority=True),
        Binding("up", "move_up", "Move up"),
        Binding("k", "move_up", "Move up", show=False),
        Binding("down", "move_down", "Move down"),
        Binding("j", "move_down", "Move down", show=False),
        Binding("home", "move_home", "First row", key_display="Home", priority=True),
        Binding("end", "move_end", "Last row", key_display="End", priority=True),
        Binding("enter", "open_selected", "Open", key_display="Enter"),
        Binding("l", "open_selected", "Open", show=False),
        Binding("backspace", "go_parent", "Parent", key_display="Backspace"),
        Binding("h", "go_parent", "Parent", show=False),
        Binding("tab", "focus_next", "Focus", key_display="Tab"),
        Binding("s", "toggle_sort", "Sort"),
        Binding("c", "toggle_cache", "Cache"),
        Binding("r", "refresh_view", "Refresh"),
        Binding("i", "show_details", "Details"),
        Binding("?", "show_help", "Help"),
    ]

    def __init__(self, initial_path: Path, runner: ScanRunner | None = None) -> None:
        super().__init__()
        self.cache = ResultCache()
        self.model = BrowserModel(initial_path, self.cache)
        self.runner = (
            runner
            if runner is not None
            else (
                WindowsScanRunner()
                if os.name == "nt"
                else CompsizeRunner(
                    authorization_callback=self._authorize_elevated_scans
                )
            )
        )
        self.manager = ScanManager(self.runner, self._on_scan_update)
        if isinstance(self.runner, WindowsScanRunner):
            self._supported_filesystem_type: str | None = "NTFS"
        elif isinstance(self.runner, CompsizeRunner):
            self._supported_filesystem_type = "btrfs"
        else:
            self._supported_filesystem_type = None
        self._current_filesystem_type: str | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._view_load_task: asyncio.Task[Any] | None = None
        self._tree_reveal_task: asyncio.Task[Any] | None = None
        self._tree_loaded: set[str] = set()
        self._tree_loading: dict[str, int] = {}
        self._tree_generations: dict[str, int] = {}
        self._row_items: dict[str, ListItem] = {}
        self._row_widgets: dict[str, DirectoryRow] = {}
        self._row_render_lock = asyncio.Lock()
        self._row_refresh_task: asyncio.Task[Any] | None = None
        self._row_refresh_pending = False
        self._page_index = 0
        self._bar_scales = BarScales(0, 0)
        self._scan_priority_task: asyncio.Task[Any] | None = None
        self._scan_priority_pending = False
        self._startup_notices: list[str] = []
        self._ui_ready = False
        self._shutting_down = False

    def compose(self) -> ComposeResult:
        """Compose the two-pane browser layout."""

        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="tree-pane"):
                yield Label("Directories", classes="pane-title")
                with Container(id="tree-host"):
                    yield Tree(
                        self._tree_label(self.model.current_path),
                        data=self.model.current_path,
                        id="directory-tree",
                    )
            with Vertical(id="content-pane"):
                yield Label(str(self.model.current_path), id="path-label")
                if isinstance(self.runner, WindowsScanRunner):
                    ratio_label = "Stored/Logical"
                    size_label = "Logical Size"
                else:
                    ratio_label = "Ratio/Used"
                    size_label = "Size"
                yield DirectoryColumnHeader(ratio_label, size_label)
                yield DirectoryListView(id="directory-list")
                yield Static("", id="empty-label")
                yield Static("", id="status")
        yield Footer()

    async def on_mount(self) -> None:
        """Start background services and load the initial directory."""

        self._ui_ready = True
        self._track(self._initialize())

    async def on_unmount(self) -> None:
        """Stop directory loading and scan workers before exit."""

        self._shutting_down = True
        self._ui_ready = False
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.manager.close()

    async def _initialize(self) -> None:
        """Start the scan manager and schedule initial filesystem work."""

        initial_path = self.model.current_path
        await self.manager.start()
        if self._shutting_down:
            return
        self._navigate_to(initial_path)
        if self._shutting_down:
            return
        tree = self.query_one("#directory-tree", Tree)
        tree.root.expand()
        self._track(self._load_tree_children(tree.root))
        self.set_focus(self.query_one("#directory-list", ListView))
        self._track(self._check_startup_environment(initial_path))

    async def _check_startup_environment(self, initial_path: Path) -> None:
        """Check optional scanner access without delaying initial navigation."""

        if os.name != "nt":
            mountinfo = await asyncio.to_thread(FilesystemDetector.read_mountinfo)
            if self._shutting_down:
                return
            if mountinfo is not None:
                filesystem_type = FilesystemDetector.filesystem_type(
                    initial_path,
                    mountinfo,
                )
                if (
                    filesystem_type is not None
                    and filesystem_type.casefold() != "btrfs"
                ):
                    self._startup_notices.append(
                        f"Initial path is on {filesystem_type}; Btrfs child mounts can "
                        "still be browsed."
                    )
        if isinstance(self.runner, CompsizeRunner):
            executable = await asyncio.to_thread(shutil.which, self.runner.executable)
            if executable is None:
                self._startup_notices.append(
                    "compsize was not found in PATH; compression statistics are unavailable."
                )
        self._update_status()

    def _track(self, coroutine: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Track an application task for clean shutdown."""

        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._task_finished)
        return task

    def _task_finished(self, task: asyncio.Task[Any]) -> None:
        """Remove a task and report unexpected background failures."""

        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            LOGGER.error(
                "Background application task failed", exc_info=task.exception()
            )

    def _navigate_to(self, path: Path, *, refresh: bool = False) -> None:
        """Change path and schedule loading without waiting for scans."""

        if self._shutting_down or not self._ui_ready:
            return
        if self._view_load_task is not None and not self._view_load_task.done():
            self._view_load_task.cancel()
        view_id = self.model.begin_view(path, refresh=refresh)
        self._page_index = 0
        self._bar_scales = BarScales(0, 0)
        self._current_filesystem_type = None
        self._update_path_label()
        self._update_status("Enumerating directories…")
        self._view_load_task = self._track(self._load_view(view_id, path))
        if self._tree_reveal_task is not None and not self._tree_reveal_task.done():
            self._tree_reveal_task.cancel()
        self._tree_reveal_task = self._track(
            self._reveal_tree_path(path, refresh=refresh)
        )
        self._schedule_row_render()

    async def _load_view(self, view_id: int, path: Path) -> None:
        """Enumerate one view and submit its missing scans."""

        try:
            await self.manager.set_view(view_id, ())
            listing = await asyncio.to_thread(enumerate_directories, path)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # pragma: no cover - worker boundary safeguard
            LOGGER.exception("Directory enumeration failed for %s", path)
            listing = DirectoryListing(
                path=path, error=f"Directory enumeration failed: {exc}"
            )
        if self._shutting_down or view_id != self.model.view_id:
            return
        self.model.set_listing(view_id, listing)
        self._bar_scales = BarScales.from_records(self.model.records.values())
        await self._render_rows()
        if self._shutting_down or not self._ui_ready:
            return
        self._update_status()
        requests = self.model.requests_for_missing_results(self._visible_paths())
        await self.manager.set_view(view_id, requests)
        if self._shutting_down or not self._ui_ready:
            return
        try:
            filesystem_type = await asyncio.to_thread(
                self._filesystem_type_for_path,
                path,
            )
        except asyncio.CancelledError:
            return
        if self._shutting_down or view_id != self.model.view_id:
            return
        if filesystem_type != self._current_filesystem_type:
            self._current_filesystem_type = filesystem_type
            self._schedule_row_render()

    def _filesystem_type_for_path(self, path: Path) -> str | None:
        """Read the filesystem type for the active platform and scanner."""

        if isinstance(self.runner, WindowsScanRunner):
            try:
                return self.runner.file_api.filesystem_type(path)
            except OSError as exc:
                LOGGER.debug("Could not identify the filesystem for %s: %s", path, exc)
                return None
        if isinstance(self.runner, CompsizeRunner):
            return FilesystemDetector.filesystem_type_for_path(path)
        return None

    async def _on_scan_update(self, job: ScanJob, result: ScanResult) -> None:
        """Apply a scan update and refresh only the affected current view."""

        if self._shutting_down or not self._ui_ready:
            return
        changed = self.model.apply_scan_update(job, result)
        if changed:
            self._bar_scales = self._bar_scales.include(result)
            self._schedule_row_render()
        self._update_status()

    async def _authorize_elevated_scans(self, command: Sequence[str]) -> bool:
        """Confirm root scans and let sudo prompt while the TUI is suspended."""

        if self._shutting_down or not self._ui_ready:
            return False

        result: asyncio.Future[bool | None] = asyncio.get_running_loop().create_future()

        def set_result(value: bool | None) -> None:
            """Receive the consent screen result."""

            if not result.done():
                result.set_result(value)

        prompt = ElevatedScanPrompt()
        self.push_screen(prompt, callback=set_result)
        try:
            approved = await result
        except asyncio.CancelledError:
            if self.screen is prompt:
                self.pop_screen()
            raise
        if not approved:
            return False

        try:
            with self.suspend():
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=None,
                    stdout=subprocess.DEVNULL,
                )
                try:
                    await process.wait()
                except asyncio.CancelledError:
                    if process.returncode is None:
                        process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        if process.returncode is None:
                            process.kill()
                        await process.wait()
                    raise
        except (OSError, SuspendNotSupported) as exc:
            LOGGER.warning("Unable to authorize elevated compsize scans: %s", exc)
            return False
        return process.returncode == 0

    async def _render_rows(self) -> None:
        """Update row widgets in place and move only rows that changed order."""

        if self._shutting_down or not self._ui_ready:
            return
        async with self._row_render_lock:
            try:
                list_view = self.query_one("#directory-list", ListView)
            except NoMatches:
                return
            while not list_view.is_attached:
                if self._shutting_down or not self._ui_ready:
                    return
                await asyncio.sleep(0)
            if self._shutting_down or not self._ui_ready:
                return
            all_records = self.model.sorted_records()
            if self.model.selected_path is not None:
                selected_key = path_key(self.model.selected_path)
                selected_global_index = next(
                    (
                        index
                        for index, record in enumerate(all_records)
                        if path_key(record.entry.path) == selected_key
                    ),
                    None,
                )
                if selected_global_index is not None:
                    self._page_index = selected_global_index // DIRECTORY_PAGE_SIZE
            page_count = max(
                1,
                (len(all_records) + DIRECTORY_PAGE_SIZE - 1) // DIRECTORY_PAGE_SIZE,
            )
            self._page_index = max(0, min(page_count - 1, self._page_index))
            page_start = self._page_index * DIRECTORY_PAGE_SIZE
            records = all_records[page_start : page_start + DIRECTORY_PAGE_SIZE]
            records_by_key = {path_key(record.entry.path): record for record in records}
            bar_scales = self._bar_scales

            stale_keys = set(self._row_items) - set(records_by_key)
            for key in stale_keys:
                item = self._row_items.pop(key)
                self._row_widgets.pop(key, None)
                if item.is_attached:
                    await item.remove()

            new_items: list[ListItem] = []
            for key, record in records_by_key.items():
                row = self._row_widgets.get(key)
                filesystem_badge = self._filesystem_badge(record)
                if row is None:
                    row = DirectoryRow(record, bar_scales, filesystem_badge)
                    self._row_widgets[key] = row
                    item = ListItem(row)
                    self._row_items[key] = item
                    new_items.append(item)
                else:
                    row.update_record(record, bar_scales, filesystem_badge)

            if new_items:
                try:
                    await list_view.mount(*new_items)
                except MountError:
                    if self._shutting_down or not list_view.is_attached:
                        return
                    raise
            if self._shutting_down or not self._ui_ready:
                return

            desired_items = [
                self._row_items[path_key(record.entry.path)] for record in records
            ]
            for _attempt in range(3):
                current_items = [
                    child for child in list_view.children if isinstance(child, ListItem)
                ]
                if len(current_items) == len(desired_items) and all(
                    item in current_items for item in desired_items
                ):
                    break
                await asyncio.sleep(0)
            else:
                self._schedule_row_render()
                return
            for index, desired_item in enumerate(desired_items):
                if current_items[index] is desired_item:
                    continue
                current_index = current_items.index(desired_item)
                list_view.move_child(desired_item, before=current_items[index])
                current_items.insert(index, current_items.pop(current_index))

            selected_index: int | None = None
            if self.model.selected_path is not None:
                selected_key = path_key(self.model.selected_path)
                if selected_key in self._row_items:
                    selected_index = next(
                        (
                            index
                            for index, record in enumerate(records)
                            if path_key(record.entry.path) == selected_key
                        ),
                        None,
                    )
            if list_view.index != selected_index:
                list_view.index = selected_index
            for index, item in enumerate(current_items):
                item.highlighted = index == selected_index
            empty_label = self.query_one("#empty-label", Static)
            if records:
                empty_label.update("")
            elif self.model.listing_error:
                empty_label.update("No directories available.")
            else:
                empty_label.update("No child directories.")
            self._update_status()

    def _filesystem_badge(self, record: DirectoryRecord) -> str | None:
        """Return a compact label for a link or a different filesystem."""

        result = record.result
        if result.is_reparse_point:
            return "link"
        filesystem_type = result.filesystem_type
        if filesystem_type is None:
            return None
        filesystem_key = filesystem_type.casefold()
        differs_from_current = (
            self._current_filesystem_type is not None
            and filesystem_key != self._current_filesystem_type.casefold()
        )
        is_unsupported = (
            self._supported_filesystem_type is not None
            and filesystem_key != self._supported_filesystem_type.casefold()
        )
        return filesystem_type if differs_from_current or is_unsupported else None

    def _schedule_row_render(self) -> None:
        """Coalesce rapid scan updates into one short UI refresh window."""

        if self._shutting_down or not self._ui_ready:
            return
        self._row_refresh_pending = True
        if self._row_refresh_task is None or self._row_refresh_task.done():
            self._row_refresh_task = self._track(self._flush_row_renders())

    async def _flush_row_renders(self) -> None:
        """Apply pending row updates after a small batching delay."""

        while self._row_refresh_pending:
            self._row_refresh_pending = False
            await asyncio.sleep(ROW_REFRESH_DELAY)
            await self._render_rows()

    def _visible_paths(self) -> set[Path]:
        """Return paths of rows currently visible in the right pane."""

        if self._shutting_down or not self._ui_ready:
            return set()
        try:
            list_view = self.query_one("#directory-list", DirectoryListView)
        except NoMatches:
            return set()
        paths: set[Path] = set()
        for item in list_view.displayed_and_visible_children:
            if not isinstance(item, ListItem):
                continue
            row = next(iter(item.query(DirectoryRow)), None)
            if row is not None:
                paths.add(row.record.entry.path)
        return paths

    def _schedule_scan_priority_update(self) -> None:
        """Coalesce viewport changes before reprioritizing pending scans."""

        if self._shutting_down or not self._ui_ready:
            return
        self._scan_priority_pending = True
        if self._scan_priority_task is None or self._scan_priority_task.done():
            self._scan_priority_task = self._track(self._flush_scan_priority_updates())

    async def _flush_scan_priority_updates(self) -> None:
        """Update pending priorities without removing any current-view work."""

        while self._scan_priority_pending:
            self._scan_priority_pending = False
            await asyncio.sleep(ROW_REFRESH_DELAY)
            if self._shutting_down or not self._ui_ready:
                return
            requests = self.model.requests_for_missing_results(self._visible_paths())
            await self.manager.set_view(self.model.view_id, requests)

    def _set_list_selection(
        self,
        list_view: ListView,
        index: int | None,
        *,
        ensure_visible: bool,
    ) -> None:
        """Set a list cursor and optionally move the viewport to it."""

        items = [child for child in list_view.children if isinstance(child, ListItem)]
        if not items:
            list_view.index = None
            return
        requested_index = (
            len(items) - 1 if index is not None and index < 0 else index or 0
        )
        target_index = max(0, min(len(items) - 1, requested_index))
        list_view.index = target_index
        for item_index, item in enumerate(items):
            item.highlighted = item_index == target_index
        self._select_highlighted_list_item(list_view)
        if ensure_visible:
            list_view.scroll_to_widget(
                items[target_index],
                animate=False,
                force=True,
                immediate=True,
            )

    def _select_highlighted_list_item(self, list_view: ListView) -> None:
        """Synchronize model selection with the list's current item."""

        item = list_view.highlighted_child
        if item is None:
            return
        row = next(iter(item.query(DirectoryRow)), None)
        if row is None:
            return
        self.model.select(row.record.entry.path)
        self._schedule_scan_priority_update()
        self._update_status()

    def _update_path_label(self) -> None:
        """Update the current path and sort indicators."""

        if self._shutting_down or not self._ui_ready:
            return
        try:
            label = self.query_one("#path-label", Label)
        except NoMatches:
            return
        label.update(
            Text(
                f"{self.model.current_path}  ·  sort: {self.model.sort_mode.value}  ·  "
                f"cache: {'on' if self.cache.enabled else 'off'}",
                overflow="ellipsis",
                no_wrap=True,
            )
        )

    def _update_status(self, transient: str | None = None) -> None:
        """Update the concise status and error area."""

        if self._shutting_down or not self._ui_ready:
            return
        try:
            status = self.query_one("#status", Static)
        except NoMatches:
            return
        if transient:
            status.update(Text(transient))
            return
        counts = self.model.state_counts
        page_count = max(
            1,
            (len(self.model.records) + DIRECTORY_PAGE_SIZE - 1) // DIRECTORY_PAGE_SIZE,
        )
        parts = [
            f"complete {counts[ScanState.COMPLETE]}",
            f"running {counts[ScanState.RUNNING]}",
            f"pending {counts[ScanState.PENDING]}",
            f"errors {counts[ScanState.ERROR]}",
            f"page {self._page_index + 1}/{page_count}",
        ]
        if counts[ScanState.UNAVAILABLE]:
            parts.append(f"unavailable {counts[ScanState.UNAVAILABLE]}")
        parts.extend(self._startup_notices)
        if self.model.listing_error:
            parts.append(self.model.listing_error)
        elif self.model.listing_warning:
            parts.append(self.model.listing_warning)
        if self.model.selected_path is not None:
            selected = self.model.records.get(path_key(self.model.selected_path))
            if selected is not None:
                if selected.result.error:
                    parts.append(selected.result.error)
                if selected.result.warning and (
                    selected.result.is_ntfs or not selected.result.error
                ):
                    parts.append(selected.result.warning)
                if selected.result.ntfs_summary:
                    parts.append(selected.result.ntfs_summary)
        status_text = "  ".join(parts)
        content_width = max(1, status.size.width - 2)
        maximum_cells = content_width * 2
        if cell_len(status_text) > maximum_cells:
            status_text = f"{_take_cells(status_text, maximum_cells - 1)}…"
        status.update(Text(status_text))

    @staticmethod
    def _tree_label(path: Path) -> Text:
        """Return a safe tree label for a filesystem path."""

        return Text(path.name or str(path), no_wrap=True, overflow="ellipsis")

    def _invalidate_tree_subtree(self, path: Path) -> None:
        """Forget loaded tree nodes at or below ``path``."""

        root = Path(path_key(path))
        keys = (
            set(self._tree_loaded)
            | set(self._tree_loading)
            | set(self._tree_generations)
        )
        for key in keys:
            try:
                Path(key).relative_to(root)
            except ValueError:
                continue
            self._tree_loaded.discard(key)
            self._tree_generations[key] = self._tree_generations.get(key, 0) + 1
            self._tree_loading.pop(key, None)

    def _invalidate_all_tree_nodes(self) -> None:
        """Invalidate all loaded or active tree enumerations."""

        keys = (
            set(self._tree_loaded)
            | set(self._tree_loading)
            | set(self._tree_generations)
        )
        for key in keys:
            self._tree_generations[key] = self._tree_generations.get(key, 0) + 1
        self._tree_loaded.clear()
        self._tree_loading.clear()

    async def _load_tree_children(self, node: TreeNode[Any]) -> None:
        """Load one tree node's direct children without recursion."""

        if self._shutting_down or not self._ui_ready:
            return
        path = node.data
        if not isinstance(path, Path):
            return
        key = path_key(path)
        if key in self._tree_loaded or key in self._tree_loading:
            return
        generation = self._tree_generations.get(key, 0)
        self._tree_loading[key] = generation
        try:
            listing = await asyncio.to_thread(enumerate_directories, path)
            if (
                self._shutting_down
                or not self._ui_ready
                or self._tree_generations.get(key, 0) != generation
            ):
                return
            if listing.error is not None:
                LOGGER.warning(
                    "Unable to load tree children for %s: %s", path, listing.error
                )
                return
            node.remove_children()
            entries = listing.entries[:TREE_CHILD_LIMIT]
            for entry in entries:
                node.add(self._tree_label(entry.path), entry.path, allow_expand=True)
            omitted = len(listing.entries) - len(entries)
            if omitted:
                node.add(
                    Text(
                        f"{omitted} more; use directory pages",
                        no_wrap=True,
                        overflow="ellipsis",
                    ),
                    None,
                    allow_expand=False,
                )
            node.allow_expand = bool(listing.entries)
            self._tree_loaded.add(key)
        finally:
            if self._tree_loading.get(key) == generation:
                self._tree_loading.pop(key, None)

    async def _reveal_tree_path(self, path: Path, *, refresh: bool = False) -> None:
        """Reveal a path and optionally reload its tree children."""

        if self._shutting_down or not self._ui_ready:
            return
        try:
            tree = self.query_one("#directory-tree", Tree)
        except NoMatches:
            return
        root = tree.root
        root_path = root.data
        if not isinstance(root_path, Path):
            return
        try:
            relative_parts = path.relative_to(root_path).parts
        except ValueError:
            root.set_label(self._tree_label(path))
            root.data = path
            self._invalidate_all_tree_nodes()
            root.remove_children()
            root.allow_expand = True
            tree.move_cursor(root)
            await self._load_tree_children(root)
            return

        node = root
        for part in relative_parts:
            await self._load_tree_children(node)
            if self._shutting_down or not self._ui_ready:
                return
            node_path = node.data
            if not isinstance(node_path, Path):
                return
            child = next(
                (
                    candidate
                    for candidate in node.children
                    if candidate.data == absolute_child_path(node_path, part)
                ),
                None,
            )
            if child is None:
                if refresh:
                    parent_path = node.data
                    if isinstance(parent_path, Path):
                        self._invalidate_tree_subtree(parent_path)
                        node.allow_expand = True
                        await self._load_tree_children(node)
                return
            node.expand()
            node = child
        if refresh:
            self._invalidate_tree_subtree(path)
            node.allow_expand = True
            await self._load_tree_children(node)
        tree.move_cursor(node)

    def _focused_tree_path(self) -> Path | None:
        """Return the highlighted tree path when the tree has focus."""

        focused = self.focused
        if isinstance(focused, Tree):
            node = focused.cursor_node
            if node is not None and isinstance(node.data, Path):
                return node.data
        return None

    def action_quit_app(self) -> None:
        """Exit the application."""

        self.exit()

    def action_previous_page(self) -> None:
        """Show the previous page of directory rows."""

        self._change_page(-1)

    def action_next_page(self) -> None:
        """Show the next page of directory rows."""

        self._change_page(1)

    def _change_page(self, delta: int) -> None:
        """Change pages and select the first row on the new page."""

        if self._shutting_down or not self._ui_ready:
            return
        records = self.model.sorted_records()
        page_count = max(
            1,
            (len(records) + DIRECTORY_PAGE_SIZE - 1) // DIRECTORY_PAGE_SIZE,
        )
        target_page = max(0, min(page_count - 1, self._page_index + delta))
        if target_page == self._page_index:
            return
        self._page_index = target_page
        page_start = target_page * DIRECTORY_PAGE_SIZE
        self.model.select(records[page_start].entry.path)
        self._schedule_row_render()
        self._schedule_scan_priority_update()
        self._update_status()

    def action_move_up(self) -> None:
        """Move the focused pane upward."""

        focused = self.focused
        if isinstance(focused, ListView):
            focused.action_cursor_up()
            self._select_highlighted_list_item(focused)
        elif isinstance(focused, Tree):
            focused.action_cursor_up()

    def action_move_down(self) -> None:
        """Move the focused pane downward."""

        focused = self.focused
        if isinstance(focused, ListView):
            focused.action_cursor_down()
            self._select_highlighted_list_item(focused)
        elif isinstance(focused, Tree):
            focused.action_cursor_down()

    def action_move_home(self) -> None:
        """Move the focused pane to its first item and reveal it."""

        focused = self.focused
        if isinstance(focused, ListView):
            self._set_list_selection(focused, 0, ensure_visible=True)
        elif isinstance(focused, Tree) and focused.last_line >= 0:
            focused.move_cursor_to_line(0, animate=False)

    def action_move_end(self) -> None:
        """Move the focused pane to its last item and reveal it."""

        focused = self.focused
        if isinstance(focused, ListView):
            self._set_list_selection(focused, -1, ensure_visible=True)
        elif isinstance(focused, Tree) and focused.last_line >= 0:
            focused.move_cursor_to_line(focused.last_line, animate=False)

    def action_open_selected(self) -> None:
        """Open the selected tree or directory-list path."""

        path = self._focused_tree_path() or self.model.selected_path
        if path is not None:
            self._navigate_to(path)

    def action_go_parent(self) -> None:
        """Open the parent directory unless already at the filesystem root."""

        parent = self.model.current_path.parent
        if parent != self.model.current_path:
            self._navigate_to(parent)

    def action_toggle_sort(self) -> None:
        """Cycle through the available row sorting modes."""

        self.model.sort_mode = self.model.sort_mode.toggled()
        self._update_path_label()
        self._schedule_row_render()
        self._track(
            self.manager.set_view(
                self.model.view_id,
                self.model.requests_for_missing_results(self._visible_paths()),
            )
        )

    def action_toggle_cache(self) -> None:
        """Toggle cache use and restore retained values when enabled."""

        self.cache.set_enabled(not self.cache.enabled)
        self._update_path_label()
        if self.cache.enabled:
            for key, record in tuple(self.model.records.items()):
                cached = self.cache.get(record.entry.path)
                if cached is not None:
                    self.model.records[key] = DirectoryRecord(
                        record.entry, cached, record.ordinal
                    )
            requests = self.model.requests_for_missing_results(self._visible_paths())
            self._track(self.manager.set_view(self.model.view_id, requests))
        self._schedule_row_render()

    def action_refresh_view(self) -> None:
        """Re-enumerate and rescan the current directory."""

        if isinstance(self.runner, CompsizeRunner):
            self.runner.reset_privilege_decision()
        self._navigate_to(self.model.current_path, refresh=True)

    def action_show_help(self) -> None:
        """Open the keyboard and semantics help screen."""

        self.push_screen(HelpScreen())

    def action_show_details(self) -> None:
        """Show full details for the selected directory scan."""

        selected_path = self.model.selected_path
        record = (
            self.model.records.get(path_key(selected_path))
            if selected_path is not None
            else None
        )
        if record is None:
            self._update_status("Select a directory row to show its details.")
            return
        self.push_screen(ScanDetailsScreen(record))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Keep model selection attached to the highlighted path."""

        if event.item is None or event.item is not event.list_view.highlighted_child:
            return
        self._select_highlighted_list_item(event.list_view)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter the directory selected in the right pane."""

        row = next(iter(event.item.query(DirectoryRow)), None)
        if row is not None:
            self._navigate_to(row.record.entry.path)

    def on_directory_list_view_visibility_changed(
        self,
        _event: DirectoryListView.VisibilityChanged,
    ) -> None:
        """Prioritize rows after the right-pane viewport changes."""

        self._schedule_scan_priority_update()

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        """Load children after a tree node is expanded."""

        self._track(self._load_tree_children(event.node))

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Enter the directory selected in the left tree."""

        if isinstance(event.node.data, Path) and path_key(event.node.data) != path_key(
            self.model.current_path
        ):
            self._navigate_to(event.node.data)


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""

    parser = argparse.ArgumentParser(
        prog="compsizer",
        description="Browse directories and filesystem compression statistics.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Initial directory to browse (default: current directory).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, validate the path, and run the TUI."""

    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    try:
        initial_path = normalize_initial_path(arguments.path)
    except InvalidInitialPathError as exc:
        parser.error(str(exc))
    app = CompsizerApp(initial_path)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
