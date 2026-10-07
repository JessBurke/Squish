"""Tests for squish_app.docdigest (synthetic DocText inputs only) and the
documents cross-references in squish_app.digest (doc_ids, alias_for_records)."""

import json
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

from squish_app import digest, docdigest

_TMP = None
NOW = datetime(2026, 10, 6, 14, 5)
SOURCE = "H:\\Jobs\\Riverside\\01 Emails (attachments) + H:\\Jobs\\Riverside\\04 Reports"
ORGS = "riverside.example=RC\nexample-consulting.com=EC"


def setUpModule():
    global _TMP
    _TMP = tempfile.TemporaryDirectory()
    os.environ["SQUISH_DATA_DIR"] = _TMP.name


def tearDownModule():
    _TMP.cleanup()


# --------------------------------------------------------------------------
# Building synthetic inputs

def doctext(kind="docx", blocks=(), status="ok", note="", title="", pages=None, drawing=False):
    """A DocText dict (see DESIGN.md). Blocks may be given as (type, text[, level])."""
    out = []
    for b in blocks:
        if isinstance(b, dict):
            out.append(b)
        else:
            out.append({"type": b[0], "text": b[1], "level": b[2] if len(b) > 2 else 0})
    chars = sum(len(b.get("text") or "") for b in out)
    return {"kind": kind, "status": status, "note": note, "title": title, "pages": pages,
            "drawing": drawing, "blocks": out, "chars": chars, "reader": "builtin"}


def email_src(date="2025-03-04T09:00:00+10:00", alias="RC.SB", name="Sam Brown",
              email="sam.brown@riverside.example", subject="RE: Geotech report"):
    return {"kind": "email", "date": date, "sender_alias": alias, "sender_name": name,
            "sender_email": email, "subject": subject, "thread": re.sub(r"^RE: ", "", subject)}


def file_src(path="H:\\Jobs\\Riverside\\04 Reports\\Report.pdf", mtime="2025-04-01T10:00:00+10:00"):
    return {"kind": "file", "path": path, "mtime": mtime}


def item(did, name, doc, sources=None, size=12345):
    return {"id": did, "name": name, "sha1": "sha-" + did + name, "size": size, "doc": doc,
            "sources": [email_src()] if sources is None else sources}


def build(docs, **kw):
    project = {"name": "Riverside Depot", "squeeze": "standard", "part_size": "medium", "org_codes": ORGS}
    project.update(kw)
    return docdigest.build_documents_digest(docs, project, source_label=SOURCE, now=NOW)


def text_of(result):
    return "\n".join(p["text"] for p in result["parts"])


def section(text, did):
    """The lines of one document's section ('## D3 ...' up to the next '## ')."""
    m = re.search(r"(?ms)^## %s .*?(?=^## |\Z)" % re.escape(did), text)
    return m.group(0) if m else ""


_SUBJECTS = ["The site walkover", "The desktop review", "The field team", "The project team", "The review",
             "The investigation", "Our assessment", "The inspection", "The survey crew", "The design review"]
_VERBS = ["looked at", "considered", "described", "examined", "noted", "covered", "summarised", "discussed"]
_OBJECTS = ["the general ground conditions", "the surface features", "the existing structures",
            "the drainage paths", "the access arrangements", "the published geology", "the vegetation cover",
            "the neighbouring properties", "the historical aerial photographs", "the utility records"]
_PLACES = ["northern boundary", "southern car park", "eastern embankment", "western fence line",
           "main workshop", "stores building", "wash bay", "fuel area", "office block", "laydown yard"]


def prose(n, seed):
    """n sentences of plain prose with no figures or requirements (nothing a cap would favour)."""
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        out.append("%s %s %s near the %s and the %s." % (
            rnd.choice(_SUBJECTS), rnd.choice(_VERBS), rnd.choice(_OBJECTS), rnd.choice(_PLACES),
            rnd.choice(_PLACES)))
    return " ".join(out)


FACTS = [
    "The allowable bearing pressure for pad footings is 150 kPa.",
    "Groundwater was measured at 2.4 m below ground level in BH3.",
    "The contractor shall provide shoring for excavations deeper than 1.5 m.",
    "The estimated cost of the remediation works is $245,000.",
    "Compaction testing to 98% standard is required under Clause 6.2.",
    "The pavement design life is 25 years in accordance with AS 2870.",
]


def long_report(sections=8, paras=6, facts=FACTS, seed=1):
    """A Word-like report: headings, many prose paragraphs, facts planted deep inside."""
    blocks = [("heading", "Riverside Depot Geotechnical Investigation", 1),
              ("para", "This report presents the ground investigation for the new depot workshop.")]
    planted = list(facts)
    for s in range(sections):
        blocks.append(("heading", "%d Section %s" % (s + 1, "ABCDEFGHIJKL"[s]), 1))
        for p in range(paras):
            text = prose(5, seed * 1000 + s * 50 + p)
            if p == paras - 1 and planted:
                text += " " + planted.pop(0)
            blocks.append(("para", text))
    return doctext("docx", blocks)


# --------------------------------------------------------------------------

