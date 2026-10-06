"""Tests for shortcut.py.

Shortcuts can only really be made on Windows, so these tests check the pure
parts: which program the shortcut starts, the values handed to PowerShell /
VBScript (with awkward folder names), and that the scripts never have those
values pasted into them.
"""

import ntpath
import os
import subprocess
import unittest
from unittest import mock

from squish_app import paths, shortcut

NASTY_DIRS = [
    r"C:\Users\Sam O'Brien\Documents\Squish App",
    r"C:\Users\x\OneDrive - Acme & Partners (AU)\Documents\Squish (v1) [test]",
    "C:\\Users\\Zo\u00eb M\u00fcller\\Documents\\\u30b9\u30af\u30a3\u30c3\u30b7\u30e5 $env;%PATH%",
    r"\\fileserver\share$\Team; Tools\Squish `tick` {braces}",
    r"H:\Projects\123-SYD\123.0001 Big Project - Stage 1 - Design & Construct",
]


def exists_in(*present):
    """An ``exists`` function that only knows about the given paths (case-insensitive)."""
    known = set(p.lower() for p in present)
    return lambda path: path.lower() in known


class WindowlessPythonTests(unittest.TestCase):
    def test_python_exe_to_pythonw(self):
        exe = r"C:\Users\sam\AppData\Local\Programs\Python\Python312\python.exe"
        want = r"C:\Users\sam\AppData\Local\Programs\Python\Python312\pythonw.exe"
        self.assertEqual(shortcut.windowless_python(exe, exists_in(want)), want)

    def test_keeps_case_of_name(self):
        exe = r"C:\Python38\Python.EXE"
        want = r"C:\Python38\Pythonw.EXE"
        self.assertEqual(shortcut.windowless_python(exe, exists_in(want)), want)

    def test_versioned_name(self):
        exe = r"C:\Tools\python3.11.exe"
        want = r"C:\Tools\pythonw3.11.exe"
        self.assertEqual(shortcut.windowless_python(exe, exists_in(want)), want)

    def test_virtual_environment(self):
        exe = r"D:\Squish App (2)\.venv\Scripts\python.exe"
        want = r"D:\Squish App (2)\.venv\Scripts\pythonw.exe"
        self.assertEqual(shortcut.windowless_python(exe, exists_in(want)), want)

    def test_already_windowless(self):
        exe = r"C:\Python312\pythonw.exe"
        self.assertEqual(shortcut.windowless_python(exe, exists_in()), exe)

    def test_falls_back_to_python_exe(self):
        exe = r"C:\Embedded\python.exe"
        self.assertEqual(shortcut.windowless_python(exe, exists_in()), exe)

    def test_py_launcher(self):
        exe = r"C:\Windows\py.exe"
        want = r"C:\Windows\pyw.exe"
        self.assertEqual(shortcut.windowless_python(exe, exists_in(want)), want)

    def test_store_python_alias_folder(self):
        folder = (r"C:\Users\sam\AppData\Local\Microsoft\WindowsApps"
                  r"\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0")
        exe = folder + r"\python.exe"
        self.assertEqual(shortcut.windowless_python(exe, exists_in(folder + r"\pythonw.exe")),
                         folder + r"\pythonw.exe")

    def test_store_python_program_files_uses_alias(self):
        exe = (r"C:\Program Files\WindowsApps"
               r"\PythonSoftwareFoundation.Python.3.12_3.12.2800.0_x64__qbz5n2kfra8p0\python3.12.exe")
        local = r"C:\Users\sam\AppData\Local"
        alias = (local + r"\Microsoft\WindowsApps"
                 r"\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\pythonw.exe")
        result = shortcut.windowless_python(
            exe, exists_in(alias, ntpath.join(ntpath.dirname(exe), "pythonw3.12.exe")),
            localappdata=local)
        self.assertEqual(result, alias)

    def test_store_python_versioned_alias(self):
        exe = (r"C:\Program Files\WindowsApps"
               r"\PythonSoftwareFoundation.Python.3.11_3.11.2544.0_x64__qbz5n2kfra8p0\python.exe")
        local = r"C:\Users\sam\AppData\Local"
        alias = local + r"\Microsoft\WindowsApps\pythonw3.11.exe"
        self.assertEqual(shortcut.windowless_python(exe, exists_in(alias), localappdata=local), alias)

    def test_store_pythonw_program_files_uses_alias(self):
        exe = (r"C:\Program Files\WindowsApps"
               r"\PythonSoftwareFoundation.Python.3.12_3.12.1008.0_x64__qbz5n2kfra8p0\pythonw.exe")
        local = r"C:\Users\x\AppData\Local"
        alias = (local + r"\Microsoft\WindowsApps"
                 r"\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\pythonw.exe")
        self.assertEqual(shortcut.windowless_python(exe, exists_in(alias), localappdata=local), alias)
        # no alias found: nothing better to offer
        self.assertEqual(shortcut.windowless_python(exe, exists_in(), localappdata=local), exe)

    def test_ordinary_pythonw_is_unchanged(self):
        local = r"C:\Users\x\AppData\Local"
        for exe in (r"C:\Python312\pythonw.exe", r"D:\Squish\.venv\Scripts\pythonw.exe",
                    r"C:\Windows\pyw.exe"):
            self.assertEqual(shortcut.windowless_python(exe, lambda p: True, localappdata=local),
                             exe)

    def test_empty(self):
        self.assertEqual(shortcut.windowless_python("", exists_in()), "")


