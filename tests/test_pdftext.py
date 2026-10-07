"""Tests for the built-in PDF text reader (pdftext.py). All PDFs are built in-test by pdf_builder."""

import os
import random
import struct
import tempfile
import time
import unittest
import zlib
from unittest import mock

from squish_app import pdftext
from tests import pdf_builder as pb
from tests.pdf_builder import A1_LANDSCAPE, A3, A4, Name, PdfWriter, Raw, Ref, Stream

RESULT_KEYS = {"pages", "page_sizes", "page_count", "title", "status", "note", "pages_read", "stopped"}


def extract(data, **kwargs):
    result = pdftext.extract_pdf(data, **kwargs)
    assert set(result) == RESULT_KEYS, result
    return result


def one_page(content, fonts=None, size=A4, xobjects=None, extra=None, version="1.4", **build):
    """A one-page PDF with this raw content stream; fonts default to F1 = Helvetica."""
    w = PdfWriter(version)
    if fonts is None:
        fonts = {"F1": w.add(pb.standard_font("Helvetica"))}
    elif callable(fonts):
        fonts = fonts(w)
    if callable(xobjects):
        xobjects = xobjects(w)
    page = pb.page_dict(w, content, fonts, size=size, xobjects=xobjects, extra=extra)
    root = w.add(pb.catalog(w, [page]))
    return w.build(root, **build)


class BasicTextTests(unittest.TestCase):

    def test_simple_text_and_title(self):
        data = pb.simple_pdf(["Hello World\nSecond line here"], title="Riverside Depot report")
        result = extract(data)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["note"], "")
        self.assertEqual(result["pages"], ["Hello World\nSecond line here"])
        self.assertEqual(result["page_count"], 1)
        self.assertEqual(result["title"], "Riverside Depot report")
        self.assertEqual(result["page_sizes"], [(595.3, 841.9)])

    def test_multi_page_and_max_pages(self):
        pages = ["Page %d text" % n for n in range(1, 8)]
        data = pb.simple_pdf(pages)
        result = extract(data)
        self.assertEqual(result["pages"], pages)
        self.assertEqual(result["page_count"], 7)
        limited = extract(data, max_pages=3)
        self.assertEqual(limited["pages"], pages[:3])
        self.assertEqual(len(limited["page_sizes"]), 3)
        self.assertEqual(limited["page_count"], 7)

    def test_winansi_characters(self):
        text = "Café 25°C ±5 mm – “quoted” €120"
        result = extract(pb.simple_pdf([text]))
        self.assertEqual(result["pages"], [text])

    def test_read_from_path(self):
        data = pb.simple_pdf(["From a file"])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "Geotech report.pdf")
            with open(path, "wb") as handle:
                handle.write(data)
            result = pdftext.extract_pdf(path=path)
            self.assertEqual(result["pages"], ["From a file"])
            missing = pdftext.extract_pdf(path=os.path.join(tmp, "nope.pdf"))
            self.assertEqual(missing["status"], "error")
            self.assertIn("could not be opened", missing["note"])

    def test_string_escapes_and_hex_strings(self):
        content = (b"BT /F1 12 Tf 72 700 Td (A \\(bracketed\\) word\\041 and \\\\ slash) Tj ET\n"
                   b"BT /F1 12 Tf 72 686 Td (nested (round) brackets) Tj ET\n"
                   b"BT /F1 12 Tf 72 672 Td <48 65 78 2> Tj ET\n"
                   b"BT /F1 12 Tf 72 658 Td (line\\\ncontinued) Tj ET\n")
        result = extract(one_page(content))
        self.assertEqual(result["pages"][0].split("\n"),
                         ["A (bracketed) word! and \\ slash", "nested (round) brackets", "Hex", "linecontinued"])

    def test_comments_and_names_with_escapes(self):
        content = (b"% a comment (not text) Tj\n"
                   b"BT /F#31 12 Tf 72 700 Td (Name escape) Tj ET % trailing\n")
        result = extract(one_page(content))
        self.assertEqual(result["pages"], ["Name escape"])


class LayoutTests(unittest.TestCase):

    def test_tj_kerning_and_word_gaps(self):
        content = (b"BT /F1 12 Tf 72 700 Td [(Ke) 20 (rn) -30 (ing)] TJ ET\n"
                   b"BT /F1 12 Tf 72 680 Td [(Hello) -300 (World) -2500 (Column)] TJ ET\n")
        result = extract(one_page(content))
        self.assertEqual(result["pages"], ["Kerning\nHello World  Column"])

    def test_words_placed_separately(self):
        # Words placed by Td without space characters get spaces; letters
        # placed one by one (no gap) do not.
        letters = b"".join(b"BT /F1 10 Tf %.2f 688 Td (%s) Tj ET\n" % (72 + 5.56 * i, c.encode())
                           for i, c in enumerate("abdenopq"))      # all 0.556 em wide
        content = (b"BT /F1 10 Tf 72 700 Td (Two) Tj 30 0 Td (words) Tj ET\n" + letters)
        result = extract(one_page(content))
        self.assertEqual(result["pages"], ["Two words\nabdenopq"])

    def test_lines_paragraphs_and_hyphenation(self):
        content = (b"BT /F1 10 Tf 12 TL 72 700 Td (The reinforce-) Tj T* (ment was checked. MS-) Tj T*"
                   b" (Word ends) Tj T* (well-) Tj T* (Known) Tj 0 -40 Td (New paragraph) Tj ET")
        result = extract(one_page(content))
        # A hyphen at a line end is kept (Word does not hyphenate by itself:
        # "self-weight"); the next word joins it.
        self.assertEqual(result["pages"][0],
                         "The reinforce-ment\nwas checked. MS-\nWord ends\nwell-\nKnown\n\nNew paragraph")

    def test_soft_hyphen_at_line_end_is_removed(self):
        # WinAnsi byte 0xAD is a soft hyphen (U+00AD): the word is joined without it.
        content = b"BT /F1 10 Tf 12 TL 72 700 Td (The reinforce\\255) Tj T* (ment was checked.) Tj ET"
        result = extract(one_page(content))
        self.assertEqual(result["pages"][0], "The reinforcement\nwas checked.")

    def test_rotated_text_and_late_glyph(self):
        # "Rotated label" is 58.9 pt long at 10 pt. The micro sign is drawn
        # last but belongs between "10 " (13.9 pt long) and "m".
        content = (b"BT /F1 10 Tf 0 1 -1 0 300 100 Tm (Rotated label) Tj ET\n"
                   b"BT /F1 10 Tf 0 1 -1 0 300 162 Tm (continues) Tj ET\n"
                   b"BT /F1 10 Tf 72 600 Td (10 ) Tj 19 0 Td (m) Tj ET\n"
                   b"BT /F1 10 Tf 86 600 Td (\\265) Tj ET\n")
        result = extract(one_page(content))
        self.assertEqual(result["pages"][0].split("\n"), ["Rotated label continues", "10 µm"])

    def test_text_matrix_scaling_and_ctm(self):
        content = (b"q 2 0 0 2 0 0 cm BT /F1 1 Tf 6 0 0 6 36 350 Tm (Scaled) Tj ( text) Tj ET Q\n"
                   b"q 1 0 0 1 0 -100 cm BT /F1 12 Tf 72 700 Td (Moved down) Tj ET Q\n")
        result = extract(one_page(content))
        self.assertEqual(result["pages"], ["Scaled text\n\nMoved down"])

    def test_quote_operators(self):
        content = b"BT /F1 10 Tf 14 TL 72 700 Td (First) Tj (Second) ' 2 1 (Third) \" ET"
        result = extract(one_page(content))
        self.assertEqual(result["pages"], ["First\nSecond\nThird"])

    def test_long_drawing_runs_are_skipped_but_cm_is_kept(self):
        lines = b"".join(b"%d %d m %d %d l S\n" % (i % 500, i % 700, i % 500 + 3, i % 700 + 2) for i in range(20000))
        content = (b"BT /F1 12 Tf 72 700 Td (Before drawing) Tj ET\n" + lines +
                   b"1 0 0 1 0 -300 cm\n" + lines +
                   b"BT /F1 12 Tf 72 700 Td (After drawing) Tj ET\n")
        start = time.monotonic()
        result = extract(one_page(content))
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(result["pages"][0].replace("\n\n", "\n"), "Before drawing\nAfter drawing")

    def test_inline_image_data_is_skipped(self):
        content = (b"BT /F1 12 Tf 72 700 Td (Above) Tj ET\n"
                   b"BI /W 4 /H 1 /BPC 8 /CS /G ID \x00(Fake) Tj BT ET\xff\xfe EI\n"
                   b"BT /F1 12 Tf 72 680 Td (Below) Tj ET\n")
        result = extract(one_page(content))
        self.assertEqual(result["pages"], ["Above\nBelow"])


