import asyncio
import os
import shlex
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

from textual.widgets import ListView, Tree

from compsizer import (
    DIRECTORY_PAGE_SIZE,
    TREE_CHILD_LIMIT,
    BarScales,
    BrowserModel,
    CompsizeParseError,
    CompsizerApp,
    CompsizeRunner,
    DirectoryEntry,
    DirectoryListing,
    DirectoryRecord,
    DirectoryRow,
    DuRunner,
    ElevatedScanPrompt,
    EntryKind,
    FilesystemDetector,
    InvalidInitialPathError,
    ResultCache,
    ScanJob,
    ScanManager,
    ScanRequest,
    ScanResult,
    ScanState,
    SortMode,
    WindowsFileApi,
    WindowsFileMetadata,
    WindowsScanRunner,
    _scandir_path,
    enumerate_directories,
    normalize_initial_path,
    parse_compsize_output,
    render_bar,
    sort_records,
)


class ParserTests(unittest.TestCase):
    """Test the standalone compsize report parser."""

    def test_parses_total_and_multiple_compression_types(self) -> None:
        stdout = """
        Processed 3 files.
        Type       Perc   Disk Usage   Uncompressed   Referenced
        TOTAL      41%    400          1000           1200
        none       100%   100           100            100
        zstd       20%    300          900            1100
        zstd       20%    300          900            1100
        """

        report = parse_compsize_output(stdout)

        self.assertEqual(report.disk_usage_bytes, 400)
        self.assertEqual(report.uncompressed_bytes, 1000)
        self.assertEqual(report.referenced_bytes, 1200)
        self.assertEqual(report.compression_types, ("none", "zstd"))
        result = ScanResult(
            Path("/tmp/example"),
            ScanState.COMPLETE,
            report.disk_usage_bytes,
            report.uncompressed_bytes,
            report.referenced_bytes,
        )
        self.assertAlmostEqual(result.ratio or 0.0, 0.4)

    def test_parses_compression_types_before_a_trailing_total(self) -> None:
        report = parse_compsize_output(
            "Processed 2 files, 3 extents.\n"
            "Type       Perc   Disk Usage   Uncompressed   Referenced\n"
            "none       100%   100          100            100\n"
            "zstd       20%    300          900            1100\n"
            "TOTAL      41%    400          1000           1200\n"
        )

        self.assertEqual(report.compression_types, ("none", "zstd"))
        self.assertEqual(report.disk_usage_bytes, 400)

    def test_uses_only_the_last_processed_report(self) -> None:
        report = parse_compsize_output(
            "Processed 1 files.\n"
            "none       100%   10           10             10\n"
            "TOTAL      100%   10           10             10\n"
            "Processed 2 files, 3 extents.\n"
            "zstd       20%    300          900            1100\n"
            "TOTAL      41%    400          1000           1200\n"
        )

        self.assertEqual(report.compression_types, ("zstd",))
        self.assertEqual(report.disk_usage_bytes, 400)

    def test_uses_stderr_as_a_warning(self) -> None:
        report = parse_compsize_output(
            "TOTAL 50% 50 100 100\n",
            "warning: one extent was skipped\n",
        )

        self.assertEqual(report.warning, "warning: one extent was skipped")

    def test_parses_empty_results(self) -> None:
        empty_from_stderr = parse_compsize_output("", "No files.\n")
        empty_from_stdout = parse_compsize_output("No files.\n")
        empty_processed = parse_compsize_output("Processed 0 files.\n")
        empty_processed_details = parse_compsize_output(
            "Processed 0 files, 0 extents.\n"
        )
        empty_processed_stderr = parse_compsize_output(
            "", "Processed 0 files, 0 extents.\n"
        )

        for report in (
            empty_from_stderr,
            empty_from_stdout,
            empty_processed,
            empty_processed_details,
            empty_processed_stderr,
        ):
            self.assertTrue(report.empty)
            self.assertEqual(report.disk_usage_bytes, 0)
            self.assertEqual(report.uncompressed_bytes, 0)

    def test_ignores_unexpected_spacing(self) -> None:
        report = parse_compsize_output("\tTOTAL\t99%\t10\t20\t30\n")

        self.assertEqual(
            (
                report.disk_usage_bytes,
                report.uncompressed_bytes,
                report.referenced_bytes,
            ),
            (10, 20, 30),
        )

    def test_rejects_malformed_output(self) -> None:
        with self.assertRaises(CompsizeParseError):
            parse_compsize_output("TOTAL 50% not-a-byte 10 10\n")
        with self.assertRaises(CompsizeParseError):
            parse_compsize_output("Processed 4 files.\n")


class CacheTests(unittest.TestCase):
    """Test process-local cache behavior."""

    def test_insert_retrieve_bypass_and_invalidate(self) -> None:
        path = Path("/tmp/cache-entry")
        result = ScanResult(path, ScanState.COMPLETE, 5, 10, 10)
        cache = ResultCache()

        cache.put(result)
        self.assertEqual(cache.get(path), result)
        cache.set_enabled(False)
        self.assertIsNone(cache.get(path))
        cache.put(ScanResult(path, ScanState.COMPLETE, 7, 14, 14))
        cache.set_enabled(True)
        self.assertEqual(cache.get(path), result)
        cache.invalidate([path])
        self.assertIsNone(cache.get(path))

    def test_errors_are_not_cached(self) -> None:
        cache = ResultCache()
        path = Path("/tmp/error-entry")

        cache.put(ScanResult.error_result(path, "failed"))

        self.assertEqual(len(cache), 0)


