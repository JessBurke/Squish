"""Tests for docs.py: Word, Excel, PowerPoint, PDF, text and zip files (all synthetic).

Office files are built with tests/doc_builder.py (standard library only); PDFs
with tests/pdf_builder.py when it exists. Hostile inputs (entity bombs, zip
bombs, deep nesting, damaged files) must give a status, never an exception,
and must finish quickly.
"""

import importlib.util
import io
import json
import os
import random
import shutil
import struct
import tempfile
import time
import unittest
import zipfile
from unittest import mock

from squish_app import docs
from tests import doc_builder as b

try:
    from tests import pdf_builder
except ImportError:  # written by another change; PDF-file tests skip without it
    pdf_builder = None

DOC_KEYS = set(["kind", "status", "note", "title", "pages", "drawing", "blocks", "chars", "reader"])
STATUSES = set(["ok", "no_text", "protected", "too_big", "unsupported", "error"])


def texts(doc, kind=None):
    return [bl["text"] for bl in doc["blocks"] if kind is None or bl["type"] == kind]


def blocks(doc):
    return [(bl["type"], bl["text"], bl["level"]) for bl in doc["blocks"]]


def check_shape(test, doc):
    """Every DocText has the contract's keys and is JSON-serialisable."""
    test.assertEqual(set(doc), DOC_KEYS)
    test.assertIn(doc["status"], STATUSES)
    json.dumps(doc)
    for bl in doc["blocks"]:
        test.assertIn(bl["type"], ("heading", "para", "item", "row", "sheet", "page", "slide", "member"))
        test.assertIsInstance(bl["text"], str)
        test.assertIsInstance(bl["level"], int)
        if bl.get("doc"):
            check_shape(test, bl["doc"])


class Timed(object):
    """Context manager asserting that a block finishes within ``seconds``."""

    def __init__(self, test, seconds):
        self.test, self.seconds = test, seconds

    def __enter__(self):
        self.start = time.time()

    def __exit__(self, *exc):
        if exc[0] is None:
            self.test.assertLess(time.time() - self.start, self.seconds)


# --------------------------------------------------------------------------
# Word
# --------------------------------------------------------------------------