class ShortcutValuesTests(unittest.TestCase):
    def test_values(self):
        app = r"C:\Users\sam\Documents\Squish App"
        exe = r"C:\Python312\python.exe"
        values = shortcut.shortcut_values(
            "Desktop", app_dir=app, executable=exe, frozen=False,
            exists=exists_in(r"C:\Python312\pythonw.exe", app + r"\assets\squish.ico"))
        self.assertEqual(sorted(values),
                         sorted(shortcut.ENV_NAMES + shortcut.APP_ID_ENV_NAMES))
        self.assertEqual(values["SQUISH_SC_FOLDER"], "Desktop")
        self.assertEqual(values["SQUISH_SC_NAME"], "Squish.lnk")
        self.assertEqual(values["SQUISH_SC_TARGET"], r"C:\Python312\pythonw.exe")
        self.assertEqual(values["SQUISH_SC_ARGS"], '"%s\\Squish.pyw"' % app)
        self.assertEqual(values["SQUISH_SC_WORKDIR"], app)
        self.assertEqual(values["SQUISH_SC_ICON"], app + r"\assets\squish.ico,0")
        self.assertEqual(values["SQUISH_SC_DESC"], "Squish - email digests for Claude")
        self.assertEqual(values["SQUISH_SC_APPID"], paths.APP_ID)
        self.assertEqual(values["SQUISH_SC_CS"], shortcut.APP_ID_CSHARP)

    def test_missing_icon_is_left_blank(self):
        values = shortcut.shortcut_values("Programs", app_dir=r"C:\S", executable=r"C:\P\python.exe",
                                          frozen=False, exists=exists_in())
        self.assertEqual(values["SQUISH_SC_ICON"], "")
        self.assertEqual(values["SQUISH_SC_FOLDER"], "Programs")

    def test_frozen_app_runs_itself(self):
        values = shortcut.shortcut_values("Desktop", app_dir=r"C:\S", executable=r"C:\S\Squish.exe",
                                          frozen=True, exists=exists_in())
        self.assertEqual(values["SQUISH_SC_TARGET"], r"C:\S\Squish.exe")
        self.assertEqual(values["SQUISH_SC_ARGS"], "")

    def test_nasty_folders_are_passed_through_unchanged(self):
        for app in NASTY_DIRS:
            values = shortcut.shortcut_values("Desktop", app_dir=app, executable=app + r"\py\python.exe",
                                              frozen=False, exists=lambda p: True)
            self.assertEqual(values["SQUISH_SC_WORKDIR"], app)
            self.assertEqual(values["SQUISH_SC_ARGS"], '"' + app + '\\Squish.pyw"')
            self.assertEqual(values["SQUISH_SC_TARGET"], app + r"\py\pythonw.exe")
            self.assertEqual(values["SQUISH_SC_ICON"], app + r"\assets\squish.ico,0")
            env = shortcut.child_environment(values, base={"PATH": "x"})
            self.assertEqual(env["PATH"], "x")
            for key, value in values.items():
                self.assertEqual(env[key], value)

    def test_quote_argument(self):
        self.assertEqual(shortcut.quote_argument(r"C:\a b\c.pyw"), r'"C:\a b\c.pyw"')
        self.assertEqual(shortcut.quote_argument('C:\\odd"name'), '"C:\\oddname"')