class SortingTests(unittest.TestCase):
    """Test stable identity-aware row ordering."""

    @staticmethod
    def record(
        name: str,
        ordinal: int,
        result: ScanResult,
    ) -> DirectoryRecord:
        """Build a record for a sorting assertion."""

        return DirectoryRecord(
            DirectoryEntry(Path("/root") / name, name), result, ordinal
        )

    def test_size_descending_puts_unknown_rows_after_known_rows(self) -> None:
        records = [
            self.record(
                "small",
                0,
                ScanResult(Path("/root/small"), ScanState.COMPLETE, 5, 10, 10),
            ),
            self.record(
                "large",
                1,
                ScanResult(Path("/root/large"), ScanState.COMPLETE, 5, 100, 100),
            ),
            self.record("pending", 2, ScanResult.pending(Path("/root/pending"))),
            self.record(
                "error", 3, ScanResult.error_result(Path("/root/error"), "failed")
            ),
        ]

        ordered = sort_records(records, SortMode.SIZE)

        self.assertEqual(
            [record.entry.name for record in ordered],
            ["large", "small", "pending", "error"],
        )

    def test_size_sort_keeps_exact_and_estimated_values_in_separate_groups(
        self,
    ) -> None:
        records = [
            self.record(
                "estimate-large",
                0,
                ScanResult(
                    Path("/root/estimate-large"),
                    ScanState.COMPLETE,
                    400,
                    2000,
                    None,
                    is_estimate=True,
                ),
            ),
            self.record(
                "exact-small",
                1,
                ScanResult(Path("/root/exact-small"), ScanState.COMPLETE, 10, 100, 100),
            ),
            self.record(
                "estimate-small",
                2,
                ScanResult(
                    Path("/root/estimate-small"),
                    ScanState.COMPLETE,
                    40,
                    50,
                    None,
                    is_estimate=True,
                ),
            ),
            self.record(
                "exact-large",
                3,
                ScanResult(Path("/root/exact-large"), ScanState.COMPLETE, 20, 200, 200),
            ),
        ]

        ordered = sort_records(records, SortMode.SIZE)

        self.assertEqual(
            [record.entry.name for record in ordered],
            ["exact-large", "exact-small", "estimate-large", "estimate-small"],
        )

    def test_bar_scales_are_independent_for_exact_and_estimated_results(
        self,
    ) -> None:
        exact = ScanResult(Path("/root/exact"), ScanState.COMPLETE, 40, 100, 100)
        estimate = ScanResult(
            Path("/root/estimate"),
            ScanState.COMPLETE,
            400,
            1000,
            None,
            is_estimate=True,
        )
        scales = BarScales.from_records(
            [
                DirectoryRecord(DirectoryEntry(exact.path, "exact"), exact, 0),
                DirectoryRecord(DirectoryEntry(estimate.path, "estimate"), estimate, 1),
            ]
        )

        exact_bar = render_bar(exact, scales.maximum_for(exact), 12)
        estimate_bar = render_bar(estimate, scales.maximum_for(estimate), 12)

        self.assertEqual(exact_bar.plain, estimate_bar.plain)

    def test_ratio_best_first(self) -> None:
        records = [
            self.record(
                "poor",
                0,
                ScanResult(Path("/root/poor"), ScanState.COMPLETE, 80, 100, 100),
            ),
            self.record(
                "best",
                1,
                ScanResult(Path("/root/best"), ScanState.COMPLETE, 20, 100, 100),
            ),
            self.record("pending", 2, ScanResult.pending(Path("/root/pending"))),
        ]

        ordered = sort_records(records, SortMode.RATIO)

        self.assertEqual(
            [record.entry.name for record in ordered], ["best", "poor", "pending"]
        )

    def test_savings_uses_absolute_byte_difference(self) -> None:
        records = [
            self.record(
                "large-modest",
                0,
                ScanResult(
                    Path("/root/large-modest"), ScanState.COMPLETE, 700, 1000, 1000
                ),
            ),
            self.record(
                "small-good",
                1,
                ScanResult(Path("/root/small-good"), ScanState.COMPLETE, 50, 400, 400),
            ),
            self.record(
                "waste",
                2,
                ScanResult(Path("/root/waste"), ScanState.COMPLETE, 120, 100, 100),
            ),
            self.record("pending", 3, ScanResult.pending(Path("/root/pending"))),
        ]

        ordered = sort_records(records, SortMode.SAVINGS)

        self.assertEqual(
            [record.entry.name for record in ordered],
            ["small-good", "large-modest", "waste", "pending"],
        )

    def test_name_sort_orders_all_rows_case_insensitively(self) -> None:
        records = [
            self.record("zeta", 0, ScanResult.pending(Path("/root/zeta"))),
            self.record(
                "Alpha", 1, ScanResult.error_result(Path("/root/Alpha"), "failed")
            ),
            self.record(
                "beta", 2, ScanResult(Path("/root/beta"), ScanState.COMPLETE, 5, 10, 10)
            ),
        ]

        ordered = sort_records(records, SortMode.NAME)

        self.assertEqual(
            [record.entry.name for record in ordered], ["Alpha", "beta", "zeta"]
        )

    def test_sort_mode_cycles_through_all_criteria(self) -> None:
        mode = SortMode.SIZE

        mode = mode.toggled()
        self.assertIs(mode, SortMode.RATIO)
        mode = mode.toggled()
        self.assertIs(mode, SortMode.SAVINGS)
        mode = mode.toggled()
        self.assertIs(mode, SortMode.NAME)
        self.assertIs(mode.toggled(), SortMode.SIZE)

    def test_visible_scan_requests_precede_offscreen_rows(self) -> None:
        root = Path("/root")
        entries = tuple(
            DirectoryEntry(root / name, name)
            for name in ("alpha", "beta", "gamma", "delta")
        )
        model = BrowserModel(root, ResultCache())
        view_id = model.begin_view(root)
        model.set_listing(view_id, DirectoryListing(root, entries))
        model.select(root / "beta")

        requests = model.requests_for_missing_results([root / "delta"])

        self.assertEqual(
            [
                request.path.name
                for request in sorted(requests, key=lambda item: item.priority)
            ],
            ["beta", "delta", "alpha", "gamma"],
        )
        self.assertEqual(
            {request.path for request in requests},
            {root / entry.name for entry in entries},
        )


class RenderingTests(unittest.TestCase):
    """Test visible graph behavior for complete size measurements."""

    def test_zero_size_complete_bar_is_blank(self) -> None:
        result = ScanResult(Path("/root/empty"), ScanState.COMPLETE, 0, 0, 0)

        bar = render_bar(result, maximum_size=0, width=8)

        self.assertEqual(bar.plain, " " * 8)