class WordTests(unittest.TestCase):

    def extract(self, body, **kw):
        doc = docs.extract("Report.docx", b.docx(body, **kw))
        check_shape(self, doc)
        return doc

    def test_headings_from_style_names_and_outline_levels(self):
        body = (b.para("Riverside Depot Upgrade", "Title")
                + b.para("Introduction", "Heading1")
                + b.para("Body text.")
                + b.para("Site conditions", "Heading2")
                + b.para("Localised style id", "Kop3")
                + b.para("Custom heading", "MyHeading")
                + b.para("Outline paragraph", outline=3))
        doc = self.extract(body)
        self.assertEqual(blocks(doc), [
            ("heading", "Riverside Depot Upgrade", 1), ("heading", "Introduction", 1),
            ("para", "Body text.", 0), ("heading", "Site conditions", 2),
            ("heading", "Localised style id", 3), ("heading", "Custom heading", 2),
            ("heading", "Outline paragraph", 4)])
        self.assertEqual(doc["kind"], "docx")
        self.assertEqual(doc["status"], "ok")
        self.assertEqual(doc["reader"], "builtin")

    def test_numbered_headings_and_lists(self):
        body = (b.para("Introduction", "Heading1", num=(4, 0))
                + b.para("Scope", "Heading2", num=(4, 1))
                + b.para("Limits", "Heading2", num=(4, 1))
                + b.para("Ground", "Heading1", num=(4, 0))
                + b.para("Bearing", "Heading2", num=(4, 1))
                + b.para("Bullet from style", "ListBullet")
                + b.para("Nested bullet", num=(1, 1))
                + b.para("First step", num=(2, 0))
                + b.para("Detail a", num=(2, 1))
                + b.para("Detail b", num=(2, 1))
                + b.para("Second step", num=(2, 0))
                + b.para("Restarted list", num=(3, 0))
                + b.para("Continues", num=(3, 0))
                + b.para("Numbering removed", "ListBullet", num=("0", 0)))
        doc = self.extract(body, numbering=True)
        self.assertEqual(blocks(doc), [
            ("heading", "1 Introduction", 1), ("heading", "1.1 Scope", 2),
            ("heading", "1.2 Limits", 2), ("heading", "2 Ground", 1), ("heading", "2.1 Bearing", 2),
            ("item", "• Bullet from style", 0), ("item", "• Nested bullet", 1),
            ("item", "1. First step", 0), ("item", "a) Detail a", 1), ("item", "b) Detail b", 1),
            ("item", "2. Second step", 0), ("item", "1. Restarted list", 0),
            ("item", "2. Continues", 0), ("para", "Numbering removed", 0)])

    def test_tables_as_rows(self):
        nested = b.table([["n1", "n2"], ["n3", "n4"]])
        body = (b.table([["Item", "Qty", "Rate"], ["Excavation", "45 m3", ""], ["Fill", "dup", "$12"],
                         ["Nested", nested, "end"]], merged={(2, 1)})
                + b.para("After the table"))
        doc = self.extract(body)
        self.assertEqual(blocks(doc), [
            ("row", "Item | Qty | Rate", 0), ("row", "Excavation | 45 m3", 1), ("row", "Fill | $12", 2),
            ("row", "Nested | n1 | n2; n3 | n4 | end", 3), ("para", "After the table", 0)])

    def test_tracked_changes_keep_insertions_drop_deletions(self):
        body = b.para('<w:r><w:t xml:space="preserve">Bearing pressure </w:t></w:r>'
                      '<w:del w:id="1" w:author="x"><w:r><w:delText>150</w:delText></w:r></w:del>'
                      '<w:ins w:id="2" w:author="x"><w:r><w:t>120</w:t></w:r></w:ins>'
                      '<w:r><w:t xml:space="preserve"> kPa</w:t></w:r>'
                      '<w:moveFrom w:id="3"><w:r><w:t>old place</w:t></w:r></w:moveFrom>'
                      '<w:moveTo w:id="4"><w:r><w:t xml:space="preserve"> new place</w:t></w:r></w:moveTo>')
        doc = self.extract(body)
        self.assertEqual(texts(doc), ["Bearing pressure 120 kPa new place"])

    def test_comments_footnotes_and_endnotes(self):
        body = (b.para('<w:r><w:t>Groundwater at 2.4 m</w:t></w:r><w:r><w:footnoteReference w:id="5"/></w:r>'
                       '<w:commentRangeStart w:id="9"/><w:r><w:t xml:space="preserve"> below surface.</w:t></w:r>'
                       '<w:commentRangeEnd w:id="9"/><w:r><w:commentReference w:id="9"/></w:r>')
                + b.para('<w:r><w:t>Second note</w:t></w:r><w:r><w:footnoteReference w:id="3"/></w:r>'
                         '<w:r><w:endnoteReference w:id="2"/></w:r>'))
        doc = self.extract(body, footnotes={"3": "Second footnote.", "5": "Measured 2025-03-04.",
                                            "8": "Never referenced."},
                           endnotes={"2": "An endnote."}, comments={"9": "Sam: please confirm"})
        self.assertEqual(texts(doc), [
            "Groundwater at 2.4 m[1] below surface. [comment: Sam: please confirm]",
            "Second note[2][e1]", "[1] Measured 2025-03-04.", "[2] Second footnote.", "[e1] An endnote."])

    def test_headers_and_footers_once(self):
        header = b.hdr_xml("header", b.para("Riverside Depot | Geotechnical Report"))
        footer = b.hdr_xml("footer", b.para("Page 3 of 12"), b.para("623.0001-RPT-001 Rev B"), b.para("7"))
        doc = self.extract(b.para("Body."), headers=[header, header], footers=[footer])
        self.assertEqual(texts(doc), ["Body.", "Header: Riverside Depot | Geotechnical Report",
                                      "Footer: 623.0001-RPT-001 Rev B"])

    def test_text_boxes_read_once(self):
        body = b.para("Before") + b.para(b.text_box("Boxed note: no stockpiles", "Second line")) + b.para("After")
        doc = self.extract(body)
        self.assertEqual(texts(doc), ["Before", "Boxed note: no stockpiles", "Second line", "After"])

    def test_fields_tabs_breaks_symbols_and_hyperlinks(self):
        body = b.para(
            '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> REF _Ref1 \\h </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>Table 3</w:t></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
            '<w:r><w:tab/><w:t>tolerance</w:t><w:sym w:font="Symbol" w:char="F0B1"/><w:t>5 mm</w:t>'
            '<w:br/><w:t>see</w:t></w:r>'
            '<w:hyperlink r:id="rId9"><w:r><w:t xml:space="preserve"> the portal</w:t></w:r></w:hyperlink>'
            '<w:fldSimple w:instr=" PAGE "><w:r><w:t xml:space="preserve"> p4</w:t></w:r></w:fldSimple>'
            '<w:r><w:noBreakHyphen/><w:t>x</w:t><w:softHyphen/><w:t>y</w:t>'
            '<w:sym w:font="Wingdings" w:char="F0FC"/></w:r>')
        doc = self.extract(body)
        self.assertEqual(texts(doc), ["Table 3 tolerance±5 mm\nsee the portal p4-xy✓"])

    def test_contents_entries_skipped(self):
        toc_control = ('<w:sdt><w:sdtPr><w:docPartObj><w:docPartGallery w:val="Table of Contents"/>'
                       '</w:docPartObj></w:sdtPr><w:sdtContent>%s%s</w:sdtContent></w:sdt>'
                       % (b.para("Contents"), b.para("2 Design basis 4")))
        other_control = ('<w:sdt><w:sdtPr><w:alias w:val="Client"/></w:sdtPr><w:sdtContent>%s</w:sdtContent>'
                         '</w:sdt>' % b.para("Riverside Depot Pty Ltd"))
        body = (b.para("1 Introduction 3", "TOC1") + toc_control + other_control
                + b.para("Introduction", "Heading1") + b.para("Text."))
        doc = self.extract(body)
        self.assertEqual(texts(doc), ["Riverside Depot Pty Ltd", "Introduction", "Text."])

    def test_title_property_and_no_page_count(self):
        # docProps/app.xml page counts are often stale (files made by tools other than Word): unused.
        doc = self.extract(b.para("x"), title="Geotechnical Investigation", pages=38)
        self.assertEqual(doc["title"], "Geotechnical Investigation")
        self.assertIsNone(doc["pages"])

    def test_strict_namespace_and_odd_part_names(self):
        strict = "http://purl.oclc.org/ooxml/wordprocessingml/main"
        xml = b.document_xml(b.para("Strict heading", "Heading1") + b.para("Strict body")).replace(b.W_NS, strict)
        styles = b.styles_xml().replace(b.W_NS, strict)
        data = b.package("word/document.xml", "x", {
            "word/document.xml": xml,
            "word/Styles.XML": styles,
            "word/_rels/document.xml.rels": b.rels_xml([("rId1", "styles", "styles.xml")])})
        doc = docs.extract("strict.docx", data)
        self.assertEqual(blocks(doc), [("heading", "Strict heading", 1), ("para", "Strict body", 0)])

    def test_no_text(self):
        doc = self.extract(b.para(""))
        self.assertEqual(doc["status"], "no_text")

    def test_text_limit(self):
        body = "".join(b.para("Paragraph number %d with some words." % i) for i in range(2000))
        with mock.patch.object(docs, "DOC_TEXT_MAX", 1000):
            doc = docs.extract("big.docx", b.docx(body))
        self.assertEqual(doc["status"], "ok")
        self.assertLessEqual(sum(len(t) for t in texts(doc)), 1000)
        self.assertGreater(doc["chars"], 1000)

    def test_block_reaching_the_limit_is_cut_at_a_space(self):
        body = b.para("Short opening.") + b.para(" ".join("word%d" % i for i in range(400)))
        with mock.patch.object(docs, "DOC_TEXT_MAX", 1000):
            doc = docs.extract("big.docx", b.docx(body))
        kept = texts(doc)
        self.assertEqual(len(kept), 2)
        self.assertTrue(kept[1].endswith(" …"))
        self.assertLessEqual(sum(len(t) for t in kept), 1000)

    def test_damaged_styles_part_is_skipped(self):
        data = b.package("word/document.xml", "x", {
            "word/document.xml": b.document_xml(b.para("Still read", "Heading1")),
            "word/styles.xml": "<w:styles><broken",
            "word/_rels/document.xml.rels": b.rels_xml([("rId1", "styles", "styles.xml")])})
        doc = docs.extract("a.docx", data)
        self.assertEqual(blocks(doc), [("para", "Still read", 0)])


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------

