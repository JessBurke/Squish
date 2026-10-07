"""Tests for engine.py and cli.py, using folders of synthetic .eml files.

Most tests replace build_digest with a small fake so they only exercise the
engine (scan, cache, read, filter, write). EndToEndTests (in
test_end_to_end.py) runs the real readers and digest.
"""

import errno
import gzip
import hashlib
import io
import json
import os
import shutil
import struct
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from squish_app import cli, docdigest, docs, engine, paths, projects
from tests import msg_builder as mb


def make_eml(subject, date="Tue, 04 Mar 2025 12:00:00 +0000", body="Body text", sender="alex@example.com"):
    date_line = ("Date: %s\n" % date) if date else ""
    return ("From: Alex Example <%s>\nTo: sam@example.org\nSubject: %s\n%s"
            "Message-ID: <%s@example.com>\n\n%s\n" % (sender, subject, date_line,
                                                      subject.replace(" ", "-"), body))


def fake_build_digest(records, project, source_label="", now=None, cancel=None, progress=None,
                      **newer_options):
    """One part (two with part_size 'small') listing the subjects, sorted."""
    subjects = sorted(r["subject"] for r in records)
    dates = sorted(r["date"][:10] for r in records if r["date"])
    text = "FAKE DIGEST | %s | %s\n%s\n" % (project.get("name"), source_label, "\n".join(subjects))
    first, last = (dates[0], dates[-1]) if dates else ("", "")
    if project.get("part_size") == "small" and len(subjects) > 1:
        half = len(subjects) // 2
        parts = [{"text": "\n".join(subjects[:half]), "first_date": first, "last_date": last,
                  "emails": half, "threads": half},
                 {"text": "\n".join(subjects[half:]), "first_date": first, "last_date": last,
                  "emails": len(subjects) - half, "threads": len(subjects) - half}]
    else:
        parts = [{"text": text, "first_date": first, "last_date": last,
                  "emails": len(subjects), "threads": len(subjects)}]
    stats = {"emails_in": len(records), "emails_used": len(records), "duplicates": 0,
             "noise_dropped": 0, "acks_dropped": 0, "filtered_out": 0, "threads": len(records),
             "recovered_quoted": 0, "raw_chars": 0, "output_chars": len(text)}
    return {"parts": parts, "stats": stats}


class EngineTestBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="squish-engine-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"SQUISH_DATA_DIR": os.path.join(self.tmp, "data")})
        env.start()
        self.addCleanup(env.stop)
        patcher = mock.patch.object(engine, "build_digest", side_effect=fake_build_digest)
        self.digest = patcher.start()
        self.addCleanup(patcher.stop)
        self.src = os.path.join(self.tmp, "emails")
        self.out = os.path.join(self.tmp, "out")
        self.files = {}
        self.add("top one.eml", make_eml("Top one", "Mon, 03 Mar 2025 12:00:00 +0000"))
        self.add("top two.EML", make_eml("Top two", "Wed, 05 Mar 2025 12:00:00 +0000"))
        self.add("sub/sub one.eml", make_eml("Sub one", "Fri, 14 Feb 2025 12:00:00 +0000"))
        self.add("sub/deeper/deep one.eml", make_eml("Deep one", "Tue, 01 Apr 2025 12:00:00 +0000"))
        self.add("sub/~$temp.eml", "temporary lock file")
        self.add("sub/notes.txt", "not an email")

    def add(self, rel, text):
        path = os.path.join(self.src, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        self.files[rel] = path
        return path

    def project(self, **changes):
        p = {"id": "abc123", "name": "Test Project", "source_folder": self.src,
             "include_subfolders": True, "output_folder": self.out, "date_from": "",
             "date_to": "", "squeeze": "standard", "part_size": "medium",
             "focus_keywords": "", "org_codes": "", "drop_noise": True,
             "recover_quoted": True, "last_run": None}
        p.update(changes)
        return p

    def outputs(self):
        return sorted(os.listdir(self.out)) if os.path.isdir(self.out) else []

    def snapshot(self):
        """{file name: bytes} of the output folder."""
        result = {}
        for name in self.outputs():
            with open(os.path.join(self.out, name), "rb") as fh:
                result[name] = fh.read()
        return result

    def add_many(self, count, day="Thu, 06 Mar 2025 12:00:00 +0000"):
        for i in range(count):
            self.add("more/m%02d.eml" % i, make_eml("More %02d" % i, day))

    def touch_all(self):
        """Give every source file a new modified time, so it must be read again."""
        for path in self.files.values():
            os.utime(path, (1800000000, 1800000000))

    def cache_entries(self):
        return engine.load_cache(engine.cache_path(self.project()))


class ScanAndReadTests(EngineTestBase):

    def test_recursive_run_result(self):
        result = engine.run_project(self.project())
        self.assertFalse(result["cancelled"])
        self.assertEqual(result["files_found"], 4)
        self.assertEqual(result["files_read"], 4)
        self.assertEqual(result["from_cache"], 0)
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["output_folder"], self.out)
        self.assertEqual(set(result), set(["cancelled", "files", "output_folder", "files_found",
                                           "files_read", "from_cache", "failed", "doc_problems",
                                           "stats", "elapsed_s", "log_path", "finished_at"]))
        self.assertEqual(self.outputs(), ["Squish - Test Project - 2025-02-14 to 2025-04-01.txt"])
        info = result["files"][0]
        self.assertEqual(info["path"], os.path.join(self.out, self.outputs()[0]))
        with open(info["path"], encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("Deep one", text)
        self.assertIn(self.src, text)   # source_label passed through
        self.assertEqual(info["chars"], len(text))
        self.assertEqual(info["bytes"], len(text.encode("utf-8")))
        self.assertEqual(info["est_tokens"], round(len(text) / engine.CHARS_PER_TOKEN))
        self.assertEqual(info["emails"], 4)
        self.assertEqual(result["stats"]["emails_in"], 4)
        self.assertEqual(result["stats"]["outside_dates"], 0)
        self.assertTrue(os.path.isfile(result["log_path"]))
        self.assertTrue(result["log_path"].startswith(os.path.join(self.tmp, "data")))
        self.assertRegex(result["finished_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        records = self.digest.call_args[0][0]
        self.assertEqual(len(records), 4)
        self.assertEqual(self.digest.call_args[1]["source_label"], self.src)

    def test_without_subfolders(self):
        result = engine.run_project(self.project(include_subfolders=False))
        self.assertEqual(result["files_found"], 2)
        subjects = sorted(r["subject"] for r in self.digest.call_args[0][0])
        self.assertEqual(subjects, ["Top one", "Top two"])

    def test_second_run_uses_cache_and_changed_file_is_reread(self):
        engine.run_project(self.project())
        cache_file = engine.cache_path(self.project())
        self.assertTrue(os.path.isfile(str(cache_file)))
        result = engine.run_project(self.project())
        self.assertEqual(result["files_read"], 0)
        self.assertEqual(result["from_cache"], 4)

        path = self.files["sub/sub one.eml"]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(make_eml("Sub one EDITED", "Fri, 14 Feb 2025 12:00:00 +0000", body="longer body text"))
        os.utime(path, (1700000000, 1700000000))
        result = engine.run_project(self.project())
        self.assertEqual(result["files_read"], 1)
        self.assertEqual(result["from_cache"], 3)
        subjects = [r["subject"] for r in self.digest.call_args[0][0]]
        self.assertIn("Sub one EDITED", subjects)

    def test_corrupt_cache_is_ignored(self):
        engine.run_project(self.project())
        cache_file = str(engine.cache_path(self.project()))
        with open(cache_file, "wb") as fh:
            fh.write(b"\x1f\x8b this is not gzip")
        result = engine.run_project(self.project())
        self.assertEqual(result["files_read"], 4)
        with gzip.open(cache_file, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["version"], engine.CACHE_VERSION)
        self.assertEqual(len(data["files"]), 4)
        # An old cache version is ignored too.
        with gzip.open(cache_file, "wt", encoding="utf-8") as fh:
            json.dump({"version": -1, "files": data["files"]}, fh)
        self.assertEqual(engine.run_project(self.project())["files_read"], 4)

    def test_unreadable_file_is_reported_but_run_succeeds(self):
        self.add("sub/broken.msg", "not really an outlook file")
        result = engine.run_project(self.project())
        self.assertEqual(result["files_found"], 5)
        self.assertEqual(len(result["failed"]), 1)
        self.assertTrue(result["failed"][0][0].endswith("broken.msg"))
        self.assertTrue(result["failed"][0][1])
        self.assertEqual(len(result["files"]), 1)
        with open(result["log_path"], encoding="utf-8") as fh:
            self.assertIn("broken.msg", fh.read())
        # Failures are not cached: the file is tried again next time.
        self.assertEqual(len(engine.run_project(self.project())["failed"]), 1)

    def test_unreadable_files_are_counted_for_the_digest_header(self):
        # Claude only sees the digest, so its header must say emails may be missing.
        real_read = engine.readers.read_email

        def read(path, **kwargs):
            if os.path.basename(path) in ("top one.eml", "deep one.eml"):
                raise ValueError("damaged")
            return real_read(path, **kwargs)

        real_scandir = os.scandir

        def scandir(path="."):
            if os.path.basename(str(path)) == "locked":
                raise PermissionError(errno.EACCES, "Permission denied")
            return real_scandir(path)

        os.makedirs(os.path.join(self.src, "locked"))
        with mock.patch.object(engine.readers, "read_email", side_effect=read), \
                mock.patch.object(engine.os, "scandir", side_effect=scandir):
            result = engine.run_project(self.project())
        self.assertEqual(len(result["failed"]), 3)       # 2 files and 1 folder
        self.assertEqual(self.digest.call_args[1]["unreadable_files"], 2)
        self.assertEqual(result["stats"]["unreadable_files"], 2)
        # Without failures the count is 0.
        engine.run_project(self.project())
        self.assertEqual(self.digest.call_args[1]["unreadable_files"], 0)

    def test_date_filter(self):
        self.add("undated.eml", make_eml("No date", date=""))
        result = engine.run_project(self.project(date_from="2025-03-01", date_to="2025-03-31"))
        subjects = sorted(r["subject"] for r in self.digest.call_args[0][0])
        self.assertEqual(subjects, ["No date", "Top one", "Top two"])
        self.assertEqual(result["stats"]["outside_dates"], 2)
        # The emails left out are counted in the header and passed on, so that an
        # email quoting them does not 'recover' them as if never filed.
        self.assertEqual(self.digest.call_args[1]["outside_dates"], 2)
        self.assertEqual(sorted(r["subject"] for r in self.digest.call_args[1]["also_filed"]),
                         ["Deep one", "Sub one"])

    def test_bad_dates_raise(self):
        with self.assertRaises(engine.SquishError):
            engine.run_project(self.project(date_from="03/04/2025"))
        with self.assertRaises(engine.SquishError):
            engine.run_project(self.project(date_from="2025-05-01", date_to="2025-04-01"))

    def test_progress_reports_each_stage(self):
        calls = []
        engine.run_project(self.project(), progress=lambda *a: calls.append(a))
        stages = [c[0] for c in calls]
        for stage in ("scan", "read", "digest", "write"):
            self.assertIn(stage, stages)
        self.assertIn(("read", 4, 4), [c[:3] for c in calls])

    def test_progress_errors_do_not_stop_the_run(self):
        def bad_progress(*args):
            raise RuntimeError("display broke")
        result = engine.run_project(self.project(), progress=bad_progress)
        self.assertEqual(len(result["files"]), 1)


class CancelAndErrorTests(EngineTestBase):

    def test_cancel_before_start_writes_nothing(self):
        cancel = threading.Event()
        cancel.set()
        result = engine.run_project(self.project(), cancel=cancel)
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["files"], [])
        self.assertEqual(self.outputs(), [])
        self.assertEqual(result["log_path"], "")
        self.assertFalse(self.digest.called)

    def test_cancel_during_read_writes_nothing(self):
        cancel = threading.Event()

        def progress(stage, done, total, message):
            if stage == "read" and done >= 1:
                cancel.set()

        with mock.patch.object(engine, "PROGRESS_INTERVAL", 0):
            result = engine.run_project(self.project(), progress=progress, cancel=cancel)
        self.assertTrue(result["cancelled"])
        self.assertLess(result["files_read"], 4)
        self.assertEqual(self.outputs(), [])
        self.assertFalse(self.digest.called)
        logs_dir = os.path.join(self.tmp, "data", "logs")
        self.assertEqual(os.listdir(logs_dir) if os.path.isdir(logs_dir) else [], [])

    def test_cancel_does_not_wait_for_files_not_yet_started(self):
        # Slow files (big emails over a VPN): Cancel takes effect while the
        # first files are still being read, so only those are waited for.
        self.add_many(14)
        cancel = threading.Event()
        real_read = engine.readers.read_email

        def slow_read(path, **kwargs):
            time.sleep(0.6)
            return real_read(path, **kwargs)

        timer = threading.Timer(0.1, cancel.set)
        with mock.patch.object(engine.readers, "read_email", side_effect=slow_read), \
                mock.patch.object(engine, "CANCEL_CHECK_S", 0.05):
            started = time.monotonic()
            timer.start()
            result = engine.run_project(self.project(), cancel=cancel)
            took = time.monotonic() - started
        timer.join()
        self.assertTrue(result["cancelled"])
        self.assertLess(took, 1.0)     # one round of reads (0.6 s), not two (1.2 s)
        self.assertLessEqual(result["files_read"], engine.READ_WORKERS)
        self.assertFalse(self.digest.called)

    def test_cancel_while_writing_keeps_the_old_files(self):
        first = engine.run_project(self.project(part_size="small"))
        before = self.snapshot()
        self.add("new one.eml", make_eml("New one", "Thu, 10 Apr 2025 12:00:00 +0000"))
        cancel = threading.Event()

        def progress(stage, done, total, message):
            if stage == "write" and done >= 1:
                cancel.set()     # after the first temp file is written

        with mock.patch.object(engine, "PROGRESS_INTERVAL", 0):
            result = engine.run_project(self.project(part_size="small"), progress=progress,
                                        cancel=cancel)
        self.assertTrue(result["cancelled"])
        self.assertEqual(self.snapshot(), before)    # no temp files, old parts untouched
        self.assertEqual(len(first["files"]), 2)

    def test_stale_cache_temp_files_are_tidied(self):
        engine.run_project(self.project())
        cache = str(engine.cache_path(self.project()))
        old_tmp, new_tmp = cache + ".tmp-1-2", cache + ".tmp-3-4"
        for path in (old_tmp, new_tmp):
            with open(path, "w") as fh:
                fh.write("left by a save that never finished")
        os.utime(old_tmp, (time.time() - 7200, time.time() - 7200))
        self.add("new one.eml", make_eml("New one", "Thu, 10 Apr 2025 12:00:00 +0000"))
        engine.run_project(self.project())
        self.assertFalse(os.path.exists(old_tmp))
        self.assertTrue(os.path.exists(new_tmp))     # maybe another Squish saving right now

    def test_missing_folder(self):
        with self.assertRaises(engine.SquishError) as ctx:
            engine.run_project(self.project(source_folder=os.path.join(self.tmp, "nope")))
        message = str(ctx.exception)
        self.assertIn("Can't find the email folder", message)
        self.assertIn("VPN", message)
        with self.assertRaises(engine.SquishError):
            engine.run_project(self.project(source_folder=""))

    def test_folder_without_emails(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        with self.assertRaises(engine.SquishError) as ctx:
            engine.run_project(self.project(source_folder=empty))
        self.assertIn("no Outlook emails", str(ctx.exception))


class OutputFileTests(EngineTestBase):

    def test_parts_and_stale_file_cleanup(self):
        os.makedirs(self.out)
        keep = ["notes.txt", "Squish - Other Project - 2024-01-01 to 2024-02-01.txt",
                "Squish - Test Project - draft notes.docx", "Squish - Test Project - my notes.txt",
                "Squish - Test Project - B - 2024-01-01 to 2024-02-01.txt"]
        stale = ["Squish - Test Project - 2024-01-01 to 2024-12-31.txt",
                 "Squish - Test Project - 2024-01-01 to 2024-12-31 (part 2 of 3).txt"]
        for name in keep + stale:
            with open(os.path.join(self.out, name), "w") as fh:
                fh.write("old")
        result = engine.run_project(self.project(part_size="small"))
        new = ["Squish - Test Project - 2025-02-14 to 2025-04-01 (part 1 of 2).txt",
               "Squish - Test Project - 2025-02-14 to 2025-04-01 (part 2 of 2).txt"]
        self.assertEqual(self.outputs(), sorted(keep + new))
        self.assertEqual([os.path.basename(f["path"]) for f in result["files"]], new)
        # Running again as a single file replaces the two parts.
        engine.run_project(self.project())
        self.assertEqual(self.outputs(),
                         sorted(keep + ["Squish - Test Project - 2025-02-14 to 2025-04-01.txt"]))

    def test_output_folder_is_created_and_files_are_utf8_lf(self):
        nested = os.path.join(self.out, "a", "b")
        result = engine.run_project(self.project(output_folder=nested, name="Café / Site: 2"))
        self.assertEqual(len(result["files"]), 1)
        name = os.path.basename(result["files"][0]["path"])
        self.assertTrue(name.startswith("Squish - Café Site 2 - "), name)
        with open(result["files"][0]["path"], "rb") as fh:
            raw = fh.read()
        self.assertNotIn(b"\r\n", raw)
        raw.decode("utf-8")

    def test_default_output_folder(self):
        with mock.patch.object(engine.paths, "default_output_folder",
                               return_value=os.path.join(self.tmp, "Docs", "Squish", "Test Project")):
            result = engine.run_project(self.project(output_folder=""))
        self.assertEqual(result["output_folder"], os.path.join(self.tmp, "Docs", "Squish", "Test Project"))
        self.assertTrue(os.path.isfile(result["files"][0]["path"]))

    def test_helpers(self):
        self.assertTrue(engine.is_email_file("A.MSG"))
        self.assertTrue(engine.is_email_file("b.eml"))
        self.assertFalse(engine.is_email_file("~$lock.msg"))
        self.assertFalse(engine.is_email_file("c.msg.txt"))
        self.assertEqual(engine.email_local_date("2025-03-04T23:30:00+11:00"), "2025-03-04")
        self.assertEqual(engine.email_local_date(""), "")
        self.assertEqual(engine.output_filenames("P", [{"first_date": "", "last_date": ""}]),
                         ["Squish - P - undated.txt"])


class SafeOutputTests(EngineTestBase):
    """A run that can't do a proper job must leave the previous digest alone."""

    FULL = "Squish - Test Project - 2025-02-14 to 2025-04-01.txt"

    def test_many_read_failures_keep_old_digest_and_cache(self):
        self.add_many(16)
        engine.run_project(self.project())
        before = self.snapshot()
        self.assertEqual(len(self.cache_entries()), 20)
        self.touch_all()
        real_read = engine.readers.read_email
        calls = []

        def flaky(path, **kwargs):
            calls.append(path)
            if len(calls) > 5:
                raise OSError(errno.EHOSTDOWN, "The network name cannot be found")
            return real_read(path, **kwargs)

        with mock.patch.object(engine.readers, "read_email", side_effect=flaky):
            with self.assertRaises(engine.SquishError) as ctx:
                engine.run_project(self.project())
        self.assertIn("15 of the 20 email files could not be opened", str(ctx.exception))
        self.assertIn("Nothing was changed", str(ctx.exception))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.cache_entries()), 20)   # nothing forgotten
        # The run log says what happened.
        with open(os.path.join(self.tmp, "data", "logs", "Test Project - last run.txt"),
                  encoding="utf-8") as fh:
            self.assertIn("Stopped:", fh.read())

    def test_all_reads_failing_keeps_old_digest(self):
        engine.run_project(self.project())
        before = self.snapshot()
        self.touch_all()
        with mock.patch.object(engine.readers, "read_email", side_effect=OSError(errno.EIO, "I/O error")):
            with self.assertRaises(engine.SquishError) as ctx:
                engine.run_project(self.project())
        self.assertIn("none of the 4 email files could be read", str(ctx.exception))
        self.assertIn("Nothing was changed - your previous digest files are still there",
                      str(ctx.exception))
        self.assertIn("VPN", str(ctx.exception))
        self.assertEqual(self.snapshot(), before)

    def test_stopped_first_run_of_damaged_files_says_so(self):
        # No earlier run (so no earlier digest), and every file opens but none is
        # an email: neither "previous files" nor the VPN advice would be true.
        with mock.patch.object(engine.readers, "read_email", side_effect=ValueError("not an email")):
            with self.assertRaises(engine.SquishError) as ctx:
                engine.run_project(self.project())
        message = str(ctx.exception)
        self.assertEqual(message.split("\n")[0],
                         "Squish stopped because none of the 4 email files could be read.")
        self.assertIn("No digest was written.", message)
        self.assertIn("look damaged or aren't Outlook emails - see View run log", message)
        self.assertNotIn("previous digest", message)
        self.assertNotIn("VPN", message)
        self.assertEqual(self.outputs(), [])

    def test_stopped_message_wording(self):
        first = "Squish stopped because x."
        for had_previous in (True, False):
            for damaged in (True, False):
                text = engine._stopped_message("x", had_previous, damaged)
                self.assertEqual(text.split("\n")[0], first)
                self.assertEqual("previous digest files are still there" in text, had_previous)
                self.assertEqual("No digest was written." in text, not had_previous)
                self.assertEqual("VPN" in text, not damaged)

    def test_a_few_failures_still_write(self):
        self.add_many(16)
        self.add("sub/broken.msg", "not really an outlook file")
        result = engine.run_project(self.project())
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(len(result["files"]), 1)

    def test_many_damaged_files_do_not_block_for_good(self):
        # Files that open fine but aren't proper emails fail every time: they are
        # reported, but must not stop every run.
        for i in range(6):
            self.add("sub/broken%d.msg" % i, "not really an outlook file")
        result = engine.run_project(self.project())
        self.assertEqual(len(result["failed"]), 6)
        self.assertEqual(len(result["files"]), 1)

    def test_folder_gone_by_the_end_of_the_run(self):
        engine.run_project(self.project())
        before = self.snapshot()
        self.add("sub/broken.msg", "not really an outlook file")
        gone = engine.SquishError("Can't find the email folder")
        with mock.patch.object(engine, "check_source_folder", side_effect=[None, gone]):
            with self.assertRaises(engine.SquishError) as ctx:
                engine.run_project(self.project())
        self.assertIn("stopped responding", str(ctx.exception))
        self.assertEqual(self.snapshot(), before)

    def scandir_failing_for(self, folder_name, error):
        real_scandir = os.scandir

        def scandir(path="."):
            if os.path.basename(str(path)) == folder_name:
                raise error
            return real_scandir(path)
        return mock.patch.object(engine.os, "scandir", side_effect=scandir)

    def test_subfolder_that_held_emails_cannot_be_opened(self):
        engine.run_project(self.project())
        before = self.snapshot()
        with self.scandir_failing_for("sub", OSError(errno.EIO, "network dropped")):
            with self.assertRaises(engine.SquishError) as ctx:
                engine.run_project(self.project())
        self.assertIn("held 2 emails last time", str(ctx.exception))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.cache_entries()), 4)
        # The window's View run log can show the log written before stopping.
        self.assertTrue(ctx.exception.log_path)
        with open(ctx.exception.log_path, encoding="utf-8") as fh:
            self.assertIn("Stopped: Squish stopped because", fh.read())

    def test_subfolder_without_permission_does_not_block(self):
        engine.run_project(self.project())
        with self.scandir_failing_for("sub", PermissionError(errno.EACCES, "Permission denied")):
            result = engine.run_project(self.project())
        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("no access", result["failed"][0][1])
        self.assertIn("it held 2 emails last time; they are not in this digest",
                      result["failed"][0][1])
        self.assertEqual(result["files_found"], 2)
        # The scan was incomplete, so the cache forgets nothing.
        self.assertEqual(len(self.cache_entries()), 4)
        # What is missing is said in the stats, the digest header and the log.
        self.assertEqual(result["stats"]["no_access_emails"], 2)
        self.assertEqual(self.digest.call_args[1]["no_access_emails"], 2)
        self.assertEqual(sorted(r["subject"] for r in self.digest.call_args[0][0]),
                         ["Top one", "Top two"])      # cached emails of that folder not used
        with open(result["log_path"], encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("Squish has no access to %s, which held 2 emails last time"
                      % os.path.join(self.src, "sub"), log)
        self.assertIn("Folders that could not be opened (1):", log)
        self.assertIn("Files that could not be read (0):", log)

    def test_no_access_to_a_folder_that_held_nothing(self):
        with self.scandir_failing_for("sub", PermissionError(errno.EACCES, "Permission denied")):
            result = engine.run_project(self.project())
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["stats"]["no_access_emails"], 0)
        self.assertEqual(self.digest.call_args[1].get("no_access_emails", 0), 0)
        self.assertNotIn("held", result["failed"][0][1])
        with open(result["log_path"], encoding="utf-8") as fh:
            self.assertNotIn("Squish has no access", fh.read())

    def test_locked_folder_is_not_taken_for_no_access(self):
        # Windows reports a sharing violation (32) or lock (33) as PermissionError
        # too: that is a passing problem, so it stops the run like any other.
        engine.run_project(self.project())
        before = self.snapshot()
        error = PermissionError(errno.EACCES, "The process cannot access the file")
        error.winerror = 32
        with self.scandir_failing_for("sub", error):
            with self.assertRaises(engine.SquishError) as ctx:
                engine.run_project(self.project())
        self.assertIn("held 2 emails last time", str(ctx.exception))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.cache_entries()), 4)

    def test_no_access_error_codes(self):
        def denied(winerror):
            exc = PermissionError(errno.EACCES, "denied")
            if winerror is not None:
                exc.winerror = winerror
            return exc
        self.assertTrue(engine._is_no_access(denied(None)))
        self.assertTrue(engine._is_no_access(denied(5)))
        self.assertTrue(engine._is_no_access(denied(65)))
        self.assertFalse(engine._is_no_access(denied(32)))
        self.assertFalse(engine._is_no_access(denied(33)))
        self.assertFalse(engine._is_no_access(OSError(errno.EIO, "I/O error")))

    def listing(self, folder):
        found = []
        for root, dirs, files in os.walk(folder):
            found.extend(os.path.relpath(os.path.join(root, n), folder) for n in dirs + files)
        return sorted(found)

    def test_output_folder_inside_the_email_folder_is_refused(self):
        before = self.listing(self.src)
        for out in (self.src, os.path.join(self.src, "sub"), os.path.join(self.src, "new", "x"),
                    self.src + os.sep):
            with self.assertRaises(engine.SquishError) as ctx:
                engine.run_project(self.project(output_folder=out))
            self.assertIn("inside the email folder", str(ctx.exception))
            self.assertEqual(self.listing(self.src), before, out)
        self.assertFalse(self.digest.called)
        # The other way round (emails inside the output folder) is fine.
        self.assertFalse(engine.output_inside_source(self.src, self.tmp))
        self.assertFalse(engine.output_inside_source(self.src, self.src + " digests"))
        self.assertTrue(engine.output_inside_source(self.src, os.path.join(self.src, "a", "..")))

    def test_unusable_output_folder_is_found_before_reading(self):
        a_file = os.path.join(self.tmp, "a file.txt")
        with open(a_file, "w") as fh:
            fh.write("not a folder")
        calls = []
        with self.assertRaises(engine.SquishError) as ctx:
            engine.run_project(self.project(output_folder=os.path.join(a_file, "digests")),
                               progress=lambda *a: calls.append(a[0]))
        self.assertIn("can't save into this folder", str(ctx.exception))
        self.assertIn("Browse...", str(ctx.exception))
        self.assertNotIn("read", calls)
        self.assertEqual(self.cache_entries(), {})
        self.assertFalse(self.digest.called)
        # A good folder is created, and the test file is gone again.
        engine.check_output_folder(self.out)
        self.assertEqual(os.listdir(self.out), [])

    def test_output_folder_reached_through_a_link_is_refused(self):
        link = os.path.join(self.tmp, "link to emails")
        try:
            os.symlink(self.src, link)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("can't make a symbolic link here")
        self.assertTrue(engine.output_inside_source(self.src, os.path.join(link, "out")))
        self.assertTrue(engine.output_inside_source(link, os.path.join(self.src, "out")))

    def test_dates_that_leave_nothing_keep_the_old_digest(self):
        engine.run_project(self.project())
        before = self.snapshot()
        result = engine.run_project(self.project(date_from="2030-01-01"))
        self.assertEqual(result["files"], [])
        self.assertEqual(result["stats"]["emails_used"], 0)
        self.assertEqual(result["stats"]["outside_dates"], 4)
        self.assertEqual(self.snapshot(), before)
        with open(result["log_path"], encoding="utf-8") as fh:
            self.assertIn("No emails were left", fh.read())

    def test_failed_part_write_changes_nothing(self):
        engine.run_project(self.project(part_size="small"))
        before = self.snapshot()
        self.assertEqual(len(before), 2)
        self.add("later.eml", make_eml("Later one", "Thu, 01 May 2025 12:00:00 +0000"))
        real_write = engine._write_text
        calls = []

        def write(path, text):
            calls.append(path)
            if len(calls) == 2:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_write(path, text)

        with mock.patch.object(engine, "_write_text", side_effect=write):
            with self.assertRaises(engine.SquishError) as ctx:
                engine.run_project(self.project(part_size="small"))
        self.assertIn("have not been changed", str(ctx.exception))
        self.assertEqual(self.snapshot(), before)

    def test_locked_part_changes_nothing(self):
        engine.run_project(self.project(part_size="small"))
        before = self.snapshot()
        self.add("sub/inside.eml", make_eml("Inside the span", "Thu, 06 Mar 2025 12:00:00 +0000"))
        real_replace = os.replace
        for stage, src_end, dst_end in (("move aside", ".txt", "(part 2 of 2).txt.squish-old"),
                                        ("put in place", ".squish-tmp", "(part 2 of 2).txt")):
            def replace(src, dst):
                if str(src).endswith(src_end) and str(dst).endswith(dst_end):
                    raise PermissionError(errno.EACCES, "The file is in use")
                return real_replace(src, dst)

            with mock.patch.object(engine.os, "replace", side_effect=replace):
                with self.assertRaises(engine.SquishError, msg=stage) as ctx:
                    engine.run_project(self.project(part_size="small"))
            self.assertIn("have not been changed", str(ctx.exception), stage)
            self.assertEqual(self.snapshot(), before, stage)

    def test_unsavable_characters_never_leave_a_temp_file(self):
        def odd_digest(records, project, source_label="", now=None, cancel=None, progress=None,
                       **newer_options):
            result = fake_build_digest(records, project, source_label)
            result["parts"][0]["text"] += "half an emoji \ud83d here\n"
            return result

        self.digest.side_effect = odd_digest
        result = engine.run_project(self.project())
        self.assertEqual(self.outputs(), [self.FULL])
        with open(result["files"][0]["path"], encoding="utf-8") as fh:
            self.assertIn("half an emoji ? here", fh.read())

    def test_rtf_emoji_email_is_read_cached_and_written(self):
        rtf = b"{\\rtf1\\ansi\\ansicpg1252 Thumbs up \\u-10179?\\u-8704? and \\u-10179? half\\par}"
        compressed = struct.pack("<IIII", len(rtf) + 12, len(rtf), 0x414C454D, 0) + rtf
        data = mb.build_msg(subject="Emoji note", sender_name="Alex Example",
                            sender_smtp="alex@example.com", rtf_compressed=compressed,
                            submit_time=datetime(2025, 3, 10, tzinfo=timezone.utc))
        path = os.path.join(self.src, "emoji.msg")
        with open(path, "wb") as fh:
            fh.write(data)
        result = engine.run_project(self.project())
        self.assertEqual(result["failed"], [])
        self.assertEqual(len(self.cache_entries()), 5)
        self.assertFalse([n for n in self.outputs() if n.endswith(".squish-tmp")])
        body = [r for r in self.digest.call_args[0][0] if r["subject"] == "Emoji note"][0]["body"]
        self.assertEqual(body, "Thumbs up \U0001F600 and \ufffd half")


