"""Tests for gui.py.

The helper functions need tkinter to import gui.py but no screen. The window
tests (SquishApp) also need a display; they are skipped without one (run them
under Xvfb on Linux: DISPLAY=:99).
"""

import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from squish_app import paths, projects

try:
    import tkinter as tk
    from squish_app import gui
except ImportError:  # this Python has no tkinter
    tk = None
    gui = None


def has_display():
    """True if a Tk window can be opened here."""
    if gui is None:
        return False
    try:
        tk.Tk().destroy()
    except tk.TclError:
        return False
    return True


class TempDataDir(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="squish-gui-test-")
        patcher = mock.patch.dict(os.environ, {"SQUISH_DATA_DIR": self.data_dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.data_dir, True)


@unittest.skipIf(gui is None, "tkinter is not available")
class HelperTests(TempDataDir):
    def test_guessed_name_keeps_the_job_number(self):
        sep = os.sep
        self.assertEqual(gui.guess_project_name(
            sep.join(["H:", "Projects", "1234 Riverside Depot Upgrade", "01 Emails"])),
            "1234 Riverside Depot Upgrade")
        self.assertEqual(gui.guess_project_name(
            sep.join(["H:", "Jobs", "640.12345 Harbour Rd"])), "640.12345 Harbour Rd")
        self.assertEqual(gui.guess_project_name(
            sep.join(["H:", "Job", "02 Correspondence"])), "Job")
        self.assertEqual(gui.guess_project_name(
            sep.join(["H:", "Pump Station", "2024", "05"])), "Pump Station")
        self.assertEqual(gui.guess_project_name("H:\\"), "")

    def test_guessed_name_from_a_pasted_path(self):
        # Explorer's 'Copy as path' puts the path in double quotes.
        pasted = '"H:\\Projects\\1234 Riverside Depot Upgrade\\01 Emails"'
        self.assertEqual(gui.guess_project_name(paths.clean_folder_text(pasted)),
                         "1234 Riverside Depot Upgrade")
        self.assertEqual(gui.guess_project_name(paths.clean_folder_text('  "H:\\"  ')), "")

    def test_filter_summary(self):
        self.assertEqual(gui.filter_summary({}), "")
        self.assertEqual(gui.filter_summary({"date_from": "", "date_to": "",
                                             "focus_keywords": " , "}), "")
        self.assertEqual(gui.filter_summary({"date_from": "2025-02-01", "date_to": "2025-02-28"}),
                         "only 2025-02-01 to 2025-02-28")
        self.assertEqual(gui.filter_summary({"date_from": "2025-02-01"}), "only from 2025-02-01")
        self.assertEqual(gui.filter_summary({"date_to": "2025-02-28"}), "only up to 2025-02-28")
        self.assertEqual(gui.filter_summary({"focus_keywords": "pump, culvert\nRFI 12"}),
                         "only conversations mentioning pump, culvert, RFI 12")
        self.assertEqual(gui.filter_summary({"date_from": "2025-02-01", "date_to": "2025-02-28",
                                             "focus_keywords": "pump"}),
                         "only 2025-02-01 to 2025-02-28 and only conversations mentioning pump")

    def test_done_message(self):
        one = [{"est_tokens": 90000}]
        text = gui.done_message(3.2, one, True)
        self.assertTrue(text.startswith("Done in 3 s - 1 file ready."))
        self.assertIn("Show in folder", text)
        self.assertIn("Copy file", text)
        self.assertNotIn("Copy file", gui.done_message(3, one, False))
        few = gui.done_message(9, [{"est_tokens": 30000}] * 2, True)
        self.assertIn("2 files ready - they fit in one Claude chat", few)
        self.assertIn("Show in folder", few)
        many = gui.done_message(9, [{"est_tokens": 120000}] * 3, True)
        self.assertIn("new Claude chat for each file", many)
        other = gui.done_message(1, one, True, other_project="Alpha",
                                 filters="only conversations mentioning pump")
        self.assertTrue(other.endswith(' - only conversations mentioning pump (project "Alpha").'))
        # The tip and the Done message use the same one-chat rule.
        for files in ([{"est_tokens": 75000}] * 2, [{"est_tokens": 75001}] * 2):
            self.assertEqual("one Claude chat" in gui.done_message(1, files, True),
                             "one Claude chat" in gui.results_tip(files, True))

    def test_small_files_always_get_one_chat_each(self):
        # People choose Small when Claude said the file was too big: never tell
        # them the Small files all fit in one chat.
        files = [{"est_tokens": 40000}] * 3
        for text in (gui.done_message(1, files, True, part_size="small"),
                     gui.results_tip(files, True, part_size="small")):
            self.assertIn("new Claude chat for each file", text)
            self.assertNotIn("one Claude chat", text)
        self.assertIn("only the ones you need", gui.results_tip(files, True, part_size="small"))
        for text in (gui.done_message(1, files, True, part_size="medium"),
                     gui.results_tip(files, True, part_size="medium")):
            self.assertIn("one Claude chat", text)
        self.assertFalse(gui.fit_one_chat(files, "small"))
        self.assertTrue(gui.fit_one_chat(files, "large"))

    def test_short_name(self):
        self.assertEqual(gui.short_name("Harbour Road"), "Harbour Road")
        exact = "x" * 30
        self.assertEqual(gui.short_name(exact), exact)
        long_name = "Riverside Depot Stormwater Upgrade Stage 2 - Detailed Design"
        short = gui.short_name(long_name)
        self.assertEqual(len(short), 29)
        self.assertTrue(short.endswith("…"))
        self.assertTrue(long_name.startswith(short[:-1]))
        self.assertEqual(gui.short_name("Harbour Road Upgrade", 10), "Harbour…")
        # The 'Cancel "<name>"' button keeps its old rule (18 characters).
        for name in ("Alpha", "x" * 18, "x" * 19, "Riverside Depot Upgrade", "Riverside  Depot  Up"):
            old_rule = name if len(name) <= 18 else name[:16].rstrip() + "…"
            self.assertEqual(gui.short_name(name, 18), old_rule)

    def test_summary_counts_auto_replies_in_good_english(self):
        def text(n):
            return gui.summary_text({"files": [{"path": "x.txt", "est_tokens": 10}],
                                     "files_found": 9, "stats": {"noise_dropped": n}})
        self.assertIn("1 auto-reply or notification skipped.", text(1))
        self.assertIn("2 auto-replies or notifications skipped.", text(2))

    def test_split_status(self):
        self.assertEqual(gui.split_status("Done in 3 s."), ("Done in 3 s.", ""))
        self.assertEqual(gui.split_status(""), ("", ""))
        message = "Squish stopped because x.\n\nNothing was changed."
        self.assertEqual(gui.split_status(message), ("Squish stopped because x.", message))
        message = "Can't find the email folder:\nH:\\Jobs\\01 Emails\n\nCheck the VPN."
        self.assertEqual(gui.split_status(message), ("Can't find the email folder", message))

    def test_run_was_filtered(self):
        def result(name, outside=0):
            return {"files": [{"path": os.path.join("x", name)}],
                    "stats": {"outside_dates": outside}}
        self.assertFalse(gui.run_was_filtered(result("Squish - A - 2025-01-01 to 2025-02-01.txt")))
        self.assertTrue(gui.run_was_filtered(result("Squish - A - 2025-01-01 to 2025-02-01.txt", 4)))
        self.assertTrue(gui.run_was_filtered(
            result("Squish - A - 2025-01-01 to 2025-02-01 (focus pump) (part 1 of 2).txt")))
        # A dated run's ' (only ...)' tag
        for tag in ("(only 2025-01-01 to 2025-01-31)", "(only from 2025-01-15)",
                    "(only up to 2025-01-31)", "(only 2025-01-01 to 2025-01-31) (focus x) "
                    "(part 1 of 2)"):
            self.assertTrue(gui.run_was_filtered(
                result("Squish - A - 2025-01-02 to 2025-01-30 %s.txt" % tag)), tag)
        # ... but not a project that happens to be called '(only ...)'
        self.assertFalse(gui.run_was_filtered(
            result("Squish - Job (only stage 1) - 2025-01-02 to 2025-01-30.txt")))

    def test_summary_counts_folders_and_emails_left_out(self):
        result = {"files": [{"path": "x.txt", "est_tokens": 1000}], "files_found": 5,
                  "stats": {"emails_used": 3, "threads": 2, "no_access_emails": 40},
                  "failed": [["H:\\Job\\Locked", "no access to this folder: Access is denied - "
                              "it held 40 emails last time; they are not in this digest"],
                             ["H:\\Job\\a.msg", "could not be read"]]}
        text = gui.summary_text(result)
        self.assertIn("1 file couldn't be read", text)
        self.assertIn("1 folder couldn't be opened", text)
        self.assertIn("40 emails in a folder Squish can't open are left out (see run log).", text)
        # A saved last_run keeps only the counts, and reads the same.
        saved = projects.last_run_from_result(result)
        self.assertEqual((saved["failed_count"], saved["failed_folders"]), (2, 1))
        self.assertEqual(gui.summary_text(saved), text)
        self.assertEqual(gui.failure_counts({"failed_count": 3}), (3, 0))   # an older last_run

    def test_output_folder_inside_the_emails_folder(self):
        src = tempfile.mkdtemp(dir=self.data_dir)
        self.assertTrue(gui.output_folder_problem(src, src))
        self.assertTrue(gui.output_folder_problem(src, os.path.join(src, "Digests"),
                                                  resolve_links=False))
        self.assertEqual(gui.output_folder_problem(src, os.path.join(self.data_dir, "out")), "")
        self.assertEqual(gui.output_folder_problem(src, ""), "")      # blank = the default folder
        # The window only compares the folders as typed: resolving links can hang
        # on a disconnected network drive (the run checks again, in its thread).
        with mock.patch("os.path.realpath", side_effect=AssertionError("realpath")):
            self.assertEqual(gui.output_folder_problem(src, os.path.join(self.data_dir, "o")), "")

    def test_count_reports_a_folder_it_cannot_open(self):
        folder = tempfile.mkdtemp(dir=self.data_dir)
        real_scandir = os.scandir

        def scandir(path="."):
            if os.path.abspath(path) == os.path.abspath(paths.long_path(folder)):
                raise PermissionError(13, "Access is denied", path)
            return real_scandir(path)

        with mock.patch("os.scandir", side_effect=scandir):
            for subfolders in (True, False):
                with self.assertRaises(OSError):
                    gui.count_email_files(folder, subfolders, threading.Event(), lambda n: None)

    def test_count_lists_subfolders_it_cannot_open(self):
        folder = tempfile.mkdtemp(dir=self.data_dir)
        for sub in ("a", "locked"):
            os.makedirs(os.path.join(folder, sub))
        for name in ("a/1.msg", "a/2.eml", "locked/3.msg", "4.msg", "~$5.msg", "notes.txt"):
            with open(os.path.join(folder, name), "w") as fh:
                fh.write("x")
        self.assertEqual(gui.count_email_files(folder, True, threading.Event(), lambda n: None), 4)
        real_scandir = os.scandir

        def scandir(path="."):
            if os.path.basename(str(path)) == "locked":
                raise PermissionError(13, "Access is denied", path)
            return real_scandir(path)

        skipped = []
        with mock.patch("os.scandir", side_effect=scandir):
            count = gui.count_email_files(folder, True, threading.Event(), lambda n: None, skipped)
        self.assertEqual(count, 3)
        self.assertEqual([os.path.basename(p) for p in skipped], ["locked"])

    def test_results_tip(self):
        one = [{"est_tokens": 90000}]
        self.assertEqual(gui.results_tip([], True), gui.TIP)
        self.assertEqual(gui.results_tip(one, True), gui.TIP)
        self.assertEqual(gui.results_tip(one, False), gui.TIP_NO_COPY)
        big = gui.results_tip([{"est_tokens": 120000}] * 3, True)
        self.assertTrue(big.startswith("3 files, oldest conversations first"))
        self.assertIn("new Claude chat for each file", big)
        self.assertIn("Copy file", big)
        self.assertNotIn("Copy file", gui.results_tip([{"est_tokens": 120000}] * 3, False))
        small = gui.results_tip([{"est_tokens": 40000}] * 3, True)
        self.assertIn("small enough for one Claude chat", small)

    def test_short_file_name(self):
        for name, short in (
                ("Squish - Job - 2024-01-02 to 2025-03-04.txt", "2024-01-02 to 2025-03-04.txt"),
                ("Squish - A - B - undated (part 2 of 3).txt", "undated (part 2 of 3).txt"),
                ("Squish - Job - 2024-01-02 to 2025-03-04 (focus culvert) (part 1 of 2).txt",
                 "2024-01-02 to 2025-03-04 (focus culvert) (part 1 of 2).txt"),
                ("Squish - Very long name ~1a2b3c - undated.txt", "undated.txt"),
                ("Squish - Job - 2025-01-19 to 2025-01-31 (only 2025-01-15 to 2025-01-31).txt",
                 "2025-01-19 to 2025-01-31 (only 2025-01-15 to 2025-01-31).txt"),
                ("Squish - Job - 2025-01-19 to 2025-02-02 (only from 2025-01-15) (focus pump) "
                 "(part 1 of 2).txt",
                 "2025-01-19 to 2025-02-02 (only from 2025-01-15) (focus pump) (part 1 of 2).txt"),
                ("Squish - Job - undated (only up to 2025-01-31).txt",
                 "undated (only up to 2025-01-31).txt"),
                ("notes.txt", "notes.txt")):
            self.assertEqual(gui.short_file_name(os.path.join("x", name)), short)

    def test_fit_middle(self):
        measure = len      # one pixel per character
        self.assertEqual(gui.fit_middle("Harbour Road", measure, 20), "Harbour Road")
        text = "Riverside Depot Stormwater Upgrade Stage 2"
        short = gui.fit_middle(text, measure, 30)
        self.assertLessEqual(len(short), 30)
        self.assertIn("\u2026", short)
        self.assertTrue(short.startswith("Riverside"))
        self.assertTrue(short.endswith("Stage 2"))
        self.assertEqual(gui.fit_middle(text, measure, 0), text)   # not drawn yet

    def test_long_paths_only_matter_on_windows(self):
        long_path = os.path.join(os.sep + "x" * 150, "y" * 120 + ".txt")
        with mock.patch.object(gui, "IS_WINDOWS", True):
            self.assertTrue(gui.path_too_long_for_explorer(long_path))
            self.assertFalse(gui.path_too_long_for_explorer(os.path.join(os.sep + "x", "y.txt")))
        with mock.patch.object(gui, "IS_WINDOWS", False):
            self.assertFalse(gui.path_too_long_for_explorer(long_path))


@unittest.skipIf(gui is None, "tkinter is not available")
class DocumentHelperTests(TempDataDir):
    """The Documents tab's helpers and the wording for runs with a documents file."""

    EMAILS = {"path": os.path.join("out", "Squish - Job - 2024-08-22 to 2025-11-30.txt"),
              "est_tokens": 60000, "emails": 1876, "kind": "emails"}
    DOCS = {"path": os.path.join("out", "Squish - Job - documents - 2024-08-22 to 2025-11-30.txt"),
            "est_tokens": 40000, "documents": 64, "kind": "documents"}

    def make_files(self, folder, names):
        for name in names:
            path = os.path.join(folder, *name.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write("x")

    def test_count_documents_and_other_files(self):
        folder = tempfile.mkdtemp(dir=self.data_dir)
        self.make_files(folder, [
            "Geotech report.pdf", "Cost plan.xlsx", "Minutes.docx", "Slides.pptx", "Notes.txt",
            "Survey.csv", "Spec.rtf", "Photos.zip",                     # 8 documents
            "Old report.doc", "Site plan.dwg", "IMG_0012.jpg",           # 3 other files
            "Email.msg", "Reply.eml", "~$Minutes.docx", ".hidden.pdf",   # never counted
            "Squish - Job - 2025-01-01 to 2025-01-02.txt",              # a digest file
            "Sub/Drainage report.pdf", "Sub/Sketch.png",
            "Digests/Squish - Job - documents - undated.txt", "Digests/Kept.pdf"])
        stop = threading.Event()
        self.assertEqual(gui.count_document_files(folder, True, stop, lambda n: None), (10, 4, 0))
        self.assertEqual(gui.count_document_files(folder, False, stop, lambda n: None), (8, 3, 0))
        # The output folder (and everything in it) is left out, as in a run.
        out = os.path.join(folder, "Digests")
        self.assertEqual(gui.count_document_files(folder, True, stop, lambda n: None,
                                                  skip_folder=out), (9, 4, 0))
        # A folder of emails only (e.g. the emails folder itself): its email files are counted.
        emails = tempfile.mkdtemp(dir=self.data_dir)
        self.make_files(emails, ["a.msg", "b.eml", "Sub/c.msg", "~$d.msg"])
        self.assertEqual(gui.count_document_files(emails, True, stop, lambda n: None), (0, 0, 3))
        self.assertEqual(gui.count_document_files(emails, False, stop, lambda n: None), (0, 0, 2))
        # With the Emails tab's folder: only the emails the email scan reads are left
        # out; saved emails elsewhere are other files (listed, as in a run).
        self.assertEqual(gui.count_document_files(folder, True, stop, lambda n: None,
                                                  email_folder=emails), (10, 6, 0))
        self.assertEqual(gui.count_document_files(emails, True, stop, lambda n: None,
                                                  email_folder=emails), (0, 0, 3))
        self.assertEqual(gui.count_document_files(emails, True, stop, lambda n: None, email_folder=emails,
                                                  email_subfolders=False), (0, 1, 0))
        stop.set()
        self.assertIsNone(gui.count_document_files(folder, True, stop, lambda n: None))
        self.assertIsNone(gui.count_document_files(emails, True, stop, lambda n: None))

    def test_count_documents_reports_folders_it_cannot_open(self):
        folder = tempfile.mkdtemp(dir=self.data_dir)
        self.make_files(folder, ["a.pdf", "Locked/b.pdf"])
        with self.assertRaises(FileNotFoundError):
            gui.count_document_files(os.path.join(folder, "gone"), True, threading.Event(),
                                     lambda n: None)
        real_scandir = os.scandir

        def scandir(path="."):
            if os.path.basename(str(path)) == "Locked":
                raise PermissionError(13, "Access is denied", path)
            return real_scandir(path)

        skipped = []
        with mock.patch("os.scandir", side_effect=scandir):
            counts = gui.count_document_files(folder, True, threading.Event(), lambda n: None,
                                              skipped)
        self.assertEqual(counts, (1, 0, 0))
        self.assertEqual([os.path.basename(p) for p in skipped], ["Locked"])

        def scandir_top(path="."):
            if os.path.abspath(str(path)) == os.path.abspath(paths.long_path(folder)):
                raise PermissionError(13, "Access is denied", path)
            return real_scandir(path)

        with mock.patch("os.scandir", side_effect=scandir_top):
            with self.assertRaises(OSError) as caught:
                gui.count_document_files(folder, True, threading.Event(), lambda n: None)
        self.assertIn("Access is denied", str(caught.exception))
        self.assertNotIn("folder could not be opened", str(caught.exception))

    def test_document_names_without_the_documents_code(self):
        self.assertTrue(gui.is_document_name("Report.PDF"))
        self.assertFalse(gui.is_document_name("Old report.doc"))
        # If the documents code can't be used, the same list of types still counts.
        with mock.patch("squish_app.docs.is_supported", side_effect=RuntimeError("broken")):
            self.assertTrue(gui.is_document_name("Cost plan.xlsx"))
            self.assertTrue(gui.is_document_name("Photos.ZIP"))
            self.assertFalse(gui.is_document_name("Site plan.dwg"))

    def test_docs_count_text(self):
        text, style = gui.docs_count_text(64, 31, 0, True)
        self.assertEqual(text, "64 documents found (incl. subfolders) - plus 31 other files, "
                               "listed, not read.")
        self.assertEqual(style, "Good")
        text, style = gui.docs_count_text(1, 0, 2, False)
        self.assertEqual(text, "1 document found (this folder only) (2 subfolders couldn't be "
                               "opened).")
        self.assertEqual(style, "Warn")
        text, style = gui.docs_count_text(0, 3, 0, False)
        self.assertIn("No Word, Excel, PowerPoint, PDF or text files here - just 3 other files",
                      text)
        # Other files alone make no documents file, so don't promise to list them.
        self.assertIn("which Squish can't read, so this folder alone won't make a documents "
                      "file.", text)
        self.assertNotIn("will be listed", text)
        self.assertIn("Tick 'Include subfolders'", text)
        self.assertEqual(style, "Warn")
        self.assertEqual(gui.docs_count_text(0, 0, 0, True), ("No files here.", "Warn"))
        text, style = gui.docs_count_text(gui.MANY_DOCUMENTS + 1, 0, 0, True)
        self.assertIn("takes a while", text)
        self.assertEqual(style, "Warn")
        # A folder of emails only (allowed: e.g. the emails folder) is not called empty.
        text, style = gui.docs_count_text(0, 0, 0, True, emails=5)
        self.assertEqual(text, "Only emails here (5 email files - Squish reads emails from the "
                               "Emails tab's folder), no other documents found.")
        self.assertEqual(style, "Hint")
        text, style = gui.docs_count_text(0, 0, 1, False, emails=1)
        self.assertIn("Only emails here (1 email file", text)
        self.assertIn("(1 subfolder couldn't be opened)", text)
        self.assertIn("Tick 'Include subfolders'", text)
        self.assertEqual(style, "Warn")
        self.assertIn("64 documents found", gui.docs_count_text(64, 0, 0, True, emails=5)[0])

    def test_blank_documents_folder_hint_follows_the_attachments_box(self):
        self.assertIn("Leave blank to condense only the attachments.",
                      gui.docs_folder_blank_hint(True))
        self.assertIn("Leave blank for no documents folder.", gui.docs_folder_blank_hint(False))
        self.assertNotIn("attachments", gui.docs_folder_blank_hint(False))

    def test_docs_folder_that_is_the_output_folder(self):
        out = os.path.join(self.data_dir, "Out")
        self.assertTrue(gui.docs_folder_problem(out, out))
        self.assertTrue(gui.docs_folder_problem(out + os.sep, out))
        # A folder inside the output folder is fine (the run skips only the output folder).
        self.assertEqual(gui.docs_folder_problem(os.path.join(out, "Sub"), out), "")
        self.assertEqual(gui.docs_folder_problem(os.path.join(self.data_dir, "Reports"), out), "")
        self.assertEqual(gui.docs_folder_problem("", out), "")

    def test_pdf_reader_text(self):
        self.assertEqual(gui.pdf_reader_text("PDF: pypdf 5.1.0"),
                         ("Installed (pypdf 5.1.0) - PDFs are read with the better reader.", "Hint"))
        text, style = gui.pdf_reader_text("PDF: built-in reader (install pypdf for best results)")
        self.assertEqual((text, style), (gui.PDF_ADVICE, "Hint"))
        self.assertTrue(text.startswith("PDFs are read with Squish's built-in reader. For better "
                                        "results,"))
        self.assertEqual(gui.pdf_reader_text("")[1], "Warn")       # documents code missing
        with mock.patch.dict(os.environ, {"SQUISH_NO_PYPDF": "1"}):
            self.assertEqual(gui.pdf_reader_text(gui.pdf_reader_status())[0], gui.PDF_ADVICE)
        with mock.patch("squish_app.docs.backend_status", side_effect=RuntimeError("broken")):
            self.assertEqual(gui.pdf_reader_status(), "")

    def test_file_kinds_and_short_names(self):
        self.assertEqual(gui.file_kind(self.DOCS), "documents")
        self.assertEqual(gui.file_kind(self.EMAILS), "emails")
        self.assertEqual(gui.file_kind({"path": "x.txt"}), "emails")   # saved before v1.1
        self.assertEqual(gui.short_file_name(self.DOCS["path"]),
                         "documents - 2024-08-22 to 2025-11-30.txt")
        self.assertEqual(gui.short_file_name(os.path.join(
            "x", "Squish - Job - documents - undated (focus pump) (part 2 of 2).txt")),
            "documents - undated (focus pump) (part 2 of 2).txt")
        self.assertEqual(gui.short_file_name(self.EMAILS["path"]), "2024-08-22 to 2025-11-30.txt")

    def test_emails_file_first_then_the_documents_file(self):
        files = [self.EMAILS, self.DOCS]                    # 100k tokens: one chat
        tip = gui.results_tip(files, True)
        self.assertTrue(tip.startswith("Drag in the emails file first; add the documents file "
                                       "when you need what the documents say."))
        self.assertIn("small enough for one Claude chat", tip)
        self.assertNotIn("same chat", tip)
        done = gui.done_message(5, files, True)
        self.assertIn("2 files ready - they fit in one Claude chat", done)
        self.assertIn("the emails file first; add the documents file when you need what the "
                      "documents say", done)
        big = [dict(self.EMAILS, est_tokens=130000), self.DOCS]
        tip = gui.results_tip(big, False)
        self.assertIn("new Claude chat for each file", tip)
        self.assertIn("Begin with the emails file; use the documents file when you need what the "
                      "documents say.", tip)
        self.assertNotIn("Copy file", tip)
        # Separate chats lose the link between the emails and their attachments: say so,
        # and how to get a pair that fits one chat.
        self.assertIn("Claude links emails to their attachments (the =D12 marks) only when both "
                      "files are in the same chat - for that, narrow the run with dates or Focus "
                      "keywords.", tip)
        done = gui.done_message(5, big, True)
        self.assertIn("use a new Claude chat for each file, starting with the emails file", done)
        self.assertIn("separate chats can't link emails to their attachments", done)
        self.assertIn("same chat", gui.results_tip([dict(f, est_tokens=1000) for f in files],
                                                   True, "small"))
        # Several parts of each: plurals, and a Small run is never one chat.
        parts = [self.EMAILS, self.EMAILS, self.DOCS, self.DOCS]
        for part_size in ("small", "medium"):
            tip = gui.results_tip([dict(f, est_tokens=10000) for f in parts], True, part_size)
            self.assertEqual("one Claude chat" in tip, part_size == "medium", tip)
        self.assertIn("the emails files first; add the documents files",
                      gui.results_tip([dict(f, est_tokens=10000) for f in parts], True))
        self.assertIn("only the ones you need", gui.results_tip(parts, True, "small"))
        # A run whose filters left no emails can write only a documents file.
        self.assertEqual(gui.results_tip([self.DOCS], True), gui.TIP)
        self.assertTrue(gui.results_tip([self.DOCS, self.DOCS], True).startswith(
            "2 documents files"))

    def test_done_message_warns_when_the_documents_folder_was_not_read(self):
        missing = {"files": [self.EMAILS, self.DOCS],
                   "doc_problems": [["H:\\Job\\Reports", "documents folder not found: check the VPN"],
                                    ["Report.pdf", "password-protected PDF"]]}
        self.assertEqual(gui.docs_folder_warning(missing),
                         " The documents folder (or a folder in it) couldn't be opened, so its "
                         "documents are missing - see run log.")
        self.assertEqual(gui.docs_folder_warning(projects.last_run_from_result(missing)),
                         gui.docs_folder_warning(missing))
        self.assertEqual(gui.docs_folder_warning({"files": [self.EMAILS], "doc_problems": [
            ["Report.pdf", "password-protected PDF"]]}), "")
        self.assertEqual(gui.docs_folder_warning({"files": [self.EMAILS]}), "")

    def test_documents_digest_that_could_not_be_made(self):
        # engine.run_project: the documents digest failed, the emails file was
        # written and the earlier documents file was kept (it is out of date).
        result = {"files": [self.EMAILS], "files_found": 5, "doc_digest_failed": True,
                  "stats": {"emails_used": 5, "threads": 2}, "failed": [], "doc_problems": []}
        sentence = (" The documents file couldn't be made this time (see run log), so only the "
                    "emails file was written - any documents file already in the folder is from "
                    "an earlier run.")
        self.assertEqual(gui.docs_digest_warning(result), sentence)
        self.assertTrue(gui.summary_text(result).endswith(sentence))
        saved = projects.last_run_from_result(result)
        self.assertEqual(gui.docs_digest_warning(saved), sentence)
        self.assertEqual(gui.summary_text(saved), gui.summary_text(result))
        self.assertIn("the emails files were written", gui.docs_digest_warning(
            dict(result, files=[self.EMAILS, self.EMAILS])))
        self.assertIn("couldn't be made either", gui.docs_digest_warning(dict(result, files=[])))
        # A normal run (or a last_run saved before 1.1) says nothing.
        for ok in ({"files": [self.EMAILS, self.DOCS]}, dict(result, doc_digest_failed=False),
                   {"files": [self.EMAILS], "stats": {"emails_used": 5}}):
            self.assertEqual(gui.docs_digest_warning(ok), "")
            self.assertNotIn("couldn't be made", gui.summary_text(ok))
        self.assertFalse(projects.last_run_from_result({"files": [self.EMAILS]})[
            "doc_digest_failed"])

    def test_no_documents_file_says_why(self):
        def run(found, **extra):
            stats = {"emails_used": 50, "threads": 5}
            if found is not None:
                stats["doc_found"] = found
            return dict({"files": [self.EMAILS], "files_found": 50, "stats": stats}, **extra)

        # A documents folder of photos, CAD and old .doc files only.
        self.assertEqual(gui.no_documents_note(run(7)),
                         "No documents file: none of the 7 files found could be read (see run "
                         "log).")
        self.assertIn("none of the 1 file found", gui.no_documents_note(run(1)))
        self.assertEqual(gui.no_documents_note(run(0)),
                         "No documents file: no documents were found.")
        focus = run(0, files=[dict(self.EMAILS, path="Squish - Job - undated (focus pump).txt")])
        self.assertEqual(gui.no_documents_note(focus),
                         "No documents file: no documents match the focus keywords.")
        text = gui.summary_text(run(7))
        self.assertTrue(text.endswith(" No documents file: none of the 7 files found could be "
                                      "read (see run log)."), text)
        self.assertEqual(gui.summary_text(projects.last_run_from_result(run(7))), text)
        # Unreadable documents among them: one sentence, not two.
        text = gui.summary_text(run(7, doc_problem_count=2))
        self.assertNotIn("2 documents couldn't be read", text)
        self.assertIn("none of the 7 files found could be read", text)
        # Documents not wanted (no doc_found), a documents file written, or a failed
        # documents digest (its own sentence): no note.
        self.assertEqual(gui.no_documents_note(run(None)), "")
        self.assertNotIn("No documents file", gui.summary_text(run(None)))
        self.assertEqual(gui.no_documents_note(run(7, files=[self.EMAILS, self.DOCS])), "")
        self.assertEqual(gui.no_documents_note(run(7, doc_digest_failed=True)), "")
        self.assertNotIn("No documents file", gui.summary_text(run(7, doc_digest_failed=True)))

    def test_documents_folder_that_was_the_output_folder(self):
        # engine._scan_documents_folder skips it: it is not "couldn't be opened".
        result = {"files": [self.EMAILS, self.DOCS], "files_found": 5, "failed": [],
                  "stats": {"emails_used": 5, "threads": 2, "documents": 3},
                  "doc_problems": [["C:\\Out", "documents folder not read: it is the output "
                                                "folder"]]}
        self.assertTrue(gui.docs_folder_is_output(result))
        self.assertEqual(gui.docs_folder_warning(result),
                         " The documents folder is the output folder, so it was skipped - see "
                         "run log.")
        text = gui.summary_text(result)
        self.assertIn("The documents folder is the output folder, so it was skipped (see run "
                      "log).", text)
        self.assertNotIn("couldn't be opened", text)
        # A saved last_run reads the same.
        saved = projects.last_run_from_result(result)
        self.assertTrue(saved["doc_folder_is_output"])
        self.assertTrue(gui.docs_folder_is_output(saved))
        self.assertEqual(gui.docs_folder_warning(saved), gui.docs_folder_warning(result))
        self.assertEqual(gui.summary_text(saved), text)
        # Other documents folder problems keep their wording.
        missing = dict(result, doc_problems=[["H:\\Reports", "documents folder not found: VPN?"]])
        self.assertFalse(gui.docs_folder_is_output(missing))
        self.assertFalse(gui.docs_folder_is_output(projects.last_run_from_result(missing)))
        self.assertIn("couldn't be opened", gui.docs_folder_warning(missing))
        self.assertIn("couldn't be opened", gui.summary_text(missing))
        self.assertFalse(gui.docs_folder_is_output({"files": [self.EMAILS]}))   # before 1.1

    def test_summary_mentions_the_documents(self):
        result = {"files": [self.EMAILS, self.DOCS], "files_found": 2345,
                  "stats": {"emails_used": 1876, "threads": 214, "documents": 64,
                            "doc_drawings": 9, "doc_other": 31, "doc_versions": 5,
                            "doc_failed": 3},
                  "failed": [],
                  "doc_problems": [["Old scan.pdf (attached to x.msg)", "no text"]] * 3 + [
                      ["H:\\Job\\Reports", "documents folder not found: check the VPN"]]}
        text = gui.summary_text(result)
        self.assertIn("2,345 emails → 1,876 used in 214 threads, 1 emails file, ~60k tokens.", text)
        # The documents that couldn't be read are among the other files listed: counted there.
        self.assertIn("73 documents (incl. 9 drawings) in 1 documents file, ~40k tokens, "
                      "plus 31 other files listed (3 of them couldn't be read - see run "
                      "log).", text)
        self.assertNotIn("3 documents couldn't be read", text)
        self.assertIn("The documents folder (or a folder in it) couldn't be opened", text)
        # A saved last_run keeps only the counts, and reads the same.
        self.assertEqual(gui.summary_text(projects.last_run_from_result(result)), text)
        # Drawings count as documents (as in the Contains column); a run without a
        # documents file says nothing about documents.
        self.assertIn("2 documents (incl. 2 drawings) in 1 documents file, ~40k tokens.",
                      gui.summary_text({"files": [self.EMAILS, self.DOCS], "stats": {"doc_drawings": 2}}))
        self.assertIn("1 document in 1 documents file, ~40k tokens.", gui.summary_text(
            {"files": [self.EMAILS, self.DOCS], "stats": {"documents": 1}}))
        # Every other file couldn't be read / only one other file / more failures than listed.
        stats = {"documents": 4, "doc_other": 2}
        self.assertIn("plus 2 other files listed (none of them could be read - see run "
                      "log).", gui.summary_text({"files": [self.DOCS], "stats": stats,
                                                 "doc_problem_count": 2}))
        self.assertIn("plus 1 other file listed (it couldn't be read - see run log).",
                      gui.summary_text({"files": [self.DOCS], "doc_problem_count": 1,
                                        "stats": dict(stats, doc_other=1)}))
        text = gui.summary_text({"files": [self.DOCS], "stats": stats, "doc_problem_count": 3})
        self.assertIn("plus 2 other files listed. 3 documents couldn't be read (see run "
                      "log).", text)
        # No documents file: the documents that couldn't be read get their own sentence.
        self.assertIn("2 documents couldn't be read (see run log).", gui.summary_text(
            {"files": [self.EMAILS], "stats": {"emails_used": 5}, "doc_problem_count": 2}))
        plain = gui.summary_text({"files": [self.EMAILS], "files_found": 5,
                                  "stats": {"emails_used": 5, "threads": 2}})
        self.assertNotIn("document", plain)

    # A run whose focus keywords (or dates) left no emails, but documents to write.
    DOCS_ONLY = {"files": [{"kind": "documents", "est_tokens": 6000, "documents": 4,
                            "path": os.path.join("out", "Squish - A - documents - 2025-01-01 to "
                                                        "2025-01-02 (focus pump).txt")}],
                 "files_found": 164,
                 "stats": {"emails_in": 164, "emails_used": 0, "filtered_out": 160,
                           "documents": 4, "doc_drawings": 18}}

    def test_documents_only_run_summary(self):
        text = gui.summary_text(self.DOCS_ONLY)
        self.assertTrue(text.startswith("164 emails read, but none were left to write, so this "
                                        "run made only a documents file (any earlier emails "
                                        "files were kept)."), text)
        self.assertNotIn("check the dates", text)
        self.assertIn("160 without the focus keywords skipped.", text)
        self.assertIn("22 documents (incl. 18 drawings) in 1 documents file, ~6k tokens.", text)
        # Nothing written at all: still the old advice.
        self.assertIn("check the dates and focus keywords", gui.summary_text(
            {"files": [], "files_found": 164, "stats": {"emails_used": 0}}))

    def test_documents_only_run_done_message(self):
        files = self.DOCS_ONLY["files"]
        done = gui.done_message(1, files, True, filters="only conversations mentioning pump")
        self.assertIn("only documents mentioning pump", done)
        self.assertNotIn("conversations", done)
        done = gui.done_message(1, files, True, filters="only 2025-01-01 to 2025-01-31 and only "
                                                        "conversations mentioning pump")
        self.assertTrue(done.endswith("only 2025-01-01 to 2025-01-31 and only documents "
                                      "mentioning pump."), done)
        # With an emails file the keywords picked conversations, as before.
        self.assertIn("only conversations mentioning pump", gui.done_message(
            1, [self.EMAILS, self.DOCS], True, filters="only conversations mentioning pump"))

    def test_documents_file_with_dates_outside_a_dated_run(self):
        def docs_file(tag, first, last, kind="documents"):
            name = ("Squish - Job - %s2024-11-15 to 2025-10-31%s.txt"
                    % ("documents - " if kind == "documents" else "", tag))
            return {"path": os.path.join("out", name), "kind": kind, "first_date": first,
                    "last_date": last, "est_tokens": 1000, "documents": 3}

        tag = " (only 2025-03-01 to 2025-04-30)"
        note = gui.undated_docs_note([docs_file(tag, "2024-11-15", "2025-10-31")])
        self.assertEqual(note, " The documents file also has the files in the documents folder, "
                               "whatever their date (attachments follow the dates).")
        self.assertEqual(gui.undated_docs_note([docs_file(tag, "2025-03-02", "2025-04-30")]), "")
        # Only the start, or only the end, of the range matters for a one-sided run.
        self.assertTrue(gui.undated_docs_note([docs_file(" (only from 2025-03-01)",
                                                         "2025-01-02", "2025-04-30")]))
        self.assertEqual(gui.undated_docs_note([docs_file(" (only from 2025-03-01)",
                                                          "2025-03-02", "2026-04-30")]), "")
        self.assertTrue(gui.undated_docs_note([docs_file(" (only up to 2025-04-30) (focus pump) "
                                                         "(part 1 of 2)", "2025-01-02",
                                                         "2025-05-01")]))
        # An all-dates run, or an emails file, never gets the note.
        self.assertEqual(gui.undated_docs_note([docs_file("", "2024-11-15", "2025-10-31")]), "")
        self.assertEqual(gui.undated_docs_note([docs_file(tag, "2024-11-15", "2025-10-31",
                                                          "emails")]), "")
        # Several parts: plural.
        parts = [docs_file(tag + " (part %d of 2)" % i, "2024-11-15", "2025-10-31")
                 for i in (1, 2)]
        self.assertIn("The documents files also have", gui.undated_docs_note(parts))
        # The summary carries it, after the documents sentence (also for a saved last_run).
        emails = docs_file(tag, "2025-03-02", "2025-04-30", "emails")
        result = {"files": [emails, docs_file(tag, "2024-11-15", "2025-10-31")],
                  "files_found": 60, "stats": {"emails_used": 46, "threads": 20, "documents": 18,
                                               "outside_dates": 14}}
        text = gui.summary_text(result)
        self.assertIn("18 documents in 1 documents file, ~1k tokens. The documents file also has "
                      "the files in the documents folder, whatever their date", text)
        self.assertEqual(gui.summary_text(projects.last_run_from_result(result)), text)
        self.assertNotIn("whatever their date", gui.summary_text(
            {"files": [emails], "files_found": 60, "stats": {"emails_used": 46}}))

    def test_squeeze_levels_say_what_they_do_to_documents(self):
        texts = dict((key, text) for key, _label, text in gui.squeeze_options())
        self.assertIn("Documents kept up to about 30,000 characters each.", texts["light"])
        self.assertIn("Long documents condensed to about 8,000 characters each.", texts["standard"])
        self.assertIn("Documents condensed to about 2,500 characters each.", texts["max"])
        self.assertTrue(texts["standard"].startswith("Long emails trimmed"))
        # Without the documents code, the email descriptions are still shown.
        with mock.patch.dict("sys.modules", {"squish_app.docdigest": None}):
            texts = dict((key, text) for key, _label, text in gui.squeeze_options())
        self.assertNotIn("characters each", texts["standard"])
        self.assertTrue(texts["standard"].startswith("Long emails trimmed"))


@unittest.skipIf(not has_display(), "no display (or no tkinter)")
class WindowTests(TempDataDir):
    """SquishApp itself, without mainloop: methods are called directly."""

    def open_app(self, items=None, compact=False):
        if items is not None:
            projects.save_projects(items)
        root = gui.tk.Tk()
        self.addCleanup(self._destroy, root)

        def size_window(app):
            app.compact = compact

        with mock.patch.object(gui.SquishApp, "_size_window", autospec=True,
                               side_effect=size_window):
            app = gui.SquishApp(root)
        root.update_idletasks()   # not update(): that could run timers such as the load report
        return app

    @staticmethod
    def fake_run(app, project, run_id=1):
        """Pretend a run of ``project`` is going (no thread)."""
        app.run = {"id": run_id, "project_id": project["id"], "name": project["name"],
                   "cancel": threading.Event(), "stage": "read", "started": time.time(),
                   "filters": ""}
        return app.run

    @staticmethod
    def _destroy(root):
        try:
            # Cancel the window's timers first: Tk runs every window's timers when any
            # window updates, so a later test's update() would call into this one.
            for job in root.tk.splitlist(root.tk.call("after", "info")):
                root.tk.call("after", "cancel", job)
            root.destroy()
        except tk.TclError:
            pass

    @staticmethod
    def start_without_engine(app, start):
        """Call ``start`` (e.g. app.start_run) with the engine replaced by a no-op,
        kept in place until the run thread has called it."""
        called = threading.Event()
        with mock.patch.object(gui, "run_in_background", side_effect=lambda *a: called.set()):
            start()
            if app.run:
                called.wait(5)

    @staticmethod
    def wait_for(app, done, seconds=15):
        """Handle the window's messages until done() is true (or time runs out)."""
        deadline = time.time() + seconds
        while not done() and time.time() < deadline:
            app.drain_queue()
            time.sleep(0.02)
        app.drain_queue()
        return done()

    def test_failed_save_stays_on_show_and_is_retried(self):
        app = self.open_app([projects.new_project("Alpha")])
        app.save_now()
        self.assertIsNotNone(app.save_note_job)       # 'Settings saved' will be cleared
        with mock.patch.object(projects, "save_projects",
                               side_effect=PermissionError(13, "Access is denied")):
            self.assertFalse(app.save_now())
        self.assertTrue(app.unsaved)
        self.assertIsNone(app.save_note_job)          # nothing left to wipe the error
        self.assertIsNotNone(app.save_job)            # tried again in a few seconds
        self.assertIn("Couldn't save settings", app.save_label.cget("text"))
        # Closing asks first; answering No keeps the window open.
        with mock.patch.object(projects, "save_projects", side_effect=PermissionError(13, "x")), \
                mock.patch.object(gui.messagebox, "askyesno", return_value=False) as ask:
            app.on_close()
        self.assertTrue(ask.called)
        self.assertFalse(app.closing)
        self.assertTrue(app.root.winfo_exists())
        # Once saving works again, the change is saved and the flag cleared.
        self.assertTrue(app.flush_save())
        self.assertFalse(app.unsaved)

    def test_unreadable_project_list_is_never_saved_over(self):
        projects.save_projects([projects.new_project("Precious")])
        path = str(paths.projects_file())
        with open(path, "rb") as fh:
            before = fh.read()
        problem = {"kind": "unreadable", "path": path, "backup": None, "error": "locked"}
        with mock.patch.object(projects, "load_projects_report", return_value=([], problem)), \
                mock.patch.object(gui.messagebox, "askretrycancel", return_value=False):
            app = self.open_app()
            app.report_load_problem()
        self.assertTrue(app.save_blocked)
        app.new_project()
        app.flush_save()
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertIn("couldn't be read", app.save_label.cget("text"))

    def test_retry_after_unreadable_project_list(self):
        projects.save_projects([projects.new_project("Precious")])
        path = str(paths.projects_file())
        problem = {"kind": "unreadable", "path": path, "backup": None, "error": "locked"}
        with mock.patch.object(projects, "load_projects_report", return_value=([], problem)):
            app = self.open_app()
        with mock.patch.object(gui.messagebox, "askretrycancel", return_value=True):
            app.report_load_problem()
        self.assertFalse(app.save_blocked)
        self.assertEqual([p["name"] for p in app.projects], ["Precious"])
        self.assertEqual(app.current_project()["name"], "Precious")

    def test_names_that_share_file_names_are_refused(self):
        a, b = projects.new_project("Bridge: Stage 1"), projects.new_project("Other")
        app = self.open_app([a, b])
        app.select_project(b["id"])
        app.form.name_var.set("Bridge Stage 1")
        self.assertTrue(app.name_taken("Bridge Stage 1"))
        self.assertEqual(app.form.name_hint.cget("style"), "Error.TLabel")
        problem = app.form.blocking_problem()
        self.assertIn("same file name", problem[2])

    def test_bad_settings_clear_the_progress_bar(self):
        app = self.open_app([projects.new_project("Alpha")])
        app.progress.configure(maximum=100, value=100)
        app.form.from_var.set("2025-13-01")
        app.start_run()
        self.assertIsNone(app.run)
        self.assertEqual(float(app.progress.cget("value")), 0.0)
        self.assertEqual(app.status.cget("style"), "StatusError.TLabel")

    def test_output_folder_inside_the_emails_folder_is_refused(self):
        src = tempfile.mkdtemp(dir=self.data_dir)
        app = self.open_app([projects.new_project("Alpha")])
        app.form.source_var.set(src)
        app.form.output_var.set(os.path.join(src, "Digests"))
        self.assertEqual(app.form.output_hint.cget("style"), "Error.TLabel")
        app.start_run()
        self.assertIsNone(app.run)
        self.assertIn("inside the emails folder", app.status.cget("text"))
        app.form.output_var.set("")
        self.assertNotEqual(app.form.output_hint.cget("style"), "Error.TLabel")

    def test_quoted_folder_paths_are_tidied(self):
        folder = tempfile.mkdtemp(dir=self.data_dir)
        app = self.open_app([projects.new_project("Alpha")])
        app.form.source_var.set('"%s"' % folder)
        self.assertEqual(app.current_project()["source_folder"], folder)
        app.form._tidy_folder(app.form.source_var)
        self.assertEqual(app.form.source_var.get(), folder)

    def test_browse_selects_the_whole_guessed_name(self):
        app = self.open_app()
        app.new_project()
        folder = os.path.join(self.data_dir, "1234 Riverside Depot Upgrade", "01 Emails")
        os.makedirs(folder)
        with mock.patch.object(gui.filedialog, "askdirectory", return_value=folder):
            app.form.browse_source()
        entry = app.form.name_entry
        self.assertEqual(app.form.name_var.get(), "1234 Riverside Depot Upgrade")
        self.assertTrue(entry.selection_present())
        self.assertEqual((entry.index("sel.first"), entry.index("sel.last")),
                         (0, len("1234 Riverside Depot Upgrade")))

    def test_another_projects_run_is_named(self):
        a, c = projects.new_project("Alpha"), projects.new_project("Charlie")
        app = self.open_app([a, c])
        app.select_project(a["id"])
        app.run = {"id": 1, "project_id": a["id"], "name": "Alpha",
                   "cancel": threading.Event(), "stage": "read"}
        app.select_project(c["id"])
        self.assertEqual(app.run_button.cget("text"), 'Cancel "Alpha"')
        app._show_progress(1, "read", 5, 10, "")
        self.assertTrue(app.status.cget("text").startswith('"Alpha": Reading 5 of 10'))
        app.start_run()
        self.assertIn('Already squishing "Alpha"', app.status.cget("text"))
        app.select_project(a["id"])
        self.assertEqual(app.run_button.cget("text"), "Cancel")
        self.assertEqual(app.results.summary.cget("text"), "Working on it...")
        app._show_progress(1, "digest", 3, 50, "Squishing emails... 3 of 50")
        app.cancel_run()
        self.assertIn("digest", app.status.cget("text"))
        app.run = None

    def test_run_passes_the_files_it_wrote_last_time(self):
        a = projects.new_project("Alpha")
        a["source_folder"] = self.data_dir
        a["last_run"] = {"finished_at": "2025-01-01 10:00",
                         "files": [{"path": os.path.join(self.data_dir, "Squish - Old - undated.txt")}]}
        app = self.open_app([a])
        seen = {}

        def fake_run(run_id, project, cancel, post):
            seen.update(project)

        with mock.patch.object(gui, "run_in_background", side_effect=fake_run):
            app.start_run()
            deadline = time.time() + 5
            while not seen and time.time() < deadline:
                time.sleep(0.01)
        self.assertEqual(seen["previous_files"],
                         [os.path.join(self.data_dir, "Squish - Old - undated.txt")])
        self.assertIsNone(seen["last_run"])
        app.run = None

    # ---- double clicks -------------------------------------------------------------

    def test_double_click_on_squish_does_not_cancel_the_run(self):
        a = projects.new_project("Alpha")
        a["source_folder"] = tempfile.mkdtemp(dir=self.data_dir)
        app = self.open_app([a])

        def double_click():
            app.on_run_button()
            app.on_run_button()        # the second click of a double-click

        self.start_without_engine(app, double_click)
        self.assertIsNotNone(app.run)
        self.assertFalse(app.run["cancel"].is_set())
        self.assertEqual(app.run_button.cget("text"), "Cancel")
        # A separate click later still cancels.
        app.last_click["run"] -= gui.CLICK_GAP_S
        app.on_run_button()
        self.assertTrue(app.run["cancel"].is_set())
        app.run = None

    def test_double_click_on_new_or_duplicate_makes_one_project(self):
        app = self.open_app([projects.new_project("Alpha")])
        app.new_button.invoke()
        app.new_button.invoke()
        self.assertEqual(len(app.projects), 2)
        app.last_click["new"] -= gui.CLICK_GAP_S       # a separate click later
        app.new_button.invoke()
        self.assertEqual(len(app.projects), 3)
        app.dup_button.invoke()
        app.dup_button.invoke()
        self.assertEqual(len(app.projects), 4)

    # ---- network drives ----------------------------------------------------------------

    def test_starting_a_run_never_resolves_links_on_the_window_thread(self):
        # os.path.realpath opens the folders: on a disconnected network drive
        # that can take minutes, so only the run's own thread may do it.
        a = projects.new_project("Alpha")
        a["source_folder"] = tempfile.mkdtemp(dir=self.data_dir)
        a["output_folder"] = tempfile.mkdtemp(dir=self.data_dir)
        app = self.open_app([a])
        on_window_thread = []
        real_realpath = os.path.realpath

        def realpath(path, *args, **kwargs):
            on_window_thread.append(threading.current_thread() is threading.main_thread())
            return real_realpath(path, *args, **kwargs)

        with mock.patch("os.path.realpath", side_effect=realpath):
            self.start_without_engine(app, app.start_run)
        self.assertIsNotNone(app.run)
        self.assertNotIn(True, on_window_thread)
        app.run = None

    def test_output_folder_linked_into_the_emails_folder_is_refused_by_the_run(self):
        src = tempfile.mkdtemp(dir=self.data_dir)
        os.makedirs(os.path.join(src, "Digests"))
        link = os.path.join(self.data_dir, "digests link")
        try:
            os.symlink(os.path.join(src, "Digests"), link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("can't make a folder link here")
        a = projects.new_project("Alpha")
        a["source_folder"], a["output_folder"] = src, link
        app = self.open_app([a])
        before = sorted(os.path.join(d, n) for d, _, names in os.walk(src) for n in names)
        app.start_run()
        self.assertIsNotNone(app.run)            # as typed, the folders look separate
        self.assertTrue(self.wait_for(app, lambda: app.run is None))
        self.assertIn("inside the email folder", app.status.cget("text"))
        after = sorted(os.path.join(d, n) for d, _, names in os.walk(src) for n in names)
        self.assertEqual(after, before)          # nothing written into the emails folder

    def test_unreachable_output_folder_is_not_called_deleted(self):
        offline = os.path.join(self.data_dir, "offline drive", "Digests")
        a = projects.new_project("Alpha")
        a["last_run"] = {"finished_at": "2026-10-01 10:00", "output_folder": offline,
                         "files": [{"path": os.path.join(offline, "Squish - Alpha - undated.txt"),
                                    "est_tokens": 10}]}
        app = self.open_app([a])
        self.assertTrue(self.wait_for(app, lambda: "Can't reach" in
                                      app.results.summary.cget("text")))
        text = app.results.summary.cget("text")
        self.assertIn("VPN", text)
        self.assertIn("probably still there", text)
        self.assertNotIn("moved or deleted", text)
        # The folder is there but the files are not: they really were moved or deleted.
        os.makedirs(offline)
        app.select_project(a["id"])
        self.assertTrue(self.wait_for(app, lambda: "moved or deleted" in
                                      app.results.summary.cget("text")))

    # ---- a stopped run, and long project names ----------------------------------------

    def test_stopped_run_reason_is_kept_for_its_project(self):
        a, b = projects.new_project("Alpha"), projects.new_project("Bravo")
        a["source_folder"] = tempfile.mkdtemp(dir=self.data_dir)
        app = self.open_app([a, b])
        log = os.path.join(self.data_dir, "Alpha - last run.txt")
        with open(log, "w") as fh:
            fh.write("none of the files could be read")
        app.select_project(b["id"])
        self.fake_run(app, a)
        app._run_error(1, "Squish stopped because none of the 30 email files could be read."
                          "\n\nNothing was changed.", log)
        self.assertTrue(app.status.cget("text").startswith('"Alpha": Squish stopped'))
        self.assertEqual(app.results.log_path, "")          # not attached to Bravo
        for _visit in range(2):
            app.select_project(a["id"])
            self.assertEqual(app.status.cget("text"), "Squish stopped because none of the 30 "
                             "email files could be read. View run log has the details.")
            self.assertEqual(app.status.cget("style"), "StatusError.TLabel")
            self.assertIn("Nothing was changed", app.status_details)
            self.assertEqual(app.results.log_path, log)
            self.assertNotIn("disabled", app.results.log_button.state())
            app.select_project(b["id"])
            self.assertEqual(app.status.cget("text"), "")
        # Changing a setting clears the reason (it may no longer apply), not the log.
        app.select_project(a["id"])
        app.form.subfolders_var.set(False)
        app.select_project(b["id"])
        app.select_project(a["id"])
        self.assertEqual(app.status.cget("text"), "")
        self.assertEqual(app.results.log_path, log)
        # A new run forgets the stop.
        self.start_without_engine(app, app.start_run)
        app._run_finished()
        app.select_project(a["id"])
        self.assertEqual(app.status.cget("text"), "")
        self.assertEqual(app.results.log_path, "")

    def test_long_project_names_are_shortened_on_the_status_line(self):
        long_name = "Riverside Depot Stormwater Upgrade Stage 2 Detailed Design and Documentation"
        short = gui.short_name(long_name)
        a, b = projects.new_project(long_name), projects.new_project("Bravo")
        app = self.open_app([a, b])
        app.select_project(b["id"])
        self.fake_run(app, a)
        app._show_progress(1, "read", 5, 10, "")
        self.assertTrue(app.status.cget("text").startswith('"%s": Reading 5 of 10' % short))
        app.start_run()
        self.assertIn('Already squishing "%s"' % short, app.status.cget("text"))
        app._run_error(1, "Squish stopped because x.\n\nNothing was changed.")
        self.assertEqual(app.status.cget("text"), '"%s": Squish stopped because x.' % short)
        self.assertIn("Project: " + long_name, app.status_details)   # behind Details...
        self.fake_run(app, a)
        app._run_done(1, {"files": [{"path": os.path.join(self.data_dir, "x.txt"),
                                     "est_tokens": 10}], "elapsed_s": 1})
        self.assertTrue(app.status.cget("text").endswith('(project "%s").' % short))

    # ---- the status line and the compact layout --------------------------------

    def test_status_shows_one_line_and_details(self):
        app = self.open_app([projects.new_project("Alpha")])
        app.set_status("Squish stopped because x.\n\nNothing was changed.", "Error")
        self.assertEqual(app.status.cget("text"), "Squish stopped because x.")
        self.assertEqual(app.details_button.winfo_manager(), "grid")
        self.assertIn("Nothing was changed", app.status_details)
        with mock.patch.object(gui, "TextWindow") as window:
            app.show_status_details()
        self.assertIn("Nothing was changed", window.call_args[0][2])
        app.set_status("short")
        self.assertEqual(app.details_button.winfo_manager(), "")
        self.assertEqual(app.status_details, "")

    def test_compact_layout_hides_plain_hints_only(self):
        folder = tempfile.mkdtemp(dir=self.data_dir)
        a = projects.new_project("Alpha")
        a["source_folder"] = folder
        app = self.open_app([a], compact=True)
        form = app.form
        self.assertEqual(form.name_hint.winfo_manager(), "")       # 'Used in the file names'
        self.assertEqual(form.output_hint.winfo_manager(), "")
        self.assertEqual(form.source_hint.winfo_manager(), "grid")  # the email count stays
        form.name_var.set("")
        self.assertEqual(form.name_hint.winfo_manager(), "grid")
        self.assertEqual(form.name_hint.cget("style"), "Error.TLabel")
        form.name_var.set("Alpha")
        self.assertEqual(form.name_hint.winfo_manager(), "")
        form.keywords_var.set("pump")      # a filter warning shows on the Emails tab
        self.assertEqual(form.dates_hint.winfo_manager(), "grid")
        self.assertEqual(form.dates_hint.cget("style"), "Warn.TLabel")

    def test_filters_are_shown_on_the_emails_tab(self):
        app = self.open_app([projects.new_project("Alpha")])
        form = app.form
        self.assertEqual(form.name_hint.winfo_manager(), "grid")   # the full layout shows all
        self.assertIn("Leave blank for all dates", form.dates_hint.cget("text"))
        # ... and says where the other filter (focus keywords) is
        self.assertIn("Focus keywords (Squeeze tab)", form.dates_hint.cget("text"))
        form.from_var.set("2025-02-01")
        self.assertIn("Only these dates", form.dates_hint.cget("text"))
        self.assertIn("(only <dates>)", form.dates_hint.cget("text"))
        form.keywords_var.set("pump, culvert")
        self.assertIn("Focus keywords are on (Squeeze tab): pump, culvert",
                      form.dates_hint.cget("text"))
        self.assertEqual(form.dates_hint.cget("style"), "Warn.TLabel")
        form.from_var.set("2025-02-31")        # a date problem still wins
        self.assertEqual(form.dates_hint.cget("style"), "Error.TLabel")

    def test_done_message_names_the_filter(self):
        a = projects.new_project("Alpha")
        a["focus_keywords"] = "pump"
        app = self.open_app([a])
        run = self.fake_run(app, a)
        run["filters"] = gui.filter_summary(a)
        path = os.path.join(self.data_dir, "Squish - Alpha - 2025-01-01 to 2025-01-02 (focus pump).txt")
        app._run_done(1, {"files": [{"path": path, "est_tokens": 100}], "elapsed_s": 1,
                          "stats": {}})
        self.assertIn("only conversations mentioning pump", app.status.cget("text"))
        self.assertIn("Show in folder", app.status.cget("text"))
        # Claims nothing about what else is in the folder (there may be no full digest).
        self.assertIn("This file has only part of the emails - for the full digest, clear the "
                      "dates and focus keywords and click Squish!",
                      app.results.summary.cget("text"))

    def test_documents_only_run_says_part_of_the_documents(self):
        # Focus keywords that match documents but no emails: only a documents file.
        a = projects.new_project("Alpha")
        a["focus_keywords"] = "pump"
        app = self.open_app([a])
        run = self.fake_run(app, a)
        run["filters"] = gui.filter_summary(a)
        result = dict(DocumentHelperTests.DOCS_ONLY, elapsed_s=1,
                      files=[dict(DocumentHelperTests.DOCS_ONLY["files"][0],
                                  path=os.path.join(self.data_dir, "Squish - Alpha - documents - "
                                                    "2025-01-01 to 2025-01-02 (focus pump).txt"))])
        app._run_done(1, result)
        status = app.status.cget("text")
        self.assertIn("1 file ready", status)
        self.assertIn("only documents mentioning pump", status)
        self.assertNotIn("conversations", status)
        summary = app.results.summary.cget("text")
        self.assertIn("so this run made only a documents file", summary)
        self.assertIn("This file has only part of the documents - for the full digest, clear the "
                      "dates and focus keywords and click Squish!", summary)
        self.assertNotIn("part of the emails", summary)
        # Shown again later (from the saved last_run): the same words.
        app.select_project(a["id"])
        self.assertIn("only part of the documents", app.results.summary.cget("text"))

    def test_dates_hint_says_the_documents_folder_is_not_dated(self):
        app = self.open_app([projects.new_project("Alpha")])
        form = app.form
        form.from_var.set("2025-03-01")
        self.assertIn("Only these dates", form.dates_hint.cget("text"))
        self.assertNotIn("whatever their date", form.dates_hint.cget("text"))
        form.docs_var.set(os.path.join(self.data_dir, "Reports"))
        self.assertIn("Only these dates", form.dates_hint.cget("text"))
        self.assertIn("(only <dates>)", form.dates_hint.cget("text"))
        self.assertIn("Files in the documents folder are included whatever their date.",
                      form.dates_hint.cget("text"))
        form.from_var.set("")
        self.assertNotIn("whatever their date", form.dates_hint.cget("text"))
        self.assertIn("files in the documents folder (Documents tab) are always included, "
                      "whatever their date", " ".join(gui.HOW_TO.split()))     # and Help says so

    def test_read_stage_mentions_the_attachments(self):
        a = projects.new_project("Alpha")
        a["source_folder"] = tempfile.mkdtemp(dir=self.data_dir)
        app = self.open_app([a])
        self.start_without_engine(app, app.start_run)
        self.assertTrue(app.run["attachments"])
        app._show_progress(app.run["id"], "read", 55, 164, "")
        self.assertEqual(app.status.cget("text"), "Reading emails and attachments 55 of 164...")
        app._run_finished()
        a["docs_from_attachments"] = False
        app.form.attachments_var.set(False)
        self.start_without_engine(app, app.start_run)
        self.assertFalse(app.run["attachments"])
        app._show_progress(app.run["id"], "read", 55, 164, "")
        self.assertEqual(app.status.cget("text"), "Reading 55 of 164 emails...")
        app._run_finished()

    def test_done_is_amber_when_emails_may_be_missing(self):
        a = projects.new_project("Alpha")
        app = self.open_app([a])
        path = os.path.join(self.data_dir, "Squish - Alpha - 2025-01-01 to 2025-01-02.txt")
        for failed, stats, style in (
                ([], {}, "StatusGood.TLabel"),
                ([["x.msg", "damaged"]], {"unreadable_files": 1}, "StatusWarn.TLabel"),
                ([], {"no_access_emails": 3}, "StatusWarn.TLabel")):
            self.fake_run(app, a)
            app._run_done(1, {"files": [{"path": path, "est_tokens": 100}], "elapsed_s": 1,
                              "failed": failed, "stats": stats})
            self.assertIn("Done in", app.status.cget("text"))
            self.assertEqual(app.status.cget("style"), style, failed or stats)

    def test_small_run_says_one_chat_per_file(self):
        a = projects.new_project("Alpha")
        a["part_size"] = "small"
        app = self.open_app([a])
        run = self.fake_run(app, a)
        run["part_size"] = "small"
        files = [{"path": os.path.join(self.data_dir, "Squish - Alpha - undated (part %d of 3).txt"
                                       % i), "est_tokens": 40000} for i in (1, 2, 3)]
        app._run_done(1, {"files": files, "elapsed_s": 1, "stats": {}})
        self.assertIn("new Claude chat for each file", app.status.cget("text"))
        self.assertIn("new Claude chat for each file", app.results.tip.cget("text"))
        app.select_project(a["id"])          # shown again later
        self.assertIn("new Claude chat for each file", app.results.tip.cget("text"))

    def test_run_remembers_its_file_size(self):
        a = projects.new_project("Alpha")
        a["source_folder"] = tempfile.mkdtemp(dir=self.data_dir)
        a["part_size"] = "small"
        app = self.open_app([a])
        self.start_without_engine(app, app.start_run)
        self.assertEqual(app.run["part_size"], "small")
        app.run = None

    # ---- names and folders --------------------------------------------------------

    def test_pasted_folder_names_a_new_project(self):
        app = self.open_app()
        app.new_project()
        folder = os.path.join(self.data_dir, "1234 Riverside Depot Upgrade", "01 Emails")
        app.form.source_var.set('"%s"' % folder)
        self.assertEqual(app.form.name_var.get(), projects.DEFAULT_NAME)  # not while typing
        app.form._source_left()
        self.assertEqual(app.form.name_var.get(), "1234 Riverside Depot Upgrade")
        self.assertEqual(app.current_project()["name"], "1234 Riverside Depot Upgrade")
        # A name the user typed is never replaced.
        app.form.name_var.set("My name")
        app.form.source_var.set(os.path.join(self.data_dir, "Other Job"))
        app.form._source_left()
        self.assertEqual(app.form.name_var.get(), "My name")

    def test_pasted_file_link_becomes_the_folder_path(self):
        app = self.open_app()
        app.new_project()
        app.form.source_var.set("<file:///H:/Jobs/6012%20Harbour%20Road/01%20Emails>")
        app.form._source_left()
        self.assertEqual(app.form.source_var.get(), "H:\\Jobs\\6012 Harbour Road\\01 Emails")
        self.assertEqual(app.form.name_var.get(), "6012 Harbour Road")
        self.assertEqual(app.current_project()["source_folder"],
                         "H:\\Jobs\\6012 Harbour Road\\01 Emails")

    def test_pasted_folder_then_squish_names_the_project(self):
        app = self.open_app()
        app.new_project()
        folder = os.path.join(self.data_dir, "Harbour Road", "Correspondence")
        os.makedirs(folder)
        app.form.source_var.set(folder)
        with mock.patch.object(gui, "run_in_background"):
            app.start_run()
        self.assertEqual(app.run["name"], "Harbour Road")
        app.run = None

    def test_folder_box_shows_the_end_of_a_long_path(self):
        app = self.open_app([projects.new_project("Alpha")])
        folder = os.path.join(self.data_dir, *(["Long folder name %d" % i for i in range(12)]
                                               + ["01 Emails"]))
        os.makedirs(folder)
        with mock.patch.object(gui.filedialog, "askdirectory", return_value=folder):
            app.form.browse_source()
        app.root.update_idletasks()
        start, end = app.form.source_entry.xview()
        self.assertGreater(start, 0.0)       # the start of the path is scrolled out of view
        self.assertEqual(end, 1.0)

    # ---- a run's outcome while another project is shown ---------------------------

    def test_background_run_outcomes_name_the_project(self):
        a, c = projects.new_project("Alpha"), projects.new_project("Charlie")
        app = self.open_app([a, c])
        app.select_project(c["id"])
        self.fake_run(app, a)
        app._run_error(1, "Squish stopped because x.\n\nNothing was changed.")
        self.assertTrue(app.status.cget("text").startswith('"Alpha": Squish stopped'))
        self.fake_run(app, a)
        app._run_done(1, {"files": []})
        self.assertTrue(app.status.cget("text").startswith('"Alpha": Finished, but no emails'))
        self.fake_run(app, a)
        app._run_done(1, {"cancelled": True})
        self.assertTrue(app.status.cget("text").startswith('"Alpha": Cancelled'))
        self.fake_run(app, a)
        with mock.patch.object(app, "_write_crash_log", return_value="x.txt"):
            app._run_crash(1, "Traceback\nRuntimeError: boom")
        self.assertTrue(app.status.cget("text").startswith('"Alpha": Something went wrong'))
        self.assertEqual(app.results.log_path, "")        # not attached to Charlie
        # With the run's own project on screen: no prefix, and the log is attached.
        app.select_project(a["id"])
        self.fake_run(app, a)
        with mock.patch.object(app, "_write_crash_log", return_value="x.txt"):
            app._run_crash(1, "Traceback\nRuntimeError: boom")
        self.assertTrue(app.status.cget("text").startswith("Something went wrong"))
        self.assertEqual(app.results.log_path, "x.txt")

    def test_stopped_run_log_can_be_viewed(self):
        a = projects.new_project("Broken job")
        app = self.open_app([a])
        log = os.path.join(self.data_dir, "stop log.txt")
        with open(log, "w") as fh:
            fh.write("Stopped: none of the files could be read")
        self.assertIn("disabled", app.results.log_button.state())
        run = self.fake_run(app, a)
        app.handle_message(("error", run["id"], "Squish stopped because x.\n\nNothing was "
                            "changed.", log))
        self.assertEqual(app.results.log_path, log)
        self.assertNotIn("disabled", app.results.log_button.state())
        self.assertIn("View run log", app.status.cget("text"))
        # The background 'do the files still exist' check keeps it.
        app._files_checked(app.files_token, {"files": [{"path": "gone"}], "log_path": ""},
                           "", [False])
        self.assertEqual(app.results.log_path, log)
        # Without a log (e.g. the folder was missing) the button stays disabled.
        app.select_project(a["id"])
        run = self.fake_run(app, a, run_id=2)
        app.handle_message(("error", 2, "Can't find the email folder:\nH:\\x\n\nVPN?", ""))
        self.assertEqual(app.results.log_path, "")
        self.assertEqual(app.status.cget("text"), "Can't find the email folder")

    def test_stopped_run_log_found_without_engine_help(self):
        a = projects.new_project("Broken job")
        app = self.open_app([a])
        run = self.fake_run(app, a)
        log = os.path.join(str(paths.logs_dir()), "Broken job - last run.txt")
        with open(log, "w") as fh:
            fh.write("Stopped")
        app._run_error(run["id"], "Squish stopped because x.\n\nNothing was changed.")
        self.assertEqual(app.results.log_path, log)
        # An old log (from before this run) is not the stopped run's log.
        os.utime(log, (time.time() - 3600, time.time() - 3600))
        run = self.fake_run(app, a, run_id=2)
        app._run_error(2, "Squish stopped because x.\n\nNothing was changed.")
        self.assertEqual(app.results.log_path, "")

    # ---- documents ----------------------------------------------------------------

    def test_documents_settings_are_shown_and_saved(self):
        a = projects.new_project("Alpha")
        a["docs_from_attachments"] = False
        a["docs_folder"] = os.path.join(self.data_dir, "Reports")
        a["docs_include_subfolders"] = False
        b = projects.new_project("Bravo")        # defaults
        app = self.open_app([a, b])
        form = app.form
        self.assertEqual(form.tab(gui.SettingsForm.TAB_DOCUMENTS, "text").strip(), "Documents")
        self.assertFalse(form.attachments_var.get())
        self.assertEqual(form.docs_var.get(), a["docs_folder"])
        self.assertFalse(form.docs_subfolders_var.get())
        self.assertIn("only listed by name", form.attachments_hint.cget("text"))
        app.select_project(b["id"])
        app.start_docs_count()       # (select_project schedules it for when the window is idle)
        self.assertTrue(form.attachments_var.get())
        self.assertEqual(form.docs_var.get(), "")
        self.assertTrue(form.docs_subfolders_var.get())
        self.assertIn("separate documents file", form.attachments_hint.cget("text"))
        self.assertIn("Optional", form.docs_hint.cget("text"))
        # Edits are saved like every other setting (quotes from 'Copy as path' removed).
        reports = os.path.join(self.data_dir, "Job", "04 Reports")
        form.docs_var.set('"%s"' % reports)
        form.docs_subfolders_var.set(False)
        form.attachments_var.set(False)
        self.assertIsNotNone(app.save_job)
        self.assertTrue(app.flush_save())
        saved = projects.find_project(projects.load_projects(), b["id"])
        self.assertEqual((saved["docs_folder"], saved["docs_include_subfolders"],
                          saved["docs_from_attachments"]), (reports, False, False))
        self.assertEqual(form.attachments_hint.cget("text"),
                         "Attachments are only listed by name in the emails file.")
        form.docs_var.set("")
        self.assertIn("no documents file is made", form.attachments_hint.cget("text"))
        # The blank folder's hint agrees with the unticked box (and follows it).
        app.start_docs_count()
        self.assertIn("Leave blank for no documents folder.", form.docs_hint.cget("text"))
        self.assertNotIn("attachments", form.docs_hint.cget("text"))
        form.attachments_var.set(True)
        self.assertIsNotNone(app.docs_count_job)          # the hint is brought up to date
        app.start_docs_count()
        self.assertIn("Leave blank to condense only the attachments.", form.docs_hint.cget("text"))

    def test_documents_folder_is_counted(self):
        folder = os.path.join(self.data_dir, "Reports")
        for name in ("a.pdf", "b.docx", "c.dwg", os.path.join("Sub", "d.xlsx")):
            os.makedirs(os.path.dirname(os.path.join(folder, name)), exist_ok=True)
            with open(os.path.join(folder, name), "w") as fh:
                fh.write("x")
        app = self.open_app([projects.new_project("Alpha")])
        hint = app.form.docs_hint
        app.form.docs_var.set(folder)
        app.start_docs_count()
        self.assertTrue(self.wait_for(app, lambda: "found" in hint.cget("text")))
        self.assertEqual(hint.cget("text"), "3 documents found (incl. subfolders) - plus 1 other "
                                            "file, listed, not read.")
        self.assertEqual(hint.cget("style"), "Good.TLabel")
        app.form.docs_subfolders_var.set(False)
        app.start_docs_count()
        self.assertTrue(self.wait_for(app, lambda: "this folder only" in hint.cget("text")))
        self.assertTrue(hint.cget("text").startswith("2 documents found"))
        # A missing folder never stops a run: a warning, not an error.
        app.form.docs_var.set(os.path.join(folder, "gone"))
        app.start_docs_count()
        self.assertTrue(self.wait_for(app, lambda: "Can't find" in hint.cget("text")))
        self.assertEqual(hint.cget("style"), "Warn.TLabel")
        # The output folder is never read for documents.
        app.form.source_var.set(os.path.join(self.data_dir, "Emails"))
        app.form.output_var.set(folder)
        app.form.docs_var.set(folder)
        app.start_docs_count()
        self.assertIn("where the digests are saved", hint.cget("text"))
        self.assertEqual(hint.cget("style"), "Warn.TLabel")
        self.start_without_engine(app, app.start_run)    # ... but it doesn't stop Squish!
        self.assertIsNotNone(app.run)
        app.run = None

    def test_documents_folder_of_emails_only(self):
        # The documents folder may be the emails folder: not "No files here."
        folder = os.path.join(self.data_dir, "01 Emails")
        os.makedirs(folder)
        for name in ("a.msg", "b.msg", "c.eml"):
            with open(os.path.join(folder, name), "w") as fh:
                fh.write("x")
        app = self.open_app([projects.new_project("Alpha")])
        hint = app.form.docs_hint
        app.form.docs_var.set(folder)
        app.start_docs_count()
        self.assertTrue(self.wait_for(app, lambda: "here" in hint.cget("text")))
        self.assertIn("Only emails here (3 email files", hint.cget("text"))
        self.assertNotEqual(hint.cget("style"), "Warn.TLabel")

    def test_pdf_reader_line(self):
        app = self.open_app([projects.new_project("Alpha")])
        hint = app.form.pdf_hint
        app.pdf_status = None
        app.form.set_hint(hint, gui.PDF_CHECKING)
        self.assertEqual(hint.cget("text"), "checking...")
        app.handle_message(("pdf_backend", "PDF: pypdf 5.1.0"))
        self.assertEqual(hint.cget("text"), "Installed (pypdf 5.1.0) - PDFs are read with the "
                                            "better reader.")
        app.handle_message(("pdf_backend", "PDF: built-in reader (install pypdf for best results)"))
        self.assertEqual(hint.cget("text"), gui.PDF_ADVICE)
        with mock.patch.object(gui.messagebox, "showinfo") as about:
            app.show_about()
        self.assertIn("PDF: built-in reader", about.call_args[0][1])

    def test_compact_layout_keeps_the_document_count_and_pdf_line(self):
        app = self.open_app([projects.new_project("Alpha")], compact=True)
        form = app.form
        self.assertEqual(form.attachments_hint.winfo_manager(), "")   # a plain hint
        self.assertEqual(form.docs_hint.winfo_manager(), "grid")
        self.assertEqual(form.pdf_hint.winfo_manager(), "grid")

    def test_run_gets_the_document_settings(self):
        a = projects.new_project("Alpha")
        a["source_folder"] = self.data_dir
        a["docs_folder"] = os.path.join(self.data_dir, "Reports")
        a["docs_from_attachments"] = False
        app = self.open_app([a])
        seen = {}

        def fake_run(run_id, project, cancel, post):
            seen.update(project)

        with mock.patch.object(gui, "run_in_background", side_effect=fake_run):
            app.start_run()
            deadline = time.time() + 5
            while not seen and time.time() < deadline:
                time.sleep(0.01)
        self.assertEqual((seen["docs_folder"], seen["docs_from_attachments"],
                          seen["docs_include_subfolders"]), (a["docs_folder"], False, True))
        app.run = None

    def test_documents_stage_progress_and_cancel(self):
        a = projects.new_project("Alpha")
        app = self.open_app([a])
        self.fake_run(app, a)
        app._show_progress(1, "documents", 12, 64, "Reading documents... 12 of 64")
        self.assertEqual(app.status.cget("text"), "Condensing documents: 12 of 64 files...")
        self.assertEqual(str(app.progress.cget("mode")), "determinate")
        self.assertEqual(float(app.progress.cget("value")), 12.0)
        app._show_progress(1, "documents", 1, 1, "Found 70 files")
        self.assertEqual(app.status.cget("text"), "Found 70 files")
        app._show_progress(1, "documents", 3, 64, "")
        app.cancel_run()
        self.assertIn("documents", app.status.cget("text"))
        app._run_finished()

    def test_results_table_shows_emails_and_documents_files(self):
        a = projects.new_project("Alpha")
        app = self.open_app([a])
        out = os.path.join(self.data_dir, "out")
        emails = dict(DocumentHelperTests.EMAILS,
                      path=os.path.join(out, "Squish - Alpha - 2024-08-22 to 2025-11-30.txt"),
                      bytes=430000, first_date="2024-08-22", last_date="2025-11-30")
        docs = dict(DocumentHelperTests.DOCS,
                    path=os.path.join(out, "Squish - Alpha - documents - 2024-08-22 to "
                                           "2025-11-30.txt"),
                    bytes=150000, first_date="2024-08-22", last_date="2025-11-30")
        self.fake_run(app, a)
        app._run_done(1, {"files": [emails, docs], "elapsed_s": 4, "output_folder": out,
                          "files_found": 2000, "failed": [], "doc_problems": [],
                          "stats": {"emails_used": 1876, "threads": 200, "documents": 64,
                                    "doc_drawings": 9, "doc_other": 31}})
        tree = app.results.tree
        rows = [tree.item(i, "values") for i in tree.get_children()]
        self.assertEqual([r[1] for r in rows], ["Emails", "Documents"])
        self.assertEqual([r[-1] for r in rows], ["1,876 emails", "64 documents"])
        self.assertEqual(rows[1][0], "documents - 2024-08-22 to 2025-11-30.txt")
        self.assertEqual(tree.selection(), ("0",))      # the emails file is picked first
        self.assertIn("73 documents (incl. 9 drawings) in 1 documents file",
                      app.results.summary.cget("text"))
        self.assertIn("add the documents file when you need what the documents say",
                      app.status.cget("text"))
        self.assertEqual(app.status.cget("style"), "StatusGood.TLabel")
        # A documents folder that couldn't be read makes the Done message amber.
        self.fake_run(app, a)
        app._run_done(1, {"files": [emails, docs], "elapsed_s": 4, "failed": [], "stats": {},
                          "doc_problems": [["H:\\Reports", "documents folder not found: VPN?"]]})
        self.assertEqual(app.status.cget("style"), "StatusWarn.TLabel")
        # So does a documents digest that couldn't be made: the emails file alone
        # was written, and the earlier documents file (still in the folder) is old.
        self.fake_run(app, a)
        app._run_done(1, {"files": [emails], "elapsed_s": 4, "failed": [], "doc_problems": [],
                          "stats": {"emails_used": 1876}, "doc_digest_failed": True})
        self.assertEqual(app.status.cget("style"), "StatusWarn.TLabel")
        self.assertIn("The documents file couldn't be made this time", app.status.cget("text"))
        self.assertIn("is from an earlier run", app.results.summary.cget("text"))
        self.assertTrue(projects.find_project(app.projects, a["id"])["last_run"][
            "doc_digest_failed"])
        # ... also when the filters left no emails to write either.
        self.fake_run(app, a)
        app._run_done(1, {"files": [], "doc_digest_failed": True})
        self.assertIn("The documents file couldn't be made either", app.status.cget("text"))
        self.fake_run(app, a)
        app._run_done(1, {"files": [emails, docs], "elapsed_s": 4, "failed": [], "stats": {},
                          "doc_problems": []})
        # Shown again later (from the saved last_run): the same table.
        app.select_project(a["id"])
        rows = [tree.item(i, "values") for i in tree.get_children()]
        self.assertEqual([r[1] for r in rows], ["Emails", "Documents"])

    def test_compact_layout_keeps_squeeze_levels_short(self):
        # The notebook is as tall as its tallest tab (the Squeeze tab): the compact
        # layout leaves out the documents sentence, so the table keeps its rows.
        app = self.open_app([projects.new_project("Alpha")], compact=True)
        texts = dict((key, text) for key, _label, text in app.form.squeeze_choices)
        self.assertFalse([t for t in texts.values() if "characters each" in t])
        self.assertTrue(texts["standard"].startswith("Long emails trimmed"))

    def test_normal_layout_says_what_squeeze_does_to_documents(self):
        app = self.open_app([projects.new_project("Alpha")], compact=False)
        texts = dict((key, text) for key, _label, text in app.form.squeeze_choices)
        self.assertIn("Long documents condensed to about 8,000 characters each.", texts["standard"])

    def test_file_column_fits_a_small_window(self):
        app = self.open_app([projects.new_project("Alpha")], compact=True)
        app.root.geometry("980x560")
        app.root.update()
        tree = app.results.tree
        total = sum(int(tree.column(c, "width")) for c in tree["columns"])
        self.assertLessEqual(total, tree.winfo_width())

    # ---- closing ------------------------------------------------------------------

    def test_run_finishing_while_close_question_is_open(self):
        a = projects.new_project("Alpha")
        app = self.open_app([a])
        self.fake_run(app, a)

        def answer(*_args, **_kwargs):
            app._run_finished()      # the run ends while the question is open
            return True

        with mock.patch.object(gui.messagebox, "askyesno", side_effect=answer), \
                mock.patch.object(app, "on_tk_error") as on_error, \
                mock.patch.object(gui.messagebox, "showerror") as error_box:
            app.on_close()
        self.assertTrue(app.closing)
        self.assertFalse(on_error.called)
        self.assertFalse(error_box.called)

    def test_close_waits_for_a_stopping_run_and_saves_its_result(self):
        a = projects.new_project("Alpha")
        app = self.open_app([a])
        run = self.fake_run(app, a)
        finish = threading.Event()
        worker = threading.Thread(target=finish.wait, name="squish-run")
        worker.daemon = True
        worker.start()
        self.addCleanup(finish.set)
        with mock.patch.object(gui.messagebox, "askyesno", return_value=True):
            app.on_close()
        self.assertTrue(run["cancel"].is_set())
        self.assertTrue(app.closing)
        self.assertTrue(app.root.winfo_exists())               # still open while it stops
        self.assertIn("Stopping", app.status.cget("text"))
        app._close_when_idle(time.monotonic() + 60)            # still busy: waits
        self.assertTrue(app.root.winfo_exists())
        # The run finishes (it had got to writing its files) and the thread ends.
        path = os.path.join(self.data_dir, "Squish - Alpha - undated.txt")
        app.post(("done", run["id"], {"files": [{"path": path, "est_tokens": 10}],
                                      "finished_at": "2026-01-01 10:00", "elapsed_s": 1}))
        finish.set()
        worker.join(5)
        app._close_when_idle(time.monotonic() + 60)
        saved = projects.find_project(projects.load_projects(), a["id"])
        self.assertEqual([f["path"] for f in saved["last_run"]["files"]], [path])
        with self.assertRaises(tk.TclError):
            app.root.winfo_exists()                            # the window is gone

    def test_second_close_click_closes_at_once(self):
        a = projects.new_project("Alpha")
        app = self.open_app([a])
        self.fake_run(app, a)
        finish = threading.Event()
        worker = threading.Thread(target=finish.wait, name="squish-run")
        worker.daemon = True
        worker.start()
        self.addCleanup(finish.set)
        with mock.patch.object(gui.messagebox, "askyesno", return_value=True) as ask:
            app.on_close()
            app.on_close()
        self.assertEqual(ask.call_count, 1)
        with self.assertRaises(tk.TclError):
            app.root.winfo_exists()


if __name__ == "__main__":
    unittest.main()
