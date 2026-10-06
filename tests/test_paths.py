"""Tests for paths.py: file and folder names, and where Squish keeps its own files."""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from squish_app import paths


class SafeFilenameTests(unittest.TestCase):

    def test_bad_characters_and_spaces(self):
        self.assertEqual(paths.safe_filename('Depot: Stage 1 / "North"?'), "Depot Stage 1 North")
        self.assertEqual(paths.safe_filename("  Bridge Upgrade.  "), "Bridge Upgrade")

    def test_no_trailing_space_or_dot_after_the_cut(self):
        base = "x" * 76 + " and"            # character 80 is the space before "Pump"
        for tail in (" Pump Station Works", ". Pump Station Works"):
            name = paths.safe_filename(base + tail)
            self.assertLessEqual(len(name), 80)
            self.assertFalse(name.endswith((" ", ".")), repr(name))
        name = paths.safe_filename("y" * 79 + ". Station Works")
        self.assertFalse(name.endswith((" ", ".")), repr(name))

    def test_windows_device_names(self):
        self.assertEqual(paths.safe_filename("CON"), "CON_")
        self.assertEqual(paths.safe_filename("aux"), "aux_")
        self.assertEqual(paths.safe_filename("com1"), "com1_")
        self.assertEqual(paths.safe_filename("Console"), "Console")
        self.assertEqual(paths.safe_filename("Con Ed site"), "Con Ed site")
        self.assertEqual(paths.safe_filename("com10"), "com10")

    def test_device_name_before_a_dot(self):
        # Windows treats "CON.anything" as the device too: the "_" goes right
        # after the device name, not at the end.
        self.assertEqual(paths.safe_filename("nul.txt"), "nul_.txt")
        self.assertEqual(paths.safe_filename("Con.Ltd"), "Con_.Ltd")
        self.assertEqual(paths.safe_filename("AUX. Building"), "AUX_. Building")
        self.assertEqual(paths.safe_filename("nul.2024"), "nul_.2024")
        self.assertEqual(paths.safe_filename("Con .Ltd"), "Con_ .Ltd")

    def test_com0_lpt0_and_superscript_digits(self):
        self.assertEqual(paths.safe_filename("COM0"), "COM0_")
        self.assertEqual(paths.safe_filename("lpt0.x"), "lpt0_.x")
        self.assertEqual(paths.safe_filename("COM\u00b9"), "COM\u00b9_")
        self.assertEqual(paths.safe_filename("LPT\u00b3.log"), "LPT\u00b3_.log")

    def test_empty_gives_fallback(self):
        self.assertEqual(paths.safe_filename(""), "project")
        self.assertEqual(paths.safe_filename(None, "Project"), "Project")
        self.assertEqual(paths.safe_filename(" ... ", "Project"), "Project")

    def test_safe_names_are_unchanged(self):
        for name in ("Riverside Depot", "6012 Depot - Stage 2", "A" * 80):
            self.assertEqual(paths.safe_filename(name), name)


class OutputNameTests(unittest.TestCase):

    def test_short_names_are_the_safe_file_name(self):
        self.assertEqual(paths.output_name("Bridge: Stage 1"), "Bridge Stage 1")
        self.assertEqual(paths.output_name(""), "Project")

    def test_long_names_are_cut_and_given_a_code(self):
        a = "Riverside Depot Upgrade Stage 2 Civil and Structural Works"
        b = "Riverside Depot Upgrade Stage 2 Civil and Structural Review"
        short_a, short_b = paths.output_name(a), paths.output_name(b)
        self.assertRegex(short_a, r"^Riverside Depot Upgrade Stage 2 Civil and ~[0-9a-f]{6}$")
        self.assertLessEqual(len(short_a), paths.OUTPUT_NAME_MAX + 8)
        self.assertNotEqual(short_a, short_b)          # same start, different files
        self.assertEqual(paths.output_name(a.upper())[-6:], short_a[-6:])   # case doesn't matter
        self.assertEqual(paths.output_name(a), short_a)                       # always the same

    def test_default_output_folder_uses_the_short_name(self):
        name = "x" * 70
        self.assertEqual(paths.default_output_folder(name).name, paths.output_name(name))