class FontTests(unittest.TestCase):

    def test_identity_h_with_to_unicode(self):
        # bfchar, bfrange and the array form of bfrange (for the ligatures).
        text = "Bearing 150 kPa ≤ φ office"
        w = PdfWriter()
        font_ref, encode = pb.identity_font(w, text, ligatures=["ffi", "fi"])
        content = b"BT /F2 12 Tf 72 700 Td " + encode(text) + b" Tj ET"
        page = pb.page_dict(w, content, {"F2": font_ref})
        data = w.build(w.add(pb.catalog(w, [page])))
        cmap = zlib.decompress(data.split(b"stream\n")[1].split(b"\nendstream")[0])
        for word in (b"beginbfchar", b"beginbfrange", b"> ["):
            self.assertIn(word, cmap)
        result = extract(data)
        self.assertEqual(result["pages"], [text])

    def test_identity_h_without_to_unicode_guesses_unicode_codes(self):
        w = PdfWriter()
        descendant = w.add({"Type": Name("Font"), "Subtype": Name("CIDFontType2"),
                            "BaseFont": Name("PlainCID"), "DW": 500,
                            "FontDescriptor": w.add({"Type": Name("FontDescriptor"), "FontName": Name("PlainCID")})})
        font = w.add({"Type": Name("Font"), "Subtype": Name("Type0"), "BaseFont": Name("PlainCID"),
                      "Encoding": Name("Identity-H"), "DescendantFonts": [descendant]})
        content = b"BT /F1 12 Tf 72 700 Td <0048006900200074006800650072006500B0> Tj ET"
        page = pb.page_dict(w, content, {"F1": font})
        result = extract(w.build(w.add(pb.catalog(w, [page]))))
        self.assertEqual(result["pages"], ["Hi there°"])

    def test_identity_h_without_to_unicode_uses_embedded_truetype_cmap(self):
        # Glyph ids as a font subsetter would give them out; no /ToUnicode map.
        glyphs = {"S": 7, "l": 3, "a": 9, "b": 4, " ": 1, "2": 12, "5": 11, "0": 10, "m": 5}
        program = pb.truetype_with_cmap(glyphs)
        for with_map in (False, True):
            w = PdfWriter()
            descriptor = w.add({"Type": Name("FontDescriptor"), "FontName": Name("ABCDEF+Sub"),
                                "FontFile2": w.add(Stream(program, filters=["FlateDecode"]))})
            descendant = {"Type": Name("Font"), "Subtype": Name("CIDFontType2"), "BaseFont": Name("ABCDEF+Sub"),
                          "DW": 500, "FontDescriptor": descriptor}
            text = "Slab 250 mm"
            if with_map:      # CIDs 100, 101, ... mapped to the glyph ids by /CIDToGIDMap
                cids = {char: 100 + i for i, char in enumerate(sorted(glyphs))}
                table = bytearray(2 * 120)
                for char, cid in cids.items():
                    table[2 * cid:2 * cid + 2] = struct.pack(">H", glyphs[char])
                descendant["CIDToGIDMap"] = w.add(Stream(bytes(table)))
            else:
                cids = glyphs
            font = w.add({"Type": Name("Font"), "Subtype": Name("Type0"), "BaseFont": Name("ABCDEF+Sub"),
                          "Encoding": Name("Identity-H"), "DescendantFonts": [w.add(descendant)]})
            shown = b"<" + b"".join(b"%04X" % cids[char] for char in text) + b">"
            page = pb.page_dict(w, b"BT /F1 12 Tf 72 700 Td " + shown + b" Tj ET", {"F1": font})
            result = extract(w.build(w.add(pb.catalog(w, [page]))))
            self.assertEqual(result["pages"], [text], with_map)

    def test_mixed_one_and_two_byte_codes(self):
        # Encoding CMap: bytes 00-7F are one-byte codes, 8000-FFFF two-byte codes.
        encoding = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
                    b"/CMapName /Test-Mixed def\n"
                    b"2 begincodespacerange <00> <7F> <8000> <FFFF> endcodespacerange\n"
                    b"1 begincidrange <20> <7E> 1 endcidrange\n"
                    b"2 begincidchar <8140> 200 <8141> 201 endcidchar\n"
                    b"endcmap CMapName currentdict /CMap defineresource pop end end\n")
        to_unicode = (b"2 begincodespacerange <00> <7F> <8000> <FFFF> endcodespacerange\n"
                      b"1 beginbfrange <20> <7E> <0020> endbfrange\n"
                      b"2 beginbfchar <8140> <2264> <8141> <00B1> endbfchar\n")
        w = PdfWriter()
        descendant = w.add({"Type": Name("Font"), "Subtype": Name("CIDFontType0"), "BaseFont": Name("Mixed"),
                            "DW": 1000, "W": [1, 95, 500, 200, [600, 600]]})
        mixed = w.add({"Type": Name("Font"), "Subtype": Name("Type0"), "BaseFont": Name("Mixed"),
                       "Encoding": w.add(Stream(encoding)), "DescendantFonts": [descendant],
                       "ToUnicode": w.add(Stream(to_unicode))})
        ucs2 = w.add({"Type": Name("Font"), "Subtype": Name("Type0"), "BaseFont": Name("HeiseiMin-W3"),
                      "Encoding": Name("UniJIS-UCS2-H"),
                      "DescendantFonts": [w.add({"Type": Name("Font"), "Subtype": Name("CIDFontType0"),
                                                 "BaseFont": Name("HeiseiMin-W3")})]})
        content = (b"BT /M 12 Tf 72 700 Td <4C6F61642081402035206B4E20814120312520> Tj ET\n"
                   b"BT /U 12 Tf 72 680 Td <5DE54E8B0020004F004B> Tj ET\n")
        page = pb.page_dict(w, content, {"M": mixed, "U": ucs2})
        result = extract(w.build(w.add(pb.catalog(w, [page]))))
        self.assertEqual(result["pages"], ["Load ≤ 5 kN ± 1%\n工事 OK"])

    def test_differences_encoding_and_glyph_names(self):
        diffs = [1, "T", "e", "s", "t", "space", "eacute", "uni2014", "u1F600", "g41", "f_i", "one.oldstyle",
                 "degree", "Omega"]
        content = b"BT /F1 12 Tf 72 700 Td <0102030405060708090A0B0C0D0E> Tj ET"
        data = one_page(content, fonts=lambda w: {"F1": w.add(pb.standard_font("Times-Roman", differences=diffs))})
        result = extract(data)
        self.assertEqual(result["pages"], ["Test é—\U0001F600Afi1°Ω"])

    def test_simple_font_to_unicode_overrides_encoding(self):
        def fonts(w):
            cmap = w.add(Stream(b"/CIDInit /ProcSet findresource begin 1 begincodespacerange <00> <FF> "
                                b"endcodespacerange 2 beginbfchar <41> <0058> <42> <00590059> endbfchar "
                                b"1 beginbfrange <61> <63> <0031> endbfrange endcmap end"))
            font = pb.standard_font("Helvetica")
            font["ToUnicode"] = cmap
            return {"F1": w.add(font)}
        result = extract(one_page(b"BT /F1 12 Tf 72 700 Td (ABabc) Tj ET", fonts=fonts))
        self.assertEqual(result["pages"], ["XYY123"])

    def test_standard_and_symbol_encodings(self):
        def fonts(w):
            return {"F1": w.add(pb.standard_font("Times-Roman", encoding="StandardEncoding")),
                    "F2": w.add(pb.standard_font("Symbol", encoding=None)),
                    "F3": w.add(pb.standard_font("Helvetica", encoding="MacRomanEncoding")),
                    "F4": w.add(pb.standard_font("ZapfDingbats", encoding=None))}
        content = (b"BT /F1 12 Tf 72 700 Td (it\\047s \\256ne) Tj ET\n"
                   b"BT /F2 12 Tf 72 680 Td (a \\261 \\263 m) Tj ET\n"
                   b"BT /F3 12 Tf 72 660 Td (caf\\216) Tj ET\n"
                   b"BT /F4 12 Tf 72 640 Td (4) Tj ET\n")
        result = extract(one_page(content, fonts=fonts))
        self.assertEqual(result["pages"][0].split("\n"),
                         ["it’s fine", "α ± ≥ μ", "café", "✔"])

    def test_wingdings2_tick_boxes(self):
        # Word's PDFs use Wingdings 2 for form tick boxes: R = ☑, 0xA3 = ☐, T = ☒.
        for to_unicode in (False, True):
            def fonts(w):
                font = {"Type": Name("Font"), "Subtype": Name("TrueType"), "BaseFont": Name("ABCDEF+Wingdings2"),
                        "FontDescriptor": w.add({"Type": Name("FontDescriptor"), "FontName": Name("ABCDEF+Wingdings2"),
                                                 "Flags": 4})}
                if to_unicode:      # as Word writes it: codes mapped to private-use characters
                    font["ToUnicode"] = w.add(Stream(b"1 begincodespacerange <00> <FF> endcodespacerange "
                                                     b"1 beginbfrange <20> <FF> <F020> endbfrange"))
                return {"F1": w.add(pb.standard_font("Helvetica")), "F2": w.add(font)}
            content = (b"BT /F1 12 Tf 72 700 Td (Released:) Tj ET BT /F2 12 Tf 150 700 Td (R) Tj ET "
                       b"BT /F1 12 Tf 165 700 Td (Yes) Tj ET BT /F2 12 Tf 200 700 Td (\\243) Tj ET "
                       b"BT /F1 12 Tf 215 700 Td (No) Tj ET BT /F2 12 Tf 250 700 Td (T) Tj ET")
            result = extract(one_page(content, fonts=fonts))
            self.assertEqual(" ".join(result["pages"][0].split()), "Released: ☑ Yes ☐ No ☒", to_unicode)
        self.assertEqual(pdftext.symbol_font_kind("Wingdings 2"), "wingdings2")
        self.assertEqual(pdftext.symbol_font_kind("Wingdings-Regular"), "wingdings")
        self.assertEqual(pdftext.symbol_pua_table("symbol")[0xF06E], "\u03bd")      # ν

    def test_type3_font(self):
        def fonts(w):
            glyph = w.add(Stream(b"600 0 0 0 500 700 d1 0 0 500 700 re f"))
            return {"T3": w.add({
                "Type": Name("Font"), "Subtype": Name("Type3"), "FontBBox": [0, 0, 750, 750],
                "FontMatrix": [0.001, 0, 0, 0.001, 0, 0], "CharProcs": {"H": glyph, "i": glyph},
                "Encoding": {"Type": Name("Encoding"), "Differences": [65, Name("H"), Name("i")]},
                "FirstChar": 65, "LastChar": 66, "Widths": [600, 300], "Resources": {}})}
        result = extract(one_page(b"BT /T3 12 Tf 72 700 Td (AB) Tj ( AB) Tj ET", fonts=fonts))
        self.assertEqual(result["pages"], ["Hi Hi"])

    def test_missing_font_still_gives_text(self):
        result = extract(one_page(b"BT /Nope 12 Tf 72 700 Td (Still here) Tj ET"))
        self.assertEqual(result["pages"], ["Still here"])


