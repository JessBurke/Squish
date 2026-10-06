"""Tests for squish_app.digest (synthetic emails only)."""

import itertools
import os
import random
import re
import tempfile
import time
import unittest
from datetime import datetime, timedelta

from squish_app import digest

_TMP = None
NOW = datetime(2026, 10, 6, 14, 5)
ORGS = "example-consulting.com=EC\nexample-builders.com.au=EB"


def setUpModule():
    global _TMP
    _TMP = tempfile.TemporaryDirectory()
    os.environ["SQUISH_DATA_DIR"] = _TMP.name


def tearDownModule():
    _TMP.cleanup()


ALEX = ("Alex Citizen", "alex.citizen@example-consulting.com")
JO = ("Jo Planner", "jo.planner@example-consulting.com")
SAM = ("Sam Builder", "sam@example-builders.com.au")
PAT = ("Pat Owner", "pat.owner@example-client.com.au")

_counter = [0]


def rec(sender, subject, body, date="2025-03-04T09:00:00+10:00", to=(), cc=(), attachments=(), **extra):
    """A synthetic EmailRecord."""
    _counter[0] += 1
    r = {
        "path": "C:\\mail\\%04d.msg" % _counter[0],
        "date": date,
        "sender_name": sender[0],
        "sender_email": sender[1],
        "to": [list(p) for p in to],
        "cc": [list(p) for p in cc],
        "subject": subject,
        "body": body,
        "attachments": [a if isinstance(a, dict) else {"name": a, "size": 1000, "inline": False}
                        for a in attachments],
        "message_id": "",
        "in_reply_to": "",
        "conversation_topic": "",
        "item_class": "IPM.Note",
        "auto_reply": False,
        "reader": "eml",
    }
    r.update(extra)
    return r


def project(**kw):
    p = {"name": "Test Job", "squeeze": "standard", "part_size": "single", "focus_keywords": "",
         "org_codes": ORGS, "drop_noise": True, "recover_quoted": True}
    p.update(kw)
    return p


def build(records, **kw):
    return digest.build_digest(records, project(**kw), source_label="H:\\Jobs\\Emails", now=NOW)


def text_of(result):
    return "\n".join(p["text"] for p in result["parts"])


def email_lines(text):
    return [l for l in text.split("\n") if re.match(r"^(?:\d\d-\d\d-\d\d )?\d\d:\d\d |^\(no date\)", l)]


def quote_block(sender, sent, to, subject, body):
    return ("\nFrom: %s <%s>\nSent: %s\nTo: %s <%s>\nSubject: %s\n\n%s\n"
            % (sender[0], sender[1], sent, to[0], to[1], subject, body))


class TableTests(unittest.TestCase):
    def test_part_sizes(self):
        self.assertEqual(list(digest.PART_SIZES), ["small", "medium", "large", "single"])
        self.assertEqual(digest.PART_SIZES["small"]["chars"], 200000)
        self.assertEqual(digest.PART_SIZES["medium"]["chars"], 480000)
        self.assertEqual(digest.PART_SIZES["large"]["chars"], 1000000)
        self.assertIsNone(digest.PART_SIZES["single"]["chars"])
        for key, info in digest.PART_SIZES.items():
            self.assertEqual(info["key"], key)
            self.assertTrue(info["label"] and info["description"])

    def test_squeeze_levels(self):
        self.assertEqual(list(digest.SQUEEZE_LEVELS), ["light", "standard", "max"])
        self.assertIsNone(digest.SQUEEZE_LEVELS["light"]["cap"])
        self.assertEqual(digest.SQUEEZE_LEVELS["standard"]["cap"], 1500)
        self.assertEqual(digest.SQUEEZE_LEVELS["max"]["cap"], 500)
        for key, info in digest.SQUEEZE_LEVELS.items():
            self.assertEqual(info["key"], key)
            self.assertTrue(info["label"] and info["description"])


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            rec(SAM, "Culvert headwall dimensions", "Hi Alex,\n\nHere's my calc on headwall length.\n\nRegards,\nSam\n",
                date="2026-02-20T10:34:00+10:00", to=[ALEX], attachments=["calc.pdf", "image001.png"]),
            rec(ALEX, "RE: Culvert headwall dimensions [Filed 23 Feb 2026 18:30]",
                "Hi Sam, Correct, confirming the headwalls are 300mm.\n\nCheers,\nAlex\n",
                date="2026-02-23T18:22:00+10:00", to=[SAM]),
        ]

    def test_header_and_lines(self):
        res = build(self.records)
        text = text_of(res)
        lines = text.split("\n")
        self.assertEqual(lines[0], "SQUISH EMAIL DIGEST | Test Job")
        self.assertEqual(lines[1], "Covers 2026-02-20 to 2026-02-23 | 2 emails in 1 thread")
        self.assertEqual(lines[2], "Source: H:\\Jobs\\Emails | squeeze: standard | made 2026-10-06 14:05")
        self.assertIn("People (ORG.Initials):", lines)
        self.assertIn("  EC = example-consulting.com: AC=Alex Citizen", lines)
        self.assertIn("  EB = example-builders.com.au: SB=Sam Builder", lines)
        self.assertIn("## Culvert headwall dimensions (2 emails, 26-02-20 to 26-02-23)", lines)
        self.assertIn("26-02-20 10:34 EB.SB>EC.AC: Hi Alex, Here's my calc on headwall length. [att: calc.pdf]", lines)
        self.assertIn("26-02-23 18:22 EC.AC>EB.SB: Hi Sam, Correct, confirming the headwalls are 300mm.", lines)
        self.assertTrue(text.endswith("\n"))
        self.assertNotIn("\r", text)
        self.assertIn("  YY-MM-DD HH:MM FROM>TO: new text only (local time; date omitted when same as the line above).",
                      lines)
        self.assertIn("TO = To recipients only (not Cc)", text)
        self.assertNotIn("Dates:", text)

    def test_header_says_what_the_date_filter_left_out(self):
        res = digest.build_digest(self.records, project(date_from="2026-02-01", date_to="2026-02-28"),
                                  source_label="H:\\Jobs\\Emails", now=NOW, outside_dates=7)
        self.assertIn("Dates: only 2026-02-01 to 2026-02-28 - 7 email files outside these dates are not included",
                      text_of(res))
        res = digest.build_digest(self.records, project(date_from="2026-02-01"), now=NOW, outside_dates=1)
        self.assertIn("Dates: only from 2026-02-01 - 1 email file outside these dates is not included", text_of(res))

    def test_singular_counts_in_dropped_line(self):
        recs = self.records + [rec(SAM, "Read: Culvert headwall dimensions", "Your message was read.",
                                   date="2026-02-21T09:00:00+10:00")]
        self.assertIn("Dropped: 1 receipt\n", text_of(build(recs)))

    def test_part_metadata_and_stats(self):
        res = build(self.records)
        part = res["parts"][0]
        self.assertEqual((part["first_date"], part["last_date"], part["emails"], part["threads"]),
                         ("2026-02-20", "2026-02-23", 2, 1))
        st = res["stats"]
        for key in ("emails_in", "emails_used", "duplicates", "noise_dropped", "acks_dropped", "filtered_out",
                    "threads", "recovered_quoted", "raw_chars", "output_chars"):
            self.assertIn(key, st)
        self.assertEqual((st["emails_in"], st["emails_used"], st["threads"]), (2, 2, 1))
        self.assertEqual(st["output_chars"], len(part["text"]))
        self.assertEqual(st["raw_chars"], sum(len(r["body"]) for r in self.records))

    def test_date_only_shown_when_it_changes(self):
        recs = [rec(ALEX, "Pour", "First note about the pour.", date="2025-03-04T09:00:00+10:00", to=[SAM]),
                rec(SAM, "RE: Pour", "Second note about the pour.", date="2025-03-04T11:30:00+10:00", to=[ALEX]),
                rec(ALEX, "RE: Pour", "Third note about the pour.", date="2025-03-05T08:15:00+10:00", to=[SAM])]
        lines = email_lines(text_of(build(recs)))
        self.assertTrue(lines[0].startswith("25-03-04 09:00 "))
        self.assertTrue(lines[1].startswith("11:30 EB.SB>"))
        self.assertTrue(lines[2].startswith("25-03-05 08:15 "))

    def test_new_thread_always_shows_date(self):
        recs = [rec(ALEX, "Pour", "Note one.", date="2025-03-04T09:00:00+10:00"),
                rec(ALEX, "Drainage", "Note two.", date="2025-03-04T10:00:00+10:00")]
        lines = email_lines(text_of(build(recs)))
        self.assertTrue(all(l.startswith("25-03-04 ") for l in lines))

    def test_threads_ordered_by_first_email_and_title_is_cleanest(self):
        recs = [rec(SAM, "RE: Beta topic", "b2", date="2025-03-06T09:00:00+10:00"),
                rec(ALEX, "Alpha topic", "a1", date="2025-03-05T09:00:00+10:00"),
                rec(ALEX, "Beta topic", "b1", date="2025-03-01T09:00:00+10:00"),
                rec(SAM, "FW: alpha TOPIC (Filed 7 Mar 2025 10:00)", "a2", date="2025-03-07T09:00:00+10:00")]
        heads = [l for l in text_of(build(recs)).split("\n") if l.startswith("## ")]
        self.assertEqual(heads, ["## Beta topic (2 emails, 25-03-01 to 25-03-06)",
                                 "## Alpha topic (2 emails, 25-03-05 to 25-03-07)"])

    def test_undated_email_sorts_last(self):
        recs = [rec(ALEX, "Pour", "Draft never sent.", date=""),
                rec(SAM, "RE: Pour", "Sent one.", date="2025-03-04T09:00:00+10:00")]
        lines = email_lines(text_of(build(recs)))
        self.assertTrue(lines[0].startswith("25-03-04 09:00 EB.SB"))
        self.assertTrue(lines[1].startswith("(no date) EC.AC"))

    def test_recipients_people_or_orgs(self):
        many = [JO, SAM, PAT]
        recs = [rec(ALEX, "One", "To two people.", to=[SAM, JO]),
                rec(ALEX, "Two", "To three people.", to=many)]
        lines = email_lines(text_of(build(recs)))
        self.assertIn("EC.AC>EB.SB,EC.JP: To two people.", lines[0])
        self.assertIn("EC.AC>EC,EB,EXAMPL: To three people.", lines[1])
        light = email_lines(text_of(build(recs, squeeze="light")))
        self.assertIn("EC.AC>EC.JP,EB.SB,EXAMPL.PO: To three people.", light[1])
        mx = email_lines(text_of(build(recs, squeeze="max")))
        self.assertIn("EC.AC>EB,EC: To two people.", mx[0])

    def test_deterministic_and_order_independent(self):
        recs = [rec(SAM, "Topic %d" % (i % 3), "Message number %d about the slab." % i,
                    date="2025-03-%02dT09:00:00+10:00" % (i + 1), to=[ALEX]) for i in range(12)]
        a = text_of(build(recs))
        b = text_of(build(list(recs)))
        shuffled = list(recs)
        random.Random(4).shuffle(shuffled)
        c = text_of(build(shuffled))
        self.assertEqual(a, b)
        self.assertEqual(a, c)


class CancelAndProgressTests(unittest.TestCase):
    def test_progress_reported_and_cancel_raises(self):
        import threading
        recs = [rec(SAM, "Topic %d" % i, "Message %d." % i, date="2025-03-%02dT09:00:00+10:00" % (i % 28 + 1))
                for i in range(120)]
        seen = []
        digest.build_digest(recs, project(), now=NOW, progress=lambda d, t: seen.append((d, t)))
        self.assertEqual(seen[-1], (120, 120))
        self.assertTrue(all(t == 120 and 0 <= d <= 120 for d, t in seen))
        self.assertEqual([d for d, _ in seen], sorted(d for d, _ in seen))
        stop = threading.Event()
        stop.set()
        with self.assertRaises(digest.DigestCancelled):
            digest.build_digest(recs, project(), now=NOW, cancel=stop)


class DedupTests(unittest.TestCase):
    def test_same_message_id(self):
        a = rec(SAM, "Pour", "Body one.", message_id="<abc@example>")
        b = dict(a, path="C:\\other\\copy.msg")
        res = build([a, b])
        self.assertEqual(res["stats"]["duplicates"], 1)
        self.assertEqual(res["stats"]["emails_used"], 1)
        self.assertIn("Dropped: 1 duplicate copy", text_of(res))

    def test_same_sender_minute_and_text(self):
        a = rec(SAM, "Pour", "Same body text.\n\nRegards,\nSam", date="2025-03-04T09:00:00+10:00")
        b = rec(SAM, "RE: Pour [Filed 4 Mar 2025 10:00]", "Same body text.\n\nRegards,\nSam Builder\nM: 0400 000 000",
                date="2025-03-04T09:00:30+10:00")
        c = rec(SAM, "Pour", "Same body text.", date="2025-03-04T09:05:00+10:00")
        res = build([a, b, c])
        self.assertEqual(res["stats"]["duplicates"], 1)
        self.assertEqual(res["stats"]["emails_used"], 2)


    def test_same_minute_different_attachments_are_not_duplicates(self):
        a = rec(SAM, "Drawings", "See attached.", date="2025-03-04T09:00:00+10:00", attachments=["ST-01.pdf"])
        b = rec(SAM, "Drawings", "See attached.", date="2025-03-04T09:00:40+10:00", attachments=["ST-02.pdf"])
        res = build([a, b])
        self.assertEqual(res["stats"]["duplicates"], 0)
        self.assertIn("ST-02.pdf", text_of(res))

    def test_fyi_forwards_of_different_emails_are_not_duplicates(self):
        a = rec(ALEX, "FW: DA conditions", "FYI" + quote_block(PAT, "Monday, 3 March 2025 8:00 AM", ALEX,
                                                                "DA conditions", "Condition 12 needs a geotech report."),
                date="2025-03-04T09:15:05+10:00")
        b = rec(ALEX, "FW: Pump lead time", "FYI" + quote_block(SAM, "Monday, 3 March 2025 11:00 AM", ALEX,
                                                                 "Pump lead time", "Lead time is now 22 weeks."),
                date="2025-03-04T09:15:40+10:00")
        res = build([a, b])
        self.assertEqual((res["stats"]["duplicates"], res["stats"]["emails_used"]), (0, 2))
        self.assertIn("Condition 12 needs a geotech report.", text_of(res))
        self.assertIn("Lead time is now 22 weeks.", text_of(res))
        c = rec(ALEX, "FW: Pump", "FYI" + quote_block(PAT, "Monday, 3 March 2025 8:00 AM", ALEX, "Pump", "Pump A."),
                date="2025-03-04T09:15:05+10:00")
        d = rec(ALEX, "FW: Pump", "FYI" + quote_block(SAM, "Monday, 3 March 2025 9:30 AM", ALEX, "Pump", "Pump B."),
                date="2025-03-04T09:15:40+10:00")
        self.assertEqual(build([c, d])["stats"]["duplicates"], 0)

    def test_blank_and_signature_only_copies_merge(self):
        a = rec(SAM, "Plans", "", date="2025-03-04T09:00:00+10:00", attachments=["Plan.pdf"])
        b = rec(SAM, "Plans", "\n\nSam Builder\nProject Manager\nM 0400 000 000\nE sam@example-builders.com.au\n",
                date="2025-03-04T09:00:20+10:00", attachments=["Plan.pdf"])
        res = build([a, b])
        self.assertEqual(res["stats"]["duplicates"], 1)
        self.assertIn("09:00 EB.SB: [att: Plan.pdf]", text_of(res))
        c = rec(SAM, "Plans", "", date="2025-03-05T09:00:00+10:00")
        d = rec(SAM, "Plans", "Sam Builder\nProject Manager\nM 0400 000 000\n", date="2025-03-05T09:00:20+10:00")
        res = build([c, d])
        self.assertEqual(res["stats"]["duplicates"], 1)
        self.assertIn("EB.SB: (no text)", text_of(res))


