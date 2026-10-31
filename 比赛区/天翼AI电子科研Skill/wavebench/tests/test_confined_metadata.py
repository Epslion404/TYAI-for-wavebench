import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from wavebench.data import confined_metadata
from wavebench.data.confined_metadata import read_confined_metadata
from wavebench.data.packages import load_capture_package
from wavebench.errors import ConfigError


class ConfinedMetadataTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.raw = self.root / "data" / "raw"
        self.package = self.raw / "capture"
        self.package.mkdir(parents=True)
        self.metadata = self.package / "metadata.json"
        self.text = '{"operation": {}, "waveform": {"summary": {"channel": 1}}}'
        self.metadata.write_text(self.text, encoding="utf-8")
        self.marker = "SYNTHETIC_OUTSIDE_METADATA_MARKER"

    def _symlink(self, link, target, *, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as exc:
            self.skipTest(f"real symlink creation unavailable: {exc}")

    def _target(self, sibling=False):
        target_dir = self.raw / "sibling" if sibling else self.root / "outside"
        target_dir.mkdir()
        target = target_dir / "metadata.json"
        target.write_text(self.marker, encoding="utf-8")
        return target

    def _assert_denied_before_read(self):
        with patch.object(confined_metadata.os, "fdopen") as fdopen:
            with self.assertRaises(ConfigError) as caught:
                read_confined_metadata(self.package, self.raw)
        fdopen.assert_not_called()
        self.assertNotIn(self.marker, str(caught.exception))
        self.assertNotIn("outside", str(caught.exception))

    def test_regular_metadata_is_read_and_loaded(self):
        self.assertEqual(read_confined_metadata(self.package, self.raw), self.text)
        package = load_capture_package(self.package, allowed_root=self.raw)
        self.assertEqual(package.metadata["waveform"]["summary"]["channel"], 1)

    def test_metadata_symlink_outside_raw_is_rejected(self):
        target = self._target()
        self.metadata.unlink()
        self._symlink(self.metadata, target)
        self._assert_denied_before_read()

    def test_metadata_symlink_to_sibling_package_is_rejected(self):
        target = self._target(sibling=True)
        self.metadata.unlink()
        self._symlink(self.metadata, target)
        self._assert_denied_before_read()

    def _run_replacement_race(self, *, parent, sibling=False):
        target = self._target(sibling=sibling)
        # Probe permissions before changing the original entry; never substitute a hardlink.
        probe = self.root / "symlink-probe"
        self._symlink(probe, target.parent if parent else target, directory=parent)
        probe.unlink()
        original_lstat = Path.lstat
        captured = self.metadata.lstat()
        replaced = False

        def replace_after_lstat(path, *args, **kwargs):
            nonlocal replaced
            if path == self.metadata and not replaced:
                replaced = True
                if parent:
                    self.package.rename(self.raw / "original-capture")
                    self._symlink(self.package, target.parent, directory=True)
                else:
                    self.metadata.unlink()
                    self._symlink(self.metadata, target)
                return captured
            return original_lstat(path, *args, **kwargs)

        with patch.object(Path, "lstat", replace_after_lstat):
            self._assert_denied_before_read()
        self.assertTrue(replaced, "race must occur between the metadata check and real open")

    def test_metadata_replaced_by_outside_symlink_after_lstat(self):
        self._run_replacement_race(parent=False)

    def test_metadata_replaced_by_sibling_symlink_after_lstat(self):
        self._run_replacement_race(parent=False, sibling=True)

    def test_parent_replaced_by_outside_directory_symlink_after_lstat(self):
        self._run_replacement_race(parent=True)

    def test_parent_replaced_by_sibling_directory_symlink_after_lstat(self):
        self._run_replacement_race(parent=True, sibling=True)

    def test_missing_metadata_is_config_error(self):
        self.metadata.unlink()
        self._assert_denied_before_read()

    def test_directory_metadata_is_config_error(self):
        self.metadata.unlink()
        self.metadata.mkdir()
        self._assert_denied_before_read()

    def test_package_outside_allowed_root_is_rejected(self):
        with patch.object(confined_metadata.os, "fdopen") as fdopen:
            with self.assertRaises(ConfigError):
                read_confined_metadata(self.package, self.root / "different-root")
        fdopen.assert_not_called()

    def test_loader_rejects_unnormalized_parent_traversal(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "metadata.json").write_text(self.text, encoding="utf-8")
        with patch.object(confined_metadata.os, "fdopen") as fdopen:
            with self.assertRaises(ConfigError):
                load_capture_package(self.raw / ".." / ".." / "outside", allowed_root=self.raw)
        fdopen.assert_not_called()

    def test_malformed_regular_metadata_is_config_error(self):
        for text, message in (("{broken", "not valid JSON"), ("[]", "JSON object")):
            with self.subTest(text=text):
                self.metadata.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(ConfigError, message):
                    load_capture_package(self.package, allowed_root=self.raw)

    def test_decode_failure_closes_open_descriptor(self):
        self.metadata.write_bytes(b"\xff")
        original_fdopen = os.fdopen
        descriptors = []

        def track_fdopen(fd, *args, **kwargs):
            descriptors.append(fd)
            return original_fdopen(fd, *args, **kwargs)

        with patch.object(confined_metadata.os, "fdopen", track_fdopen):
            with self.assertRaises(ConfigError):
                read_confined_metadata(self.package, self.raw)
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])

    @unittest.skipUnless(os.name == "posix", "POSIX no-follow descriptor walk")
    def test_posix_open_rejects_file_and_parent_symlinks_without_lstat(self):
        target = self._target()
        self.metadata.unlink()
        self._symlink(self.metadata, target)
        with self.subTest(entry="metadata"):
            with self.assertRaises(OSError):
                confined_metadata._open_posix_metadata(self.metadata)
        self.metadata.unlink()
        self.package.rmdir()
        self._symlink(self.package, target.parent, directory=True)
        with self.subTest(entry="parent"):
            with self.assertRaises(OSError):
                confined_metadata._open_posix_metadata(self.metadata)

    @unittest.skipUnless(os.name == "nt", "Windows handle verification")
    def test_windows_handle_is_verified_before_read_and_closed_on_denial(self):
        original_handle_path = confined_metadata._windows_handle_path
        descriptors = []

        def mismatched_handle_path(fd):
            descriptors.append(fd)
            self.assertEqual(original_handle_path(fd), self.metadata)
            return self.root / "outside" / "metadata.json"

        with patch.object(confined_metadata, "_windows_handle_path", mismatched_handle_path):
            self._assert_denied_before_read()
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])


if __name__ == "__main__":
    unittest.main()