class ExcelTests(unittest.TestCase):

    def test_values_dates_formulas_and_hidden_sheets(self):
        rows = [
            (1, [b.c_shared("A1", 0), b.c_shared("B1", 1), b.c_shared("C1", 2), b.c_shared("E1", 3)]),
            (2, [b.c_inline("A2", "Excavation"), b.c_num("B2", "45"), b.c_num("C2", "0.30000000000000004"),
                 b.c_num("E2", "45000", 1)]),
            (3, [b.c_inline("A3", "Fill"), b.c_formula("B3", "B2*2", "90"),
                 b.c_formula("C3", "C2/3", "0.1", style=7), b.c_num("E3", "45678.5", 4)]),
            (5, [b.c_inline("A5", "Total"), b.c_formula("B5", "SUM(B2:B3)", "135"), b.c_bool("C5", True),
                 b.c_error("D5", "#N/A"), b.c_num("E5", "0.35", 5)]),
            (6, [b.c_num("A6", "45000", 2), b.c_num("B6", "45000", 6), b.c_num("C6", "0.75", 3),
                 b.c_formula("D6", 'A1&"x"', "Itemx", kind="str"), b.c_formula("E6", "NOW()")]),
            (7, ['<c r="A7" t="d"><v>2025-03-04T00:00:00Z</v></c>', '<c r="B7" t="d"><v>2025-03-04T14:30:00</v></c>',
                 b.c_inline("C7", "Line_x000D_\nbreak"), b.c_num("D7", "1.5E-3"), b.c_num("E7", "123456789012")]),
        ]
        hidden = b.sheet_xml([(1, [b.c_inline("A1", "secret rates")])])
        data = b.xlsx([("Quantities", b.sheet_xml(rows), ""), ("Rates", hidden, "hidden"),
                       ("Old", hidden, "veryHidden")],
                      shared=["Item", "Qty", ["Ra", "te"], "Date"], title="Bill of quantities")
        doc = docs.extract("BoQ.xlsx", data)
        check_shape(self, doc)
        self.assertEqual(doc["pages"], 3)
        self.assertEqual(doc["title"], "Bill of quantities")
        self.assertEqual(blocks(doc), [
            ("sheet", "Quantities", 1),
            ("row", "Item | Qty | Rate | | Date", 0),
            ("row", "Excavation | 45 | 0.3 | | 2023-03-15", 1),
            ("row", "Fill | 90 | 0.1 | | 2025-01-21 12:00", 2),
            ("row", "Total | 135 | TRUE | #N/A | 35%", 3),
            ("row", "2023-03-15 | 2023-03-15 | 18:00 | Itemx", 4),
            ("row", "2025-03-04 | 2025-03-04 14:30 | Line break | 0.0015 | 123456789012", 5),
            ("sheet", "Rates", 2), ("sheet", "Old", 3)])
        self.assertEqual(doc["blocks"][0]["rows"], 6)
        self.assertTrue(doc["blocks"][-1]["hidden"])
        joined = " ".join(texts(doc))
        self.assertNotIn("SUM(", joined)
        self.assertNotIn("secret", joined)
        self.assertNotIn("PHONETIC", joined)

    def test_1904_date_system(self):
        data = b.xlsx([("S", b.sheet_xml([(1, [b.c_num("A1", "0", 1), b.c_num("B1", "43831", 1)])]), "")],
                      date1904=True)
        self.assertEqual(texts(docs.extract("a.xlsx", data), "row"), ["1904-01-01 | 2024-01-02"])

    def test_wide_sparse_sheet_drops_empty_columns(self):
        rows = [(1, [b.c_inline("A1", "Ref"), b.c_inline("XFD1", "Far right")]),
                (40, [b.c_inline("A40", "R1"), b.c_inline("M40", "middle")]),
                (900, [b.c_num("XFD900", "7")])]
        doc = docs.extract("a.xlsx", b.xlsx([("Sparse", b.sheet_xml(rows), "")]))
        self.assertEqual(texts(doc, "row"), ["Ref | | Far right", "R1 | middle", "| | 7"])
        self.assertEqual(doc["blocks"][0]["rows"], 3)

    def test_big_sheet_is_streamed_and_counted(self):
        rows = "".join('<row r="%d"><c r="A%d"><v>%d</v></c><c r="B%d" t="s"><v>%d</v></c></row>'
                       % (i, i, i, i, i % 2) for i in range(1, 60001))
        xml = '<worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>' % (b.S_NS, rows)
        data = b.xlsx([("Log", xml, ""), ("Second", b.sheet_xml([(1, [b.c_inline("A1", "still read")])]), "")],
                      shared=["even", "odd"])
        with mock.patch.object(docs, "SHEET_ROWS_MAX", 100), Timed(self, 10):
            doc = docs.extract("log.xlsx", data)
        sheets = [bl for bl in doc["blocks"] if bl["type"] == "sheet"]
        self.assertEqual([(s["text"], s["rows"]) for s in sheets], [("Log", 60000), ("Second", 1)])
        self.assertEqual(len(texts(doc, "row")), 101)
        self.assertEqual(texts(doc, "row")[:2], ["1 | odd", "2 | even"])
        self.assertEqual(texts(doc, "row")[-1], "still read")

    def test_text_limit_counts_rows_of_later_sheets(self):
        sheet = b.sheet_xml([(i, [b.c_inline("A%d" % i, "row text %d" % i)]) for i in range(1, 301)])
        with mock.patch.object(docs, "DOC_TEXT_MAX", 500):
            doc = docs.extract("a.xlsx", b.xlsx([("One", sheet, ""), ("Two", sheet, "")]))
        sheets = [bl for bl in doc["blocks"] if bl["type"] == "sheet"]
        self.assertEqual([(s["text"], s["rows"]) for s in sheets], [("One", 300), ("Two", 300)])
        self.assertLessEqual(sum(len(t) for t in texts(doc)), 500)

    def test_chart_sheet_and_missing_part(self):
        data = b.xlsx([("Data", b.sheet_xml([(1, [b.c_num("A1", "1")])]), "")])
        zf = zipfile.ZipFile(io.BytesIO(data))
        files = dict((n, zf.read(n)) for n in zf.namelist() if n != "xl/worksheets/sheet1.xml")
        doc = docs.extract("a.xlsx", b.make_zip(files))
        self.assertEqual(blocks(doc), [("sheet", "Data", 1)])
        self.assertEqual(doc["status"], "no_text")

    def test_number_formatting(self):
        cases = [("0.30000000000000004", "0.3"), ("45", "45"), ("45.0", "45"), ("-0.0", "0"),
                 ("1234567.891", "1234567.891"), ("4.0000000000000001E-2", "0.04"),
                 ("1.5E-3", "0.0015"), ("1E+20", "1e+20"), ("123456789012", "123456789012"),
                 ("2.9999999999999996", "3"), ("not a number", "not a number")]
        for raw, expected in cases:
            self.assertEqual(docs.format_number(raw), expected, raw)

    def test_excel_dates(self):
        self.assertEqual(docs.excel_date(1), "1900-01-01")
        self.assertEqual(docs.excel_date(59), "1900-02-28")
        self.assertEqual(docs.excel_date(61), "1900-03-01")
        self.assertEqual(docs.excel_date(45000), "2023-03-15")
        self.assertEqual(docs.excel_date(45000.75, kind="datetime"), "2023-03-15 18:00")
        self.assertEqual(docs.excel_date(45000.75, kind="date"), "2023-03-15")
        self.assertEqual(docs.excel_date(0.5, kind="time"), "12:00")
        self.assertEqual(docs.excel_date(1.5625, kind="elapsed"), "37:30")
        self.assertEqual(docs.excel_date(0, True), "1904-01-01")
        self.assertIsNone(docs.excel_date(-1))
        self.assertIsNone(docs.excel_date(3000000))

    def test_format_kinds(self):
        self.assertEqual(docs._format_kind("dd/mm/yyyy"), "date")
        self.assertEqual(docs._format_kind("[$-C09]dddd, d mmmm yyyy;@"), "date")
        self.assertEqual(docs._format_kind("mmm-yy"), "date")
        self.assertEqual(docs._format_kind("d/mm/yy h:mm"), "datetime")
        self.assertEqual(docs._format_kind("h:mm AM/PM"), "time")
        self.assertEqual(docs._format_kind("[h]:mm:ss"), "elapsed")
        self.assertEqual(docs._format_kind("0.0%"), "percent:1")
        self.assertEqual(docs._format_kind("0%"), "percent:0")
        self.assertIsNone(docs._format_kind('"$"#,##0.00;[Red]\\-"$"#,##0.00'))
        self.assertIsNone(docs._format_kind("#,##0 \"days\""))
        self.assertIsNone(docs._format_kind("General"))
        self.assertIsNone(docs._format_kind("0.00E+00"))