class PeopleTests(unittest.TestCase):
    def test_alias_collision_uses_next_surname_letter(self):
        jb1 = ("Jane Brown", "jane.brown@example-consulting.com")
        jb2 = ("John Bloggs", "john.bloggs@example-consulting.com")
        recs = [rec(jb1, "A", "One.", date="2025-03-01T09:00:00+10:00"),
                rec(jb1, "B", "Two.", date="2025-03-02T09:00:00+10:00"),
                rec(jb2, "C", "Three.", date="2025-03-03T09:00:00+10:00")]
        text = text_of(build(recs))
        self.assertIn("  EC = example-consulting.com: JB=Jane Brown, JBl=John Bloggs", text)

    def test_alias_collision_falls_back_to_number(self):
        a = ("Jo Bo", "jo.bo@example-consulting.com")
        b = ("Jim Bo", "jim.bo@example-consulting.com")
        text = text_of(build([rec(a, "A", "One."), rec(a, "B", "Two."), rec(b, "C", "Three.")]))
        self.assertIn("JB=Jo Bo", text)
        self.assertIn("JBo=Jim Bo", text)
        c = ("Jen Bo", "jen.bo@example-consulting.com")
        text = text_of(build([rec(a, "A", "One."), rec(a, "B", "Two."), rec(b, "C", "Three."),
                              rec(b, "D", "Four."), rec(c, "E", "Five.")]))
        self.assertIn("JB2=Jen Bo", text)

    def test_org_codes_and_derived_codes(self):
        recs = [rec(("Lee Smith", "lee@mail.example-builders.com.au"), "A", "Subdomain."),
                rec(("Kim Lee", "kim@pipeworks-example.com"), "B", "Derived."),
                rec(("Ray Ng", "ray@council.qld.gov.au"), "C", "Gov.")]
        text = text_of(build(recs))
        self.assertIn("EB.LS:", text)
        self.assertIn("PIPEWO.KL:", text)
        self.assertIn("COUNCI.RN:", text)
        self.assertIn("  PIPEWO = pipeworks-example.com: KL=Kim Lee", text)

    def test_same_person_two_addresses_one_alias(self):
        recs = [rec(("Alex Citizen", "alex.citizen@example-consulting.com"), "A", "One."),
                rec(("Citizen, Alex", "a.citizen@mail.example-consulting.com"), "B", "Two.")]
        text = text_of(build(recs))
        self.assertEqual(text.count("=Alex Citizen"), 1)
        self.assertNotIn("AC2", text)

    def test_unknown_sender_resolved_by_display_name(self):
        recs = [rec(("Alex Citizen", ""), "A", "Filed with only an X500 address."),
                rec(SAM, "B", "Hello.", to=[ALEX])]
        text = text_of(build(recs))
        self.assertIn("EC.AC: Filed with only an X500", text)
        self.assertNotIn("?.", text)

    def test_unknown_sender_resolved_from_quoted_header(self):
        body = "Noted." + quote_block(SAM, "Monday, 3 March 2025 9:00 AM", ("Alex Citizen", ALEX[1]), "A", "Hi.")
        recs = [rec(("Alex Citizen", ""), "RE: A", body, date="2025-03-04T09:00:00+10:00")]
        self.assertIn("EC.AC:", text_of(build(recs)))

    def test_unknown_sender_resolved_from_local_part(self):
        recs = [rec(("janecitizen", ""), "A", "Name-only sender."),
                rec(ALEX, "B", "Hi.", to=[("Jane Citizen", "janecitizen@example-client.com.au")])]
        self.assertIn("EXAMPL.JC: Name-only sender.", text_of(build(recs)))

    def test_unresolved_sender_goes_to_unknown_org(self):
        text = text_of(build([rec(("Morgan Mystery", ""), "A", "Who am I?")]))
        self.assertIn("?.MM: Who am I?", text)
        self.assertIn("  ? = address unknown: MM=Morgan Mystery", text)

    def test_accented_and_non_latin_names(self):
        self.assertEqual(next(digest._alias_candidates("Hans M\u00fcller")), "HM")
        self.assertEqual(next(digest._alias_candidates("\u00c9milie Dubois")), "\u00c9D")
        recs = [rec(("\u738b\u5c0f\u660e", ""), "A", "First unknown sender."),
                rec(("\u041e\u043b\u044c\u0433\u0430 \u0418\u0432\u0430\u043d\u043e\u0432\u0430", ""), "B",
                    "Second unknown sender.")]
        text = text_of(build(recs))
        first = [l for l in email_lines(text) if "First unknown" in l][0]
        second = [l for l in email_lines(text) if "Second unknown" in l][0]
        self.assertNotEqual(first.split()[2], second.split()[2])
        self.assertIn("?.", first)
        legend = [l for l in text.split("\n") if l.startswith("  ? = address unknown: ")][0]
        self.assertIn("\u738b\u5c0f\u660e", legend)
        self.assertIn("\u041e\u043b\u044c\u0433\u0430", legend)

    def test_same_person_with_and_without_accents(self):
        recs = [rec(("Jos\u00e9 \u00c1lvarez", ""), "A", "Name with accents."),
                rec(SAM, "B", "Hello.", to=[("Jose Alvarez", "jose.alvarez@example-consulting.com")])]
        self.assertIn("EC.JA: Name with accents.", text_of(build(recs)))

    def test_recipient_only_org_is_explained(self):
        recs = [rec(SAM, "A", "To several orgs.", to=[ALEX, ("Kim Pump", "kim@acmepumps-example.com.au"), PAT])]
        mx = text_of(build(recs, squeeze="max"))
        self.assertIn("  ACMEPU = acmepumps-example.com.au", mx.split("\n"))
        self.assertIn("  EC = example-consulting.com", mx.split("\n"))
        std = text_of(build([rec(SAM, "A", "To one person.", to=[ALEX])]))
        self.assertIn("  EC = example-consulting.com: AC=Alex Citizen", std.split("\n"))

    def test_legend_lists_only_people_in_the_part(self):
        recs = [rec(ALEX, "A", "Message one.", to=[JO, SAM, PAT])]
        text = text_of(build(recs))
        self.assertIn("AC=Alex Citizen", text)
        self.assertNotIn("Jo Planner", text)     # shown only as the org list, so not in the legend
        self.assertNotIn("Pat Owner", text)


class AttachmentTests(unittest.TestCase):
    def test_inline_attachments_never_listed(self):
        recs = [rec(SAM, "A", "See attached.", attachments=[
            "image001.png", "Outlook-logo.png", "~WRL0001.tmp", {"name": "photo.jpg", "size": 10, "inline": True},
            "Pile layout.pdf"])]
        text = text_of(build(recs, squeeze="light"))
        self.assertIn("[att: Pile layout.pdf]", text)
        for bad in ("image001", "Outlook-", "WRL", "photo.jpg"):
            self.assertNotIn(bad, text)

    def test_repeated_attachment_counted_as_above(self):
        recs = [rec(SAM, "A", "First.", date="2025-03-04T09:00:00+10:00", attachments=["Plan.pdf", "IMG_1.jpeg"]),
                rec(ALEX, "RE: A", "Second.", date="2025-03-04T10:00:00+10:00",
                    attachments=["Plan.pdf", "Calc.xlsx"])]
        text = text_of(build(recs))
        lines = email_lines(text)
        self.assertTrue(lines[0].endswith("[att: Plan.pdf; IMG_1.jpeg]"))
        self.assertTrue(lines[1].endswith("Second. [att: Calc.xlsx; +1 as above]"))
        self.assertIn('"N as above" in [att: ...] = N files whose names were listed earlier in the thread', text)
        light = text_of(build(recs, squeeze="light"))
        self.assertTrue(email_lines(light)[1].endswith("[att: Plan.pdf; Calc.xlsx]"))
        self.assertNotIn("as above", light)
        mx = email_lines(text_of(build(recs, squeeze="max")))
        self.assertTrue(mx[0].endswith("First. [att: Plan.pdf]"))
        self.assertTrue(mx[1].endswith("Second. [att: Calc.xlsx; +1 as above]"))

    def test_revised_file_with_the_same_name_is_listed_again(self):
        recs = [rec(SAM, "Drawings", "For review.", date="2025-03-04T09:00:00+10:00", attachments=["ST-1060.pdf"]),
                rec(SAM, "RE: Drawings", "Drawing 1060 updated for your review.", date="2025-03-05T09:00:00+10:00",
                    attachments=["ST-1060.pdf"])]
        lines = email_lines(text_of(build(recs)))
        self.assertTrue(lines[1].endswith("updated for your review. [att: ST-1060.pdf]"))

    def test_forward_resending_files_is_not_no_text(self):
        recs = [rec(SAM, "Drawings", "For review.", date="2025-03-04T09:00:00+10:00",
                    attachments=["ST-01.pdf", "ST-02.pdf"]),
                rec(ALEX, "FW: Drawings", "", date="2025-03-05T09:00:00+10:00", to=[PAT],
                    attachments=["ST-01.pdf", "ST-02.pdf", "image001.png"])]
        text = text_of(build(recs))
        self.assertIn("EC.AC>EXAMPL.PO: [att: 2 as above]", text)
        self.assertNotIn("(no text)", text)

    def test_camera_photos_grouped_at_standard(self):
        recs = [rec(SAM, "Site photos", "Photos from today.", attachments=["IMG_1001.jpeg", "IMG_1002.jpeg",
                                                                           "Pit 3 detail.jpg", "IMG_1003.jpeg"])]
        self.assertIn("[att: 3 photos IMG_1001,1002,1003; Pit 3 detail.jpg]", text_of(build(recs)))
        self.assertIn("[att: IMG_1001.jpeg; IMG_1002.jpeg; Pit 3 detail.jpg; IMG_1003.jpeg]",
                      text_of(build(recs, squeeze="light")))