class StructureTests(unittest.TestCase):

    def test_xref_stream_with_object_streams(self):
        pages = ["Object stream page one", "Page two"]
        for options in ({"xref_stream": True}, {"xref_stream": True, "predictor": True},
                        {"object_streams": True}, {"object_streams": True, "predictor": True},
                        {"hybrid": True}):
            data = pb.simple_pdf(pages, title="Café – report", **options)
            if options.get("object_streams"):
                self.assertIn(b"/ObjStm", data)
                self.assertNotIn(b"/Type /Page", data)
            result = extract(data)
            self.assertEqual(result["pages"], pages, options)
            self.assertEqual(result["title"], "Café – report")
            self.assertEqual(result["note"], "")

    def test_incremental_update_newer_objects_win(self):
        w = PdfWriter()
        font = w.add(pb.standard_font("Helvetica"))
        contents = w.add(Stream(pb.text_content(["Original wording"]), filters=["FlateDecode"]))
        page = w.add({"Type": Name("Page"), "MediaBox": [0, 0, 595, 842],
                      "Resources": {"Font": {"F1": font}}, "Contents": contents})
        root = w.add(pb.catalog(w, [page]))
        info = w.add({"Title": "Rev A"})
        base = w.build(root, info=info)
        updated = w.update(base, {contents: Stream(pb.text_content(["Revised wording"]), filters=["FlateDecode"]),
                                  info: {"Title": "Rev B"}}, root, info=info)
        self.assertEqual(extract(base)["pages"], ["Original wording"])
        result = extract(updated)
        self.assertEqual(result["pages"], ["Revised wording"])
        self.assertEqual(result["title"], "Rev B")

    def test_page_sizes_inheritance_crop_and_rotate(self):
        w = PdfWriter()
        font = w.add(pb.standard_font())
        tree = w.reserve()
        kids = []
        for extra in ({}, {"MediaBox": [0, 0, A3[0], A3[1]]}, {"Rotate": 90},
                      {"MediaBox": [0, 0, A1_LANDSCAPE[0], A1_LANDSCAPE[1]], "CropBox": [10, 10, 1010, 510]},
                      {"MediaBox": [0, 0, 100, 100], "UserUnit": 10}):
            contents = w.add(Stream(pb.text_content(["Sheet"])))
            page = {"Type": Name("Page"), "Parent": tree, "Contents": contents}
            page.update(extra)
            kids.append(w.add(page))
        # MediaBox and Resources inherited from the page tree node.
        w.set(tree, {"Type": Name("Pages"), "Kids": kids, "Count": len(kids),
                     "MediaBox": [0, 0, A4[0], A4[1]], "Resources": {"Font": {"F1": font}}})
        root = w.add({"Type": Name("Catalog"), "Pages": tree})
        result = extract(w.build(root))
        self.assertEqual(result["page_sizes"], [(595.3, 841.9), (841.9, 1190.5), (841.9, 595.3),
                                                (1000.0, 500.0), (1000.0, 1000.0)])
        self.assertEqual(result["pages"], ["Sheet"] * 5)

    def test_cyclic_and_deep_page_tree(self):
        w = PdfWriter()
        font = w.add(pb.standard_font())
        tree = w.reserve()
        inner = w.reserve()
        page1 = w.add({"Type": Name("Page"), "Parent": tree, "MediaBox": [0, 0, 200, 200],
                       "Resources": {"Font": {"F1": font}},
                       "Contents": w.add(Stream(pb.text_content(["First"], y=100)))})
        page2 = w.add({"Type": Name("Page"), "Parent": inner, "MediaBox": [0, 0, 200, 200],
                       "Resources": {"Font": {"F1": font}},
                       "Contents": w.add(Stream(pb.text_content(["Second"], y=100)))})
        # The tree lists itself, the inner node lists the root again and page1 twice.
        w.set(tree, {"Type": Name("Pages"), "Kids": [page1, inner, tree], "Count": 2})
        w.set(inner, {"Type": Name("Pages"), "Kids": [tree, page2, inner, page1], "Count": 1, "Parent": tree})
        root = w.add({"Type": Name("Catalog"), "Pages": tree})
        result = extract(w.build(root))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["pages"], ["First", "Second"])

    def test_form_xobject_text(self):
        def xobjects(w):
            inner_font = w.add(pb.standard_font("Courier"))
            nested = w.add(Stream(b"BT /FC 10 Tf 0 -12 Td (Nested form) Tj ET",
                                  {"Type": Name("XObject"), "Subtype": Name("Form"), "BBox": [0, 0, 300, 100],
                                   "Resources": {"Font": {"FC": inner_font}}}))
            form = w.add(Stream(b"BT /FC 10 Tf 0 0 Td (Title block text) Tj ET /Inner Do /Self Do",
                                {"Type": Name("XObject"), "Subtype": Name("Form"), "BBox": [0, 0, 300, 100],
                                 "Matrix": [1, 0, 0, 1, 100, 0],
                                 "Resources": {"Font": {"FC": inner_font}, "XObject": {"Inner": nested}}},
                                filters=["FlateDecode"]))
            # A form that draws itself must not loop forever.
            w.objects[form.num].entries["Resources"]["XObject"]["Self"] = form
            image = w.add(Stream(b"\xff" * 12, {"Type": Name("XObject"), "Subtype": Name("Image"),
                                               "Width": 2, "Height": 2, "BitsPerComponent": 8,
                                               "ColorSpace": Name("DeviceRGB")}, filters=["DCTDecode"]))
            return {"Fm1": form, "Im1": image}
        pb.ENCODERS["DCTDecode"] = lambda data: data
        try:
            content = (b"BT /F1 12 Tf 72 700 Td (Page text) Tj ET\n"
                       b"q 1 0 0 1 0 500 cm /Fm1 Do Q /Im1 Do /Missing Do\n")
            result = extract(one_page(content, xobjects=xobjects))
        finally:
            del pb.ENCODERS["DCTDecode"]
        self.assertEqual(result["pages"][0].split("\n"), ["Page text", "", "Title block text", "Nested form"])

    def test_form_field_appearances(self):
        w = PdfWriter()
        font = w.add(pb.standard_font("Helvetica"))
        appearance = w.add(Stream(b"/Tx BMC BT /Helv 10 Tf 2 4 Td (Lot 14 footing F3) Tj ET EMC",
                                  {"Type": Name("XObject"), "Subtype": Name("Form"), "BBox": [0, 0, 200, 18],
                                   "Resources": {"Font": {"Helv": font}}}))
        hidden = w.add(Stream(b"BT /Helv 10 Tf 2 4 Td (Hidden value) Tj ET",
                              {"Type": Name("XObject"), "Subtype": Name("Form"), "BBox": [0, 0, 200, 18],
                               "Resources": {"Font": {"Helv": font}}}))
        annots = [w.add({"Type": Name("Annot"), "Subtype": Name("Widget"), "Rect": [150, 600, 350, 618],
                         "AP": {"N": appearance}}),
                  w.add({"Type": Name("Annot"), "Subtype": Name("Widget"), "Rect": [150, 500, 350, 518],
                         "F": 2, "AP": {"N": hidden}})]
        page = pb.page_dict(w, b"BT /F1 12 Tf 72 604 Td (Lot:) Tj ET", {"F1": font}, extra={"Annots": annots})
        result = extract(w.build(w.add(pb.catalog(w, [page]))))
        self.assertEqual(result["pages"], ["Lot:  Lot 14 footing F3"])


    def test_form_field_value_without_appearance(self):
        # /NeedAppearances: the value is only in /V (here on the parent field), never drawn.
        w = PdfWriter()
        font = w.add(pb.standard_font("Helvetica"))
        parent = w.add({"FT": Name("Tx"), "T": b"comments", "V": b"UPDATED: HP3 released 13/06/2025\rsubject to NCR-017"})
        secret = w.add({"FT": Name("Tx"), "Ff": 1 << 13, "V": b"hunter2"})
        annots = [w.add({"Type": Name("Annot"), "Subtype": Name("Widget"), "Rect": [150, 580, 450, 618],
                         "Parent": parent}),
                  w.add({"Type": Name("Annot"), "Subtype": Name("Widget"), "Rect": [150, 500, 450, 518],
                         "Parent": secret})]
        content = b"BT /F1 12 Tf 72 604 Td (Comments:) Tj ET BT /F1 12 Tf 72 504 Td (Password:) Tj ET"
        page = pb.page_dict(w, content, {"F1": font}, extra={"Annots": annots})
        result = extract(w.build(w.add(pb.catalog(w, [page]))))
        lines = [" ".join(line.split()) for line in result["pages"][0].split("\n") if line]
        # The value's first line joins its label; annotations come after the page's own text.
        self.assertEqual(lines, ["Comments: UPDATED: HP3 released 13/06/2025", "Password:", "subject to NCR-017"])