class FolderTextTests(unittest.TestCase):

    def test_clean_folder_text(self):
        self.assertEqual(paths.clean_folder_text('"C:\\Projects\\Emails"'), "C:\\Projects\\Emails")
        self.assertEqual(paths.clean_folder_text(' "C:\\x" '), "C:\\x")
        self.assertEqual(paths.clean_folder_text("H:\\Jobs\\01 Emails"), "H:\\Jobs\\01 Emails")
        self.assertEqual(paths.clean_folder_text('H:\\Jobs\\Emails"'), "H:\\Jobs\\Emails")
        self.assertEqual(paths.clean_folder_text(""), "")
        self.assertEqual(paths.clean_folder_text(None), "")

    def test_pasted_file_links_become_folder_paths(self):
        clean = paths.clean_folder_text
        self.assertEqual(clean("file:///H:/Jobs/6012%20Depot/01%20Emails"),
                         "H:\\Jobs\\6012 Depot\\01 Emails")
        self.assertEqual(clean(" <file:///H:/Jobs/01%20Emails/> "), "H:\\Jobs\\01 Emails\\")
        self.assertEqual(clean('"FILE:///C:/x"'), "C:\\x")
        self.assertEqual(clean("file:///C|/x"), "C:\\x")
        self.assertEqual(clean("file://H:/Jobs"), "H:\\Jobs")
        self.assertEqual(clean("file://localhost/C:/Jobs"), "C:\\Jobs")
        self.assertEqual(clean("file://server/share/Jobs/R%26D%20(2025)"),
                         "\\\\server\\share\\Jobs\\R&D (2025)")
        self.assertEqual(clean("file:////server/share/Jobs"), "\\\\server\\share\\Jobs")
        self.assertEqual(clean("file:///\\\\server\\share\\Jobs"), "\\\\server\\share\\Jobs")
        self.assertEqual(clean("file:///home/sam/mail%20box"), "/home/sam/mail box")
        self.assertEqual(clean("file:/home/sam"), "/home/sam")
        self.assertEqual(clean("file:///H:/Jobs/caf%C3%A9"), "H:\\Jobs\\caf\u00e9")
        # Not links: left alone.
        self.assertEqual(clean("H:\\Jobs\\file: notes"), "H:\\Jobs\\file: notes")
        self.assertEqual(clean("<H:\\x>"), "<H:\\x>")

    def test_default_name_for_folder(self):
        name = paths.default_name_for_folder
        self.assertEqual(name("/x/Riverside Depot/01 Emails"), "Riverside Depot - 01 Emails")
        self.assertEqual(name("H:\\Jobs\\6012 Depot\\Correspondence\\"), "6012 Depot - Correspondence")
        self.assertEqual(name("/x/Harbour Bridge"), "Harbour Bridge")
        self.assertEqual(name("/x/Harbour Bridge/"), "Harbour Bridge")
        self.assertEqual(name("H:\\Emails"), "Emails")       # no useful parent
        self.assertEqual(name(""), "Emails")


class DataFolderTests(unittest.TestCase):

    def test_windows_cache_is_local_and_projects_roam(self):
        env = {"APPDATA": "C:\\Users\\sam\\AppData\\Roaming",
               "LOCALAPPDATA": "C:\\Users\\sam\\AppData\\Local"}
        roaming = paths.app_folder(False, env, "nt", "win32", "C:\\Users\\sam")
        local = paths.app_folder(True, env, "nt", "win32", "C:\\Users\\sam")
        self.assertTrue(roaming.startswith(env["APPDATA"]))
        self.assertTrue(local.startswith(env["LOCALAPPDATA"]))
        self.assertTrue(local.endswith("Squish"))
        # LOCALAPPDATA missing: the usual place under the home folder.
        local = paths.app_folder(True, {}, "nt", "win32", "C:\\Users\\sam")
        self.assertIn("Local", local)

    def test_other_platforms_use_one_folder(self):
        for platform in ("darwin", "linux"):
            self.assertEqual(paths.app_folder(True, {}, "posix", platform, "/home/sam"),
                             paths.app_folder(False, {}, "posix", platform, "/home/sam"))

    def test_override_keeps_everything_together(self):
        tmp = tempfile.mkdtemp(prefix="squish-paths-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.dict(os.environ, {"SQUISH_DATA_DIR": tmp}):
            for folder in (paths.data_dir(), paths.local_data_dir(), paths.cache_dir(),
                           paths.logs_dir(), paths.projects_file().parent):
                self.assertTrue(str(folder).startswith(tmp), folder)
            self.assertTrue(os.path.isdir(str(paths.cache_dir())))
        self.assertEqual(paths.app_folder(True, {"SQUISH_DATA_DIR": tmp}, "nt", "win32", "C:\\"), tmp)


if __name__ == "__main__":
    unittest.main()