class RecoveryTests(unittest.TestCase):
    def unfiled_reply(self, date="2025-03-05T09:00:00+10:00"):
        q = quote_block(PAT, "Monday, 3 March 2025 4:15 PM", ALEX, "Fees",
                        "Hi Alex,\n\nCan you send the revised fee for stage 2?\n\nRegards,\nPat")
        return rec(ALEX, "RE: Fees", "Hi Pat,\n\nRevised fee attached: $12,500 excl GST.\n" + q, date=date, to=[PAT])

    def test_unfiled_quoted_email_is_recovered(self):
        res = build([self.unfiled_reply()])
        text = text_of(res)
        self.assertIn("  \u21b3 25-03-03 16:15 EXAMPL.PO: Hi Alex, Can you send the revised fee for stage 2?", text)
        self.assertEqual(res["stats"]["recovered_quoted"], 1)
        self.assertIn("PO=Pat Owner", text)

    def test_filed_quoted_email_is_not_recovered(self):
        original = rec(PAT, "Fees", "Hi Alex,\n\nCan you send the revised fee for stage 2?\n\nRegards,\nPat",
                       date="2025-03-03T16:15:00+10:00", to=[ALEX])
        res = build([original, self.unfiled_reply()])
        self.assertNotIn("\u21b3", text_of(res))
        self.assertEqual(res["stats"]["recovered_quoted"], 0)

    def test_filed_original_matched_across_time_zones(self):
        original = rec(PAT, "Fees", "Hi Alex,\n\nCan you send the revised fee?\n",
                       date="2025-03-03T17:15:00+11:00", to=[ALEX])   # same instant written in Sydney summer time
        res = build([original, self.unfiled_reply()])
        self.assertEqual(res["stats"]["recovered_quoted"], 0)

    def test_recovered_only_once(self):
        res = build([self.unfiled_reply("2025-03-05T09:00:00+10:00"), self.unfiled_reply("2025-03-06T09:00:00+10:00")])
        self.assertEqual(text_of(res).count("\n  \u21b3 "), 1)
        self.assertEqual(res["stats"]["recovered_quoted"], 1)

    def test_not_recovered_when_switched_off(self):
        self.assertNotIn("\u21b3", text_of(build([self.unfiled_reply()], recover_quoted=False)))
        fwd = rec(ALEX, "FW: Fees", "FYI, see below." + quote_block(PAT, "Monday, 3 March 2025 4:15 PM", ALEX, "Fees",
                                                                     "Please send the fee."), to=[SAM])
        self.assertNotIn("\u21b3", text_of(build([fwd], squeeze="max", recover_quoted=False)))

    def test_max_recovers_only_under_thin_emails(self):
        q = quote_block(PAT, "Monday, 3 March 2025 4:15 PM", ALEX, "Fees", "Can you send the revised fee?")
        reply = rec(ALEX, "RE: Fees", "Revised fee attached: $12,500 excl GST, valid until the end of June." + q)
        self.assertNotIn("\u21b3", text_of(build([reply], squeeze="max")))   # has text of its own
        council = ("Dear Applicant, Council requires the following information for DA 123/2025: " +
                   "a revised stormwater plan showing the detention tank and outlet levels. " * 6)
        older = quote_block(SAM, "Friday, 28 February 2025 9:00 AM", PAT, "DA info", "Older email in the chain.")
        fwd = rec(ALEX, "FW: Council information request", "FYI, see below." +
                  quote_block(PAT, "Monday, 3 March 2025 4:15 PM", ALEX, "Council information request", council) + older,
                  to=[SAM])
        res = build([fwd], squeeze="max")
        rlines = [l for l in text_of(res).split("\n") if l.startswith("  \u21b3")]
        self.assertEqual(len(rlines), 1)                   # only the newest quoted email
        self.assertIn("Council requires the following information", rlines[0])
        self.assertLessEqual(len(rlines[0].split(": ", 1)[1]), 252)

    def test_ack_with_unfiled_quote_kept_at_max(self):
        q = quote_block(PAT, "Monday, 3 March 2025 4:15 PM", ALEX, "Fees", "Please send the revised fee for stage 2.")
        r = rec(ALEX, "RE: Fees", "Thanks Pat" + q, date="2025-03-05T09:00:00+10:00", to=[PAT])
        res = build([r], squeeze="max")
        text = text_of(res)
        self.assertIn("09:00 EC.AC>EXAMPL: (ack)\n  \u21b3 25-03-03 16:15 EXAMPL.PO: Please send the revised fee", text)
        self.assertIn('"(ack)" = short thanks/acknowledgement.', text)
        self.assertIn('"\u21b3" = an earlier email', text)
        self.assertEqual(res["stats"]["acks_dropped"], 0)

    def test_unrelated_email_hours_away_does_not_hide_the_quoted_one(self):
        other = rec(PAT, "Site access", "The gate code changes on Monday.", date="2025-03-03T13:01:00+10:00", to=[ALEX])
        q = quote_block(PAT, "Monday, 3 March 2025 10:00 AM", ALEX, "Kerb ramp",
                        "The kerb ramp exceeds 1 in 14, please revise the levels.")
        reply = rec(ALEX, "RE: Kerb ramp", "Revised levels attached." + q, date="2025-03-04T09:00:00+10:00", to=[PAT])
        res = build([other, reply])
        self.assertEqual(res["stats"]["recovered_quoted"], 1)
        self.assertIn("\u21b3 25-03-03 10:00 EXAMPL.PO: The kerb ramp exceeds 1 in 14", text_of(res))

    def test_filed_copy_an_hour_away_with_the_same_text_is_not_recovered(self):
        for body in ("Hi Alex,\n\nCan you send the revised fee for stage 2 by Friday please?", "Fee by Friday?"):
            original = rec(PAT, "Fees", body, date="2025-03-03T11:00:00+10:00", to=[ALEX])
            q = quote_block(PAT, "Monday, 3 March 2025 10:00 AM", ALEX, "Fees", body)
            reply = rec(ALEX, "RE: Fees", "Fee attached." + q, date="2025-03-04T09:00:00+10:00", to=[PAT])
            res = build([original, reply])
            self.assertEqual(res["stats"]["recovered_quoted"], 0, body)

    def test_two_unfiled_emails_two_hours_apart_are_both_recovered(self):
        q1 = quote_block(PAT, "Monday, 3 March 2025 12:00 PM", ALEX, "Fees", "Second question: what about stage 3?")
        q2 = quote_block(PAT, "Monday, 3 March 2025 10:00 AM", ALEX, "Fees", "First question: the fee for stage 2?")
        reply = rec(ALEX, "RE: Fees", "Answers attached." + q1 + q2, date="2025-03-04T09:00:00+10:00", to=[PAT])
        res = build([reply])
        self.assertEqual(res["stats"]["recovered_quoted"], 2)

    def test_deep_chain_recovered_oldest_first(self):
        people = [PAT, SAM, JO, ("Kim Tran", "kim@example-client.com.au"), ("Lee Ward", "lee@example.org"),
                  ("Ray Ng", "ray@example.net")]
        body = "Latest reply on the culvert."
        for n, who in enumerate(people):
            body += quote_block(who, "Monday, %d March 2025 9:%02d AM" % (10 - n, 10 + n), ALEX, "Culvert",
                                "Message number %d about the culvert design." % (6 - n))
        res = build([rec(ALEX, "RE: Culvert", body, date="2025-03-11T09:00:00+10:00")])
        rlines = [l for l in text_of(res).split("\n") if l.startswith("  \u21b3")]
        self.assertEqual(res["stats"]["recovered_quoted"], 6)
        self.assertIn("Message number 1 about", rlines[0])
        self.assertIn("Message number 6 about", rlines[-1])

    def test_recovered_time_follows_the_filed_time_zone(self):
        question = rec(PAT, "Plans", "Could you please ask the client about the parking plan?",
                       date="2024-10-11T10:16:00+11:00", to=[ALEX])
        answer = rec(ALEX, "RE: Plans", "The client has not raised it yet; I will ask them today.",
                     date="2024-10-11T10:21:00+11:00", to=[PAT])
        q1 = quote_block(ALEX, "Friday, 11 October 2024 9:21 AM", PAT, "Plans",
                         "The client has not raised it yet; I will ask them today.")
        q2 = quote_block(ALEX, "Friday, 11 October 2024 9:20 AM", PAT, "Plans",
                         "Draft note that was never filed about the parking plan.")
        thanks = rec(PAT, "RE: Plans", "Thanks Alex" + q1 + q2, date="2024-10-11T10:22:00+11:00", to=[ALEX])
        text = text_of(build([question, answer, thanks]))
        self.assertIn("\u21b3 24-10-11 10:20 EC.AC: Draft note that was never filed", text)

    def test_undated_short_quote_recovered_once(self):
        q = ("\nVon: Kim Tran <kim@example-client.com.au>\nGesendet: Montag, 3. M\u00e4rz 2025 10:00\nAn: Alex Citizen\n"
             "Betreff: Pump\n\nIs the duty pump 30 L/s?\n")
        a = rec(ALEX, "RE: Pump", "Yes, 30 L/s." + q, date="2025-03-04T09:00:00+10:00")
        b = rec(SAM, "RE: Pump", "Agreed with Alex." + q, date="2025-03-04T10:00:00+10:00")
        res = build([a, b])
        self.assertEqual(res["stats"]["recovered_quoted"], 1)
        self.assertEqual(text_of(res).count("Is the duty pump 30 L/s?"), 1)

    def test_recovered_capped(self):
        long_body = "Hi Alex,\n\n" + "This sentence is part of a long quoted email. " * 60
        q = quote_block(PAT, "Monday, 3 March 2025 4:15 PM", ALEX, "Fees", long_body)
        r = rec(ALEX, "RE: Fees", "Noted, will review." + q, to=[PAT])
        line = [l for l in text_of(build([r])).split("\n") if l.startswith("  \u21b3")][0]
        self.assertLessEqual(len(line.split(": ", 1)[1]), 802)
        self.assertTrue(line.endswith("email. \u2026"))

    def test_nested_quotes_listed_oldest_first(self):
        q1 = quote_block(PAT, "Tuesday, 4 March 2025 8:00 AM", ALEX, "Fees", "Second question about fees.")
        q2 = quote_block(SAM, "Monday, 3 March 2025 8:00 AM", PAT, "Fees", "First question about fees.")
        r = rec(ALEX, "RE: Fees", "Answers below." + q1 + q2, date="2025-03-05T09:00:00+10:00")
        lines = [l for l in text_of(build([r])).split("\n") if l.startswith("  \u21b3")]
        self.assertEqual(len(lines), 2)
        self.assertIn("25-03-03 08:00 EB.SB: First question", lines[0])
        self.assertIn("25-03-04 08:00 EXAMPL.PO: Second question", lines[1])

    def test_inline_replies_to_a_filed_email(self):
        original = rec(SAM, "Pour", "Hi Alex,\n\nQuestions:\n1. Bar chairs?\n2. Cover?\n\nRegards,\nSam",
                       date="2025-03-03T16:15:00+10:00", to=[ALEX])
        q = quote_block(SAM, "Monday, 3 March 2025 4:15 PM", ALEX, "Pour",
                        "Hi Alex,\n\nQuestions:\n1. Bar chairs?\nUse 65 mm chairs at 800 centres.\n2. Cover?\n"
                        "Cover is 65 mm to all faces.\n\nRegards,\nSam")
        reply = rec(ALEX, "RE: Pour", "Hi Sam,\n\nSee my comments below in red.\n" + q,
                    date="2025-03-04T09:00:00+10:00", to=[SAM])
        text = text_of(build([original, reply]))
        self.assertIn('See my comments below in red. [inline replies: re "1. Bar chairs\u2026": Use 65 mm chairs at '
                      '800 centres. re "2. Cover\u2026": Cover is 65 mm to all faces.]', text)

    def test_inline_replies_use_the_exact_time_original(self):
        unrelated = rec(SAM, "Induction", "Hi Alex,\n\nInduction is on Tuesday at 7am.",
                        date="2025-03-03T15:15:00+10:00", to=[ALEX])
        original = rec(SAM, "Pour", "Hi Alex,\n\nQuestions:\n1. Bar chairs?\n2. Cover?\n\nRegards,\nSam",
                       date="2025-03-03T16:15:00+10:00", to=[ALEX])
        q = quote_block(SAM, "Monday, 3 March 2025 4:15 PM", ALEX, "Pour",
                        "Hi Alex,\n\nQuestions:\n1. Bar chairs?\nUse 65 mm chairs.\n2. Cover?\nCover is 65 mm.\n\n"
                        "Regards,\nSam")
        reply = rec(ALEX, "RE: Pour", "Hi Sam,\n\nSee my comments below in red.\n" + q,
                    date="2025-03-04T09:00:00+10:00", to=[SAM])
        for order in ([unrelated, original, reply], [original, unrelated, reply]):
            text = text_of(build(order))
            self.assertIn('[inline replies: re "1. Bar chairs\u2026": Use 65 mm chairs. re "2. Cover\u2026": '
                          'Cover is 65 mm.]', text)

    def test_email_index_is_order_independent_and_prefers_closest(self):
        idx = digest.EmailIndex()
        idx.add({"e:a@x.example", "n:leesample"}, datetime(2025, 3, 3, 9, 0), "", "first", "first email text here")
        idx.add({"e:b@y.example", "n:leesample"}, datetime(2025, 3, 3, 10, 0), "", "second", "second email text")
        for keys in (["n:leesample", "e:b@y.example"], ["e:b@y.example", "n:leesample"]):
            self.assertEqual(idx.find(keys, datetime(2025, 3, 3, 10, 0), ""), "second")
        self.assertEqual(idx.find(["n:leesample"], datetime(2025, 3, 3, 9, 1), ""), "first")
        # an hour away matches only when the text opens the same way
        self.assertIsNone(idx.find(["n:leesample"], datetime(2025, 3, 3, 11, 0), "", "another email"))
        self.assertEqual(idx.find(["n:leesample"], datetime(2025, 3, 3, 11, 0), "", "second email text"), "second")
        self.assertEqual(idx.find_offset(["e:b@y.example"], datetime(2025, 3, 3, 9, 0), "second email text"), 1)