class ReviewMarkupTests(unittest.TestCase):
    """Stamps, notes, clouds and highlights added in Bluebeam or Acrobat review."""

    def test_review_markups_become_comments(self):
        result = extract(pb.markup_pdf())
        lines = [line for line in result["pages"][0].split("\n") if line]
        self.assertEqual(lines[:2], ["Shop drawing SD-104 Rev 0", "Base plate BP1 25 mm thick, 4 x M24 bolts."])
        self.assertIn("REVISE AND RESUBMIT", lines)              # the stamp, as it is drawn
        comments = [line for line in lines if line.startswith("[comment: ")]
        self.assertEqual(comments, pb.MARKUP_COMMENTS)
        # Each comment is its own block, after the page's own text.
        self.assertIn("\n\n[comment: Anchor bolt", result["pages"][0])
        self.assertEqual(result["note"], "")

    def test_comments_already_on_the_page_are_not_repeated(self):
        page = "Footing F3 to be 900 deep\nOK to proceed"
        self.assertEqual(pdftext._with_comments(page, ["footing F3 to be 900 DEEP", "OK", "ok"]), page)
        self.assertEqual(pdftext._with_comments(page, ["No"]), page + "\n\n[comment: No]")
        self.assertEqual(pdftext._with_comments("Booked", ["ok"]), "Booked\n\n[comment: ok]")
        self.assertEqual(pdftext._with_comments("", ["Check"]), "[comment: Check]")


