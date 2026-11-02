"""CLI file boundaries; generated timelines and temporary test paths only."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from jcc_realtime_probe import __main__ as cli
from jcc_realtime_probe.profile import PROJECT


class PrivateOutputTests(unittest.TestCase):
    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = cli.main(["synthetic", "--case", "static", *map(str, args)])
        return status, out.getvalue(), err.getvalue()

    def test_outputs_are_private_at_creation_with_permissive_umask(self):
        for mask in (0o022, 0):
            with self.subTest(umask=oct(mask)), tempfile.TemporaryDirectory() as temp:
                Path(temp).chmod(0o755)  # Traversable parent: the file mode must protect each output.
                events, report = Path(temp) / "events.jsonl", Path(temp) / "report.json"
                modes = []
                real_open, real_path_open = os.open, Path.open

                def inspect_open(path, flags, mode=0o777, *, dir_fd=None):
                    fd = real_open(path, flags, mode, dir_fd=dir_fd)
                    if flags & os.O_CREAT:
                        modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
                    return fd

                def inspect_path_open(path, *args, **kwargs):
                    file = real_path_open(path, *args, **kwargs)
                    mode = args[0] if args else kwargs.get("mode", "r")
                    if "x" in mode:
                        modes.append(stat.S_IMODE(os.fstat(file.fileno()).st_mode))
                    return file

                previous = os.umask(mask)
                try:
                    with patch("os.open", side_effect=inspect_open), \
                         patch.object(Path, "open", new=inspect_path_open):
                        status, _, err = self.invoke("--events", events, "--report", report)
                finally:
                    os.umask(previous)
                self.assertEqual((status, err), (0, ""))
                # Observed before fdopen/chmod or any timeline processing.
                self.assertEqual(modes, [0o600, 0o600])
                self.assertEqual(stat.S_IMODE(events.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
                rows = list(map(json.loads, events.read_text().splitlines()))
                self.assertTrue(any(r["status"] == "simulated_candidate" for r in rows))
                self.assertFalse(any(r["status"] == "candidate" for r in rows))
                self.assertEqual(json.loads(report.read_text())["version"], "realtime-probe-v2")

    def test_dangling_output_symlink_is_rejected_without_creating_its_target(self):
        for option in ("--events", "--report"):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as temp:
                link, target = Path(temp) / "output-link", Path(temp) / "missing-target"
                link.symlink_to(target)
                status, out, err = self.invoke(option, link)
                self.assertEqual(status, 1)
                self.assertEqual(out, "")
                self.assertTrue(link.is_symlink())
                self.assertFalse(target.exists())
                self.assertEqual(json.loads(err)["status"], "failed")
                self.assertNotIn(str(target), err)

    def test_existing_file_directory_and_symlink_are_never_overwritten_or_chmodded(self):
        for option in ("--events", "--report"):
            for kind in ("file", "directory", "symlink"):
                with self.subTest(option=option, kind=kind), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    target, output = root / "user-file", root / "output"
                    target.write_bytes(b"user-content-must-survive")
                    target.chmod(0o640)
                    if kind == "file":
                        output = target
                    elif kind == "directory":
                        output.mkdir(mode=0o750)
                    else:
                        output.symlink_to(target)
                    before = output.lstat()
                    status, out, _ = self.invoke(option, output)
                    self.assertEqual((status, out), (1, ""))
                    self.assertEqual(output.lstat().st_mode, before.st_mode)
                    self.assertEqual(target.read_bytes(), b"user-content-must-survive")
                    self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_parent_alias_uses_an_external_directory_but_rejects_project_alias(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory, alias = root / "directory", root / "alias"
            directory.mkdir()
            alias.symlink_to(directory, target_is_directory=True)
            status, _, err = self.invoke("--report", alias / "new-report.json")
            self.assertEqual((status, err), (0, ""))
            self.assertEqual(stat.S_IMODE((directory / "new-report.json").stat().st_mode), 0o600)
            self.assertTrue(alias.is_symlink())
            alias.unlink()
            alias.symlink_to(PROJECT, target_is_directory=True)
            with patch.object(cli, "run_timeline") as run:
                status, out, err = self.invoke("--report", alias / "10K-must-not-create.json")
            self.assertEqual((status, out), (1, ""))
            self.assertEqual(json.loads(err)["error_type"], "ValueError")
            run.assert_not_called()

    def test_second_output_failure_closes_first_and_keeps_only_its_private_partial(self):
        with tempfile.TemporaryDirectory() as temp:
            events, report = Path(temp) / "events", Path(temp) / "user-report"
            report.write_bytes(b"user-report")
            report.chmod(0o644)
            opened = []
            real_fdopen = os.fdopen

            def track_fdopen(*args, **kwargs):
                file = real_fdopen(*args, **kwargs)
                opened.append(file)
                return file

            with patch("os.fdopen", side_effect=track_fdopen), patch.object(cli, "run_timeline") as run:
                status, out, err = self.invoke("--events", events, "--report", report)
            self.assertEqual((status, out), (1, ""))
            self.assertEqual(json.loads(err)["error_type"], "FileExistsError")
            self.assertEqual(len(opened), 1)
            self.assertTrue(opened[0].closed)
            self.assertEqual(events.read_bytes(), b"")
            self.assertEqual(stat.S_IMODE(events.stat().st_mode), 0o600)
            self.assertEqual(report.read_bytes(), b"user-report")
            self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o644)
            run.assert_not_called()

    def test_processing_failure_closes_both_outputs_and_retains_private_partial_data(self):
        with tempfile.TemporaryDirectory() as temp:
            events, report = Path(temp) / "events", Path(temp) / "report"
            opened = []
            real_fdopen = os.fdopen

            def track_fdopen(*args, **kwargs):
                file = real_fdopen(*args, **kwargs)
                opened.append(file)
                return file

            def fail(*args, events_file, **kwargs):
                events_file.write('{"anonymous_partial":true}\n')
                raise RuntimeError("private-input-detail")

            with patch("os.fdopen", side_effect=track_fdopen), patch.object(cli, "run_timeline", side_effect=fail):
                status, out, err = self.invoke("--events", events, "--report", report)
            self.assertEqual((status, out), (1, ""))
            self.assertEqual(len(opened), 2)
            self.assertTrue(all(f.closed for f in opened))
            self.assertEqual(json.loads(events.read_text()), dict(anonymous_partial=True))
            self.assertEqual(report.read_bytes(), b"")
            self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in (events, report)))
            self.assertEqual(json.loads(err), dict(status="failed", error_type="RuntimeError"))

    def test_fdopen_failure_closes_the_raw_descriptor_and_retains_empty_private_file(self):
        with tempfile.TemporaryDirectory() as temp:
            output, descriptors = Path(temp) / "report", []

            def fail(fd, *args, **kwargs):
                descriptors.append(fd)
                raise OSError("private-fdopen-detail")

            with patch("os.fdopen", side_effect=fail):
                status, out, err = self.invoke("--report", output)
            self.assertEqual((status, out), (1, ""))
            self.assertEqual(json.loads(err), dict(status="failed", error_type="OSError"))
            self.assertEqual(len(descriptors), 1)
            with self.assertRaises(OSError):
                os.fstat(descriptors[0])
            self.assertEqual(output.read_bytes(), b"")
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_failure_cleanup_does_not_unlink_a_user_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "events"

            def replace_and_fail(*args, events_file, **kwargs):
                output.unlink()
                output.write_bytes(b"user-replacement")
                output.chmod(0o644)
                events_file.write("unlinked-partial-data")
                raise RuntimeError("private-failure")

            with patch.object(cli, "run_timeline", side_effect=replace_and_fail):
                status, out, _ = self.invoke("--events", output)
            self.assertEqual((status, out), (1, ""))
            self.assertEqual(output.read_bytes(), b"user-replacement")
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o644)

    def test_racing_file_creation_is_rejected_by_exclusive_open(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "report"
            real_open = os.open

            def create_first(path, flags, mode=0o777, *, dir_fd=None):
                if flags & os.O_CREAT:
                    output.write_bytes(b"racing-user-file")
                    output.chmod(0o640)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with patch("os.open", side_effect=create_first):
                status, out, _ = self.invoke("--report", output)
            self.assertEqual((status, out), (1, ""))
            self.assertEqual(output.read_bytes(), b"racing-user-file")
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o640)

    def test_close_error_closes_other_output_and_is_reported_before_success_stdout(self):
        with tempfile.TemporaryDirectory() as temp:
            events, report = Path(temp) / "events", Path(temp) / "report"
            files = []
            real_output = cli._new_private_output

            class BadClose:
                def __init__(self, file):
                    self.file = file

                def __enter__(self):
                    return self.file

                def __exit__(self, *args):
                    self.file.close()
                    raise OSError("private-close-detail")

            def open_output(path):
                file = real_output(path)
                files.append(file)
                return BadClose(file) if str(path) == str(report) else file

            with patch.object(cli, "_new_private_output", side_effect=open_output):
                status, out, err = self.invoke("--events", events, "--report", report)
            self.assertEqual((status, out), (1, ""))
            self.assertTrue(all(f.closed for f in files))
            self.assertEqual(json.loads(err), dict(status="failed", error_type="OSError"))


if __name__ == "__main__":
    unittest.main()