class ScriptTests(unittest.TestCase):
    def test_powershell_script_uses_environment_only(self):
        script = shortcut.POWERSHELL_SCRIPT
        for name in shortcut.ENV_NAMES:
            self.assertIn("$env:" + name, script)
        for needle in ("GetFolderPath", "WScript.Shell", "CreateShortcut", ".Save()", "exit 1"):
            self.assertIn(needle, script)
        # one line and no double quotes, so it survives being passed as one argument
        self.assertNotIn("\n", script)
        self.assertNotIn('"', script)
        self.assertNotIn("\\", script)

    def test_powershell_sets_app_id_quietly(self):
        script = shortcut.POWERSHELL_SCRIPT
        for name in shortcut.APP_ID_ENV_NAMES:
            self.assertIn("$env:" + name, script)
        start = script.index("Add-Type")
        # inside its own try/catch, after the shortcut is saved, before OK is written
        self.assertLess(script.index("$sc.Save()"), start)
        self.assertLess(start, script.index("Write-Output ('OK "))
        self.assertIn("catch { }", script[start:])
        self.assertIn("[SquishShortcutId]::SetAppId($lnk, $env:SQUISH_SC_APPID)", script)

    def test_app_id_helper_source(self):
        source = shortcut.APP_ID_CSHARP
        source.encode("ascii")
        self.assertEqual(source.count("{"), source.count("}"))
        self.assertEqual(source.count("("), source.count(")"))
        self.assertIn("public static class SquishShortcutId", source)
        self.assertIn("public static void SetAppId(string lnkPath, string appId)", source)
        # System.AppUserModel.ID and the interfaces it needs
        self.assertIn("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3", source)
        self.assertIn("key.PropertyId = 5;", source)
        self.assertIn("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99", source)   # IPropertyStore
        self.assertIn("00021401-0000-0000-C000-000000000046", source)   # ShellLink
        self.assertNotIn(paths.APP_ID, source)      # the id itself comes from SQUISH_SC_APPID

    def test_powershell_command(self):
        cmd = shortcut.powershell_command("powershell.exe")
        self.assertEqual(cmd[:6], ["powershell.exe", "-NoProfile", "-NonInteractive",
                                   "-ExecutionPolicy", "Bypass", "-Command"])
        self.assertEqual(cmd[6], shortcut.POWERSHELL_SCRIPT)
        self.assertEqual(len(cmd), 7)
        # Windows command line: the script is wrapped in quotes and otherwise untouched
        line = subprocess.list2cmdline(cmd)
        self.assertTrue(line.endswith('"' + shortcut.POWERSHELL_SCRIPT + '"'))

    def test_powershell_command_never_contains_values(self):
        for app in NASTY_DIRS:
            values = shortcut.shortcut_values("Desktop", app_dir=app, executable=r"C:\P\python.exe",
                                              frozen=False, exists=lambda p: True)
            line = subprocess.list2cmdline(shortcut.powershell_command("powershell.exe"))
            for value in values.values():
                if value not in ("Desktop", "Squish.lnk"):
                    self.assertNotIn(value, line)

    def test_vbs_script(self):
        script = shortcut.VBS_SCRIPT
        script.encode("ascii")  # plain ASCII so cscript reads it on any code page
        for name in shortcut.ENV_NAMES:
            self.assertIn('env("%s")' % name, script)
        self.assertIn("SpecialFolders", script)
        self.assertIn("WScript.Quit 1", script)
        self.assertIn("\r\n", script)
        self.assertNotIn("Squish.pyw", script)

    def test_cscript_command(self):
        path = r"C:\Users\Sam O'Brien\AppData\Local\Temp\squish-shortcut-x.vbs"
        cmd = shortcut.cscript_command(path, "cscript.exe")
        self.assertEqual(cmd[0], "cscript.exe")
        self.assertEqual(cmd[1], "//nologo")
        self.assertEqual(cmd[-1], path)

    def test_parse_output(self):
        self.assertEqual(shortcut.parse_output("OK C:\\Users\\x\\Desktop\\Squish.lnk\r\n"),
                         (True, "C:\\Users\\x\\Desktop\\Squish.lnk"))
        self.assertEqual(shortcut.parse_output("noise\nFAILED Access is denied.\n"),
                         (False, "Access is denied."))
        self.assertEqual(shortcut.parse_output(""), (False, "no response"))
        self.assertEqual(shortcut.parse_output("something odd"), (False, "something odd"))