# --------------------------------------------------------------------------
# PowerPoint
# --------------------------------------------------------------------------

class PowerPointTests(unittest.TestCase):

    def test_slides_in_order_with_titles_tables_and_notes(self):
        data = b.pptx([
            {"title": "Project update", "body": ["Piling complete", "Deck pour 12 March"],
             "notes": "Mention the RFI 12 delay", "footer": "Riverside Depot - confidential"},
            {"title": "Costs", "table": [["Item", "Cost"], ["Piles", "$120,000"]]},
            {"body": ["Slide without a title"]},
        ], title="Monthly update")
        doc = docs.extract("Update.pptx", data)
        check_shape(self, doc)
        self.assertEqual(doc["pages"], 3)
        self.assertEqual(doc["title"], "Monthly update")
        self.assertEqual(blocks(doc), [
            ("slide", "Project update", 1), ("para", "Piling complete", 0), ("para", "Deck pour 12 March", 0),
            ("para", "Notes: Mention the RFI 12 delay", 0),
            ("slide", "Costs", 2), ("row", "Item | Cost", 0), ("row", "Piles | $120,000", 1),
            ("slide", "", 3), ("para", "Slide without a title", 0)])


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

A4 = (595.0, 842.0)
A3 = (1191.0, 842.0)

REPORT_PAGE = """RIVERSIDE DEPOT GEOTECHNICAL INVESTIGATION
1 Introduction
This report presents the results of a geotechnical investigation carried out for the
proposed depot upgrade at the Riverside site. The fieldwork comprised six boreholes
drilled to depths of up to 12 m.
6.2 Allowable bearing pressure
An allowable bearing pressure of 150 kPa may be adopted for pad footings founded in
stiff clay at a depth of at least 1.2 m below finished surface level.
• Groundwater was encountered at 2.4 m.
• Excavations deeper than 1.5 m require shoring.
Page 3 of 12"""


def fake_reader(pages, sizes=None, title="", status="ok", note=""):
    """A stand-in for the PDF backends: returns a fixed result."""
    result = {"pages": pages, "page_sizes": sizes if sizes is not None else [A4] * len(pages),
              "status": status, "note": note, "title": title, "count": len(pages)}
    return mock.patch.multiple(docs, _get_pypdf=mock.Mock(return_value=None),
                               _pdf_with_pdftext=mock.Mock(return_value=result))