class NoiseAndAckTests(unittest.TestCase):
    def records(self):
        return [rec(SAM, "Pour", "Can we pour Friday?", date="2025-03-04T09:00:00+10:00", to=[ALEX]),
                rec(ALEX, "RE: Pour", "Thanks Sam", date="2025-03-04T10:00:00+10:00", to=[SAM]),
                rec(JO, "Accepted: Site meeting", "", date="2025-03-04T11:00:00+10:00"),
                rec(JO, "Automatic reply: Pour", "I am out of the office.", date="2025-03-04T11:05:00+10:00"),
                rec(("Teams", "no-reply@teams.mail.microsoft"), "You have new messages", "Chat",
                    date="2025-03-04T11:10:00+10:00")]

    def test_noise_dropped_and_counted(self):
        res = build(self.records())
        text = text_of(res)
        self.assertEqual(res["stats"]["noise_dropped"], 3)
        self.assertIn("1 meeting response, 1 auto-reply, 1 notification", text)
        self.assertNotIn("out of the office", text)

    def test_meeting_response_with_comment_is_kept(self):
        recs = [rec(JO, "Accepted: Site meeting", "Can we please add Pat to the meeting?",
                    date="2025-03-04T11:00:00+10:00")]
        recs.insert(0, rec(ALEX, "Site meeting", "Meeting on site to walk the pour.",
                           date="2025-03-04T10:00:00+10:00", item_class="IPM.Schedule.Meeting.Request"))
        res = build(recs)
        self.assertEqual(res["stats"]["noise_dropped"], 0)
        self.assertEqual(res["stats"]["threads"], 1)
        self.assertIn("11:00 EC.JP: (accepted) Can we please add Pat to the meeting?", text_of(res))

    def test_meeting_time_and_place_shown(self):
        teams = ("Microsoft Teams meeting\nJoin on your computer or mobile app\n"
                 "Click here to join the meeting\n")
        when = {"start": "2025-03-13T14:00:00+10:00", "end": "2025-03-13T15:30:00+10:00",
                "location": "Site office, Gate 2"}
        recs = [rec(ALEX, "Pour walk", teams, date="2025-03-04T10:00:00+10:00", to=[SAM],
                    item_class="IPM.Schedule.Meeting.Request", meeting=when),
                rec(ALEX, "Design review", "Agenda: slab levels. " * 120, date="2025-03-05T10:00:00+10:00",
                    to=[SAM], item_class="IPM.Schedule.Meeting.Request",
                    meeting={"start": "2025-03-20T09:00:00+10:00", "end": "", "location": ""}),
                rec(ALEX, "Canceled: Pour walk", "", date="2025-03-06T10:00:00+10:00", to=[SAM],
                    item_class="IPM.Schedule.Meeting.Canceled", meeting=when),
                rec(ALEX, "Thanks catch-up", "Thanks", date="2025-03-07T10:00:00+10:00", to=[SAM],
                    item_class="IPM.Schedule.Meeting.Request",
                    meeting={"start": "2025-03-24T00:00:00+10:00", "end": "2025-03-26T00:00:00+10:00",
                             "location": ""}),
                rec(ALEX, "Old style invite", teams, date="2025-03-08T10:00:00+10:00", to=[SAM],
                    item_class="IPM.Schedule.Meeting.Request", meeting=None)]
        text = text_of(build(recs))
        self.assertIn("EC.AC>EB.SB: (meeting 25-03-13 14:00-15:30 @ Site office, Gate 2)\n", text)
        line = [l for l in email_lines(text) if "25-03-20" in l][0]
        self.assertIn("EC.AC>EB.SB: [meeting 25-03-20 09:00] Agenda: slab levels.", line)
        self.assertIn("…", line)          # the text is capped, the meeting label is not
        self.assertIn("(meeting cancelled 25-03-13 14:00-15:30 @ Site office, Gate 2)", text)
        self.assertIn("[meeting 25-03-24 to 25-03-25 all day] Thanks", text)   # never an (ack)
        self.assertIn("EC.AC>EB.SB: (meeting invite)", text)

    def test_noise_kept_when_switched_off(self):
        res = build(self.records(), drop_noise=False)
        self.assertEqual(res["stats"]["noise_dropped"], 0)
        self.assertIn("out of the office", text_of(res))

    def test_ack_handling_per_level(self):
        light = text_of(build(self.records(), squeeze="light"))
        self.assertIn("10:00 EC.AC>EB.SB: Thanks Sam", light)
        std = text_of(build(self.records()))
        self.assertIn("10:00 EC.AC>EB.SB: (ack)", std)
        res = build(self.records(), squeeze="max")
        self.assertNotIn("10:00 EC.AC", text_of(res))
        self.assertEqual(res["stats"]["acks_dropped"], 1)
        self.assertIn("1 thank-you/ack email", text_of(res))

    def test_decisions_are_not_acks(self):
        recs = [rec(SAM, "Pour", "Can we pour Friday?", date="2025-03-04T09:00:00+10:00", to=[ALEX]),
                rec(ALEX, "RE: Pour", "Approved, thanks", date="2025-03-04T10:00:00+10:00", to=[SAM])]
        res = build(recs, squeeze="max")
        self.assertIn("10:00 EC.AC>EB: Approved, thanks", text_of(res))
        self.assertEqual(res["stats"]["acks_dropped"], 0)

    def test_ack_with_a_document_kept_at_max(self):
        recs = [rec(SAM, "Pour", "Can we pour Friday?", date="2025-03-04T09:00:00+10:00", to=[ALEX]),
                rec(ALEX, "RE: Pour", "Thanks Sam.", date="2025-03-04T10:00:00+10:00", to=[SAM],
                    attachments=["C-101 Rev C.pdf"])]
        res = build(recs, squeeze="max")
        self.assertIn("10:00 EC.AC>EB: [att: C-101 Rev C.pdf]", text_of(res))
        self.assertNotIn("(ack)", text_of(res))
        self.assertEqual(res["stats"]["acks_dropped"], 0)
        recs[1]["attachments"] = [{"name": "photo.jpg", "size": 10, "inline": False}]
        self.assertEqual(build(recs, squeeze="max")["stats"]["acks_dropped"], 1)


class KeywordTests(unittest.TestCase):
    def test_thread_kept_if_any_email_matches(self):
        recs = [rec(SAM, "Pour 3", "When can we pour?", date="2025-03-04T09:00:00+10:00"),
                rec(ALEX, "RE: Pour 3", "After the NCR is closed.", date="2025-03-04T10:00:00+10:00"),
                rec(SAM, "Invoice", "March invoice attached.", date="2025-03-05T09:00:00+10:00"),
                rec(SAM, "Photos", "See photos.", date="2025-03-06T09:00:00+10:00",
                    attachments=["NCR-007 photos.zip"])]
        res = build(recs, focus_keywords="ncr")
        text = text_of(res)
        self.assertIn("When can we pour?", text)        # whole thread kept
        self.assertIn("See photos.", text)              # matched on attachment name
        self.assertNotIn("March invoice", text)
        self.assertEqual(res["stats"]["filtered_out"], 1)
        self.assertIn("Focus keywords: ncr", text)

    def test_signature_and_disclaimer_words_in_quotes_do_not_match(self):
        q = quote_block(SAM, "Monday, 3 March 2025 9:00 AM", ALEX, "Pour",
                        "Hi Alex,\n\nThe culvert pour is booked.\n\nRegards,\nSam Builder\nProject Manager\n"
                        "Bypass Alliance\nM 0400 000 000\nThis email is confidential and intended only for the "
                        "addressee.")
        recs = [rec(ALEX, "RE: Pour", "Noted, I will be on site." + q, date="2025-03-04T09:00:00+10:00")]
        self.assertEqual(build(recs, focus_keywords="bypass")["stats"]["filtered_out"], 1)
        self.assertEqual(build(recs, focus_keywords="addressee")["stats"]["filtered_out"], 1)
        self.assertEqual(build(recs, focus_keywords="culvert")["stats"]["filtered_out"], 0)

    def test_curly_punctuation_in_keywords(self):
        recs = [rec(ALEX, "Drainage", "St John's Park culvert - stage 2 is ready.")]
        self.assertEqual(build(recs, focus_keywords="st john\u2019s")["stats"]["filtered_out"], 0)
        self.assertEqual(build(recs, focus_keywords="culvert \u2013 stage")["stats"]["filtered_out"], 0)
        recs = [rec(ALEX, "Drainage", "See attached.", attachments=["image001.png", "Plan.pdf"])]
        self.assertEqual(build(recs, focus_keywords="image001")["stats"]["filtered_out"], 1)


class ThreadSplitTests(unittest.TestCase):
    def test_generic_subject_split_into_conversations(self):
        recs = [rec(SAM, "Attached Image", "Scan one.", date="2025-03-01T09:00:00+10:00", attachments=["a.pdf"]),
                rec(SAM, "Attached Image", "Scan two.", date="2025-05-01T09:00:00+10:00", attachments=["a.pdf"]),
                rec(ALEX, "FW: Attached Image", "Scan three.", date="2025-09-01T09:00:00+10:00")]
        res = build(recs)
        heads = [l for l in text_of(res).split("\n") if l.startswith("## ")]
        self.assertEqual(heads, ["## Attached Image", "## Attached Image", "## Attached Image"])
        self.assertEqual(res["stats"]["threads"], 3)
        self.assertEqual(text_of(res).count("[att: a.pdf]"), 2)     # listed in both conversations

    def test_replies_and_close_emails_stay_together(self):
        recs = [rec(SAM, "Pour", "Question.", date="2025-03-01T09:00:00+10:00"),
                rec(ALEX, "RE: Pour", "Late answer.", date="2025-04-10T09:00:00+10:00"),
                rec(SAM, "Pour", "Follow-up ten days later.", date="2025-04-20T09:00:00+10:00")]
        res = build(recs)
        self.assertEqual(res["stats"]["threads"], 1)
        self.assertIn("## Pour (3 emails, 25-03-01 to 25-04-20)", text_of(res))


class CapTests(unittest.TestCase):
    def test_standard_and_max_caps_cut_at_sentences(self):
        body = " ".join("Sentence number %d explains the slab design in some detail." % i for i in range(60))
        r = rec(SAM, "Long", body)
        for level, cap in (("standard", 1500), ("max", 500)):
            line = email_lines(text_of(build([r], squeeze=level)))[0]
            content = line.split(": ", 1)[1]
            self.assertLessEqual(len(content), cap + 2, level)
            self.assertTrue(content.endswith("detail. \u2026"), level)
        light = email_lines(text_of(build([r], squeeze="light")))[0]
        self.assertTrue(light.endswith("detail."))
        self.assertNotIn("\u2026", light)

    def test_long_fee_email_keeps_its_total(self):
        rows = " ".join("\nStage %d design\n%d\n$%d,500.00" % (i, i * 10, i) for i in range(1, 40))
        body = "Hi Pat,\n\nPlease find the fee breakdown below:\n" + rows + "\nTotal\n7800\n$780,500.00\n"
        line = email_lines(text_of(build([rec(SAM, "Fee", body)])))[0]
        self.assertIn("Total \u2022 7800 \u2022 $780,500.00", line)
        self.assertLessEqual(len(line.split(": ", 1)[1]), 1502)


