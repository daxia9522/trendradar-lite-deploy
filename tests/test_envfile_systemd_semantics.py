"""Offline EnvironmentFile compatibility and private-file ownership regression tests."""

import ctypes
import glob
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
from envfile import EnvDocument, atomic_write, literal, private_backup


class EnvWhitespaceTests(unittest.TestCase):
    def parse(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env"
            path.write_text(content)
            return EnvDocument(path).values

    def test_escaped_trailing_space_is_significant(self):
        self.assertEqual(self.parse("X=secret\\ \n"), {"X": "secret "})
        self.assertEqual(self.parse("X=secret\\  \t\n"), {"X": "secret "})
        self.assertEqual(self.parse("X=secret\\\t\n"), {"X": "secret\t"})
        self.assertEqual(self.parse("X=secret \t\n"), {"X": "secret"})

    def test_unquoted_eof_escape_is_dropped(self):
        self.assertEqual(self.parse("X=secret\\"), {"X": "secret"})
        self.assertEqual(self.parse("X=secret\\ \\"), {"X": "secret "})

    def test_removing_escaped_secret_space_is_a_real_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env"
            path.write_text("EMAIL_PASSWORD=secret\\ \n")
            document = EnvDocument(path)
            self.assertTrue(document.save({"EMAIL_PASSWORD": "secret"}))
            self.assertEqual(EnvDocument(path).values["EMAIL_PASSWORD"], "secret")
            self.assertEqual(document.backup.read_text(), "EMAIL_PASSWORD=secret\\ \n")

    def test_root_setup_preserves_host_owner_for_atomic_file_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_bytes(b"old")
            uid, gid = path.stat().st_uid, path.stat().st_gid
            with mock.patch("envfile.os.geteuid", return_value=uid + 1), mock.patch(
                "envfile.os.fchown"
            ) as fchown, mock.patch("envfile.os.chown") as chown:
                atomic_write(path, b"new")
                backup = private_backup(path, b"old")
            self.assertEqual(fchown.call_count, 2)
            for call in fchown.call_args_list:
                self.assertEqual(call.args[1:], (uid, gid))
            chown.assert_called_once_with(backup.parent, uid, gid)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(backup.parent.stat().st_mode & 0o777, 0o700)

    def test_native_systemd_255_parser_oracle_offline(self):
        # Optional independent parser oracle. This loads a library only: no bus,
        # systemctl, service, socket, root privilege or production files involved.
        libraries = glob.glob("/usr/lib/*/systemd/libsystemd-shared-255.so")
        if not libraries:
            self.skipTest("systemd 255 shared parser unavailable")
        library = ctypes.CDLL(libraries[0])
        try:
            load, free = library.load_env_file, library.strv_free
        except AttributeError:
            self.skipTest("systemd parser symbols unavailable")
        pointer = ctypes.POINTER(ctypes.c_char_p)
        load.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(pointer)]
        load.restype = ctypes.c_int
        free.argtypes = [pointer]
        free.restype = ctypes.c_void_p
        fixtures = [
            "X=secret\\ \n", "X=secret\\", "X=secret\\\t\n", "X=secret \t\n",
            "X=secret\\  \t\n", "X=secret\\ \\\ncontinued\n",
            "X=" + literal(" space 'single' \"double\" \\ $HOME `literal` # end ") + "\n",
            "X='literal\\slash $HOME'\n", "X=one\\ two\n", "X=one\\\ntwo\n",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env"
            for fixture in fixtures:
                with self.subTest(fixture=fixture):
                    path.write_text(fixture)
                    output = pointer()
                    result = load(None, os.fsencode(path), ctypes.byref(output))
                    self.assertGreaterEqual(result, 0)
                    try:
                        expected, index = {}, 0
                        while output[index]:
                            key, value = output[index].decode().split("=", 1)
                            expected[key] = value
                            index += 1
                    finally:
                        free(output)
                    self.assertEqual(EnvDocument(path).values, expected)


if __name__ == "__main__":
    unittest.main()