class PdfLogicTests(unittest.TestCase):
    """PDF handling with the backend replaced (no PDF file needed)."""

    def test_page_text_becomes_headings_paragraphs_and_items(self):
        with fake_reader([REPORT_PAGE], title="Microsoft Word - Geotech Report.docx"):
            doc = docs.extract("Geotech Report Rev B.pdf", b"%PDF-1.4 fake")
        check_shape(self, doc)
        self.assertEqual(doc["kind"], "pdf")
        self.assertEqual(doc["reader"], "pdftext")
        self.assertEqual(doc["title"], "Geotech Report.docx")
        self.assertEqual(blocks(doc), [
            ("page", "", 1),
            ("heading", "RIVERSIDE DEPOT GEOTECHNICAL INVESTIGATION", 1),
            ("heading", "1 Introduction", 1),
            ("para", "This report presents the results of a geotechnical investigation carried out for the "
                     "proposed depot upgrade at the Riverside site. The fieldwork comprised six boreholes "
                     "drilled to depths of up to 12 m.", 0),
            ("heading", "6.2 Allowable bearing pressure", 2),
            ("para", "An allowable bearing pressure of 150 kPa may be adopted for pad footings founded in "
                     "stiff clay at a depth of at least 1.2 m below finished surface level.", 0),
            ("item", "• Groundwater was encountered at 2.4 m.", 0),
            ("item", "• Excavations deeper than 1.5 m require shoring.", 0),
            ("para", "Page 3 of 12", 0)])
        self.assertFalse(doc["drawing"])

    def test_blank_lines_end_paragraphs(self):
        page = ("Header line\n\nA first paragraph that runs the whole width of the page and wraps\n"
                "onto a second line.\n\nA second paragraph that also runs the whole width and\n"
                "wraps too.\n\nFooter")
        with fake_reader([page]):
            doc = docs.extract("r.pdf", b"%PDF-1.4")
        self.assertEqual(texts(doc, "para"), [
            "Header line",
            "A first paragraph that runs the whole width of the page and wraps onto a second line.",
            "A second paragraph that also runs the whole width and wraps too.", "Footer"])

    def test_dates_and_priced_rows_are_not_headings(self):
        page = ("BEDROCK LABS - FEE QUOTATION Q-2291\n18 September 2024\nDear Sam,\n"
                "Thank you for the opportunity to quote for the investigation described in your request.\n"
                "1 Service location and site walkover $2,450.00\n"
                "2 Drilling 12 boreholes to 6-9.5 m incl. rig mobilisation $19,800.00\n"
                "Total (excl. GST) $38,940.00\nThe quote is valid for 60 days.\nPage 1 of 1")
        with fake_reader([page]):
            doc = docs.extract("Quote.pdf", b"%PDF-1.4")
        self.assertEqual(texts(doc, "heading"), ["BEDROCK LABS - FEE QUOTATION Q-2291"])
        # A row that ends in an amount is not run on into the next line.
        paras = texts(doc, "para")
        self.assertIn("2 Drilling 12 boreholes to 6-9.5 m incl. rig mobilisation $19,800.00", paras)
        self.assertIn("Total (excl. GST) $38,940.00", paras)
        self.assertEqual(docs._pdf_heading("6.2 Allowable bearing pressure"), 2)

    def test_contents_lines_are_not_headings_or_joined(self):
        page = ("Contents\n1 Introduction .................................................. 1\n"
                "2 Site description ........................................... 2\n"
                "2.1 Geology ................................................. 3")
        with fake_reader([page]):
            doc = docs.extract("r.pdf", b"%PDF-1.4")
        self.assertEqual(texts(doc, "para"), [
            "1 Introduction .................................................. 1",
            "2 Site description ........................................... 2",
            "2.1 Geology ................................................. 3"])

    def test_scanned_pdf_is_no_text(self):
        with fake_reader(["", " ", "12"], sizes=[A4] * 3):
            doc = docs.extract("Scan 0001.pdf", b"%PDF-1.4")
        self.assertEqual(doc["status"], "no_text")
        self.assertEqual(doc["note"], "scanned or image-only PDF")
        self.assertEqual(doc["pages"], 3)

    def test_protected_and_damaged(self):
        with fake_reader([], status="protected", note="password-protected PDF"):
            doc = docs.extract("x.pdf", b"%PDF-1.4")
        self.assertEqual((doc["status"], doc["note"]), ("protected", "password-protected PDF"))
        with fake_reader([], status="error", note="damaged PDF"):
            self.assertEqual(docs.extract("x.pdf", b"%PDF-1.4")["status"], "error")

    def test_no_reader_available(self):
        with mock.patch.multiple(docs, _get_pypdf=mock.Mock(return_value=None),
                                 _pdf_with_pdftext=mock.Mock(return_value=None)):
            doc = docs.extract("x.pdf", b"%PDF-1.4")
        self.assertEqual((doc["status"], doc["note"]), ("unsupported", "PDF reader not available"))

    def test_drawing_by_page_size(self):
        with fake_reader(["GENERAL ARRANGEMENT PLAN SCALE 1:100 REV C"] * 8, sizes=[A3] * 7 + [A4]):
            self.assertTrue(docs.extract("Riverside set.pdf", b"%PDF-1.4")["drawing"])
        with fake_reader(["Report text " * 10] * 8, sizes=[A4] * 6 + [A3] * 2):
            self.assertFalse(docs.extract("Riverside report.pdf", b"%PDF-1.4")["drawing"])

    def test_drawing_by_name_needs_few_pages(self):
        name = "623.0001-ST-1200 DEPOT SLAB SETOUT PLAN [H].pdf"
        with fake_reader(["Title block text here"] * 2):
            self.assertTrue(docs.extract(name, b"%PDF-1.4")["drawing"])
        with fake_reader(["Title block text here"] * 6):
            self.assertFalse(docs.extract(name, b"%PDF-1.4")["drawing"])

    def test_drawing_names(self):
        drawings = [
            "623.0001-ST-1200 DEPOT SLAB SETOUT PLAN [H].pdf",
            "RVD-C-0101 Stormwater layout.pdf", "SK01 Headwall detail.pdf", "SK-003.pdf",
            "S-100 General notes.pdf", "A101.pdf", "12345-CI-DRG-0001[C].pdf", "DWG Site plan.pdf",
            "Riverside drawings Rev C.pdf", "623.0001 Retaining wall elevations [B].pdf",
            "Sketch - culvert headwall.pdf", "ST1201 Rev C.pdf", "Bridge GA 4410 Rev H.pdf",
        ]
        others = [
            "Geotechnical Investigation Report Rev B.pdf", "Drawing Register Rev 3.pdf", "Transmittal 0042.pdf",
            "Invoice 1234.pdf", "Meeting minutes 2025-03-04.pdf", "Appendix A - Borehole logs.pdf",
            "M12 bolt specification.pdf", "Main St 12 photos.pdf", "Fee proposal Rev A.pdf",
            "RFI 012 response.pdf", "Site photos [1].pdf", "Report.pdf", "Structural calcs Rev 2.pdf",
            "Programme 2025.pdf", "Letter to council.pdf", "Scan 0001.pdf",
        ]
        for name in drawings:
            self.assertTrue(docs.looks_like_drawing_name(name), name)
        for name in others:
            self.assertFalse(docs.looks_like_drawing_name(name), name)

    def test_pypdf_that_hangs_is_abandoned(self):
        class Page(object):
            mediabox = mock.Mock(width=595, height=842)

            def __init__(self, n):
                self.n = n

            def extract_text(self):
                if self.n >= 2:
                    time.sleep(3)
                return "Page %d text with enough characters to count as text." % (self.n + 1)

        class Reader(object):
            is_encrypted = False
            metadata = None

            def __init__(self, stream, strict=False):
                self.pages = [Page(n) for n in range(5)]

        fake = mock.Mock(PdfReader=Reader, __version__="0")
        with mock.patch.object(docs, "_get_pypdf", return_value=fake), \
                mock.patch.object(docs, "PDF_TIME_MAX", 0.3), Timed(self, 3):
            doc = docs.extract("slow.pdf", b"%PDF-1.4")
        self.assertEqual(doc["reader"], "pypdf")
        self.assertEqual(doc["pages"], 5)
        self.assertEqual(doc["note"], "stopped after 2 pages (slow to read)")
        self.assertIn("Page 2 text", " ".join(texts(doc)))

    def test_pypdf_that_cannot_open_falls_back(self):
        fake = mock.Mock(PdfReader=mock.Mock(side_effect=ValueError("bad")), __version__="0")
        result = {"pages": ["Built-in reader text that is long enough."], "page_sizes": [A4],
                  "status": "ok", "note": "", "title": "", "count": 1}
        with mock.patch.multiple(docs, _get_pypdf=mock.Mock(return_value=fake),
                                 _pdf_with_pdftext=mock.Mock(return_value=result)):
            doc = docs.extract("x.pdf", b"%PDF-1.4")
        self.assertEqual((doc["reader"], doc["status"]), ("pdftext", "ok"))

    def test_backend_status(self):
        with mock.patch.dict(os.environ, {"SQUISH_NO_PYPDF": "1"}):
            self.assertEqual(docs.backend_status(), "PDF: built-in reader (install pypdf for best results)")
        module = mock.Mock(__version__="5.1.0")
        with mock.patch.object(docs, "_get_pypdf", return_value=module):
            self.assertEqual(docs.backend_status(), "PDF: pypdf 5.1.0")