class FilteredRunTests(EngineTestBase):
    """Runs for a period or with focus keywords keep the all-dates digest."""

    FULL = "Squish - Test Project - 2025-02-14 to 2025-04-01.txt"

    def test_dated_and_keyword_runs_keep_the_full_digest(self):
        engine.run_project(self.project())
        march = self.project(date_from="2025-03-01", date_to="2025-03-31")
        engine.run_project(march)
        # A dated file's name says it has only part of the emails.
        dated = "Squish - Test Project - 2025-03-03 to 2025-03-05 (only 2025-03-01 to 2025-03-31).txt"
        self.assertEqual(self.outputs(), [self.FULL, dated])
        # The same period again replaces its own file only.
        self.add("late march.eml", make_eml("Late March", "Mon, 24 Mar 2025 12:00:00 +0000"))
        engine.run_project(march)
        dated = "Squish - Test Project - 2025-03-03 to 2025-03-24 (only 2025-03-01 to 2025-03-31).txt"
        self.assertEqual(self.outputs(), [self.FULL, dated])
        # Focus keywords: the name says so, and nothing else is touched.
        result = engine.run_project(self.project(focus_keywords="Top, (deep)"))
        focus = "Squish - Test Project - 2025-02-14 to 2025-04-01 (focus top, deep).txt"
        self.assertEqual(os.path.basename(result["files"][0]["path"]), focus)
        self.assertEqual(self.outputs(), sorted([self.FULL, focus, dated]))
        # A new all-dates run replaces only the all-dates digest.
        self.add("april.eml", make_eml("April", "Fri, 04 Apr 2025 12:00:00 +0000"))
        engine.run_project(self.project())
        self.assertEqual(self.outputs(), sorted(["Squish - Test Project - 2025-02-14 to 2025-04-04.txt",
                                                 dated, focus]))
        # The same keywords in another order and case are the same kind of run.
        result = engine.run_project(self.project(focus_keywords="(DEEP),top"))
        self.assertEqual(self.outputs(), sorted([
            "Squish - Test Project - 2025-02-14 to 2025-04-04.txt", dated,
            "Squish - Test Project - 2025-02-14 to 2025-04-04 (focus deep, top).txt"]))
        # Dates and keywords together: both tags, dates first.
        result = engine.run_project(self.project(date_from="2025-03-01", focus_keywords="top"))
        self.assertEqual(os.path.basename(result["files"][0]["path"]),
                         "Squish - Test Project - 2025-03-03 to 2025-04-04 (only from 2025-03-01) "
                         "(focus top).txt")

    def test_dated_run_without_a_record_keeps_the_full_digest(self):
        # With the record of what each run wrote lost, the previous files stand
        # in for it: a dated run must not take the all-dates digest for its own.
        engine.run_project(self.project())
        os.remove(str(engine.manifest_path(self.project())))
        previous = [os.path.join(self.out, self.FULL)]
        engine.run_project(self.project(date_to="2025-03-31", previous_files=previous))
        self.assertEqual(self.outputs(), sorted([
            self.FULL, "Squish - Test Project - 2025-02-14 to 2025-03-05 (only up to 2025-03-31).txt"]))
        # ... and an all-dates run doesn't take the dated file for an old digest.
        os.remove(str(engine.manifest_path(self.project())))
        engine.run_project(self.project())
        self.assertIn("Squish - Test Project - 2025-02-14 to 2025-03-05 (only up to 2025-03-31).txt",
                      self.outputs())

    def test_helpers(self):
        self.assertEqual(engine.filter_key("", "", ""), "")
        self.assertEqual(engine.filter_key("", "", " , "), "")
        self.assertEqual(engine.filter_key("2025-01-01", "", "b, A"),
                         engine.filter_key("2025-01-01", "", "a\nb"))
        self.assertEqual(engine.focus_label(""), "")
        self.assertEqual(engine.focus_label("RFI 12, culvert/headwall"), "focus rfi 12, culvert headwall")
        self.assertTrue(engine.is_old_output("Squish - P - undated (focus x).txt", "P"))
        self.assertTrue(engine.is_old_output("Squish - P - 2025-01-01 to 2025-02-01 (focus x) (part 1 of 2).txt", "P"))
        self.assertFalse(engine.is_old_output("Squish - P - B - 2025-01-01 to 2025-02-01.txt", "P"))
        self.assertEqual(engine.date_label("", ""), "")
        self.assertEqual(engine.date_label("2025-01-01", "2025-01-31"), "only 2025-01-01 to 2025-01-31")
        self.assertEqual(engine.date_label("2025-01-15", ""), "only from 2025-01-15")
        self.assertEqual(engine.date_label("", "2025-01-31"), "only up to 2025-01-31")
        for tail in ("(only 2025-01-01 to 2025-01-31).txt", "(only from 2025-01-15) (part 1 of 2).txt",
                     "(only up to 2025-01-31) (focus x) (part 2 of 2).txt"):
            name = "Squish - P - 2025-01-02 to 2025-01-30 " + tail
            self.assertTrue(engine.is_old_output(name, "P"), name)
            self.assertTrue(engine.is_digest_file_name(name), name)
        # A project that happens to be called '(only ...)' is not a dated run.
        self.assertFalse(engine.is_old_output("Squish - P (only stage 1) - 2025-01-02 to 2025-01-30.txt", "P"))

    def test_long_keyword_lists_that_start_alike_get_their_own_files(self):
        # The code in the label is made from the whole list, not from the
        # first 80 characters that make a safe file name.
        base = ("culvert, headwall, RFI 12, RFI 13, drainage pit, stormwater, kerb inlet, "
                "Main St, Hill Rd")
        self.assertGreater(len(base), 80)
        labels = [engine.focus_label(k) for k in (base, base + ", alpha", base + ", beta")]
        self.assertEqual(len(set(labels)), 3)
        for label in labels:
            self.assertLessEqual(len(label), len("focus ") + engine.FOCUS_TEXT_MAX)
            self.assertRegex(label, r" ~[0-9a-f]{6}$")
        # A run with each list keeps its own focus digest.
        self.add("topics.eml", make_eml("Culvert and headwall", "Thu, 06 Mar 2025 12:00:00 +0000"))
        first = engine.run_project(self.project(focus_keywords=base + ", alpha"))["files"]
        second = engine.run_project(self.project(focus_keywords=base + ", beta"))["files"]
        self.assertNotEqual(first[0]["path"], second[0]["path"])
        self.assertTrue(os.path.isfile(first[0]["path"]))
        self.assertTrue(os.path.isfile(second[0]["path"]))


