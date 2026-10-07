"""Tests for docs.py: Word, Excel, PowerPoint, PDF, text and zip files (all synthetic).

Office files are built with tests/doc_builder.py (standard library only); PDFs
with tests/pdf_builder.py when it exists. Hostile inputs (entity bombs, zip
bombs, deep nesting, damaged files) must give a status, never an exception,
and must finish quickly.
"""

import hashlib
import importlib.util
import io
import json
import os
import random
import shutil
import struct
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock

from squish_app import docs, msgfile
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
    """Every DocText has the contract's keys (plus "retry" when a time limit cut
    it short, and a PDF's "large_pages" and "prose_pages") and is JSON-serialisable."""
    test.assertEqual(set(doc) - set(["retry", "large_pages", "prose_pages"]), DOC_KEYS)
    test.assertIn(doc["status"], STATUSES)
    json.dumps(doc)
    for bl in doc["blocks"]:
        test.assertIn(bl["type"], ("heading", "para", "item", "row", "sheet", "page", "slide", "member"))
        test.assertIsInstance(bl["text"], str)
        test.assertIsInstance(bl["level"], int)
        if bl.get("doc"):
            check_shape(test, bl["doc"])


def wait_for_pypdf():
    """Let pypdf reads that a test abandoned (PDF_TIME_MAX) finish, so later
    tests get pypdf again."""
    for thread in list(docs._pypdf_abandoned):
        thread.join(10)


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

    def test_symbol_and_wingdings_characters(self):
        def sym(font, code):
            return '<w:sym w:font="%s" w:char="%s"/>' % (font, code)
        symbol = "".join(sym("Symbol", c) for c in ("F079", "F06E", "F06A", "F0C6", "F0E5", "F0A2", "F0B1"))
        boxes = "".join(sym("Wingdings 2", c) for c in ("F052", "F054", "F0A3", "F050", "F04F"))
        body = (b.para("<w:r>%s</w:r>" % symbol)
                + b.para("<w:r>%s%s</w:r>" % (boxes, sym("Wingdings", "F0FC"))))
        self.assertEqual(texts(self.extract(body)), ["ψνϕ∅∑′±", "☑☒☐✓✗✓"])

    def test_equations_are_written_on_one_line(self):
        def frac(num, den, kind=None):
            pr = '<m:fPr><m:type m:val="%s"/><m:ctrlPr><w:rPr><w:i/></w:rPr></m:ctrlPr></m:fPr>' % kind if kind else ""
            return "<m:f>%s<m:num>%s</m:num><m:den>%s</m:den></m:f>" % (pr, num, den)

        def sup(base, power):
            return "<m:sSup><m:e>%s</m:e><m:sup>%s</m:sup></m:sSup>" % (base, power)
        r = b.mr
        cases = [
            (b.math(r("σ="), frac(r("P"), r("A")), r("="), frac(r("450"), r("0.09"))), "σ=P/A=450/0.09"),
            (b.math(frac(r("12.5×") + sup(r("8.0"), r("2")), r("8"))), "(12.5×8.0^2)/8"),
            (b.math(r("M*="), frac(r("w") + sup(r("L"), r("2")), r("8"))), "M*=wL^2/8"),
            (b.math(r("0.17"), '<m:rad><m:radPr><m:degHide m:val="1"/></m:radPr><m:deg/><m:e>%s</m:e></m:rad>'
                    % r("f'c"), r("bd")), "0.17√(f'c)bd"),
            (b.math('<m:rad><m:deg>%s</m:deg><m:e>%s</m:e></m:rad>' % (r("3"), r("27"))), "∛27"),
            (b.math("<m:sSub><m:e>%s</m:e><m:sub>%s</m:sub></m:sSub>" % (r("V"), r("uc"))), "V_uc"),
            (b.math("<m:d><m:dPr><m:ctrlPr/></m:dPr><m:e>%s</m:e></m:d>" % r("a+b"), sup(r(""), r(""))), "(a+b)"),
            (b.math('<m:d><m:dPr><m:begChr m:val="["/><m:sepChr m:val=";"/><m:endChr m:val=""/></m:dPr>'
                    '<m:e>%s</m:e><m:e>%s</m:e></m:d>' % (r("1"), r("2"))), "[1;2"),
            (b.math('<m:nary><m:naryPr><m:chr m:val="∑"/></m:naryPr><m:sub>%s</m:sub><m:sup>%s</m:sup>'
                    '<m:e>%s</m:e></m:nary>' % (r("i=1"), r("n"), r("x_i"))), "∑_(i=1)^n x_i"),
            (b.math(frac(r("n"), r("k"), "noBar")), "(n; k)"),
            (b.math('<m:func><m:fName>%s</m:fName><m:e>%s</m:e></m:func>' % (r("sin"), r("θ"))), "sin θ"),
        ]
        for xml, expected in cases:
            self.assertEqual(texts(self.extract(b.para(xml))), [expected], expected)
        para = b.para(b.run("EQ: ") + '<m:oMathPara>%s%s</m:oMathPara>'
                      % (b.math(r("x=1")), b.math(r("y="), frac(r("1"), r("2")))))
        self.assertEqual(texts(self.extract(para)), ["EQ: x=1\ny=1/2"])

    def test_superscript_numbers_keep_their_power(self):
        body = (b.para(b.run("E = 200 × 10") + b.sup_run("3") + b.run(" MPa"))
                + b.para(b.run("k = 1 × 10") + b.sup_run("-") + b.sup_run("7") + b.run(" m/s"))
                + b.para(b.run("10") + b.sup_run("1") + b.sup_run("2") + b.run(" cycles"))
                + b.para(b.run("the 1") + b.sup_run("st") + b.run(" pour, 25 m") + b.sup_run("2")
                         + b.run(", ref") + b.sup_run("12") + b.run(", H") + b.sup_run("2", "subscript") + b.run("O")))
        self.assertEqual(texts(self.extract(body)), [
            "E = 200 × 10^3 MPa", "k = 1 × 10^-7 m/s", "10^12 cycles", "the 1st pour, 25 m2, ref12, H2O"])

    def test_text_runs_in_symbol_fonts(self):
        body = (b.para(b.run("film 85 ") + b.font_run("Symbol", "m") + b.run("m, ")
                       + b.font_run("Symbol", "n = 0.2, \uf066 = 0.8, \uf0b3 200") + b.run(" mm"))
                + b.para(b.run("Released ") + b.font_run("Wingdings 2", "R") + b.run(" Yes ")
                         + b.font_run("Wingdings 2", "£") + b.run(" No ") + b.font_run("Wingdings", "þ"))
                + b.para(b.font_run("Segoe UI Symbol", "m ✓") + b.font_run("Symbol", " μ")))
        self.assertEqual(texts(self.extract(body)), [
            "film 85 μm, ν = 0.2, φ = 0.8, ≥ 200 mm", "Released ☑ Yes ☐ No ☑", "m ✓ μ"])

    def test_legacy_form_fields(self):
        def box(state, label):
            return b.para('<w:r><w:fldChar w:fldCharType="begin"><w:ffData><w:name w:val="Check1"/><w:checkBox>'
                          '<w:sizeAuto/>%s</w:checkBox></w:ffData></w:fldChar></w:r><w:r><w:instrText '
                          'xml:space="preserve"> FORMCHECKBOX </w:instrText></w:r><w:r><w:fldChar '
                          'w:fldCharType="end"/></w:r><w:r><w:t xml:space="preserve"> %s</w:t></w:r>' % (state, label))

        def drop(result):
            entries = "".join('<w:listEntry w:val="%s"/>' % e for e in ("Conforms", "NCR raised", "Hold"))
            return b.para('<w:r><w:t xml:space="preserve">Result: </w:t></w:r><w:r><w:fldChar w:fldCharType="begin">'
                          '<w:ffData><w:name w:val="Drop1"/><w:ddList>%s%s</w:ddList></w:ffData></w:fldChar></w:r>'
                          '<w:r><w:instrText xml:space="preserve"> FORMDROPDOWN </w:instrText></w:r>'
                          '<w:r><w:fldChar w:fldCharType="end"/></w:r>' % (result, entries))
        text_field = b.para('<w:r><w:t xml:space="preserve">Inspector: </w:t></w:r><w:r><w:fldChar '
                            'w:fldCharType="begin"><w:ffData><w:name w:val="Text1"/><w:textInput><w:default '
                            'w:val="name"/></w:textInput></w:ffData></w:fldChar></w:r><w:r><w:instrText '
                            'xml:space="preserve"> FORMTEXT </w:instrText></w:r><w:r><w:fldChar '
                            'w:fldCharType="separate"/></w:r><w:r><w:t>Sam Brown</w:t></w:r><w:r><w:fldChar '
                            'w:fldCharType="end"/></w:r>')
        body = (box('<w:default w:val="0"/><w:checked/>', "Formwork inspected")
                + box('<w:default w:val="0"/>', "Reinforcement inspected")
                + box('<w:default w:val="1"/>', "Survey checked")
                + box('<w:default w:val="1"/><w:checked w:val="0"/>', "Cover checked")
                + drop('<w:result w:val="1"/>') + drop("") + text_field)
        self.assertEqual(texts(self.extract(body)), [
            "☒ Formwork inspected", "☐ Reinforcement inspected", "☒ Survey checked", "☐ Cover checked",
            "Result: NCR raised", "Result: Conforms", "Inspector: Sam Brown"])

    def test_empty_content_controls_are_blank(self):
        def control(content, placeholder=True, inline=True):
            pr = "<w:showingPlcHdr/>" if placeholder else ""
            return '<w:sdt><w:sdtPr><w:alias w:val="Lot"/>%s</w:sdtPr><w:sdtContent>%s</w:sdtContent></w:sdt>' % (
                pr, content)
        prompt = b.run("Click or tap here to enter text.")
        cell = ('<w:tbl><w:tr>%s%s</w:tr></w:tbl>'
                % ("<w:tc>%s</w:tc>" % b.para("Date poured"),
                   control("<w:tc>%s</w:tc>" % b.para("Click or tap to enter a date."))))
        body = (b.para(b.run("Lot number: ") + control(prompt))
                + b.para(b.run("Client: ") + control(b.run("Riverside Depot Pty Ltd"), placeholder=False))
                + control(b.para("Click or tap here to enter text."))
                + cell)
        self.assertEqual(texts(self.extract(body)), [
            "Lot number: [blank]", "Client: Riverside Depot Pty Ltd", "[blank]", "Date poured | [blank]"])

    def test_smartart_text_and_watermark(self):
        header = b.hdr_xml("header", b.watermark("NOT FOR CONSTRUCTION"), b.para("Riverside Depot"))
        data = b.diagram_data_xml("Excavate to 1.5 m", "Proof roll")
        body = b.para(b.run("Sequence:") + b.smartart("rIdD1")) + b.para("After the diagram")
        doc = self.extract(body, headers=[header], extra_parts={"word/diagrams/data1.xml": data},
                           extra_rels=[("rIdD1", "diagramData", "diagrams/data1.xml")])
        self.assertEqual(blocks(doc), [
            ("para", "Sequence:", 0), ("item", "Excavate to 1.5 m", 0), ("item", "Proof roll", 0),
            ("para", "After the diagram", 0),
            ("para", "Header: Watermark: NOT FOR CONSTRUCTION | Riverside Depot", 0)])

    def test_list_level_redefined_by_an_override(self):
        numbering = (b.XML_HEAD + '<w:numbering xmlns:w="%s"><w:abstractNum w:abstractNumId="0">'
                     '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%%1."/></w:lvl>'
                     '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerRoman"/><w:lvlText w:val="%%1.%%2"/>'
                     '</w:lvl></w:abstractNum>'
                     '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
                     '<w:num w:numId="9"><w:abstractNumId w:val="0"/><w:lvlOverride w:ilvl="0"><w:lvl w:ilvl="0">'
                     '<w:numFmt w:val="upperLetter"/><w:lvlText w:val="(%%1)"/></w:lvl></w:lvlOverride></w:num>'
                     '<w:num w:numId="7"><w:abstractNumId w:val="0"/><w:lvlOverride w:ilvl="0"><w:startOverride '
                     'w:val="4"/><w:lvl w:ilvl="0"><w:start w:val="2"/><w:numFmt w:val="lowerLetter"/></w:lvl>'
                     '</w:lvlOverride></w:num></w:numbering>' % b.W_NS)
        body = (b.para("Plain one", num=(1, 0)) + b.para("Plain two", num=(1, 0))
                + b.para("OVR_ONE", num=(9, 0)) + b.para("Sub item", num=(9, 1)) + b.para("OVR_TWO", num=(9, 0))
                + b.para("Restarted", num=(7, 0)))
        self.assertEqual(texts(self.extract(body, numbering=numbering)), [
            "1. Plain one", "2. Plain two", "(A) OVR_ONE", "A.i Sub item", "(B) OVR_TWO", "d. Restarted"])

    def test_hidden_text_is_left_out(self):
        body = b.para('<w:r><w:t xml:space="preserve">visible </w:t></w:r>'
                      '<w:r><w:rPr><w:vanish/></w:rPr><w:t>HIDDENVANISH</w:t></w:r>'
                      '<w:r><w:rPr><w:vanish w:val="0"/></w:rPr><w:t xml:space="preserve">shown</w:t></w:r>'
                      '<w:r><w:rPr><w:specVanish/></w:rPr><w:t>ALSOHIDDEN</w:t></w:r>'
                      '<w:r><w:t xml:space="preserve"> end</w:t></w:r>')
        self.assertEqual(texts(self.extract(body)), ["visible shown end"])

    def test_deleted_list_item_takes_no_number(self):
        deleted = ('<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr>'
                   '<w:rPr><w:del w:id="7" w:author="x"/></w:rPr></w:pPr>'
                   '<w:del w:id="8" w:author="x"><w:r><w:delText>Old item</w:delText></w:r></w:del></w:p>')
        body = (b.para("Submit ITP", num=(2, 0)) + deleted + b.para("Dewatering plan due 23/04/2025", num=(2, 0))
                + b.para("", num=(2, 0))          # an empty item still takes a number in Word
                + b.para("Next toolbox talk", num=(2, 0)))
        doc = self.extract(body, numbering=True)
        self.assertEqual(texts(doc), ["1. Submit ITP", "2. Dewatering plan due 23/04/2025", "4. Next toolbox talk"])

    def test_zero_width_joiners_are_kept(self):
        for text in ("می\u200cخواهم", "\U0001F477\u200d\u2640\ufe0f", "क्\u200dष"):
            self.assertEqual(texts(self.extract(b.para(text))), [text])
            self.assertEqual(docs._tidy(text), text)
        self.assertEqual(docs._tidy("a\u200bb\u00adc\ufeffd\u2060e"), "abcde")

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
        self.assertEqual(doc["note"], "partly read: first 100 of 60000 rows of sheet Log")

    def test_text_limit_counts_rows_of_later_sheets(self):
        sheet = b.sheet_xml([(i, [b.c_inline("A%d" % i, "row text %d" % i)]) for i in range(1, 301)])
        with mock.patch.object(docs, "DOC_TEXT_MAX", 500):
            doc = docs.extract("a.xlsx", b.xlsx([("One", sheet, ""), ("Two", sheet, "")]))
        sheets = [bl for bl in doc["blocks"] if bl["type"] == "sheet"]
        self.assertEqual([(s["text"], s["rows"]) for s in sheets], [("One", 300), ("Two", 300)])
        self.assertLessEqual(sum(len(t) for t in texts(doc)), 500)
        self.assertEqual(doc["note"], "partly read: text limit reached at sheet One")

    def test_text_limit_lists_every_sheet_and_says_so(self):
        big = b.sheet_xml([(i, [b.c_inline("A%d" % i, "bill item %d excavation" % i)]) for i in range(1, 400)])
        notes = b.sheet_xml([(1, [b.c_inline("A1", "Tender closes 14 March 2025")])])
        with mock.patch.object(docs, "DOC_TEXT_MAX", 2000):
            doc = docs.extract("BoQ.xlsx", b.xlsx([("Bill 1", big, ""), ("Bill 2", big, ""), ("Notes", notes, "")]))
        check_shape(self, doc)
        self.assertEqual([(bl["text"], bl["rows"]) for bl in doc["blocks"] if bl["type"] == "sheet"],
                         [("Bill 1", 399), ("Bill 2", 399), ("Notes", 1)])
        self.assertTrue(doc["note"].startswith("partly read: text limit reached"), doc["note"])
        with mock.patch.object(docs, "DOC_TEXT_MAX", 500):
            doc = docs.extract("big.docx", b.docx("".join(b.para("Paragraph %d with words." % i) for i in range(500))))
        self.assertEqual(doc["note"], "partly read: text limit reached")

    def test_cell_notes_and_hidden_columns(self):
        cells = [
            b.c_inline("A1", "RFI") + b.c_inline("B1", "Subject") + b.c_inline("C1", "Status")
            + b.c_inline("D1", "Internal cost") + b.c_inline("E1", "Group"),
            b.c_inline("A2", "RFI-047") + b.c_inline("B2", "Footing F12 clash") + b.c_inline("C2", "Open")
            + b.c_num("D2", "1200") + b.c_inline("E2", "Footings"),
            b.c_inline("A3", "RFI-089") + b.c_inline("B3", "Crane beam splice") + b.c_inline("C3", "Closed")
            + b.c_num("D3", "3400")]
        sheet = (b.XML_HEAD + '<worksheet xmlns="%s"><cols><col min="4" max="4" width="9" hidden="1"/>'
                 '<col min="5" max="5" hidden="1" outlineLevel="1"/></cols><sheetData>'
                 '<row r="1">%s</row><row r="2">%s</row><row r="3" hidden="1">%s</row>'
                 '</sheetData></worksheet>' % (b.S_NS, cells[0], cells[1], cells[2]))
        threaded = ("[Threaded comment]\n\nYour version of Excel allows you to read this threaded comment; "
                    "however, any edits to it will get removed if the file is opened in a newer version of "
                    "Excel.\n\nComment:\n    Splice detail from fabricator?\nReply:\n    Due 21/11")
        comments = (b.XML_HEAD + '<comments xmlns="%s"><authors><author>Sam Brown</author></authors><commentList>'
                    '<comment ref="C2" authorId="0"><text><r><t xml:space="preserve">Sam Brown:\nawaiting '
                    'shop drawing Rev 1, chase by 14/11</t></r></text></comment>'
                    '<comment ref="B3" authorId="0"><text><t xml:space="preserve">%s</t></text></comment>'
                    '<comment ref="D2" authorId="0"><text><t>margin 18%%</t></text></comment>'
                    '<comment ref="$G$9" authorId="0"><text><t>Register checked 31/10</t></text></comment>'
                    '</commentList></comments>' % (b.S_NS, threaded))
        rels = b.rels_xml([("rId1", "comments", "../comments1.xml")])
        parts = {"xl/worksheets/_rels/sheet1.xml.rels": rels, "xl/comments1.xml": comments}
        doc = docs.extract("RFI Register.xlsx", b.xlsx([("RFIs", sheet, "")], sheet_parts=parts))
        check_shape(self, doc)
        self.assertEqual(texts(doc, "row"), [
            "RFI | Subject | Status | Group",
            "RFI-047 | Footing F12 clash | Open [note: Sam Brown: awaiting shop drawing Rev 1, chase by 14/11] | Footings",
            "RFI-089 | Crane beam splice [note: Splice detail from fabricator? / Due 21/11] | Closed",
            "Notes: G9: Register checked 31/10"])
        joined = " ".join(texts(doc))
        for hidden in ("Internal cost", "1200", "3400", "margin"):
            self.assertNotIn(hidden, joined)
        # A damaged comments part is skipped; the sheet is still read.
        parts["xl/comments1.xml"] = "<comments><broken"
        doc = docs.extract("RFI Register.xlsx", b.xlsx([("RFIs", sheet, "")], sheet_parts=parts))
        self.assertEqual(texts(doc, "row")[1], "RFI-047 | Footing F12 clash | Open | Footings")

    def test_percents_round_like_excel_and_long_numbers_keep_digits(self):
        styles = (b.XML_HEAD + '<styleSheet xmlns="%s"><numFmts count="1"><numFmt numFmtId="164" formatCode="0.0%%"/>'
                  '</numFmts><cellXfs count="3"><xf numFmtId="0"/><xf numFmtId="9"/><xf numFmtId="164"/></cellXfs>'
                  '</styleSheet>' % b.S_NS)
        row = [b.c_num("A1", "0.125", 1), b.c_num("B1", "0.145", 1), b.c_num("C1", "-0.0525", 2),
               b.c_num("D1", "0.35", 1), b.c_num("E1", "123456789.12"), b.c_num("F1", "1234567890123")]
        doc = docs.extract("a.xlsx", b.xlsx([("S", b.sheet_xml([(1, row)]), "")], styles=styles))
        self.assertEqual(texts(doc, "row"), ["13% | 15% | -5.3% | 35% | 123456789.12 | 1234567890123"])
        self.assertEqual(docs.format_number("0.30000000000000004"), "0.3")
        self.assertEqual(docs.format_number("123456789.12"), "123456789.12")

    def test_number_format_text_and_leading_zeros(self):
        formats = ['"RFI-"000', '00000', '"Lot "0', '0.0" kN";[Red]\\-0.0" kN"', '"$"#,##0.00', '#,##0" kPa"',
                   '0.0\\ "mm"', '#,##0.00']
        num_fmts = "".join('<numFmt numFmtId="%d" formatCode="%s"/>' % (164 + i, b.escape(f, {'"': "&quot;"}))
                           for i, f in enumerate(formats))
        xfs = "".join('<xf numFmtId="%d"/>' % (164 + i) for i in range(len(formats)))
        styles = (b.XML_HEAD + '<styleSheet xmlns="%s"><numFmts count="%d">%s</numFmts><cellXfs count="%d">'
                  '<xf numFmtId="0"/>%s</cellXfs></styleSheet>' % (b.S_NS, len(formats), num_fmts, len(formats) + 1, xfs))
        values = ["7", "123", "14", "-35.2", "4513426.78", "120", "25", "1234.5"]
        row = [b.c_num("%s1" % "ABCDEFGH"[i], v, i + 1) for i, v in enumerate(values)]
        row.append(b.c_inline("I1", "text stays"))
        doc = docs.extract("a.xlsx", b.xlsx([("S", b.sheet_xml([(1, row)]), "")], styles=styles))
        self.assertEqual(texts(doc, "row"), [
            "RFI-007 | 00123 | Lot 14 | -35.2 kN | $4513426.78 | 120 kPa | 25 mm | 1234.5 | text stays"])

    def test_number_affixes(self):
        for code in ("#,##0", "0.00", "General", '#,##0,"k"', "0.00E+00", "# ?/?", "[>=1000]#,##0;0",
                     '00"-"000', "0+000", "[$-409]#,##0.00", "@", "", "0.0%"):
            self.assertIsNone(docs._number_affixes(code), code)
        self.assertEqual(docs._number_affixes('_-"$"* #,##0.00_-;\\-"$"* #,##0.00_-;_-"$"* "-"??_-;_-@_-'),
                         ("$", 1, ""))
        self.assertEqual(docs._number_affixes("[$€-407] #,##0.00"), ("€ ", 1, ""))
        self.assertEqual(docs._number_affixes('[Blue]"Lot "General'), ("Lot ", 0, ""))
        self.assertEqual(docs._with_affixes("-0.5", ("", 3, " m")), "-000.5 m")
        self.assertEqual(docs._with_affixes("1E+20", ("", 5, "")), "1e+20")      # (no padding in exponent form)

    def test_sparse_rows_name_their_columns(self):
        weeks = ["W%d" % n for n in range(1, 41)]
        cols = [docs._col_letter(n) for n in range(1, 41)]          # B..AO
        header = [b.c_inline("A2", "Task")] + [b.c_inline("%s2" % c, w) for c, w in zip(cols, weeks)]
        task = [b.c_inline("A3", "Task 1")] + [b.c_inline("%s3" % c, "x") for c in cols[29:33]] + \
            [b.c_inline("AQ3", "done")]
        subtotal = [b.c_inline("A4", "Subtotal"), b.c_num("B4", "2"), b.c_num("D4", "3"), b.c_num("F4", "4")]
        lead = [b.c_inline("B5", "indented"), b.c_inline("AO5", "last")]
        rows = [(1, [b.c_inline("A1", "Programme as at 2025-06-30")]), (2, header), (3, task), (4, subtotal),
                (5, lead)]
        doc = docs.extract("Programme.xlsx", b.xlsx([("Programme", b.sheet_xml(rows), "")]))
        got = texts(doc, "row")
        self.assertEqual(got[0], "Programme as at 2025-06-30")
        self.assertTrue(got[1].startswith("Task | W1 | W2 |"))
        self.assertEqual(got[2], "Task 1 | W30: x | W31: x | W32: x | W33: x | AQ: done")
        self.assertEqual(got[3], "Subtotal | 2 | | 3 | | 4")
        self.assertEqual(got[4], "| indented | W40: last")
        self.assertEqual([docs._col_letter(n) for n in (0, 25, 26, 27, 701, 702)], ["A", "Z", "AA", "AB", "ZZ", "AAA"])

    def test_text_boxes_on_sheets(self):
        drawing = b.drawing_xml(
            b.anchor(b.xdr_shape(["Design assumes simply supported beam.", "NOT VALID FOR SPANS OVER 9 m."])),
            b.anchor('<xdr:grpSp><xdr:nvGrpSpPr/><xdr:grpSpPr/>%s</xdr:grpSp>' % b.xdr_shape(["Grouped note"])),
            '<mc:AlternateContent><mc:Choice Requires="a14">%s</mc:Choice><mc:Fallback>%s</mc:Fallback>'
            '</mc:AlternateContent>' % (b.anchor(b.xdr_shape(["Choice note"])), b.anchor(b.xdr_shape(["Choice note"]))),
            b.anchor(b.xdr_shape(["Check Box 1"], hidden=True)),
            b.anchor(b.CHART_FRAME))
        sheet = b.sheet_xml([(1, [b.c_inline("A1", "Span"), b.c_num("B1", "8.4")])])
        rels = b.rels_xml([("rId1", "drawing", "../drawings/drawing1.xml")])
        parts = {"xl/worksheets/_rels/sheet1.xml.rels": rels, "xl/worksheets/_rels/sheet2.xml.rels": rels,
                 "xl/drawings/drawing1.xml": drawing}
        doc = docs.extract("Calc.xlsx", b.xlsx([("Calc", sheet, ""), ("Old", sheet, "hidden")], sheet_parts=parts))
        check_shape(self, doc)
        self.assertEqual(blocks(doc), [
            ("sheet", "Calc", 1), ("row", "Span | 8.4", 0),
            ("para", "Text box: Design assumes simply supported beam. NOT VALID FOR SPANS OVER 9 m.", 0),
            ("para", "Text box: Grouped note", 0), ("para", "Text box: Choice note", 0),
            ("sheet", "Old", 2)])

    def test_row_count_leaves_out_formatted_empty_rows(self):
        filled = 3000
        rows = []
        for i in range(1, filled + 1):
            cell = b.c_inline("A%d" % i, "log %d" % i) if i % 3 else b.c_shared("A%d" % i, 0)
            rows.append('<row r="%d">%s<c r="B%d" s="1"/></row>' % (i, cell, i))
        rows += ['<row r="%d"><c r="A%d" s="1"/><c r="B%d" s="1"/></row>' % (i, i, i) for i in range(filled + 1, 9001)]
        xml = '<worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>' % (b.S_NS, "".join(rows))
        with mock.patch.object(docs, "SHEET_ROWS_MAX", 100):
            doc = docs.extract("log.xlsx", b.xlsx([("Log", xml, "")], shared=["shared entry"]))
        self.assertEqual(doc["blocks"][0]["rows"], filled)
        # Chunk edges inside a row end or a value tag, and prefixed tags, are counted right.
        xml = (b'<x:row r="1"><x:c r="A1"><x:v>1</x:v></x:c></x:row><row r="2"><c r="A2" s="3"/></row>'
               b'<row r="3"><c r="A3" t="inlineStr"><is><t>a</t></is></c></row><row r="4"><c><v>4</v></c></row>'
               b'<row r="5" s="2" customFormat="1"/><row r="6"><c r="A6"><v>6</v></c></row>')
        for size in (1, 3, 7, 1000):
            chunks = [xml[i:i + size] for i in range(0, len(xml), size)]
            self.assertEqual(docs._count_rows(iter(chunks)), 4, size)
        self.assertEqual(docs._count_rows(iter([b'<c r="Z9"/></row><row><c/></row>']), pending=True), 1)

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


    def test_hidden_slides_are_marked_and_numbered_paragraphs_keep_numbers(self):
        data = b.pptx([
            {"title": "Next steps", "body": [b.autonum("Issue IFC drawings", start=3), b.autonum("Submit ITP"),
                                             b.autonum("Check bolts", "alphaLcParenR", level=1),
                                             b.autonum("Torque", "alphaLcParenR", level=1),
                                             b.autonum("Close out", start=3), "Plain paragraph",
                                             b.autonum("Restarted", "romanUcPeriod"),
                                             b.autonum("Both", "arabicParenBoth")]},
            {"title": "Backup figures", "body": ["Internal margin 12%"], "hidden": True},
            {"body": ["Shown slide"]},
        ])
        doc = docs.extract("Update.pptx", data)
        check_shape(self, doc)
        self.assertEqual(blocks(doc), [
            ("slide", "Next steps", 1), ("para", "3. Issue IFC drawings", 0), ("para", "4. Submit ITP", 0),
            ("para", "a) Check bolts", 0), ("para", "b) Torque", 0), ("para", "5. Close out", 0),
            ("para", "Plain paragraph", 0), ("para", "I. Restarted", 0), ("para", "(1) Both", 0),
            ("slide", "Backup figures (hidden)", 2), ("para", "Internal margin 12%", 0),
            ("slide", "", 3), ("para", "Shown slide", 0)])
        self.assertTrue(doc["blocks"][9]["hidden"])
        self.assertNotIn("hidden", doc["blocks"][-2])


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