@unittest.skipIf(pdf_builder is None, "tests/pdf_builder.py not available yet")
class PdfFileTests(unittest.TestCase):
    """Real (tiny, hand-written) PDF files through whichever readers are installed."""

    def make(self, pages, size=A4, **options):
        return pdf_builder.simple_pdf(pages, size=size, **options)

    def readers(self):
        found = [("builtin", {"SQUISH_NO_PYPDF": "1"})]
        if importlib.util.find_spec("pypdf") is not None:
            found.append(("pypdf", {}))
        return found

    def test_text_pdf(self):
        data = self.make(["Allowable bearing pressure 150 kPa at 1.2 m depth.", "Second page text here please."])
        for label, env in self.readers():
            with mock.patch.dict(os.environ, env):
                with mock.patch.dict(docs._pypdf_state, {"checked": False, "module": None}):
                    doc = docs.extract("Report.pdf", data)
            check_shape(self, doc)
            self.assertEqual(doc["status"], "ok", label)
            self.assertEqual(doc["pages"], 2, label)
            joined = " ".join(texts(doc))
            self.assertIn("150 kPa", joined, label)
            self.assertIn("Second page", joined, label)

    def each_reader(self, data, name="Report.pdf"):
        """(reader label, DocText) for the built-in reader and pypdf (when installed)."""
        results = []
        for label, env in self.readers():
            with mock.patch.dict(os.environ, env):
                with mock.patch.dict(docs._pypdf_state, {"checked": False, "module": None}):
                    doc = docs.extract(name, data)
            check_shape(self, doc)
            results.append((label, doc))
        return results

    def test_title_property(self):
        data = self.make(["Some text on the page for the title test."], title="Microsoft Word - Geotech.docx")
        for label, doc in self.each_reader(data):
            self.assertEqual(doc["title"], "Geotech.docx", label)

    def test_encrypted_pdf_is_protected(self):
        data = self.make(["Secret text"], encrypt=True)
        for label, doc in self.each_reader(data):
            self.assertIn(doc["status"], ("protected", "error"), label)
            self.assertNotIn("Secret", " ".join(texts(doc)), label)

    def test_page_without_text_is_no_text(self):
        for label, doc in self.each_reader(self.make(["", ""])):
            self.assertEqual(doc["status"], "no_text", label)
            self.assertEqual(doc["pages"], 2, label)

    def test_a3_pdf_is_drawing(self):
        data = self.make(["GENERAL ARRANGEMENT PLAN SCALE 1:100 DRAWN SB CHECKED RC REV C"], size=A3)
        doc = docs.extract("Plan.pdf", data)
        self.assertTrue(doc["drawing"])

    def test_pdf_named_docx(self):
        doc = docs.extract("Not really.docx", self.make(["Bearing pressure 150 kPa in stiff clay."]))
        self.assertEqual(doc["kind"], "pdf")
        self.assertIn("150 kPa", " ".join(texts(doc)))


# --------------------------------------------------------------------------
# Text, Markdown, CSV, RTF
# --------------------------------------------------------------------------

class TextTests(unittest.TestCase):

    def test_text_mentioning_pdf_is_still_text(self):
        doc = docs.extract("notes.txt", b"Saved as %PDF-1.4 by the scanner")
        self.assertEqual((doc["kind"], texts(doc)), ("text", ["Saved as %PDF-1.4 by the scanner"]))

    def test_plain_text_encodings(self):
        self.assertEqual(texts(docs.extract("a.txt", "\ufeffCafé 25 m³\nnext line\n\nPara two".encode("utf-8"))),
                         ["Café 25 m³\nnext line", "Para two"])
        self.assertEqual(texts(docs.extract("a.txt", "Café – 5°".encode("cp1252"))), ["Café – 5°"])
        self.assertEqual(texts(docs.extract("a.txt", "Notes 20 kPa".encode("utf-16"))), ["Notes 20 kPa"])
        self.assertEqual(docs.extract("a.txt", "- first\n- second".encode())["blocks"][0]["type"], "item")

    def test_markdown(self):
        md = "# Site notes\n\nSome text.\n\n## Actions\n- Sam to check levels\n\n| Item | Qty |\n|---|---|\n| Pit | 2 |\n"
        doc = docs.extract("notes.md", md.encode())
        self.assertEqual(blocks(doc), [("heading", "Site notes", 1), ("para", "Some text.", 0),
                                       ("heading", "Actions", 2), ("item", "- Sam to check levels", 0),
                                       ("row", "Item | Qty", 0), ("row", "Pit | 2", 1)])

    def test_csv(self):
        data = "Item;Qty;Rate\nExcavation;45;12,50\n;;\nFill;10;\n".encode("cp1252")
        doc = docs.extract("BoQ export.csv", data)
        self.assertEqual(doc["kind"], "text")
        self.assertEqual(blocks(doc), [("sheet", "BoQ export.csv", 1), ("row", "Item | Qty | Rate", 0),
                                       ("row", "Excavation | 45 | 12,50", 1), ("row", "Fill | 10", 2)])
        self.assertEqual(doc["blocks"][0]["rows"], 3)

    def test_rtf(self):
        rtf = (b"{\\rtf1\\ansi\\ansicpg1252{\\fonttbl{\\f0 Arial;}}\\f0 Bearing pressure 150 kPa\\par "
               b"Second paragraph \\'96 caf\\'e9\\par}")
        doc = docs.extract("memo.rtf", rtf)
        self.assertEqual(texts(doc), ["Bearing pressure 150 kPa", "Second paragraph – café"])

    def test_rtf_table_rows_end_without_a_separator(self):
        rtf = (b"{\\rtf1\\ansi{\\fonttbl{\\f0 Arial;}}\\trowd\\cellx2000\\cellx6000 "
               b"\\intbl Project\\cell \\intbl Riverside Depot\\cell \\row "
               b"Provide 2-N20 trimmer bars.\\par}")
        doc = docs.extract("SI-012.rtf", rtf)
        self.assertEqual(texts(doc), ["Project | Riverside Depot", "Provide 2-N20 trimmer bars."])