class SizeCapTests(unittest.TestCase):
    """Page content bigger than a cap is never read silently as 'no text'."""

    HEAVY = b"1 2 m 3 4 l S\n" * 5000 + b"BT /F1 10 Tf 72 72 Td (TITLE BLOCK) Tj ET"

    def form_pdf(self):
        def xobjects(w):
            font = w.add(pb.standard_font("Helvetica"))
            return {"Fm1": w.add(Stream(self.HEAVY, {"Type": Name("XObject"), "Subtype": Name("Form"),
                                                     "BBox": [0, 0, 600, 800],
                                                     "Resources": {"Font": {"F1": font}}},
                                        filters=["FlateDecode"]))}
        return one_page(b"/Fm1 Do", xobjects=xobjects)

    def test_content_streams_are_read_up_to_the_document_budget(self):
        self.assertGreaterEqual(pdftext.MAX_CONTENT_BYTES, pdftext.MAX_TOTAL_DECODED)
        for data in (one_page(self.HEAVY), self.form_pdf()):
            result = extract(data)
            self.assertEqual((result["pages"], result["note"]), (["TITLE BLOCK"], ""))
            with mock.patch.object(pdftext, "MAX_CONTENT_BYTES", 1000):
                capped = extract(data)
            self.assertEqual(capped["pages"], [""])
            self.assertIn("incomplete", capped["note"])

    def test_character_map_ranges_are_bounded_in_time(self):
        # One 'endbfrange' with 333 full ranges asks for 22 million entries.
        ranges = b"333 beginbfrange\n" + b"<0000> <FFFF> <0041>\n" * 333 + b"endbfrange\n"
        cmap = b"1 begincodespacerange <0000> <FFFF> endcodespacerange\n" + ranges
        for count in (1, 20):
            w = PdfWriter()
            fonts = {}
            content = []
            for n in range(count):
                font = pb.standard_font("Helvetica")
                font["ToUnicode"] = w.add(Stream(cmap, filters=["FlateDecode"]))
                fonts["F%d" % n] = w.add(font)
                content.append(b"BT /F%d 10 Tf 72 %d Td (A) Tj ET" % (n, 800 - 20 * n))
            page = pb.page_dict(w, b"\n".join(content), fonts)
            start = time.monotonic()
            result = extract(w.build(w.add(pb.catalog(w, [page]))), time_budget=2)
            self.assertLess(time.monotonic() - start, 6, count)
            if count == 20:
                self.assertEqual(result["stopped"], "time")

    def test_truetype_character_map_work_is_capped(self):
        segs = 30000
        ends = struct.pack(">%dH" % segs, *([0xFFFE] * segs))
        starts = struct.pack(">%dH" % segs, *([0x20] * segs))
        deltas = struct.pack(">%dh" % segs, *([1] * segs))
        body = struct.pack(">HHHH", 2 * segs, 0, 0, 0) + ends + b"\x00\x00" + starts + deltas + bytes(2 * segs)
        fmt4 = struct.pack(">HHH", 4, 0, 0) + body
        groups = 100000
        fmt12 = struct.pack(">HHIII", 12, 0, 16 + 12 * groups, 0, groups) + \
            struct.pack(">III", 0x20, 0x10020, 1) * groups
        for table in (fmt4, fmt12):
            start = time.monotonic()
            mapping = pdftext._cmap_subtable(table, 0)
            self.assertLess(time.monotonic() - start, 1.5)
            self.assertEqual(mapping[0x41], 0x42 if table is fmt4 else 0x22)


