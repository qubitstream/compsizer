import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from textual.widgets import ListView

from compsizer import (
    BrowserModel,
    CompsizeParseError,
    CompsizerApp,
    CompsizeRunner,
    DirectoryEntry,
    DirectoryListing,
    DirectoryRecord,
    EntryKind,
    InvalidInitialPathError,
    ResultCache,
    ScanJob,
    ScanManager,
    ScanRequest,
    ScanResult,
    ScanState,
    SortMode,
    enumerate_directories,
    normalize_initial_path,
    parse_compsize_output,
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

        for report in (empty_from_stderr, empty_from_stdout, empty_processed):
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


class FakeRunner:
    """Small asynchronous runner used to test bounded scheduling."""

    def __init__(self, delay: float = 0.01) -> None:
        self.delay = delay
        self.active = 0
        self.maximum_active = 0
        self.closed = False

    async def scan(self, path: Path) -> ScanResult:
        """Return a predictable result after a short asynchronous delay."""

        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            return ScanResult(path, ScanState.COMPLETE, 10, 100, 100)
        finally:
            self.active -= 1

    async def close(self) -> None:
        """Record manager shutdown."""

        self.closed = True


class AppTests(unittest.IsolatedAsyncioTestCase):
    """Exercise immediate UI loading with a fake compression runner."""

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
                directory_list = app.query_one("#directory-list", ListView)
                for _ in range(20):
                    if directory_list.index is not None:
                        break
                    await pilot.pause(0.01)
                self.assertIsNotNone(directory_list.index)
                await pilot.press("enter")
                await pilot.pause(0.01)
                self.assertEqual(app.model.current_path, root / "alpha")
                self.assertIn(
                    "nested",
                    {record.entry.name for record in app.model.records.values()},
                )
                await pilot.press("backspace")


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


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    """Test subprocess availability failures without requiring Btrfs."""

    async def test_missing_executable_becomes_row_error(self) -> None:
        runner = CompsizeRunner("compsize-command-that-does-not-exist")

        result = await runner.scan(Path("/tmp/example"))

        self.assertIs(result.state, ScanState.ERROR)
        self.assertIn("not found", result.error or "")
        await runner.close()
