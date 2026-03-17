"""
Unit tests for tools/remote.py.

Uses monkeypatch of _ssh_run and subprocess.run — no SSH connection required.
Run with: python3 tests/test_remote.py
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mktemp(suffix=".sqlite")
os.environ["HUB_DB_PATH"] = _tmp

from hub.bootstrap import ensure_ready
ensure_ready()

import tools.remote as remote_mod
from tools.remote import (
    _KNOWN_HOSTS,
    remote_exec,
    remote_explore_project,
    remote_list_dir,
    remote_read_file,
    remote_write_file,
)

_OK = {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}
_SSH_ERR = {"ok": False, "error": "Connection refused"}


def _ssh_ok(stdout=""):
    return {**_OK, "stdout": stdout}


class TestCheckHost(unittest.TestCase):
    def test_unknown_host_rejected(self):
        r = remote_exec("evil-host", "ls")
        self.assertFalse(r["ok"])
        self.assertIn("unknown host_alias", r["error"])

    def test_known_host_passes_check(self):
        host = next(iter(_KNOWN_HOSTS))
        with patch.object(remote_mod, "_ssh_run", return_value=_ssh_ok("ok\n")):
            r = remote_exec(host, "echo ok")
        self.assertTrue(r["ok"])


class TestRemoteExec(unittest.TestCase):
    HOST = "maincua-prod"

    def test_plain_command(self):
        with patch.object(remote_mod, "_ssh_run", return_value=_ssh_ok("hello\n")) as m:
            r = remote_exec(self.HOST, "echo hello")
        self.assertTrue(r["ok"])
        self.assertEqual(r["stdout"], "hello\n")
        m.assert_called_once_with(self.HOST, "echo hello", 30)

    def test_with_working_dir(self):
        with patch.object(remote_mod, "_ssh_run", return_value=_ssh_ok()) as m:
            remote_exec(self.HOST, "ls", working_dir="/tmp")
        cmd_used = m.call_args[0][1]
        self.assertIn("cd /tmp", cmd_used)
        self.assertIn("ls", cmd_used)

    def test_propagates_ssh_error(self):
        with patch.object(remote_mod, "_ssh_run", return_value=_SSH_ERR):
            r = remote_exec(self.HOST, "ls")
        self.assertFalse(r["ok"])


class TestRemoteReadFile(unittest.TestCase):
    HOST = "maincua-prod"

    def test_reads_file(self):
        with patch.object(remote_mod, "_ssh_run", return_value=_ssh_ok("line1\nline2\n")):
            r = remote_read_file(self.HOST, "/tmp/f.txt")
        self.assertTrue(r["ok"])
        self.assertEqual(r["lines"], 2)
        self.assertIn("line1", r["content"])

    def test_max_lines_uses_head(self):
        with patch.object(remote_mod, "_ssh_run", return_value=_ssh_ok("x\n")) as m:
            remote_read_file(self.HOST, "/tmp/f.txt", max_lines=5)
        cmd = m.call_args[0][1]
        self.assertIn("head -n 5", cmd)

    def test_ssh_failure_propagated(self):
        with patch.object(remote_mod, "_ssh_run", return_value=_SSH_ERR):
            r = remote_read_file(self.HOST, "/tmp/f.txt")
        self.assertFalse(r["ok"])


class TestRemoteWriteFile(unittest.TestCase):
    HOST = "maincua-prod"

    def test_unknown_host_rejected_before_subprocess(self):
        with patch("subprocess.run") as mock_sp:
            r = remote_write_file("evil-host", "/tmp/x.txt", "data")
        self.assertFalse(r["ok"])
        mock_sp.assert_not_called()

    def test_write_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = b""
        with patch("subprocess.run", return_value=mock_result) as mock_sp:
            r = remote_write_file(self.HOST, "/tmp/x.txt", "hello")
        self.assertTrue(r["ok"])
        self.assertEqual(r["bytes_written"], 5)
        # Verify BatchMode=yes is in the SSH call
        call_args = mock_sp.call_args[0][0]
        self.assertIn("BatchMode=yes", call_args)

    def test_content_size_cap(self):
        big = "x" * (512 * 1024 + 1)
        with patch("subprocess.run") as mock_sp:
            r = remote_write_file(self.HOST, "/tmp/x.txt", big)
        self.assertFalse(r["ok"])
        self.assertIn("KB limit", r["error"])
        mock_sp.assert_not_called()

    def test_ssh_failure_returns_error(self):
        mock_result = MagicMock()
        mock_result.returncode = 255
        mock_result.stderr = b"Connection refused"
        with patch("subprocess.run", return_value=mock_result):
            r = remote_write_file(self.HOST, "/tmp/x.txt", "data")
        self.assertFalse(r["ok"])
        self.assertIn("Connection refused", r["error"])


class TestRemoteListDir(unittest.TestCase):
    HOST = "maincua-prod"

    _FIND_OUTPUT = (
        "d 4096 subdir\n"
        "f 1234 normal.txt\n"
        "f 567 file with spaces.txt\n"
        "l 0 symlink\n"
    )

    def test_parses_entries_correctly(self):
        with patch.object(remote_mod, "_ssh_run", return_value=_ssh_ok(self._FIND_OUTPUT)):
            r = remote_list_dir(self.HOST, "/home/rdios")
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 4)
        names = [e["name"] for e in r["entries"]]
        self.assertIn("file with spaces.txt", names)  # key: spaces preserved
        self.assertIn("symlink", names)

    def test_types_classified(self):
        with patch.object(remote_mod, "_ssh_run", return_value=_ssh_ok(self._FIND_OUTPUT)):
            r = remote_list_dir(self.HOST, "/home/rdios")
        types = {e["name"]: e["type"] for e in r["entries"]}
        self.assertEqual(types["subdir"], "dir")
        self.assertEqual(types["normal.txt"], "file")
        self.assertEqual(types["symlink"], "link")

    def test_hidden_filter_in_command(self):
        with patch.object(remote_mod, "_ssh_run", return_value=_ssh_ok()) as m:
            remote_list_dir(self.HOST, "/tmp", show_hidden=False)
        cmd = m.call_args[0][1]
        self.assertIn("-not -name '.*'", cmd)

    def test_show_hidden_omits_filter(self):
        with patch.object(remote_mod, "_ssh_run", return_value=_ssh_ok()) as m:
            remote_list_dir(self.HOST, "/tmp", show_hidden=True)
        cmd = m.call_args[0][1]
        self.assertNotIn("-not -name", cmd)

    def test_ssh_failure_propagated(self):
        with patch.object(remote_mod, "_ssh_run", return_value=_SSH_ERR):
            r = remote_list_dir(self.HOST, "/tmp")
        self.assertFalse(r["ok"])

    def test_nonexistent_dir_returns_false(self):
        # find exits with code 1 when path doesn't exist — must propagate
        _not_found = {"ok": False, "exit_code": 1, "stdout": "", "stderr": "No such file or directory"}
        with patch.object(remote_mod, "_ssh_run", return_value=_not_found):
            r = remote_list_dir(self.HOST, "/nonexistent")
        self.assertFalse(r["ok"])

    def test_no_stderr_in_stdout(self):
        # Verify 2>&1 is NOT in the generated command
        with patch.object(remote_mod, "_ssh_run", return_value=_ssh_ok()) as m:
            remote_list_dir(self.HOST, "/tmp")
        cmd = m.call_args[0][1]
        self.assertNotIn("2>&1", cmd)


class TestRemoteExploreProject(unittest.TestCase):
    HOST = "maincua-prod"

    def _fake_ssh_reachable(self, host, cmd, timeout):
        if cmd == "true":
            return _ssh_ok()
        if cmd.startswith("test -d"):
            return _ssh_ok()   # path exists by default
        if "find" in cmd:
            return _ssh_ok("/home/rdios/apps/proj\n/home/rdios/apps/proj/src\n")
        if "git log" in cmd:
            return _ssh_ok("abc1234 initial commit\n")
        if "git status" in cmd:
            return _ssh_ok("")
        if "README" in cmd:
            return _ssh_ok("# My Project\n")
        return _ssh_ok("")

    def test_unknown_host_rejected(self):
        with patch("subprocess.run") as mock_sp:
            r = remote_explore_project("evil-host", "/home/rdios/apps/myproject")
        self.assertFalse(r["ok"])
        mock_sp.assert_not_called()

    def test_unreachable_host_returns_false(self):
        with patch.object(remote_mod, "_ssh_run", return_value=_SSH_ERR):
            r = remote_explore_project(self.HOST, "/home/rdios/apps/myproject")
        self.assertFalse(r["ok"])
        self.assertIn("unreachable", r["error"])

    def test_success_returns_ok_true_with_path_exists(self):
        with patch.object(remote_mod, "_ssh_run", side_effect=self._fake_ssh_reachable):
            r = remote_explore_project(self.HOST, "/home/rdios/apps/proj")
        self.assertTrue(r["ok"])
        self.assertTrue(r["path_exists"])
        self.assertIn("initial commit", r["git"]["log"])
        self.assertIn("# My Project", r["readme"])
        self.assertNotIn("warnings", r)

    def test_invalid_path_ok_true_path_exists_false(self):
        # Host reachable, but project_path doesn't exist.
        # Contract: ok=True (connectivity fine), path_exists=False, warnings populated.
        def fake_ssh(host, cmd, timeout):
            if cmd == "true":
                return _ssh_ok()
            # test -d fails for non-existent path
            return {"ok": False, "exit_code": 1, "stdout": "", "stderr": ""}

        with patch.object(remote_mod, "_ssh_run", side_effect=fake_ssh):
            r = remote_explore_project(self.HOST, "/home/rdios/apps/nonexistent")
        self.assertTrue(r["ok"])
        self.assertFalse(r["path_exists"])
        self.assertEqual(r["structure"], "")
        self.assertTrue(len(r["warnings"]) > 0)
        self.assertIn("nonexistent", r["warnings"][0])

    def test_partial_failure_adds_warnings(self):
        # Host reachable, path exists, but structure fails in normal _ssh_run format:
        # ok=False, exit_code, stderr — no "error" key. Warnings must still be populated.
        def fake_ssh(host, cmd, timeout):
            if cmd == "true":
                return _ssh_ok()
            if cmd.startswith("test -d"):
                return _ssh_ok()
            if "find" in cmd:
                return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "Permission denied"}
            return _ssh_ok("")

        with patch.object(remote_mod, "_ssh_run", side_effect=fake_ssh):
            r = remote_explore_project(self.HOST, "/home/rdios/apps/proj")
        self.assertTrue(r["ok"])
        self.assertTrue(r["path_exists"])
        self.assertIn("warnings", r)
        self.assertTrue(any("structure" in w for w in r["warnings"]))
        # detail should use stderr, not error key
        self.assertTrue(any("Permission denied" in w for w in r["warnings"]))

    def test_partial_failure_exit_code_fallback(self):
        # Worst case: ok=False, no error key, no stderr — warning uses exit_code
        def fake_ssh(host, cmd, timeout):
            if cmd == "true":
                return _ssh_ok()
            if cmd.startswith("test -d"):
                return _ssh_ok()
            if "find" in cmd:
                return {"ok": False, "exit_code": 2, "stdout": "", "stderr": ""}
            return _ssh_ok("")

        with patch.object(remote_mod, "_ssh_run", side_effect=fake_ssh):
            r = remote_explore_project(self.HOST, "/home/rdios/apps/proj")
        self.assertIn("warnings", r)
        self.assertTrue(any("exit_code=2" in w for w in r["warnings"]))


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total = result.testsRun
    failures = len(result.failures) + len(result.errors)
    print(f"\n  {total - failures}/{total} tests passed")
    sys.exit(0 if failures == 0 else 1)
