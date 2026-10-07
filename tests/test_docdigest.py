"""Tests for squish_app.docdigest (synthetic DocText inputs only) and the
documents cross-references in squish_app.digest (doc_ids, alias_for_records)."""

import json
import os
import random
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
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
            blocks.append(("heading", "%d Chapter about item number %d" % (n + 1, n + 1), 1))
            blocks.append(("heading", "%d.1 A rather long sub heading for item %d with words" % (n + 1, n + 1), 2))
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
            ("para", "1 Introduction 3"), ("para", "2 Site description 4"),
            ("para", "3 Ground conditions 7"), ("para", "Appendix A Borehole logs 21"),
            ("page", "", 3), ("heading", "1 Introduction", 1),
            ("para", "Executive summary ................ 2"),
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
        sheet = doctext("xlsx", [{"type": "sheet", "text": "S1", "level": 1, "rows": 2},
                                 ("row", "Ref | | | Far", 0), ("row", "Total | | 4513426.78 | ", 1)], pages=1)
        text = section(text_of(build([item("D2", "Sheet.xlsx", sheet)])), "D2")
        self.assertIn("Ref | | | Far\nTotal | | 4513426.78", text)

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
        self.assertIn("- RFI-050 | Question about grid line 50 | Open", d2)
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


if __name__ == "__main__":
    unittest.main()