class CapTests(unittest.TestCase):
    def body(self, result, did):
        sec = section(text_of(result), did)
        return "\n".join(sec.split("\n")[2:]).strip()

    def test_caps_per_level(self):
        doc = long_report(sections=12, paras=10)
        sizes = {}
        for level, caps in docdigest.DOC_CAPS.items():
            body = self.body(build([item("D1", "Report.docx", doc)], squeeze=level), "D1")
            sizes[level] = len(body)
            self.assertLessEqual(len(body), caps["chars"] * 1.05, level)
        self.assertGreater(sizes["light"], sizes["standard"])
        self.assertGreater(sizes["standard"], sizes["max"])
        self.assertGreater(sizes["max"], 1500)
        self.assertEqual(list(docdigest.DOC_CAPS), ["light", "standard", "max"])
        self.assertEqual(docdigest.DOC_CAPS["standard"]["chars"], 8000)
        self.assertEqual(docdigest.DOC_CAPS["max"]["rows"], 30)
        self.assertEqual(docdigest.DOC_CAPS["light"]["pages"], 200)

    def test_short_document_is_kept_whole(self):
        doc = doctext("docx", [("heading", "Site instruction 14", 1),
                               ("para", "Excavations deeper than 1.5 m require shoring."),
                               ("item", "\u2022 Contractor to submit shoring design."),
                               ("item", "\u2022 Engineer to inspect footing excavation.")])
        text = text_of(build([item("D1", "SI-14.docx", doc)]))
        self.assertIn("# Site instruction 14\nExcavations deeper than 1.5 m require shoring. "
                      "\u2022 Contractor to submit shoring design. \u2022 Engineer to inspect footing excavation.",
                      text)
        self.assertNotIn("\u2026", section(text, "D1"))

    def test_headings_opening_and_facts_kept(self):
        doc = long_report(sections=8, paras=8)
        for level in ("standard", "max"):
            body = self.body(build([item("D1", "Report.docx", doc)], squeeze=level), "D1")
            for s in range(8):
                self.assertIn("# %d Section %s" % (s + 1, "ABCDEFGH"[s]), body, level)
            self.assertIn("This report presents the ground investigation", body)
            for fact in FACTS:
                self.assertIn(fact, body, level)
            self.assertIn("\u2026", body)

    def test_heading_share_limits_headings_and_keeps_top_levels(self):
        blocks = []
        for n in range(60):
            # (headings that differ only in their numbers would be folded into one)
            code = "".join(chr(97 + d) for d in (n // 26, n % 26))
            blocks.append(("heading", "%d Chapter about item %s" % (n + 1, code), 1))
            blocks.append(("heading", "%d.1 A rather long sub heading for item %s with words" % (n + 1, code), 2))
            blocks.append(("para", prose(3, n)))
        body = self.body(build([item("D1", "Big.docx", doctext("docx", blocks))], squeeze="max"), "D1")
        heads = [l for l in body.split("\n") if l.startswith("# ")]
        self.assertLessEqual(sum(len(h) + 1 for h in heads), docdigest.DOC_CAPS["max"]["chars"] * 0.2 + 50)
        self.assertTrue(any("Chapter" in h for h in heads))
        self.assertFalse(any("sub heading" in h for h in heads))   # level 1 first

    def test_long_fact_sentence_split_at_clauses(self):
        filler = prose(40, 7)
        fact = ("Following the review of the borehole logs, the laboratory results and the in situ testing "
                "carried out across the whole of the site during the second stage of the works, and after "
                "discussion with the structural engineer and the client's representative, it was agreed that "
                "the allowable bearing pressure is 120 kPa for strip footings founded in the stiff clay, "
                "while pad footings in the weathered rock may be designed for a higher pressure once the "
                "results of the additional cored boreholes have been received and reviewed by the team")
        doc = doctext("docx", [("para", filler), ("para", fact + ". " + prose(40, 8))])
        body = self.body(build([item("D1", "Memo.docx", doc)], squeeze="max"), "D1")
        self.assertIn("120 kPa", body)
        self.assertLessEqual(len(body), docdigest.DOC_CAPS["max"]["chars"] * 1.05)

    def test_structural_designations_count_as_figures(self):
        plain = docdigest.doc_fact_score("Try a deeper beam with a cap channel.")
        self.assertGreater(docdigest.doc_fact_score("Try 360UB56.7 with 200PFC cap channel."), plain)
        for text in ("Posts are 150x150x9.0 SHS.", "Add N16-200 each way.", "Provide 2-N20 trimmer bars.",
                     "SL92 mesh top.", "Use M24 chemical anchors.", "Bolts are 8.8/S.", "Grade 300PLUS steel."):
            self.assertGreaterEqual(docdigest.doc_fact_score(text), docdigest.HARD_BONUS + 2, text)

    def test_a_long_listing_does_not_crowd_out_the_text_around_it(self):
        # A calculation package: one key sentence among pages of member checks
        # that only change their figures.
        blocks = [("heading", "C-07 CRANE RUNWAY BEAM", 1),
                  ("para", prose(6, 3)),
                  ("para", "Try 360UB56.7 with 200PFC cap channel. Span 8.0 m simply supported.")]
        for n in range(1, 80):
            blocks.append(("para", "Member C-07-%02d L=%d.%d m N*=%d kN M*=%d kNm util=0.%02d OK"
                           % (n, 2 + n % 6, n % 10, 20 + 3 * n, 10 + 2 * n, 30 + n % 60)))
        doc = doctext("pdf", [("page", "", 1)] + blocks, pages=1)
        for level in ("standard", "max"):
            body = self.body(build([item("D1", "Calcs.pdf", doc)], squeeze=level), "D1")
            self.assertIn("360UB56.7", body, level)
            self.assertIn("Member C-07-", body, level)       # (some of the listing still shows)

    def test_pdf_page_cap_noted(self):
        blocks = []
        for page in range(1, 41):
            blocks.append(("page", "", page))
            blocks.append(("para", prose(3, page) + (" Settlement of 25 mm is expected." if page == 39 else "")))
        doc = doctext("pdf", blocks, pages=40)
        text = text_of(build([item("D1", "Long.pdf", doc)], squeeze="max"))
        self.assertIn("(pages 16-40 not shown)", text)
        self.assertNotIn("25 mm", text)
        self.assertIn("(PDF, 40 pages)", text)

    def test_spreadsheet_rows(self):
        rows = [{"type": "sheet", "text": "Quantities", "level": 1, "rows": 302}]
        rows.append({"type": "row", "text": "Item | Description | Qty | Rate | Amount", "level": 0})
        for n in range(1, 301):
            rows.append({"type": "row", "text": "%d | Excavation zone %d | %d | 12.5 | %d" % (n, n, n * 3, n * 37),
                         "level": n})
        rows.append({"type": "row", "text": " | Total | | | 1669810", "level": 301})
        rows.append({"type": "sheet", "text": "Rates", "level": 2, "hidden": True, "rows": 0})
        doc = doctext("xlsx", rows, pages=2)
        for level, caps in docdigest.DOC_CAPS.items():
            body = self.body(build([item("D1", "BoQ.xlsx", doc)], squeeze=level), "D1")
            lines = body.split("\n")
            self.assertEqual(lines[0], "# Sheet Quantities (302 rows)")
            self.assertEqual(lines[1], "Item | Description | Qty | Rate | Amount")
            self.assertIn("| Total | | | 1669810", lines)
            data = [l for l in lines if re.match(r"^\d+ \| Excavation", l)]
            self.assertLessEqual(len(data), caps["rows"])
            self.assertTrue(data)
            more = [l for l in lines if l.startswith("(+")]
            left = 302 - 2 - len(data)
            self.assertEqual(more, ["(+%d more rows)" % left] if left else [])
            self.assertIn("# Sheet Rates (hidden)", lines)
            numbers = [int(l.split(" | ")[0]) for l in data]
            self.assertEqual(numbers, sorted(numbers))     # original order
        self.assertIn('"(+N more rows)"', text_of(build([item("D1", "BoQ.xlsx", doc)])))

    def test_slides(self):
        doc = doctext("pptx", [("slide", "Monthly update", 1), ("para", "March 2025 progress."),
                               ("slide", "Cost summary", 2), ("row", "Package | Budget", 0),
                               ("row", "Structures | $670,000", 1), ("para", "Notes: contingency 10%.")],
                      pages=2)
        text = text_of(build([item("D1", "Update.pptx", doc)]))
        self.assertIn("## D1 Update.pptx (PowerPoint, 2 slides)", text)
        self.assertIn("# Slide 1: Monthly update\nMarch 2025 progress.\n# Slide 2: Cost summary\n"
                      "Package | Budget\nStructures | $670,000\nNotes: contingency 10%.", text)


class BoilerplateTests(unittest.TestCase):
    def test_copyright_and_limitations_removed(self):
        doc = doctext("docx", [
            ("heading", "Geotechnical Report", 1),
            ("para", "\u00a9 Example Consulting Pty Ltd 2025. All rights reserved. This document must not be "
                     "reproduced without the written permission of Example Consulting."),
            ("para", "This report has been prepared for the sole use of Riverside Council and no "
                     "responsibility is accepted to any third party who relies on it."),
            ("para", "The site is underlain by stiff clay over weathered siltstone."),
            ("heading", "9 Limitations", 1),
            ("para", "Subsurface conditions can change and our professional judgement is based on the "
                     "information available at the time."),
            ("para", "The investigation comprised five boreholes to a maximum depth of 6 m."),
            ("heading", "10 Statement of limitations", 1),
            ("para", "This report should be read in full and no part should be relied upon in isolation."),
            ("heading", "Appendix C - Important information about your report", 1),
            ("item", "\u2022 Reports are written for a particular client and should not be used by others."),
            ("heading", "Appendix D - Test certificates", 1),
            ("para", "Accredited for compliance with ISO/IEC 17025. Results relate only to the items tested."),
            ("para", "This document remains the property of Example Consulting; unauthorised use is prohibited."),
            ("heading", "11 References", 1),
            ("para", "Standards Australia, AS 1726 Geotechnical site investigations."),
            ("para", "Example Consulting Pty Ltd ABN 12 345 678 901 www.example-consulting.com T: 02 9999 0000"),
        ])
        text = section(text_of(build([item("D1", "Report.docx", doc)])), "D1")
        self.assertNotIn("rights reserved", text)
        self.assertNotIn("sole use", text)
        self.assertNotIn("professional judgement", text)
        self.assertNotIn("relied upon in isolation", text)
        self.assertNotIn("Statement of limitations", text)     # nothing left under it
        self.assertNotIn("ABN", text)
        self.assertNotIn("Important information", text)
        self.assertNotIn("particular client", text)
        self.assertNotIn("17025", text)
        self.assertNotIn("property of", text)
        self.assertIn("stiff clay over weathered siltstone", text)
        self.assertIn("# 9 Limitations\nThe investigation comprised five boreholes to a maximum depth of 6 m.", text)
        self.assertIn("AS 1726", text)

    def test_contents_list_removed(self):
        doc = doctext("pdf", [
            ("page", "", 1), ("para", "Riverside Depot Geotechnical Report"),
            ("page", "", 2), ("heading", "Contents", 1),
            ("para", "Executive summary ................ 2"),
            ("para", "1 Introduction 3"), ("para", "2 Site description 4"),
            ("para", "3 Ground conditions 7"), ("para", "Appendix A Borehole logs 21"),
            ("page", "", 3), ("heading", "1 Introduction", 1),
            ("para", "The depot is to be extended to the north."),
            ("page", "", 4), ("para", "1.1 Scope 3"), ("para", "1.2 Site 4"), ("para", "1.3 Geology 5"),
            ("para", "The scope covered five boreholes."),
        ], pages=4)
        text = section(text_of(build([item("D1", "Report.pdf", doc)])), "D1")
        self.assertNotIn("Contents", text)
        self.assertNotIn("Site description 4", text)
        self.assertNotIn("Borehole logs 21", text)
        self.assertNotIn("....", text)
        self.assertNotIn("Geology 5", text)
        self.assertIn("# 1 Introduction", text)
        self.assertIn("The depot is to be extended to the north.", text)

    def test_repeated_page_headers_footers_and_numbers_removed(self):
        blocks = []
        for page in range(1, 9):
            blocks += [("page", "", page),
                       ("para", "Riverside Depot | Geotechnical Report | 623.0001-RPT-001 Rev C"),
                       ("para", "Paragraph %s about the ground conditions on this part of the site."
                        % "ABCDEFGH"[page - 1]),
                       ("para", "Page %d of 8" % page if page % 2 else str(page))]
        doc = doctext("pdf", blocks, pages=8)
        text = section(text_of(build([item("D1", "Report.pdf", doc)])), "D1")
        self.assertNotIn("623.0001-RPT-001", text)
        self.assertNotIn("Page 3 of 8", text)
        self.assertNotRegex(text, r"(?m)^\d$| 4 Paragraph")
        for letter in "ABCDEFGH":
            self.assertIn("Paragraph %s about" % letter, text)

    def test_company_copyright_notes_go_but_list_labels_stay(self):
        self.assertTrue(docdigest._boilerplate(
            "(c) Example Consulting Pty Ltd. This drawing is confidential and shall only be used for "
            "the purposes of this project.", False, True))
        for text in ("Load case (c) Wind uplift governs.", "(c) the Contractor shall provide a limited warranty"):
            self.assertFalse(docdigest._boilerplate(text, False, True), text)

    def test_end_of_a_dropped_sentence_on_its_own_line_goes_too(self):
        doc = doctext("pdf", [("page", "", 1), ("heading", "Purchase order", 1),
                              ("para", "Total $629,277.00"),
                              ("para", "This report has been prepared for the sole use of Riverside Council and no "
                                       "liability is accepted to any third party arising in"),
                              ("para", "connection with this report."),
                              ("para", "Payment terms are 30 days.")], pages=1)
        body = text_of(build([item("D1", "PO.pdf", doc)]))
        self.assertIn("Total $629,277.00", body)
        self.assertNotIn("connection with this report", body)
        self.assertNotIn("sole use", body)
        self.assertIn("Payment terms are 30 days.", body)

    def test_repeated_paragraphs_and_empty_cells(self):
        note = "All dimensions are in millimetres unless noted otherwise on the drawings."
        doc = doctext("docx", [("para", note), ("para", "First part."), ("para", note),
                               ("row", "Item | | Qty", 0), ("row", "Concrete | | 25", 1), ("row", " |  | ", 2)])
        text = section(text_of(build([item("D1", "Spec.docx", doc)])), "D1")
        self.assertEqual(text.count("millimetres"), 1)
        self.assertIn("Item | Qty\nConcrete | 25", text)
        # A sparse row's 'header: value' cells count as figures by their value.
        self.assertEqual(docdigest._row_score("Footing F4 | W12: 1200 | W13: 300", 0),
                         docdigest._row_score("Footing F4 | 1200 | 300", 0))
        sheet = doctext("xlsx", [{"type": "sheet", "text": "S1", "level": 1, "rows": 2},
                                 ("row", "Ref | | | Far", 0), ("row", "Total | | 4513426.78 | ", 1)], pages=1)
        text = section(text_of(build([item("D2", "Sheet.xlsx", sheet)])), "D2")
        self.assertIn("Ref | | | Far\nTotal | | 4513426.78", text)

    def test_wide_sparse_sheet_keeps_its_labelled_cells(self):
        head = "Task | Start | " + " | ".join("W%d" % n for n in range(1, 61))
        rows = [("row", head, 0)]
        for n in range(1, 31):
            rows.append(("row", "Pour slab bay %d | 2025-03-%02d | W%d: x | W%d: x" % (n, n % 28 + 1, n, n + 1), n))
        sheet = doctext("xlsx", [{"type": "sheet", "text": "Programme", "level": 1, "rows": 31}] + rows, pages=1)
        text = section(text_of(build([item("D1", "Programme Rev 6.xlsx", sheet)])), "D1")
        self.assertIn("Pour slab bay 7 | 2025-03-08 | W7: x | W8: x", text)
        self.assertNotIn("| | | |", text)

    def test_word_header_and_footer_lines(self):
        doc = doctext("docx", [("heading", "1 Scope", 1), ("para", "The works cover the new wash bay."),
                               ("heading", "2 Limitations", 1),
                               ("para", "This report must not be relied upon by any third party."),
                               ("para", "Header: Riverside Depot | Wash bay report | 623-RPT-002 Rev C"),
                               ("para", "Footer: \u00a9 Example Consulting Pty Ltd. All rights reserved.")])
        doc2 = doctext("docx", [("para", "Text."),
                                ("para", "Footer: 623-RPT-002 | 14 March 2025 | Commercial in confidence")])
        self.assertIn("\nFooter: 623-RPT-002 | 14 March 2025\n", text_of(build([item("D3", "R.docx", doc2)])))
        text = section(text_of(build([item("D1", "Report.docx", doc)])), "D1")
        self.assertIn("The works cover the new wash bay.\nHeader: Riverside Depot | Wash bay report | "
                      "623-RPT-002 Rev C", text)
        self.assertNotIn("Limitations", text)
        self.assertNotIn("Footer", text)
        self.assertNotIn("(no text left", text)
        # A watermark that is only a classification label leaves no empty label behind.
        doc3 = doctext("docx", [("para", "Text."),
                                ("para", "Header: Watermark: CONFIDENTIAL | 623-RPT-002 | Wash bay")])
        self.assertIn("\nHeader: 623-RPT-002 | Wash bay\n", text_of(build([item("D4", "R.docx", doc3)])))
        empty = doctext("docx", [("para", "\u00a9 Example Consulting 2025. All rights reserved.")])
        self.assertIn("(no text left after removing", text_of(build([item("D2", "Cover.docx", empty)])))

    def test_pdf_lines_joined_across_pages(self):
        doc = doctext("pdf", [("page", "", 1),
                              ("para", "The geotechnical engineer shall provide the programme for the deck pour on 12"),
                              ("page", "", 2),
                              ("para", "March subject to the engineer's approval of drawing 623.0001-ST-"),
                              ("para", "1200 Rev C.")], pages=2)
        text = text_of(build([item("D1", "Letter.pdf", doc)]))
        self.assertIn("for the deck pour on 12 March subject to the engineer's approval", text)


class VersionTests(unittest.TestCase):
    def report(self, pressure, extra=""):
        paras = [("heading", "1 Introduction", 1), ("para", prose(6, 1)),
                 ("heading", "6 Foundations", 1),
                 ("para", prose(4, 2) + " The allowable bearing pressure is %d kPa for pad footings. " % pressure
                  + prose(4, 3)),
                 ("para", prose(6, 4))]
        if extra:
            paras.append(("para", extra))
        return doctext("docx", paras)

    def test_family_key(self):
        same = ["Geotech Report Rev B.pdf", "Geotech report_rev C.docx", "Geotech Report v2.pdf",
                "Geotech Report [C].pdf", "Geotech Report (1).pdf", "Geotech Report 2025-03-04.pdf",
                "Geotech Report - FINAL.pdf", "geotech  report RevB.pdf", "Geotech Report 14 March 2025.pdf"]
        keys = set(docdigest.family_key(n) for n in same)
        self.assertEqual(keys, {"geotech report"})
        self.assertNotEqual(docdigest.family_key("Pavement Report.pdf"), "geotech report")
        self.assertEqual(docdigest.family_key("Rev B.pdf"), "")
        self.assertEqual(docdigest.family_key("Review of design.pdf"), "review of design")

    def test_similar_version_shows_changes(self):
        docs = [item("D1", "Geotech Report Rev B.docx", self.report(150)),
                item("D2", "Geotech Report Rev C.docx", self.report(120, "Groundwater was found at 2.4 m."),
                     [email_src("2025-04-10T09:00:00+10:00")])]
        result = build(docs)
        text = text_of(result)
        d2 = section(text, "D2")
        self.assertIn("Changes from D1:", d2)
        self.assertIn("# 6 Foundations", d2)
        self.assertRegex(d2, r"(?m)^\+ .*pressure is 120 kPa for pad footings.*$")
        self.assertRegex(d2, r"(?m)^- .*pressure is 150 kPa.*$")
        self.assertIn("+ Groundwater was found at 2.4 m.", d2)
        self.assertLess(len(d2), 900)
        self.assertEqual(result["stats"]["doc_versions"], 1)
        self.assertIn("1 later version shown as changes", text)
        self.assertIn('"Changes from D7:"', text)
        self.assertIn("150 kPa", section(text, "D1"))     # the first version in full

    def test_an_older_revision_found_later_is_still_the_older_one(self):
        # Rev C came by email (D1); Rev B sits in the documents folder's Superseded
        # folder, so it is numbered after it (D2). Rev C is still the later version.
        docs = [item("D1", "Geotech Report Rev C.docx", self.report(120, "Groundwater was found at 2.4 m."),
                     [email_src("2025-04-10T09:00:00+10:00")]),
                item("D2", "Geotech Report Rev B.docx", self.report(150),
                     [file_src("H:\\Jobs\\Riverside\\04 Reports\\Superseded\\Geotech Report Rev B.docx")])]
        text = text_of(build(docs))
        d1, d2 = section(text, "D1"), section(text, "D2")
        self.assertIn("Changes from D2:", d1)
        self.assertRegex(d1, r"(?m)^\+ .*pressure is 120 kPa")
        self.assertRegex(d1, r"(?m)^- .*pressure is 150 kPa")
        self.assertNotIn("Changes from", d2)
        self.assertIn("150 kPa", d2)                   # the earlier revision in full
        # Revisions run P1, P2, then letters, then numbers.
        names = ["R Rev 1.pdf", "R Rev A.pdf", "R [P2].pdf", "R Rev 0.pdf", "R_rev B.pdf", "R P1.pdf"]
        order = sorted(names, key=docdigest._revision_key)
        self.assertEqual(order, ["R P1.pdf", "R [P2].pdf", "R Rev A.pdf", "R_rev B.pdf", "R Rev 0.pdf",
                                 "R Rev 1.pdf"])
        # Without revisions, dates in the names (else when they were sent or saved) decide.
        docs = [item("D1", "RFI Register 2025-10-31.docx", self.report(120)),
                item("D2", "RFI Register 2025-06-30.docx", self.report(150))]
        self.assertIn("Changes from D2:", section(text_of(build(docs)), "D1"))

    def test_versions_compared_beyond_the_page_cap(self):
        def pdf(pressure):
            blocks = []
            for page in range(1, 31):
                text = prose(3, page)
                if page == 25:
                    text += " The design bearing pressure is %d kPa." % pressure
                blocks += [("page", "", page), ("para", text)]
            return doctext("pdf", blocks, pages=30)
        docs = [item("D1", "Report Rev A.pdf", pdf(150)), item("D2", "Report Rev B.pdf", pdf(120))]
        d2 = section(text_of(build(docs, squeeze="max")), "D2")
        self.assertIn("Changes from D1:", d2)
        self.assertIn("120 kPa", d2)
        self.assertNotIn("Same text", d2)

    def test_register_versions_show_changed_rows(self):
        def register(n_rows, closed):
            blocks = [{"type": "sheet", "text": "Register", "level": 1, "rows": n_rows + 1},
                      ("row", "RFI | Subject | Status", 0)]
            for n in range(1, n_rows + 1):
                status = "Closed" if n in closed else "Open"
                blocks.append(("row", "RFI-%03d | Question about grid line %d | %s" % (n, n, status), n))
            return doctext("xlsx", blocks, pages=1)
        docs = [item("D1", "RFI Register 2025-06-30.xlsx", register(60, set(range(1, 50)))),
                item("D2", "RFI Register 2025-10-31.xlsx", register(64, set(range(1, 52))))]
        d2 = section(text_of(build(docs)), "D2")
        self.assertIn("Changes from D1:", d2)
        self.assertIn("+ RFI-050 | Question about grid line 50 | Closed", d2)
        self.assertIn("+ RFI-050 | Question about grid line 50 | Closed\n- RFI-050 | \u2026 | Open", d2)
        self.assertIn("+ RFI-064 | Question about grid line 64 | Open", d2)
        self.assertNotIn("RFI-020", d2)

    def test_dissimilar_version_shown_in_full(self):
        other = doctext("docx", [("heading", "Pavement design", 1), ("para", prose(30, 99))])
        docs = [item("D1", "Geotech Report Rev B.docx", self.report(150)),
                item("D2", "Geotech Report Rev C.docx", other)]
        result = build(docs)
        d2 = section(text_of(result), "D2")
        self.assertNotIn("Changes from", d2)
        self.assertIn("# Pavement design", d2)
        self.assertEqual(result["stats"]["doc_versions"], 0)

    def test_same_text_under_another_name(self):
        docs = [item("D1", "Report.docx", self.report(150)), item("D2", "Issued copy for client.pdf",
                                                                  self.report(150))]
        d2 = section(text_of(build(docs)), "D2")
        self.assertIn("Same text as D1.", d2)

    def test_word_and_pdf_of_one_report_line_up(self):
        word = self.report(150)
        pdf_blocks = [("page", "", 1)]
        for b in word["blocks"]:
            if b["type"] == "heading":
                pdf_blocks.append(("para", b["text"]))
            else:   # the PDF wraps the paragraph into lines and splits it over a page
                words = b["text"].split()
                half = len(words) // 2
                pdf_blocks += [("para", " ".join(words[:half])), ("page", "", 2), ("para", " ".join(words[half:]))]
        pdf = doctext("pdf", pdf_blocks, pages=2)
        docs = [item("D1", "Geotech Report.docx", word), item("D2", "Geotech Report Rev 0.pdf", pdf)]
        d2 = section(text_of(build(docs)), "D2")
        self.assertIn("Changes from D1: none (the text is the same).", d2)
        # the same name in another format: one document
        docs = [item("D1", "Geotech Report.docx", word), item("D2", "Geotech Report.pdf", pdf)]
        self.assertIn("Same text as D1 (in another format).", section(text_of(build(docs)), "D2"))
        pdf["blocks"].append({"type": "para", "text": "Borehole Depth BH1 1.5 BH2 3.0 BH3 4.5", "level": 0})
        d2 = section(text_of(build(docs)), "D2")
        self.assertRegex(d2, r"The same document as D1 in another format \(\d\d% of the text reads the same")


class DrawingsAndOtherFilesTests(unittest.TestCase):
    def test_drawings_index(self):
        dwg = doctext("pdf", [("page", "", 1), ("para", "GENERAL NOTES"), ("para", "1. ALL DIMENSIONS IN MM."),
                              ("row", "DRAWING TITLE | DEPOT SLAB SETOUT PLAN", 0),
                              ("row", "REV | H", 1), ("para", "FOR CONSTRUCTION")], pages=1, drawing=True)
        report = doctext("docx", [("para", "See the layout drawing.")])
        result = build([item("D1", "Report.docx", report),
                        item("D2", "623.0001-ST-1200 Layout [H].pdf", dwg,
                             [email_src("2025-02-20T10:00:00+10:00", alias="EC.KP", name="Kim Park",
                                        email="kim.park@example-consulting.com")])])
        text = text_of(result)
        self.assertNotIn("## D2 ", text)
        # (the revision is in the file name, so it is not repeated)
        self.assertIn("## Drawings\nD2 623.0001-ST-1200 Layout [H].pdf (1 sheet, 25-02-20 email EC.KP) - "
                      "title: DEPOT SLAB SETOUT PLAN, for construction", text)
        # A revision only the title block shows is added.
        plain = text_of(build([item("D2", "623.0001-ST-1200 Layout.pdf", dwg)]))
        self.assertIn("D2 623.0001-ST-1200 Layout.pdf (1 sheet, 25-03-04 email RC.SB) - title: DEPOT SLAB "
                      "SETOUT PLAN, rev H, for construction", plain)
        self.assertEqual(result["stats"]["doc_drawings"], 1)
        self.assertIn('"## Drawings"', text)
        self.assertIn("KP=Kim Park", text)
        light = text_of(build([item("D2", "623.0001-ST-1200 Layout [H].pdf", dwg)], squeeze="light"))
        self.assertIn("| notes: ", light)
        self.assertIn("ALL DIMENSIONS IN MM", light)

    def test_drawing_notes_leave_out_grid_lines(self):
        dwg = doctext("pdf", [("page", "", 1), ("para", "8000 3 8000 4 8000 5 A B C D E F F1 F2 F3"),
                              ("para", "1. FOOTINGS DESIGNED FOR 120 kPa."), ("para", "TITLE: FOOTING PLAN")],
                      pages=1, drawing=True)
        light = text_of(build([item("D2", "ST-1202 [D].pdf", dwg)], squeeze="light"))
        self.assertIn("| notes: ", light)
        self.assertIn("FOOTINGS DESIGNED FOR 120 kPa", light)
        self.assertNotIn("8000 3", light)

    def test_title_block(self):
        dwg = doctext("pdf", [("para", "TITLE"), ("para", "CULVERT HEADWALL DETAILS"), ("para", "SCALE 1:50"),
                              ("para", "REVISION"), ("para", "C")], drawing=True)
        self.assertEqual(docdigest.title_block(dwg, "SK01.pdf"), ("CULVERT HEADWALL DETAILS", "C", ""))
        self.assertEqual(docdigest.title_block(dwg, "SK01 Rev D.pdf")[1], "D")

    def test_other_files(self):
        docs = [item("D1", "Report.docx", doctext("docx", [("para", "Some text.")])),
                item("", "Site plan.dwg", doctext("other", status="unsupported", note="CAD drawing, not read"),
                     size=2202009),
                item("", "Secret.pdf", doctext("pdf", status="protected", note="password-protected"),
                     [file_src("H:\\Jobs\\Riverside\\04 Reports\\Old\\Secret.pdf")]),
                item("", "Scan.pdf", doctext("pdf", status="no_text"), size=500)]
        result = build(docs)
        text = text_of(result)
        self.assertIn("## Other files\nSite plan.dwg (2.1 MB; 25-03-04 email RC.SB; CAD drawing, not read)\n"
                      "Secret.pdf (12 KB; documents folder\\Old; password-protected)\n"
                      "Scan.pdf (500 bytes; 25-03-04 email RC.SB; no text (scanned or image-only))", text)
        self.assertEqual(result["stats"]["doc_other"], 3)
        self.assertEqual(result["stats"]["doc_failed"], 2)
        self.assertIn("1 document, 3 other files", text)

    def test_nothing_useful_no_parts(self):
        docs = [item("", "Site plan.dwg", doctext("other", status="unsupported", note="CAD drawing, not read"))]
        result = build(docs)
        self.assertEqual(result["parts"], [])
        self.assertEqual(result["stats"]["doc_other"], 1)
        self.assertEqual(build([])["parts"], [])

    def test_zip_members(self):
        inner = doctext("docx", [("heading", "Site instruction 14", 1),
                                 ("para", "Excavations deeper than 1.5 m require shoring.")])
        members = [{"type": "member", "text": "SI-14/Site instruction.docx", "level": 0, "size": 8248,
                    "doc": inner},
                   {"type": "member", "text": "Locked.pdf", "level": 0, "size": 100,
                    "doc": doctext("pdf", status="protected", note="encrypted inside the zip")},
                   {"type": "member", "text": "dwg/ST-1200 [H].pdf", "level": 0, "size": 100,
                    "doc": doctext("pdf", [("para", "TITLE: GENERAL ARRANGEMENT")], pages=1, drawing=True)}]
        for n in range(5):
            members.append({"type": "member", "text": "photos/IMG_%04d.jpg" % n, "level": 0, "size": 103})
        members.append({"type": "para", "text": "(+3 more files)", "level": 0})
        zipdoc = doctext("zip", members)
        text = text_of(build([item("D5", "Transmittal.zip", zipdoc)]))
        self.assertIn("## D5 Transmittal.zip (zip, 11 files)", text)
        self.assertIn("Files: SI-14/Site instruction.docx (below), Locked.pdf (encrypted inside the zip), "
                      "dwg/ST-1200 [H].pdf (drawing, see Drawings), photos/ 5 photos IMG_0000,0001,0002,0003,0004, "
                      "(+3 more files)", text)
        self.assertIn("## D5 > SI-14/Site instruction.docx (Word)\n# Site instruction 14\n"
                      "Excavations deeper than 1.5 m require shoring.", text)
        self.assertIn("## Drawings\nD5 > dwg/ST-1200 [H].pdf (1 sheet) - title: GENERAL ARRANGEMENT", text)
        self.assertIn('"## D30 > name"', text)

    def test_zip_of_drawings_in_one_folder(self):
        def drawing(title):
            return doctext("pdf", [("para", "TITLE: " + title)], pages=1, drawing=True)

        members = [{"type": "member", "text": "IFC 2025-02-14/TR-031 Transmittal.pdf", "level": 0, "size": 900,
                    "doc": doctext("pdf", [("para", "Drawings issued for construction.")], pages=1)}]
        for n, title in enumerate(("COVER SHEET", "GENERAL NOTES", "FOOTING PLAN")):
            members.append({"type": "member", "text": "IFC 2025-02-14/ST-10%d [B] %s.pdf" % (n, title),
                            "level": 0, "size": 900, "doc": drawing(title)})
        text = text_of(build([item("D14", "IFC drawings.zip", doctext("zip", members))]))
        # The folder that holds the whole zip is named once; its drawings are counted.
        self.assertIn("Files (in IFC 2025-02-14/): TR-031 Transmittal.pdf (below), 3 drawings (see Drawings)\n"
                      "## D14 > TR-031 Transmittal.pdf (PDF, 1 page)", text)
        self.assertIn("## Drawings\nD14 > ST-100 [B] COVER SHEET.pdf (1 sheet)\n"
                      "D14 > ST-101 [B] GENERAL NOTES.pdf (1 sheet)\n", text)
        self.assertEqual(docdigest._zip_folder(["a/b/x.pdf", "a/b/c/y.pdf"]), "a/b/")
        self.assertEqual(docdigest._zip_folder(["a/x.pdf", "y.pdf"]), "")

    def test_zip_member_identical_to_a_listed_document_is_not_counted_again(self):
        plan = doctext("pdf", [("para", "TITLE: FOOTING PLAN")], pages=1, drawing=True)
        memo = doctext("docx", [("para", "Footings to bear on weathered rock, 300 kPa allowable.")])
        members = [{"type": "member", "text": "IFC/ST-1202 [C] FOOTING PLAN.pdf", "level": 0, "size": 900,
                    "doc": plan, "sha1": "plan-bytes"},
                   {"type": "member", "text": "IFC/ST-1203 [B] ROOF PLAN.pdf", "level": 0, "size": 900,
                    "doc": doctext("pdf", [("para", "TITLE: ROOF PLAN")], pages=1, drawing=True),
                    "sha1": "roof-bytes"},
                   {"type": "member", "text": "IFC/Memo.docx", "level": 0, "size": 900, "doc": memo,
                    "sha1": "memo-bytes"}]
        docs = [item("D3", "ST-1202 [C] FOOTING PLAN.pdf", plan), item("D4", "Memo.docx", memo),
                item("D5", "IFC.zip", doctext("zip", members))]
        docs[0]["sha1"], docs[1]["sha1"] = "plan-bytes", "memo-bytes"
        result = build(docs)
        text = text_of(result)
        self.assertIn("D5 > ST-1202 [C] FOOTING PLAN.pdf (same as D3)\n", text)
        self.assertIn("D5 > ST-1203 [B] ROOF PLAN.pdf (1 sheet)", text)
        self.assertIn("## D5 > Memo.docx (Word)\nSame as D4.", text)
        self.assertEqual(text.count("300 kPa allowable"), 1)
        # Two drawings, not three: the copy in the zip is not counted again.
        self.assertEqual(result["stats"]["doc_drawings"], 2)
        self.assertIn("2 drawings", text.split("\n## ", 1)[0])


class SourcesAndHeaderTests(unittest.TestCase):
    def test_sources_line(self):
        doc = doctext("pdf", [("page", "", 1), ("para", "Text of the report.")], pages=1)
        sources = [email_src("2025-03-04T09:00:00+10:00", subject="RE: Geotech report for the depot"),
                   email_src("2025-03-06T09:00:00+10:00", alias="EC.AC", name="Alex Chen",
                             email="alex.chen@example-consulting.com"),
                   email_src("2025-03-09T09:00:00+10:00"),
                   file_src("H:\\Jobs\\Riverside\\04 Reports\\Geotech\\Geotech report.pdf"),
                   file_src("H:\\Jobs\\Riverside\\04 Reports\\Geotech report.pdf")]
        result = build([item("D12", "Geotech report.pdf", doc, sources)])
        text = text_of(result)
        self.assertIn('## D12 Geotech report.pdf (PDF, 1 page)\nFrom: 25-03-04 email RC.SB "Geotech report for the '
                      'depot" (+2 more emails); also documents folder (modified 25-04-01) (+1 more copy)\n'
                      "Text of the report.", text)
        self.assertEqual(result["stats"]["doc_multi_email"], 1)
        self.assertIn("1 document (1 attached to several emails)", text)
        loose = text_of(build([item("D1", "Loose.pdf", doc, [file_src("H:\\Other\\Place\\Loose.pdf", "")])]))
        self.assertIn("From: H:\\Other\\Place\n", loose)

    def test_header(self):
        doc = doctext("docx", [("para", "Text.")])
        result = build([item("D1", "A.docx", doc, [email_src("2024-08-22T09:00:00+10:00")]),
                        item("D2", "B.docx", doctext("docx", [("para", "Other text.")]),
                             [file_src(mtime="2025-11-30T08:00:00+10:00")])],
                       focus_keywords="pump, culvert", date_from="2024-01-01", date_to="2025-12-31")
        lines = result["parts"][0]["text"].split("\n")
        self.assertEqual(lines[0], "SQUISH DOCUMENTS DIGEST | Riverside Depot")
        self.assertEqual(lines[1], "Covers documents from 2024-08-22 to 2025-11-30 | 2 documents")
        self.assertEqual(lines[2], "Source: %s | squeeze: standard | made 2026-10-06 14:05" % SOURCE)
        self.assertTrue(lines[3].startswith("Focus keywords: pump, culvert"))
        self.assertEqual(lines[4], "Dates: only documents attached to emails dated 2024-01-01 to 2025-12-31; "
                                   "files in the documents folder are included whatever their date")
        self.assertTrue(lines[5].startswith("How to read:"))
        self.assertEqual(result["parts"][0]["first_date"], "2024-08-22")
        self.assertEqual(result["parts"][0]["last_date"], "2025-11-30")
        self.assertEqual(result["parts"][0]["documents"], 2)
        self.assertEqual(result["stats"]["documents"], 2)
        self.assertEqual(result["stats"]["output_chars"], len(result["parts"][0]["text"]))
        self.assertNotIn("Changes from D7", result["parts"][0]["text"])     # explained only when used
        self.assertNotIn("Drawings", result["parts"][0]["text"])

    def test_title_property(self):
        doc = doctext("pdf", [("page", "", 1), ("para", "Text.")], pages=1,
                      title="Geotechnical Investigation Report")
        text = text_of(build([item("D1", "623-RPT-001.pdf", doc)]))
        self.assertIn('## D1 623-RPT-001.pdf (PDF, 1 page) "Geotechnical Investigation Report"', text)
        doc["title"] = "Microsoft Word - 623-RPT-001.docx"
        self.assertIn("## D1 623-RPT-001.pdf (PDF, 1 page)\n", text_of(build([item("D1", "623-RPT-001.pdf", doc)])))

    def test_fallback_aliases_and_legend(self):
        doc = doctext("docx", [("para", "Text.")])
        docs = [item("D1", "A.docx", doc, [email_src(alias="", name="Sam Brown")]),
                item("D2", "B.docx", doctext("docx", [("para", "More.")]),
                     [email_src(alias="", name="Brown, Sam")]),
                item("D3", "C.docx", doctext("docx", [("para", "Third.")]),
                     [email_src(alias="", name="Pat Lee", email="pat@acmepumps.com.au")])]
        text = text_of(build(docs))
        self.assertIn('From: 25-03-04 email RC.SB "Geotech report"', text)
        self.assertIn("email ACMEPU.PL", text)
        self.assertIn("People (ORG.Initials, as in the email digest):\n  RC = riverside.example: SB=Sam Brown\n"
                      "  ACMEPU = acmepumps.com.au: PL=Pat Lee", text)


class PartTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(digest.PART_SIZES["small"], {"chars": 6000})
        patcher.start()
        self.addCleanup(patcher.stop)

    def docs(self, n=8):
        out = []
        for k in range(n):
            alias, name, email = (("RC.SB", "Sam Brown", "sam.brown@riverside.example") if k % 2 == 0 else
                                  ("EC.AC", "Alex Chen", "alex.chen@example-consulting.com"))
            doc = doctext("docx", [("heading", "Memo %d" % k, 1), ("para", prose(8, k))])
            out.append(item("D%d" % (k + 1), "Memo %d.docx" % k, doc,
                            [email_src("2025-03-%02dT09:00:00+10:00" % (k + 1), alias=alias, name=name,
                                       email=email)]))
        return out

    def test_parts_stand_alone_and_documents_are_not_split(self):
        result = build(self.docs(), part_size="small")
        parts = result["parts"]
        self.assertGreater(len(parts), 1)
        for n, part in enumerate(parts, 1):
            text = part["text"]
            self.assertLessEqual(len(text), 6000)
            self.assertTrue(text.startswith("SQUISH DOCUMENTS DIGEST | Riverside Depot | part %d of %d\n"
                                            % (n, len(parts))))
            self.assertIn("How to read:", text)
            self.assertIn("(this part: ", text)
            for alias, who in (("RC.SB", "SB=Sam Brown"), ("EC.AC", "AC=Alex Chen")):
                self.assertEqual(alias in text.split("People (")[1], who in text)   # legend = aliases used
            self.assertNotIn("(continued)", text)
        for k in range(8):
            self.assertEqual(sum(1 for p in parts if "## D%d Memo" % (k + 1) in p["text"]), 1)
        self.assertEqual([p["first_date"] for p in parts], sorted(p["first_date"] for p in parts))
        self.assertEqual(sum(p["documents"] for p in parts), 8)

    def test_oversize_document_continues(self):
        big = doctext("docx", [("heading", "Long memo", 1)] +
                      [("heading", "Part %d" % n, 1) for n in range(1)] +
                      [("para", prose(4, n) + " Item %d costs $%d,000." % (n, n)) for n in range(60)])
        docs = self.docs(2) + [item("D9", "Long.docx", big)]
        result = build(docs, part_size="small", squeeze="light")
        texts = [p["text"] for p in result["parts"]]
        self.assertGreater(len(texts), 2)
        self.assertTrue(any("## D9 Long.docx (Word)" in t for t in texts))
        self.assertTrue(any("## D9 Long.docx (continued)" in t for t in texts))
        for t in texts:
            self.assertLessEqual(len(t), 6000)

    def test_lists_continue_across_parts(self):
        docs = [item("D1", "A.docx", doctext("docx", [("para", "Text.")]))]
        for n in range(150):
            docs.append(item("", "Old drawing %03d with a long name.dwg" % n,
                             doctext("other", status="unsupported", note="CAD drawing, not read")))
        texts = [p["text"] for p in build(docs, part_size="small")["parts"]]
        self.assertGreater(len(texts), 1)
        self.assertIn("## Other files\n", texts[0] if "## Other files" in texts[0] else texts[1])
        self.assertTrue(any("## Other files (continued)\n" in t for t in texts))
        self.assertEqual(sum(t.count(".dwg (") for t in texts), 150)

    def test_deterministic(self):
        docs = self.docs()
        a = text_of(build(docs, part_size="small"))
        b = text_of(build(json.loads(json.dumps(docs)), part_size="small"))
        self.assertEqual(a, b)
        code = ("import json, sys\nfrom datetime import datetime\nsys.path.insert(0, sys.argv[1])\n"
                "from squish_app import docdigest\n"
                "docs = json.loads(sys.stdin.read())\n"
                "r = docdigest.build_documents_digest(docs, {'name': 'X', 'org_codes': 'riverside.example=RC'},"
                " 'H:\\\\x', datetime(2026, 1, 2, 3, 4))\n"
                "sys.stdout.write('\\n'.join(p['text'] for p in r['parts']))\n")
        for d in docs:
            d["sources"][0]["sender_alias"] = ""      # aliases worked out here
        outs = set()
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for seed in ("1", "2", "3"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            out = subprocess.run([sys.executable, "-c", code, here], input=json.dumps(docs), env=env,
                                 stdout=subprocess.PIPE, universal_newlines=True, timeout=120)
            outs.add(out.stdout)
        self.assertEqual(len(outs), 1)
        self.assertIn("## D1 ", outs.pop())


# --------------------------------------------------------------------------
# PDF pages: headers and footers, records one per page, paragraphs across pages

def kept_texts(doc):
    """The texts of a document's paragraphs after boilerplate is removed."""
    return [p["text"] for p in docdigest._paragraphs(doc)]


class PageFurnitureTests(unittest.TestCase):
    def test_record_pages_keep_their_labels_and_results(self):
        blocks = []
        for n in range(1, 11):
            blocks += [("page", "", n), ("para", "Acme Civil | QA records | Footings and slab"),
                       ("para", "Page %d of 10" % n),
                       ("para", "Lot %03d - Pad footing F%d - Inspection and test record" % (n, n)),
                       ("para", "ITP hold point released after inspection. " + prose(3, n)),
                       ("para", "Cylinders taken: 3 7 day result: %d.3 MPa" % (20 + n)),
                       ("para", "Attached: scanned delivery docket")]
        joined = "\n".join(kept_texts(doctext("pdf", blocks, pages=10)))
        for n in range(1, 11):
            self.assertIn("Lot %03d - Pad footing F%d - Inspection and test record" % (n, n), joined)
            self.assertIn("7 day result: %d.3 MPa" % (20 + n), joined)
        self.assertNotIn("QA records", joined)
        self.assertNotIn("Page 3 of 10", joined)
        self.assertNotIn("delivery docket", joined)

    def test_table_rows_do_not_run_into_the_next_row(self):
        rows = ["BL-LC-5508-%d Pad footing F4 at grid C4 cast 08/04/2025 tested at 28 days %d.2 MPa"
                % (n, 39 + n) for n in range(1, 4)]
        doc = doctext("pdf", [("page", "", 1)] + [("para", r) for r in rows] +
                      [("para", "Results comply with the specified strength of 32 MPa at 28 days and the "
                                "slump was within tolerance on every load delivered")], pages=1)
        texts = kept_texts(doc)
        for r in rows:
            self.assertIn(r, texts)

    def test_certificate_numbers_kept(self):
        blocks = []
        for n in range(1, 7):
            blocks += [("page", "", n), ("heading", "Bedrock Labs - Concrete test certificates", 1),
                       ("para", "Test certificate BL-LC-550%d" % n),
                       ("row", "Cylinder | 7 day (MPa) | 28 day (MPa)", 0),
                       ("row", "C%d-1 | %d.5 | %d.0" % (n, 20 + n, 30 + n), 1),
                       ("para", "Accredited for compliance with ISO/IEC 17025 - Testing. NATA accredited laboratory."),
                       ("para", "Lab | NATA site 99999 | Page %d of 6" % n)]
        joined = "\n".join(kept_texts(doctext("pdf", blocks, pages=6)))
        for n in range(1, 7):
            self.assertIn("Test certificate BL-LC-550%d" % n, joined)
        self.assertNotIn("NATA site 99999", joined)

    def test_calc_sheet_headers_go_but_member_rows_stay(self):
        blocks = []
        for n in range(1, 9):
            blocks += [("page", "", n),
                       ("para", "Example Consulting | Job 24117 | Calc C-0%d | By SB 02/05/25" % n),
                       ("para", "Calc C-0%d | Sheet %d of 8" % (n, n)),
                       ("para", prose(3, 40 + n)),
                       ("para", "Report 623.0001-RPT-001 | Page %d of 8 | Rev C" % n),
                       ("para", "Member C-0%d-18 L=5.5 m N*=249 kN util=0.58 OK" % n)]
        joined = "\n".join(kept_texts(doctext("pdf", blocks, pages=8)))
        self.assertNotIn("Job 24117", joined)
        self.assertNotIn("Sheet 3 of 8", joined)
        self.assertNotIn("623.0001-RPT-001", joined)
        for n in range(1, 9):
            self.assertIn("Member C-0%d-18 L=5.5 m" % n, joined)

    def test_borehole_logs_keep_their_numbers(self):
        blocks = []
        for n in range(1, 9):
            blocks += [("page", "", n),
                       ("para", "Bedrock Labs | Geotechnical Investigation | BL-24117-R01 Rev C"),
                       ("para", "Borehole BH%d" % n),
                       ("para", "Easting 3128%02d | Surface RL 15.%02d m AHD" % (n, 10 + 7 * n)),
                       ("para", prose(3, 60 + n)),
                       ("para", "Page %d of 8" % n)]
        doc = doctext("pdf", blocks, pages=8)
        joined = "\n".join(kept_texts(doc))
        light = text_of(build([item("D1", "Geotech.pdf", doc)], squeeze="light"))
        for n in range(1, 9):
            for text in (joined, light):
                self.assertRegex(text, r"Borehole BH%d\b" % n)
                self.assertIn("Surface RL 15.%02d m AHD" % (10 + 7 * n), text)
        self.assertNotIn("BL-24117-R01", joined)
        self.assertNotIn("Page 3 of 8", joined)

    def test_one_sample_per_page(self):
        results = [(3, 1.5, 32, 180), (5, 0.8, 41, 120), (7, 2.2, 18, 250), (9, 1.0, 55, 90),
                   (11, 3.0, 24, 210), (12, 0.5, 38, 140)]
        blocks = []
        for n, (bh, depth, pi, ucs) in enumerate(results, 1):
            blocks += [("page", "", n), ("para", "Bedrock Labs - Test certificate"),
                       ("para", "Sample from BH%d at %.1f m depth, received 3 March 2025." % (bh, depth)),
                       ("para", "Plasticity index %d %% and unconfined compressive strength %d kPa." % (pi, ucs)),
                       ("para", "Certificate %d of %d" % (n, len(results)))]
        text = text_of(build([item("D1", "Lab certificates.pdf", doctext("pdf", blocks, pages=6))],
                             squeeze="light"))
        self.assertNotIn("(no text left", text)
        for bh, depth, pi, ucs in results:
            self.assertIn("BH%d at %.1f m depth" % (bh, depth), text)
            self.assertIn("strength %d kPa" % ucs, text)
        self.assertNotIn("Certificate 3 of 6", text)

    def test_a_sentence_ended_by_a_dropped_footer_line_does_not_run_on(self):
        blocks = []
        for n in range(1, 5):
            blocks += [("page", "", n), ("heading", "BEDROCK LABS - TEST CERTIFICATES", 1),
                       ("para", "Test certificate BL-LC-550%d" % n),
                       ("para", "Cylinder C%d-1 Footing F%d 28 days 4%d.0 MPa. Accredited for compliance with "
                                "ISO/IEC 17025. This document shall not be reproduced except in" % (n, n, n)),
                       ("para", "full."), ("para", "Bedrock Labs | NATA site 99999 | Page %d of 4" % n)]
        texts = kept_texts(doctext("pdf", blocks, pages=4))
        for n in range(1, 5):
            self.assertIn("Test certificate BL-LC-550%d" % n, texts)

    def test_review_comments_stay_whole_and_do_not_hide_the_footer(self):
        comments = ["[comment: Check the bearing value with the geotechnical engineer before issue",
                    "[comment: Sam Brown: slab thickness to suit the forklift wheel loads",
                    "[comment: REVISE AND RESUBMIT]"]
        blocks = []
        for n in range(1, 5):
            blocks += [("page", "", n), ("para", "Section %d of the structural report " % n + prose(2, n))]
            if n == 2:      # (a line that does not end a sentence, then the footer, then the comments)
                blocks += [("para", prose(2, 20)), ("para", prose(2, 21)),
                           ("para", "The slab thickness was confirmed on site by the contractor and the engineer")]
            blocks += [("para", "Riverside Depot | Structural report | Page %d of 4" % n)]
            if n == 2:
                blocks += [("para", c) for c in comments]
        texts = kept_texts(doctext("pdf", blocks, pages=4))
        self.assertFalse([t for t in texts if "Structural report |" in t], texts)
        for c in comments:
            self.assertIn(c, texts)
        self.assertIn("The slab thickness was confirmed on site by the contractor and the engineer", texts)

    def test_footer_with_a_page_number_offset_from_the_page(self):
        blocks = [("page", "", 1), ("para", "Riverside Depot - Geotechnical report (cover)")]
        for page in range(2, 10):
            lead = "Section %s describes this part of the site. " % "ABCDEFGH"[page - 2]
            blocks += [("page", "", page), ("para", lead + prose(2, page)),
                       ("para", "Riverside Depot | 623.0001-RPT-001 | %d" % (page - 1)),
                       ("para", "Uncontrolled when printed")]
        joined = "\n".join(kept_texts(doctext("pdf", blocks, pages=9)))
        self.assertNotIn("623.0001-RPT-001", joined)
        self.assertIn("Section C describes", joined)

    def test_a_record_label_on_the_first_line_of_each_page_is_kept(self):
        blocks = []
        for n in range(1, 9):
            blocks += [("page", "", n), ("para", "Lot %03d - Pad footing F%d - Inspection and test record" % (n, n)),
                       ("para", prose(3, 80 + n)), ("para", "Page %d of 8" % n)]
        joined = "\n".join(kept_texts(doctext("pdf", blocks, pages=8)))
        for n in range(1, 9):
            self.assertIn("Lot %03d - Pad footing F%d - Inspection and test record" % (n, n), joined)

    def test_records_do_not_run_on_across_pages(self):
        kinds = ["Pad footing F", "Ground beam GB", "Pile cap PC", "Slab bay S"]
        blocks = []
        for n in range(1, 71):
            blocks += [("page", "", n),
                       ("para", "Lot %03d - %s%d - Inspection and test record" % (n, kinds[n % 4], n)),
                       ("para", "Concrete placed by pump in one pour with vibration and curing as per the ITP; "
                                "delivery dockets checked against the mix design and the slump, air content "
                                "and temperature recorded for each truck. Slump: %d mm Temperature: %d C"
                        % (80 + n % 20, 12 + n % 9))]
        doc = doctext("pdf", blocks, pages=70)
        for level, first_left_out in (("standard", 61), ("max", 16)):
            body = section(text_of(build([item("D1", "QA records.pdf", doc)], squeeze=level)), "D1")
            self.assertIn("(pages %d-70 not shown)" % first_left_out, body, level)
            for n in range(first_left_out, 71):
                self.assertNotIn("Lot %03d" % n, body, level)
            self.assertLess(max(len(line) for line in body.split("\n")), 1500, level)
        self.assertEqual([t.count("Concrete placed") for t in kept_texts(doc) if "Concrete placed" in t], [1] * 70)

    def test_a_sentence_carried_onto_the_next_page_is_joined(self):
        doc = doctext("pdf", [("page", "", 1),
                              ("para", "The contractor shall submit the concrete mix design for each element to the"),
                              ("page", "", 2), ("para", "Superintendent for review before the first pour."),
                              ("para", "Slump: 80 mm, air content 2.5%, Temperature: 15 C"),
                              ("page", "", 3), ("para", "Pour 2 was placed on the next day without issue.")],
                      pages=3)
        texts = kept_texts(doc)
        self.assertIn("The contractor shall submit the concrete mix design for each element to the "
                      "Superintendent for review before the first pour.", texts)
        self.assertIn("Pour 2 was placed on the next day without issue.", texts)


class ContentsAndClausesTests(unittest.TestCase):
    def test_table_rows_with_rising_numbers_are_not_a_contents_list(self):
        doc = doctext("pdf", [("page", "", 1), ("para", "Dynamic cone penetrometer test DCP3 at pad footing PF4."),
                              ("para", "Depth (mm) Blows per 100 mm"),
                              ("para", "0 - 100 2"), ("para", "100 - 200 3"), ("para", "200 - 300 3"),
                              ("para", "300 - 400 6"), ("para", "400 - 500 9"), ("para", "500 - 600 14"),
                              ("page", "", 2), ("para", "Shop drawing member schedule"),
                              ("para", "MARK C1-01 460UB74.6 L=8150 HOLES 4/22 DIA WELD 6FW QTY 1"),
                              ("para", "MARK C1-02 460UB74.6 L=8187 HOLES 4/22 DIA WELD 6FW QTY 1"),
                              ("para", "MARK C1-03 460UB74.6 L=8203 HOLES 4/22 DIA WELD 6FW QTY 1"),
                              ("para", "Refusal was not met.")], pages=2)
        text = text_of(build([item("D1", "DCP test report.pdf", doc)], squeeze="light"))
        for row in ("0 - 100 2", "100 - 200 3", "400 - 500 9", "500 - 600 14", "L=8150", "L=8187", "L=8203"):
            self.assertIn(row, text)

    def test_numbered_contents_list_without_a_heading_still_goes(self):
        doc = doctext("pdf", [("page", "", 1), ("para", "Riverside Depot report"),
                              ("para", "1 Introduction 3"), ("para", "1.1 Scope 3"), ("para", "2 Site 4"),
                              ("para", "6.10 Acid sulfate soils 23"),
                              ("para", "Appendix E - Consolidation test results (BH14 to BH16) 45"),
                              ("para", "Figure 3 Borehole locations 46"),
                              ("page", "", 2), ("heading", "1 Introduction", 1), ("para", "The depot is to grow.")],
                      pages=50)
        texts = kept_texts(doc)
        self.assertNotIn("6.10 Acid sulfate soils 23", texts)
        self.assertNotIn("Figure 3 Borehole locations 46", texts)
        self.assertIn("The depot is to grow.", texts)

    def contract(self, clause, n=20, heading=None):
        blocks = [("heading", "Subcontract Agreement", 1)]
        for k in range(n):
            blocks.append(("para", "Clause %d. %s" % (k + 1, prose(2, 300 + k))))
            if k == n // 2:
                if heading:
                    blocks.append(("heading", heading, 1))
                blocks.append(("para", clause))
        return doctext("docx", blocks)

    def test_contract_clauses_are_not_boilerplate(self):
        for clause, heading in (
                ("The Subcontractor shall indemnify the Contractor against any claim by a third party arising "
                 "out of the Subcontract Works.", None),
                ("The Consultant's total liability under this Agreement is limited to the fees paid.",
                 "14 Limitation of liability"),
                ("Variations shall not be carried out without the prior written approval of the Client.", None)):
            text = text_of(build([item("D1", "Subcontract Agreement.docx", self.contract(clause, heading=heading))],
                                 squeeze="light"))
            self.assertIn(clause, text)
        fee = doctext("docx", [("heading", "Fee proposal", 1)] + [("para", prose(2, 400 + k)) for k in range(4)] +
                      [("para", "Variations shall not be carried out without the prior written approval of "
                                "the Client.")])
        self.assertIn("prior written approval of the Client",
                      text_of(build([item("D1", "Fee proposal.docx", fee)], squeeze="light")))

    def test_disclaimers_are_still_boilerplate(self):
        self.assertTrue(docdigest._boilerplate(
            "This report has been prepared for the exclusive use of the Client and must not be relied upon by "
            "any third party.", False, False))
        self.assertTrue(docdigest._boilerplate(
            "No part of this document may be reproduced without the prior written permission of the "
            "Consultant.", False, True))


# --------------------------------------------------------------------------
# What is kept: open register items, summaries, calc listings, repeated headings

_ANSWERS = ["Confirmed as shown on the drawings.", "Refer to response to RFI-%03d.",
            "Offset by 150 mm; no structural impact.", "Proceed as proposed by the Contractor.",
            "Provide additional N12 bars at 200 centres.", "Adopt the dimension on the architectural drawings.",
            "Hold - awaiting Council confirmation."]


def rfi_register(n_rows, open_rows):
    """An RFI register sheet: closed rows with dates and answers, open rows with blank answers."""
    start = datetime(2025, 2, 1)
    blocks = [{"type": "sheet", "text": "RFI Register", "level": 1, "rows": n_rows + 1},
              ("row", "RFI No. | Date raised | Subject / question | Response | Responded by | Date responded | "
                      "Status | Impact", 0)]
    for n in range(1, n_rows + 1):
        raised = (start + timedelta(days=2 * n)).strftime("%Y-%m-%d")
        if n in open_rows:
            tail = " | | | | Open | %s" % ("TBC" if n % 2 else "Nil")
        else:
            answer = _ANSWERS[n % len(_ANSWERS)]
            if "%03d" in answer:
                answer = answer % max(1, n - 3)
            tail = " | %s | %s | %s | Closed | %s" % (
                answer, ["Sam Brown (Riverside)", "Tom Hughes (Riverside)"][n % 2],
                (start + timedelta(days=2 * n + 5 + n % 9)).strftime("%Y-%m-%d"), ["Nil", "TBC", "Minor cost"][n % 3])
        blocks.append(("row", "RFI-%03d | %s | Question about grid line %d near pit SW%d%s"
                       % (n, raised, n, n % 17, tail), n))
    return doctext("xlsx", blocks, pages=1)


class SelectionTests(unittest.TestCase):
    def test_open_register_items_among_closed_ones_are_kept(self):
        open_rows = {23, 61, 74, 79}
        doc = rfi_register(80, open_rows)
        for level in ("standard", "max"):
            body = section(text_of(build([item("D1", "RFI Register.xlsx", doc)], squeeze=level)), "D1")
            shown = sorted(int(m) for m in re.findall(r"(?m)^RFI-(\d+) [^\n]*\| Open \|", body))
            self.assertEqual(shown, sorted(open_rows), level)

    def test_open_register_items_kept_in_changes(self):
        docs = [item("D1", "RFI Register 2025-06-30.xlsx", rfi_register(80, {23, 61, 74, 79})),
                item("D2", "RFI Register 2025-10-31.xlsx", rfi_register(118, {89, 112, 115, 116, 117, 118}))]
        for level in ("standard", "max"):
            d2 = section(text_of(build(docs, squeeze=level)), "D2")
            self.assertIn("Changes from D1:", d2)
            shown = sorted(int(m) for m in re.findall(r"(?m)^\+ RFI-(\d+) [^\n]*\| Open \|", d2))
            self.assertEqual(shown, [89, 112, 115, 116, 117, 118], level)
            # the items closed since: the new row, then the old cells that changed ('Open')
            for n in (23, 61, 74, 79):
                self.assertRegex(d2, r"(?m)^\+ RFI-%03d [^\n]*\| Closed \|[^\n]*\n- RFI-%03d \| \u2026 \|[^\n]*"
                                     r"\| Open\b" % (n, n), (level, n))

    def test_executive_summary_findings_kept_at_max(self):
        body = [
            "Footings shall be founded in stiff clay at least 0.8 m below finished level in accordance with "
            "AS 2870-2011 and AS 3600.",
            "Concrete in contact with the ground must have a minimum cover of 50 mm and exposure classification "
            "A2 to AS 3600 clause 4.3.",
            "Temporary batters should be no steeper than 1.5H:1V for depths up to 2 m and permanent batters no "
            "steeper than 2H:1V.",
            "Fill shall be compacted to a minimum dry density ratio of 98% Standard (AS 1289.5.1.1) within 2% of "
            "optimum moisture content.",
            "For earthquake design to AS 1170.4-2007 the site sub-soil class is Ce with a hazard factor Z = 0.09.",
            "A modulus of subgrade reaction of 25 kPa/mm may be adopted for slab design to AS 3600 for a 750 mm "
            "loaded area.",
            "Piles shall be designed to AS 2159-2009 with a geotechnical strength reduction factor not exceeding "
            "0.50.",
            "Retaining walls must be designed for an at-rest coefficient K0 of 0.55 and a bulk unit weight of "
            "19 kN/m3 (AS 4678-2002).",
            "Excavations deeper than 1.5 m that personnel must enter shall be shored in accordance with the WHS "
            "Regulation 2017 clause 306.",
            "Level 1 inspection and testing to AS 3798-2007 is required for structural fill exceeding 0.5 m in "
            "thickness.",
            "Stockpiles must not be placed within 3 m of open excavations deeper than 1.2 m (AS 4678 section 5).",
            "Pavement subgrade shall be proof rolled with a 12 tonne roller and a design CBR of 3% adopted per "
            "Austroads AGPT02.",
            "Soil pH of 5.4 to 6.8 and chloride below 300 mg/kg give an exposure classification of Mild to "
            "AS 2159-2009 Table 6.4.2.",
            "Footings on residual clay may be designed for 250 kPa provided the base is inspected per AS 1726-2017 "
            "clause 6.",
            "Groundwater inflows into excavations below 2 m must be managed by sump pumping to AS/NZS 3500.3 "
            "requirements.",
            "Blinding of at least 50 mm of N20 concrete shall be placed within 4 hours of excavation per "
            "specification clause 2.4.",
        ]
        blocks = [("para", "GEOTECHNICAL INVESTIGATION REPORT"), ("para", "Proposed Depot Upgrade"),
                  ("para", "Riverside Depot, 40 Quarry Road, Riverside"), ("para", "Prepared for: Riverside Council"),
                  ("para", "Prepared by: Example Labs Pty Ltd"), ("para", "Report No: EL-001 Rev A"),
                  ("para", "Date: 15 November 2024"), ("para", "Document control"),
                  ("row", "Revision | Date | Description | Prepared | Reviewed", 0),
                  ("row", "A | 15/11/2024 | Draft for comment | S. Brown | T. Hughes", 1),
                  ("para", "Distribution: Riverside Council (1 electronic copy); Example Consulting Pty Ltd "
                           "(1 electronic copy); Example Labs file copy."),
                  ("heading", "Executive summary", 1),
                  ("para", "Example Labs was commissioned by Riverside Council to undertake a geotechnical "
                           "investigation for the proposed upgrade of the Riverside Depot, comprising a new "
                           "workshop, a washbay canopy and new hardstand pavements. The structural engineer for "
                           "the project is Example Consulting Pty Ltd."),
                  ("item", "\u2022 Uncontrolled fill up to 1.85 m thick is present near the former fuel bay (BH7).", 0),
                  ("item", "\u2022 Groundwater was encountered at 2.35 m depth in BH3 during drilling.", 0),
                  ("item", "\u2022 The site is classified as Class M in accordance with AS 2870.", 0),
                  ("item", "\u2022 Pad footings may be designed for an allowable bearing pressure of 150 kPa.", 0),
                  ("heading", "1 Introduction", 1),
                  ("para", "Riverside Council proposes to upgrade the existing works depot, which comprises a brick "
                           "workshop, an administration building and open storage bays."),
                  ("heading", "6 Discussion and recommendations", 1)]
        for n, sentence in enumerate(body):
            if n % 2 == 0:
                blocks.append(("heading", "6.%d Item %s" % (n // 2 + 1, "ABCDEFGHIJ"[n // 2]), 2))
            blocks.append(("para", sentence))
        lines, _paras = docdigest.condense(doctext("docx", blocks), docdigest.DOC_CAPS["max"])
        text = "\n".join(lines)
        for needle in ("1.85 m", "2.35 m", "Class M", "150 kPa"):
            self.assertIn(needle, text)
        self.assertEqual(docdigest._section_weight("1 Executive Summary"), 2)
        self.assertEqual(docdigest._section_weight("SUMMARY OF FINDINGS"), 2)
        self.assertEqual(docdigest._section_weight("Conclusions and recommendations"), 2)
        self.assertEqual(docdigest._section_weight("Cost summary"), 1)
        self.assertEqual(docdigest._section_weight("6 Discussion and recommendations"), 1)
        # a table under a summary heading counts only the usual extra
        table = doctext("docx", [("heading", "Summary of results", 1),
                                 ("row", "BH | Depth | UCS", 0), ("row", "BH1 | 1.5 m | 180 kPa (AS 1289.3.3.1)", 1)])
        pieces = docdigest._pieces(docdigest._paragraphs(table), 400)
        rows = [p for p in pieces if p["kind"] == "row"]
        self.assertTrue(rows)
        for p in rows:
            self.assertLessEqual(p["score"], docdigest.ROW_SCORE_MAX + docdigest.SECTION_BONUS)

    def test_checks_between_member_listings_are_kept(self):
        blocks = [("page", "", 1)]
        checks = []
        for s in range(1, 9):
            blocks.append(("heading", "C-%02d CHECK %d" % (s, s), 1))
            fact = "Total %d kN per pile." % (600 + s)
            checks.append(fact)
            blocks.append(("para", "Pile capacity from the geotechnical report. " + fact))
            blocks.append(("heading", "C-%02d CHECK %d (CONT.)" % (s, s), 1))
            blocks.append(("para", " ".join("Member C-%02d-%02d L=%d.%d m N*=%d kN M*=%d kNm util=0.%02d OK"
                                            % (s, n, 2 + n % 6, n % 10, 20 + 3 * n, 10 + 2 * n, 30 + n % 60)
                                            for n in range(1, 19))))
        doc = doctext("pdf", blocks, pages=1)
        text = text_of(build([item("D1", "Calcs.pdf", doc)], squeeze="standard"))
        for fact in checks:
            self.assertIn(fact, text)
        self.assertNotIn("(CONT.)", text)
        # the odd ends of a listing cut into pieces count as little as the rest of it
        pieces = [p for p in docdigest._pieces(docdigest._paragraphs(doc), 125) if "kNm" in p["text"]]
        self.assertGreater(len(pieces), 50)
        for p in pieces:
            self.assertLess(p["score"], docdigest.HARD_BONUS, p["text"])

    def test_continued_heading_dropped(self):
        doc = doctext("pdf", [("page", "", 1), ("heading", "C-01 DESIGN BASIS", 1),
                              ("para", "Importance level 2, design working life 50 years."),
                              ("page", "", 2), ("heading", "C-01 DESIGN BASIS (CONT.)", 1),
                              ("para", "Wind region A2, terrain category 2.5.")], pages=2)
        body = section(text_of(build([item("D1", "Calcs.pdf", doc)])), "D1")
        self.assertNotIn("CONT.", body)
        self.assertIn("# C-01 DESIGN BASIS\nImportance level 2, design working life 50 years.", body)
        self.assertIn("Wind region A2, terrain category 2.5.", body)

    def boreholes(self, extra=""):
        blocks = [("heading", "Borehole logs", 1), ("para", "The logs follow, one borehole to a page.")]
        for n in range(1, 13):
            text = prose(30, 500 + n)
            if n == 3:
                text += " Groundwater was encountered at 2.35 m." + extra
            blocks += [("heading", "Borehole BH%d" % n, 2), ("para", text)]
        return doctext("docx", blocks)

    def test_headings_that_differ_only_in_their_numbers_show_once(self):
        body = section(text_of(build([item("D1", "Logs.docx", self.boreholes())], squeeze="standard")), "D1")
        self.assertIn("# Borehole BH1 (+11 more like it)", body)
        self.assertNotIn("# Borehole BH5", body)
        self.assertRegex(body, r"(?m)^# Borehole BH3\b.*\n.*Groundwater was encountered at 2\.35 m\.")

    def test_changes_do_not_fold_headings(self):
        docs = [item("D1", "Logs Rev A.docx", self.boreholes()),
                item("D2", "Logs Rev B.docx", self.boreholes(" Standing water level 2.10 m on 6 January 2025."),
                     [email_src("2025-04-10T09:00:00+10:00")])]
        d2 = section(text_of(build(docs)), "D2")
        self.assertIn("Changes from D1:", d2)
        self.assertIn("# Borehole BH3", d2)
        self.assertNotIn("more like it", d2)


# --------------------------------------------------------------------------
# Drawings, other files, versions and the options the engine passes

def drawing(*pages):
    """A drawing PDF; each argument is a sheet's lines (title block text as the PDF reads it)."""
    blocks = []
    for n, lines in enumerate(pages, 1):
        blocks.append(("page", "", n))
        blocks += [("para", line) for line in lines]
    return doctext("pdf", blocks, pages=len(pages), drawing=True)


class DrawingLineTests(unittest.TestCase):
    def test_latest_revision_row_shown(self):
        dwg = drawing(["TITLE: FOOTING PLAN", "SCALE: 1:100", "A 12.02.25 PRELIMINARY ISSUE",
                       "B 03.03.25 ISSUED FOR TENDER", "C 10.03.25 ISSUED FOR CONSTRUCTION",
                       "D 29.04.25 FOOTINGS REVISED TO SUIT GEOTECH REV C", "STATUS: FOR CONSTRUCTION"])
        text = text_of(build([item("D1", "RD-ST-1202 [D] FOOTING PLAN.pdf", dwg)]))
        self.assertIn("D1 RD-ST-1202 [D] FOOTING PLAN.pdf (1 sheet, 25-03-04 email RC.SB) - "
                      "rev D 29.04.25 FOOTINGS REVISED TO SUIT GEOTECH REV C, for construction", text)
        self.assertEqual(docdigest._latest_revision(dwg, "B"),
                         ("B", "03.03.25", "ISSUED FOR TENDER", "B 03.03.25 ISSUED FOR TENDER"))

    def test_revision_row_that_only_repeats_the_status_adds_nothing(self):
        dwg = drawing(["TITLE: SLAB PLAN", "C 10.03.25 ISSUED FOR CONSTRUCTION", "STATUS: FOR CONSTRUCTION"])
        text = text_of(build([item("D1", "RD-ST-1220 [C] SLAB PLAN.pdf", dwg)]))
        self.assertIn("D1 RD-ST-1220 [C] SLAB PLAN.pdf (1 sheet, 25-03-04 email RC.SB) - for construction\n", text)

    def test_a_set_of_sheets_lists_each_sheet(self):
        sheets = [["TITLE: COVER SHEET AND DRAWING SCHEDULE", "DRAWING No. RD-ST-1001 REV B"],
                  ["TITLE: GENERAL NOTES", "DRAWING No. RD-ST-1002 REV B"],
                  ["TITLE: FOOTING PLAN", "DRAWING No. RD-ST-1202 REV D"]]
        text = text_of(build([item("D1", "RD Structural Drawings Rev C Set.pdf", drawing(*sheets))]))
        self.assertIn("D1 RD Structural Drawings Rev C Set.pdf (3 sheets, 25-03-04 email RC.SB) - sheets: "
                      "RD-ST-1001 B COVER SHEET AND DRAWING SCHEDULE; RD-ST-1002 B GENERAL NOTES; "
                      "RD-ST-1202 D FOOTING PLAN", text)

    def test_a_title_holding_the_word_sheet(self):
        dwg = doctext("pdf", [("para", "PROJECT: RIVERSIDE DEPOT TITLE: COVER SHEET AND DRAWING SCHEDULE SCALE: NTS")],
                      drawing=True)
        self.assertEqual(docdigest.title_block(dwg)[0], "COVER SHEET AND DRAWING SCHEDULE")

    def test_light_notes_leave_out_title_block_cells_and_repeated_notes(self):
        common = ("para", "1. REFER TO RD-ST-1002 FOR GENERAL NOTES. 2. SETOUT FROM GRID LINES ONLY.")
        docs = []
        for n, title in enumerate(("FOOTING PLAN", "SLAB PLAN", "ROOF PLAN")):
            dwg = doctext("pdf", [("page", "", 1), common, ("para", "SCALE: 1:100 DRAWN: LO"),
                                  ("para", "%d. %s NOTE ONLY ON THIS SHEET." % (3 + n, title)),
                                  ("para", "TITLE: " + title)], pages=1, drawing=True)
            docs.append(item("D%d" % (n + 1), "RD-ST-12%d0 [A] %s.pdf" % (n, title), dwg))
        text = text_of(build(docs, squeeze="light"))
        self.assertEqual(text.count("SETOUT FROM GRID LINES"), 1)
        self.assertNotIn("DRAWN: LO", text)
        for title in ("FOOTING PLAN", "SLAB PLAN", "ROOF PLAN"):
            self.assertIn("%s NOTE ONLY ON THIS SHEET" % title, text)


def loose(name, folder="Photos", size=1000, status="unsupported"):
    return item("", name, doctext("other", status=status, note=""),
                [file_src("H:\\Jobs\\Riverside\\04 Reports\\%s\\%s" % (folder, name))], size=size)


class OtherFilesGroupingTests(unittest.TestCase):
    def test_a_folder_of_many_loose_files_is_one_line(self):
        docs = [item("D1", "Report.docx", doctext("docx", [("para", "Some text.")]))]
        docs += [loose("IMG_%d.JPG" % n) for n in range(1000, 1600)]
        docs += [loose("Plan %02d.dwg" % n) for n in range(60)] + [loose("Plan %02d.bak" % n) for n in range(60)]
        docs += [loose("A.doc"), loose("B.doc"), loose("Locked.pdf", status="protected")]
        docs += [loose(n, folder="Misc") for n in ("Site plan.dwg", "Old spec.doc", "Notes.dat")]
        result = build(docs)
        text = text_of(result)
        self.assertIn("documents folder\\Photos: 600 photos IMG_1000-1599, 60 .bak, 60 .dwg, A.doc, B.doc "
                      "(705 KB; not read)", text)
        self.assertIn("Locked.pdf (1000 bytes; documents folder\\Photos; password-protected, not read)", text)
        for name in ("Site plan.dwg", "Old spec.doc", "Notes.dat"):
            self.assertIn("%s (1000 bytes; documents folder\\Misc; not read)" % name, text)
        self.assertEqual(result["stats"]["doc_other"], 726)
        self.assertIn("1 document, 726 other files", text)
        self.assertEqual(text, text_of(build(docs)))

    def test_photos_in_a_folder_share_a_line(self):
        docs = [item("D1", "Report.docx", doctext("docx", [("para", "Some text.")]))]
        docs += [loose("IMG_%d.jpg" % n, folder="Site Photos") for n in range(1001, 1021)]
        docs += [loose("DSC%02d.JPG" % n, folder="Site Photos") for n in (1, 2, 3)]
        docs += [loose("Crack at grid C4.jpg", folder="Site Photos"), loose("IMG_7.jpg", folder="Other")]
        text = text_of(build(docs))
        self.assertIn("documents folder\\Site Photos: 3 photos DSC01,02,03, 20 photos IMG_1001-1020, "
                      "Crack at grid C4.jpg (23 KB; not read)", text)
        self.assertIn("IMG_7.jpg (1000 bytes; documents folder\\Other; not read)", text)

    def test_saved_emails_in_a_folder_are_counted_together(self):
        docs = [item("D1", "Report.docx", doctext("docx", [("para", "Some text.")]))]
        docs += [loose("RE Footing F%d.msg" % n, folder="Correspondence") for n in range(6)]
        docs += [loose("Approval %d.eml" % n, folder="Correspondence") for n in range(3)]
        text = text_of(build(docs))
        self.assertIn("documents folder\\Correspondence: 9 saved emails (", text)

    def test_attachments_are_listed_one_by_one(self):
        docs = [item("D1", "Report.docx", doctext("docx", [("para", "Some text.")]))]
        docs += [item("", "IMG_%d.jpg" % n, doctext("other", status="unsupported", note="")) for n in range(10)]
        self.assertEqual(text_of(build(docs)).count("IMG_"), 10)


class VersionChangeTests(unittest.TestCase):
    def report(self, moved_to_end, signatories):
        clause = "Pad footings shall be founded on stiff clay with an allowable bearing pressure of 150 kPa."
        blocks = [("heading", "1 Introduction", 1), ("para", prose(6, 1)),
                  ("heading", "2 Site works", 1), ("para", prose(4, 2))]
        if not moved_to_end:
            blocks.append(("para", clause))
        blocks += [("heading", "3 Slab", 1), ("para", prose(6, 3)),
                   ("heading", "5 Footings", 1), ("para", prose(4, 4))]
        if moved_to_end:
            blocks.append(("para", clause))
        for n, who in enumerate(signatories, 1):
            blocks += [("heading", "Test certificate T-%03d" % n, 1),
                       ("para", "Cylinder strength %d MPa at 28 days for pour %d." % (30 + n, n)),
                       ("para", "Approved signatory: %s | Laboratory: Riverside site no. 99999" % who)]
        return doctext("docx", blocks)

    def test_moved_and_swapped_text_shows_both_sides(self):
        docs = [item("D1", "Geotech Report Rev B.docx", self.report(False, ["R. Singh", "L. Grant"])),
                item("D2", "Geotech Report Rev C.docx", self.report(True, ["L. Grant", "R. Singh"]),
                     [email_src("2025-04-10T09:00:00+10:00")])]
        d2 = section(text_of(build(docs)), "D2")
        self.assertRegex(d2, r"(?m)^- Pad footings shall be founded")
        self.assertRegex(d2, r"(?m)^\+ Pad footings shall be founded")
        self.assertRegex(d2, r"(?m)^# Test certificate T-002\n\+ Approved signatory: R\. Singh")
        self.assertRegex(d2, r"(?m)^- Approved signatory: L\. Grant")

    def programme(self, shift):
        blocks = [{"type": "sheet", "text": "Programme", "level": 1, "rows": 1501},
                  ("row", "ID | Task | Zone | Start | Finish | Days", 0)]
        for n in range(1, 1501):
            start = datetime(2025, 1, 6) + timedelta(days=n % 300 + shift)
            blocks.append(("row", "%d | Pour slab zone %d | Zone %d | %s | %s | %d"
                           % (n, n % 40, n % 12, start.strftime("%Y-%m-%d"),
                              (start + timedelta(days=5)).strftime("%Y-%m-%d"), 5 + shift), n))
        return doctext("xlsx", blocks, pages=1)

    def test_a_large_changed_register_is_compared_quickly(self):
        docs = [item("D1", "Programme Rev 5.xlsx", self.programme(0)),
                item("D2", "Programme Rev 6.xlsx", self.programme(7), [email_src("2025-04-10T09:00:00+10:00")])]
        started = time.time()
        text = text_of(build(docs))
        self.assertLess(time.time() - started, 20)
        self.assertIn("## D2 Programme Rev 6.xlsx", text)
        self.assertEqual(docdigest._words_ratio("a b c " * 1500, "a b d " * 1500), 2.0 / 3)
        old = [{"kind": "text", "text": "Load %d is %d kN on grid %d." % (n, n, n % 9), "sec": -1}
               for n in range(800)]
        new = [{"kind": "text", "text": "Load %d is %d kN on grid %d." % (n, n + 5, n % 9), "sec": -1}
               for n in range(800)]
        old_units, new_units = docdigest.version_units(old), docdigest.version_units(new)
        _sim, ops = docdigest.compare(old_units, new_units)
        paras = docdigest._diff_paras(old, new, old_units, new_units, ops)
        self.assertEqual(set(p["kind"] for p in paras), {"add", "del"})
        self.assertIn("Load 3 is 8 kN on grid 3.", [p["text"] for p in paras])


class EngineOptionTests(unittest.TestCase):
    def docs(self):
        return [item("D1", "Report.docx", long_report()), item("D2", "Memo.docx", long_report(seed=2))]

    def test_cancel_and_progress(self):
        cancel = threading.Event()
        calls = []
        result = docdigest.build_documents_digest(self.docs(), {"name": "X"}, cancel=cancel,
                                                  progress=lambda done, total: calls.append((done, total)))
        self.assertTrue(result["parts"])
        self.assertEqual(calls[0], (0, calls[0][1]))
        self.assertEqual(calls[-1][0], calls[-1][1])
        cancel.set()
        with self.assertRaises(digest.DigestCancelled):
            docdigest.build_documents_digest(self.docs(), {"name": "X"}, cancel=cancel)

        def cancel_midway(done, total):
            if done == 2:
                cancel.set()
        cancel.clear()
        with self.assertRaises(digest.DigestCancelled):
            docdigest.build_documents_digest(self.docs(), {"name": "X"}, cancel=cancel, progress=cancel_midway)

    def test_folders_not_read_are_said_in_the_header(self):
        gone = "H:\\Jobs\\Riverside\\04 Reports\\Old"
        result = docdigest.build_documents_digest(self.docs(), {"name": "X"}, source_label=SOURCE, now=NOW,
                                                  not_read_folders=[gone])
        header = text_of(result).split("\n## ", 1)[0]
        self.assertIn("Not included: documents in folders Squish could not open: " + gone, header)
        self.assertNotIn("Not included", text_of(build(self.docs())))

    def test_documents_folder_with_a_plus_in_its_name(self):
        folder = "H:\\Jobs\\Design + Construct\\04 Reports"
        label = "H:\\Jobs\\Design + Construct\\01 Emails (attachments) + " + folder
        docs = [item("D1", "A.docx", doctext("docx", [("para", "Text.")]),
                     [file_src(folder + "\\Superseded\\A.docx")]),
                item("D2", "B.docx", doctext("docx", [("para", "Other text.")]),
                     [file_src("H:\\Jobs\\Design\\B.docx")])]
        text = text_of(docdigest.build_documents_digest(docs, {"name": "X"}, source_label=label, now=NOW,
                                                        docs_folders=[folder]))
        self.assertIn("From: documents folder\\Superseded (modified 25-04-01)", text)
        self.assertIn("From: H:\\Jobs\\Design (modified 25-04-01)", text)
        self.assertIn("Source: " + label + " | ", text)


# --------------------------------------------------------------------------
# The email digest's side: '=D12' marks and the aliases

def rec(sender, subject, body, date, attachments=(), path=None, **extra):
    r = {"path": path or "C:\\mail\\%s.msg" % re.sub(r"\W", "", subject + date), "date": date,
         "sender_name": sender[0], "sender_email": sender[1], "to": [["Alex Chen", "alex.chen@example-consulting.com"]],
         "cc": [], "subject": subject, "body": body,
         "attachments": [{"name": a, "size": 1000, "inline": False} for a in attachments],
         "message_id": "", "in_reply_to": "", "conversation_topic": "", "item_class": "IPM.Note",
         "auto_reply": False, "reader": "eml"}
    r.update(extra)
    return r


SAM = ("Sam Brown", "sam.brown@riverside.example")
ALEX = ("Alex Chen", "alex.chen@example-consulting.com")


class EmailCrossReferenceTests(unittest.TestCase):
    def records(self):
        return [
            rec(SAM, "Geotech report", "Hi Alex, please find the report attached.", "2025-03-04T09:00:00+10:00",
                ["Geotech report.pdf", "IMG_0001.jpg"]),
            rec(ALEX, "RE: Geotech report", "Thanks Sam, the bearing pressure looks low.",
                "2025-03-05T09:00:00+10:00"),
            rec(SAM, "RE: Geotech report", "Updated report attached.", "2025-03-06T09:00:00+10:00",
                ["Geotech report.pdf"]),
            rec(SAM, "RE: Geotech report", "Resending the same file as before.", "2025-03-07T09:00:00+10:00",
                ["Geotech report.pdf"]),
        ]

    def project(self, **kw):
        p = {"name": "Riverside Depot", "squeeze": "standard", "part_size": "single", "org_codes": ORGS}
        p.update(kw)
        return p

    def test_no_change_without_doc_ids(self):
        records = self.records()
        plain = digest.build_digest(records, self.project(), source_label="H:\\x", now=NOW)
        none = digest.build_digest(records, self.project(), source_label="H:\\x", now=NOW, doc_ids=None)
        empty = digest.build_digest(records, self.project(), source_label="H:\\x", now=NOW, doc_ids={})
        self.assertEqual(text_of(plain), text_of(none))
        self.assertEqual(text_of(plain), text_of(empty))
        self.assertNotIn("=D", text_of(plain))

    def test_doc_id_marks(self):
        records = self.records()
        ids = {(records[0]["path"], 0): "D1", (records[2]["path"], 0): "D2", (records[3]["path"], 0): "D2"}
        for level in ("light", "standard", "max"):
            text = text_of(digest.build_digest(records, self.project(squeeze=level), source_label="H:\\x",
                                               now=NOW, doc_ids=ids))
            self.assertIn("[att: Geotech report.pdf =D1", text, level)
            self.assertIn('"name =D12" in [att: ...] = what the file says is in the documents digest', text)
            self.assertIn("Updated report attached. [att: Geotech report.pdf =D2]", text)   # new contents
            if level != "light":
                self.assertIn("Resending the same file as before. [att: 1 as above]", text)
            self.assertNotIn("IMG_0001.jpg =D", text)
        unmarked = text_of(digest.build_digest(records[1:2], self.project(), source_label="H:\\x", now=NOW,
                                               doc_ids=ids))
        self.assertNotIn("=D12", unmarked)        # no '=Dn' in this digest: no explanation

    def test_aliases_match_the_email_digest(self):
        records = self.records()
        copy = dict(records[0], path="C:\\mail\\copy.msg")       # Mail Manager filed it twice
        records.append(copy)
        records.append(rec(("Pat Lee", "pat@acmepumps.com.au"), "Pump quote", "Quote attached.",
                           "2025-03-08T09:00:00+10:00", ["Quote.pdf"]))
        project = self.project(focus_keywords="geotech")
        result = digest.build_digest(records, project, source_label="H:\\x", now=NOW)
        aliases = digest.alias_for_records(records, project)
        self.assertEqual(aliases, result["aliases"])
        self.assertEqual(aliases[records[0]["path"]], "RC.SB")
        self.assertEqual(aliases["C:\\mail\\copy.msg"], "RC.SB")
        self.assertEqual(aliases[records[1]["path"]], "EC.AC")
        self.assertEqual(aliases[records[-1]["path"]], "ACMEPU.PL")    # not in the digest (focus keywords)
        text = text_of(result)
        for line in text.split("\n"):
            m = re.match(r"^(?:\d\d-\d\d-\d\d )?\d\d:\d\d (\S+?)[>:]", line)
            if m:
                self.assertIn(m.group(1), ("RC.SB", "EC.AC"))



# --------------------------------------------------------------------------
# Register changes, rarity, report tables, clauses, version headings, drawings'
# revision rows, document furniture, contents lists, contact details, partly read
# documents, page furniture shapes and site instructions

def register(n_rows, open_rows, answer_of):
    """An RFI register sheet; answer_of(n) gives a closed row's response."""
    blocks = [{"type": "sheet", "text": "RFI Register", "level": 1, "rows": n_rows + 1},
              ("row", "RFI No. | Subject | Response | Responded by | Status", 0)]
    for n in range(1, n_rows + 1):
        if n in open_rows:
            tail = " | | | Open"
        else:
            tail = " | %s | %s | Closed" % (answer_of(n), ["Sam Brown", "Tom Hughes"][n % 2])
        blocks.append(("row", "RFI-%03d | Question about grid line %d%s" % (n, n, tail), n))
    return doctext("xlsx", blocks, pages=1)


def padding(n, seed):
    """n paragraphs of plain prose (nothing a cap would favour)."""
    return [("para", prose(5, seed + k)) for k in range(n)]


class RegisterChangeTests(unittest.TestCase):
    def test_a_row_that_only_gained_a_column_has_no_minus_line(self):
        def report(extra):
            rows = [("row", "Borehole | Depth (m) | SWL (m)" + (" | Jan 2025 SWL (m)" if extra else ""), 0)]
            for n, (w, j) in enumerate([("2.5", "2.2"), ("2.9", "2.15"), ("3.4", "3.0")], 1):
                rows.append(("row", "BH%d | 6.0 | %s" % (n, w) + (" | %s" % j if extra else ""), n))
            return doctext("docx", [("heading", "5 Groundwater", 1), ("para", prose(8, 7))] + rows +
                           [("para", prose(6, 8))])
        docs = [item("D1", "Geotech Report Rev A.docx", report(False)),
                item("D2", "Geotech Report Rev B.docx", report(True))]
        d2 = section(text_of(build(docs)), "D2")
        self.assertIn("Changes from D1:", d2)
        self.assertIn("+ BH2 | 6.0 | 2.9 | 2.15", d2)
        self.assertNotRegex(d2, r"(?m)^- ")

    def test_old_cells_and_closures(self):
        self.assertEqual(docdigest._old_cells("RFI-077 | Q | | | Open | TBC", "RFI-077 | Q | A | SB | Closed | TBC"),
                         "RFI-077 | … | | | Open | …")
        self.assertIsNone(docdigest._old_cells("BH1 | 6.0", "BH1 | 6.0 | 2.2"))
        self.assertEqual(docdigest._old_cells("Open | 4", "Open | 6"), "Open | 4")
        self.assertTrue(docdigest._closes("RFI-1 | Q | Open", "RFI-1 | Q | Closed"))
        self.assertFalse(docdigest._closes("RFI-1 | Q | Open", "RFI-1 | Q2 | Open"))

    def test_written_out_answer_among_numbered_template_answers_is_kept(self):
        self.assertEqual(docdigest._cell_shape("refer to response to rfi-010."),
                         docdigest._cell_shape("refer to response to rfi-053."))
        self.assertEqual(docdigest._cell_shape("stage 5"), "stage 5")
        templates = ["Refer to revised detail on RD-ST-12%02d.", "Refer to response to RFI-%03d.",
                     "Confirmed as shown on the drawings.", "Offset by 150 mm; no structural impact."]
        open_rows = {23, 61, 74, 79}
        for special in (60, 71):
            def answer(n):
                if n == special:
                    return "Relocate footing F12 600 mm north; stormwater pipe to remain in place."
                t = templates[n % len(templates)]
                return t % n if "%" in t else t
            doc = register(80, open_rows, answer)
            body = section(text_of(build([item("D1", "RFI Register.xlsx", doc)], squeeze="max")), "D1")
            self.assertIn("Relocate footing F12", body, special)
            shown = sorted(int(m) for m in re.findall(r"(?m)^RFI-(\d+) [^\n]*\| Open$", body))
            self.assertEqual(shown, sorted(open_rows), special)


class ReportTableTests(unittest.TestCase):
    def test_units_and_ph_count_as_figures(self):
        for text in ("Sulfate was below 400 mg/kg.", "The bulk unit weight is 18 kN/m3.",
                     "Resistivity was 2,400 ohm.cm.", "A soil pH of 5.4 was measured."):
            self.assertGreaterEqual(docdigest.doc_fact_score(text), docdigest.HARD_BONUS, text)

    def test_references_list_counts_like_an_appendix(self):
        self.assertEqual(docdigest._section_weight("References"), -1)
        self.assertEqual(docdigest._section_weight("8 References"), -1)
        self.assertEqual(docdigest._section_weight("6.7 Retaining walls"), 0)

    def report(self, table_heading="6.7 Retaining walls"):
        blocks = [("heading", "Geotechnical report", 1), ("para", prose(4, 900))]
        for n in range(8):
            blocks.append(("heading", "%d Section %s" % (n + 1, "ABCDEFGH"[n]), 1))
            blocks += padding(3, 910 + 10 * n)
            blocks.append(("para", "The contractor shall keep the work areas in a tidy and safe condition."))
        blocks += [("heading", table_heading, 1),
                   ("para", "Retaining walls up to 2 m high are proposed at the washbay ramp."),
                   ("row", "Material | Unit weight | Ka | K0 | Kp", 0),
                   ("row", "Engineered fill | 20 | 0.33 | 0.50 | 3.0", 1),
                   ("row", "Alluvial clay | 18 | 0.36 | 0.55 | 2.8", 2),
                   ("heading", "1.4 Hold points", 1),
                   ("row", "No. | Clause | Description | Type", 0),
                   ("row", "HP6 | 5.2 | Crane runway survey before crane installation | Hold", 1),
                   ("row", "HP7 | 6.1 | Crane load test witnessed before handover | Hold", 2),
                   ("heading", "9 References", 1)]
        blocks += [("item", "• AS %d Methods of testing soils for engineering purposes, part %d." % (1289 + k, k), 0)
                   for k in range(6)]
        return doctext("docx", blocks)

    def test_headed_table_rows_kept_ahead_of_references_and_padding(self):
        body = section(text_of(build([item("D1", "Report.docx", self.report())], squeeze="max")), "D1")
        self.assertIn("Alluvial clay | 18 | 0.36 | 0.55 | 2.8", body)
        self.assertIn("HP7 | 6.1 | Crane load test witnessed before handover | Hold", body)
        self.assertIn("Material | Unit weight | Ka | K0 | Kp\n", body)       # with its header row
        pieces = docdigest._pieces(docdigest._paragraphs(self.report()), 125)
        score = dict((p["text"], p["score"]) for p in pieces)
        self.assertGreater(score["Alluvial clay | 18 | 0.36 | 0.55 | 2.8"],
                           score["The contractor shall keep the work areas in a tidy and safe condition."])
        self.assertGreater(score["Alluvial clay | 18 | 0.36 | 0.55 | 2.8"],
                           max(v for t, v in score.items() if "Methods of testing" in t))

    def test_rows_in_an_appendix_gain_nothing(self):
        paras = docdigest._paragraphs(self.report("Appendix A - Retaining wall parameters"))
        headed = set(p["group"] for p in paras if p["kind"] == "row" and p.get("header"))
        row = [p for p in paras if p["text"].startswith("Alluvial clay")][0]
        self.assertEqual(docdigest._row_floor(row, headed), 0)
        self.assertEqual(docdigest._row_floor(dict(row, key=0), headed), docdigest.HARD_BONUS + 2.0)
        self.assertEqual(docdigest._row_floor(dict(row, key=0, group=None), headed), 0)     # (a change)

    def test_a_row_is_kept_only_with_room_for_its_header(self):
        header = "Material | Unit weight (kN/m3) | At-rest coefficient K0 | Active coefficient Ka | Passive Kp"
        doc = doctext("docx", [("para", prose(6, 950)), ("row", header, 0), ("row", "Fill | 20 | 0.50 | 0.33 | 3.0", 1)])
        pieces = docdigest._pieces(docdigest._paragraphs(doc), 400)
        data = [i for i, p in enumerate(pieces) if p["text"].startswith("Fill")][0]
        head = [i for i, p in enumerate(pieces) if p["text"] == header][0]
        need = docdigest._cost(pieces[data]) + docdigest._cost(pieces[head])
        keep = docdigest._choose(pieces, need - 5)
        self.assertFalse(data in keep and head not in keep)
        self.assertNotIn(data, keep)
        keep = docdigest._choose(pieces, need + 5)
        self.assertIn(data, keep)
        self.assertIn(head, keep)


def lot_records(count=12):
    """QA records, one paragraph per lot, each long enough to be cut at its clauses."""
    paras = [("heading", "QA records - footings", 1)]
    for n in range(1, count + 1):
        paras.append(("para", "Lot %03d - Pad footing F%d - Inspection and test record ITP: ITP-STR-%02d Hold point: "
                      "HP3 reinforcement and cover Released by: S. Brown Excavation inspected: Yes Founding "
                      "material: stiff to very stiff clay Reinforcement checked: Yes Cover checked: Yes Bar "
                      "chairs: Yes Concrete supplier docket: 4%05d Volume: %d.%d m3 Slump: 100 mm Temperature: "
                      "%d C Cylinders taken: 3 7 day result: %d.%d MPa"
                      % (n, n, n % 6, 10000 + 731 * n, 5 + n, n % 10, 12 + n, 24 + n % 9, n % 10)))
    return doctext("pdf", [("page", "", 1)] + paras, pages=1)


class ClauseTests(unittest.TestCase):
    def test_a_lot_result_is_never_kept_without_its_lot(self):
        paras = docdigest._paragraphs(lot_records())
        pieces = docdigest._pieces(paras, 125)
        self.assertTrue(any(not p.get("starts", True) for p in pieces))      # (clauses were cut)
        shown = 0
        for cap in range(500, 2600, 300):
            lines = docdigest._render(pieces, docdigest._choose(pieces, cap, fold_heads=True))
            results = [l for l in lines if "MPa" in l]
            shown += len(results)
            for line in results:
                self.assertIn("Lot ", line, (cap, line))
        self.assertGreater(shown, 5)

    def test_a_clause_is_kept_only_with_the_start_of_its_sentence(self):
        text = ("Pad footings founded in the stiff to very stiff natural clay below the fill and the topsoil may be "
                "designed for an allowable bearing pressure of 150 kPa, provided the bases are clean and dry. "
                "Settlement of footings designed in accordance with these recommendations is expected to be "
                "less than 25 mm total and 15 mm differential, provided the founding clay is protected from "
                "drying and wetting during construction.")
        doc = doctext("docx", [("heading", "6 Foundations", 1), ("para", prose(8, 960)), ("para", text),
                               ("para", prose(8, 961))])
        pieces = docdigest._pieces(docdigest._paragraphs(doc), 120)
        for cap in range(150, 700, 25):
            lines = docdigest._render(pieces, docdigest._choose(pieces, cap, fold_heads=True))
            joined = "\n".join(lines)
            if "and 15 mm differential" in joined:
                self.assertIn("Settlement of footings", joined, cap)
            for line in lines:
                self.assertNotRegex(line, r"(?m)^(?:and|designed|less) ", cap)


class VersionHeadingTests(unittest.TestCase):
    def report(self, extra_sections=()):
        blocks = [("heading", "6 Recommendations", 1), ("para", prose(6, 970))]
        names = ["Footings", "Slabs", "Pavements"] + list(extra_sections) + ["Construction considerations",
                                                                              "Further work"]
        for n, name in enumerate(names, 1):
            blocks.append(("heading", "6.%d %s" % (n, name), 2))
            seed = 980 + ["Footings", "Slabs", "Pavements", "Construction considerations", "Further work",
                          "Ground improvement"].index(name) if name != "Piles" else 999
            blocks.append(("para", prose(5, seed)))
        return doctext("docx", blocks)

    def test_renumbered_headings_are_not_changes(self):
        docs = [item("D1", "Geotech Report Rev B.docx", self.report()),
                item("D2", "Geotech Report Rev C.docx", self.report(["Piles", "Ground improvement"]))]
        d2 = section(text_of(build(docs)), "D2")
        self.assertIn("Changes from D1:", d2)
        self.assertIn("+ # 6.4 Piles", d2)
        self.assertNotIn("Construction considerations", d2)
        self.assertNotIn("Further work", d2)
        self.assertEqual(docdigest._unnumbered("# head 6 13 construction considerations"),
                         "# head construction considerations")
        self.assertEqual(docdigest._unnumbered("# head 2025 annual report"), "# head 2025 annual report")

    def test_headings_only_with_a_change_kept_under_them(self):
        old = [("heading", "1 Scope", 1), ("para", prose(4, 990))]
        new = [("heading", "1 Scope", 1), ("para", prose(4, 990))]
        for n in range(1, 9):
            old += [("heading", "%d.1 Item %s" % (n + 1, "ABCDEFGH"[n - 1]), 2), ("para", prose(3, 1000 + n))]
            change = " The site was walked over again by the team." if n % 2 else ""
            new += [("heading", "%d.1 Item %s" % (n + 1, "ABCDEFGH"[n - 1]), 2), ("para", prose(3, 1000 + n) + change)]
        appendix = "Appendix E - Consolidation test results for the soft clay lens samples (BH14, BH15 and BH16)"
        new += [("heading", appendix, 1),
                ("para", "Compression index Cc = 0.38 (average of three tests); preconsolidation pressure 45 kPa "
                         "to 60 kPa (AS 1289.6.6.1).")]
        base = {"id": "D1", "name": "R Rev A.docx", "doc": doctext("docx", old)}
        entry = {"id": "D2", "name": "R Rev B.docx", "doc": doctext("docx", new)}
        for e in (base, entry):
            e["paras"] = docdigest._paragraphs(e["doc"])
            e["units"] = docdigest.version_units(e["paras"])
        _sim, entry["ops"] = docdigest.compare(base["units"], entry["units"])
        caps = dict(docdigest.DOC_CAPS["max"])
        cap = 400
        self.assertGreater(len(appendix), docdigest.HEADING_SHARE * cap)     # (beyond the heading share)
        lines = docdigest._diff_lines(base, entry, caps, cap)
        text = "\n".join(lines)
        self.assertIn("+ # %s\n+ Compression index Cc = 0.38" % appendix, text)
        self.assertLess(text.count("+ The site was walked over again"), 4)     # (not all fit)
        for n, line in enumerate(lines):
            if line.startswith("# "):        # a context heading: a change follows it
                self.assertTrue(n + 1 < len(lines) and lines[n + 1].startswith(("+ ", "- ")), text)

    def test_a_repeated_text_that_seems_to_move_shows_where_it_changed(self):
        def certificates(signatories):
            blocks = []
            for n, who in enumerate(signatories, 1):
                blocks += [("heading", "Test certificate BL-T-%03d" % n, 1),
                           ("para", "Sample %d from BH%d at %d.0 m; moisture content %d%%." % (n, n, n, 10 + n)),
                           ("para", "Approved signatory: %s | Laboratory: Riverside NATA site no. 99999" % who)]
            return doctext("docx", [("para", prose(6, 1020))] + blocks)
        docs = [item("D1", "Lab results Rev A.docx", certificates(["R. Singh", "R. Singh"])),
                item("D2", "Lab results Rev B.docx", certificates(["B. Ward", "R. Singh"]))]
        d2 = section(text_of(build(docs, squeeze="light")), "D2")
        self.assertIn("+ Approved signatory: B. Ward", d2)
        self.assertIn("- Approved signatory: R. Singh", d2)
        self.assertNotIn("+ Approved signatory: R. Singh", d2)
        self.assertNotIn("BL-T-002", d2)


class RevisionRowTests(unittest.TestCase):
    def line(self, name, *pages):
        return [l for l in text_of(build([item("D1", name, drawing(*pages))])).split("\n") if l.startswith("D1 ")][0]

    def test_merged_rows_are_split_and_initials_left_off(self):
        line = self.line("RD-ST-1202 [D] FOOTING PLAN.pdf",
                         ["TITLE: FOOTING PLAN", "REV DATE DESCRIPTION BY CHK APP",
                          "D 29.04.25 FOOTINGS REVISED TO SUIT GEOTECH REV C LO SB TH C 14.02.25 ISSUED FOR "
                          "CONSTRUCTION LO SB TH", "STATUS: FOR CONSTRUCTION"])
        self.assertTrue(line.endswith(" - rev D 29.04.25 FOOTINGS REVISED TO SUIT GEOTECH REV C, for construction"),
                        line)

    def test_a_row_that_only_repeats_the_status_once_its_initials_go(self):
        line = self.line("SK [B].pdf", ["B 14.02.25 ISSUED FOR CONSTRUCTION LO SB TH",
                                        "A 13.12.24 CONCEPT ISSUE LO SB TH", "STATUS: FOR CONSTRUCTION"])
        self.assertTrue(line.endswith(" - for construction"), line)

    def test_oldest_first_table_still_finds_the_drawing_revision(self):
        line = self.line("RD-ST-1240 [B] JOINT LAYOUT.pdf",
                         ["A 14.02.25 ISSUED FOR CONSTRUCTION LO SB TH B 20.06.25 REBATE AT ROLLER DOOR RD2 ADDED LO "
                          "SB TH", "STATUS: FOR CONSTRUCTION"])
        self.assertIn(" - rev B 20.06.25 REBATE AT ROLLER DOOR RD2 ADDED, for construction", line)

    def test_descriptions_without_initials_are_kept_whole(self):
        dwg = drawing(["REV DATE DESCRIPTION BY CHK APP", "C 10.03.25 ISSUED FOR DA"])
        self.assertEqual(docdigest._latest_revision(dwg, "C")[2], "ISSUED FOR DA")
        dwg = drawing(["A 13.12.24 ADDED NEW PIT"])
        self.assertEqual(docdigest._latest_revision(dwg, "A")[2], "ADDED NEW PIT")
        self.assertIsNone(docdigest._latest_revision(dwg, "B"))


class PdfLineTests(unittest.TestCase):
    def test_complete_table_rows_are_not_gathered_as_cells(self):
        rows = ["RD-ST-1202 FOOTING PLAN C A1", "RD-ST-1203 ROOF FRAMING PLAN B A1", "RD-ST-1210 FOOTING DETAILS B A1"]
        doc = doctext("pdf", [("page", "", 1), ("para", "Transmittal TR-031")] + [("para", r) for r in rows] +
                      [("para", "Sam Brown"), ("para", "Issued for review"), ("para", "Example Consulting")], pages=1)
        texts = kept_texts(doc)
        for r in rows:
            self.assertIn(r, texts)
        self.assertIn("Sam Brown Issued for review Example Consulting", texts)


class FurnitureTests(unittest.TestCase):
    def test_header_repeating_the_title_goes_but_a_footer_with_a_reference_stays(self):
        doc = doctext("docx", [("para", "SITE INSPECTION REPORT SIR-03"), ("para", "Project | Riverside Depot Upgrade"),
                               ("para", "Footing F9 soft clay removed to 2.8 m and mass concrete placed."),
                               ("para", "Header: Riverside Depot Upgrade | Site inspection report SIR-03"),
                               ("para", "Footer: Example Consulting | 623-RPT-019")])
        body = section(text_of(build([item("D1", "Site Inspection Report SIR-03.docx", doc)])), "D1")
        self.assertNotIn("Header:", body)
        self.assertIn("Footer: Example Consulting | 623-RPT-019", body)
        self.assertNotIn("…", body)
        self.assertTrue(docdigest._meta_redundant("Header: Inception Meeting Minutes",
                                                  set(["inception", "meeting", "minutes", "docx"])))

    def test_closing_wording_repeated_in_a_series_is_shown_once(self):
        disclaimer = ("This report records the observations made during the inspection. It does not relieve the "
                      "Contractor of its obligations under the contract.")
        payment = "Payment of $2,450.00 is due within 30 days of the date of this invoice."
        docs = []
        for n in range(1, 4):
            doc = doctext("docx", [("para", "Inspection %d at footing F%d: founding in stiff clay." % (n, n)),
                                   ("para", disclaimer), ("para", payment)])
            docs.append(item("D%d" % n, "Inspection %d.docx" % n, doc))
        text = text_of(build(docs))
        self.assertEqual(text.count("does not relieve the Contractor"), 1)
        self.assertEqual(text.count("Payment of $2,450.00"), 3)
        for n in range(1, 4):
            self.assertIn("Inspection %d at footing F%d" % (n, n), section(text, "D%d" % n))


class FamilyKeyTests(unittest.TestCase):
    def test_year_and_month_in_a_name_is_a_date(self):
        self.assertEqual(docdigest.family_key("Settlement monitoring 2025-08.csv"), "settlement monitoring")
        self.assertEqual(docdigest.family_key("Settlement monitoring_2025_09.csv"), "settlement monitoring")
        self.assertEqual(docdigest.family_key("Report 2025-08-14.pdf"), "report")
        self.assertEqual(docdigest.family_key("Report 2025.pdf"), "report 2025")

    def test_monthly_files_show_changes(self):
        def csv(rows):
            blocks = [{"type": "sheet", "text": "x.csv", "level": 1, "rows": rows + 1},
                      ("row", "Date | SP-01 (mm) | SP-02 (mm) | Surveyor", 0)]
            blocks += [("row", "2025-08-%02d | %d.%d | %d.%d | R. Singh" % (k + 1, k, k, k + 1, k), k + 1)
                       for k in range(rows)]
            return doctext("text", blocks, pages=1)
        docs = [item("D1", "Settlement monitoring 2025-08.csv", csv(5)),
                item("D2", "Settlement monitoring 2025-09.csv", csv(10))]
        d2 = section(text_of(build(docs)), "D2")
        self.assertIn("Changes from D1:", d2)
        self.assertNotIn("# Sheet", d2.replace("# Sheet x.csv", ""))
        self.assertNotIn("2025-08-01 |", d2)


class ContentsRuleTests(unittest.TestCase):
    def test_dot_leader_values_are_kept(self):
        lines = ["Concrete strength f'c (MPa) .......... 32", "Minimum cover (mm) .......... 40",
                 "Design life (years) .......... 50", "Site class .......... C", "Discipline .......... Civil",
                 "Exposure classification .......... B1"]
        for kind in ("pdf", "docx"):
            blocks = [("page", "", 1)] if kind == "pdf" else []
            blocks += [("heading", "3 Design criteria", 1)] + [("para", l) for l in lines] + \
                      [("para", "The design life is 50 years.")]
            texts = kept_texts(doctext(kind, blocks, pages=1 if kind == "pdf" else None))
            for l in lines:
                self.assertIn(l, texts, kind)      # (a 'label .... value' line is not gathered with others)

    def test_list_of_drawings_is_kept(self):
        rows = ["623.0001-ST-1001 Cover sheet and drawing schedule 2", "623.0001-ST-1202 Footing plan 4",
                "623.0001-ST-1210 Footing details 3", "623.0001-ST-1300 Retaining wall sections 1"]
        doc = doctext("pdf", [("page", "", 1), ("para", "Report text."), ("page", "", 2),
                              ("para", "The structural drawings issued for construction are listed below."),
                              ("heading", "List of Drawings", 1)] + [("para", r) for r in rows] +
                      [("para", "Gridline B footings were enlarged.")], pages=2)
        texts = kept_texts(doc)
        self.assertIn("List of Drawings", texts)
        for r in rows:
            self.assertIn(r, texts)         # (each drawing on its own line)
        doc = doctext("docx", [("para", "The structural drawings issued for construction are listed below."),
                               ("heading", "List of Drawings", 1)] + [("para", r) for r in rows])
        self.assertEqual(kept_texts(doc)[2:], rows)

    def test_contents_with_front_matter_pages_still_goes(self):
        toc = ["Preface ..... iii", "Glossary ..... iv", "1 Introduction ..... 1", "2 Site ..... 3",
               "3 Ground conditions ..... 7"]
        long_toc = ["%d %s ........ %d" % (n, "Long section title about the ground model and design " * 2, n + 2)
                    for n in range(1, 4)]
        for lines in (toc, long_toc):
            doc = doctext("docx", [("para", l) for l in lines] + [("heading", "1 Introduction", 1),
                                                                  ("para", "The depot is to grow.")])
            texts = kept_texts(doc)
            for l in lines:
                self.assertNotIn(l, texts)
            self.assertIn("The depot is to grow.", texts)


class ContactRuleTests(unittest.TestCase):
    def test_findings_at_a_level_are_not_contact_details(self):
        for text in ("Cracking observed in the slab soffit at level 2, near grid C4.",
                     "Water ingress at level 1, stair 2 after heavy rain.",
                     "Refer to p. 12 of the geotechnical report for borehole logs."):
            self.assertFalse(docdigest._boilerplate(text, False, False), text)
        for text in ("Level 3, 100 Smith Street, Riverside NSW 2000", "Sam Brown, m: 0400 000 000",
                     "PO Box 123, Riverside NSW 2000"):
            self.assertTrue(docdigest._boilerplate(text, False, False), text)

    def test_findings_section_keeps_its_level_findings(self):
        doc = doctext("docx", [("heading", "Inspection report", 1)] + padding(12, 1040) +
                      [("heading", "5 Inspection findings", 1),
                       ("item", "• Cracking observed in the slab soffit at level 2, near grid C4.", 0),
                       ("item", "• Spalling to the column at level 3, grid B2; reinforcement exposed.", 0)] +
                      padding(12, 1060))
        body = section(text_of(build([item("D1", "Inspection.docx", doc)], squeeze="light")), "D1")
        self.assertIn("level 2, near grid C4", body)
        self.assertIn("level 3, grid B2", body)


class PartlyReadTests(unittest.TestCase):
    def register(self, rows_kept, total, changed=False):
        blocks = [{"type": "sheet", "text": "Register", "level": 1, "rows": total},
                  ("row", "RFI | Subject | Status", 0)]
        blocks += [("row", "RFI-%03d | Question about grid line %d | Closed" % (n, n), n) for n in range(1, rows_kept)]
        return doctext("xlsx", blocks, pages=1)

    def test_a_register_cut_at_the_row_limit_is_never_the_same_text(self):
        docs = [item("D1", "RFI register Rev 3.xlsx", self.register(50, 6001)),
                item("D2", "RFI register Rev 4.xlsx", self.register(50, 6001))]
        d2 = section(text_of(build(docs)), "D2")
        self.assertNotIn("Same text", d2)
        self.assertIn("Changes from D1 in the part read (the rest was not read, so not compared): none.", d2)
        self.assertTrue(docdigest._partly_read(self.register(50, 6001)))
        self.assertFalse(docdigest._partly_read(self.register(50, 50)))

    def test_a_pdf_cut_at_the_page_limit_is_never_the_same_text(self):
        def pdf(note=""):
            blocks = []
            for page in (1, 2):
                blocks += [("page", "", page), ("para", prose(4, 1080 + page))]
            return doctext("pdf", blocks, pages=5, note=note)
        self.assertTrue(docdigest._partly_read(pdf()))
        self.assertTrue(docdigest._partly_read(dict(pdf(), pages=2, note="partly read: first 2 of 5 pages")))
        self.assertFalse(docdigest._partly_read(dict(pdf(), pages=2)))
        docs = [item("D1", "Report.pdf", pdf()), item("D2", "Copy for client.pdf", pdf())]
        self.assertNotIn("Same text", section(text_of(build(docs)), "D2"))

    def test_csv_revisions_do_not_show_their_sheet_names_as_changes(self):
        def csv(name, rows):
            blocks = [{"type": "sheet", "text": name, "level": 1, "rows": len(rows) + 1}, ("row", "A | B", 0)]
            return doctext("text", blocks + [("row", r, n + 1) for n, r in enumerate(rows)], pages=1)
        rows = ["RFI-%03d | Closed" % n for n in range(1, 30)]
        docs = [item("D1", "RFI register Rev 3.csv", csv("RFI register Rev 3.csv", rows)),
                item("D2", "RFI register Rev 4.csv", csv("RFI register Rev 4.csv", rows + ["RFI-030 | Open"]))]
        d2 = section(text_of(build(docs)), "D2")
        self.assertIn("+ RFI-030 | Open", d2)
        self.assertNotRegex(d2, r"(?m)^[+-] # Sheet")
        docs = [item("D1", "Register A.csv", csv("Register A.csv", rows)),
                item("D2", "Register B.csv", csv("Register B.csv", rows))]
        self.assertIn("Same text as D1.", section(text_of(build(docs)), "D2"))


class PageShapeTests(unittest.TestCase):
    def pdf(self, lines, edge="last"):
        blocks = []
        for n, line in enumerate(lines, 1):
            body = ("para", "Calculation sheet %d: %s" % (n, prose(2, 1100 + n)))
            blocks += [("page", "", n)] + ([body, ("para", line)] if edge == "last" else [("para", line), body])
        return doctext("pdf", blocks, pages=len(lines))

    def test_footers_with_different_numbers_of_figures(self):
        doc = self.pdf(["Riverside Depot Structural Calculations 1", "Riverside Depot Structural Calculations Page 2 of 3",
                        "Riverside Depot Structural Calculations Page 3 of 3"])
        text = text_of(build([item("D1", "Calcs.pdf", doc)]))
        self.assertIn("Calculation sheet 2:", text)
        self.assertNotIn("Page 2 of 3", text)

    def test_a_literal_hash_does_not_break_page_lines(self):
        doc = self.pdf(["Lot 5 Pour 12", "Lot # Pour 12", "Lot 7 Pour 12"], edge="first")
        self.assertTrue(text_of(build([item("D1", "Pours.pdf", doc)])))
        doc = self.pdf(["Lot # Pour", "Lot 14 Pour", "Lot 9 Pour", "Lot 22 Pour"], edge="first")
        texts = kept_texts(doc)
        for line in ("Lot # Pour", "Lot 14 Pour", "Lot 9 Pour", "Lot 22 Pour"):
            self.assertIn(line, texts)


class SiteInstructionTests(unittest.TestCase):
    def test_hold_points_without_a_named_party_are_kept(self):
        holds = ["No excavation below the water table is to proceed without the approval of the geotechnical "
                 "engineer.",
                 "Do not backfill the retaining wall footings without the written approval of the site engineer.",
                 "Any rock encountered must not be removed by blasting without the consent of Council.",
                 "Props shall not be removed without the approval of the engineer."]
        doc = doctext("docx", [("heading", "Site instruction 14", 1),
                               ("para", "Further to the inspection on site today, please note the following.")] +
                      [("item", "• " + h, 0) for h in holds])
        body = section(text_of(build([item("D1", "Site instruction 14.docx", doc)], squeeze="light")), "D1")
        for h in holds:
            self.assertIn(h, body)
        self.assertTrue(docdigest._boilerplate(
            "This drawing must not be copied without the written consent of Example Consulting.", False, True))
        self.assertTrue(docdigest._boilerplate(
            "It should not be used or relied upon by any other party without the prior written consent of "
            "Bedrock Labs.", False, True))


if __name__ == "__main__":
    unittest.main()