class EncryptionTests(unittest.TestCase):
    """Locked PDFs: an owner password only (they open without a password) are read."""

    def test_known_answers(self):
        self.assertEqual(pdftext._rc4(b"Key", b"Plaintext").hex(), "bbf316e8d940af0ad3")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        for key, cipher in (("000102030405060708090a0b0c0d0e0f", "69c4e0d86a7b0430d8cdb78070b4c55a"),
                            ("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
                             "8ea2b7ca516745bfeafc49904b496089")):      # FIPS-197 C.1 and C.3
            key = bytes.fromhex(key)
            self.assertEqual(pdftext._aes_cbc_encrypt(key, bytes(16), plain).hex(), cipher)
            self.assertEqual(pdftext._aes_cbc_decrypt(key, bytes.fromhex(cipher), iv=bytes(16), unpad=False), plain)

    def test_keys_from_another_implementation(self):
        # /O /U /ID and an encrypted Info string from PDFs written by pypdf.
        rc4 = pdftext._Security({
            "Filter": "Standard", "V": 2, "R": 3, "Length": 128, "P": 0,
            "O": bytes.fromhex("3579de908f71f3958370af350f7239155038530b5b7210c88b7b416d9572485a"),
            "U": bytes.fromhex("0ca5821dc5412538d6bd25f24b4d420328bf4e5e4e758a4164004e56fffa0108")},
            bytes.fromhex("6637376662326633373638653531376665346236363835363939343735393436"), lambda v: v)
        self.assertEqual(rc4.decrypt(1, 0, bytes.fromhex("8920c0095a487eab49a8ad30befc3a0d135de30b3e171a"), "rc4"),
                         b"Owner only test RC4-128")
        aes = pdftext._Security({
            "Filter": "Standard", "V": 5, "R": 6, "P": 0, "StmF": "StdCF", "StrF": "StdCF",
            "CF": {"StdCF": {"CFM": "AESV3", "Length": 32}},
            "O": bytes.fromhex("55d40d01262685581cb293dd96bcf7d1f00db02bb3ac3ddfc70655607daf7bd9"
                               "66fcfedd23a0d78be482a428f5dbd31f"),
            "U": bytes.fromhex("8b5239a451f3b20012b1f32927e1842e9276d222f686143f4900bebfae44542f"
                               "732703fa83e2d703693237fbbdc84e7b"),
            "UE": bytes.fromhex("14be44c971de3583e43c5ee48df7dd852f76dfd5fe036bf9369062a656f7c9ea")},
            b"", lambda v: v)
        self.assertEqual(aes.decrypt(1, 0, bytes.fromhex(
            "cae90f191b950c48595bf4d7fbb88d14fe36270b5760e5f22b13cf96ed926b67"
            "fef2019b3b802bd2b6a47da028fea564"), aes.string_method), b"Owner only test AES-256")

    def test_owner_password_only_files_are_read(self):
        cases = [("rc4-40", {}), ("rc4-128", {}), ("rc4-128", {"object_streams": True}),
                 ("rc4-128-v4", {}), ("aes-128", {}), ("aes-128", {"object_streams": True}),
                 ("aes-128", {"encrypt_metadata": False}), ("aes-256-r5", {}), ("aes-256", {})]
        for kind, options in cases:
            data = pb.simple_pdf(["Locked report (page 1): 43.5 MPa", "Second page"], title="Café – certificates",
                                 encrypt=kind, **options)
            result = extract(data)
            self.assertEqual(result["status"], "ok", (kind, options))
            self.assertEqual(result["pages"], ["Locked report (page 1): 43.5 MPa", "Second page"], (kind, options))
            self.assertEqual(result["title"], "Café – certificates", (kind, options))

    def test_files_that_need_a_password_stay_protected(self):
        for kind in ("rc4-128", "aes-128", "aes-256-r5"):
            result = extract(pb.simple_pdf(["Secret text"] * 2, encrypt=kind, user_password=b"letmein"))
            self.assertEqual(result["status"], "protected", kind)
            self.assertEqual(result["pages"], [], kind)
            self.assertEqual(result["page_count"], 2, kind)
            self.assertIn("password needed to open it", result["note"], kind)

    def test_other_locks_are_unsupported(self):
        w = PdfWriter()
        page = pb.page_dict(w, pb.text_content(["Certificate security"]), {"F1": w.add(pb.standard_font())})
        data = w.build(w.add(pb.catalog(w, [page])), trailer_extra={
            "Encrypt": w.add({"Filter": Name("Adobe.PubSec"), "V": 4, "R": 4})})
        result = extract(data)
        self.assertEqual(result["status"], "protected")
        self.assertIn("security Squish cannot read", result["note"])