class NavigationTests(unittest.TestCase):
    """Test path normalization and direct directory enumeration."""

    def test_enumeration_skips_files_and_directory_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "real").mkdir()
            (root / ".hidden").mkdir()
            (root / "file").write_text("content", encoding="utf-8")
            try:
                os.symlink(root / "real", root / "linked")
            except OSError as exc:
                self.skipTest(f"Cannot create a symlink in this environment: {exc}")

            listing = enumerate_directories(root)

            self.assertEqual(
                [entry.name for entry in listing.entries], [".hidden", "real"]
            )
            self.assertTrue(
                all(entry.kind is EntryKind.DIRECTORY for entry in listing.entries)
            )

    def test_windows_enumeration_keeps_volume_mount_directories_browsable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            real_directory = MagicMock()
            real_directory.name = "real"
            real_directory.is_dir.return_value = True
            mounted_volume = MagicMock()
            mounted_volume.name = "mounted-volume"
            mounted_volume.is_dir.return_value = True
            scanner = MagicMock()
            scanner.__enter__.return_value = scanner
            scanner.__exit__.return_value = None
            scanner.__iter__.return_value = iter((real_directory, mounted_volume))

            with patch("compsizer.os.scandir", return_value=scanner):
                listing = enumerate_directories(root)

            self.assertEqual(
                [entry.name for entry in listing.entries], ["mounted-volume", "real"]
            )

    def test_filesystem_detection_uses_the_deepest_mount_and_decodes_paths(
        self,
    ) -> None:
        mountinfo = r"""22 1 8:1 / / rw,relatime - ext4 /dev/root rw
        23 22 0:45 / /mnt/archive rw,relatime - btrfs /dev/data rw
        24 23 8:2 / /mnt/archive/snapshots rw - ext4 /dev/snapshot rw
        25 22 8:3 / /mnt/space\040name rw - xfs /dev/space rw"""

        self.assertEqual(
            FilesystemDetector.filesystem_type(Path("/mnt/archive/photos"), mountinfo),
            "btrfs",
        )
        self.assertEqual(
            FilesystemDetector.filesystem_type(
                Path("/mnt/archive/snapshots/daily"),
                mountinfo,
            ),
            "ext4",
        )
        self.assertEqual(
            FilesystemDetector.filesystem_type(Path("/mnt/space name"), mountinfo),
            "xfs",
        )

    def test_normalizes_directories_and_rejects_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            file_path = root / "file"
            file_path.write_text("content", encoding="utf-8")

            self.assertEqual(normalize_initial_path(root / "."), root.resolve())
            with self.assertRaises(InvalidInitialPathError):
                normalize_initial_path(file_path)

    def test_selection_stays_with_path_when_results_reorder(self) -> None:
        root = Path("/root")
        first = DirectoryEntry(root / "first", "first")
        second = DirectoryEntry(root / "second", "second")
        cache = ResultCache()
        model = BrowserModel(root, cache)
        view_id = model.begin_view(root)
        model.set_listing(view_id, DirectoryListing(root, (first, second)))
        self.assertEqual(model.state_counts[ScanState.PENDING], 2)
        model.select(second.path)

        model.apply_scan_update(
            ScanJob(1, second.path, view_id, 0),
            ScanResult(second.path, ScanState.COMPLETE, 20, 200, 200),
        )
        model.apply_scan_update(
            ScanJob(2, first.path, view_id, 1),
            ScanResult(first.path, ScanState.COMPLETE, 5, 50, 50),
        )

        self.assertEqual(model.sorted_records()[0].entry.path, second.path)
        self.assertEqual(model.selected_path, second.path)
        self.assertEqual(model.state_counts[ScanState.PENDING], 0)
        self.assertEqual(model.state_counts[ScanState.COMPLETE], 2)

    def test_refresh_invalidates_current_path_and_child_results(self) -> None:
        root = Path("/root/current")
        child = root / "child"
        cache = ResultCache()
        cache.put(ScanResult(root, ScanState.COMPLETE, 10, 20, 20))
        cache.put(ScanResult(child, ScanState.COMPLETE, 5, 10, 10))
        model = BrowserModel(root, cache)
        view_id = model.begin_view(root)
        model.set_listing(
            view_id,
            DirectoryListing(root, (DirectoryEntry(child, child.name),)),
        )

        model.begin_view(root, refresh=True)

        self.assertIsNone(cache.get(root))
        self.assertIsNone(cache.get(child))


class FakeRunner:
    """Small asynchronous runner used to test bounded scheduling."""

    def __init__(
        self,
        delay: float = 0.01,
        uncompressed_by_name: dict[str, int] | None = None,
    ) -> None:
        self.delay = delay
        self.uncompressed_by_name = uncompressed_by_name or {}
        self.active = 0
        self.maximum_active = 0
        self.closed = False

    async def scan(self, path: Path) -> ScanResult:
        """Return a predictable result after a short asynchronous delay."""

        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            uncompressed = self.uncompressed_by_name.get(
                path.name, 200 if path.name == "beta" else 100
            )
            return ScanResult(path, ScanState.COMPLETE, 10, uncompressed, uncompressed)
        finally:
            self.active -= 1

    async def close(self) -> None:
        """Record manager shutdown."""

        self.closed = True


class ErrorRunner:
    """Count scans that always end in a visible error."""

    def __init__(self) -> None:
        self.calls: list[Path] = []

    async def scan(self, path: Path) -> ScanResult:
        """Return one deterministic scan error."""

        self.calls.append(path)
        await asyncio.sleep(0.01)
        return ScanResult.error_result(path, "test scan failure")

    async def close(self) -> None:
        """Stop the fake runner."""


