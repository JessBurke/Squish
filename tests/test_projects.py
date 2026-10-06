"""Tests for projects.py (the saved project list)."""

import builtins
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from squish_app import paths, projects


class ProjectsTestCase(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="squish-test-")
        patcher = mock.patch.dict(os.environ, {"SQUISH_DATA_DIR": self.data_dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.data_dir, True)
        self.path = str(paths.projects_file())

    def write_file(self, text):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(text)


class NewProjectTests(ProjectsTestCase):
    def test_defaults(self):
        p = projects.new_project("Harbour Bridge")
        self.assertEqual(p["name"], "Harbour Bridge")
        self.assertEqual(len(p["id"]), 32)
        self.assertEqual(p["source_folder"], "")
        self.assertTrue(p["include_subfolders"])
        self.assertEqual(p["output_folder"], "")
        self.assertEqual((p["date_from"], p["date_to"]), ("", ""))
        self.assertEqual(p["squeeze"], "standard")
        self.assertEqual(p["part_size"], "medium")
        self.assertEqual(p["focus_keywords"], "")
        self.assertEqual(p["org_codes"], "slrconsulting.com=SLR")
        self.assertTrue(p["drop_noise"])
        self.assertTrue(p["recover_quoted"])
        self.assertIsNone(p["last_run"])

    def test_ids_are_unique(self):
        ids = set(projects.new_project("x")["id"] for _ in range(50))
        self.assertEqual(len(ids), 50)

    def test_blank_name_gets_default(self):
        self.assertEqual(projects.new_project("   ")["name"], projects.DEFAULT_NAME)

    def test_settings_are_independent(self):
        a = projects.new_project("A")
        b = projects.new_project("B")
        a["org_codes"] = "example.com=EX"
        self.assertEqual(b["org_codes"], "slrconsulting.com=SLR")


class LoadSaveTests(ProjectsTestCase):
    def test_missing_file_is_empty_list(self):
        self.assertEqual(projects.load_projects(), [])

    def test_round_trip(self):
        a = projects.new_project("Road Upgrade")
        a["source_folder"] = r"H:\Projects\Road Upgrade\01 Emails"
        a["focus_keywords"] = "culvert, pavement"
        a["last_run"] = {"finished_at": "2026-01-02 03:04", "files": []}
        b = projects.new_project("Pump Station (Stage 2) - O'Neill & Co \u00e9t\u00e9")
        projects.save_projects([a, b])
        loaded = projects.load_projects()
        self.assertEqual(loaded, [a, b])

    def test_file_is_utf8_json_with_version(self):
        projects.save_projects([projects.new_project("Caf\u00e9 \u2013 fit-out")])
        with open(self.path, "rb") as fh:
            raw = fh.read()
        data = json.loads(raw.decode("utf-8"))
        self.assertEqual(data["version"], projects.FILE_VERSION)
        self.assertEqual(data["projects"][0]["name"], "Caf\u00e9 \u2013 fit-out")

    def test_save_leaves_no_temp_files(self):
        projects.save_projects([projects.new_project("A")])
        projects.save_projects([projects.new_project("B")])
        self.assertEqual(sorted(os.listdir(self.data_dir)), ["projects.json"])

    def test_save_replaces_previous_list(self):
        projects.save_projects([projects.new_project("A"), projects.new_project("B")])
        projects.save_projects([projects.new_project("C")])
        self.assertEqual([p["name"] for p in projects.load_projects()], ["C"])

    def test_failed_write_keeps_old_file(self):
        projects.save_projects([projects.new_project("Keep me")])
        with mock.patch("squish_app.projects.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                projects.save_projects([projects.new_project("New")])
        self.assertEqual([p["name"] for p in projects.load_projects()], ["Keep me"])
        self.assertEqual(sorted(os.listdir(self.data_dir)), ["projects.json"])

    def test_missing_keys_are_filled(self):
        self.write_file(json.dumps({"projects": [{"id": "abc", "name": "Old",
                                                  "source_folder": "C:\\mail"}]}))
        (p,) = projects.load_projects()
        self.assertEqual(p["id"], "abc")
        self.assertEqual(p["source_folder"], "C:\\mail")
        self.assertEqual(p["squeeze"], "standard")
        self.assertEqual(p["part_size"], "medium")
        self.assertTrue(p["recover_quoted"])
        self.assertIsNone(p["last_run"])

    def test_bare_list_file_is_accepted(self):
        self.write_file(json.dumps([{"id": "x1", "name": "Listed"}]))
        self.assertEqual([p["name"] for p in projects.load_projects()], ["Listed"])

    def test_bad_values_are_corrected(self):
        self.write_file(json.dumps({"projects": [{
            "name": None, "squeeze": "extreme", "part_size": 12, "drop_noise": 0,
            "include_subfolders": None, "last_run": "yesterday", "focus_keywords": None}]}))
        (p,) = projects.load_projects()
        self.assertEqual(p["name"], projects.DEFAULT_NAME)
        self.assertEqual(len(p["id"]), 32)
        self.assertEqual(p["squeeze"], "standard")
        self.assertEqual(p["part_size"], "medium")
        self.assertIs(p["drop_noise"], False)
        self.assertIs(p["include_subfolders"], True)
        self.assertIsNone(p["last_run"])
        self.assertEqual(p["focus_keywords"], "")

    def test_unknown_keys_are_kept(self):
        self.write_file(json.dumps({"projects": [{"id": "k", "name": "N", "future_setting": [1, 2]}]}))
        (p,) = projects.load_projects()
        self.assertEqual(p["future_setting"], [1, 2])
        projects.save_projects([p])
        self.assertEqual(projects.load_projects()[0]["future_setting"], [1, 2])

    def test_non_dict_entries_skipped_and_repeated_ids_fixed(self):
        self.write_file(json.dumps({"projects": [
            {"id": "same", "name": "One"}, "junk", 5, {"id": "same", "name": "Two"}]}))
        loaded = projects.load_projects()
        self.assertEqual([p["name"] for p in loaded], ["One", "Two"])
        self.assertEqual(loaded[0]["id"], "same")
        self.assertNotEqual(loaded[1]["id"], "same")

    def test_empty_file_is_empty_list(self):
        self.write_file("   \n")
        self.assertEqual(projects.load_projects(), [])

    def test_utf8_bom_is_accepted(self):
        with open(self.path, "wb") as fh:
            fh.write(b"\xef\xbb\xbf" + json.dumps([{"id": "b", "name": "Bom"}]).encode("utf-8"))
        self.assertEqual(projects.load_projects()[0]["name"], "Bom")


class CorruptFileTests(ProjectsTestCase):
    def test_corrupt_file_is_backed_up(self):
        self.write_file('{"projects": [{"name": "half writ')
        self.assertEqual(projects.load_projects(), [])
        self.assertFalse(os.path.exists(self.path))
        backup = self.path + ".bad-1"
        with open(backup, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), '{"projects": [{"name": "half writ')

    def test_second_corrupt_file_gets_next_number(self):
        self.write_file("not json")
        projects.load_projects()
        self.write_file("[still not json")
        projects.load_projects()
        self.assertTrue(os.path.exists(self.path + ".bad-1"))
        self.assertTrue(os.path.exists(self.path + ".bad-2"))

    def test_wrong_shape_is_treated_as_corrupt(self):
        self.write_file(json.dumps({"projects": "oops"}))
        self.assertEqual(projects.load_projects(), [])
        self.assertTrue(os.path.exists(self.path + ".bad-1"))

    def test_not_utf8_is_treated_as_corrupt(self):
        with open(self.path, "wb") as fh:
            fh.write(b"\xff\xfe\x00garbage\x9c")
        self.assertEqual(projects.load_projects(), [])
        self.assertTrue(os.path.exists(self.path + ".bad-1"))

    def test_can_save_after_corrupt_load(self):
        self.write_file("{{{")
        self.assertEqual(projects.load_projects(), [])
        projects.save_projects([projects.new_project("Fresh start")])
        self.assertEqual([p["name"] for p in projects.load_projects()], ["Fresh start"])
        self.assertTrue(os.path.exists(self.path + ".bad-1"))


class DuplicateTests(ProjectsTestCase):
    def test_duplicate(self):
        original = projects.new_project("Bridge")
        original["source_folder"] = "D:\\mail"
        original["org_codes"] = "example.com=EX"
        original["last_run"] = {"finished_at": "2026-01-01 10:00", "files": [{"path": "x"}]}
        copy = projects.duplicate_project(original)
        self.assertEqual(copy["name"], "Bridge (copy)")
        self.assertNotEqual(copy["id"], original["id"])
        self.assertIsNone(copy["last_run"])
        self.assertEqual(copy["source_folder"], "D:\\mail")
        self.assertEqual(copy["org_codes"], "example.com=EX")
        # the original is untouched
        self.assertEqual(original["name"], "Bridge")
        self.assertIsNotNone(original["last_run"])

    def test_duplicate_is_a_deep_copy(self):
        original = projects.new_project("Deep")
        original["extra"] = {"list": [1]}
        copy = projects.duplicate_project(original)
        copy["extra"]["list"].append(2)
        self.assertEqual(original["extra"]["list"], [1])

    def test_duplicate_name_is_unique_when_names_given(self):
        original = projects.new_project("Bridge")
        names = ["Bridge", "Bridge (copy)", "bridge (copy 2)"]
        self.assertEqual(projects.duplicate_project(original, names)["name"], "Bridge (copy 3)")

    def test_unique_name(self):
        self.assertEqual(projects.unique_name("New project", []), "New project")
        self.assertEqual(projects.unique_name("New project", ["new project"]), "New project 2")
        self.assertEqual(projects.unique_name("New project", ["New project", "New project 2"]),
                         "New project 3")

    def test_long_name_is_shortened_so_the_copy_gets_its_own_files(self):
        base = "Riverside Depot Stormwater Upgrade Stage 2 Detailed Design and Construction Pha"
        original = projects.new_project(base + "se Services")      # 91 characters
        copy = projects.duplicate_project(original, [original["name"]])
        self.assertTrue(copy["name"].endswith(" (copy)"))
        self.assertLessEqual(len(copy["name"]), projects.COPY_BASE_MAX + len(" (copy)"))
        self.assertNotEqual(paths.safe_filename(copy["name"]),
                            paths.safe_filename(original["name"]))
        self.assertFalse(projects.same_file_name(copy["name"], original["name"]))

    def test_find_project(self):
        a, b = projects.new_project("A"), projects.new_project("B")
        self.assertIs(projects.find_project([a, b], b["id"]), b)
        self.assertIsNone(projects.find_project([a, b], "nope"))


class NormaliseTests(ProjectsTestCase):
    def test_folder_quotes_are_removed(self):
        p = projects.normalise_project({"id": "q", "name": "Q",
                                        "source_folder": '"H:\\Jobs\\01 Emails"',
                                        "output_folder": ' "D:\\Out" '})
        self.assertEqual(p["source_folder"], "H:\\Jobs\\01 Emails")
        self.assertEqual(p["output_folder"], "D:\\Out")


class SameFileNameTests(unittest.TestCase):
    def test_names_that_share_files(self):
        long_a = "Harbour Road Upgrade " * 4 + "Stage 1 detailed design"    # 85+ characters
        long_b = "Harbour Road Upgrade " * 4 + "Stage 2 construction phase"
        self.assertGreater(len(long_a), 85)
        for a, b in (("Bridge: Stage 1", "Bridge Stage 1"), ("Job/A", "job a"),
                     ("Job A.", "Job A"), ("Same", "same "), (long_a, long_b)):
            self.assertTrue(projects.same_file_name(a, b), (a, b))

    def test_names_that_do_not(self):
        for a, b in (("Bridge", "Bridge - B"), ("Job A", "Job B"), ("Stage 1", "Stage 2")):
            self.assertFalse(projects.same_file_name(a, b), (a, b))


class LoadReportTests(ProjectsTestCase):
    """A file that can't be opened for a moment is not the same as a damaged file."""

    def setUp(self):
        ProjectsTestCase.setUp(self)
        sleep = mock.patch("squish_app.projects.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def open_failing(self, times):
        """An open() that raises PermissionError for projects.json ``times`` times."""
        real_open = builtins.open
        calls = {"n": 0}

        def fake_open(file, *args, **kwargs):
            if str(file) == self.path and calls["n"] < times:
                calls["n"] += 1
                raise PermissionError(13, "The process cannot access the file")
            return real_open(file, *args, **kwargs)
        return mock.patch("builtins.open", side_effect=fake_open)

    def test_missing_file_is_no_problem(self):
        self.assertEqual(projects.load_projects_report(), ([], None))

    def test_one_failed_open_is_retried(self):
        projects.save_projects([projects.new_project("A"), projects.new_project("B")])
        with self.open_failing(1):
            items, problem = projects.load_projects_report()
        self.assertIsNone(problem)
        self.assertEqual([p["name"] for p in items], ["A", "B"])
        self.assertEqual(sorted(os.listdir(self.data_dir)), ["projects.json"])

    def test_file_that_stays_locked_is_left_alone(self):
        projects.save_projects([projects.new_project("Keep")])
        with open(self.path, "rb") as fh:
            before = fh.read()
        with self.open_failing(99):
            items, problem = projects.load_projects_report()
            self.assertEqual(projects.load_projects(), [])
        self.assertEqual(items, [])
        self.assertEqual(problem["kind"], "unreadable")
        self.assertEqual(problem["path"], self.path)
        self.assertIsNone(problem["backup"])
        self.assertTrue(problem["error"])
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual(sorted(os.listdir(self.data_dir)), ["projects.json"])

    def test_damaged_file_is_set_aside(self):
        self.write_file("{not json")
        items, problem = projects.load_projects_report()
        self.assertEqual(items, [])
        self.assertEqual(problem["kind"], "damaged")
        self.assertTrue(problem["backup"].endswith("projects.json.bad-1"))
        self.assertTrue(os.path.exists(problem["backup"]))

    def test_damaged_file_that_cannot_be_set_aside_counts_as_unreadable(self):
        self.write_file("{not json")
        with mock.patch.object(projects, "_backup_bad_file", return_value=None):
            items, problem = projects.load_projects_report()
        self.assertEqual(items, [])
        self.assertEqual(problem["kind"], "unreadable")
        self.assertTrue(os.path.exists(self.path))


class WindowLockTests(ProjectsTestCase):
    def take(self):
        lock = projects.take_window_lock()
        if lock is not None:
            self.addCleanup(lock.release)
        return lock

    def test_second_lock_is_refused_until_the_first_is_released(self):
        first = self.take()
        self.assertIsNotNone(first)
        self.assertIsNotNone(first.fd)
        self.assertIsNone(self.take())
        first.release()
        first.release()        # harmless twice
        self.assertIsNotNone(self.take())

    def test_leftover_lock_file_does_not_block(self):
        self.take().release()
        self.assertTrue(os.path.exists(os.path.join(self.data_dir, projects.WINDOW_LOCK_NAME)))
        self.assertIsNotNone(self.take())

    def test_another_process_cannot_take_it(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(projects.__file__)))
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from squish_app import projects; "
                "print(projects.take_window_lock() is None)")
        env = dict(os.environ, SQUISH_DATA_DIR=self.data_dir)

        def child_refused():
            out = subprocess.run([sys.executable, "-c", code, here], env=env, timeout=60,
                                 stdout=subprocess.PIPE, universal_newlines=True)
            return out.stdout.strip()

        lock = self.take()
        self.assertEqual(child_refused(), "True")
        lock.release()
        self.assertEqual(child_refused(), "False")

    def test_lock_file_that_cannot_be_made_does_not_stop_squish(self):
        with mock.patch("squish_app.projects.os.open", side_effect=PermissionError(13, "denied")):
            lock = projects.take_window_lock()
        self.assertIsNotNone(lock)
        self.assertIsNone(lock.fd)
        lock.release()