class FilterTests(unittest.TestCase):

    def test_all_filters(self):
        text = pb.text_content(["Filtered text " * 4 + "end"])
        for filters in (["ASCIIHexDecode"], ["ASCII85Decode"], ["RunLengthDecode"], ["LZWDecode"],
                        ["ASCII85Decode", "FlateDecode"], ["ASCIIHexDecode", "LZWDecode"],
                        ["ASCII85Decode", "RunLengthDecode"], []):
            w = PdfWriter()
            font = w.add(pb.standard_font())
            page = pb.page_dict(w, text, {"F1": font}, filters=filters)
            result = extract(w.build(w.add(pb.catalog(w, [page]))))
            self.assertEqual(result["pages"], ["Filtered text " * 4 + "end"], filters)

    def test_lzw_long_data_and_early_change(self):
        rng = random.Random(5)
        data = bytes(rng.choice(b"abcdefgh ") for _ in range(40000))      # fills the table twice
        limits = pdftext._Limits(10)
        self.assertEqual(pdftext._lzw(pb.lzw(data), 1, limits, 10 ** 8), data)
        self.assertEqual(pdftext._lzw(pb.lzw(data, early_change=0), 0, limits, 10 ** 8), data)

    def test_ascii85_edge_cases(self):
        for raw in (b"", b"\x00\x00\x00\x00", b"abc", b"\xff" * 9, bytes(range(256))):
            encoded = pb.ascii85(raw)
            self.assertEqual(pdftext._ascii85(encoded, 10 ** 6), raw)
            self.assertEqual(pdftext._ascii85(b"<~" + encoded[:-2] + b"\n ~>", 10 ** 6), raw)
        big = bytes(range(256)) * 3000
        self.assertEqual(pdftext._ascii85(pb.ascii85(big), 10 ** 7), big)
        self.assertEqual(pdftext._ascii85(b"z" * 1000000 + b"~>", 1000), bytes(1000))

    def test_damaged_flate_stream_gives_partial_text(self):
        rng = random.Random(3)
        noise = bytes(rng.choice(b"abcdefghijklmnopqrstuvwxyz") for _ in range(5000))
        good = zlib.compress(pb.text_content(["Readable start of the page"]) + b"%" + noise)
        w = PdfWriter()
        font = w.add(pb.standard_font())
        contents = w.add(Stream(good[:len(good) // 2], {"Filter": Name("FlateDecode")}))
        page = {"Type": Name("Page"), "MediaBox": [0, 0, 595, 842], "Resources": {"Font": {"F1": font}},
                "Contents": contents}
        result = extract(w.build(w.add(pb.catalog(w, [page]))))
        self.assertEqual(result["pages"], ["Readable start of the page"])


class DamagedFileTests(unittest.TestCase):

    PAGES = ["Damaged file page one", "Damaged file page two", "Damaged file page three"]

    def test_broken_cross_reference_tables(self):
        for options in ({"break_xref": "offsets"}, {"break_xref": "missing"}, {"break_xref": "startxref"},
                        {"object_streams": True, "break_xref": "missing"},
                        {"object_streams": True, "break_xref": "offsets"},
                        {"prefix": b"junk before the header\n"}):
            result = extract(pb.simple_pdf(self.PAGES, title="Recovered", **options))
            self.assertEqual(result["status"], "ok", options)
            self.assertEqual(result["pages"], self.PAGES, options)
            self.assertEqual(result["title"], "Recovered", options)
            if options.get("break_xref"):
                self.assertIn("damaged", result["note"], options)

    def test_truncated_file(self):
        data = pb.simple_pdf(["Long first page text"] + ["More text"] * 5)
        for cut in (0.95, 0.8, 0.6, 0.4, 0.2):        # the page objects are in the second half
            result = extract(data[:int(len(data) * cut)])
            if cut >= 0.6:
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["pages"][0], "Long first page text", cut)
                self.assertIn("damaged", result["note"])
            else:
                self.assertEqual(result["status"], "error")
                self.assertIn("No pages found", result["note"])

    def test_wrong_stream_length(self):
        w = PdfWriter()
        font = w.add(pb.standard_font())
        page = pb.page_dict(w, pb.text_content(["Length is wrong"]), {"F1": font})
        w.objects[page["Contents"].num].length = 5
        result = extract(w.build(w.add(pb.catalog(w, [page]))))
        self.assertEqual(result["pages"], ["Length is wrong"])

    def test_encrypted_marker(self):
        result = extract(pb.simple_pdf(["Secret text"] * 3, encrypt=True))
        self.assertEqual(result["status"], "protected")
        self.assertEqual(result["pages"], [])
        self.assertEqual(result["page_count"], 3)
        self.assertEqual(len(result["page_sizes"]), 3)
        self.assertIn("encrypted", result["note"])

    def test_not_a_pdf_and_empty_input(self):
        for data in (b"", b"PK\x03\x04 a zip file", b"%PDF-1.4\n", b"\x00" * 1000, "text".encode()):
            result = extract(data)
            self.assertEqual(result["status"], "error", data[:20])
            self.assertTrue(result["note"])
        self.assertEqual(pdftext.extract_pdf()["status"], "error")

    def test_deep_nesting(self):
        deep = b"[" * 200000 + b"]" * 200000
        content = deep + b" << " * 5000 + b"\nBT /F1 12 Tf 72 700 Td (After nesting) Tj ET"
        result = extract(one_page(content))
        self.assertEqual(result["pages"], ["After nesting"])
        # A nested object elsewhere in the file (here the Info dictionary).
        w = PdfWriter()
        font = w.add(pb.standard_font())
        page = pb.page_dict(w, pb.text_content(["Body text"]), {"F1": font})
        info = w.add(Raw(b"<< /Title " + b"[" * 100000 + b">>"))
        result = extract(w.build(w.add(pb.catalog(w, [page])), info=info))
        self.assertEqual(result["pages"], ["Body text"])
        self.assertEqual(result["title"], "")

    def test_decompression_bomb_is_capped(self):
        bomb = zlib.compress(b"0 0 m " * (3 * 1024 * 1024 // 6), 9)
        w = PdfWriter()
        font = w.add(pb.standard_font())
        pages = []
        for _ in range(4):
            bomb_ref = w.add(Raw(b"<< /Filter /FlateDecode /Length %d >>\nstream\n" % len(bomb) + bomb + b"\nendstream"))
            text = w.add(Stream(pb.text_content(["Text after the bomb"])))
            pages.append({"Type": Name("Page"), "MediaBox": [0, 0, 595, 842],
                          "Resources": {"Font": {"F1": font}}, "Contents": [bomb_ref, text]})
        data = w.build(w.add(pb.catalog(w, pages)))
        with mock.patch.object(pdftext, "MAX_STREAM_BYTES", 1024 * 1024), \
                mock.patch.object(pdftext, "MAX_TOTAL_DECODED", 5 * 1024 * 1024):
            start = time.monotonic()
            result = extract(data)
            self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["pages"][0], "Text after the bomb")
        self.assertIn("too large", result["note"])
        self.assertEqual(len(result["pages"]), 4)

    def test_time_budget(self):
        content = b"BT /F1 12 Tf 72 700 Td (Start) Tj ET\n" + b"q Q " * 300000
        result = extract(one_page(content), time_budget=0)
        self.assertEqual(result["status"], "ok")
        self.assertIn("Stopped after 0 seconds", result["note"])
        self.assertEqual(result["pages"], ["Start"])
        self.assertEqual((result["pages_read"], result["stopped"]), (1, "time"))

    def test_pages_not_reached_are_counted(self):
        # Page 1 is slow: the time budget runs out on it; pages 2-3 are never reached.
        slow = pb.text_content(["Page one"]) + b"q Q " * 300000
        w = PdfWriter()
        font = w.add(pb.standard_font("Helvetica"))
        pages = [pb.page_dict(w, slow, {"F1": font})] + [
            pb.page_dict(w, pb.text_content(["Page %d" % n]), {"F1": font}) for n in (2, 3)]
        result = extract(w.build(w.add(pb.catalog(w, pages))), time_budget=0)
        self.assertEqual(result["pages"], ["Page one", "", ""])
        self.assertEqual((result["pages_read"], result["stopped"]), (1, "time"))
        self.assertIn("pages from 2 on were not read", result["note"])
        whole = extract(pb.simple_pdf(["a", "b"]))
        self.assertEqual((whole["pages_read"], whole["stopped"]), (2, ""))

    def test_stop_function_stops_between_pages(self):
        asked = []

        def stop():
            asked.append(1)
            return len(asked) > 2
        result = extract(pb.simple_pdf(["One", "Two", "Three", "Four"]), stop=stop)
        self.assertEqual(result["pages"], ["One", "Two", "", ""])
        self.assertEqual((result["pages_read"], result["stopped"]), (2, "cancelled"))

    def test_operator_cap_keeps_partial_page(self):
        content = b"BT /F1 12 Tf 72 700 Td (Start) Tj ET\n" + b"q Q " * 50000 + b"BT /F1 12 Tf 72 600 Td (Never) Tj ET"
        with mock.patch.object(pdftext, "MAX_OPS_PER_PAGE", 1000):
            result = extract(one_page(content))
        self.assertEqual(result["pages"], ["Start"])
        self.assertIn("incomplete", result["note"])

    def test_reference_loops_and_bad_values(self):
        w = PdfWriter()
        loop_a = w.reserve()
        loop_b = w.reserve()
        w.set(loop_a, loop_b)
        w.set(loop_b, loop_a)
        font = w.add(pb.standard_font())
        page = pb.page_dict(w, b"BT /F1 12 Tf 72 700 Td (Survives) Tj ET 1 2 3 Tm (x) Tj BT /F1 () Tf ET",
                            {"F1": font}, extra={"MediaBox": loop_a, "Rotate": Ref(999)})
        result = extract(w.build(w.add(pb.catalog(w, [page])), info=loop_b))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["pages"][0], "Survives\n\nx")      # "x" lands at 0, 0
        self.assertEqual(result["page_sizes"], [(612.0, 792.0)])

    def test_random_damage_never_raises(self):
        base = pb.simple_pdf(["Fuzz page one " * 5, "Fuzz page two"], title="Fuzz", object_streams=True)
        plain = pb.simple_pdf(["Plain " * 20, "Two"], title="Plain")
        rng = random.Random(1234)
        start = time.monotonic()
        for i in range(150):
            data = bytearray(base if i % 2 else plain)
            for _ in range(rng.randint(1, 6)):
                where = rng.randrange(len(data))
                action = rng.randrange(5)
                if action == 0:
                    data[where] = rng.randrange(256)
                elif action == 1:
                    del data[where:where + rng.randint(1, 200)]
                elif action == 2:
                    data[where:where] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 50)))
                elif action == 3:
                    data[where:where] = rng.choice([b"[", b"<<", b"(", b"<", b"%", b" 0 R", b"obj", b"stream"])
                else:
                    del data[where:]
            result = extract(bytes(data), time_budget=5)
            self.assertIn(result["status"], ("ok", "error", "protected"))
            self.assertIsInstance(result["note"], str)
            self.assertTrue(all(isinstance(p, str) for p in result["pages"]))
        self.assertLess(time.monotonic() - start, 30)