class RenameAndLongNameTests(EngineTestBase):
    """Renamed projects and long names leave no stale digest files behind."""

    FULL = "Squish - Test Project - 2025-02-14 to 2025-04-01.txt"
    OTHER = "Squish - Other Project - 2024-01-01 to 2024-02-01.txt"

    def setUp(self):
        super(RenameAndLongNameTests, self).setUp()
        os.makedirs(self.out)
        with open(os.path.join(self.out, self.OTHER), "w") as fh:
            fh.write("another project's digest")

    def test_renamed_project_replaces_its_old_files(self):
        engine.run_project(self.project())
        march = self.project(date_from="2025-03-01", date_to="2025-03-31")
        engine.run_project(march)
        dated = "Squish - Test Project - 2025-03-03 to 2025-03-05 (only 2025-03-01 to 2025-03-31).txt"
        self.assertEqual(self.outputs(), sorted([self.FULL, dated, self.OTHER]))
        engine.run_project(self.project(name="Renamed Job"))
        self.assertEqual(self.outputs(), sorted(["Squish - Renamed Job - 2025-02-14 to 2025-04-01.txt",
                                                 dated, self.OTHER]))
        engine.run_project(self.project(name="Renamed Job", date_from="2025-03-01", date_to="2025-03-31"))
        self.assertEqual(self.outputs(), sorted([
            "Squish - Renamed Job - 2025-02-14 to 2025-04-01.txt",
            "Squish - Renamed Job - 2025-03-03 to 2025-03-05 (only 2025-03-01 to 2025-03-31).txt",
            self.OTHER]))

    def test_previous_files_stand_in_for_a_lost_record(self):
        engine.run_project(self.project())
        focus = engine.run_project(self.project(focus_keywords="top"))["files"][0]["path"]
        os.remove(str(engine.manifest_path(self.project())))
        previous = [os.path.join(self.out, self.FULL), focus,
                    os.path.join(self.tmp, "elsewhere", "Squish - Test Project - undated.txt")]
        engine.run_project(self.project(name="Renamed Job", previous_files=previous))
        self.assertEqual(self.outputs(), sorted([
            "Squish - Renamed Job - 2025-02-14 to 2025-04-01.txt", os.path.basename(focus), self.OTHER]))

    def test_long_names_are_shortened_and_old_long_files_replaced(self):
        name = "Riverside Depot Upgrade Stage 2 Civil and Structural Works"
        old_style = "Squish - %s - 2024-01-01 to 2024-12-31.txt" % name
        with open(os.path.join(self.out, old_style), "w") as fh:
            fh.write("written before long names were shortened")
        result = engine.run_project(self.project(name=name))
        written = os.path.basename(result["files"][0]["path"])
        self.assertEqual(written, "Squish - %s - 2025-02-14 to 2025-04-01.txt" % paths.output_name(name))
        self.assertTrue(engine.is_old_output(written, name))
        self.assertTrue(engine.is_old_output(old_style, name))
        self.assertEqual(self.outputs(), sorted([written, self.OTHER]))

    def test_each_part_is_named_with_its_own_dates(self):
        parts = [{"first_date": "2024-01-02", "last_date": "2024-06-30"},
                 {"first_date": "2024-05-01", "last_date": "2025-03-04"},
                 {"first_date": "", "last_date": ""}]
        self.assertEqual(engine.output_filenames("P", parts, "focus pump"), [
            "Squish - P - 2024-01-02 to 2024-06-30 (focus pump) (part 1 of 3).txt",
            "Squish - P - 2024-05-01 to 2025-03-04 (focus pump) (part 2 of 3).txt",
            "Squish - P - undated (focus pump) (part 3 of 3).txt"])
        self.assertEqual(engine.output_filenames("P", parts[:1], dates="only from 2024-01-01"),
                         ["Squish - P - 2024-01-02 to 2024-06-30 (only from 2024-01-01).txt"])
        self.assertTrue(engine.is_digest_file_name("Squish - A - B - undated (part 3 of 3).txt"))
        self.assertFalse(engine.is_digest_file_name("Squish - A - notes.txt"))
        self.assertFalse(engine.is_digest_file_name("Other - A - undated.txt"))

    def test_focus_text_is_shortened_to_keep_paths_short(self):
        name = "1234 Riverside Depot Upgrade Stage 2 Civil and Structural"
        folder = "C:\\Users\\firstname.lastname\\OneDrive - Example Consulting\\Documents\\Squish\\" \
                 + paths.output_name(name)
        parts = [{"first_date": "2024-08-22", "last_date": "2025-11-30"}] * 12
        focus = engine.focus_label("culvert, headwall, pump station, RFI 12, drainage")
        other = engine.focus_label("culvert, headwall, pump station, RFI 12, stormwater")
        names = engine.output_filenames(name, parts, focus, folder)
        for n in names:
            self.assertLessEqual(len(folder) + 1 + len(n), engine.PATH_BUDGET, n)
            self.assertTrue(engine.is_digest_file_name(n), n)
            self.assertTrue(engine.is_old_output(n, name), n)
            self.assertRegex(n, r" \(focus[^()]* ~[0-9a-f]{6}\) \(part \d+ of 12\)\.txt$")
        self.assertEqual(len(set(names)), 12)
        self.assertFalse(set(names) & set(engine.output_filenames(name, parts, other, folder)))
        # Short paths keep the names they always had.
        self.assertEqual(engine.output_filenames(name, parts, focus, self.out),
                         engine.output_filenames(name, parts, focus))
        # A shorter name leaves room for the first keywords (cut at a word).
        short = engine.output_filenames("Riverside Depot Upgrade", parts, focus, folder)
        self.assertRegex(short[0], r" \(focus culvert, headwall, pump ~[0-9a-f]{6}\) \(part 1 of 12\)")
        self.assertLessEqual(len(folder) + 1 + len(short[-1]), engine.PATH_BUDGET)
        # The dates tag is never cut; the focus text makes room for it.
        dates = engine.date_label("2024-08-01", "2025-11-30")
        folder = "C:\\Users\\firstname.lastname\\Documents\\Squish\\" + paths.output_name(name)
        self.assertGreater(len(folder) + 1 + len(engine.output_filenames(name, parts, focus, dates=dates)[0]),
                           engine.PATH_BUDGET)
        dated = engine.output_filenames(name, parts, focus, folder, dates=dates)
        for n in dated:
            self.assertLessEqual(len(folder) + 1 + len(n), engine.PATH_BUDGET, n)
            self.assertIn(" (%s) (focus" % dates, n)
            self.assertTrue(engine.is_old_output(n, name), n)


class PathAndSpeedTests(EngineTestBase):

    def test_quoted_folders_from_copy_as_path(self):
        self.assertIsNone(engine.check_source_folder('"%s"' % self.src))
        self.assertEqual(engine.output_folder_for({"output_folder": ' "%s" ' % self.out, "name": "P"}),
                         self.out)
        result = engine.run_project(self.project(source_folder='"%s"' % self.src,
                                                 output_folder='"%s"' % self.out))
        self.assertEqual(self.outputs(), ["Squish - Test Project - 2025-02-14 to 2025-04-01.txt"])
        self.assertEqual(result["output_folder"], self.out)
        self.assertEqual(self.digest.call_args[1]["source_label"], self.src)

    def test_unchanged_files_are_not_looked_at_one_by_one(self):
        engine.run_project(self.project())
        real_stat = os.stat
        looked_at = []

        def counting_stat(path, *args, **kwargs):
            if str(path).lower().endswith(".eml"):
                looked_at.append(path)
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(engine.os, "stat", side_effect=counting_stat):
            result = engine.run_project(self.project())
        self.assertEqual((result["files_read"], result["from_cache"]), (0, 4))
        self.assertEqual(looked_at, [])

    def test_cancel_while_the_digest_is_built(self):
        class Stopped(Exception):
            pass

        cancel = threading.Event()

        def slow_digest(records, project, source_label="", now=None, cancel=None, progress=None,
                        **newer_options):
            progress(1, 10)
            cancel.set()
            raise Stopped()

        self.digest.side_effect = slow_digest
        calls = []
        with mock.patch.object(engine, "DigestCancelled", Stopped), \
                mock.patch.object(engine, "PROGRESS_INTERVAL", 0):
            result = engine.run_project(self.project(), progress=lambda *a: calls.append(a),
                                        cancel=cancel)
        self.assertTrue(result["cancelled"])
        self.assertEqual(self.outputs(), [])
        self.assertIn(("digest", 1, 10), [c[:3] for c in calls])
        self.assertIs(self.digest.call_args[1]["cancel"], cancel)