A4 = (595.0, 842.0)
A3 = (1191.0, 842.0)
A1 = (2384.0, 1684.0)

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

    def test_mostly_scanned_pdf_keeps_its_typed_pages(self):
        letter = ("RFI 23 response. The pile cut-off level is lowered to RL 12.45 and pile P14 is extended to 9.5 m. "
                  "Use N40 bars with 50 mm cover to all pile caps on grid lines A to D. Refer to the marked-up "
                  "drawings attached (scanned) for the revised pile layout and the cap reinforcement details.")
        self.assertGreater(len(letter.replace(" ", "")), docs.TYPED_PAGE_MIN)
        with fake_reader([letter] + [""] * 30):
            doc = docs.extract("RFI 23 response.pdf", b"%PDF-1.4")
        self.assertEqual((doc["status"], doc["note"]), ("ok", "pages 2-31 have no text (scanned?)"))
        self.assertIn("RL 12.45", " ".join(texts(doc)))
        with fake_reader([""] + [letter] + [""] * 3 + ["x"] + [""] * 30, note="1 page could not be read."):
            doc = docs.extract("Mixed.pdf", b"%PDF-1.4")
        self.assertEqual(doc["note"], "1 page could not be read.; pages 1, 3-36 have no text (scanned?)")
        pages = [letter if n in (2, 9, 15, 30) else "" for n in range(1, 81)]
        with fake_reader(pages):
            self.assertEqual(docs.extract("Mixed.pdf", b"%PDF-1.4")["note"], "76 of 80 pages have no text (scanned?)")
        # Still scans: a stamp on every page, or one short line among blank pages.
        for pages in (["Scanned with CamScan"[:19]] * 10, ["x" * 150] + [""] * 20):
            with fake_reader(pages):
                doc = docs.extract("Scan.pdf", b"%PDF-1.4")
            self.assertEqual((doc["status"], doc["note"]), ("no_text", "scanned or image-only PDF"))

    def test_review_comments_are_their_own_paragraphs(self):
        body = REPORT_PAGE.replace("\n\n", "\n")
        comments = ["[comment: Increase to 175 kPa]", "[comment: Check with Sam Brown]"]
        with fake_reader([body]):
            plain = blocks(docs.extract("Report.pdf", b"%PDF-1.4"))
        with fake_reader([body + "\n\n" + "\n\n".join(comments)]):
            commented = blocks(docs.extract("Report.pdf", b"%PDF-1.4"))
        # The page's own blocks (header and footer lines included) are as without comments.
        self.assertEqual(commented, plain + [("para", c, 0) for c in comments])
        self.assertEqual(plain[-1], ("para", "Page 3 of 12", 0))

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
        wait_for_pypdf()
        self.assertEqual(doc["reader"], "pypdf")
        self.assertEqual(doc["pages"], 5)
        self.assertEqual(doc["note"], "stopped after 2 pages (slow to read)")
        self.assertIn("Page 2 text", " ".join(texts(doc)))
        self.assertTrue(doc["retry"])         # cut short by time: not worth keeping in the cache

    def test_pypdf_that_cannot_open_falls_back(self):
        fake = mock.Mock(PdfReader=mock.Mock(side_effect=ValueError("bad")), __version__="0")
        result = {"pages": ["Built-in reader text that is long enough."], "page_sizes": [A4],
                  "status": "ok", "note": "", "title": "", "count": 1}
        with mock.patch.multiple(docs, _get_pypdf=mock.Mock(return_value=fake),
                                 _pdf_with_pdftext=mock.Mock(return_value=result)):
            doc = docs.extract("x.pdf", b"%PDF-1.4")
        self.assertEqual((doc["reader"], doc["status"]), ("pdftext", "ok"))

    def test_table_rows_stay_separate(self):
        # A short page: every line is "full width", but table rows still end at their last figure.
        page = ("Test certificate BL-LC-5509\n"
                "Cylinder Element Date cast Age (days) Strength (MPa)\n"
                "BL-LC-5509-1 Footing F9 09/04/2025 28 40.8 MPa\n"
                "BL-LC-5509-2 Footing F9 09/04/2025 28 38.1 MPa\n"
                "BL-LC-5509-3 Footing F9 09/04/2025 28 39.4 MPa\n"
                "Page 2 of 6")
        with fake_reader([page]):
            doc = docs.extract("Certificates.pdf", b"%PDF-1.4")
        self.assertEqual(texts(doc, "para"), page.split("\n"))
        # A wrapped line ending in a figure but with fewer than 3 numbers still joins up.
        prose = ("Minutes of site meeting\nThe engineer confirmed the workshop slab will be 200 mm\n"
                 "Thick with N12 bars as shown on the drawings.\nEnd")
        with fake_reader([prose]):
            doc = docs.extract("Minutes.pdf", b"%PDF-1.4")
        self.assertIn("The engineer confirmed the workshop slab will be 200 mm Thick with N12 bars as shown "
                      "on the drawings.", texts(doc, "para"))

    def test_register_rows_are_not_headings(self):
        self.assertEqual(docs._pdf_heading("RD-ST-1002 GENERAL NOTES B A1"), 0)
        self.assertEqual(docs._pdf_heading("623.0001-ST-1200 DEPOT SLAB SETOUT PLAN"), 0)
        self.assertEqual(docs._pdf_heading("C-01 DESIGN BASIS AND LOADS"), 1)
        self.assertEqual(docs._pdf_heading("BH-01 BOREHOLE LOG"), 1)
        self.assertEqual(docs._pdf_heading("GENERAL NOTES"), 1)
        self.assertTrue(docs.table_row("RD-ST-1002 GENERAL NOTES B A1"))
        self.assertTrue(docs.table_row("BL-LC-5509-1 Footing F9 09/04/2025 28 40.8 MPa"))
        self.assertFalse(docs.table_row("The contractor confirmed that the slab pour on 12"))

    def test_a3_report_is_not_a_drawing(self):
        def prose(n):
            return "\n".join("Page %d %s: the traffic generated by the upgraded depot was modelled for "
                             "the morning and evening peak hours at the site access." % (n, part)
                             for part in ("first", "second", "third", "fourth"))
        with fake_reader([prose(n) for n in range(1, 7)], sizes=[A3] * 6):
            self.assertFalse(docs.extract("Riverside Depot Traffic Impact Assessment.pdf", b"%PDF-1.4")["drawing"])
        title_block = ("Do not scale from this drawing and check all dimensions on site before work starts.\n"
                       "This drawing is to be read with the specification and the other structural drawings.\n"
                       "All work is to be carried out in accordance with the relevant standards and codes.\n"
                       "GRID A GRID B LEVEL 1 RL 12.450")
        with fake_reader([title_block] * 6, sizes=[A3] * 6):
            self.assertTrue(docs.extract("Riverside set.pdf", b"%PDF-1.4")["drawing"])
        rows = "\n".join("Task %d Pour slab bay %d 12/05/2025 16/05/2025 5 days" % (n, n) for n in range(30))
        with fake_reader([rows] * 3, sizes=[A3] * 3):
            self.assertFalse(docs.extract("Acme Civil Construction Programme Rev 6.pdf", b"%PDF-1.4")["drawing"])
        notes = "\n".join("%d. All concrete for the footings and the slab is to be placed and cured as per the "
                          "specification clause %d." % (n, n) for n in range(1, 8))
        with fake_reader([notes], sizes=[A1]):
            self.assertTrue(docs.extract("RD-ST-1002 [B] GENERAL NOTES.pdf", b"%PDF-1.4")["drawing"])
        self.assertTrue(docs.reads_like_document("Drawing Register Rev 3.pdf", []))
        self.assertFalse(docs.reads_like_document("RD-ST-1201 GA PLAN.pdf", []))

    def test_pdf_cut_short_by_time_keeps_what_was_read(self):
        from squish_app import pdftext
        pages = ["pad footing 1800 square, N16-200 bottom, page %d" % n for n in range(1, 4)] + [""] * 37
        note = "Stopped after 20 seconds (very complex PDF); pages from 4 on were not read."
        raw = {"pages": pages, "page_sizes": [A4] * 40, "page_count": 40, "title": "", "status": "ok",
               "note": note, "pages_read": 3, "stopped": "time"}
        with mock.patch.object(pdftext, "extract_pdf", return_value=raw), \
                mock.patch.object(docs, "_get_pypdf", return_value=None):
            doc = docs.extract("slow.pdf", b"%PDF-1.4")
            check_shape(self, doc)
            self.assertEqual((doc["status"], doc["note"]), ("ok", note))
            self.assertIn("N16-200 bottom, page 3", " ".join(texts(doc)))
            self.assertTrue(doc["retry"])
            # Nothing read before the time ran out: an error with the reader's note, never "scanned".
            note = "Stopped after 20 seconds (very complex PDF); pages from 1 on were not read."
            raw.update(pages=[""] * 40, pages_read=0, note=note)
            doc = docs.extract("slow.pdf", b"%PDF-1.4")
            self.assertEqual((doc["status"], doc["note"]), ("error", note))
            self.assertTrue(doc["retry"])
            # A size limit: the same, but reading again will not help.
            note = "The PDF is too large or complex to read in full; pages from 1 on were not read."
            raw.update(note=note, stopped="data")
            doc = docs.extract("big.pdf", b"%PDF-1.4")
            self.assertEqual((doc["status"], doc["note"]), ("error", note))
            self.assertNotIn("retry", doc)
            # A complete reading without text is a scan (a reader note is kept).
            raw.update(note="", stopped="", pages_read=40)
            self.assertEqual(docs.extract("scan.pdf", b"%PDF-1.4")["note"], "scanned or image-only PDF")
            raw.update(note="1 very complex page may be incomplete.")
            doc = docs.extract("cad.pdf", b"%PDF-1.4")
            self.assertEqual((doc["status"], doc["note"]), ("no_text", "1 very complex page may be incomplete."))

    def test_pypdf_without_cryptography(self):
        class DependencyError(Exception):
            pass
        fake = mock.Mock(PdfReader=mock.Mock(side_effect=DependencyError("cryptography is required for AES")),
                         errors=mock.Mock(DependencyError=DependencyError), __version__="0")
        with mock.patch.object(docs, "_get_pypdf", return_value=fake):
            doc = docs.extract("Locked.pdf", b"%PDF-1.4")
        self.assertEqual(doc["status"], "protected")
        self.assertIn("cryptography", doc["note"])

    def test_moved_glyphs(self):
        self.assertTrue(docs._moved_glyphs("85 m on steel. μ", "85 μm on steel."))
        self.assertFalse(docs._moved_glyphs("RL 1 2.4 50", "RL 12.450"))         # spacing only
        self.assertFalse(docs._moved_glyphs("85 m on steel.", "85 μm on steel."))  # other characters
        self.assertFalse(docs._moved_glyphs("مرحبا بالعالم", "ملاعلاب ابحرم"))       # right-to-left: left alone
        # pypdf 5 gives the micro sign (U+00B5) where the built-in reader has the Greek mu (U+03BC)
        self.assertTrue(docs._moved_glyphs("85m on steel.µ", "85 μm on steel."))
        self.assertEqual(docs._fix_glyph_order("Dry film 85m on steel.µ\nNext line",
                                               "Dry film 85 μm on steel.\nNext line"),
                         "Dry film 85 μm on steel.\nNext line")

    def test_symbol_codes_from_pypdf(self):
        seen = [("\uf06e", "wingdings"), ("\uf06e", "symbol"), ("\uf0a3", "symbol"), ("\uf0b7", None)]
        self.assertEqual(docs._fix_symbol_chars("\uf06e Item nu \uf06e = 0.2 \uf0a3 6 m \uf0b7", seen),
                         "■ Item nu ν = 0.2 ≤ 6 m \uf0b7")
        # The visitor saw other characters than the text holds: left for _tidy.
        self.assertEqual(docs._fix_symbol_chars("\uf06e x", [("\uf0a3", "symbol")]), "\uf06e x")

    def test_backend_status(self):
        with mock.patch.dict(os.environ, {"SQUISH_NO_PYPDF": "1"}):
            self.assertEqual(docs.backend_status(), "PDF: built-in reader (install pypdf for best results)")
        module = mock.Mock(__version__="5.1.0")
        with mock.patch.object(docs, "_get_pypdf", return_value=module):
            self.assertEqual(docs.backend_status(), "PDF: pypdf 5.1.0")