class AppTests(unittest.IsolatedAsyncioTestCase):
    """Exercise immediate UI loading with a fake compression runner."""

    @staticmethod
    def _write_executable(path: Path, content: str) -> None:
        """Create a temporary executable for the integrated privilege flow."""

        path.write_text(f"#!/bin/sh\n{content}", encoding="utf-8")
        path.chmod(0o755)

    async def test_navigation_renders_directories_before_scan_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "alpha").mkdir()
            (root / "beta").mkdir()
            (root / "alpha" / "nested").mkdir()
            app = CompsizerApp(root, runner=FakeRunner(delay=0.2))

            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(0.05)
                self.assertEqual(
                    {record.entry.name for record in app.model.records.values()},
                    {"alpha", "beta"},
                )
                items_before = dict(app._row_items)
                directory_list = app.query_one("#directory-list", ListView)
                for _ in range(20):
                    if directory_list.index is not None:
                        break
                    await pilot.pause(0.01)
                self.assertIsNotNone(directory_list.index)
                await pilot.pause(0.3)
                self.assertEqual(
                    [record.entry.name for record in app.model.sorted_records()],
                    ["beta", "alpha"],
                )
                for key, item in items_before.items():
                    self.assertIs(app._row_items[key], item)
                await pilot.press("home")
                await pilot.pause(0.01)
                self.assertEqual(app.model.selected_path, root / "beta")
                self.assertEqual(directory_list.index, 0)
                await pilot.press("end")
                await pilot.pause(0.01)
                self.assertEqual(app.model.selected_path, root / "alpha")
                self.assertEqual(directory_list.index, 1)
                await pilot.press("enter")
                await pilot.pause(0.01)
                self.assertEqual(app.model.current_path, root / "alpha")
                self.assertIn(
                    "nested",
                    {record.entry.name for record in app.model.records.values()},
                )
                await pilot.press("backspace")

    async def test_error_rows_wait_for_refresh_before_retrying(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            child = root / "child"
            child.mkdir()
            runner = ErrorRunner()
            app = CompsizerApp(root, runner=runner)

            async with app.run_test(size=(100, 30)) as pilot:
                for _ in range(100):
                    records = tuple(app.model.records.values())
                    if records and records[0].result.state is ScanState.ERROR:
                        break
                    await pilot.pause(0.01)
                self.assertEqual(runner.calls, [child])

                await pilot.pause(0.1)
                self.assertEqual(runner.calls, [child])

                await pilot.press("r")
                for _ in range(100):
                    records = tuple(app.model.records.values())
                    if (
                        len(runner.calls) == 2
                        and records
                        and records[0].result.state is ScanState.ERROR
                    ):
                        break
                    await pilot.pause(0.01)
                self.assertEqual(runner.calls, [child, child])
                await pilot.pause(0.1)
                self.assertEqual(runner.calls, [child, child])

    async def test_status_diagnostics_fit_within_two_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app = CompsizerApp(Path(temporary_directory), runner=FakeRunner())

            async with app.run_test(size=(80, 20)) as pilot:
                await pilot.pause(0.05)
                app.model.listing_warning = "Long diagnostic. " * 200
                app._update_status()

                status = app.query_one("#status")
                rendered = status.render().plain
                maximum_cells = max(1, status.size.width - 2) * 2
                self.assertLessEqual(len(rendered), maximum_cells)
                self.assertTrue(rendered.endswith("…"))

    async def test_refresh_reloads_tree_and_invalidates_current_path_cache(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            child = root / "child"
            child.mkdir()
            old_child = child / "old"
            old_child.mkdir()
            app = CompsizerApp(root, runner=FakeRunner())

            async with app.run_test(size=(100, 30)) as pilot:
                tree = app.query_one("#directory-tree", Tree)
                for _ in range(100):
                    tree_child = next(
                        (node for node in tree.root.children if node.data == child),
                        None,
                    )
                    if app.cache.get(child) is not None and tree_child is not None:
                        break
                    await pilot.pause(0.01)
                self.assertIsNotNone(app.cache.get(child))
                self.assertIsNotNone(tree_child)
                if tree_child is None:
                    self.fail("The directory tree did not load the child path.")

                tree_child.expand()
                for _ in range(100):
                    if any(node.data == old_child for node in tree_child.children):
                        break
                    await pilot.pause(0.01)
                self.assertIn(old_child, [node.data for node in tree_child.children])

                await pilot.press("enter")
                for _ in range(100):
                    if app.model.current_path == child:
                        break
                    await pilot.pause(0.01)
                self.assertEqual(app.model.current_path, child)

                new_child = child / "new"
                new_child.mkdir()
                await pilot.press("r")
                self.assertIsNone(app.cache.get(child))
                for _ in range(100):
                    names = {record.entry.name for record in app.model.records.values()}
                    tree_names = {node.data for node in tree_child.children}
                    if "new" in names and new_child in tree_names:
                        break
                    await pilot.pause(0.01)

                self.assertEqual(
                    {record.entry.name for record in app.model.records.values()},
                    {"new", "old"},
                )
                self.assertIn(new_child, [node.data for node in tree_child.children])

                app._invalidate_tree_subtree(child)
                with (
                    patch(
                        "compsizer.enumerate_directories",
                        return_value=DirectoryListing(child, error="temporary failure"),
                    ),
                    self.assertLogs("compsizer", level="WARNING"),
                ):
                    await app._load_tree_children(tree_child)
                self.assertEqual(
                    {node.data for node in tree_child.children},
                    {old_child, new_child},
                )

    async def test_large_directory_rows_are_paginated_and_browsable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for index in range(205):
                (root / f"child-{index:03d}").mkdir()
            app = CompsizerApp(root, runner=FakeRunner(delay=30.0))

            async with app.run_test(size=(120, 30)) as pilot:
                directory_list = app.query_one("#directory-list", ListView)
                tree = app.query_one("#directory-tree", Tree)

                async def wait_for_page(
                    page_index: int,
                    selected_path: Path,
                    row_count: int,
                ) -> None:
                    for _ in range(200):
                        visible_names = [
                            row.record.entry.name for row in app._row_widgets.values()
                        ]
                        if (
                            app._page_index == page_index
                            and app.model.selected_path == selected_path
                            and len(directory_list.children) == row_count
                            and visible_names
                            and visible_names[0] == selected_path.name
                        ):
                            return
                        await pilot.pause(0.01)
                    self.fail(
                        f"Directory page {page_index + 1} did not finish loading."
                    )

                for _ in range(100):
                    if (
                        len(app.model.records) == 205
                        and len(directory_list.children) == DIRECTORY_PAGE_SIZE
                        and len(tree.root.children) == TREE_CHILD_LIMIT + 1
                        and "page 1/3" in app.query_one("#status").render().plain
                    ):
                        break
                    await pilot.pause(0.01)

                self.assertEqual(len(app.model.records), 205)
                self.assertEqual(len(directory_list.children), DIRECTORY_PAGE_SIZE)
                self.assertEqual(len(tree.root.children), TREE_CHILD_LIMIT + 1)
                self.assertIn("page 1/3", app.query_one("#status").render().plain)

                await pilot.press("pagedown")
                await wait_for_page(1, root / "child-100", DIRECTORY_PAGE_SIZE)
                self.assertEqual(app._page_index, 1)
                self.assertEqual(app.model.selected_path, root / "child-100")

                await pilot.press("pagedown")
                await wait_for_page(2, root / "child-200", 5)
                self.assertEqual(app._page_index, 2)
                self.assertEqual(app.model.selected_path, root / "child-200")

                await pilot.press("pageup")
                await wait_for_page(1, root / "child-100", DIRECTORY_PAGE_SIZE)
                self.assertEqual(app._page_index, 1)
                self.assertEqual(app.model.selected_path, root / "child-100")

                await pilot.press("pagedown")
                await wait_for_page(2, root / "child-200", 5)
                self.assertEqual(app._page_index, 2)
                self.assertEqual(app.model.selected_path, root / "child-200")

                await pilot.press("enter")
                for _ in range(200):
                    if (
                        app.model.current_path == root / "child-200"
                        and not app.model.records
                        and "page 1/1" in app.query_one("#status").render().plain
                    ):
                        break
                    await pilot.pause(0.01)
                self.assertEqual(app.model.current_path, root / "child-200")

    async def test_home_and_end_keep_cursor_and_scroll_in_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for index in range(40):
                (root / f"directory-{index:02d}").mkdir()
            app = CompsizerApp(root, runner=FakeRunner(delay=1.0))

            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.pause(0.1)
                directory_list = app.query_one("#directory-list", ListView)
                for _ in range(20):
                    if directory_list.index is not None:
                        break
                    await pilot.pause(0.01)
                await pilot.press("end")
                await pilot.pause(0.01)
                self.assertEqual(directory_list.index, 39)
                self.assertGreater(directory_list.scroll_y, 0)
                await pilot.press("home")
                await pilot.pause(0.01)
                self.assertEqual(directory_list.index, 0)
                self.assertEqual(directory_list.scroll_y, 0)

    async def test_keyboard_selection_survives_pending_row_reordering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            values = {f"directory-{index:02d}": (index + 1) * 100 for index in range(8)}
            app = CompsizerApp(
                root,
                runner=FakeRunner(delay=0.15, uncompressed_by_name=values),
            )
            for name in values:
                (root / name).mkdir()

            async with app.run_test(size=(120, 20)) as pilot:
                await pilot.pause(0.03)
                directory_list = app.query_one("#directory-list", ListView)
                await pilot.press("down")
                selected_path = root / "directory-01"
                self.assertEqual(app.model.selected_path, selected_path)
                await pilot.pause(1.0)
                highlighted = directory_list.highlighted_child
                if highlighted is None:
                    self.fail("The directory list has no highlighted row.")
                row = highlighted.query_one(DirectoryRow)
                self.assertEqual(app.model.selected_path, selected_path)
                self.assertEqual(row.record.entry.path, selected_path)

    async def test_late_ui_work_is_ignored_after_unmount(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "child").mkdir()
            runner = FakeRunner()
            app = CompsizerApp(root, runner=runner)

            await app.on_unmount()
            await app._load_view(app.model.view_id, root)
            await app._on_scan_update(
                ScanJob(1, root / "child", app.model.view_id, 0),
                ScanResult(root / "child", ScanState.COMPLETE, 10, 20, 20),
            )

            self.assertTrue(runner.closed)

    async def test_elevated_scan_requires_consent_before_releasing_terminal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app = CompsizerApp(Path(temporary_directory), runner=FakeRunner())

            async with app.run_test(size=(100, 30)) as pilot:
                with patch.object(
                    app, "suspend", return_value=nullcontext()
                ) as suspend:
                    authorization = asyncio.create_task(
                        app._authorize_elevated_scans(["/bin/true"])
                    )
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ElevatedScanPrompt)
                    await pilot.press("n")
                    self.assertFalse(await authorization)
                    suspend.assert_not_called()

                    authorization = asyncio.create_task(
                        app._authorize_elevated_scans(["/bin/true"])
                    )
                    await pilot.pause()
                    await pilot.press("y")
                    self.assertTrue(await authorization)
                    suspend.assert_called_once()

                    authorization = asyncio.create_task(
                        app._authorize_elevated_scans(["/bin/true"])
                    )
                    await pilot.pause()
                    authorization.cancel()
                    await asyncio.gather(authorization, return_exceptions=True)
                    await pilot.pause()
                    self.assertNotIsInstance(app.screen, ElevatedScanPrompt)

    async def test_declining_elevation_displays_du_size_estimates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            child = root / "child"
            child.mkdir()
            compsize = root / "compsize"
            sudo = root / "sudo"
            du = root / "du"
            self._write_executable(
                compsize,
                "printf '%s: Operation not permitted\\n' \"$4\" >&2\nexit 1\n",
            )
            self._write_executable(sudo, "exit 1\n")
            self._write_executable(
                du,
                'case " $* " in\n'
                '  *" --apparent-size "*) printf "1200\\tignored\\n" ;;\n'
                '  *) printf "700\\tignored\\n" ;;\n'
                "esac\n",
            )
            app = CompsizerApp(root)
            self.assertIsInstance(app.runner, CompsizeRunner)
            app.runner.executable = str(compsize)
            app.runner.sudo_executable = str(sudo)
            app.runner.fallback_runner = DuRunner(str(du))

            with patch("compsizer.SYSTEM_EXECUTABLE_PATH", os.fspath(root)):
                async with app.run_test(size=(100, 30)) as pilot:
                    for _ in range(100):
                        if isinstance(app.screen, ElevatedScanPrompt):
                            break
                        await pilot.pause(0.01)
                    self.assertIsInstance(app.screen, ElevatedScanPrompt)
                    await pilot.press("n")

                    for _ in range(100):
                        result = next(iter(app.model.records.values())).result
                        if result.state is ScanState.COMPLETE and result.is_estimate:
                            break
                        await pilot.pause(0.01)

                    self.assertEqual(result.disk_usage_bytes, 700)
                    self.assertEqual(result.uncompressed_bytes, 1200)
                    self.assertIn("declined", result.warning or "")
                    row = next(iter(app._row_widgets.values()))
                    self.assertIn("~700 B", row.render().plain)


class ScanManagerTests(unittest.IsolatedAsyncioTestCase):
    """Test bounded asynchronous scan scheduling."""

    async def test_limits_concurrent_scans(self) -> None:
        runner = FakeRunner()
        completed: list[Path] = []

        async def callback(_job: ScanJob, result: ScanResult) -> None:
            if result.state is ScanState.COMPLETE:
                completed.append(result.path)

        manager = ScanManager(runner, callback, concurrency=2)
        await manager.start()
        requests = [ScanRequest(Path(f"/root/{index}"), 1, index) for index in range(5)]
        await manager.set_view(1, requests)
        await asyncio.sleep(0.1)
        await manager.close()

        self.assertLessEqual(runner.maximum_active, 2)
        self.assertEqual(len(completed), 5)
        self.assertTrue(runner.closed)

    async def test_priority_updates_ignore_stale_heap_entries(self) -> None:
        runner = FakeRunner()
        completed: list[Path] = []
        all_done = asyncio.Event()

        async def callback(_job: ScanJob, result: ScanResult) -> None:
            if result.state is ScanState.COMPLETE:
                completed.append(result.path)
                if len(completed) == 2:
                    all_done.set()

        manager = ScanManager(runner, callback, concurrency=1)
        first = Path("/root/first")
        second = Path("/root/second")
        await manager.set_view(
            1,
            [ScanRequest(first, 1, 1), ScanRequest(second, 1, 0)],
        )
        await manager.set_view(
            1,
            [ScanRequest(first, 1, -1), ScanRequest(second, 1, 0)],
        )
        await manager.start()
        await asyncio.wait_for(all_done.wait(), timeout=1.0)
        await manager.close()

        self.assertEqual(completed, [first, second])


class FakeWindowsFileApi:
    """Provide deterministic volume and metadata results for Windows scans."""

    def __init__(
        self,
        filesystem: str,
        metadata_by_name: dict[str, WindowsFileMetadata],
        inaccessible_names: set[str] | None = None,
    ) -> None:
        self.filesystem = filesystem
        self.metadata_by_name = metadata_by_name
        self.inaccessible_names = inaccessible_names or set()
        self.inspected_names: list[str] = []

    def filesystem_type(self, path: Path) -> str:
        """Return the configured filesystem name."""

        return self.filesystem

    def inspect_path(self, path: Path) -> WindowsFileMetadata:
        """Return configured metadata or emulate an access denial."""

        if path.name not in self.metadata_by_name and path.is_dir():
            return WindowsFileMetadata(
                file_identity=None,
                logical_size=0,
                allocated_size=0,
                is_directory=True,
                is_reparse_point=False,
                is_compressed=False,
                is_sparse=False,
            )
        self.inspected_names.append(path.name)
        if path.name in self.inaccessible_names:
            raise PermissionError(f"Access denied: {path.name}")
        return self.metadata_by_name[path.name]


class BlockingWindowsFileApi(FakeWindowsFileApi):
    """Hold metadata calls so cancellation and worker limits can be checked."""

    def __init__(self) -> None:
        super().__init__(
            "NTFS",
            {
                "payload.bin": WindowsFileMetadata(
                    file_identity=(3, b"payload-id"),
                    logical_size=10,
                    allocated_size=8,
                    is_directory=False,
                    is_reparse_point=False,
                    is_compressed=False,
                    is_sparse=False,
                )
            },
        )
        self.started = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.inspect_count = 0

    def inspect_path(self, path: Path) -> WindowsFileMetadata:
        """Wait for the test to release one file metadata request."""

        if path.is_dir():
            return WindowsFileMetadata(
                file_identity=None,
                logical_size=0,
                allocated_size=0,
                is_directory=True,
                is_reparse_point=False,
                is_compressed=False,
                is_sparse=False,
            )
        with self._lock:
            self.inspect_count += 1
        self.started.set()
        self.release.wait(timeout=2)
        return self.metadata_by_name[path.name]


class WindowsScanTests(unittest.IsolatedAsyncioTestCase):
    """Test NTFS metric aggregation and filesystem-based scanner selection."""

    def test_scandir_path_uses_extended_path_on_windows(self) -> None:
        path = Path("C:/long/tree")
        extended_path = "\\\\?\\C:\\long\\tree"

        with (
            patch("compsizer.os.name", "nt"),
            patch.object(
                WindowsFileApi,
                "_extended_path",
                return_value=extended_path,
            ) as extend_path,
        ):
            result = _scandir_path(path)

        self.assertEqual(result, extended_path)
        extend_path.assert_called_once_with(path)

    @staticmethod
    def _metadata(
        identity: tuple[int, bytes] | None,
        logical: int = 0,
        allocated: int = 0,
        *,
        directory: bool = False,
        reparse: bool = False,
        compressed: bool = False,
        sparse: bool = False,
    ) -> WindowsFileMetadata:
        """Build one fake file-information record."""

        return WindowsFileMetadata(
            file_identity=identity,
            logical_size=logical,
            allocated_size=allocated,
            is_directory=directory,
            is_reparse_point=reparse,
            is_compressed=compressed,
            is_sparse=sparse,
        )

    async def test_ntfs_scan_reports_size_compression_and_sparse_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            nested = root / "nested"
            nested.mkdir()
            junction = root / "junction"
            junction.mkdir()
            for path in (
                root / "compressed.bin",
                root / "compressed-hardlink.bin",
                root / "plain.bin",
                root / "sparse.bin",
                nested / "nested.bin",
                junction / "must-not-scan.bin",
            ):
                path.write_bytes(b"metadata fixture")

            shared_identity = (1, b"compressed-file-id")
            metadata = {
                "compressed.bin": self._metadata(
                    shared_identity,
                    1000,
                    300,
                    compressed=True,
                ),
                "compressed-hardlink.bin": self._metadata(
                    shared_identity,
                    1000,
                    300,
                    compressed=True,
                ),
                "plain.bin": self._metadata((1, b"plain-file-id"), 500, 512),
                "sparse.bin": self._metadata(
                    (1, b"sparse-file-id"),
                    10000,
                    4000,
                    sparse=True,
                ),
                "nested": self._metadata(
                    None,
                    directory=True,
                ),
                "nested.bin": self._metadata((1, b"nested-file-id"), 200, 256),
                "junction": self._metadata(
                    None,
                    directory=True,
                    reparse=True,
                ),
                "must-not-scan.bin": self._metadata((1, b"outside-file-id"), 900, 1024),
            }
            api = FakeWindowsFileApi("NTFS", metadata)
            runner = WindowsScanRunner(api)

            result = await runner.scan(root)

            self.assertIs(result.state, ScanState.COMPLETE)
            self.assertEqual(result.uncompressed_bytes, 11700)
            self.assertEqual(result.disk_usage_bytes, 5068)
            self.assertTrue(result.is_ntfs)
            self.assertEqual(result.ntfs_compressed_files, 1)
            self.assertEqual(result.ntfs_sparse_files, 1)
            self.assertAlmostEqual(result.ratio or 0.0, 5068 / 11700)
            self.assertIn("Skipped 1 reparse point", result.warning or "")
            self.assertNotIn("must-not-scan.bin", api.inspected_names)

            record = DirectoryRecord(DirectoryEntry(root, root.name), result, 0)
            row = DirectoryRow(record, BarScales.from_records([record]))
            self.assertIn("C", row.render().plain)
            self.assertIn("S", row.render().plain)
            self.assertIn("NTFS-compressed files: 1", str(row.tooltip))

            app = CompsizerApp(root, runner=runner)
            self.assertIn("Stored/Logical", app._column_header())
            self.assertIn("Logical Size", app._column_header())
            await runner.close()

    async def test_non_ntfs_volume_is_browsable_without_scan_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            child = root / "child"
            child.mkdir()
            api = FakeWindowsFileApi("exFAT", {})
            runner = WindowsScanRunner(api)

            result = await runner.scan(child)

            self.assertIs(result.state, ScanState.UNAVAILABLE)
            self.assertIsNone(result.disk_usage_bytes)
            self.assertIn("exFAT", result.warning or "")
            self.assertEqual(api.inspected_names, [])

            model = BrowserModel(root, ResultCache())
            view_id = model.begin_view(root)
            model.set_listing(
                view_id,
                DirectoryListing(root, (DirectoryEntry(child, child.name),)),
            )
            model.apply_scan_update(ScanJob(1, child, view_id, 0), result)
            self.assertEqual(model.state_counts[ScanState.UNAVAILABLE], 1)
            self.assertEqual(model.requests_for_missing_results(), [])

            record = DirectoryRecord(DirectoryEntry(child, child.name), result, 0)
            row = DirectoryRow(record, BarScales.from_records([record]))
            self.assertIn("—", row.render().plain)
            await runner.close()

    async def test_opened_volume_mount_root_is_scanned_on_its_own_volume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mount = root / "mounted-volume"
            mount.mkdir()
            (mount / "payload.bin").write_bytes(b"outside tree")
            api = FakeWindowsFileApi(
                "NTFS",
                {
                    "mounted-volume": self._metadata(
                        None,
                        directory=True,
                        reparse=True,
                    ),
                    "payload.bin": self._metadata((4, b"payload-id"), 12, 8),
                },
            )
            runner = WindowsScanRunner(api)

            result = await runner.scan(mount)

            self.assertIs(result.state, ScanState.COMPLETE)
            self.assertEqual(result.uncompressed_bytes, 12)
            self.assertEqual(result.disk_usage_bytes, 8)
            self.assertEqual(api.inspected_names, ["mounted-volume", "payload.bin"])
            await runner.close()

    async def test_ntfs_permission_errors_keep_partial_totals_and_continue(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "readable.bin").write_bytes(b"readable")
            (root / "denied.bin").write_bytes(b"denied")
            api = FakeWindowsFileApi(
                "NTFS",
                {
                    "readable.bin": self._metadata((2, b"readable-id"), 80, 64),
                },
                {"denied.bin"},
            )
            runner = WindowsScanRunner(api)

            result = await runner.scan(root)

            self.assertIs(result.state, ScanState.ERROR)
            self.assertTrue(result.has_statistics)
            self.assertEqual(result.disk_usage_bytes, 64)
            self.assertEqual(result.uncompressed_bytes, 80)
            self.assertIn("denied.bin", result.error or "")
            self.assertCountEqual(api.inspected_names, ["readable.bin", "denied.bin"])
            await runner.close()

    async def test_cancelled_thread_keeps_its_scan_slot_until_it_stops(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first_path = root / "first"
            second_path = root / "second"
            first_path.mkdir()
            second_path.mkdir()
            (first_path / "payload.bin").write_bytes(b"first")
            (second_path / "payload.bin").write_bytes(b"second")
            api = BlockingWindowsFileApi()
            runner = WindowsScanRunner(api, concurrency=1)
            first_scan = asyncio.create_task(runner.scan(first_path))
            second_scan: asyncio.Task[ScanResult] | None = None

            try:
                self.assertTrue(await asyncio.to_thread(api.started.wait, 1))
                first_scan.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first_scan

                second_scan = asyncio.create_task(runner.scan(second_path))
                await asyncio.sleep(0.05)
                self.assertEqual(api.inspect_count, 1)

                api.release.set()
                result = await asyncio.wait_for(second_scan, timeout=1)
                self.assertIs(result.state, ScanState.COMPLETE)
                self.assertEqual(api.inspect_count, 2)
            finally:
                api.release.set()
                for task in (first_scan, second_scan):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(
                    first_scan,
                    *(task for task in (second_scan,) if task is not None),
                    return_exceptions=True,
                )
                await runner.close()


@unittest.skipUnless(os.name == "nt", "Windows API integration test")
class WindowsApiTests(unittest.TestCase):
    """Smoke-test the native metadata wrapper on an NTFS volume."""

    def test_native_api_reads_ntfs_file_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            file_path = root / "metadata-only.txt"
            file_path.write_bytes(b"metadata")
            file_api = WindowsFileApi()
            if file_api.filesystem_type(root).casefold() != "ntfs":
                self.skipTest("The temporary directory is not on NTFS.")

            metadata = file_api.inspect_path(file_path)

            self.assertIsNotNone(metadata.file_identity)
            self.assertEqual(metadata.logical_size, len(b"metadata"))
            self.assertGreaterEqual(metadata.allocated_size, 0)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    """Test subprocess, fallback, and elevation behavior without Btrfs."""

    @staticmethod
    def _write_executable(path: Path, content: str) -> None:
        """Create a temporary executable for subprocess tests."""

        path.write_text(f"#!/bin/sh\n{content}", encoding="utf-8")
        path.chmod(0o755)

    def test_trusted_executable_lookup_rejects_external_paths_and_symlinks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            trusted = root / "trusted"
            trusted.mkdir()
            external = root / "external"
            self._write_executable(external, "exit 0\n")
            (trusted / "compsize").symlink_to(external)

            with patch("compsizer.SYSTEM_EXECUTABLE_PATH", os.fspath(trusted)):
                self.assertIsNone(CompsizeRunner._trusted_executable_path("compsize"))
                self.assertIsNone(
                    CompsizeRunner._trusted_executable_path(os.fspath(external))
                )

    async def test_sudo_startup_failures_do_not_request_authorization_again(
        self,
    ) -> None:
        for startup_error in (
            FileNotFoundError("not found"),
            PermissionError("permission denied"),
        ):
            with (
                self.subTest(error=type(startup_error).__name__),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                root = Path(temporary_directory)
                sudo = root / "sudo"
                compsize = root / "compsize"
                self._write_executable(sudo, "exit 0\n")
                self._write_executable(compsize, "exit 0\n")
                authorization_calls = 0

                async def authorize(_command: list[str]) -> bool:
                    nonlocal authorization_calls
                    authorization_calls += 1
                    return False

                runner = CompsizeRunner(
                    "compsize",
                    sudo_executable="sudo",
                    authorization_callback=authorize,
                    fallback_runner=FakeRunner(),
                )
                runner._running_as_root = False
                runner._sudo_enabled = True
                with (
                    patch("compsizer.SYSTEM_EXECUTABLE_PATH", os.fspath(root)),
                    patch(
                        "asyncio.create_subprocess_exec",
                        side_effect=startup_error,
                    ) as spawn,
                ):
                    result = await runner.scan(root / "target")

                self.assertIs(result.state, ScanState.ERROR)
                self.assertIn(os.fspath(sudo), result.error or "")
                self.assertEqual(authorization_calls, 0)
                self.assertIs(
                    spawn.await_args.kwargs["stdin"],
                    asyncio.subprocess.DEVNULL,
                )
                await runner.close()

    async def test_missing_executable_becomes_row_error(self) -> None:
        runner = CompsizeRunner("compsize-command-that-does-not-exist")

        result = await runner.scan(Path("/tmp/example"))

        self.assertIs(result.state, ScanState.ERROR)
        self.assertIn("not found", result.error or "")
        await runner.close()

    async def test_du_runner_returns_allocated_and_apparent_size_estimates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            du = root / "du"
            self._write_executable(
                du,
                'case " $* " in\n'
                '  *" --apparent-size "*) printf "1200\\tignored\\n" ;;\n'
                '  *) printf "700\\tignored\\n" ;;\n'
                "esac\n",
            )
            path = root / "directory with spaces"
            runner = DuRunner(str(du))

            result = await runner.scan(path)

            self.assertIs(result.state, ScanState.COMPLETE)
            self.assertEqual(result.disk_usage_bytes, 700)
            self.assertEqual(result.uncompressed_bytes, 1200)
            self.assertTrue(result.is_estimate)
            self.assertIsNone(result.ratio)
            self.assertIsNone(result.ratio_fraction)
            record = DirectoryRecord(DirectoryEntry(path, path.name), result, 0)
            row = DirectoryRow(record, BarScales.from_records([record]))
            self.assertIn("~700 B", row.render().plain)
            self.assertIn("~1.2 KiB", row.render().plain)
            await runner.close()

    async def test_du_runner_keeps_partial_totals_with_a_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            du = root / "du"
            self._write_executable(
                du,
                'case " $* " in\n'
                '  *" --apparent-size "*) printf "1200\\tignored\\n" ;;\n'
                '  *) printf "700\\tignored\\n"; '
                "printf 'du: permission denied\\n' >&2; exit 1 ;;\n"
                "esac\n",
            )
            runner = DuRunner(str(du))

            result = await runner.scan(root / "partial")

            self.assertIs(result.state, ScanState.COMPLETE)
            self.assertEqual(result.disk_usage_bytes, 700)
            self.assertEqual(result.uncompressed_bytes, 1200)
            self.assertIn("permission denied", result.warning or "")
            await runner.close()

    async def test_missing_du_returns_a_row_error(self) -> None:
        runner = DuRunner("du-command-that-does-not-exist")

        result = await runner.scan(Path("/tmp/example"))

        self.assertIs(result.state, ScanState.ERROR)
        self.assertIn("not found", result.error or "")
        await runner.close()

    async def test_permission_failure_uses_passwordless_sudo_after_consent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            compsize = root / "compsize"
            sudo = root / "sudo"
            self._write_executable(
                compsize,
                'if [ "${COMPSIZER_TEST_ELEVATED:-}" != 1 ]; then\n'
                "  printf '%s: SEARCH_V2: Operation not permitted\\n' \"$4\" >&2\n"
                "  exit 1\n"
                "fi\n"
                "printf 'TOTAL 50%% 50 100 100\\n'\n",
            )
            self._write_executable(
                sudo,
                '[ "$1" = -n ] || exit 88\n'
                "shift\n"
                '[ "$1" = -- ] || exit 88\n'
                "shift\n"
                'COMPSIZER_TEST_ELEVATED=1 exec "$@"\n',
            )
            authorization_calls = 0

            async def authorize(_command: list[str]) -> bool:
                nonlocal authorization_calls
                authorization_calls += 1
                return True

            runner = CompsizeRunner(
                str(compsize),
                sudo_executable=str(sudo),
                authorization_callback=authorize,
            )

            with patch("compsizer.SYSTEM_EXECUTABLE_PATH", os.fspath(root)):
                result = await runner.scan(root / "first")

            self.assertIs(result.state, ScanState.COMPLETE)
            self.assertEqual(authorization_calls, 1)
            await runner.close()

    async def test_permission_failure_prompts_once_for_concurrent_scans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            compsize = root / "compsize"
            sudo = root / "sudo"
            marker = root / "authorized"
            self._write_executable(
                compsize,
                'if [ "${COMPSIZER_TEST_ELEVATED:-}" != 1 ]; then\n'
                "  printf '%s: SEARCH_V2: Operation not permitted\\n' \"$4\" >&2\n"
                "  exit 1\n"
                "fi\n"
                "printf 'TOTAL 50%% 50 100 100\\n'\n",
            )
            self._write_executable(
                sudo,
                f'[ "$1" = -n ] || exit 88\n'
                "shift\n"
                '[ "$1" = -- ] || exit 88\n'
                "shift\n"
                f"[ -f {shlex.quote(os.fspath(marker))} ] || "
                "{ printf 'sudo: a password is required\\n' >&2; exit 1; }\n"
                'COMPSIZER_TEST_ELEVATED=1 exec "$@"\n',
            )
            authorization_calls = 0

            async def authorize(command: list[str]) -> bool:
                nonlocal authorization_calls
                authorization_calls += 1
                self.assertEqual(command[-1], "--help")
                await asyncio.sleep(0.01)
                marker.touch()
                return True

            runner = CompsizeRunner(
                str(compsize),
                sudo_executable=str(sudo),
                authorization_callback=authorize,
            )

            with patch("compsizer.SYSTEM_EXECUTABLE_PATH", os.fspath(root)):
                results = await asyncio.gather(
                    runner.scan(root / "first"),
                    runner.scan(root / "second"),
                )

            self.assertEqual(
                [result.state for result in results],
                [ScanState.COMPLETE, ScanState.COMPLETE],
            )
            self.assertEqual(authorization_calls, 1)
            await runner.close()

    async def test_permission_warning_makes_partial_report_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            compsize = root / "compsize"
            sudo = root / "sudo"
            self._write_executable(
                compsize,
                'if [ "${COMPSIZER_TEST_ELEVATED:-}" != 1 ]; then\n'
                "  printf 'TOTAL 50%% 50 100 100\\n'\n"
                "  printf '%s: Permission denied\\n' \"$4\" >&2\n"
                "  exit 0\n"
                "fi\n"
                "printf 'TOTAL 25%% 25 100 100\\n'\n",
            )
            self._write_executable(
                sudo,
                '[ "$1" = -n ] || exit 88\n'
                "shift\n"
                '[ "$1" = -- ] || exit 88\n'
                "shift\n"
                'COMPSIZER_TEST_ELEVATED=1 exec "$@"\n',
            )
            authorization_calls = 0

            async def authorize(_command: list[str]) -> bool:
                nonlocal authorization_calls
                authorization_calls += 1
                return True

            runner = CompsizeRunner(
                str(compsize),
                sudo_executable=str(sudo),
                authorization_callback=authorize,
            )

            with patch("compsizer.SYSTEM_EXECUTABLE_PATH", os.fspath(root)):
                result = await runner.scan(root / "partial")

            self.assertIs(result.state, ScanState.COMPLETE)
            self.assertEqual(result.disk_usage_bytes, 25)
            self.assertEqual(authorization_calls, 1)
            await runner.close()

    async def test_non_btrfs_failure_does_not_request_elevation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            compsize = root / "compsize"
            self._write_executable(
                compsize,
                "printf '%s: Not btrfs (or SEARCH_V2 unsupported).\\n' \"$4\" >&2\n"
                "exit 1\n",
            )
            authorization_calls = 0

            async def authorize(_command: list[str]) -> bool:
                nonlocal authorization_calls
                authorization_calls += 1
                return True

            runner = CompsizeRunner(
                str(compsize),
                authorization_callback=authorize,
            )

            with patch("compsizer.SYSTEM_EXECUTABLE_PATH", os.fspath(root)):
                result = await runner.scan(root / "not-btrfs")

            self.assertIs(result.state, ScanState.ERROR)
            self.assertIn("Not btrfs", result.error or "")
            self.assertEqual(authorization_calls, 0)
            await runner.close()

    async def test_declining_elevation_uses_du_without_repeated_prompts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            compsize = root / "compsize"
            sudo = root / "sudo"
            du = root / "du"
            compsize_calls = root / "compsize-calls"
            self._write_executable(
                compsize,
                f"printf x >> {shlex.quote(os.fspath(compsize_calls))}\n"
                "printf '%s: SEARCH_V2: Operation not permitted\\n' \"$4\" >&2\n"
                "exit 1\n",
            )
            self._write_executable(
                sudo,
                "printf 'sudo: a password is required\\n' >&2\nexit 1\n",
            )
            self._write_executable(
                du,
                'case " $* " in\n'
                '  *" --apparent-size "*) printf "1200\\tignored\\n" ;;\n'
                '  *) printf "700\\tignored\\n" ;;\n'
                "esac\n",
            )
            authorization_calls = 0

            async def decline(_command: list[str]) -> bool:
                nonlocal authorization_calls
                authorization_calls += 1
                return False

            runner = CompsizeRunner(
                str(compsize),
                sudo_executable=str(sudo),
                authorization_callback=decline,
                fallback_runner=DuRunner(str(du)),
            )

            with patch("compsizer.SYSTEM_EXECUTABLE_PATH", os.fspath(root)):
                first = await runner.scan(root / "first")
                second = await runner.scan(root / "second")

            self.assertIs(first.state, ScanState.COMPLETE)
            self.assertIs(second.state, ScanState.COMPLETE)
            self.assertEqual(first.disk_usage_bytes, 700)
            self.assertEqual(first.uncompressed_bytes, 1200)
            self.assertIn("declined", first.warning or "")
            self.assertIn("du estimates", second.warning or "")
            self.assertEqual(authorization_calls, 1)
            self.assertEqual(compsize_calls.read_text(encoding="utf-8"), "x")

            runner.reset_privilege_decision()
            with patch("compsizer.SYSTEM_EXECUTABLE_PATH", os.fspath(root)):
                await runner.scan(root / "third")
            self.assertEqual(authorization_calls, 2)
            self.assertEqual(compsize_calls.read_text(encoding="utf-8"), "xx")
            await runner.close()