class CliTests(EngineTestBase):

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_run_with_source(self):
        code, out, err = self.run_cli("run", "--source", self.src, "--out", self.out,
                                      "--name", "Cli Test", "--from", "2025-03-01",
                                      "--org", "example.com=EX, example.org=EO", "--no-subfolders")
        self.assertEqual(code, 0, err)
        self.assertIn("Wrote 1 file(s)", out)
        dated = "Squish - Cli Test - 2025-03-03 to 2025-03-05 (only from 2025-03-01).txt"
        self.assertEqual(self.outputs(), [dated])
        project = self.digest.call_args[0][1]
        self.assertEqual(project["org_codes"], "example.com=EX\nexample.org=EO")
        self.assertFalse(project["include_subfolders"])
        # Same folder again, now with subfolders and all dates: the two top-level
        # emails come from the cache. The dated run's digest is kept alongside.
        code, out, err = self.run_cli("run", "--source", self.src, "--out", self.out, "--name", "Cli Test")
        self.assertEqual(code, 0, err)
        self.assertIn("2 read, 2 from cache", out)
        self.assertEqual(self.outputs(), ["Squish - Cli Test - 2025-02-14 to 2025-04-01.txt", dated])

    def test_nothing_left_to_write_is_an_error_exit(self):
        code, out, err = self.run_cli("run", "--source", self.src, "--out", self.out, "--name", "Cli Test")
        self.assertEqual(code, 0, err)
        before = self.outputs()
        code, out, err = self.run_cli("run", "--source", self.src, "--out", self.out,
                                      "--name", "Cli Test", "--from", "2030-01-01")
        self.assertEqual(code, 1)
        self.assertIn("No emails were left", out)
        self.assertEqual(self.outputs(), before)

    def test_default_name_includes_parent_of_generic_folder(self):
        # Two projects' "01 Emails" folders must not share digest files.
        docs = os.path.join(self.tmp, "Docs")
        sources = []
        for job, date in (("Riverside Depot", "Mon, 03 Mar 2025 12:00:00 +0000"),
                          ("Harbour Bridge", "Tue, 01 Jul 2025 12:00:00 +0000")):
            folder = os.path.join(self.tmp, job, "01 Emails")
            os.makedirs(folder)
            with open(os.path.join(folder, "a.eml"), "w", encoding="utf-8") as fh:
                fh.write(make_eml(job + " update", date))
            sources.append(folder)
        with mock.patch.object(engine.paths, "documents_dir", return_value=Path(docs)):
            for folder in sources:
                code, out, err = self.run_cli("run", "--source", folder)
                self.assertEqual(code, 0, err)
        made = sorted((os.path.basename(d), sorted(f)) for d, _sub, f in os.walk(os.path.join(docs, "Squish"))
                      if f)
        self.assertEqual(made, [
            ("Harbour Bridge - 01 Emails",
             ["Squish - Harbour Bridge - 01 Emails - 2025-07-01 to 2025-07-01.txt"]),
            ("Riverside Depot - 01 Emails",
             ["Squish - Riverside Depot - 01 Emails - 2025-03-03 to 2025-03-03.txt"])])

    def test_project_from_args_names(self):
        parser = cli.build_parser()
        args = parser.parse_args(["run", "--source", os.path.join(self.tmp, "Riverside Depot", "01 Emails")])
        project, error = cli._project_from_args(args)
        self.assertEqual(project["name"], "Riverside Depot - 01 Emails")
        args = parser.parse_args(["run", "--source", os.path.join(self.tmp, "Harbour Bridge")])
        self.assertEqual(cli._project_from_args(args)[0]["name"], "Harbour Bridge")
        args = parser.parse_args(["run", "--source", os.path.join(self.tmp, "x", "01 Emails"),
                                  "--name", "My Job"])
        self.assertEqual(cli._project_from_args(args)[0]["name"], "My Job")
        args = parser.parse_args(["run", "--source", '"%s"' % self.src, "--out", '"%s"' % self.out])
        project = cli._project_from_args(args)[0]
        self.assertEqual((project["source_folder"], project["output_folder"]), (self.src, self.out))

    def test_run_errors(self):
        code, out, err = self.run_cli("run", "--source", os.path.join(self.tmp, "missing"))
        self.assertEqual(code, 1)
        self.assertIn("Can't find the email folder", err)
        code, out, err = self.run_cli("run")
        self.assertEqual(code, 1)
        code, out, err = self.run_cli("run", "--squeeze", "extreme", "--source", self.src)
        self.assertEqual(code, 2)

    def test_saved_project_and_list(self):
        saved = [self.project(name="Bridge Upgrade"), self.project(name="Depot Slab", id="d2")]
        with mock.patch.object(cli, "_load_projects", return_value=saved):
            code, out, err = self.run_cli("list")
            self.assertEqual(code, 0)
            self.assertIn("Bridge Upgrade", out)
            self.assertIn("Depot Slab", out)
            code, out, err = self.run_cli("run", "bridge upgrade")
            self.assertEqual(code, 0, err)
            self.assertEqual(self.digest.call_args[0][1]["name"], "Bridge Upgrade")
            code, out, err = self.run_cli("run", "Nothing like it")
            self.assertEqual(code, 1)
            self.assertIn("No saved project", err)

    def test_saved_project_run_is_recorded_for_the_window(self):
        from squish_app import projects
        saved = self.project(name="Bridge Upgrade")
        projects.save_projects([saved])
        code, out, err = self.run_cli("run", "bridge upgrade")
        self.assertEqual(code, 0, err)
        last = projects.load_projects()[0]["last_run"]
        self.assertEqual([os.path.basename(f["path"]) for f in last["files"]], self.outputs())
        # --out writes somewhere else: the project's last run is left as it was.
        code, out, err = self.run_cli("run", "bridge upgrade", "--out", os.path.join(self.tmp, "o2"))
        self.assertEqual(code, 0, err)
        self.assertEqual(projects.load_projects()[0]["last_run"], last)
        # While a Squish window is open it owns the project list: say so instead.
        lock = projects.take_window_lock()
        self.addCleanup(lock.release)
        self.add("new one.eml", make_eml("New one", "Thu, 10 Apr 2025 12:00:00 +0000"))
        code, out, err = self.run_cli("run", "bridge upgrade")
        self.assertEqual(code, 0, err)
        self.assertIn("open Squish window", err)
        self.assertEqual(projects.load_projects()[0]["last_run"], last)

    def test_saved_project_cannot_take_another_source_or_name(self):
        saved = [self.project(name="Bridge Upgrade")]
        with mock.patch.object(cli, "_load_projects", return_value=saved):
            code, out, err = self.run_cli("run", "bridge upgrade")
            self.assertEqual(code, 0, err)
            before = self.snapshot()
            cached = len(engine.load_cache(engine.cache_path(saved[0])))
            calls = self.digest.call_count
            other = os.path.join(self.tmp, "other")
            os.makedirs(other)
            with open(os.path.join(other, "x.eml"), "w", encoding="utf-8") as fh:
                fh.write(make_eml("Unrelated", "Mon, 26 May 2025 12:00:00 +0000"))
            for extra in (("--source", other), ("--name", "X"), ("--no-subfolders",),
                          ("--source", other, "--name", "X")):
                code, out, err = self.run_cli("run", "bridge upgrade", *extra)
                self.assertEqual(code, 2, extra)
                self.assertIn("can't be used with a saved project", err)
                self.assertIn(extra[0], err)
        self.assertEqual(self.digest.call_count, calls)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(engine.load_cache(engine.cache_path(saved[0]))), cached)
        # --out and the other options still work with a saved project.
        with mock.patch.object(cli, "_load_projects", return_value=saved):
            code, out, err = self.run_cli("run", "bridge upgrade", "--squeeze", "max",
                                          "--out", os.path.join(self.tmp, "elsewhere"))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.digest.call_args[0][1]["squeeze"], "max")

    def test_source_run_cannot_take_over_a_saved_projects_files(self):
        # A --source run named like a saved project would delete that project's
        # digest (all-dates clean-up) and overwrite its run log: refused.
        projects.save_projects([self.project(name="Clash Job", output_folder=self.out)])
        result = engine.run_project(projects.load_projects()[0])
        self.assertTrue(result["files"])
        before = self.snapshot()
        calls = self.digest.call_count
        code, out, err = self.run_cli("run", "--source", self.src, "--out", self.out,
                                      "--name", "clash job")
        self.assertEqual(code, 2)
        self.assertIn('run "Clash Job"', err)
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        with open(os.path.join(other, "x.eml"), "w", encoding="utf-8") as fh:
            fh.write(make_eml("Unrelated", "Mon, 26 May 2025 12:00:00 +0000"))
        code, out, err = self.run_cli("run", "--source", other, "--out", self.out,
                                      "--name", "Clash Job")
        self.assertEqual(code, 2)
        self.assertIn("--name", err)
        self.assertEqual(self.digest.call_count, calls)
        self.assertEqual(self.snapshot(), before)
        # Another name is fine, and the saved project's files stay.
        code, out, err = self.run_cli("run", "--source", other, "--out", self.out,
                                      "--name", "Clash Job 2")
        self.assertEqual(code, 0, err)
        for name, data in before.items():
            with open(os.path.join(self.out, name), "rb") as fh:
                self.assertEqual(fh.read(), data)

    def test_summary_counts_folders_apart_from_files(self):
        real_scandir = os.scandir

        def scandir(path="."):
            if os.path.basename(str(path)) == "sub":
                raise PermissionError(errno.EACCES, "Permission denied")
            return real_scandir(path)

        with mock.patch.object(engine.os, "scandir", side_effect=scandir):
            code, out, err = self.run_cli("run", "--source", self.src, "--out", self.out,
                                          "--name", "Test Project")
        self.assertEqual(code, 0, err)
        self.assertIn("0 could not be read", out)
        self.assertIn("1 folder could not be opened", out)

    def test_summary_file_line_says_characters_and_small_token_counts(self):
        project = {"name": "Job"}
        result = {"files": [{"path": "/x/a.txt", "chars": 700, "est_tokens": 200,
                             "first_date": "2025-01-01", "last_date": "2025-01-02"},
                            {"path": "/x/b.txt", "chars": 7000, "est_tokens": 2000,
                             "first_date": "2025-01-03", "last_date": "2025-01-04"}],
                  "output_folder": "/x", "stats": {"emails_used": 3}}
        text = cli._format_summary(project, result)
        self.assertIn("a.txt  (700 characters, ~200 tokens, 2025-01-01 to 2025-01-02)", text)
        self.assertIn("b.txt  (7,000 characters, ~2k tokens, 2025-01-03 to 2025-01-04)", text)
        self.assertNotIn("chars", text)

    def test_damaged_project_list_is_reported(self):
        data_dir = os.environ["SQUISH_DATA_DIR"]
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, "projects.json"), "w") as fh:
            fh.write("{not json")
        code, out, err = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("damaged", err)
        self.assertIn("projects.json.bad-", err)