# --------------------------------------------------------------------------
# Zip
# --------------------------------------------------------------------------

class ZipTests(unittest.TestCase):

    def test_members_and_nested_documents(self):
        data = b.make_zip({
            "Reports/Geotech.docx": b.docx(b.para("Bearing pressure 150 kPa")),
            "photos/IMG_0012.jpg": b"\xff\xd8\xff junk",
            "inner.zip": b.make_zip({"deeper.docx": b.docx(b.para("too deep"))}),
            "__MACOSX/._Geotech.docx": b"junk",
            "notes.txt": "Plain note",
        })
        doc = docs.extract("transmittal.zip", data)
        check_shape(self, doc)
        self.assertEqual(doc["kind"], "zip")
        members = [bl for bl in doc["blocks"] if bl["type"] == "member"]
        self.assertEqual([m["text"] for m in members],
                         ["Reports/Geotech.docx", "photos/IMG_0012.jpg", "inner.zip", "notes.txt"])
        self.assertEqual(texts(members[0]["doc"]), ["Bearing pressure 150 kPa"])
        self.assertNotIn("doc", members[1])
        self.assertNotIn("doc", members[2])          # one level only
        self.assertEqual(texts(members[3]["doc"]), ["Plain note"])
        self.assertEqual(members[1]["size"], 8)

    def test_zip_with_office_like_folder_names_is_a_zip(self):
        data = b.make_zip({"Word/Report.docx": b.docx(b.para("Inside")), "XL/book.txt": "x"})
        doc = docs.extract("Reports.zip", data)
        self.assertEqual(doc["kind"], "zip")
        self.assertEqual(texts(doc["blocks"][0]["doc"]), ["Inside"])

    def test_name_cap_keeps_documents(self):
        files = dict(("photos/IMG_%04d.jpg" % i, b"x") for i in range(100))
        files["zz report.docx"] = b.docx(b.para("Found it"))
        doc = docs.extract("photos.zip", b.make_zip(files))
        members = [bl for bl in doc["blocks"] if bl["type"] == "member"]
        self.assertEqual(len(members), docs.ZIP_NAMES_MAX + 1)
        self.assertEqual(texts(members[-1]["doc"]), ["Found it"])
        self.assertEqual(texts(doc, "para"), ["(+40 more files)"])

    def test_document_count_cap(self):
        files = dict(("note %d.txt" % i, "Note %d" % i) for i in range(5))
        with mock.patch.object(docs, "ZIP_DOCS_MAX", 3):
            doc = docs.extract("notes.zip", b.make_zip(files))
        statuses = [bl["doc"]["status"] for bl in doc["blocks"]]
        self.assertEqual(statuses, ["ok", "ok", "ok", "too_big", "too_big"])

    def test_encrypted_member(self):
        data = bytearray(b.make_zip({"secret.docx": b.docx(b.para("x")), "open.txt": "visible"}))
        # Set the "encrypted" flag on the first member (local and central headers).
        for sig, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            at = data.find(sig)
            data[at + offset] |= 1
        doc = docs.extract("a.zip", bytes(data))
        member = doc["blocks"][0]
        self.assertEqual(member["doc"]["status"], "protected")
        self.assertEqual(texts(doc["blocks"][1]["doc"]), ["visible"])


# --------------------------------------------------------------------------
# Hostile and damaged input
# --------------------------------------------------------------------------

LAUGHS = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
          '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
          '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>'
          '<w:document xmlns:w="%s"><w:body><w:p><w:r><w:t>&lol3;</w:t></w:r></w:p></w:body></w:document>' % b.W_NS)


def zip_with_member(name, data, declared_size=None):
    """A zip holding one member; ``declared_size`` overwrites the sizes in its headers."""
    raw = bytearray(b.make_zip({"[Content_Types].xml": "<Types/>", name: data}))
    if declared_size is not None:
        at = raw.rfind(b"PK\x03\x04", 0, raw.find(name.encode()))
        struct.pack_into("<I", raw, at + 22, declared_size)
        cd = raw.rfind(b"PK\x01\x02", 0, raw.rfind(name.encode()))
        struct.pack_into("<I", raw, cd + 24, declared_size)
    return bytes(raw)


