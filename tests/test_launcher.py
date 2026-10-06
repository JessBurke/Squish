"""Tests for the launcher (squish_app/__main__.py): messages when the window can't open."""

import builtins
import os
import shutil
import tempfile
import unittest
from unittest import mock

from squish_app import __main__ as launcher


def without_tkinter(name, *args, **kwargs):
    """An __import__ that behaves as if this Python had no tkinter."""
    if name == "tkinter" or name.startswith("tkinter."):
        raise ImportError("No module named 'tkinter'")
    return REAL_IMPORT(name, *args, **kwargs)


REAL_IMPORT = builtins.__import__


class LauncherMessageTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="squish-launcher-test-")
        patcher = mock.patch.dict(os.environ, {"SQUISH_DATA_DIR": self.data_dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.data_dir, True)

    def test_native_box_is_windows_only(self):
        with mock.patch.object(launcher.sys, "platform", "linux"):
            self.assertFalse(launcher._native_box("Squish", "text", True))

    def test_without_tkinter_the_windows_box_is_used(self):
        with mock.patch("builtins.__import__", side_effect=without_tkinter), \
                mock.patch.object(launcher, "_native_box", return_value=True) as box, \
                mock.patch.object(launcher, "say") as say:
            launcher.show_message("Squish", "Hello", error=True)
        box.assert_called_once_with("Squish", "Hello", True)
        self.assertFalse(say.called)

    def test_without_any_box_the_message_is_printed(self):
        with mock.patch("builtins.__import__", side_effect=without_tkinter), \
                mock.patch.object(launcher, "_native_box", return_value=False), \
                mock.patch.object(launcher, "say") as say:
            launcher.show_message("Squish", "Hello")
        self.assertIn("Hello", say.call_args[0][0])

    def test_missing_tkinter_gets_the_tcl_tk_advice(self):
        details = "Traceback...\nModuleNotFoundError: No module named 'tkinter'"
        with mock.patch.object(launcher, "show_message") as show, \
                mock.patch.object(launcher, "say"):
            launcher.report_crash(details)
        self.assertIn("tcl/tk", show.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