def fake_pypdf(texts_or_function, size=(595, 842), encrypted=False):
    """A stand-in pypdf module whose pages give these texts (or call the function with the page number)."""
    class Page(object):
        mediabox = mock.Mock(width=size[0], height=size[1])

        def __init__(self, n):
            self.n = n

        def extract_text(self, **options):
            if callable(texts_or_function):
                return texts_or_function(self.n)
            return texts_or_function[self.n]

    class Reader(object):
        is_encrypted = encrypted
        metadata = None

        def __init__(self, stream, strict=False):
            count = 1 if callable(texts_or_function) else len(texts_or_function)
            self.pages = [Page(n) for n in range(count)]

        def decrypt(self, password):
            return 1

    return mock.Mock(PdfReader=Reader, __version__="0")


@unittest.skipIf(pdf_builder is None, "tests/pdf_builder.py not available yet")
class PdfReaderChoiceTests(unittest.TestCase):
    """Which reader's text is used: real (tiny) PDFs, pypdf replaced by a stand-in."""

    def tearDown(self):
        wait_for_pypdf()

    def test_drawing_is_read_by_the_builtin_reader_first(self):
        data = pdf_builder.simple_pdf(["GENERAL ARRANGEMENT PLAN\nALL DIMENSIONS IN MM"], size=pdf_builder.A1_LANDSCAPE)
        calls = []

        def slow_pypdf(n):
            calls.append(n)            # pypdf takes minutes on a real CAD sheet
            return "pypdf text of the drawing sheet"
        fake = fake_pypdf(slow_pypdf, size=A1)
        with mock.patch.object(docs, "_get_pypdf", return_value=fake):
            doc = docs.extract("Riverside site plan.pdf", data)
        check_shape(self, doc)
        self.assertEqual((doc["reader"], doc["status"], doc["drawing"]), ("pdftext", "ok", True))
        self.assertIn("GENERAL ARRANGEMENT PLAN", " ".join(texts(doc)))
        self.assertEqual(calls, [])
        # The built-in reader cannot read it: pypdf does.
        error = {"pages": [], "page_sizes": [], "status": "error", "note": "damaged", "title": "", "count": 0}
        with mock.patch.object(docs, "_get_pypdf", return_value=fake), \
                mock.patch.object(docs, "_pdf_with_pdftext", return_value=error):
            doc = docs.extract("Riverside site plan.pdf", data)
        self.assertEqual((doc["reader"], doc["status"]), ("pypdf", "ok"))
        self.assertIn("pypdf text of the drawing sheet", " ".join(texts(doc)))

    def test_drawing_keeps_the_builtin_reading_whenever_it_found_text(self):
        data = pdf_builder.simple_pdf(["GENERAL ARRANGEMENT PLAN"], size=pdf_builder.A1_LANDSCAPE)
        calls = []

        def slow_pypdf(n):
            calls.append(n)
            return "pypdf text of the drawing sheet"
        fake = fake_pypdf(slow_pypdf, size=A1)
        damaged = {"pages": ["GENERAL ARRANGEMENT PLAN\nRD-ST-1200 [H]"], "page_sizes": [A1], "status": "ok",
                   "note": "The PDF is damaged; text was recovered where possible.", "title": "", "count": 1,
                   "read": 1, "cut": True}
        with mock.patch.object(docs, "_get_pypdf", return_value=fake), \
                mock.patch.object(docs, "_pdf_with_pdftext", return_value=damaged):
            doc = docs.extract("RD-ST-1200 [H] SLAB SETOUT.pdf", data)
        self.assertEqual((doc["reader"], doc["status"], doc["drawing"]), ("pdftext", "ok", True))
        self.assertEqual(calls, [])
        # The built-in reader found no text (and says why): pypdf is still tried.
        empty = dict(damaged, pages=[""], note="The PDF is too large or complex to read in full.")
        with mock.patch.object(docs, "_get_pypdf", return_value=fake), \
                mock.patch.object(docs, "_pdf_with_pdftext", return_value=empty):
            doc = docs.extract("RD-ST-1200 [H] SLAB SETOUT.pdf", data)
        self.assertEqual(calls, [0])
        self.assertEqual(doc["reader"], "pypdf")
        self.assertIn("pypdf text of the drawing sheet", " ".join(texts(doc)))

    def test_heavy_page_is_left_to_the_builtin_reader(self):
        data = pdf_builder.simple_pdf(["Footing design memo with enough words to count as text.",
                                       "FIGURE 2 SITE PLAN WITH CAD LINEWORK"])
        calls = []

        class Contents(object):
            def __init__(self, size):
                self._data = b"x" * size

            def get_object(self):
                return self

        class Page(object):
            mediabox = mock.Mock(width=595, height=842)

            def __init__(self, n):
                self.n = n

            def get(self, key, default=None):
                return Contents(5000 if self.n == 1 else 50) if key == "/Contents" else default

            def extract_text(self, **options):
                calls.append(self.n)
                return "Footing design memo with enough words to count as text."

        class Reader(object):
            is_encrypted = False
            metadata = None

            def __init__(self, stream, strict=False):
                self.pages = [Page(0), Page(1)]

        fake = mock.Mock(PdfReader=Reader, __version__="0")
        self.assertEqual(docs._pypdf_content_bytes(Page(1)), 5000)
        self.assertEqual(docs._pypdf_content_bytes(object()), 0)     # unknown: 0
        with mock.patch.object(docs, "_get_pypdf", return_value=fake), \
                mock.patch.object(docs, "PDF_HEAVY_PAGE_BYTES", 1000):
            doc = docs.extract("Memo.pdf", data)
        self.assertEqual(calls, [0])                  # pypdf never read the heavy page
        self.assertEqual(doc["reader"], "pypdf")
        self.assertEqual(texts(doc, "para"), ["Footing design memo with enough words to count as text.",
                                              "FIGURE 2 SITE PLAN WITH CAD LINEWORK"])

    def test_cancel_does_not_wait_for_pypdf(self):
        def stuck(n):
            time.sleep(3)
            return "late text"
        cancel = threading.Event()
        timer = threading.Timer(0.3, cancel.set)
        timer.start()
        try:
            with mock.patch.object(docs, "_get_pypdf", return_value=fake_pypdf(stuck)), \
                    mock.patch.object(docs, "PDF_TIME_MAX", 20), Timed(self, 1.5):
                doc = docs.extract("Report.pdf", b"%PDF-1.4", stop=cancel.is_set)
        finally:
            timer.cancel()
        self.assertEqual((doc["status"], doc["note"]), ("error", "reading was cancelled"))
        wait_for_pypdf()
        # Waiting for pypdf's one slot (another PDF holds it) also notices Cancel.
        cancel = threading.Event()
        timer = threading.Timer(0.3, cancel.set)
        self.assertTrue(docs._pypdf_slot.acquire(timeout=5))
        timer.start()
        try:
            with mock.patch.object(docs, "_get_pypdf", return_value=fake_pypdf(["pypdf text " * 5])), \
                    Timed(self, 1.5):
                doc = docs.extract("Report.pdf", b"%PDF-1.4", stop=cancel.is_set)
        finally:
            timer.cancel()
            docs._pypdf_slot.release()
        self.assertEqual((doc["status"], doc["note"]), ("error", "reading was cancelled"))

    def test_pdf_read_without_pypdf_because_it_was_busy_is_read_again(self):
        data = pdf_builder.simple_pdf(["Allowable bearing pressure 150 kPa in stiff clay."])
        release = threading.Event()
        busy = threading.Thread(target=release.wait)
        busy.start()
        with docs._pypdf_lock:
            docs._pypdf_abandoned.append(busy)
        try:
            with mock.patch.object(docs, "_get_pypdf", return_value=fake_pypdf(["pypdf text " * 5])):
                doc = docs.extract("Report.pdf", data)
        finally:
            release.set()
            busy.join(5)
            with docs._pypdf_lock:
                docs._pypdf_abandoned[:] = [t for t in docs._pypdf_abandoned if t is not busy]
        self.assertEqual((doc["reader"], doc["status"]), ("pdftext", "ok"))
        self.assertIn("150 kPa", " ".join(texts(doc)))
        self.assertTrue(doc["retry"])

    def test_pages_beyond_the_page_cap_are_noted(self):
        pages = ["Page %d text with enough words to count as text." % n for n in range(1, 4)]
        data = pdf_builder.simple_pdf(pages)
        for module in (None, fake_pypdf(pages)):
            with mock.patch.object(docs, "_get_pypdf", return_value=module), \
                    mock.patch.object(docs, "PAGE_MAX", 2):
                doc = docs.extract("Report.pdf", data)
            self.assertEqual((doc["status"], doc["pages"]), ("ok", 3))
            self.assertEqual(doc["note"], "partly read: first 2 of 3 pages")
            self.assertEqual(len(texts(doc, "page")), 2)
        self.assertEqual(docs.extract("Report.pdf", data)["note"], "")

    def test_pypdf_too_slow_uses_the_builtin_reader(self):
        data = pdf_builder.simple_pdf(["Allowable bearing pressure 150 kPa in stiff clay."])

        def stuck(n):
            time.sleep(2)
            return "late text"
        with mock.patch.object(docs, "_get_pypdf", return_value=fake_pypdf(stuck)), \
                mock.patch.object(docs, "PDF_TIME_MAX", 0.2), Timed(self, 4):
            doc = docs.extract("Report.pdf", data)
        wait_for_pypdf()
        check_shape(self, doc)
        self.assertEqual((doc["reader"], doc["status"]), ("pdftext", "ok"))
        self.assertIn("150 kPa", " ".join(texts(doc)))
        self.assertNotIn("retry", doc)
        # Neither reader gets any text in time: an error (worth another try), never "scanned".
        with mock.patch.object(docs, "_get_pypdf", return_value=fake_pypdf(stuck)), \
                mock.patch.object(docs, "PDF_TIME_MAX", 0.2), Timed(self, 4):
            doc = docs.extract("Report.pdf", b"%PDF-1.4")
        self.assertEqual((doc["status"], doc["note"]), ("error", "PDF too slow to read"))
        self.assertTrue(doc["retry"])
        # While an abandoned read is still running, pypdf is not used at all.
        with mock.patch.object(docs, "_get_pypdf", return_value=fake_pypdf(["pypdf text " * 5])):
            self.assertEqual(docs.extract("Report.pdf", data)["reader"], "pdftext")
        wait_for_pypdf()
        with mock.patch.object(docs, "_get_pypdf", return_value=fake_pypdf(["pypdf text " * 5])):
            self.assertEqual(docs.extract("Report.pdf", data)["reader"], "pypdf")

    def test_little_text_from_pypdf_tries_the_builtin_reader(self):
        data = pdf_builder.simple_pdf(["Allowable bearing pressure 150 kPa in stiff clay."])
        nothing = {"pages": [], "page_sizes": [], "status": "ok", "note": "stopped after 0 pages (slow to read)",
                   "title": "", "count": 1, "cut": True}
        with mock.patch.object(docs, "_get_pypdf", return_value=mock.Mock()), \
                mock.patch.object(docs, "_pdf_with_pypdf", return_value=nothing):
            doc = docs.extract("Report.pdf", data)
        self.assertEqual((doc["reader"], doc["status"]), ("pdftext", "ok"))
        with mock.patch.object(docs, "_get_pypdf", return_value=fake_pypdf(["  "])):
            doc = docs.extract("Report.pdf", data)
        self.assertEqual((doc["reader"], doc["status"]), ("pdftext", "ok"))
        self.assertIn("150 kPa", " ".join(texts(doc)))

    def test_secured_pdf_pypdf_cannot_decrypt(self):
        # pypdf without 'cryptography' opens an AES-locked PDF but fails on every page:
        # the built-in reader decrypts it.
        data = pdf_builder.simple_pdf(["Test certificate BL-LC-5512 strength 43.5 MPa"], encrypt="aes-128")

        def fails(n):
            raise ValueError("AES needs cryptography")
        with mock.patch.object(docs, "_get_pypdf", return_value=fake_pypdf(fails, encrypted=True)):
            doc = docs.extract("Certificates.pdf", data)
        self.assertEqual((doc["reader"], doc["status"]), ("pdftext", "ok"))
        self.assertIn("43.5 MPa", " ".join(texts(doc)))

    def test_glyph_order_lines_up_the_two_readings(self):
        data = pdf_builder.simple_pdf(["Dry film 85 \u00b5m on steel.\nSecond line"])
        fake = fake_pypdf(["Dry film 85 m on steel.\u00b5\nSecond line"])
        with mock.patch.object(docs, "_get_pypdf", return_value=fake):
            doc = docs.extract("Spec.pdf", data)
        self.assertEqual(texts(doc, "para"), ["Dry film 85 \u00b5m on steel.", "Second line"])
        # The built-in reader has the two lines as one: its line is used for both.
        one_line = {"pages": ["Dry film 85 \u00b5m on steel. Second line"], "page_sizes": [A4], "status": "ok",
                    "note": "", "title": "", "count": 1, "read": 1}
        with mock.patch.object(docs, "_get_pypdf", return_value=fake), \
                mock.patch.object(docs, "_pdf_with_pdftext", return_value=one_line):
            doc = docs.extract("Spec.pdf", data)
        self.assertEqual(texts(doc, "para"), ["Dry film 85 \u00b5m on steel. Second line"])

    def test_glyph_order_fix_where_line_counts_differ(self):
        fix = docs._fix_glyph_order
        # A header line that the readers split differently does not stop the fix.
        page = fix("Calc C-05\nSheet 1 of 2\nDry film 85 m on steel. \u03bc\nBolts M20",
                   "Calc C-05  Sheet 1 of 2\n\nDry film 85 \u03bcm on steel.\nBolts M20")
        self.assertIn("Dry film 85 \u03bcm on steel.", page.split("\n"))
        self.assertIn("Bolts M20", page)
        # pypdf put the late glyph and the line's pieces on lines of their own.
        self.assertEqual(fix("Primer 85 \nm DFT.\n\u03bc\nNext", "Primer 85 \u03bcm DFT.\nNext"),
                         "Primer 85 \u03bcm DFT.\nNext")
        # Whole lines in another order (columns), different content and right-to-left text stay as pypdf has them.
        for page, other in (("Left column text here\nRight column words there",
                             "Right column words there\nLeft column text here"),
                            ("Footing F1 600 deep\nFooting F2 900 deep", "Footing F2 600 deep\nFooting F1 900 deep extra"),
                            ("\u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645\nx",
                             "\u0645\u0644\u0627\u0639\u0644\u0627\u0628 \u0627\u0628\u062d\u0631\u0645\nx")):
            self.assertEqual(fix(page, other), page)

    def test_stop_cancels_reading(self):
        pages = ["Page %d text with enough words to count as text." % n for n in range(1, 5)]
        data = pdf_builder.simple_pdf(pages)
        for module in (None, fake_pypdf(pages)):
            with mock.patch.object(docs, "_get_pypdf", return_value=module):
                doc = docs.extract("Report.pdf", data, stop=lambda: True)
                self.assertEqual((doc["status"], doc["note"]), ("error", "reading was cancelled"))
                self.assertEqual(docs.extract("Report.pdf", data, stop=lambda: False)["status"], "ok")
        asked = []

        def stop():
            asked.append(1)
            return len(asked) > 1
        files = {"a.txt": "one", "b.txt": "two", "c.txt": "three"}
        doc = docs.extract("x.zip", b.make_zip(files), stop=stop)
        self.assertEqual((doc["status"], doc["note"]), ("error", "reading was cancelled"))
        self.assertEqual(docs.extract("x.zip", b.make_zip(files), stop=lambda: False)["status"], "ok")


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

    def test_drawing_is_decided_by_the_name_shown(self):
        memo = self.make(["Footing design memo. Allowable bearing pressure 150 kPa.", "Page two of the memo."])
        for label, doc in self.each_reader(memo, "623.0001 Rev A.pdf"):
            self.assertTrue(doc["drawing"], label)          # named like a drawing, 2 pages
            self.assertEqual((doc["large_pages"], doc["prose_pages"]), (False, False), label)
            self.assertFalse(docs.drawing_for_name(doc, "Footing design memo.pdf"), label)
            self.assertTrue(docs.drawing_for_name(doc, "623.0001 Rev A.pdf"), label)
        plan = self.make(["GENERAL ARRANGEMENT PLAN SCALE 1:100"], size=A3)
        doc = docs.extract("Plan.pdf", plan)
        self.assertTrue(doc["large_pages"])
        self.assertFalse(docs.drawing_for_name(doc, "Structural Design Report Rev 1.pdf"))
        self.assertTrue(docs.drawing_for_name(doc, "RD-ST-1202 [D] FOOTING PLAN.pdf"))
        # A DocText without the name-independent parts (cached before them) keeps its "drawing".
        self.assertTrue(docs.drawing_for_name({"drawing": True}, "Report.pdf"))
        self.assertFalse(docs.drawing_for_name({"kind": "docx", "drawing": False}, "ST-1200.docx"))

    def test_pdf_named_docx(self):
        doc = docs.extract("Not really.docx", self.make(["Bearing pressure 150 kPa in stiff clay."]))
        self.assertEqual(doc["kind"], "pdf")
        self.assertIn("150 kPa", " ".join(texts(doc)))

    def lines(self, doc):
        return [" ".join(t.split()) for t in texts(doc) if t]

    def test_locked_pdfs_open_without_a_password(self):
        for kind in ("rc4-128", "aes-128"):
            data = self.make(["Test certificate BL-LC-5512 strength 43.5 MPa"], encrypt=kind)
            for label, doc in self.each_reader(data, "Certificates.pdf"):
                self.assertEqual(doc["status"], "ok", (kind, label))
                self.assertIn("43.5 MPa", " ".join(texts(doc)), (kind, label))
            data = self.make(["Payment claim 07"], encrypt=kind, user_password=b"letmein")
            for label, doc in self.each_reader(data, "Claim.pdf"):
                self.assertEqual(doc["status"], "protected", (kind, label))

    def form_pdf(self, filled=True):
        """A form page: a filled text field and a typed comment (FreeText), both drawn by appearance streams."""
        w = pdf_builder.PdfWriter()
        font = w.add(pdf_builder.standard_font("Helvetica"))

        def appearance(text):
            return w.add(pdf_builder.Stream(b"/Tx BMC BT /Helv 10 Tf 2 4 Td (" + text + b") Tj ET EMC",
                                            {"Type": pdf_builder.Name("XObject"), "Subtype": pdf_builder.Name("Form"),
                                             "BBox": [0, 0, 300, 18], "Resources": {"Font": {"Helv": font}}}))
        name = pdf_builder.Name
        annots = [w.add({"Type": name("Annot"), "Subtype": name("Widget"), "FT": name("Tx"),
                         "Rect": [150, 600, 450, 618], "AP": {"N": appearance(b"Sam Brown" if filled else b"")}})]
        if filled:
            annots.append(w.add({"Type": name("Annot"), "Subtype": name("FreeText"), "Rect": [150, 400, 450, 418],
                                 "Contents": b"REVIEW: increase F12 to 2400 sq",
                                 "AP": {"N": appearance(b"REVIEW: increase F12 to 2400 sq, see RFI-047")}}))
        content = (b"BT /F1 12 Tf 72 700 Td (Inspection and test record for pad footing F12) Tj ET\n"
                   b"BT /F1 12 Tf 72 604 Td (Inspected by:) Tj ET\n")
        page = pdf_builder.page_dict(w, content, {"F1": font}, extra={"Annots": annots})
        return w.build(w.add(pdf_builder.catalog(w, [page])))

    def test_form_fields_and_typed_comments(self):
        for label, doc in self.each_reader(self.form_pdf(), "ITP form.pdf"):
            self.assertEqual(self.lines(doc), ["Inspection and test record for pad footing F12",
                                               "Inspected by: Sam Brown",
                                               "REVIEW: increase F12 to 2400 sq, see RFI-047"], label)
            self.assertEqual(doc["reader"], "pdftext" if label == "builtin" else "pypdf")
        for label, doc in self.each_reader(self.form_pdf(filled=False), "ITP blank.pdf"):
            self.assertEqual(self.lines(doc), ["Inspection and test record for pad footing F12", "Inspected by:"])

    def test_symbol_drawn_after_its_line(self):
        # LibreOffice draws a fallback-font symbol after the rest of its line; pypdf
        # alone puts it at the line end ("85 m on all steel.μ").
        w = pdf_builder.PdfWriter()
        fonts = {"F1": w.add(pdf_builder.standard_font("Helvetica")),
                 "F2": w.add(pdf_builder.standard_font("Symbol", encoding=None))}
        content = (b"BT /F1 12 Tf 1 0 0 1 72 700 Tm (Dry film thickness 85) Tj 1 0 0 1 200 700 Tm (m on all steel.) Tj ET\n"
                   b"BT /F2 12 Tf 1 0 0 1 193 700 Tm (m) Tj ET\n"
                   b"BT /F1 12 Tf 1 0 0 1 72 680 Tm (Rock strength) Tj 1 0 0 1 160 680 Tm (c = 25 MPa.) Tj ET\n"
                   b"BT /F2 12 Tf 1 0 0 1 152 680 Tm (s) Tj ET\n")
        page = pdf_builder.page_dict(w, content, fonts)
        data = w.build(w.add(pdf_builder.catalog(w, [page])))
        for label, doc in self.each_reader(data, "Spec.pdf"):
            self.assertEqual(self.lines(doc), ["Dry film thickness 85 μm on all steel.", "Rock strength σ c = 25 MPa."],
                             label)

    def test_symbol_and_wingdings_codes(self):
        # Word's PDFs: Symbol and Wingdings characters mapped to private-use codes U+F020-U+F0FF.
        w = pdf_builder.PdfWriter()

        def symbol_font(base):
            cmap = w.add(pdf_builder.Stream(b"1 begincodespacerange <00> <FF> endcodespacerange "
                                            b"1 beginbfrange <20> <FF> <F020> endbfrange"))
            name = pdf_builder.Name
            return w.add({"Type": name("Font"), "Subtype": name("TrueType"), "BaseFont": name(base),
                          "FirstChar": 32, "LastChar": 255, "Widths": [600] * 224, "ToUnicode": cmap,
                          "FontDescriptor": w.add({"Type": name("FontDescriptor"), "FontName": name(base),
                                                   "Flags": 4})})
        fonts = {"F1": w.add(pdf_builder.standard_font("Helvetica", widths=True)),
                 "S": symbol_font("ABCDEF+SymbolMT"), "W": symbol_font("ABCDEG+Wingdings-Regular")}
        content = (b"BT 72 700 Td /W 12 Tf (n) Tj /F1 12 Tf ( Item nu ) Tj /S 12 Tf (n) Tj /F1 12 Tf ( = 0.2 ) Tj "
                   b"/S 12 Tf (\\243) Tj /F1 12 Tf ( 6 m) Tj ET\n"
                   b"BT 72 680 Td /W 12 Tf (l) Tj /F1 12 Tf ( done ) Tj /W 12 Tf (\\374) Tj /F1 12 Tf ( box ) Tj "
                   b"/W 12 Tf (\\247) Tj ET\n")
        page = pdf_builder.page_dict(w, content, fonts)
        data = w.build(w.add(pdf_builder.catalog(w, [page])))
        for label, doc in self.each_reader(data, "Spec.pdf"):
            self.assertEqual(self.lines(doc), ["■ Item nu ν = 0.2 ≤ 6 m", "● done ✓ box ▪"], label)

    def test_review_markups(self):
        for label, doc in self.each_reader(pdf_builder.markup_pdf(), "SD-104 shop drawing review.pdf"):
            paras = texts(doc, "para")
            self.assertEqual([p for p in paras if p.startswith("[comment: ")], pdf_builder.MARKUP_COMMENTS, label)
            self.assertIn("REVISE AND RESUBMIT", texts(doc), label)
            self.assertIn("Base plate BP1 25 mm thick, 4 x M24 bolts.", paras, label)
            self.assertEqual(" ".join(texts(doc)).count("Base plate"), 1, label)

    def test_mostly_scanned_pdf_with_both_readers(self):
        letter = "\n".join("Line %d: pile P14 extended to 9.5 m, cut-off level RL 12.45, N40 bars." % n
                           for n in range(1, 5))
        data = self.make([letter] + [""] * 30)
        for label, doc in self.each_reader(data, "RFI 23 response with markups.pdf"):
            self.assertEqual((doc["status"], doc["note"]), ("ok", "pages 2-31 have no text (scanned?)"), label)
            self.assertIn("RL 12.45", " ".join(texts(doc)), label)

    def test_page_content_cut_at_a_cap_is_not_a_scan(self):
        content = b"1 2 m 3 4 l S\n" * 5000 + b"BT /F1 10 Tf 72 72 Td (TITLE BLOCK) Tj ET"
        w = pdf_builder.PdfWriter()
        page = pdf_builder.page_dict(w, content, {"F1": w.add(pdf_builder.standard_font())})
        data = w.build(w.add(pdf_builder.catalog(w, [page])))
        from squish_app import pdftext
        with mock.patch.dict(os.environ, {"SQUISH_NO_PYPDF": "1"}), \
                mock.patch.dict(docs._pypdf_state, {"checked": False, "module": None}), \
                mock.patch.object(pdftext, "MAX_CONTENT_BYTES", 1000):
            doc = docs.extract("Report.pdf", data)
        self.assertNotEqual(doc["note"], "scanned or image-only PDF")
        self.assertIn("incomplete", doc["note"])

    def test_wingdings_font_without_a_text_map(self):
        # A TrueType Wingdings 2 font with no /ToUnicode: pypdf would give "R" and "£".
        w = pdf_builder.PdfWriter()
        name = pdf_builder.Name
        wingdings = w.add({"Type": name("Font"), "Subtype": name("TrueType"), "BaseFont": name("ABCDEF+Wingdings2"),
                           "FirstChar": 32, "LastChar": 255, "Widths": [800] * 224,
                           "FontDescriptor": w.add({"Type": name("FontDescriptor"),
                                                    "FontName": name("ABCDEF+Wingdings2"), "Flags": 4})})
        content = (b"BT /F1 12 Tf 72 700 Td (Hold point released: Yes) Tj ET BT /W 12 Tf 260 700 Td (R) Tj ET "
                   b"BT /F1 12 Tf 280 700 Td (No) Tj ET BT /W 12 Tf 310 700 Td (\243) Tj ET")
        page = pdf_builder.page_dict(w, content, {"F1": w.add(pdf_builder.standard_font("Helvetica", widths=True)),
                                                  "W": wingdings})
        data = w.build(w.add(pdf_builder.catalog(w, [page])))
        for label, doc in self.each_reader(data, "ITP.pdf"):
            self.assertEqual(self.lines(doc), ["Hold point released: Yes ☑ No ☐"], label)
            self.assertEqual(doc["reader"], "pdftext", label)
        # The standard Symbol font is decoded by pypdf itself: pypdf stays the reader.
        w = pdf_builder.PdfWriter()
        fonts = {"F1": w.add(pdf_builder.standard_font("Helvetica", widths=True)),
                 "S": w.add(pdf_builder.standard_font("Symbol", encoding=None))}
        content = b"BT /F1 12 Tf 72 700 Td (Bearing pressure ) Tj /S 12 Tf (\263) Tj /F1 12 Tf ( 150 kPa) Tj ET"
        data = w.build(w.add(pdf_builder.catalog(w, [pdf_builder.page_dict(w, content, fonts)])))
        for label, doc in self.each_reader(data, "Spec.pdf"):
            self.assertIn("≥ 150 kPa", " ".join(texts(doc)), label)
            self.assertEqual(doc["reader"], "pypdf" if label == "pypdf" else "pdftext", label)

    def cid_pdf(self, program=True, to_unicode=False):
        """Type0 / Identity-H text; without /ToUnicode the text is only in the embedded font's cmap."""
        lines = ["Slab 250 mm", "Slab 250 mm slabs", "250 mm Slab"]
        w = pdf_builder.PdfWriter()
        name = pdf_builder.Name
        if to_unicode:
            font, encode = pdf_builder.identity_font(w, "".join(lines))
            shown = [encode(t) for t in lines]
        else:
            glyphs = dict((c, ord(c) - 29) for c in "Slab250ms ") if program else {"x": 99}
            descriptor = w.add({"Type": name("FontDescriptor"), "FontName": name("ABCDEF+Sub"), "Flags": 32,
                                "FontFile2": w.add(pdf_builder.Stream(pdf_builder.truetype_with_cmap(glyphs),
                                                                      filters=["FlateDecode"]))})
            descendant = w.add({"Type": name("Font"), "Subtype": name("CIDFontType2"),
                                "BaseFont": name("ABCDEF+Sub"), "DW": 500, "FontDescriptor": descriptor})
            font = w.add({"Type": name("Font"), "Subtype": name("Type0"), "BaseFont": name("ABCDEF+Sub"),
                          "Encoding": name("Identity-H"), "DescendantFonts": [descendant]})
            shown = [pdf_builder.Raw(b"<" + b"".join(b"%04X" % (ord(c) - 29) for c in t) + b">") for t in lines]
        content = b"".join(b"BT /F1 12 Tf 72 %d Td " % (700 - 20 * i) + pdf_builder.serialize(t) + b" Tj ET\n"
                           for i, t in enumerate(shown))
        page = pdf_builder.page_dict(w, content, {"F1": font})
        return w.build(w.add(pdf_builder.catalog(w, [page])))

    def test_composite_font_without_text_map(self):
        if importlib.util.find_spec("pypdf") is None:
            self.skipTest("pypdf not installed")
        wanted = ["Slab 250 mm", "Slab 250 mm slabs", "250 mm Slab"]
        for label, doc in self.each_reader(self.cid_pdf(), "Slab.pdf"):
            self.assertEqual((self.lines(doc), doc["reader"], doc["note"]), (wanted, "pdftext", ""), label)
        # No usable map in the font either: pypdf's text is kept, with a warning.
        doc = dict(self.each_reader(self.cid_pdf(program=False), "Slab.pdf"))["pypdf"]
        self.assertEqual((doc["status"], doc["reader"]), ("ok", "pypdf"))
        self.assertIn("garbled", doc["note"])
        # With a /ToUnicode map pypdf reads it itself.
        doc = dict(self.each_reader(self.cid_pdf(to_unicode=True), "Slab.pdf"))["pypdf"]
        self.assertEqual((self.lines(doc), doc["reader"], doc["note"]), (wanted, "pypdf", ""))


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
        self.assertEqual(doc["note"], "")
        with mock.patch.object(docs, "SHEET_ROWS_MAX", 2):
            doc = docs.extract("BoQ export.csv", data)
        self.assertEqual((doc["blocks"][0]["rows"], len(texts(doc, "row"))), (3, 2))
        self.assertEqual(doc["note"], "partly read: first 2 of 3 rows")

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


    SYMBOL_RTF = (b"{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl{\\f0\\fswiss\\fcharset0 Arial;}"
                  b"{\\f1\\froman\\fcharset2{\\*\\panose 05050102010706020507}Symbol;}{\\f2\\fnil\\fcharset2 Wingdings 2;}}"
                  b"\\f0 Slab \\f1\\'b3\\f0  200 mm, \\f1 f\\f0  = 0.8, film 85 {\\f1 m}m, {\\f1\\u-3917\\'b3} 1\\par "
                  b"Released {\\f2 R} Yes {\\f2\\'a3} No\\par}")

    def test_rtf_symbol_and_wingdings_characters(self):
        doc = docs.extract("SI-014.rtf", self.SYMBOL_RTF)
        self.assertEqual(texts(doc), ["Slab ≥ 200 mm, φ = 0.8, film 85 μm, ≥ 1", "Released ☑ Yes ☐ No"])
        # Emails (no symbol_char) read RTF exactly as before.
        self.assertEqual(msgfile.rtf_to_html_or_text(self.SYMBOL_RTF),
                         ("text", "Slab ³ 200 mm, f = 0.8, film 85 mm,  1\nReleased R Yes £ No\n"))


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
        # A member that was read carries the sha1 of its bytes (the digest spots
        # copies of documents also attached or filed on their own).
        self.assertEqual(members[3]["sha1"], hashlib.sha1(b"Plain note").hexdigest())
        self.assertNotIn("sha1", members[1])

    @unittest.skipIf(pdf_builder is None, "tests/pdf_builder.py not available")
    def test_stored_zip_starting_with_a_pdf_is_a_zip(self):
        files = {"SI-14/Site instruction 14.pdf": pdf_builder.simple_pdf(["Shoring to north wall excavation"]),
                 "RFI 22 response.pdf": pdf_builder.simple_pdf(["Pad footing to be 1800 square"]),
                 "Register.csv": "No,Title\n1,SI 14\n"}
        data = b.make_zip(files, zipfile.ZIP_STORED)
        self.assertIn(b"%PDF-", data[:1024])       # what used to make it look like a PDF
        for name in ("Transmittal 14.zip", "Transmittal 14.pdf"):
            doc = docs.extract(name, data)
            check_shape(self, doc)
            self.assertEqual((doc["kind"], doc["status"]), ("zip", "ok"), name)
            members = [bl for bl in doc["blocks"] if bl["type"] == "member"]
            self.assertEqual([m["text"] for m in members], list(files), name)
            self.assertEqual([m["doc"]["status"] for m in members], ["ok", "ok", "ok"], name)
            self.assertIn("Shoring to north wall", " ".join(texts(members[0]["doc"])))
            self.assertIn("1800 square", " ".join(texts(members[1]["doc"])))
        data = b.make_zip({"readme.txt": "Transmittal", "cover.pdf": pdf_builder.simple_pdf(["Cover"])},
                          zipfile.ZIP_STORED)
        self.assertEqual(docs.extract("t.zip", data)["kind"], "zip")

    def test_documents_in_a_zip_share_a_time_limit(self):
        data = b.make_zip({"a.docx": b.docx(b.para("One")), "photo.jpg": b"\xff\xd8 junk",
                           "b.docx": b.docx(b.para("Two"))})
        with mock.patch.object(docs, "ZIP_TIME_MAX", -1.0):
            doc = docs.extract("Transmittal.zip", data)
        check_shape(self, doc)
        members = [bl for bl in doc["blocks"] if bl["type"] == "member"]
        self.assertEqual([m["text"] for m in members], ["a.docx", "photo.jpg", "b.docx"])
        for m in (members[0], members[2]):
            self.assertEqual((m["doc"]["status"], m["doc"]["note"]),
                             ("error", "not read: the zip took too long to read"))
        self.assertNotIn("doc", members[1])
        self.assertTrue(doc["retry"])           # read again (once) next run
        self.assertNotIn("retry", docs.extract("Transmittal.zip", data))

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