class HostileTests(unittest.TestCase):

    def assertStatus(self, doc, status, note_part=""):
        check_shape(self, doc)
        self.assertEqual(doc["status"], status, doc["note"])
        self.assertIn(note_part, doc["note"])

    def test_billion_laughs_in_main_part(self):
        with Timed(self, 2):
            doc = docs.extract("a.docx", b.docx("", raw_document=LAUGHS))
        self.assertStatus(doc, "error", "DTD")

    def test_entity_declaration_in_other_encodings(self):
        utf16 = LAUGHS.replace('<?xml version="1.0"?>', "").encode("utf-16")
        self.assertStatus(docs.extract("a.docx", b.docx("", raw_document=utf16)), "error", "DTD")
        ebcdic = (b'<?xml version="1.0" encoding="cp500"?>'
                  + '<!DOCTYPE x [<!ENTITY a "b">]><w:document xmlns:w="x"/>'.encode("cp500"))
        self.assertStatus(docs.extract("a.docx", b.docx("", raw_document=ebcdic)), "error", "encoding")

    def test_billion_laughs_in_shared_strings_is_skipped(self):
        data = b.xlsx([("S", b.sheet_xml([(1, [b.c_inline("A1", "inline ok"), b.c_shared("B1", 0)])]), "")],
                      shared=["x"])
        zf = zipfile.ZipFile(io.BytesIO(data))
        files = dict((n, zf.read(n)) for n in zf.namelist())
        files["xl/sharedStrings.xml"] = LAUGHS.replace("w:document", "sst").encode()
        doc = docs.extract("a.xlsx", b.make_zip(files))
        self.assertStatus(doc, "ok")
        self.assertEqual(texts(doc, "row"), ["inline ok"])

    def test_zip_bomb_declared_size(self):
        bomb = zip_with_member("word/document.xml", b"<w:document>" + b" " * (80 * 1024 * 1024))
        with Timed(self, 3):
            doc = docs.extract("bomb.docx", bomb)
        self.assertIn(doc["status"], ("too_big", "error"))

    def test_zip_bomb_lying_about_its_size(self):
        body = b'<w:document xmlns:w="%s"><w:body><w:p><w:r><w:t>' % b.W_NS.encode()
        bomb = zip_with_member("word/document.xml", body + b"a" * (20 * 1024 * 1024) + b"</w:t></w:r></w:p></w:body>"
                               b"</w:document>", declared_size=5000)
        with Timed(self, 3):
            doc = docs.extract("bomb.docx", bomb)
        self.assertStatus(doc, "error")

    def test_compression_ratio_guard(self):
        data = zip_with_member("word/document.xml", b"<a>" + b"\0" * (30 * 1024 * 1024) + b"</a>")
        with mock.patch.object(docs, "RATIO_MAX", 50), Timed(self, 3):
            doc = docs.extract("a.docx", data)
        self.assertStatus(doc, "error", "zip bomb")

    def test_deep_nesting(self):
        deep = '<w:document xmlns:w="%s"><w:body>%s%s</w:body></w:document>' % (
            b.W_NS, "<w:p>" * 200000, "</w:p>" * 200000)
        with Timed(self, 3):
            doc = docs.extract("a.docx", b.docx("", raw_document=deep))
        self.assertStatus(doc, "error", "nested")

    def test_huge_number_of_elements_is_bounded(self):
        many = '<w:document xmlns:w="%s"><w:body><w:p>%s</w:p></w:body></w:document>' % (
            b.W_NS, "<w:r/>" * 3000000)
        with Timed(self, 20):
            doc = docs.extract("a.docx", b.docx("", raw_document=many))
        self.assertStatus(doc, "too_big", "too much XML")

    def test_damaged_and_empty_files(self):
        good = b.docx(b.para("hello"))
        self.assertStatus(docs.extract("a.docx", good[: len(good) // 2]), "error")
        self.assertStatus(docs.extract("a.docx", b""), "no_text", "empty")
        self.assertStatus(docs.extract("a.docx", b"just some text"), "error", "not a valid Word file")
        self.assertStatus(docs.extract("a.xlsx", b"PK\x03\x04garbage"), "error")
        self.assertStatus(docs.extract("a.pptx", b.make_zip({"x.txt": "x"})), "error")
        bad_xml = b.docx("", raw_document=b'<w:document xmlns:w="x"><w:body><w:p>\xff\xfe</w:p>')
        self.assertStatus(docs.extract("a.docx", bad_xml), "error", "damaged")

    def test_wrong_extensions_use_magic_bytes(self):
        self.assertEqual(docs.extract("report.pdf", b.docx(b.para("Actually Word")))["kind"], "docx")
        self.assertEqual(docs.extract("book.docx", b.xlsx([("S", None, "")]))["kind"], "xlsx")
        old = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 600
        doc = docs.extract("old.docx", old)
        self.assertEqual((doc["status"], doc["note"]), ("unsupported", "old Word format (.doc) renamed .docx, not read"))
        enc = old + "EncryptedPackage".encode("utf-16-le")
        self.assertStatus(docs.extract("locked.xlsx", enc), "protected")
        doc = docs.extract("pdf.docx", b"%PDF-1.4\n%%EOF")
        self.assertEqual(doc["kind"], "pdf")

    def test_unsupported_types(self):
        for name, note in (("Old report.doc", "old Word format (.doc), not read"),
                           ("Site.dwg", "CAD drawing, not read"), ("photo.jpg", "file type not read")):
            doc = docs.extract(name, b"anything")
            self.assertEqual((doc["kind"], doc["status"], doc["note"]), ("other", "unsupported", note))

    def test_too_many_zip_entries(self):
        files = dict(("f%d.txt" % i, b"") for i in range(docs.ZIP_ENTRIES_MAX + 1))
        self.assertStatus(docs.extract("many.zip", b.make_zip(files, zipfile.ZIP_STORED)), "error", "too many")

    def test_random_damage_never_raises(self):
        samples = {
            "a.docx": b.docx(b.para("Heading", "Heading1") + b.table([["a", "b"]]), numbering=True,
                             footnotes={"2": "note"}),
            "a.xlsx": b.xlsx([("S", b.sheet_xml([(1, [b.c_shared("A1", 0), b.c_num("B1", "1", 1)])]), "")],
                             shared=["x"]),
            "a.pptx": b.pptx([{"title": "T", "body": ["x"], "notes": "n"}]),
            "a.zip": b.make_zip({"a.docx": b.docx(b.para("x")), "b.txt": "y"}),
            "a.rtf": b"{\\rtf1\\ansi{\\fonttbl{\\f0 Arial;}}hello\\par}",
            "a.csv": b"a,b\n1,2\n",
        }
        rng = random.Random(1234)
        with Timed(self, 60):
            for name, data in samples.items():
                for _ in range(60):
                    damaged = bytearray(data)
                    for _ in range(rng.randint(1, 8)):
                        damaged[rng.randrange(len(damaged))] = rng.randrange(256)
                    if rng.random() < 0.3:
                        damaged = damaged[: rng.randrange(len(damaged))]
                    check_shape(self, docs.extract(name, bytes(damaged)))


class FileInputTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_path_input(self):
        path = os.path.join(self.tmp, "Report.docx")
        with open(path, "wb") as f:
            f.write(b.docx(b.para("From disk")))
        self.assertEqual(texts(docs.extract("Report.docx", path=path)), ["From disk"])

    def test_too_big_file_is_not_read(self):
        path = os.path.join(self.tmp, "Huge.pdf")
        with open(path, "wb") as f:
            f.truncate(docs.DOC_MAX_BYTES + 1)
        doc = docs.extract("Huge.pdf", path=path)
        self.assertEqual(doc["status"], "too_big")
        self.assertEqual(docs.extract("Huge.pdf", b"x" * (docs.DOC_MAX_BYTES + 1))["status"], "too_big")

    def test_missing_file(self):
        doc = docs.extract("Gone.docx", path=os.path.join(self.tmp, "nope.docx"))
        self.assertEqual((doc["status"], doc["note"]), ("error", "could not open the file"))

    def test_supported_extensions(self):
        self.assertIn(".docx", docs.SUPPORTED_EXT)
        self.assertIn(".pdf", docs.SUPPORTED_EXT)
        self.assertNotIn(".doc", docs.SUPPORTED_EXT)
        self.assertTrue(docs.is_supported("C:\\Jobs\\X\\Report.DOCX"))
        self.assertFalse(docs.is_supported("photo.jpg"))


if __name__ == "__main__":
    unittest.main()