class CreateShortcutTests(unittest.TestCase):
    def test_not_windows(self):
        with mock.patch.object(shortcut, "is_windows", return_value=False):
            self.assertEqual(shortcut.create_desktop_shortcut(),
                             (False, "Desktop shortcuts are only created on Windows"))
            self.assertEqual(shortcut.create_start_menu_shortcut()[0], False)
            self.assertEqual(shortcut.create_all_shortcuts(),
                             [(False, "Desktop shortcuts are only created on Windows")])

    def _fake_run(self, outcomes, calls):
        def run(command, env):
            calls.append((command, env))
            return outcomes.pop(0)
        return run

    def test_powershell_success(self):
        calls = []
        run = self._fake_run([(True, r"C:\Users\x\OneDrive\Desktop\Squish.lnk")], calls)
        with mock.patch.object(shortcut, "is_windows", return_value=True), \
                mock.patch.object(shortcut, "_run", side_effect=run):
            ok, message = shortcut.create_desktop_shortcut(app_dir=NASTY_DIRS[0])
        self.assertTrue(ok)
        self.assertIn(r"OneDrive\Desktop\Squish.lnk", message)
        self.assertEqual(len(calls), 1)
        command, env = calls[0]
        self.assertIn("-Command", command)
        self.assertEqual(env["SQUISH_SC_FOLDER"], "Desktop")
        self.assertEqual(env["SQUISH_SC_WORKDIR"], NASTY_DIRS[0])

    def test_falls_back_to_vbscript(self):
        calls = []
        run = self._fake_run([(False, "Cannot create type. Only core types are supported"),
                              (True, r"C:\Users\x\AppData\Roaming\Microsoft\Windows\Start Menu"
                                     r"\Programs\Squish.lnk")], calls)
        with mock.patch.object(shortcut, "is_windows", return_value=True), \
                mock.patch.object(shortcut, "_run", side_effect=run):
            ok, message = shortcut.create_start_menu_shortcut(app_dir=NASTY_DIRS[1])
        self.assertTrue(ok)
        self.assertTrue(message.startswith("Start Menu shortcut created"))
        self.assertEqual(len(calls), 2)
        vbs_command, env = calls[1]
        self.assertTrue(vbs_command[-1].endswith(".vbs"))
        self.assertFalse(os.path.exists(vbs_command[-1]), "temporary script should be removed")
        self.assertEqual(env["SQUISH_SC_FOLDER"], "Programs")

    def test_both_fail_gives_clear_message(self):
        calls = []
        run = self._fake_run([(False, "blocked by policy"), (False, "it could not be started")], calls)
        with mock.patch.object(shortcut, "is_windows", return_value=True), \
                mock.patch.object(shortcut, "_run", side_effect=run):
            ok, message = shortcut.create_desktop_shortcut(app_dir=r"C:\S")
        self.assertFalse(ok)
        self.assertIn("blocked by policy", message)
        self.assertIn("Send to > Desktop (create shortcut)", message)

    def test_all_shortcuts_on_windows(self):
        with mock.patch.object(shortcut, "is_windows", return_value=True), \
                mock.patch.object(shortcut, "_run", return_value=(True, "X:\\Squish.lnk")):
            results = shortcut.create_all_shortcuts(app_dir=r"C:\S")
        self.assertEqual([ok for ok, _ in results], [True, True])
        self.assertTrue(results[0][1].startswith("Desktop"))
        self.assertTrue(results[1][1].startswith("Start Menu"))


if __name__ == "__main__":
    unittest.main()
