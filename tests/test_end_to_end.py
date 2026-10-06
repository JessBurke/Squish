"""End-to-end tests: a folder of synthetic emails -> real readers -> real digest
-> files on disk. Also checks the seams between the modules (projects, engine,
CLI and the window's helpers all agree on the same names and shapes).

All names, addresses and text are made up.
"""

import io
import os
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from unittest import mock

from squish_app import cli, digest, engine, projects
from tests import msg_builder as mb

try:
    from squish_app import gui
except ImportError:  # this Python has no tkinter
    gui = None

UTC = timezone.utc
ORGS = "example-consulting.com=EC\nexample-builders.com.au=EB"


def eml_bytes(sender, to, subject, body, when=None, cc=(), message_id=None,
              attachments=(), headers=None):
    """A synthetic .eml file. ``when`` is a UTC datetime (None = no Date header)."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    if when is not None:
        msg["Date"] = format_datetime(when)
    if message_id:
        msg["Message-ID"] = message_id
    for name, value in (headers or {}).items():
        msg[name] = value
    msg.set_content(body)
    for name, maintype, subtype in attachments:
        msg.add_attachment(b"synthetic file", maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


SAM = "Sam Builder <sam@example-builders.com.au>"
ALEX = "Alex Citizen <alex.citizen@example-consulting.com>"
JO = "Jo Planner <jo.planner@example-consulting.com>"

FIRST = eml_bytes(
    SAM, [ALEX], "Culvert headwall levels",
    "Hi Alex,\n\n"
    "Please find attached my calc for the culvert headwall levels. Can you confirm the "
    "invert level of 12.45 m by Friday?\n\n"
    "Regards,\n"
    "Sam Builder\n"
    "Site Engineer | Example Builders\n"
    "M 0400 000 000\n"
    "Level 2, 10 Sample Street, Brisbane QLD 4000\n\n"
    "This email is confidential and intended only for the addressee. If you have received "
    "it in error please notify the sender immediately and delete this email.\n",
    when=datetime(2025, 3, 4, 12, 0, tzinfo=UTC), cc=[JO],
    message_id="<e2e-first@example-builders.com.au>",
    attachments=[("calc.pdf", "application", "pdf"), ("image001.png", "image", "png")])

REPLY = eml_bytes(
    ALEX, [SAM], "RE: Culvert headwall levels [Filed 05 Mar 2025 14:00]",
    "Hi Sam,\n\n"
    "Confirmed - the invert level is 12.45 m. Council also asked for the wingwall detail, "
    "see below.\n\n"
    "Thanks,\nAlex\n\n"
    "From: Kim Council <kim.council@example-council.gov.au>\n"
    "Sent: Monday, 3 March 2025 9:15 AM\n"
    "To: Alex Citizen <alex.citizen@example-consulting.com>\n"
    "Subject: Wingwall detail\n\n"
    "Hi Alex, please include the wingwall detail in the next issue of the drawings.\n\n"
    "Kind regards,\nKim\n",
    when=datetime(2025, 3, 5, 12, 0, tzinfo=UTC),
    message_id="<e2e-reply@example-consulting.com>")

AUTO_REPLY = eml_bytes(
    JO, [SAM], "Automatic reply: Culvert headwall levels",
    "I am out of the office until Monday with no access to email.\n",
    when=datetime(2025, 3, 4, 12, 5, tzinfo=UTC), headers={"Auto-Submitted": "auto-replied"})

THANKS = eml_bytes(
    SAM, [ALEX], "RE: Culvert headwall levels",
    "Thanks Alex\n\nSam Builder\nSite Engineer | Example Builders\nM 0400 000 000\n",
    when=datetime(2025, 3, 6, 12, 0, tzinfo=UTC))

UNDATED = eml_bytes(ALEX, [JO], "Site induction", "Induction is booked for all staff.\n")

PUMP_MSG = mb.build_msg(
    subject="Pump station RFI 12",
    body="Hi Pat,\r\n\r\nThe RFI 12 response is attached. The pump duty point is 35 L/s at "
         "18 m head.\r\n\r\nRegards\r\nJo\r\n",
    sender_name="Jo Planner", sender_email="jo.planner@example-consulting.com",
    sender_smtp="jo.planner@example-consulting.com",
    recipients=[("Pat Owner", "pat.owner@example-client.com.au", "pat.owner@example-client.com.au", 1)],
    attachments=[{"long_name": "RFI 12 response.pdf", "data": b"%PDF-1.4 synthetic",
                  "mime": "application/pdf"}],
    submit_time=datetime(2025, 3, 7, 12, 0, tzinfo=UTC),
    message_id="<e2e-pump@example-consulting.com>")

FOLDER = {
    "2025-03/01 headwall levels.eml": FIRST,
    "Filed by person/Sam Builder/headwall levels.eml": FIRST,   # Mail Manager copy
    "2025-03/02 RE headwall levels.eml": REPLY,
    "2025-03/03 automatic reply.eml": AUTO_REPLY,
    "2025-03/04 thanks.eml": THANKS,
    "2025-03/05 pump station.msg": PUMP_MSG,
    "2025-03/06 broken.msg": b"this is not an Outlook file",
    "2025-03/~$lock.msg": b"office lock file",
    "2025-03/notes.txt": b"not an email",
    "undated/site induction.eml": UNDATED,
}


class EndToEndTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="squish-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, {"SQUISH_DATA_DIR": os.path.join(self.tmp, "data")})
        env.start()
        self.addCleanup(env.stop)
        self.src = os.path.join(self.tmp, "Riverside Depot", "01 Emails")
        self.out = os.path.join(self.tmp, "out")
        for rel, data in FOLDER.items():
            path = os.path.join(self.src, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)
        self.project = projects.new_project("E2E Job")
        self.project.update(source_folder=self.src, output_folder=self.out, org_codes=ORGS)

    def read(self, path):
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return fh.read()

    def test_folder_to_digest_files(self):
        result = engine.run_project(self.project)

        # The run itself.
        self.assertFalse(result["cancelled"])
        self.assertEqual(result["files_found"], 8)      # ~$lock.msg and notes.txt skipped
        self.assertEqual(result["files_read"], 7)
        self.assertEqual([os.path.basename(p) for p, _ in result["failed"]], ["06 broken.msg"])
        stats = result["stats"]
        self.assertEqual(stats["emails_in"], 7)
        self.assertEqual(stats["duplicates"], 1)
        self.assertEqual(stats["noise_dropped"], 1)
        self.assertEqual(stats["emails_used"], 5)
        self.assertEqual(stats["threads"], 3)
        self.assertEqual(stats["recovered_quoted"], 1)
        self.assertEqual(stats["acks"], 1)
        self.assertEqual(stats["outside_dates"], 0)

        # One file on disk, named as the contract says.
        self.assertEqual(os.listdir(self.out), ["Squish - E2E Job - 2025-03-03 to 2025-03-07.txt"])
        info = result["files"][0]
        text = self.read(info["path"])
        self.assertEqual(info["chars"], len(text))
        self.assertEqual(info["bytes"], len(text.encode("utf-8")))
        self.assertEqual((info["first_date"], info["last_date"]), ("2025-03-03", "2025-03-07"))
        self.assertEqual((info["emails"], info["threads"]), (5, 3))
        self.assertNotIn("\r", text)
        self.assertTrue(text.endswith("\n"))

        # Header and legend.
        lines = text.split("\n")
        self.assertEqual(lines[0], "SQUISH EMAIL DIGEST | E2E Job")
        self.assertEqual(lines[1], "Covers 2025-03-03 to 2025-03-07 | 5 emails in 3 threads")
        self.assertTrue(lines[2].startswith("Source: %s | squeeze: standard | made " % self.src))
        # The damaged .msg is said in the header, so Claude knows emails may be missing.
        self.assertEqual(lines[3], "Not included: 1 email file Squish could not read (damaged or "
                                   "locked), so emails may be missing")
        self.assertEqual(stats["unreadable_files"], 1)
        self.assertIn("1 duplicate copy,", text)
        self.assertIn("1 auto-reply", text)
        self.assertNotIn("1 auto-replies", text)
        self.assertIn("  EB = example-builders.com.au: SB=Sam Builder", lines)
        self.assertTrue(any(l.startswith("  EC = example-consulting.com: ") and "AC=Alex Citizen" in l
                            and "JP=Jo Planner" in l for l in lines), text)

        # The emails: new text kept, signatures, disclaimers and noise gone.
        self.assertIn("## Culvert headwall levels (3 emails + 1 recovered, 25-03-03 to 25-03-06)", lines)
        self.assertRegex(text, r"25-03-04 \d\d:\d\d EB\.SB>EC\.AC.*: Hi Alex, Please find attached my "
                               r"calc for the culvert headwall levels\. Can you confirm the invert "
                               r"level of 12\.45 m by Friday\? \[att: calc\.pdf\]\n")
        self.assertIn("Confirmed - the invert level is 12.45 m.", text)
        self.assertRegex(text, r"\n  \u21b3 25-03-03 \d\d:\d\d [^\n]*please include the wingwall "
                               r"detail in the next issue of the drawings\.")
        self.assertIn("(ack)", text)
        self.assertIn("## Pump station RFI 12", lines)
        self.assertIn("The pump duty point is 35 L/s at 18 m head. [att: RFI 12 response.pdf]", text)
        self.assertIn("## Site induction", lines)
        self.assertRegex(text, r"\(no date\) EC\.AC>EC\.JP: Induction is booked for all staff\.")
        for leak in ("image001", "0400 000 000", "Sample Street", "confidential", "Site Engineer",
                     "out of the office", "From:", "Sent:"):
            self.assertNotIn(leak, text)

        # The run log.
        log = self.read(result["log_path"])
        self.assertIn("Files that could not be read (1):", log)
        self.assertIn("06 broken.msg", log)

        # A second run reads everything from the cache and writes the same digest.
        again = engine.run_project(self.project)
        self.assertEqual((again["files_read"], again["from_cache"]), (0, 7))
        self.assertEqual(os.listdir(self.out), ["Squish - E2E Job - 2025-03-03 to 2025-03-07.txt"])
        made = re.compile(r"made \d{4}-\d{2}-\d{2} \d{2}:\d{2}")
        self.assertEqual(made.sub("made X", self.read(again["files"][0]["path"])), made.sub("made X", text))

    def test_folder_without_access_is_named_in_the_header(self):
        engine.run_project(self.project)
        real_scandir = os.scandir

        def scandir(path="."):
            if os.path.basename(str(path)) == "undated":
                raise PermissionError(13, "Access is denied", str(path))
            return real_scandir(path)

        with mock.patch.object(engine.os, "scandir", side_effect=scandir):
            result = engine.run_project(self.project)
        self.assertEqual(result["stats"]["no_access_emails"], 1)
        text = self.read(result["files"][0]["path"])
        self.assertIn("\nNot included: 1 email in folders Squish could not open (no access)\n", text)
        self.assertNotIn("## Site induction", text)

    def test_parts_replace_the_single_file(self):
        engine.run_project(self.project)
        self.project["part_size"] = "small"
        with mock.patch.dict(digest.PART_SIZES["small"], {"chars": 1800}):
            result = engine.run_project(self.project)
        names = sorted(os.listdir(self.out))
        count = len(result["files"])
        self.assertGreater(count, 1)
        # Each part is named with its own dates; the single file is gone.
        self.assertEqual(names, ["Squish - E2E Job - %s to %s (part %d of %d).txt"
                                 % (f["first_date"], f["last_date"], i + 1, count)
                                 for i, f in enumerate(result["files"])])
        self.assertEqual(names[0], "Squish - E2E Job - 2025-03-03 to 2025-03-06 (part 1 of 2).txt")
        texts = [self.read(f["path"]) for f in result["files"]]
        for i, text in enumerate(texts):
            # Every part stands alone: its own header and its own people list.
            self.assertTrue(text.startswith("SQUISH EMAIL DIGEST | E2E Job | part %d of %d\n" % (i + 1, count)))
            self.assertIn("People (ORG.Initials):", text)
        joined = "".join(texts)
        for heading in ("## Culvert headwall levels", "## Pump station RFI 12", "## Site induction"):
            self.assertEqual(joined.count(heading), 1, heading)
        self.assertEqual(sum(f["emails"] for f in result["files"]), 5)

    def test_date_filter_and_focus_keywords(self):
        self.project.update(date_from="2025-03-05", focus_keywords="wingwall")
        result = engine.run_project(self.project)
        self.assertEqual(result["stats"]["outside_dates"], 3)   # 4 March: first email, its copy, auto-reply
        # The file name says it holds only part of the emails, so it is never
        # taken for (or written over) the full digest.
        self.assertRegex(os.path.basename(result["files"][0]["path"]),
                         r"^Squish - E2E Job - \S+ to \S+ \(only from 2025-03-05\) \(focus wingwall\)\.txt$")
        if gui is not None:
            self.assertTrue(gui.run_was_filtered({"files": result["files"]}))
        text = self.read(result["files"][0]["path"])
        self.assertIn("Focus keywords: wingwall", text)
        self.assertIn("## Culvert headwall levels", text)
        self.assertNotIn("Pump station", text)
        self.assertNotIn("Site induction", text)

    def test_cli_runs_the_same_folder(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["run", "--source", self.src, "--out", self.out, "--name", "E2E Job",
                             "--org", "example-consulting.com=EC,example-builders.com.au=EB"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("Found 8 email files (7 read, 0 from cache, 1 could not be read)", out.getvalue())
        self.assertIn("Squish - E2E Job - 2025-03-03 to 2025-03-07.txt", out.getvalue())
        self.assertIn("EB.SB>EC.AC", self.read(os.path.join(self.out, os.listdir(self.out)[0])))


def attached_email_msg():
    """A filed email that only says "Approval attached", with the client's
    approval (never filed itself) attached as an email."""
    approval = mb.message_tree(
        subject="RE: Variation 3",
        body="Hi Jo,\r\n\r\nVariation 3 for the extra wingwall is approved at $8,400 excl "
             "GST. Please proceed.\r\n\r\nRegards\r\nPat\r\n",
        sender_name="Pat Owner", sender_email="pat.owner@example-client.com.au",
        sender_smtp="pat.owner@example-client.com.au",
        recipients=[("Jo Planner", "jo.planner@example-consulting.com",
                     "jo.planner@example-consulting.com", 1)],
        submit_time=datetime(2025, 3, 10, 5, 30, tzinfo=UTC), embedded=True)
    return mb.build_msg(
        subject="FW: Variation 3", body="Approval attached.\r\n",
        sender_name="Jo Planner", sender_email="jo.planner@example-consulting.com",
        sender_smtp="jo.planner@example-consulting.com",
        recipients=[("Alex Citizen", "alex.citizen@example-consulting.com",
                     "alex.citizen@example-consulting.com", 1)],
        submit_time=datetime(2025, 3, 11, 0, 0, tzinfo=UTC),
        attachments=[{"display_name": "RE: Variation 3", "embedded": approval}])


class AttachedEmailEndToEndTests(unittest.TestCase):
    """An email attached to a filed email goes through the real readers (built-in
    and, when installed, extract-msg) into the digest as a recovered email."""

    def test_attached_email_reaches_the_digest(self):
        tmp = tempfile.mkdtemp(prefix="squish-e2e-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        src = os.path.join(tmp, "emails")
        os.makedirs(src)
        with open(os.path.join(src, "variation 3.msg"), "wb") as fh:
            fh.write(attached_email_msg())
        for no_extract in ("1", ""):
            data = os.path.join(tmp, "data" + no_extract)
            with mock.patch.dict(os.environ, {"SQUISH_DATA_DIR": data,
                                              "SQUISH_NO_EXTRACT_MSG": no_extract}):
                project = projects.new_project("Attached")
                project.update(source_folder=src, output_folder=os.path.join(tmp, "out" + no_extract),
                               org_codes=ORGS)
                result = engine.run_project(project)
                cached = list(engine.load_cache(engine.cache_path(project)).values())
            if no_extract:
                self.assertEqual(cached[0]["record"]["reader"], "builtin_msg")
            with open(result["files"][0]["path"], encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("EC.JP>EC.AC: Approval attached. [att: RE: Variation 3.msg]", text)
            self.assertRegex(text, r"\n  \u21b3 25-03-10 \d\d:30 EXAMPL\.PO: Hi Jo, Variation 3 for the "
                                   r"extra wingwall is approved at \$8,400 excl GST\. Please proceed\.")
            self.assertEqual(result["stats"]["recovered_quoted"], 1)


class SeamTests(unittest.TestCase):
    """The modules agree on setting names, defaults and result shapes."""

    def test_project_choices_match_the_digest_tables(self):
        self.assertEqual(list(projects.SQUEEZE_CHOICES), list(digest.SQUEEZE_LEVELS))
        self.assertEqual(list(projects.PART_SIZE_CHOICES), list(digest.PART_SIZES))
        self.assertEqual(engine.part_size_keys(), list(digest.PART_SIZES))
        parser = cli.build_parser()
        args = parser.parse_args(["run", "--squeeze", "max", "--part-size", "single", "--source", "x"])
        self.assertEqual((args.squeeze, args.part_size), ("max", "single"))

    def test_new_project_defaults(self):
        p = projects.new_project("Job")
        self.assertEqual(p["squeeze"], digest.DEFAULT_SQUEEZE)
        self.assertEqual(p["part_size"], digest.DEFAULT_PART_SIZE)
        self.assertEqual(p["org_codes"], "slrconsulting.com=SLR")
        self.assertTrue(p["include_subfolders"] and p["drop_noise"] and p["recover_quoted"])
        self.assertIsNone(p["last_run"])

    @unittest.skipIf(gui is None, "tkinter is not available")
    def test_window_helpers_read_a_real_run_result(self):
        tmp = tempfile.mkdtemp(prefix="squish-e2e-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        src = os.path.join(tmp, "emails")
        os.makedirs(src)
        for name, data in (("a.eml", FIRST), ("b.eml", REPLY), ("c.msg", b"broken")):
            with open(os.path.join(src, name), "wb") as fh:
                fh.write(data)
        with mock.patch.dict(os.environ, {"SQUISH_DATA_DIR": os.path.join(tmp, "data")}):
            project = projects.new_project("Seams")
            project.update(source_folder=src, output_folder=os.path.join(tmp, "out"))
            result = engine.run_project(project)
            last = gui.last_run_from_result(result)
            project["last_run"] = last
            projects.save_projects([project])
            loaded = projects.load_projects()[0]["last_run"]
        self.assertEqual(loaded, last)
        self.assertNotIn("failed", loaded)
        self.assertEqual(loaded["failed_count"], 1)
        self.assertEqual(gui.summary_text(loaded), gui.summary_text(result))
        self.assertIn("\u2192 2 used in 1 thread", gui.summary_text(loaded))
        self.assertIn("1 file couldn't be read", gui.summary_text(loaded))
        self.assertEqual(gui.short_file_name(result["files"][0]["path"]),
                         "2025-03-03 to 2025-03-05.txt")


if __name__ == "__main__":
    unittest.main()