# --------------------------------------------------------------------------
# Documents (v1.1)
# --------------------------------------------------------------------------

GEOTECH = b"Allowable bearing pressure 150 kPa. Groundwater at 2.4 m."
CALC = b"Pump duty 35 L/s at 18 m head"
LOOSE_TIME = datetime(2025, 3, 11, 12, 0, tzinfo=timezone.utc).timestamp()


def make_eml_with_files(subject, date_text, files, body="See attached."):
    """Bytes of an .eml with file attachments [(name, bytes)]."""
    msg = EmailMessage()
    msg["From"] = "Sam Brown <sam.brown@riverside.example>"
    msg["To"] = "alex@example.com"
    msg["Subject"] = subject
    msg["Date"] = date_text
    msg["Message-ID"] = "<%s@riverside.example>" % subject.replace(" ", "-").replace(":", "")
    msg.set_content(body)
    for name, data in files:
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    return msg.as_bytes()


def fake_extract(name, data=None, path=None):
    """Stands in for docs.extract, so these engine tests don't depend on how a
    reader lays out a file: the bytes are the text ("BROKEN..." fails,
    "DRAWING..." is a drawing). The real readers are used in test_end_to_end."""
    doc = {"kind": "text", "status": "ok", "note": "", "title": "", "pages": None,
           "drawing": False, "blocks": [], "chars": 0, "reader": "builtin"}
    if not docs.is_supported(name):
        doc.update(kind="other", status="unsupported", note="file type not read")
        return doc
    if data is None and path:
        with open(path, "rb") as fh:
            data = fh.read()
    if data is None:
        doc.update(status="error", note="no file given")
        return doc
    text = data.decode("utf-8", "replace")
    if text.startswith("BROKEN"):
        doc.update(status="error", note="damaged file")
        return doc
    doc["blocks"] = [{"type": "para", "text": text, "level": 0}]
    doc["chars"] = len(text)
    doc["drawing"] = text.startswith("DRAWING")
    return doc


def focus_build_digest(records, project, source_label="", now=None, cancel=None, progress=None,
                       doc_ids=None, **newer_options):
    """Like fake_build_digest, but keeps only emails whose subject has a focus
    keyword, and lists their documents as '[att: name =Dn]' like the real digest."""
    words = [w.strip().lower() for w in (project.get("focus_keywords") or "").split(",") if w.strip()]
    kept = [r for r in records if not words or any(w in r["subject"].lower() for w in words)]
    result = fake_build_digest(kept, project, source_label)
    # Like the real header, the "How to read" line has an example ID.
    lines = ['How to read: "name =D3" in [att: ...] = in the documents digest', "## Fake thread"]
    for r in kept:
        marks = ["%s =%s" % (a["name"], doc_ids[(r["path"], i)])
                 for i, a in enumerate(r["attachments"]) if doc_ids and (r["path"], i) in doc_ids]
        lines.append("%s [att: %s]" % (r["subject"], "; ".join(marks)))
    result["parts"][0]["text"] += "\n".join(lines) + "\n"
    result["stats"]["emails_in"] = len(records)
    return result


