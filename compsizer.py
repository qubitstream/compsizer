#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "textual>=8.2.8",
# ]
# ///

"""Browse Btrfs compression statistics in a terminal user interface."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any, ClassVar, Protocol

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static, Tree
from textual.widgets.tree import TreeNode

LOGGER = logging.getLogger("compsizer")

DEFAULT_CONCURRENCY = 2
MAX_DIAGNOSTIC_LENGTH = 500
NAME_COLUMN_WIDTH = 26
RATIO_COLUMN_WIDTH = 9
SIZE_COLUMN_WIDTH = 13


class InvalidInitialPathError(ValueError):
    """Report an invalid command-line directory path."""


class ScanState(Enum):
    """States reported for a directory compression scan."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    ERROR = "error"


class SortMode(Enum):
    """Available directory row sort criteria."""

    SIZE = "size"
    RATIO = "ratio"

    def toggled(self) -> SortMode:
        """Return the other user-facing sort criterion."""

        if self is SortMode.SIZE:
            return SortMode.RATIO
        return SortMode.SIZE


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
class ParsedCompsizeReport:
    """Represent byte values extracted from a successful report."""

    disk_usage_bytes: int
    uncompressed_bytes: int
    referenced_bytes: int
    compression_types: tuple[str, ...] = ()
    warning: str | None = None
    empty: bool = False


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Represent the current compression scan state for one path."""

    path: Path
    state: ScanState
    disk_usage_bytes: int | None = None
    uncompressed_bytes: int | None = None
    referenced_bytes: int | None = None
    compression_types: tuple[str, ...] = ()
    warning: str | None = None
    error: str | None = None
    exit_code: int | None = None

    @property
    def ratio(self) -> float | None:
        """Return disk usage divided by uncompressed size."""

        if self.disk_usage_bytes is None or self.uncompressed_bytes in (None, 0):
            return None
        return self.disk_usage_bytes / self.uncompressed_bytes

    @property
    def ratio_fraction(self) -> Fraction | None:
        """Return the exact ratio used for sorting, when available."""

        if self.disk_usage_bytes is None or self.uncompressed_bytes in (None, 0):
            return None
        return Fraction(self.disk_usage_bytes, self.uncompressed_bytes)

    @property
    def has_statistics(self) -> bool:
        """Return whether byte statistics are present on the result."""

        return (
            self.disk_usage_bytes is not None
            and self.uncompressed_bytes is not None
            and self.referenced_bytes is not None
        )

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
    ) -> ScanResult:
        """Create an error result without hiding the failing path."""

        return cls(
            path=path,
            state=ScanState.ERROR,
            error=message,
            exit_code=exit_code,
        )


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


def enumerate_directories(path: Path) -> DirectoryListing:
    """Enumerate direct child directories without following symlinks.

    The function does not recurse. It is suitable for running in a worker
    thread so a slow filesystem cannot block the Textual event loop.
    """

    entries: list[DirectoryEntry] = []
    warnings: list[str] = []
    try:
        with os.scandir(path) as directory:
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


def _is_empty_compsize_report(stdout: str, stderr: str) -> bool:
    """Recognize compsize's normal empty-input response."""

    stdout_message = stdout.strip().casefold()
    if stdout_message in {"no files", "no files."}:
        return not stderr.strip()
    if stdout_message:
        return False
    messages = [line.strip().casefold() for line in stderr.splitlines() if line.strip()]
    return bool(messages) and all(
        message in {"no files", "no files."} for message in messages
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
        )

    total: tuple[int, int, int] | None = None
    compression_types: list[str] = []
    for line in stdout.splitlines():
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0].upper() == "TOTAL":
            total = _parse_byte_columns(tokens, line)
            compression_types = []
            continue
        if len(tokens) < 5 or not tokens[1].endswith("%"):
            continue
        try:
            _parse_byte_columns(tokens, line)
        except CompsizeParseError:
            continue
        type_name = tokens[0]
        if type_name not in compression_types:
            compression_types.append(type_name)

    if total is None:
        processed_zero = any(
            line.strip().casefold() in {"processed 0 files.", "processed 0 files"}
            for line in stdout.splitlines()
        )
        if processed_zero and not stderr.strip():
            return ParsedCompsizeReport(0, 0, 0, empty=True)
        detail = diagnostic_text(stderr) or "No TOTAL row was found."
        raise CompsizeParseError(f"Could not parse compsize output: {detail}")

    warning = diagnostic_text(stderr) or None
    return ParsedCompsizeReport(
        disk_usage_bytes=total[0],
        uncompressed_bytes=total[1],
        referenced_bytes=total[2],
        compression_types=tuple(compression_types),
        warning=warning,
    )