class HelperTests(unittest.TestCase):

    def test_parse_object_values(self):
        value, end = pdftext._parse_object(
            b"<< /Type /Page /A [1 2.5 -3 .5 +4] /S (a\\)b) /H <41 42> /R 12 0 R /N null /T true /E#20x 1 >>", 0)
        self.assertEqual(value, {"Type": "Page", "A": [1, 2.5, -3, 0.5, 4], "S": b"a)b", "H": b"AB",
                                 "R": pdftext.Ref(12, 0), "T": True, "E x": 1})
        self.assertEqual(pdftext._parse_object(b"7 0 R", 0)[0], pdftext.Ref(7, 0))
        self.assertEqual(pdftext._parse_object(b"  42 endobj", 0)[0], 42)
        self.assertEqual(pdftext._parse_object(b"<< /Open [1 2 endobj", 0)[0], {"Open": [1, 2]})

    def test_glyph_names(self):
        for name, text in (("eacute", "é"), ("Scaron", "Š"), ("alpha", "α"),
                           ("lambda", "λ"), ("uni00410042", "AB"), ("u2264", "≤"),
                           ("f_f_i", "ffi"), ("a.sc", "a"), ("G30", "0"), ("c65", "A"),
                           ("quotedblleft", "“"), ("nonsense12", "")):
            self.assertEqual(pdftext._glyph_text(name), text, name)

    def test_title_encodings(self):
        self.assertEqual(pdftext._text_string(b"\xfe\xff\x00R\x00e\x00v\x20\x13\x00B"), "Rev–B")
        self.assertEqual(pdftext._text_string(b"Report \x84 draft\x92"), "Report — draft™")


if __name__ == "__main__":
    unittest.main()