class DocumentTestBase(EngineTestBase):

    def setUp(self):
        EngineTestBase.setUp(self)
        patcher = mock.patch.object(engine.docs, "extract", side_effect=fake_extract)
        self.extract = patcher.start()
        self.addCleanup(patcher.stop)
        # The real documents digest, watched so the tests can see what it was given.
        patcher = mock.patch.object(engine, "build_documents_digest",
                                    side_effect=docdigest.build_documents_digest)
        self.doc_digest = patcher.start()
        self.addCleanup(patcher.stop)
        self.docs_dir = os.path.join(self.tmp, "04 Reports")
        self.report = self.add_bytes("reports/report.eml", make_eml_with_files(
            "Geotech report", "Mon, 10 Mar 2025 12:00:00 +0000",
            [("Geotech report.pdf", GEOTECH), ("IMG_0001.jpg", b"photo"),
             ("Old spec.doc", b"old word")]))
        self.resent = self.add_bytes("reports/resent.eml", make_eml_with_files(
            "RE: Geotech report", "Wed, 12 Mar 2025 12:00:00 +0000",
            [("Geotech report (1).pdf", GEOTECH), ("Calc.xlsx", CALC)]))
        self.add_loose("Geotech copy.pdf", GEOTECH)
        self.add_loose("Notes.txt", b"Site notes: gate code changed")
        self.add_loose("Site plan.dwg", b"cad")
        self.add_loose("sub/Deep.docx", b"Deep doc about piles")
        self.add_loose("~$Calc.xlsx", b"office lock file")
        self.add_loose(".hidden.pdf", b"hidden")
        self.add_loose("filed.eml", make_eml("Filed in the reports folder").encode("utf-8"))
        self.add_loose("Squish - Other - 2025-01-01 to 2025-01-02.txt", b"a digest")

    def add_bytes(self, rel, data):
        path = os.path.join(self.src, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        self.files[rel] = path
        return path

    def add_loose(self, rel, data):
        """A file in the documents folder, modified 2025-03-11 (a loose file's date
        counts in the documents digest's dates and file name)."""
        path = os.path.join(self.docs_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        os.utime(path, (LOOSE_TIME, LOOSE_TIME))
        return path

    def project(self, **changes):
        p = EngineTestBase.project(self, docs_from_attachments=True, docs_folder=self.docs_dir,
                                   docs_include_subfolders=True)
        p.update(changes)
        return p

    def docs_passed(self):
        """The documents list the last documents digest was given."""
        return self.doc_digest.call_args[0][0]

    def extracted(self):
        """Names docs.extract was asked to read (with their bytes). The report is
        attached twice under two names; whichever read thread gets to it first
        reads it, so both names count as "Geotech report.pdf"."""
        return sorted(c[0][0].replace(" (1)", "") for c in self.extract.call_args_list
                      if c[1].get("data") is not None)

    def text_of(self, kind):
        paths_ = [f["path"] for f in self.last["files"] if f["kind"] == kind]
        with open(paths_[0], encoding="utf-8") as fh:
            return fh.read()

    def run_it(self, **changes):
        self.last = engine.run_project(self.project(**changes))
        return self.last


class DocumentRunTests(DocumentTestBase):

    def test_attachments_and_loose_files_make_a_documents_digest(self):
        result = self.run_it()
        self.assertEqual(self.outputs(), [
            "Squish - Test Project - 2025-02-14 to 2025-04-01.txt",
            "Squish - Test Project - documents - 2025-03-10 to 2025-03-12.txt"])
        self.assertEqual([f["kind"] for f in result["files"]], ["emails", "documents"])
        self.assertEqual(result["files"][1]["documents"], 4)    # (the 2 other files are only listed)
        # Each document once, numbered by first appearance: attachments by email
        # date, then loose files by path. Unread ones (old .doc, .dwg) get no ID.
        passed = self.docs_passed()
        self.assertEqual([(d["id"], d["name"]) for d in passed], [
            ("D1", "Geotech report.pdf"), ("", "Old spec.doc"), ("D2", "Calc.xlsx"),
            ("D3", "Notes.txt"), ("", "Site plan.dwg"), ("D4", "Deep.docx")])
        self.assertEqual(set(passed[0]), set(["id", "name", "sha1", "size", "doc", "sources"]))
        self.assertEqual(passed[0]["size"], len(GEOTECH))
        # The same report attached twice and saved in the folder: one document, three sources.
        sources = passed[0]["sources"]
        self.assertEqual([s["kind"] for s in sources], ["email", "email", "file"])
        self.assertEqual([s["subject"] for s in sources[:2]], ["Geotech report", "RE: Geotech report"])
        self.assertEqual(sources[1]["thread"], "Geotech report")
        self.assertEqual(sources[0]["sender_name"], "Sam Brown")
        self.assertEqual(sources[0]["sender_email"], "sam.brown@riverside.example")
        self.assertEqual(sources[0]["path"], self.report)
        self.assertEqual(sources[0]["sender_alias"], "")     # (the fake digest has no aliases)
        self.assertEqual(sources[2]["path"], os.path.join(self.docs_dir, "Geotech copy.pdf"))
        self.assertTrue(datetime.fromisoformat(sources[2]["mtime"]).tzinfo)
        # Each distinct content is read once; skipped: the photo, '~$' and hidden
        # files, emails and digests in the documents folder.
        self.assertEqual(self.extracted(), ["Calc.xlsx", "Deep.docx", "Geotech report.pdf",
                                            "Notes.txt"])
        # The email digest marks the attachments with their IDs.
        self.assertEqual(self.digest.call_args[1]["doc_ids"], {
            (self.report, 0): "D1", (self.resent, 0): "D1", (self.resent, 1): "D2"})
        self.assertEqual(self.doc_digest.call_args[1]["source_label"],
                         "%s (attachments) + %s" % (self.src, self.docs_dir))
        stats = result["stats"]
        self.assertEqual([stats[k] for k in ("documents", "doc_drawings", "doc_other",
                                             "doc_versions", "doc_failed")], [4, 0, 2, 0, 0])
        self.assertEqual(result["doc_problems"], [])
        with open(result["log_path"], encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("Documents: attachments yes | folder: %s" % self.docs_dir, log)
        self.assertIn("Documents: 6 found (4 condensed this run", log)
        self.assertIn("Documents that could not be read (0):", log)

    def test_sources_use_the_email_digests_aliases(self):
        def with_aliases(records, project, **kwargs):
            result = fake_build_digest(records, project, **kwargs)
            result["aliases"] = dict((r["path"], "RC.SB") for r in records)
            return result

        self.digest.side_effect = with_aliases
        self.run_it()
        self.assertEqual([s.get("sender_alias") for s in self.docs_passed()[0]["sources"]],
                         ["RC.SB", "RC.SB", None])

    def test_progress_has_a_documents_stage(self):
        stages = []
        engine.run_project(self.project(), progress=lambda st, d, t, m: stages.append(st))
        order = [s for i, s in enumerate(stages) if i == 0 or stages[i - 1] != s]
        self.assertEqual(order, ["scan", "read", "documents", "digest", "write"])

    def test_without_documents_nothing_changes(self):
        result = self.run_it(docs_from_attachments=False, docs_folder="")
        self.assertEqual(self.outputs(), ["Squish - Test Project - 2025-02-14 to 2025-04-01.txt"])
        self.assertNotIn("doc_ids", self.digest.call_args[1])
        self.assertFalse(self.extract.called)
        self.assertFalse(self.doc_digest.called)
        self.assertFalse(os.path.exists(str(engine.docs_cache_path(self.project()))))
        self.assertEqual(result["stats"]["documents"], 0)
        for entry in self.cache_entries().values():
            self.assertFalse(entry["docs"])
            for att in entry["record"]["attachments"]:
                self.assertNotIn("sha1", att)
        with open(result["log_path"], encoding="utf-8") as fh:
            self.assertNotIn("Documents", fh.read())

    def test_documents_whose_contents_were_not_read_make_no_documents_digest(self):
        self.add_bytes("reports/report.eml", make_eml_with_files(
            "Geotech report", "Mon, 10 Mar 2025 12:00:00 +0000", [("Report.pdf", b"BROKEN pdf")]))
        os.remove(self.resent)
        result = self.run_it(docs_folder="")
        self.assertEqual(self.outputs(), ["Squish - Test Project - 2025-02-14 to 2025-04-01.txt"])
        self.assertFalse(self.doc_digest.called)
        self.assertNotIn("doc_ids", self.digest.call_args[1])
        self.assertEqual(result["doc_problems"], [["Report.pdf (attached to %s)" % self.report,
                                                   "damaged file"]])

    def test_attachments_only_or_folder_only(self):
        self.run_it(docs_folder="")
        self.assertEqual([d["name"] for d in self.docs_passed()],
                         ["Geotech report.pdf", "Old spec.doc", "Calc.xlsx"])
        self.assertEqual(self.doc_digest.call_args[1]["source_label"], "%s (attachments)" % self.src)
        self.run_it(docs_from_attachments=False)
        self.assertEqual([(d["id"], d["name"]) for d in self.docs_passed()],
                         [("D1", "Geotech copy.pdf"), ("D2", "Notes.txt"), ("", "Site plan.dwg"),
                          ("D3", "Deep.docx")])
        self.assertNotIn("doc_ids", self.digest.call_args[1])
        self.assertEqual(self.doc_digest.call_args[1]["source_label"], self.docs_dir)
        # Without subfolders, the subfolder's file is left out.
        self.run_it(docs_from_attachments=False, docs_include_subfolders=False)
        self.assertNotIn("Deep.docx", [d["name"] for d in self.docs_passed()])

    def test_ids_follow_email_dates_not_file_names(self):
        self.add_bytes("a first.eml", make_eml_with_files(
            "Later email", "Fri, 14 Mar 2025 12:00:00 +0000", [("Later.pdf", b"later")]))
        self.add_bytes("z last.eml", make_eml_with_files(
            "Early email", "Sat, 01 Mar 2025 12:00:00 +0000", [("Early.pdf", b"early")]))
        self.add_bytes("undated.eml", make_eml_with_files(
            "No date", "", [("Undated.pdf", b"undated")]).replace(b"Date: \n", b""))
        self.run_it(docs_folder="")
        self.assertEqual([d["name"] for d in self.docs_passed() if d["id"]],
                         ["Early.pdf", "Geotech report.pdf", "Calc.xlsx", "Later.pdf", "Undated.pdf"])
        self.assertEqual([d["id"] for d in self.docs_passed() if d["id"]],
                         ["D1", "D2", "D3", "D4", "D5"])
        # The same input gives the same IDs.
        first = [(d["id"], d["sha1"]) for d in self.docs_passed()]
        self.run_it(docs_folder="")
        self.assertEqual([(d["id"], d["sha1"]) for d in self.docs_passed()], first)

    def test_one_email_filed_twice_is_one_source(self):
        with open(self.report, "rb") as fh:
            self.add_bytes("copies/report copy.eml", fh.read())
        self.run_it(docs_folder="")
        geotech = self.docs_passed()[0]
        self.assertEqual([s["subject"] for s in geotech["sources"]],
                         ["Geotech report", "RE: Geotech report"])
        self.assertEqual(self.digest.call_args[1]["doc_ids"][
            (self.files["copies/report copy.eml"], 0)], "D1")

    def test_big_attachments_and_unreadable_contents_are_other_files(self):
        with mock.patch.object(docs, "DOC_MAX_BYTES", 40):
            self.run_it(docs_folder="")
        passed = dict((d["name"], d) for d in self.docs_passed())
        self.assertEqual(passed["Geotech report.pdf"]["doc"]["status"], "too_big")
        self.assertEqual(passed["Geotech report.pdf"]["id"], "")
        self.assertEqual(passed["Calc.xlsx"]["id"], "D1")
        # Not read, so the copy sent under another name can't be known to be the same.
        self.assertEqual(passed["Geotech report (1).pdf"]["doc"]["status"], "too_big")
        self.assertEqual(self.last["stats"]["doc_failed"], 2)
        self.assertEqual(self.last["doc_problems"][0],
                         ["Geotech report.pdf (attached to %s)" % self.report,
                          "larger than 0 MB, not read"])

    def test_date_filter_applies_to_attachments_not_loose_files(self):
        result = self.run_it(date_from="2025-03-11")
        passed = self.docs_passed()
        self.assertEqual([(d["id"], d["name"]) for d in passed], [
            ("D1", "Geotech report (1).pdf"), ("D2", "Calc.xlsx"), ("D3", "Notes.txt"),
            ("", "Site plan.dwg"), ("D4", "Deep.docx")])
        self.assertEqual([s["kind"] for s in passed[0]["sources"]], ["email", "file"])
        names = [os.path.basename(f["path"]) for f in result["files"]]
        self.assertEqual(names[1], "Squish - Test Project - documents - 2025-03-11 to 2025-03-12 "
                                   "(only from 2025-03-11).txt")      # (the loose files' date counts)
        # The documents used by emails outside the dates stay in the cache.
        found, _files = engine.load_docs_cache(engine.docs_cache_path(self.project()))
        self.assertEqual(len(found), 4)

    def test_focus_keeps_matching_documents_and_those_shown_in_the_email_digest(self):
        self.digest.side_effect = focus_build_digest
        self.add_loose("Pile report.pdf", b"Pile capacity and geotechnical notes")
        result = self.run_it(focus_keywords="geotech")
        # Geotech report: name and text; Calc.xlsx: attached to a kept email (=D2
        # in the email digest); Pile report: text ('geotechnical'). Notes, the
        # deep doc and the unread files have no keyword.
        self.assertEqual([d["name"] for d in self.docs_passed()],
                         ["Geotech report.pdf", "Calc.xlsx", "Pile report.pdf"])
        self.assertEqual([d["id"] for d in self.docs_passed()], ["D1", "D2", "D4"])
        self.assertIn("=D2", self.text_of("emails"))
        self.assertNotIn("Notes.txt", [d["name"] for d in self.docs_passed()])   # D3: the example
        names = [os.path.basename(f["path"]) for f in result["files"]]
        self.assertEqual(names[1], "Squish - Test Project - documents - 2025-03-10 to 2025-03-12 "
                                   "(focus geotech).txt")
        # Focus keywords match like the email digest's: accents, word starts.
        self.run_it(focus_keywords="GROUNDWÄTER")
        self.assertEqual([d["name"] for d in self.docs_passed()], ["Geotech report.pdf"])

    def test_no_emails_left_still_writes_the_documents_digest(self):
        self.digest.side_effect = focus_build_digest
        result = self.run_it(focus_keywords="piles")
        self.assertEqual([d["name"] for d in self.docs_passed()], ["Deep.docx"])
        self.assertEqual([f["kind"] for f in result["files"]], ["documents"])
        self.assertEqual(self.outputs(), ["Squish - Test Project - documents - 2025-03-11 to 2025-03-11 "
                                          "(focus piles).txt"])


class DocumentCacheTests(DocumentTestBase):

    def test_second_run_extracts_nothing(self):
        first = self.run_it()
        before = self.snapshot()
        self.extract.reset_mock()
        again = self.run_it()
        self.assertEqual((again["files_read"], again["from_cache"]), (0, first["files_read"]))
        self.assertEqual(self.extracted(), [])
        self.assertEqual(self.docs_passed()[0]["sources"][-1]["kind"], "file")
        self.assertEqual(self.snapshot().keys(), before.keys())
        with open(again["log_path"], encoding="utf-8") as fh:
            self.assertIn("(0 condensed this run", fh.read())

    def test_changed_loose_file_is_read_again_but_same_content_is_not_extracted(self):
        self.run_it()
        copy = os.path.join(self.docs_dir, "Geotech copy.pdf")
        os.utime(copy, (1800000000, 1800000000))
        notes = os.path.join(self.docs_dir, "Notes.txt")
        with open(notes, "wb") as fh:
            fh.write(b"Site notes: new gate code")
        self.extract.reset_mock()
        real_open = open
        opened = []

        def spy_open(path, *args, **kwargs):
            opened.append(os.path.basename(str(path)))
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", spy_open):
            self.run_it()
        self.assertEqual(self.extracted(), ["Notes.txt"])
        self.assertIn("Geotech copy.pdf", opened)       # re-hashed: its time changed
        self.assertNotIn("Deep.docx", opened)           # unchanged: not opened at all
        self.assertIn("new gate code", self.docs_passed()[3]["doc"]["blocks"][0]["text"])

    def test_documents_switched_on_later_rereads_the_emails(self):
        off = self.run_it(docs_from_attachments=False, docs_folder="")
        self.assertEqual(off["files_read"], 6)
        on = self.run_it()
        self.assertEqual((on["files_read"], on["from_cache"]), (6, 0))
        entries = self.cache_entries()
        self.assertTrue(all(e["docs"] for e in entries.values()))
        report = [e for p, e in entries.items() if p == self.report][0]["record"]
        self.assertEqual(report["attachments"][0]["sha1"], hashlib.sha1(GEOTECH).hexdigest())
        self.assertEqual(report["attachments"][0]["doc_size"], len(GEOTECH))
        self.assertNotIn("sha1", report["attachments"][1])     # the photo
        # Switching documents off again uses the cache as it is.
        off = self.run_it(docs_from_attachments=False, docs_folder="")
        self.assertEqual(off["files_read"], 0)

    def test_attachment_bytes_are_never_cached(self):
        self.run_it()
        with gzip.open(str(engine.cache_path(self.project())), "rt", encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("_data", raw)
        self.assertNotIn("Allowable bearing", raw)      # the document's text lives in the docs cache
        with gzip.open(str(engine.docs_cache_path(self.project())), "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["version"], engine.DOCS_CACHE_VERSION)
        self.assertEqual(len(data["docs"]), 4)
        self.assertEqual(sorted(os.path.basename(p) for p in data["files"]),
                         ["Deep.docx", "Geotech copy.pdf", "Notes.txt"])
        entry = data["docs"][hashlib.sha1(GEOTECH).hexdigest()]
        self.assertEqual(entry["used"], datetime.now().date().isoformat())
        self.assertEqual(entry["doc"]["blocks"][0]["text"], GEOTECH.decode())

    def test_lost_documents_cache_rereads_only_emails_with_documents(self):
        self.run_it()
        os.remove(str(engine.docs_cache_path(self.project())))
        self.extract.reset_mock()
        again = self.run_it()
        self.assertEqual(again["files_read"], 2)      # the two emails with documents
        self.assertEqual(self.extracted(), ["Calc.xlsx", "Deep.docx", "Geotech report.pdf",
                                            "Notes.txt"])

    def test_corrupt_documents_cache_is_ignored(self):
        self.run_it()
        with open(str(engine.docs_cache_path(self.project())), "wb") as fh:
            fh.write(b"not gzip")
        again = self.run_it()
        self.assertEqual(len(again["files"]), 2)
        self.assertEqual(engine.load_docs_cache(engine.docs_cache_path(self.project()))[0].keys(),
                         set(d["sha1"] for d in self.docs_passed() if d["sha1"]))

    def test_unused_documents_are_dropped_after_30_days(self):
        path = os.path.join(self.tmp, "docs.json.gz")
        today = datetime(2026, 10, 6).date()
        found = {"a": {"doc": {"status": "ok"}, "used": "2026-09-06"},
                 "b": {"doc": {"status": "ok"}, "used": "2026-09-05"},
                 "c": {"doc": {"status": "ok"}, "used": "2026-10-06"}}
        files = {"/x/a.pdf": {"size": 1, "mtime": 1.0, "sha1": "a"},
                 "/x/b.pdf": {"size": 1, "mtime": 1.0, "sha1": "b"}}
        self.assertTrue(engine.save_docs_cache(path, found, files, today=today))
        kept, kept_files = engine.load_docs_cache(path)
        self.assertEqual(sorted(kept), ["a", "c"])
        self.assertEqual(sorted(kept_files), ["/x/a.pdf"])
        # A version from another Squish is ignored.
        engine._save_json_gz(path, {"version": 999, "docs": found, "files": files})
        self.assertEqual(engine.load_docs_cache(path), ({}, {}))

    def test_cancel_while_documents_are_read_keeps_what_was_done(self):
        cancel = threading.Event()

        def extract_then_cancel(name, data=None, path=None):
            if name == "Notes.txt":
                cancel.set()
            return fake_extract(name, data, path)

        self.extract.side_effect = extract_then_cancel
        with mock.patch.object(engine, "READ_WORKERS", 1):
            result = engine.run_project(self.project(), cancel=cancel)
        self.assertTrue(result["cancelled"])
        self.assertEqual(self.outputs(), [])
        self.assertFalse(self.doc_digest.called)
        found, _files = engine.load_docs_cache(engine.docs_cache_path(self.project()))
        self.assertIn(hashlib.sha1(GEOTECH).hexdigest(), found)
        self.assertIn(hashlib.sha1(CALC).hexdigest(), found)

    def test_at_most_a_few_documents_are_condensed_at_once(self):
        for i in range(12):
            self.add_bytes("many/m%02d.eml" % i, make_eml_with_files(
                "Many %02d" % i, "Thu, 06 Mar 2025 12:00:00 +0000",
                [("Doc %02d.pdf" % i, b"doc %d" % i)]))
        lock = threading.Lock()
        busy = {"now": 0, "most": 0}

        def slow_extract(name, data=None, path=None):
            if data is None:       # only works out the kind of an unread file: instant
                return fake_extract(name, data, path)
            with lock:
                busy["now"] += 1
                busy["most"] = max(busy["most"], busy["now"])
            time.sleep(0.02)
            with lock:
                busy["now"] -= 1
            return fake_extract(name, data, path)

        self.extract.side_effect = slow_extract
        self.run_it()
        self.assertEqual(busy["most"], engine.EXTRACT_AT_ONCE)
        self.assertEqual(len([d for d in self.docs_passed() if d["id"]]), 16)


class DocumentSafetyTests(DocumentTestBase):

    def test_documents_never_stop_a_run(self):
        def broken_extract(name, data=None, path=None):
            if data is not None and name == "Calc.xlsx":
                raise RuntimeError("bug in a reader")
            return fake_extract(name, data, path)

        self.extract.side_effect = broken_extract
        result = self.run_it()
        calc = [d for d in self.docs_passed() if d["name"] == "Calc.xlsx"][0]
        self.assertEqual(calc["doc"]["status"], "error")
        self.assertEqual(calc["id"], "")
        self.assertEqual(result["stats"]["doc_failed"], 1)
        self.assertIn(["Calc.xlsx (attached to %s)" % self.resent,
                       "could not read this file (RuntimeError)"], result["doc_problems"])
        with open(result["log_path"], encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("Documents that could not be read (1):\n  Calc.xlsx (attached to", log)

    def test_failed_documents_digest_leaves_the_email_digest_without_marks(self):
        self.doc_digest.side_effect = ValueError("docdigest bug")
        result = self.run_it()
        self.assertEqual(self.outputs(), ["Squish - Test Project - 2025-02-14 to 2025-04-01.txt"])
        self.assertIn("doc_ids", self.digest.call_args_list[0][1])
        self.assertNotIn("doc_ids", self.digest.call_args[1])     # built again without =Dn
        self.assertEqual(result["stats"]["documents"], 0)
        with open(result["log_path"], encoding="utf-8") as fh:
            self.assertIn("The documents digest could not be made (ValueError: docdigest bug)",
                          fh.read())

    def test_missing_documents_folder_is_reported_not_fatal(self):
        result = self.run_it(docs_folder=os.path.join(self.tmp, "gone"))
        self.assertEqual(len(result["files"]), 2)      # emails, and the attachments' documents
        self.assertEqual(len(result["doc_problems"]), 1)
        folder, message = result["doc_problems"][0]
        self.assertEqual(folder, os.path.join(self.tmp, "gone"))
        self.assertTrue(engine.is_doc_folder_problem(message))
        self.assertFalse(engine.is_folder_problem(message))
        self.assertEqual(result["failed"], [])         # not an email problem

    def test_output_folder_inside_the_documents_folder_is_skipped(self):
        out = os.path.join(self.docs_dir, "Squish output")
        self.add_loose("Squish output/leftover.pdf", b"not a project document")
        engine.run_project(self.project(output_folder=out))
        names = [d["name"] for d in self.docs_passed()]
        self.assertNotIn("leftover.pdf", names)
        self.assertIn("Notes.txt", names)
        # The documents folder being the output folder: nothing is read from it.
        result = engine.run_project(self.project(output_folder=self.docs_dir))
        self.assertEqual(result["doc_problems"][0][0], self.docs_dir)
        self.assertTrue(engine.is_doc_folder_problem(result["doc_problems"][0][1]))

    def test_documents_folder_may_be_the_email_folder(self):
        with open(os.path.join(self.src, "Loose report.pdf"), "wb") as fh:
            fh.write(b"loose report text")
        engine.run_project(self.project(docs_folder=self.src, docs_from_attachments=False))
        self.assertEqual([d["name"] for d in self.docs_passed()], ["Loose report.pdf", "notes.txt"])

    def test_scan_documents_skips_what_it_should(self):
        found = engine.scan_documents(self.docs_dir)
        self.assertEqual([os.path.relpath(p, self.docs_dir) for p, _m, _s in found],
                         ["Geotech copy.pdf", "Notes.txt", "Site plan.dwg",
                          os.path.join("sub", "Deep.docx")])
        self.assertTrue(all(isinstance(m, float) and s > 0 for _p, m, s in found))
        flat = engine.scan_documents(self.docs_dir, include_subfolders=False)
        self.assertEqual(len(flat), 3)
        skipped = engine.scan_documents(self.docs_dir, skip_folder=os.path.join(self.docs_dir, "sub"))
        self.assertEqual(len(skipped), 3)
        cancel = threading.Event()
        cancel.set()
        self.assertIsNone(engine.scan_documents(self.docs_dir, cancel=cancel))


class DocumentOutputTests(DocumentTestBase):

    def test_email_only_runs_never_delete_the_documents_digest(self):
        self.run_it()
        docs_file = "Squish - Test Project - documents - 2025-03-10 to 2025-03-12.txt"
        self.assertIn(docs_file, self.outputs())
        self.run_it(docs_from_attachments=False, docs_folder="")
        self.assertEqual(self.outputs(), ["Squish - Test Project - 2025-02-14 to 2025-04-01.txt",
                                          docs_file])
        # A new email with a document: the documents digest is replaced, the
        # email digest (same name) rewritten, nothing else left behind.
        self.add_bytes("reports/later.eml", make_eml_with_files(
            "Revised calc", "Thu, 20 Mar 2025 12:00:00 +0000", [("Calc rev B.xlsx", b"Rev B")]))
        self.run_it()
        self.assertEqual(self.outputs(), [
            "Squish - Test Project - 2025-02-14 to 2025-04-01.txt",
            "Squish - Test Project - documents - 2025-03-10 to 2025-03-20.txt"])

    def test_documents_runs_never_delete_the_email_digest_of_another_kind(self):
        self.run_it()
        self.run_it(date_from="2025-03-11")
        self.assertEqual(self.outputs(), [
            "Squish - Test Project - 2025-02-14 to 2025-04-01.txt",
            "Squish - Test Project - 2025-03-12 to 2025-04-01 (only from 2025-03-11).txt",
            "Squish - Test Project - documents - 2025-03-10 to 2025-03-12.txt",
            "Squish - Test Project - documents - 2025-03-11 to 2025-03-12 (only from 2025-03-11).txt"])
        manifest = json.loads(Path(str(engine.manifest_path(self.project()))).read_text())
        kinds = sorted(list(manifest["folders"].values())[0])
        self.assertEqual(kinds, ["", "documents", "documents | from 2025-03-11 | to end | focus -",
                                 "from 2025-03-11 | to end | focus -"])

    def test_previous_files_stand_in_for_a_lost_record_per_kind(self):
        first = self.run_it()
        os.remove(str(engine.manifest_path(self.project())))
        stale_docs = os.path.join(self.out, "Squish - Old Name - documents - "
                                            "2025-01-01 to 2025-01-02.txt")
        Path(stale_docs).write_text("old documents digest")
        previous = [f["path"] for f in first["files"]] + [stale_docs]
        self.run_it(previous_files=previous, docs_folder="")
        self.assertNotIn(os.path.basename(stale_docs), self.outputs())
        self.assertEqual(len(self.outputs()), 2)

    def test_file_names_and_kinds(self):
        parts = [{"first_date": "2025-01-02", "last_date": "2025-03-04"}]
        self.assertEqual(engine.output_filenames("P", parts, kind="documents"),
                         ["Squish - P - documents - 2025-01-02 to 2025-03-04.txt"])
        self.assertEqual(engine.output_filenames("P", parts * 2, "focus pump",
                                                 dates="only from 2025-01-01", kind="documents"),
                         ["Squish - P - documents - 2025-01-02 to 2025-03-04 (only from 2025-01-01) "
                          "(focus pump) (part %d of 2).txt" % n for n in (1, 2)])
        name = "Squish - P - documents - undated (part 1 of 2).txt"
        self.assertTrue(engine.is_old_output(name, "P", kind="documents"))
        self.assertFalse(engine.is_old_output(name, "P"))
        self.assertTrue(engine.is_digest_file_name(name))
        self.assertEqual(engine.digest_file_kind(name), "documents")
        self.assertEqual(engine.digest_file_kind("Squish - P - 2025-01-01 to 2025-01-02.txt"), "emails")
        self.assertEqual(engine.digest_file_kind("Report.pdf"), "")
        self.assertFalse(engine.is_old_output("Squish - P - 2025-01-01 to 2025-01-02.txt", "P",
                                              kind="documents"))
        self.assertEqual(engine.manifest_key("", "documents"), "documents")
        self.assertEqual(engine.manifest_key("from x", "documents"), "documents | from x")
        self.assertEqual(engine.manifest_key("from x"), "from x")


class DocumentCliTests(DocumentTestBase):

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_docs_options(self):
        code, out, err = self.run_cli("run", "--source", self.src, "--out", self.out,
                                      "--name", "Cli Docs", "--docs-folder", '"%s"' % self.docs_dir)
        self.assertEqual(code, 0, err)
        project = self.digest.call_args[0][1]
        self.assertEqual(project["docs_folder"], self.docs_dir)
        self.assertIn("Wrote 2 file(s)", out)
        self.assertIn("Squish - Cli Docs - documents - 2025-03-10 to 2025-03-12.txt", out)
        self.assertIn("Documents: 4 documents, 2 other files", out)
        code, out, err = self.run_cli("run", "--source", self.src, "--out", self.out,
                                      "--name", "Cli Docs", "--no-docs")
        self.assertEqual(code, 0, err)
        self.assertFalse(self.digest.call_args[0][1]["docs_from_attachments"])
        self.assertNotIn("Documents:", out)
        self.assertNotIn("doc_ids", self.digest.call_args[1])

    def test_summary_lines_for_document_problems(self):
        result = {"files": [], "output_folder": "/x", "doc_problems": [
                      ["/x/04 Reports", "documents folder not found: check the VPN"],
                      ["Report.pdf (attached to /x/a.msg)", "damaged file"]],
                  "stats": {"emails_used": 1, "documents": 1, "doc_drawings": 2, "doc_other": 0,
                            "doc_versions": 1, "doc_failed": 1}}
        text = cli._format_summary({"name": "Job"}, result)
        self.assertIn("Documents: 1 document, 2 drawings (1 later version shown as changes)", text)
        self.assertIn("Documents folder /x/04 Reports: documents folder not found", text)
        self.assertIn("1 document could not be read (see the run log)", text)


if __name__ == "__main__":
    unittest.main()