class LastRunTests(ProjectsTestCase):
    """last_run_from_result / record_last_run (runs made without the window)."""

    RESULT = {"finished_at": "2026-01-02 10:00", "elapsed_s": 2.5, "output_folder": "/out",
              "log_path": "/logs/A - last run.txt", "files_found": 3, "files_read": 3,
              "from_cache": 0, "stats": {"emails_used": 3},
              "files": [{"path": "/out/Squish - A - undated.txt", "est_tokens": 9}],
              "failed": [["/x.msg", "bad"]], "cancelled": False}

    def test_last_run_leaves_out_the_failed_list(self):
        saved = projects.last_run_from_result(self.RESULT)
        self.assertEqual(saved["failed_count"], 1)
        self.assertNotIn("failed", saved)
        self.assertNotIn("cancelled", saved)
        self.assertEqual(saved["files"], self.RESULT["files"])
        saved["files"][0]["path"] = "changed"           # a copy, not the result itself
        self.assertEqual(self.RESULT["files"][0]["path"], "/out/Squish - A - undated.txt")

    def test_record_last_run_saves_it(self):
        a, b = projects.new_project("A"), projects.new_project("B")
        projects.save_projects([a, b])
        self.assertEqual(projects.record_last_run(a["id"], self.RESULT), (True, ""))
        items = projects.load_projects()
        self.assertEqual(items[0]["last_run"]["files"], self.RESULT["files"])
        self.assertIsNone(items[1]["last_run"])
        self.assertIsNotNone(projects.take_window_lock())   # the lock was given back

    def test_record_last_run_skips_a_run_without_files(self):
        a = projects.new_project("A")
        projects.save_projects([a])
        result = dict(self.RESULT, files=[])
        self.assertEqual(projects.record_last_run(a["id"], result), (False, ""))
        self.assertIsNone(projects.load_projects()[0]["last_run"])

    def test_record_last_run_leaves_an_open_window_alone(self):
        a = projects.new_project("A")
        projects.save_projects([a])
        lock = projects.take_window_lock()     # a Squish window is open
        try:
            saved, note = projects.record_last_run(a["id"], self.RESULT)
        finally:
            lock.release()
        self.assertFalse(saved)
        self.assertIn("window", note)
        self.assertIsNone(projects.load_projects()[0]["last_run"])

    def test_record_last_run_never_saves_over_a_damaged_list(self):
        self.write_file("{not json")
        saved, note = projects.record_last_run("x", self.RESULT)
        self.assertFalse(saved)
        self.assertTrue(note)


if __name__ == "__main__":
    unittest.main()