class SplitTests(unittest.TestCase):
    def many_threads(self, n=30, size=3000):
        recs = []
        for i in range(n):
            body = "Hi. " + ("Thread %d content sentence goes here. " % i) * (size // 40)
            recs.append(rec(SAM if i % 2 else ALEX, "Topic %02d" % i, body, date="2025-03-%02dT09:00:00+10:00" % (i % 28 + 1),
                            to=[ALEX if i % 2 else SAM]))
        return recs

    def test_parts_respect_limit_and_never_split_threads(self):
        res = digest.build_digest(self.many_threads(), project(part_size="small", squeeze="light"), now=NOW)
        # 30 threads of ~3 KB fit in one 200k part, so use a smaller limit via a custom table entry
        self.assertEqual(len(res["parts"]), 1)
        old = digest.PART_SIZES["small"]["chars"]
        digest.PART_SIZES["small"]["chars"] = 20000
        try:
            res = digest.build_digest(self.many_threads(), project(part_size="small", squeeze="light"), now=NOW)
        finally:
            digest.PART_SIZES["small"]["chars"] = old
        parts = res["parts"]
        self.assertGreater(len(parts), 3)
        n = len(parts)
        seen_titles = []
        for i, p in enumerate(parts, 1):
            self.assertLessEqual(len(p["text"]), 20000)
            self.assertTrue(p["text"].startswith("SQUISH EMAIL DIGEST | Test Job | part %d of %d\n" % (i, n)))
            span = p["first_date"] if p["first_date"] == p["last_date"] else \
                "%s to %s" % (p["first_date"], p["last_date"])
            self.assertIn("(this part: %d email%s, %d thread%s, %s)" % (
                p["emails"], "" if p["emails"] == 1 else "s", p["threads"], "" if p["threads"] == 1 else "s",
                span), p["text"].split("\n")[1])
            self.assertIn("People (ORG.Initials):", p["text"])
            self.assertNotIn("(continued)", p["text"])
            seen_titles += re.findall(r"^## (Topic \d+)", p["text"], re.M)
        self.assertEqual(len(seen_titles), 30)
        self.assertEqual(len(set(seen_titles)), 30)
        self.assertEqual(sum(p["emails"] for p in parts), 30)

    def test_per_part_legend(self):
        recs = [rec(ALEX, "Topic A", "x " * 6000, date="2025-03-01T09:00:00+10:00"),
                rec(SAM, "Topic B", "y " * 6000, date="2025-03-02T09:00:00+10:00")]
        old = digest.PART_SIZES["small"]["chars"]
        digest.PART_SIZES["small"]["chars"] = 15000
        try:
            res = digest.build_digest(recs, project(part_size="small", squeeze="light"), now=NOW)
        finally:
            digest.PART_SIZES["small"]["chars"] = old
        self.assertEqual(len(res["parts"]), 2)
        a, b = res["parts"][0]["text"], res["parts"][1]["text"]
        self.assertIn("AC=Alex Citizen", a)
        self.assertNotIn("Sam Builder", a)
        self.assertIn("SB=Sam Builder", b)
        self.assertNotIn("Alex Citizen", b)

    def test_oversize_thread_continues(self):
        recs = [rec(SAM if i % 2 else ALEX, "RE: Big thread" if i else "Big thread",
                    ("Email %d sentence about the retaining wall. " % i) * 50,
                    date="2025-03-%02dT09:00:00+10:00" % (i + 1)) for i in range(20)]
        old = digest.PART_SIZES["small"]["chars"]
        digest.PART_SIZES["small"]["chars"] = 12000
        try:
            res = digest.build_digest(recs, project(part_size="small", squeeze="light"), now=NOW)
        finally:
            digest.PART_SIZES["small"]["chars"] = old
        parts = res["parts"]
        self.assertGreater(len(parts), 1)
        self.assertIn("## Big thread (20 emails, 25-03-01 to 25-03-20)", parts[0]["text"])
        for p in parts[1:]:
            self.assertIn("## Big thread (continued)", p["text"])
            first = email_lines(p["text"])[0]
            self.assertRegex(first, r"^25-03-\d\d 09:00 ")    # full date again after the split
        for p in parts:
            self.assertLessEqual(len(p["text"]), 12000)
        self.assertEqual(sum(len(email_lines(p["text"])) for p in parts), 20)
        for p in parts:     # the thread counts in every part it appears in, never "0 threads"
            self.assertEqual(p["threads"], 1)
            self.assertIn("1 thread, ", p["text"].split("\n")[1])
        self.assertEqual(res["stats"]["threads"], 1)


SAFE = "https://eur01.safelinks.protection.outlook.com/?url=https%3A%2F%2Fexample-builders.com.au&data=05%7C{}%7C"


def builder_signature(recipient):
    """Sam's signature; its links encode the recipient, as SafeLinks do."""
    return ("\nRegards,\nSam Builder\nWeb: www.example-builders.com.au <%s>\n"
            "Click HERE <%s> to Subscribe to our Newsletter\n" % (SAFE.format(recipient), SAFE.format(recipient + "x")))


class TwoMinuteMatchTests(unittest.TestCase):
    """A quoted email matches a filed one a minute or two away only when their texts
    do not plainly differ, and never matches the email that contains it."""

    def test_second_email_a_minute_later_is_recovered(self):
        first = rec(ALEX, "Pump station", "Hi Sam,\n\nPlease find attached the revised pump station drawings.\n\n"
                    "Regards\nAlex", date="2025-03-03T10:00:00+10:00", to=[SAM])
        q = quote_block(ALEX, "Monday, 3 March 2025 10:01 AM", SAM, "Retaining wall",
                        "Hi Sam,\n\nCan you please hold the retaining wall pour until the geotech inspection on "
                        "Thursday?\n\nRegards\nAlex")
        reply = rec(SAM, "RE: Retaining wall", "Hi Alex,\n\nYes, we will hold the pour until Friday.\n" + q,
                    date="2025-03-03T11:30:00+10:00", to=[ALEX])
        res = build([first, reply])
        self.assertEqual(res["stats"]["recovered_quoted"], 1)
        self.assertIn("\u21b3 25-03-03 10:01 EC.AC: Hi Sam, Can you please hold the retaining wall pour", text_of(res))

    def test_own_email_forwarded_a_minute_later_is_recovered(self):
        body = ("Hi Sam,\n\nHold point 4 (reinforcement inspection) must be released by us before any concrete "
                "is placed.\n\nRegards\nAlex")
        for fwd_text in ("FYI", ""):
            fwd = rec(ALEX, "FW: Hold points", fwd_text + quote_block(ALEX, "Wednesday, 5 March 2025 9:00 AM", SAM,
                                                                       "Hold points", body),
                      date="2025-03-05T09:01:00+10:00", to=[JO])
            for level in ("standard", "max"):
                res = build([fwd], squeeze=level)
                self.assertIn("\u21b3 25-03-05 09:00 EC.AC: Hi Sam, Hold point 4", text_of(res), (fwd_text, level))

    def test_same_text_a_minute_away_still_counts_as_filed(self):
        body = "Hi Alex,\n\nThe pump duty point is 30 L/s at 12 m head, please confirm by Friday.\n\nRegards,\nSam"
        original = rec(SAM, "Pump", body, date="2025-03-03T10:00:00+10:00", to=[ALEX])
        reply = rec(ALEX, "RE: Pump", "Confirmed." + quote_block(SAM, "Monday, 3 March 2025 10:01 AM", ALEX, "Pump",
                                                                 body), date="2025-03-03T11:00:00+10:00", to=[SAM])
        self.assertEqual(build([original, reply])["stats"]["recovered_quoted"], 0)

    def test_email_index_never_returns_the_excluded_email(self):
        idx = digest.EmailIndex()
        mine, other = {"id": 1}, {"id": 2}
        idx.add({"n:alexcitizen"}, datetime(2025, 3, 3, 9, 1), "a" * 60, mine, "fyi")
        self.assertIsNone(idx.find({"n:alexcitizen"}, datetime(2025, 3, 3, 9, 0), "a" * 60, "", exclude=mine))
        idx.add({"n:alexcitizen"}, datetime(2025, 3, 3, 9, 2), "", other, "hi sam the crane pad needs roadbase")
        self.assertIsNone(idx.find({"n:alexcitizen"}, datetime(2025, 3, 3, 9, 0), "",
                                   "hi jo can you book the surveyor for monday"))
        self.assertIs(idx.find({"n:alexcitizen"}, datetime(2025, 3, 3, 9, 0), "",
                               "hi sam the crane pad needs roadbase and geofabric"), other)
        self.assertIs(idx.find({"n:alexcitizen"}, datetime(2025, 3, 3, 9, 0), "", exclude=mine), other)  # time only


class InlineReplyTests(unittest.TestCase):
    def original(self):
        return rec(ALEX, "Slab", "Hi Sam,\n\nItems:\n1. Is the vapour barrier in?\n2. Is cover 65 mm?\n"
                   "3. When is the pour?\n\nRegards\nAlex", date="2025-03-03T09:00:00+10:00", to=[SAM])

    def test_each_partys_answers_stay_with_them(self):
        red = ("Hi Sam,\n\nItems:\n1. Is the vapour barrier in?\nYes, a double layer is down.\n2. Is cover 65 mm?\n"
               "Cover is 50 mm at the edges.\n3. When is the pour?\n\nRegards\nAlex")
        b = rec(SAM, "RE: Slab", "Hi Alex,\n\nSee comments below in red.\n" +
                quote_block(ALEX, "Monday, 3 March 2025 9:00 AM", SAM, "Slab", red),
                date="2025-03-04T09:00:00+10:00", to=[ALEX])
        blue = red.replace("at the edges.\n", "at the edges.\nNot acceptable: cover must be 65 mm, add bar chairs.\n")
        c = rec(ALEX, "RE: Slab", "Hi Sam,\n\nMy responses are in blue below.\n" +
                quote_block(ALEX, "Monday, 3 March 2025 9:00 AM", SAM, "Slab", blue),
                date="2025-03-05T09:00:00+10:00", to=[SAM])
        for order in ([self.original(), b, c], [c, b, self.original()]):
            lines = email_lines(text_of(build(order)))
            # each answer is labelled with the point it answers
            self.assertIn('[inline replies: re "1. Is the vapour barrier in\u2026": Yes, a double layer is down. '
                          're "2. Is cover 65 mm\u2026": Cover is 50 mm at the edges.]', lines[1])
            self.assertIn('[inline replies: re "Cover is 50 mm at the\u2026": Not acceptable: cover must be 65 mm, '
                          'add bar chairs.]', lines[2])
            self.assertNotIn("double layer", lines[2])

    def test_answers_in_a_deeper_quote_and_changing_links(self):
        e1 = rec(SAM, "Pit lids", "Hi Alex,\n\n1. Are the pit lids class D?\n"
                 "2. Can the grates be galvanised?\n" + builder_signature("alex"),
                 date="2025-03-03T09:00:00+10:00", to=[ALEX])
        e2 = rec(ALEX, "RE: Pit lids", "Hi Pat,\n\nCan you answer Sam's questions below?\n\nRegards,\nAlex",
                 date="2025-03-03T10:00:00+10:00", to=[PAT])
        answered = ("Hi Alex,\n\n1. Are the pit lids class D?\nYes, class D lids throughout.\n"
                    "2. Can the grates be galvanised?\nGalvanised is fine by me.\n" + builder_signature("pat"))
        e3 = rec(PAT, "RE: Pit lids", "Hi Alex,\n\nI've added a couple of comments below in blue.\n" +
                 quote_block(ALEX, "Monday, 3 March 2025 10:00 AM", PAT, "RE: Pit lids",
                             "Hi Pat,\n\nCan you answer Sam's questions below?\n\nRegards,\nAlex") +
                 quote_block(SAM, "Monday, 3 March 2025 9:00 AM", ALEX, "Pit lids", answered),
                 date="2025-03-04T09:00:00+10:00", to=[ALEX])
        line = [l for l in email_lines(text_of(build([e1, e2, e3]))) if "comments below in blue" in l][0]
        self.assertIn('[inline replies: re "1. Are the pit lids class\u2026": Yes, class D lids throughout. '
                      're "2. Can the grates be galvanised\u2026": Galvanised is fine by me.]', line)
        for junk in ("Web:", "Click HERE", "Newsletter", "Sam Builder"):
            self.assertNotIn(junk, line)

    def test_answer_typed_on_the_question_line(self):
        q = quote_block(ALEX, "Monday, 3 March 2025 9:00 AM", SAM, "Slab",
                        "Hi Sam,\n\nItems:\n1. Is the vapour barrier in?\n2. Is cover 65 mm? No, 50 mm at the edges.\n"
                        "3. When is the pour?\n\nRegards\nAlex")
        b = rec(SAM, "RE: Slab", "Hi Alex,\n\nAnswers inline.\n" + q, date="2025-03-04T09:00:00+10:00", to=[ALEX])
        self.assertIn('[inline replies: re "2. Is cover 65 mm\u2026": No, 50 mm at the edges.]',
                      text_of(build([self.original(), b])))

    def test_someone_elses_answers_are_not_a_hint(self):
        self.assertTrue(digest._has_inline_hint("Hi Sam,\nSee my comments below in red."))
        self.assertTrue(digest._has_inline_hint("Responses inline."))
        self.assertFalse(digest._has_inline_hint("Thanks, I will also use your response below in the report."))

    def test_cap_keeps_the_label_and_the_closing_bracket(self):
        long_q = "".join("%d. Question number %d about the slab detail?\n" % (i, i) for i in range(1, 40))
        original = rec(ALEX, "Slab", "Hi Sam,\n\n" + long_q + "\nRegards\nAlex", date="2025-03-03T09:00:00+10:00")
        answered = "".join("%d. Question number %d about the slab detail?\nAnswer %d: the detail is fine as drawn.\n"
                           % (i, i, i) for i in range(1, 40))
        own = "Hi Alex,\n\nSee my comments below in red. " + "We have also reviewed the pour sequence. " * 40
        b = rec(SAM, "RE: Slab", own + "\n" + quote_block(ALEX, "Monday, 3 March 2025 9:00 AM", SAM, "Slab",
                                                          "Hi Sam,\n\n" + answered + "\nRegards\nAlex"),
                date="2025-03-04T09:00:00+10:00", to=[ALEX])
        line = email_lines(text_of(build([original, b])))[1]
        content = line.split(": ", 1)[1]
        self.assertIn(' [inline replies: re "1. Question number 1 about the\u2026": Answer 1: the detail is fine '
                      'as drawn.', content)
        self.assertTrue(content.endswith("]"), content[-40:])
        self.assertLessEqual(len(content), 1500 + 4)


class TeamsChatTests(unittest.TestCase):
    def chat(self, name, first, message, date="2025-03-04T11:10:00+10:00"):
        body = ("%s\n\nHi,\nYour teammates are trying to reach you in Microsoft Teams <https://teams.example/x> .\n"
                "\n\t%s sent a message in chat <https://teams.example/c>\n%s\n\n\t<https://teams.example/i>\n"
                "Reply in Teams <https://teams.example/r>\n\nInstall Microsoft Teams now\n"
                "This email was sent from an unmonitored mailbox.\n" % (message, first, message))
        return rec((name + " in Teams", "noreply@emeaemail.teams.microsoft.com"), "%s sent a message" % first, body,
                   date=date, to=[ALEX])

    def test_chat_message_shown_from_the_colleague(self):
        recs = [rec(JO, "Survey", "The survey is booked for Monday.", date="2025-03-03T09:00:00+10:00", to=[ALEX]),
                self.chat("Jo Planner", "Jo", "Can you check the pour sequence before 3pm?")]
        res = build(recs)
        text = text_of(res)
        self.assertIn("11:10 EC.JP>EC.AC: Can you check the pour sequence before 3pm?", text)
        for junk in ("Reply in Teams", "Install Microsoft Teams", "unmonitored", "teams.microsoft", "TEAMS"):
            self.assertNotIn(junk, text)
        self.assertEqual(res["stats"]["noise_dropped"], 0)

    def test_group_chat_first_name_resolved(self):
        recs = [rec(JO, "Survey", "The survey is booked for Monday.", date="2025-03-03T09:00:00+10:00", to=[ALEX]),
                self.chat("Teams", "Jo + 1", "Drawings go out this afternoon.")]
        recs[1]["sender_name"] = "You have new messages in Teams"
        recs[1]["subject"] = "Jo and Sam sent 2 messages to your chat"
        self.assertIn("EC.JP>EC.AC: Drawings go out this afternoon.", text_of(build(recs)))

    def test_other_teams_notifications_still_dropped(self):
        res = build([rec(("Teams", "no-reply@teams.mail.microsoft"), "You have new messages", "Chat",
                         date="2025-03-04T11:10:00+10:00")])
        self.assertEqual(res["stats"]["noise_dropped"], 1)


class TimeZoneTests(unittest.TestCase):
    """A quoted header's time is written in the time zone of whoever quoted it."""

    HDR = "\n\nFrom: %s <%s>\nSent: %s\nTo: Sam Builder <sam@example-builders.com.au>\nSubject: RE: Slab level\n\n"
    DANA = ("Dana Fox", "dana@example-builders.com.au")
    BEN = ("Ben Ng", "ben@example-consulting.com")
    AMY = ("Amy Lee", "amy@example-consulting.com")

    def chain(self, when):
        dana = rec(self.DANA, "Slab level", "Can you confirm the slab level for pour 3 by Friday please?",
                   date="2026-01-12T09:00:00+11:00", to=[SAM])
        amy = rec(self.AMY, "RE: Slab level", "Thanks Ben, noted." +
                  self.HDR % (self.BEN + ("Monday, 12 January 2026 11:10 AM",)) +
                  "Slab level for pour 3 is confirmed as RL 12.50 per the latest survey." +
                  self.HDR % (self.DANA + ("Monday, 12 January 2026 8:00 AM",)) +
                  "Can you confirm the slab level for pour 3 by Friday please?", date=when, to=[SAM])
        return [dana, amy]

    def test_offset_of_a_deeper_level_is_not_applied_to_a_newer_one(self):
        text = text_of(build(self.chain("2026-01-12T11:30:00+11:00")))
        self.assertIn("\u21b3 26-01-12 11:10 EC.BN: Slab level for pour 3 is confirmed", text)
        self.assertNotIn("12:10", text)

    def test_each_writers_own_offset_is_used(self):
        carl = ("Carl Ray", "carl@example-builders.com.au")
        recs = self.chain("2026-01-12T18:00:00+11:00") + [
            rec(carl, "Pour 4", "Pour 4 is booked for Thursday at 6am.", date="2026-01-13T10:00:00+11:00", to=[SAM]),
            rec(self.AMY, "RE: Pour 4", "Great, thanks Carl." + self.HDR % (carl + ("Tuesday, 13 January 2026 10:00 AM",))
                + "Pour 4 is booked for Thursday at 6am.", date="2026-01-13T10:30:00+11:00", to=[SAM])]
        text = text_of(build(recs))
        self.assertIn("\u21b3 26-01-12 11:10 EC.BN: Slab level for pour 3 is confirmed", text)
        self.assertNotIn("12:10", text)

    def test_two_different_quoted_emails_two_minutes_apart_both_recovered(self):
        a = rec(ALEX, "RE: Pit", "Noted." + quote_block(PAT, "Monday, 3 March 2025 10:00 AM", ALEX, "Pit",
                                                        "Can you send the pit schedule for stage 2?"),
                date="2025-03-04T09:00:00+10:00", to=[PAT])
        b = rec(ALEX, "RE: Hire", "Noted." + quote_block(PAT, "Monday, 3 March 2025 10:02 AM", ALEX, "Hire",
                                                        "What is the daily hire rate for the excavator?"),
                date="2025-03-04T10:00:00+10:00", to=[PAT])
        res = build([a, b])
        self.assertEqual(res["stats"]["recovered_quoted"], 2)


class ConversationGapTests(unittest.TestCase):
    def first(self):
        return rec(SAM, "Riverside", "Hi Alex,\n\nAgenda for the culvert design meeting attached.",
                   date="2025-01-10T09:00:00+10:00", to=[ALEX])

    def test_late_reply_to_something_else_starts_a_conversation(self):
        q = quote_block(PAT, "Monday, 24 February 2025 9:00 AM", ALEX, "Riverside",
                        "Hi Alex,\n\nThe pavement test results are attached.")
        late = rec(ALEX, "RE: Riverside", "Thanks Pat, we will review the pavement results." + q,
                   date="2025-03-11T09:00:00+10:00", to=[PAT])
        res = build([self.first(), late])
        heads = [l for l in text_of(res).split("\n") if l.startswith("## ")]
        self.assertEqual(heads, ["## Riverside", "## Riverside (1 email + 1 recovered, 25-02-24 to 25-03-11)"])
        self.assertEqual(res["stats"]["threads"], 2)

    def test_late_reply_to_the_conversation_stays(self):
        q = quote_block(SAM, "Friday, 10 January 2025 9:00 AM", ALEX, "Riverside",
                        "Hi Alex,\n\nAgenda for the culvert design meeting attached.")
        late = rec(ALEX, "RE: Riverside", "Sorry for the slow reply, the agenda is fine." + q,
                   date="2025-03-11T09:00:00+10:00", to=[SAM])
        self.assertEqual(build([self.first(), late])["stats"]["threads"], 1)

    def test_late_reply_without_a_quote_stays(self):
        late = rec(ALEX, "RE: Riverside", "Following up on this.", date="2025-03-11T09:00:00+10:00", to=[SAM])
        self.assertEqual(build([self.first(), late])["stats"]["threads"], 1)


class NearDuplicateTests(unittest.TestCase):
    def pair(self, to_b=None, later="2026-03-02T11:32:05+11:00", undated=False):
        sent = rec(("Alex Citizen", ""), "Levels", "FYI, the revised levels are attached.", attachments=["Levels.pdf"],
                   date="" if undated else "2026-03-02T11:31:55+11:00", to=[SAM])
        got = rec(ALEX, "Levels", "FYI, the revised levels are attached.", attachments=["Levels.pdf"],
                  date="" if undated else later, to=[to_b or SAM])
        return [sent, got]

    def test_sent_and_received_copies_across_a_minute_boundary_merge(self):
        res = build(self.pair())
        self.assertEqual((res["stats"]["emails_used"], res["stats"]["duplicates"]), (1, 1))
        self.assertIn("26-03-02 11:32 EC.AC>EB.SB: FYI, the revised levels are attached.", text_of(res))

    def test_different_recipients_or_minutes_apart_stay_separate(self):
        self.assertEqual(build(self.pair(to_b=PAT))["stats"]["emails_used"], 2)
        self.assertEqual(build(self.pair(later="2026-03-02T11:35:00+11:00"))["stats"]["emails_used"], 2)

    def test_undated_copies_still_merge(self):
        self.assertEqual(build(self.pair(undated=True))["stats"]["duplicates"], 1)


class MessageIdDedupTests(unittest.TestCase):
    OPENING = ("Hi Pat,\n\nThank you for the opportunity to submit our fee proposal for the culvert design. "
               "We have reviewed the brief and the survey and are confident we can meet the programme. ")

    def test_same_minute_resend_with_different_message_id_is_kept(self):
        a = rec(ALEX, "Fee proposal", self.OPENING + "Our lump sum fee is $18,500 excl GST.",
                date="2025-03-04T09:00:05+10:00", message_id="<a1@example>", to=[PAT])
        b = rec(ALEX, "Fee proposal", self.OPENING + "Our lump sum fee is $18,500 excl GST. "
                "Correction: the fee is $21,850 excl GST.", date="2025-03-04T09:00:55+10:00",
                message_id="<a2@example>", to=[PAT])
        res = build([a, b])
        self.assertEqual((res["stats"]["duplicates"], res["stats"]["emails_used"]), (0, 2))
        self.assertIn("Correction: the fee is $21,850 excl GST.", text_of(res))

    def test_different_meeting_requests_same_minute_are_kept(self):
        for ids in (("<b1@example>", "<b2@example>"), ("", "")):
            recs = [rec(ALEX, "Site walk", "", date="2025-03-04T09:00:10+10:00", message_id=ids[0], to=[SAM],
                        item_class="IPM.Schedule.Meeting.Request",
                        meeting={"start": "2025-04-08T10:00:00+10:00", "end": "2025-04-08T11:00:00+10:00",
                                 "location": "Gate 2"}),
                    rec(ALEX, "Site walk", "", date="2025-03-04T09:00:50+10:00", message_id=ids[1], to=[SAM],
                        item_class="IPM.Schedule.Meeting.Request",
                        meeting={"start": "2025-04-09T14:00:00+10:00", "end": "2025-04-09T15:00:00+10:00",
                                 "location": "Gate 5"})]
            res = build(recs)
            text = text_of(res)
            self.assertEqual(res["stats"]["duplicates"], 0, ids)
            self.assertIn("(meeting 25-04-08 10:00-11:00 @ Gate 2)", text)
            self.assertIn("(meeting 25-04-09 14:00-15:00 @ Gate 5)", text)

    def test_copy_without_message_id_still_merges(self):
        a = rec(ALEX, "Fee", "The fee is attached.", date="2025-03-04T09:00:05+10:00", message_id="<c1@example>")
        b = rec(ALEX, "Fee", "The fee is attached.", date="2025-03-04T09:00:25+10:00", message_id="")
        self.assertEqual(build([a, b])["stats"]["duplicates"], 1)


class AliasAndOrgTests(unittest.TestCase):
    def test_one_word_names_get_short_aliases(self):
        names = [next(digest._alias_candidates("Reception")), list(itertools.islice(
            digest._alias_candidates("Records"), 2))]
        self.assertEqual(names, ["Re", ["Re", "Rec"]])
        recs = [rec(("Reception", "reception@example-consulting.com"), "A", "One."),
                rec(("Reception", "reception@example-consulting.com"), "B", "Two."),
                rec(("Records", "records@example-consulting.com"), "C", "Three."),
                rec(("Rex Evans", "rex@example-consulting.com"), "D", "Four.")]
        text = text_of(build(recs))
        self.assertIn("EC.Re: One.", text)
        self.assertIn("EC.Rec: Three.", text)
        # 'RE' would differ from 'Re' only in letter case: Rex Evans gets the next letter
        self.assertIn("EC.REv: Four.", text)

    def test_decomposed_accents_give_the_right_initials(self):
        self.assertEqual(next(digest._alias_candidates("Jose\u0301 Nu\u0301n\u0303ez")), "JN")
        text = text_of(build([rec(("Rene\u0301e Le\u0301vesque", "renee@example-consulting.com"), "A",
                                  "Levels attached.")]))
        self.assertIn("EC.RL: Levels attached.", text)
        self.assertIn("RL=Ren\u00e9e L\u00e9vesque", text)

    def test_clashing_org_codes_get_more_letters(self):
        recs = [rec(("Sam Bell", "sam.bell@cityofnorthvale-example.nsw.gov.au"), "Kerb", "Kerb ramp design attached.",
                    to=[("Kim Ward", "kim@cityofnorthvale-example.nsw.gov.au")]),
                rec(("Lee Wu", "lee@cityofsouthvale-example.nsw.gov.au"), "Footpath", "Footpath levels attached."),
                rec(("Pat Ng", "pat@zorbexwater.com.au"), "Main", "Water main location."),
                rec(("Ray Lim", "ray@zorbex.edu.au"), "Lab", "Lab results attached."),
                rec(("Kai Roe", "kai@quokka.com.au"), "Other", "Another firm."),
                rec(("Ann Poe", "ann@zorbexwater.org"), "Main 2", "Same water authority.")]
        text = text_of(build(recs, org_codes=ORGS + "\nquokka-consulting.example=QUOKKA"))
        self.assertIn("CITYOFN.SB>CITYOFN.KW: Kerb ramp", text)
        self.assertIn("CITYOFS.LW: Footpath", text)
        self.assertIn("ZORBEXW.PN: Water main", text)
        self.assertIn("ZORBEX.RL: Lab results", text)
        self.assertIn("QUOKKA2.KR: Another firm.", text)      # QUOKKA is a configured code
        self.assertIn("ZORBEXW.AP: Same water authority.", text)
        legend = [l for l in text.split("\n") if l.startswith("  ") and " = " in l]
        self.assertEqual(len(set(l.split(" = ")[0] for l in legend)), len(legend))
        self.assertEqual(digest.unique_org_codes({"ec"}, {"EC"}), {"ec": "EC2"})

    def test_same_name_at_two_companies_stays_two_people(self):
        recs = [rec(("Alex Morgan", "alex@acmepumps-example.com.au"), "Pump", "Duty point is 12 L/s."),
                rec(("Alex Morgan", "alex@acmepublishing-example.com"), "Book", "The book is printed.")]
        text = text_of(build(recs))
        self.assertIn("ACMEPUM.AM: Duty point is 12 L/s.", text)
        self.assertIn("ACMEPUB.AM: The book is printed.", text)

    def test_name_only_shared_mailbox_is_not_matched_to_an_outside_org(self):
        outside = rec(SAM, "Invoice", "Invoice attached.", to=[("Accounts", "accounts@example-client.com.au")])
        reminder = rec(("Accounts", ""), "Timesheets", "Timesheets are due today.", date="2025-03-05T09:00:00+10:00")
        self.assertIn("?.Ac: Timesheets are due today.", text_of(build([outside, reminder])))
        home = rec(JO, "Invoice", "Invoice approved.", to=[("Accounts", "accounts@example-consulting.com")])
        self.assertIn("EC.Ac: Timesheets are due today.", text_of(build([outside, home, reminder])))

    def test_name_only_sender_prefers_the_home_org(self):
        recs = [rec(SAM, "A", "Plans attached.", to=[("Alex Citizen", "alex.citizen@other-example.com")]),
                rec(SAM, "B", "More plans.", to=[("Alex Citizen", "alex.citizen@other-example.com")]),
                rec(PAT, "C", "Survey attached.", to=[ALEX]),
                rec(("Alex Citizen", ""), "D", "Filed with no SMTP address.", date="2025-03-05T09:00:00+10:00")]
        self.assertIn("EC.AC: Filed with no SMTP address.", text_of(build(recs)))


class FocusMatchTests(unittest.TestCase):
    def kept(self, recs, words):
        return build(recs, focus_keywords=words)["stats"]["filtered_out"] == 0

    def test_filing_and_external_tags_do_not_match(self):
        recs = [rec(SAM, "[EXTERNAL] RE: Car park lighting [Filed 24 Nov 2025 10:50]", "Lux levels attached.")]
        for word in ("external", "filed", "nov 2025"):
            self.assertFalse(self.kept(recs, word), word)
        self.assertTrue(self.kept(recs, "car park"))

    def test_accents_and_spaces_are_folded(self):
        recs = [rec(SAM, "Caf\u00e9 fit-out", "The retaining wall is next to the cafe.")]
        self.assertTrue(self.kept(recs, "cafe"))
        self.assertTrue(self.kept(recs, "caf\u00e9"))
        self.assertTrue(self.kept(recs, "retaining  wall"))

    def test_keywords_match_from_the_start_of_a_word(self):
        self.assertFalse(self.kept([rec(SAM, "RFI 125 - lighting", "See response.")], "RFI 12"))
        self.assertTrue(self.kept([rec(SAM, "RFI 12, lighting", "See response.")], "RFI 12"))
        self.assertTrue(self.kept([rec(SAM, "RFI 12a", "See response.")], "RFI 12"))
        self.assertFalse(self.kept([rec(SAM, "Hospital car park", "Capital works.")], "pit"))
        self.assertTrue(self.kept([rec(SAM, "Pit schedule", "Pits 1 to 4.")], "pit"))
        self.assertTrue(self.kept([rec(SAM, "Drainage", "Two culverts.")], "culvert"))


class HeaderAndLegendTests(unittest.TestCase):
    def test_recovered_emails_count_in_the_thread_header(self):
        q = quote_block(PAT, "Monday, 3 March 2025 4:15 PM", ALEX, "Fees", "Can you send the revised fee?")
        recs = [rec(ALEX, "RE: Fees", "Fee attached." + q, date="2025-03-05T09:00:00+10:00", to=[PAT]),
                rec(PAT, "RE: Fees", "Thanks, approved.", date="2025-03-06T09:00:00+10:00", to=[ALEX])]
        self.assertIn("## Fees (2 emails + 1 recovered, 25-03-03 to 25-03-06)", text_of(build(recs)))

    def test_bare_unknown_sender_is_explained(self):
        text = text_of(build([rec(("", ""), "Draft", "Draft never sent.", date="")]))
        self.assertIn("(no date) ?: Draft never sent.", text)
        self.assertIn("  ? = address unknown (a bare ? = no name or address)", text.split("\n"))

    def test_unknown_recipient_org_is_explained(self):
        recs = [rec(ALEX, "A", "To several.", to=[SAM, JO, ("Morgan Mystery", "")])]
        text = text_of(build(recs, squeeze="max"))
        self.assertIn("EC.AC>EB,EC,?: To several.", text)
        self.assertIn("  ? = address unknown", text.split("\n"))


class AlsoFiledTests(unittest.TestCase):
    def test_email_filed_outside_the_dates_is_not_recovered(self):
        body = "Hi Sam,\n\nPlease find attached the draft stormwater design report for your review."
        original = rec(ALEX, "Stormwater report", body, date="2025-01-31T11:15:00+10:00", to=[SAM],
                       attachments=["Stormwater report DRAFT.docx"])
        reply = rec(SAM, "RE: Stormwater report", "Comments by Friday." +
                    quote_block(ALEX, "Friday, 31 January 2025 11:15 AM", SAM, "Stormwater report", body),
                    date="2025-02-03T09:00:00+10:00", to=[ALEX])
        res = digest.build_digest([reply], project(date_from="2025-02-01"), now=NOW, outside_dates=1,
                                  also_filed=[original])
        self.assertNotIn("\u21b3", text_of(res))
        self.assertEqual(res["stats"]["emails_used"], 1)
        self.assertIn("\u21b3", text_of(digest.build_digest([reply], project(), now=NOW)))


class NoAccessHeaderTests(unittest.TestCase):
    def test_header_says_how_many_emails_could_not_be_opened(self):
        records = [rec(SAM, "Pump", "Hi Alex, the pump duty point is 35 L/s.", to=[ALEX])]
        text = text_of(digest.build_digest(records, project(), now=NOW, no_access_emails=40))
        self.assertIn("Not included: 40 emails in folders Squish could not open (no access)\n", text)
        text = text_of(digest.build_digest(records, project(), now=NOW, no_access_emails=1))
        self.assertIn("Not included: 1 email in folders", text)
        self.assertNotIn("Not included", text_of(build(records)))

    def test_header_says_how_many_files_could_not_be_read(self):
        # Claude only sees the digest, so it must say that emails may be missing.
        records = [rec(SAM, "Pump", "Hi Alex, the pump duty point is 35 L/s.", to=[ALEX])]
        text = text_of(digest.build_digest(records, project(), now=NOW, unreadable_files=0))
        self.assertNotIn("Not included", text)
        text = text_of(digest.build_digest(records, project(), now=NOW, unreadable_files=1))
        self.assertIn("\nNot included: 1 email file Squish could not read (damaged or locked), "
                      "so emails may be missing\n", text)
        text = text_of(digest.build_digest(records, project(), now=NOW, no_access_emails=2,
                                           unreadable_files=3))
        lines = text.split("\n")
        at = lines.index("Not included: 2 emails in folders Squish could not open (no access)")
        self.assertEqual(lines[at + 1], "Not included: 3 email files Squish could not read "
                                        "(damaged or locked), so emails may be missing")


def attached_email(sender, body, date="2025-03-03T16:15:00+10:00", subject="RE: Variation 3"):
    """An attachment holding an email (an email forwarded or attached as proof)."""
    return {"name": subject + ".msg", "size": 20000, "inline": False,
            "email": {"sender_name": sender[0], "sender_email": sender[1], "date": date,
                      "to": [list(ALEX)], "cc": [], "subject": subject, "body": body}}


class AttachedEmailTests(unittest.TestCase):
    APPROVAL = "Hi Alex,\n\nI'm happy with Variation 3 at $8,400 excl GST. Please proceed.\n\nRegards,\nPat"

    def carrier(self, text="Here it is.", date="2025-03-05T09:00:00+10:00"):
        return rec(ALEX, "RE: Variation 3", text, date=date, to=[JO],
                   attachments=[attached_email(PAT, self.APPROVAL)])

    def test_unfiled_attached_email_is_recovered(self):
        for squeeze in ("standard", "max"):
            res = build([self.carrier()], squeeze=squeeze)
            text = text_of(res)
            self.assertIn("  \u21b3 25-03-03 16:15 EXAMPL.PO: Hi Alex, I'm happy with Variation 3 at "
                          "$8,400 excl GST.", text, squeeze)
            self.assertEqual(res["stats"]["recovered_quoted"], 1)
            self.assertIn("forward or attachment", text)

    def test_filed_attached_email_is_not_recovered(self):
        original = rec(PAT, "RE: Variation 3", self.APPROVAL, date="2025-03-03T16:15:00+10:00", to=[ALEX])
        res = build([original, self.carrier()])
        self.assertNotIn("\u21b3", text_of(res))

    def test_attached_twice_is_shown_once(self):
        res = build([self.carrier(), self.carrier("Again, see attached.", date="2025-03-06T09:00:00+10:00")])
        self.assertEqual(text_of(res).count("\n  \u21b3 "), 1)

    def test_max_needs_a_thin_carrier(self):
        long_text = ("Jo, the client has approved the variation; the approval is attached. We can "
                     "now order the precast units and confirm the delivery date with the supplier.")
        self.assertNotIn("\u21b3", text_of(build([self.carrier(long_text)], squeeze="max")))
        self.assertIn("\u21b3", text_of(build([self.carrier(long_text)])))

    def test_quoted_email_inside_the_attachment_is_recovered_oldest_first(self):
        inner = self.APPROVAL + quote_block(ALEX, "Friday, 28 February 2025 2:00 PM", PAT, "Variation 3",
                                            "Hi Pat,\n\nPlease confirm you accept Variation 3 for the "
                                            "extra headwall.\n\nRegards,\nAlex")
        carrier = rec(ALEX, "RE: Variation 3", "Here it is.", date="2025-03-05T09:00:00+10:00", to=[JO],
                      attachments=[attached_email(PAT, inner)])
        lines = text_of(build([carrier])).split("\n")
        rec_lines = [l for l in lines if l.startswith("  \u21b3 ")]
        self.assertEqual(len(rec_lines), 2)
        self.assertIn("25-02-28 14:00", rec_lines[0])
        self.assertIn("25-03-03 16:15", rec_lines[1])

    def test_focus_keywords_search_the_attached_email(self):
        res = build([self.carrier()], focus_keywords="Variation 3, precast")
        self.assertEqual(res["stats"]["emails_used"], 1)
        res = build([rec(ALEX, "Site visit", "See attached.", to=[JO],
                         attachments=[attached_email(PAT, "The precast units arrive on Monday.",
                                                     subject="Delivery")])],
                    focus_keywords="precast")
        self.assertEqual(res["stats"]["emails_used"], 1)

    def test_inline_attachment_email_is_ignored(self):
        att = attached_email(PAT, self.APPROVAL)
        att["inline"] = True
        res = build([rec(ALEX, "RE: Variation 3", "Here it is.", to=[JO], attachments=[att])])
        self.assertNotIn("\u21b3", text_of(res))


class GreetingReplyTests(unittest.TestCase):
    def test_short_decision_after_a_greeting_line_is_kept_at_max(self):
        recs = [rec(SAM, "Variation", "Can we claim the extra rock excavation?", date="2025-03-04T09:00:00+10:00",
                    to=[ALEX]),
                rec(ALEX, "RE: Variation", "Hi Sam\n\nRejected, thanks.\n\nAlex", date="2025-03-04T10:00:00+10:00",
                    to=[SAM])]
        res = build(recs, squeeze="max")
        self.assertIn("10:00 EC.AC>EB: Hi Sam, Rejected, thanks.", text_of(res))
        self.assertEqual(res["stats"]["acks_dropped"], 0)


class SplitSpeedTests(unittest.TestCase):
    def test_very_long_thread_splits_quickly(self):
        recs = [rec(SAM if i % 2 else ALEX, "RE: Long thread" if i else "Long thread",
                    "Email %d about the retaining wall and its drainage." % i,
                    date=(datetime(2025, 1, 1) + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:00+10:00"))
                for i in range(1500)]
        old = digest.PART_SIZES["small"]["chars"]
        digest.PART_SIZES["small"]["chars"] = 20000
        try:
            start = time.time()
            res = digest.build_digest(recs, project(part_size="small"), now=NOW)
            took = time.time() - start
        finally:
            digest.PART_SIZES["small"]["chars"] = old
        self.assertLess(took, 10)
        parts = res["parts"]
        self.assertGreater(len(parts), 5)
        self.assertEqual(sum(len(email_lines(p["text"])) for p in parts), 1500)
        for n, p in enumerate(parts):
            self.assertLessEqual(len(p["text"]), 20000)
            self.assertIn("## Long thread (continued)" if n else "## Long thread (1500 emails", p["text"])



class WholeHourMatchTests(unittest.TestCase):
    SHORT = "Hi Alex\n\nPlease see the attached"
    LONG = ("Hi Alex,\n\nPlease see the attached quote. Lead time is 1 week for drawings and 4 weeks for "
            "manufacture. Price is $12,400 ex GST.")

    def test_a_short_email_an_hour_later_does_not_hide_a_longer_one(self):
        fw = rec(SAM, "FW: Access covers", "FYI, see below." +
                 quote_block(PAT, "Monday, 3 March 2025 10:00 AM", ALEX, "Access covers", self.SHORT) +
                 quote_block(PAT, "Monday, 3 March 2025 9:00 AM", ALEX, "Access covers", self.LONG),
                 date="2025-03-04T09:00:00+10:00", to=[JO])
        for squeeze in ("light", "standard"):
            self.assertIn("$12,400", text_of(build([fw], squeeze=squeeze)), squeeze)

    def test_a_filed_short_email_does_not_hide_a_longer_one(self):
        short = rec(PAT, "Access covers", self.SHORT, date="2025-03-03T10:00:00+10:00", to=[ALEX])
        fw = rec(SAM, "FW: Access covers", "FYI, see below." +
                 quote_block(PAT, "Monday, 3 March 2025 9:00 AM", ALEX, "Access covers", self.LONG),
                 date="2025-03-04T09:00:00+10:00", to=[JO])
        self.assertIn("$12,400", text_of(build([short, fw])))


class PrefixMatchTests(unittest.TestCase):
    OPENING = ("Hi Alex,\n\nWeekly site report for the Riverside depot works package, week ending Friday, "
               "prepared by the site team. ")

    def test_a_templated_report_that_opens_like_a_filed_one_is_recovered(self):
        week1 = rec(SAM, "Weekly report", self.OPENING + "Crane pad complete and formwork started.",
                    date="2025-03-03T09:00:00+10:00", to=[ALEX])
        reply = rec(ALEX, "RE: Weekly report", "Can you call me about this?" +
                    quote_block(SAM, "Monday, 10 March 2025 9:00 AM", ALEX, "Weekly report",
                                self.OPENING + "The crane failed inspection and a $45,000 variation is needed."),
                    date="2025-03-10T10:00:00+10:00", to=[SAM])
        text = text_of(build([week1, reply]))
        self.assertIn("\u21b3 25-03-10 09:00 EB.SB: ", text)
        self.assertIn("$45,000 variation is needed.", text)

    def test_the_same_text_a_few_minutes_off_is_still_filed(self):
        body = self.OPENING + "Crane pad complete and formwork started."
        filed = rec(SAM, "Weekly report", body, date="2025-03-03T09:06:00+10:00", to=[ALEX])
        reply = rec(ALEX, "RE: Weekly report", "Thanks Sam, noted the formwork." +
                    quote_block(SAM, "Monday, 3 March 2025 9:00 AM", ALEX, "Weekly report", body),
                    date="2025-03-04T10:00:00+10:00", to=[SAM])
        self.assertNotIn("\u21b3", text_of(build([filed, reply])))

    def test_two_unfiled_reports_with_one_opening_are_both_recovered(self):
        replies = []
        for n, (day, item) in enumerate([(10, "The crane failed inspection."), (17, "The crane passed inspection.")]):
            replies.append(rec(ALEX, "RE: Weekly report", "Noted, thanks %d." % n +
                               quote_block(SAM, "Monday, %d March 2025 9:00 AM" % day, ALEX, "Weekly report",
                                           self.OPENING + item),
                               date="2025-03-%02dT10:00:00+10:00" % day, to=[SAM]))
        res = build(replies)
        self.assertEqual(res["stats"]["recovered_quoted"], 2)
        self.assertIn("The crane failed inspection.", text_of(res))
        self.assertIn("The crane passed inspection.", text_of(res))


class InlineAnswerLabelTests(unittest.TestCase):
    def original(self):
        return rec(ALEX, "Pour", "Hi Sam,\n\nRequirements:\n\u2022 Starter bars to be lapped 600 mm.\n"
                   "\u2022 Conduits to be capped before the pour.\n\u2022 Slump to be tested on every truck.\n\n"
                   "Regards\nAlex", date="2025-03-03T09:00:00+10:00", to=[SAM])

    def reply(self, answered):
        return rec(SAM, "RE: Pour", "Hi Alex,\n\nPlease see below comments in red.\n" +
                   quote_block(ALEX, "Monday, 3 March 2025 9:00 AM", SAM, "Pour", answered),
                   date="2025-03-04T09:00:00+10:00", to=[ALEX])

    def line(self, answered):
        return [l for l in email_lines(text_of(build([self.original(), self.reply(answered)])))
                if "comments in red" in l][0]

    def test_bulleted_answers_on_their_own_line_are_labelled(self):
        line = self.line("Hi Sam,\n\nRequirements:\n\u2022 Starter bars to be lapped 600 mm.\n\u2022 Noted\n"
                         "\u2022 Conduits to be capped before the pour.\n\u2022 Noted, however this is a variation.\n"
                         "\u2022 Slump to be tested on every truck.\n\nRegards\nAlex")
        self.assertIn('[inline replies: re "Starter bars to be lapped 600\u2026": Noted '
                      're "Conduits to be capped before the\u2026": Noted, however this is a variation.]', line)

    def test_an_answer_over_two_lines_gets_one_label(self):
        line = self.line("Hi Sam,\n\nRequirements:\n\u2022 Starter bars to be lapped 600 mm.\n"
                         "\u2022 Conduits to be capped before the pour.\nThis is not on the issued drawings.\n"
                         "It will be claimed as a variation.\n\u2022 Slump to be tested on every truck.\n\n"
                         "Regards\nAlex")
        self.assertIn('[inline replies: re "Conduits to be capped before the\u2026": This is not on the issued '
                      'drawings. It will be claimed as a variation.]', line)
        self.assertEqual(line.count('re "'), 1)

    def test_a_signature_in_the_quoted_copy_is_not_an_answer(self):
        line = self.line("Hi Sam,\n\nRequirements:\n\u2022 Starter bars to be lapped 600 mm.\n"
                         "\u2022 Conduits to be capped before the pour.\nNo, this is a variation.\n"
                         "\u2022 Slump to be tested on every truck.\n\nRegards\nAlex\nAlex Citizen\n"
                         "Senior Engineer\nM: 0400 111 222\n")
        self.assertIn('re "Conduits to be capped before the\u2026": No, this is a variation.', line)
        self.assertNotIn('re "Regards', line)
        self.assertNotIn("0400", line)

    def test_a_short_leftover_is_not_an_answer(self):
        line = self.line("Hi Sam,\n\nRequirements:\n\u2022 Starter bars to be lapped 600 mm.\n"
                         "\u2022 Conduits to be capped before the pour.\n\u2022 Slump to be tested on "
                         "every truck.\n\nRegards\nAlex\nExampleCo\n")
        self.assertNotIn("[inline replies:", line)


class QuotedInvitePlaceTests(unittest.TestCase):
    def forward(self, where):
        invite = ("From: Pat Owner <pat.owner@example-client.com.au>\nSent: Monday, 3 March 2025 9:00 AM\n"
                  "To: Alex Citizen <alex.citizen@example-consulting.com>\nSubject: Site walk\n"
                  "When: Tuesday, 11 March 2025 10:00 AM-11:00 AM (UTC+10:00) Brisbane.\nWhere: %s\n\n"
                  "Walk of the north boundary with the council officer.\n" % where)
        return rec(ALEX, "FW: Site walk", "FYI, see below.\n\n" + invite, date="2025-03-04T09:00:00+10:00", to=[JO])

    def test_the_place_of_a_quoted_invite_is_kept(self):
        text = text_of(build([self.forward("Site office, 1 Example St")]))
        self.assertIn("When: Tuesday, 11 March 2025 10:00 AM-11:00 AM (UTC+10:00) Brisbane @ Site office, "
                      "1 Example St. Walk of the north boundary", text)

    def test_an_online_place_is_left_out(self):
        text = text_of(build([self.forward("Microsoft Teams Meeting")]))
        self.assertIn("(UTC+10:00) Brisbane. Walk of the north boundary", text)
        self.assertNotIn(" @ ", text)

    def test_a_long_place_is_cut(self):
        text = text_of(build([self.forward("Meeting room 4, " + "Level 3 " * 20 + "Example House")]))
        place = re.search(r"Brisbane @ (.*?)\. Walk", text).group(1)
        self.assertEqual(len(place), digest._PLACE_MAX)
        self.assertTrue(place.endswith("\u2026"))


class LateForwardTests(unittest.TestCase):
    def june(self):
        return [rec(SAM, "RFI 41 - Kerb return radius", "Hi Alex,\n\nCan the kerb return use a 6 m radius?",
                    date="2025-06-16T09:00:00+10:00", to=[ALEX]),
                rec(ALEX, "RE: RFI 41 - Kerb return radius", "Hi Sam,\n\nYes, a 6 m radius is fine.",
                    date="2025-06-17T09:00:00+10:00", to=[SAM])]

    def test_a_late_forward_that_quotes_the_conversation_stays_in_it(self):
        chain = (quote_block(PAT, "Monday, 20 July 2025 1:00 PM", JO, "RE: RFI 41 - Kerb return radius",
                             "Hi Jo,\n\nSee below about changing the kerb radius.") +
                 quote_block(ALEX, "Tuesday, 17 June 2025 9:00 AM", SAM, "RE: RFI 41 - Kerb return radius",
                             "Hi Sam,\n\nYes, a 6 m radius is fine."))
        fw = rec(JO, "FW: RFI 41 - Kerb return radius", "Alex, can you check this one?" + chain,
                 date="2025-08-05T09:00:00+10:00", to=[ALEX])
        res = build(self.june() + [fw])
        self.assertEqual(res["stats"]["threads"], 1)
        heads = [l for l in text_of(res).split("\n") if l.startswith("## ")]
        self.assertEqual(len(heads), 1)

    def test_a_late_forward_that_quotes_nothing_still_splits(self):
        fw = rec(JO, "FW: RFI 41 - Kerb return radius", "Alex, can you check this one?",
                 date="2025-08-05T09:00:00+10:00", to=[ALEX])
        self.assertEqual(build(self.june() + [fw])["stats"]["threads"], 2)


class PhotoNumberTests(unittest.TestCase):
    def test_photo_numbers_are_kept(self):
        self.assertEqual(digest._photo_label(["IMG_%d.jpeg" % n for n in range(9720, 9729)]),
                         "9 photos IMG_9720-9728")
        self.assertEqual(digest._photo_label(["IMG_9722.jpeg", "IMG_9720.jpeg", "IMG_9729.jpeg"]),
                         "3 photos IMG_9720,9722,9729")
        self.assertEqual(digest._photo_label(["IMG_%d.jpeg" % n for n in (1, 2, 3, 4, 5, 6, 8, 9)]),
                         "8 photos IMG_1,2,3,4,5,6,8,9")
        self.assertEqual(digest._photo_label(["IMG_1001.jpeg", "PXL_20250101_1.jpg"]), "2 photos")
        self.assertEqual(digest._photo_label(["IMG_1001 (1).jpeg", "IMG_1002.jpeg"]), "2 photos IMG_1001,1002")

    def test_photos_sent_again_keep_their_numbers(self):
        first = rec(SAM, "Pour 2", "1. Stair landing formwork fixed.", date="2025-03-03T09:00:00+10:00",
                    attachments=["IMG_%d.jpeg" % n for n in range(9720, 9729)])
        again = rec(SAM, "RE: Pour 2", "3. Joint filler placed along the kerb.", date="2025-03-04T09:00:00+10:00",
                    attachments=["IMG_9727.jpeg", "IMG_9728.jpeg", "IMG_9730.jpeg", "IMG_9731.jpeg"])
        text = text_of(build([first, again]))
        self.assertIn("[att: 9 photos IMG_9720-9728]", text)
        self.assertIn("[att: 2 photos IMG_9730,9731; +2 as above: IMG_9727,9728]", text)
        self.assertIn('"N as above" in [att: ...]', text)


class RecoveredDateSpanTests(unittest.TestCase):
    def test_covers_and_part_dates_include_recovered_emails(self):
        q = quote_block(PAT, "Friday, 28 February 2025 4:15 PM", ALEX, "Fees",
                        "Hi Alex,\n\nCan you send the revised fee for stage 2?\n\nRegards,\nPat")
        reply = rec(ALEX, "RE: Fees", "Hi Pat,\n\nRevised fee attached: $12,500 excl GST.\n" + q,
                    date="2025-03-05T09:00:00+10:00", to=[PAT])
        other = rec(SAM, "Pour", "Pour 3 is booked.", date="2025-03-06T09:00:00+10:00", to=[ALEX])
        res = build([reply, other])
        self.assertIn("\nCovers 2025-02-28 to 2025-03-06 | 2 emails in 2 threads", text_of(res))
        self.assertEqual(res["parts"][0]["first_date"], "2025-02-28")


class RepeatedOwnTextTests(unittest.TestCase):
    FILLER = ["Sentence number %d about the general state of the access road, the site compound and the "
              "laydown area." % n for n in range(14)]

    def test_new_sentences_in_a_reworked_email_are_kept(self):
        first = "Hi Jo, ok? " + " ".join(self.FILLER)
        new = ["The contractor must coordinate the shop drawings with the precaster.",
               "Any delay to the shop drawings is at the contractor's cost.",
               "The final version goes to the client on Friday afternoon."]
        second = "Hi Jo, ok? " + " ".join(self.FILLER) + " " + " ".join(new)
        self.assertLess(len(first), 1500)
        self.assertGreater(len(second), 1500)
        recs = [rec(ALEX, "Coordination", first, date="2025-03-04T09:00:00+10:00", to=[JO]),
                rec(ALEX, "RE: Coordination", second, date="2025-03-04T11:00:00+10:00", to=[JO])]
        lines = email_lines(text_of(build(recs)))
        self.assertTrue(lines[0].endswith(first), lines[0][-80:])
        for sentence in new:                       # every new sentence fits once the repeats go
            self.assertIn(sentence, lines[1])
        self.assertIn(" \u2026 ", lines[1])
        self.assertNotIn("Sentence number 12", lines[1])
        # another sender repeating the same text gets no special treatment
        recs[1] = rec(SAM, "RE: Coordination", second, date="2025-03-04T11:00:00+10:00", to=[JO])
        other = email_lines(text_of(build(recs)))[1]
        self.assertIn("Sentence number 12", other)
        self.assertNotIn(new[1], other)


class DomainLabelTests(unittest.TestCase):
    def test_country_codes_and_second_level_labels_are_skipped(self):
        for domain, label in [("nzta.govt.nz", "nzta"), ("aucklandcouncil.govt.nz", "aucklandcouncil"),
                              ("acmepiling.com.sg", "acmepiling"), ("acme.sg", "acme"), ("acme.co.za", "acme"),
                              ("acme.ie", "acme"), ("acme.co.jp", "acme"), ("acme.or.jp", "acme"),
                              ("acme.gob.mx", "acme"), ("mail.ge.com.sg", "ge"), ("x.sg", "x"), ("hp.com", "hp"),
                              ("mail.acmepumps.com.au", "acmepumps"), ("health.nsw.gov.au", "health"),
                              ("ab.co.nz", "ab"), ("example-consulting.com", "exampleconsulting")]:
            self.assertEqual(digest._domain_label(domain), label, domain)

    def test_same_name_at_two_overseas_firms_stays_two_people(self):
        recs = [rec(("Sean Byrne", "sean@dublinsteel.ie"), "Steel", "Steel delivery is on 4 June."),
                rec(("Sean Byrne", "sean.byrne@corkcivil.ie"), "Civil", "The kerbs are poured.")]
        text = text_of(build(recs))
        self.assertIn("DUBLIN.SB: Steel delivery is on 4 June.", text)
        self.assertIn("CORKCI.SB: The kerbs are poured.", text)

    def test_a_council_and_a_company_of_one_name_get_two_codes(self):
        self.assertEqual(digest._org_key("riverside-example.nsw.gov.au"), "riversideexample/nsw")
        self.assertEqual(digest._org_key("riverside-example.com.au"), "riversideexample")
        recs = [rec(("Pat Lee", "pat@riverside-example.com.au"), "Pump", "Our quote is $48,500 ex GST."),
                rec(("Peta Lim", "peta@riverside-example.nsw.gov.au"), "DA", "Condition 14 needs a plan."),
                rec(("Sally Bird", "sally@transport-example.nsw.gov.au"), "Road", "Road closure approved."),
                rec(("Sam Brown", "sam@transport-example.vic.gov.au"), "Road 2", "Permit issued."),
                rec(("Kim Ng", "kim@slrexample.com"), "A", "One firm."),
                rec(("Kay Ng", "kay@slrexample.org"), "B", "Same firm.")]
        text = text_of(build(recs))
        self.assertIn("RIVERS.PL: Our quote", text)
        self.assertIn("RIVERSNSW.PL: Condition 14", text)
        self.assertIn("TRANSPNSW.SB: Road closure", text)
        self.assertIn("TRANSPVIC.SB: Permit issued", text)
        self.assertIn("  SLREXA = slrexample.com, slrexample.org: ", text)     # .com and .org: one firm


class PlaceholderQuotedDateTests(unittest.TestCase):
    def test_a_quoted_date_in_year_9999_does_not_crash(self):
        filed = rec(PAT, "Levels", "Hi Alex,\n\nLevels attached.", date="2025-03-03T09:00:00+10:00", to=[ALEX])
        for sent in ("9999-12-31 23:30", "Friday, 31 December 9999 11:59 PM", "0001-01-01 00:00"):
            reply = rec(ALEX, "RE: Levels", "Pat, these levels are 50 mm too high." +
                        quote_block(PAT, sent, ALEX, "Levels", "Hi Alex,\n\nOld levels attached."),
                        date="2025-03-04T09:00:00+10:00", to=[PAT])
            self.assertIn("these levels are 50 mm too high.", text_of(build([filed, reply])), sent)


class FocusReferenceTests(unittest.TestCase):
    def kept(self, subject, words="RFI 12", attachments=()):
        return build([rec(SAM, subject, "See response.", attachments=attachments)],
                     focus_keywords=words)["stats"]["filtered_out"] == 0

    def test_reference_numbers_match_however_they_are_written(self):
        for subject in ("RFI-12 culvert", "RFI #12 culvert", "RFI012 culvert", "RFI 012 culvert",
                        "RFI No. 12 culvert", "RFI_12 culvert", "RFI12"):
            self.assertTrue(self.kept(subject), subject)
        self.assertTrue(self.kept("Culvert", attachments=["RFI_12_response.pdf"]))
        self.assertTrue(self.kept("RFI 12 lighting", "RFI-012"))
        self.assertTrue(self.kept("RFI 12 lighting", "rfi12"))

    def test_other_numbers_and_words_still_do_not_match(self):
        for subject, words in (("RFI 125", "RFI 12"), ("RFI 120", "RFI 12"), ("Hospital", "pit"),
                               ("a 1 metre wall", "a1"), ("piano 3", "pia 3"), ("the RFI. 12 units", "RFI 12")):
            self.assertFalse(self.kept(subject, words), (subject, words))


class SplitCancelTests(unittest.TestCase):
    def test_cancel_is_checked_while_parts_are_packed(self):
        import threading
        from unittest import mock
        recs = [rec(SAM, "Topic %d" % i, "Message %d." % i) for i in range(5)]
        stop = threading.Event()
        real = digest._split_parts

        def split(*args, **kwargs):
            stop.set()
            return real(*args, **kwargs)
        with mock.patch.object(digest, "_split_parts", split):
            with self.assertRaises(digest.DigestCancelled):
                digest.build_digest(recs, project(part_size="small"), now=NOW, cancel=stop)

    def test_cached_people_give_the_same_legend(self):
        people = digest.People(digest.parse_org_codes(ORGS))
        for name, email in (ALEX, SAM, PAT, JO):
            people.learn(name, email)
        people.finish()
        idents = set(people.identity(n, e) for n, e in (ALEX, SAM, PAT))
        people.assign_aliases(idents)
        known = {}
        self.assertEqual(digest._legend_lines(people, idents, {people.identity(*JO)}),
                         digest._legend_lines(people, idents, {people.identity(*JO)},
                                              person_info=lambda i: known.setdefault(i, people.info(i))))


class AliasCaseTests(unittest.TestCase):
    def test_aliases_never_differ_only_in_case(self):
        recs = [rec(("Mabel", "mabel@example-consulting.com"), "A", "One."),
                rec(("Mark Adams", "mark@example-consulting.com"), "B", "Two.")]
        text = text_of(build(recs))
        aliases = re.findall(r"EC\.(\w+): (?:One|Two)\.", text)
        self.assertEqual(len(aliases), 2)
        self.assertEqual(len(set(a.lower() for a in aliases)), 2, aliases)

    def test_longer_aliases_are_letters_only(self):
        recs = [rec(("Mark Oakes", "mark.oakes@example-consulting.com"), "A", "One."),
                rec(("Mark Oakes", "mark.oakes@example-consulting.com"), "B", "Two."),
                rec(("Mary-Jane O'Neil", "mj@example-consulting.com"), "C", "Three.")]
        text = text_of(build(recs))
        alias = re.search(r"EC\.(\S+): Three\.", text).group(1)
        self.assertEqual(alias, "MOn")
        self.assertIn("MOn=Mary-Jane O'Neil", text)


if __name__ == "__main__":
    unittest.main()