class ScanRunner(Protocol):
    """Protocol implemented by asynchronous compression scanners."""

    async def scan(self, path: Path) -> ScanResult:
        """Scan one directory."""

    async def close(self) -> None:
        """Stop active scanner resources."""


class CompsizeRunner:
    """Run ``compsize`` with argument-safe asynchronous subprocesses."""

    def __init__(self, executable: str = "compsize") -> None:
        self.executable = executable
        self._processes: set[asyncio.subprocess.Process] = set()

    async def scan(self, path: Path) -> ScanResult:
        """Run a complete scan for ``path`` and convert failures to results."""

        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                "-b",
                "-x",
                "--",
                os.fspath(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return ScanResult.error_result(
                path,
                f"Unable to run {self.executable!r}: executable not found in PATH.",
            )
        except OSError as exc:
            return ScanResult.error_result(path, f"Unable to start compsize: {exc}")

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
        try:
            report = parse_compsize_output(stdout, stderr)
        except CompsizeParseError as exc:
            detail = diagnostic_text(stderr)
            message = str(exc)
            if detail and detail not in message:
                message = f"{message} ({detail})"
            return ScanResult.error_result(path, message, exit_code=exit_code)

        if report.empty:
            return ScanResult(
                path=path,
                state=ScanState.COMPLETE,
                disk_usage_bytes=0,
                uncompressed_bytes=0,
                referenced_bytes=0,
                warning=report.warning,
                exit_code=exit_code,
            )
        if exit_code not in (None, 0):
            error = (
                diagnostic_text(stderr) or f"compsize exited with status {exit_code}."
            )
            return ScanResult(
                path=path,
                state=ScanState.ERROR,
                disk_usage_bytes=report.disk_usage_bytes,
                uncompressed_bytes=report.uncompressed_bytes,
                referenced_bytes=report.referenced_bytes,
                compression_types=report.compression_types,
                warning=report.warning,
                error=error,
                exit_code=exit_code,
            )
        return ScanResult(
            path=path,
            state=ScanState.COMPLETE,
            disk_usage_bytes=report.disk_usage_bytes,
            uncompressed_bytes=report.uncompressed_bytes,
            referenced_bytes=report.referenced_bytes,
            compression_types=report.compression_types,
            warning=report.warning,
            exit_code=exit_code,
        )

    async def close(self) -> None:
        """Terminate all subprocesses owned by this runner."""

        processes = tuple(self._processes)
        if processes:
            await asyncio.gather(
                *(self._terminate_process(process) for process in processes),
                return_exceptions=True,
            )

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
        if (
            mode is SortMode.SIZE
            and result.state is ScanState.COMPLETE
            and result.uncompressed_bytes is not None
        ):
            return (0, -result.uncompressed_bytes, name_key, identity)
        if mode is SortMode.RATIO and result.state is ScanState.COMPLETE:
            ratio = result.ratio_fraction
            if ratio is not None:
                return (0, ratio, name_key, identity)
        return (1, record.ordinal, name_key, identity)

    return sorted(records, key=key)


class BrowserModel:
    """Hold navigation, selection, cache, and current directory row state."""

    def __init__(self, initial_path: Path, cache: ResultCache) -> None:
        self.cache = cache
        self.current_path = initial_path
        self.sort_mode = SortMode.SIZE
        self.view_id = 0
        self.records: dict[str, DirectoryRecord] = {}
        self.listing_warning: str | None = None
        self.listing_error: str | None = None
        self.selected_path: Path | None = None
        self._selection_by_directory: dict[str, Path] = {}

    def begin_view(self, path: Path, *, refresh: bool = False) -> int:
        """Start a new view before any filesystem or subprocess work."""

        if self.selected_path is not None:
            self._selection_by_directory[path_key(self.current_path)] = (
                self.selected_path
            )
        if refresh:
            self.cache.invalidate(record.entry.path for record in self.records.values())
        self.current_path = path
        self.view_id += 1
        self.records = {}
        self.listing_warning = None
        self.listing_error = None
        self.selected_path = self._selection_by_directory.get(path_key(path))
        return self.view_id

    def set_listing(self, view_id: int, listing: DirectoryListing) -> list[ScanRequest]:
        """Apply a direct listing and return missing-statistics requests."""

        if view_id != self.view_id:
            return []
        self.records = {}
        self.listing_warning = listing.warning
        self.listing_error = listing.error
        requests: list[ScanRequest] = []
        for ordinal, entry in enumerate(listing.entries):
            cached = self.cache.get(entry.path)
            result = cached if cached is not None else ScanResult.pending(entry.path)
            self.records[path_key(entry.path)] = DirectoryRecord(entry, result, ordinal)
            if cached is None:
                priority = ordinal
                if self.selected_path is not None and path_key(
                    self.selected_path
                ) == path_key(entry.path):
                    priority = -1
                requests.append(ScanRequest(entry.path, view_id, priority))
        if (
            self.selected_path is None
            or path_key(self.selected_path) not in self.records
        ):
            ordered = self.sorted_records()
            self.selected_path = ordered[0].entry.path if ordered else None
        return requests

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
        self.records[key] = DirectoryRecord(record.entry, result, record.ordinal)
        return True

    def requests_for_missing_results(self) -> list[ScanRequest]:
        """Return current rows that still need a scan."""

        return [
            ScanRequest(record.entry.path, self.view_id, index)
            for index, record in enumerate(self.sorted_records())
            if record.result.state is not ScanState.COMPLETE
        ]


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
        self._active: dict[int, tuple[ScanJob, asyncio.Task[ScanResult]]] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._next_request_id = 0
        self._next_sequence = 0
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
            else:
                for key in tuple(self._pending):
                    if key not in desired_keys:
                        del self._pending[key]
            active_paths = {
                path_key(job.path)
                for job, _task in self._active.values()
                if job.view_id == view_id
            }
            for key, request in request_map.items():
                if key in active_paths:
                    continue
                self._next_request_id += 1
                self._next_sequence += 1
                self._pending[key] = ScanJob(
                    request_id=self._next_request_id,
                    path=request.path,
                    view_id=view_id,
                    priority=(request.priority * 1_000_000) + self._next_sequence,
                )
            self._condition.notify_all()

    async def close(self) -> None:
        """Cancel queued work, active scans, and worker tasks."""

        async with self._condition:
            if self._closed:
                return
            self._closed = True
            self._pending.clear()
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
                job_key, job = min(
                    self._pending.items(),
                    key=lambda item: (item[1].priority, item[1].request_id),
                )
                del self._pending[job_key]

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
    """Format a compression ratio as a percentage."""

    if ratio is None:
        return "—"
    return f"{ratio * 100:.1f}%"


def render_bar(
    result: ScanResult,
    maximum_uncompressed: int,
    width: int,
) -> Text:
    """Render compressed usage and logical savings in one bar."""

    bar = Text()
    if width <= 0:
        return bar
    if (
        result.state is ScanState.COMPLETE
        and result.uncompressed_bytes is not None
        and result.disk_usage_bytes is not None
        and maximum_uncompressed > 0
    ):
        logical_fraction = max(0.0, result.uncompressed_bytes / maximum_uncompressed)
        disk_fraction = max(0.0, result.disk_usage_bytes / maximum_uncompressed)
        logical_cells = min(width, round(width * logical_fraction))
        disk_cells = min(logical_cells, round(width * disk_fraction))
        bar.append("█" * disk_cells, style="green")
        bar.append("░" * max(0, logical_cells - disk_cells), style="yellow")
        bar.append(" " * max(0, width - logical_cells))
        return bar
    if result.state is ScanState.ERROR:
        bar.append("!" * width, style="red")
    else:
        bar.append("·" * width, style="dim")
    return bar


class DirectoryRow(Static):
    """Render one directory record with responsive columns."""

    def __init__(self, record: DirectoryRecord, maximum_uncompressed: int) -> None:
        super().__init__(markup=False, classes="directory-row")
        self.record = record
        self.maximum_uncompressed = maximum_uncompressed

    def update_record(self, record: DirectoryRecord, maximum_uncompressed: int) -> None:
        """Replace row data and refresh its display."""

        self.record = record
        self.maximum_uncompressed = maximum_uncompressed
        self.tooltip = record.result.error or record.result.warning
        self.refresh()

    def on_resize(self, _event: events.Resize) -> None:
        """Refresh the bar when the terminal width changes."""

        self.refresh()

    def render(self) -> Text:
        """Build a row using fixed numeric columns and a flexible graph."""

        width = max(
            self.size.width,
            NAME_COLUMN_WIDTH + RATIO_COLUMN_WIDTH + SIZE_COLUMN_WIDTH + 5,
        )
        fixed_width = NAME_COLUMN_WIDTH + RATIO_COLUMN_WIDTH + SIZE_COLUMN_WIDTH + 4
        graph_width = max(1, width - fixed_width)
        result = self.record.result
        name = truncate_middle(self.record.entry.name, NAME_COLUMN_WIDTH)
        ratio = format_ratio(result.ratio)
        size = format_bytes(result.uncompressed_bytes)
        if result.state is ScanState.ERROR and not result.has_statistics:
            ratio = "error"
            size = "error"

        line = Text()
        line.append(pad_right(name, NAME_COLUMN_WIDTH))
        line.append(" ")
        line.append(render_bar(result, self.maximum_uncompressed, graph_width))
        line.append(" ")
        ratio_start = len(line.plain)
        line.append(pad_left(ratio, RATIO_COLUMN_WIDTH))
        line.append(" ")
        size_start = len(line.plain)
        line.append(pad_left(size, SIZE_COLUMN_WIDTH))
        if result.state is ScanState.ERROR:
            line.stylize("red", ratio_start, len(line.plain))
        elif result.state is ScanState.RUNNING:
            line.stylize("dim", ratio_start, len(line.plain))
        elif result.warning:
            line.stylize("yellow", ratio_start, size_start)
        return line


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

        text = """Compsizer controls

Up/Down, j/k   Move selection
Enter, l        Open selected directory
Backspace, h    Open parent directory
Tab             Change pane
s               Sort by size / compression ratio
c               Toggle result cache
r               Refresh current directory and rescan
?               Show this help
q               Quit

Bars show disk usage (█) and the difference from uncompressed
extent usage (░). The size column shows uncompressed bytes.
Independent directory scans are not additive because Btrfs
reflinks and shared extents may overlap.

Esc             Close help"""
        yield Container(Static(Text(text), id="help-text"), id="help-dialog")

    def action_close(self) -> None:
        """Close the help dialog."""

        self.dismiss(None)


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
    padding: 0 1;
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
        Binding("up", "move_up", "Move up"),
        Binding("k", "move_up", "Move up", show=False),
        Binding("down", "move_down", "Move down"),
        Binding("j", "move_down", "Move down", show=False),
        Binding("enter", "open_selected", "Open", key_display="Enter"),
        Binding("l", "open_selected", "Open", show=False),
        Binding("backspace", "go_parent", "Parent", key_display="Backspace"),
        Binding("h", "go_parent", "Parent", show=False),
        Binding("tab", "focus_next", "Focus", key_display="Tab"),
        Binding("s", "toggle_sort", "Sort"),
        Binding("c", "toggle_cache", "Cache"),
        Binding("r", "refresh_view", "Refresh"),
        Binding("?", "show_help", "Help"),
    ]

    def __init__(self, initial_path: Path, runner: ScanRunner | None = None) -> None:
        super().__init__()
        self.cache = ResultCache()
        self.model = BrowserModel(initial_path, self.cache)
        self.runner = runner if runner is not None else CompsizeRunner()
        self.manager = ScanManager(self.runner, self._on_scan_update)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._view_load_task: asyncio.Task[Any] | None = None
        self._tree_reveal_task: asyncio.Task[Any] | None = None
        self._tree_loaded: set[str] = set()
        self._tree_loading: set[str] = set()
        self._row_items: dict[str, ListItem] = {}
        self._row_render_lock = asyncio.Lock()

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
                yield Label(self._column_header(), id="column-label")
                yield ListView(id="directory-list")
                yield Static("", id="empty-label")
                yield Static("", id="status")
        yield Footer()

    async def on_mount(self) -> None:
        """Start background services and load the initial directory."""

        self._track(self._initialize())

    async def on_unmount(self) -> None:
        """Stop directory loading and subprocess workers before exit."""

        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.manager.close()

    async def _initialize(self) -> None:
        """Start the scan manager and schedule initial filesystem work."""

        await self.manager.start()
        self._navigate_to(self.model.current_path)
        tree = self.query_one("#directory-tree", Tree)
        tree.root.expand()
        self._track(self._load_tree_children(tree.root))
        self.set_focus(self.query_one("#directory-list", ListView))

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

        if self._view_load_task is not None and not self._view_load_task.done():
            self._view_load_task.cancel()
        view_id = self.model.begin_view(path, refresh=refresh)
        self._update_path_label()
        self._update_status("Enumerating directories…")
        self._view_load_task = self._track(self._load_view(view_id, path))
        if self._tree_reveal_task is not None and not self._tree_reveal_task.done():
            self._tree_reveal_task.cancel()
        self._tree_reveal_task = self._track(self._reveal_tree_path(path))
        self._track(self._render_rows())

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
        if view_id != self.model.view_id:
            return
        requests = self.model.set_listing(view_id, listing)
        await self._render_rows()
        self._update_status()
        await self.manager.set_view(view_id, requests)

    async def _on_scan_update(self, job: ScanJob, result: ScanResult) -> None:
        """Apply a scan update and refresh only the affected current view."""

        changed = self.model.apply_scan_update(job, result)
        if changed:
            await self._render_rows()
        self._update_status()

    async def _render_rows(self) -> None:
        """Rebuild rows while restoring selection by path identity."""

        async with self._row_render_lock:
            list_view = self.query_one("#directory-list", ListView)
            while not list_view.is_attached:
                await asyncio.sleep(0)
            records = self.model.sorted_records()
            maximum_uncompressed = max(
                (
                    record.result.uncompressed_bytes or 0
                    for record in records
                    if record.result.state is ScanState.COMPLETE
                ),
                default=0,
            )
            items: list[ListItem] = []
            self._row_items = {}
            for record in records:
                row = DirectoryRow(record, maximum_uncompressed)
                item = ListItem(row)
                items.append(item)
                self._row_items[path_key(record.entry.path)] = item
            list_view.index = None
            await list_view.remove_children()
            if items:
                await list_view.mount(*items)
                if self.model.selected_path is not None:
                    selected_item = self._row_items.get(
                        path_key(self.model.selected_path)
                    )
                    if selected_item is not None:
                        list_view.index = items.index(selected_item)
            empty_label = self.query_one("#empty-label", Static)
            if records:
                empty_label.update("")
            elif self.model.listing_error:
                empty_label.update("No directories available.")
            else:
                empty_label.update("No child directories.")
            self._update_status()

    def _update_path_label(self) -> None:
        """Update the current path and sort indicators."""

        label = self.query_one("#path-label", Label)
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

        status = self.query_one("#status", Static)
        if transient:
            status.update(Text(transient))
            return
        records = tuple(self.model.records.values())
        counts = {
            state: sum(record.result.state is state for record in records)
            for state in ScanState
        }
        parts = [
            f"complete {counts[ScanState.COMPLETE]}",
            f"running {counts[ScanState.RUNNING]}",
            f"pending {counts[ScanState.PENDING]}",
            f"errors {counts[ScanState.ERROR]}",
        ]
        if self.model.listing_error:
            parts.append(self.model.listing_error)
        elif self.model.listing_warning:
            parts.append(self.model.listing_warning)
        if self.model.selected_path is not None:
            selected = self.model.records.get(path_key(self.model.selected_path))
            if selected is not None and selected.result.error:
                parts.append(selected.result.error)
            elif selected is not None and selected.result.warning:
                parts.append(selected.result.warning)
        status.update(Text("  ".join(parts)))

    @staticmethod
    def _column_header() -> str:
        """Return the fixed-column header shown above the rows."""

        return f"{pad_right('Directory', NAME_COLUMN_WIDTH)} {'Bar':<{10}} {pad_left('Ratio', RATIO_COLUMN_WIDTH)} {pad_left('Uncompressed', SIZE_COLUMN_WIDTH)}"

    @staticmethod
    def _tree_label(path: Path) -> Text:
        """Return a safe tree label for a filesystem path."""

        return Text(path.name or str(path), no_wrap=True, overflow="ellipsis")

    async def _load_tree_children(self, node: TreeNode[Any]) -> None:
        """Load one tree node's direct children without recursion."""

        path = node.data
        if not isinstance(path, Path):
            return
        key = path_key(path)
        if key in self._tree_loaded or key in self._tree_loading:
            return
        self._tree_loading.add(key)
        try:
            listing = await asyncio.to_thread(enumerate_directories, path)
            node.remove_children()
            for entry in listing.entries:
                node.add(self._tree_label(entry.path), entry.path, allow_expand=True)
            node.allow_expand = bool(listing.entries)
            self._tree_loaded.add(key)
        finally:
            self._tree_loading.discard(key)

    async def _reveal_tree_path(self, path: Path) -> None:
        """Reveal the current path in the lazily loaded directory tree."""

        tree = self.query_one("#directory-tree", Tree)
        root = tree.root
        root_path = root.data
        if not isinstance(root_path, Path):
            return
        try:
            relative_parts = path.relative_to(root_path).parts
        except ValueError:
            root.set_label(self._tree_label(path))
            root.data = path
            root.remove_children()
            root.allow_expand = True
            self._tree_loaded.clear()
            self._tree_loading.clear()
            tree.move_cursor(root)
            await self._load_tree_children(root)
            return

        node = root
        for part in relative_parts:
            await self._load_tree_children(node)
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
                return
            node.expand()
            node = child
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

    def action_move_up(self) -> None:
        """Move the focused pane upward."""

        focused = self.focused
        if isinstance(focused, (Tree, ListView)):
            focused.action_cursor_up()

    def action_move_down(self) -> None:
        """Move the focused pane downward."""

        focused = self.focused
        if isinstance(focused, (Tree, ListView)):
            focused.action_cursor_down()

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
        """Switch between size and ratio sorting."""

        self.model.sort_mode = self.model.sort_mode.toggled()
        self._update_path_label()
        self._track(self._render_rows())
        self._track(
            self.manager.set_view(
                self.model.view_id, self.model.requests_for_missing_results()
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
            requests = self.model.requests_for_missing_results()
            self._track(self.manager.set_view(self.model.view_id, requests))
        self._track(self._render_rows())

    def action_refresh_view(self) -> None:
        """Re-enumerate and rescan the current directory."""

        self._navigate_to(self.model.current_path, refresh=True)

    def action_show_help(self) -> None:
        """Open the keyboard and semantics help screen."""

        self.push_screen(HelpScreen())

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Keep model selection attached to the highlighted path."""

        if event.item is None:
            return
        row = next(iter(event.item.query(DirectoryRow)), None)
        if row is None:
            return
        self.model.select(row.record.entry.path)
        self._update_status()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter the directory selected in the right pane."""

        row = next(iter(event.item.query(DirectoryRow)), None)
        if row is not None:
            self._navigate_to(row.record.entry.path)

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
        description="Browse Btrfs compression statistics with compsize.",
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
