"""Test helper: build small but valid .docx, .xlsx, .pptx and .zip files in memory.

Standard library only (zipfile + hand-written XML), so the tests need none of
the packages that normally write Office files. All content is synthetic.
PDFs are made by tests/pdf_builder.py.
"""

import io
import zipfile
from xml.sax.saxutils import escape

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
WPS_NS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
V_NS = "urn:schemas-microsoft-com:vml"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
DGM_NS = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
XDR_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
C_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"

XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def make_zip(files, compression=zipfile.ZIP_DEFLATED):
    """Zip ``{name: bytes or str}`` into bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in files.items():
            if isinstance(data, str):
                data = data.encode("utf-8")
            zf.writestr(name, data)
    return buf.getvalue()


def rels_xml(rels):
    """Relationships part from [(id, type suffix, target), ...]."""
    items = "".join('<Relationship Id="%s" Type="%s%s" Target="%s"/>' % (rid, REL, t, escape(target))
                    for rid, t, target in rels)
    return XML_HEAD + '<Relationships xmlns="%s">%s</Relationships>' % (PKG_REL_NS, items)


def content_types(overrides):
    items = "".join('<Override PartName="/%s" ContentType="%s"/>' % (name, ct) for name, ct in overrides)
    return (XML_HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>%s</Types>' % items)


def core_xml(title):
    return (XML_HEAD + '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
            'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:title>%s</dc:title><dc:creator>Sam Brown</dc:creator></cp:coreProperties>' % escape(title))


def app_xml(pages):
    return (XML_HEAD + '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
            'extended-properties"><Pages>%d</Pages></Properties>' % pages)


def package(main_part, main_type, parts, title=None, pages=None):
    """Assemble an Office package: content types, package rels, core/app properties."""
    rels = [("rId1", "officeDocument", main_part)]
    files = {}
    if title is not None:
        rels.append(("rId2", "metadata/core-properties", "docProps/core.xml"))
        files["docProps/core.xml"] = core_xml(title)
    if pages is not None:
        rels.append(("rId3", "extended-properties", "docProps/app.xml"))
        files["docProps/app.xml"] = app_xml(pages)
    files["[Content_Types].xml"] = content_types([(main_part, main_type)])
    files["_rels/.rels"] = rels_xml(rels).replace(REL + "metadata/core-properties",
                                                  "http://schemas.openxmlformats.org/package/2006/"
                                                  "relationships/metadata/core-properties")
    files.update(parts)
    return make_zip(files)


# --------------------------------------------------------------------------
# Word
# --------------------------------------------------------------------------

def run(text, deleted=False):
    """A w:r run (a w:delText run when deleted)."""
    if deleted:
        return '<w:r><w:delText xml:space="preserve">%s</w:delText></w:r>' % escape(text)
    return '<w:r><w:t xml:space="preserve">%s</w:t></w:r>' % escape(text)


def para(content="", style=None, num=None, outline=None):
    """A w:p. ``content`` is plain text (made into a run) or raw XML starting with '<'.

    ``num`` = (numId, ilvl) adds list numbering.
    """
    ppr = ""
    if style:
        ppr += '<w:pStyle w:val="%s"/>' % style
    if num is not None:
        ppr += '<w:numPr><w:ilvl w:val="%d"/><w:numId w:val="%s"/></w:numPr>' % (num[1], num[0])
    if outline is not None:
        ppr += '<w:outlineLvl w:val="%d"/>' % outline
    if ppr:
        ppr = "<w:pPr>%s</w:pPr>" % ppr
    if content and not content.startswith("<"):
        content = run(content)
    return "<w:p>%s%s</w:p>" % (ppr, content)


def table(rows, merged=None):
    """A w:tbl from rows of cell texts. ``merged`` = set of (row, col) vMerge continuations."""
    merged = merged or set()
    out = ['<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr><w:tblGrid/>']
    for r, cells in enumerate(rows):
        out.append("<w:tr>")
        for c, text in enumerate(cells):
            if (r, c) in merged:
                out.append('<w:tc><w:tcPr><w:vMerge/></w:tcPr>%s</w:tc>' % para(""))
            else:
                body = text if text.startswith("<") else para(text)
                out.append("<w:tc><w:tcPr/>%s</w:tc>" % body)
        out.append("</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def text_box(*paragraphs):
    """A run holding a modern text box (mc:AlternateContent with a VML fallback copy)."""
    inner = "".join(para(p) for p in paragraphs)
    return ('<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wps:wsp><wps:txbx>'
            '<w:txbxContent>%s</w:txbxContent></wps:txbx></wps:wsp></w:drawing></mc:Choice>'
            '<mc:Fallback><w:pict><v:shape><v:textbox><w:txbxContent>%s</w:txbxContent>'
            '</v:textbox></v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r>' % (inner, inner))


HEADING_STYLES = (
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>'
    '<w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>'
    '<w:basedOn w:val="Heading1"/><w:pPr><w:outlineLvl w:val="1"/></w:pPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Kop3"><w:name w:val="heading 3"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="MyHeading"><w:name w:val="Report Heading"/>'
    '<w:basedOn w:val="Heading2"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/>'
    '<w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="TOC1"><w:name w:val="toc 1"/></w:style>'
)


def styles_xml(extra=""):
    return XML_HEAD + '<w:styles xmlns:w="%s">%s%s</w:styles>' % (W_NS, HEADING_STYLES, extra)


def numbering_xml():
    """numId 1: bullets; numId 2: decimal "1." / "a)"; numId 3: same list restarted at 1;
    numId 4: legal "1" / "1.1" heading numbering."""
    return (XML_HEAD + '<w:numbering xmlns:w="%s">'
            '<w:abstractNum w:abstractNumId="0">'
            '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val=""/></w:lvl>'
            '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="o"/></w:lvl>'
            '</w:abstractNum>'
            '<w:abstractNum w:abstractNumId="1">'
            '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%%1."/></w:lvl>'
            '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="%%2)"/></w:lvl>'
            '</w:abstractNum>'
            '<w:abstractNum w:abstractNumId="2">'
            '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%%1"/></w:lvl>'
            '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%%1.%%2"/></w:lvl>'
            '</w:abstractNum>'
            '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
            '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>'
            '<w:num w:numId="3"><w:abstractNumId w:val="1"/>'
            '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride></w:num>'
            '<w:num w:numId="4"><w:abstractNumId w:val="2"/></w:num>'
            '</w:numbering>' % W_NS)


def notes_xml(kind, notes):
    """footnotes.xml / endnotes.xml from {id: text} (plus the separator notes Word adds)."""
    tag = "footnote" if kind == "footnotes" else "endnote"
    items = ['<w:%s w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:%s>' % (tag, tag),
             '<w:%s w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/>'
             '</w:r></w:p></w:%s>' % (tag, tag)]
    for nid, text in notes.items():
        items.append('<w:%s w:id="%s"><w:p><w:r><w:%sRef/></w:r>%s</w:p></w:%s>'
                     % (tag, nid, tag, run(" " + text), tag))
    return XML_HEAD + '<w:%s xmlns:w="%s">%s</w:%s>' % (kind, W_NS, "".join(items), kind)


def comments_xml(comments):
    items = "".join('<w:comment w:id="%s" w:author="Sam Brown"><w:p>%s</w:p></w:comment>'
                    % (cid, run(text)) for cid, text in comments.items())
    return XML_HEAD + '<w:comments xmlns:w="%s">%s</w:comments>' % (W_NS, items)


# Namespaces declared on the root of every Word part written here.
_W_ROOT_NS = ('xmlns:w="%s" xmlns:r="%s" xmlns:mc="%s" xmlns:wps="%s" xmlns:v="%s" xmlns:m="%s" '
              'xmlns:a="%s" xmlns:dgm="%s"' % (W_NS, R_NS, MC_NS, WPS_NS, V_NS, M_NS, A_NS, DGM_NS))


def hdr_xml(kind, *paragraphs):
    tag = "hdr" if kind == "header" else "ftr"
    return XML_HEAD + '<w:%s %s>%s</w:%s>' % (tag, _W_ROOT_NS, "".join(paragraphs), tag)


def document_xml(body):
    return XML_HEAD + '<w:document %s><w:body>%s<w:sectPr/></w:body></w:document>' % (_W_ROOT_NS, body)


def math(*parts):
    """An m:oMath equation from raw Office Math XML parts (see mr)."""
    return "<m:oMath>%s</m:oMath>" % "".join(parts)


def mr(text):
    """An Office Math run (m:r) with a little of the formatting Word writes around it."""
    return ('<m:r><m:rPr><m:sty m:val="p"/></m:rPr><w:rPr><w:rFonts w:ascii="Cambria Math"/></w:rPr>'
            '<m:t>%s</m:t></m:r>' % escape(text))


def sup_run(text, align="superscript"):
    """A w:r raised (superscript) or lowered (subscript)."""
    return ('<w:r><w:rPr><w:vertAlign w:val="%s"/></w:rPr><w:t xml:space="preserve">%s</w:t></w:r>'
            % (align, escape(text)))


def font_run(font, text):
    """A w:r set in ``font`` (e.g. Symbol, where "m" shows as μ)."""
    return ('<w:r><w:rPr><w:rFonts w:ascii="%s" w:hAnsi="%s"/></w:rPr><w:t xml:space="preserve">%s</w:t></w:r>'
            % (font, font, escape(text)))


def diagram_data_xml(*texts):
    """A SmartArt data part with one point per text (plus the empty document point)."""
    points = ['<dgm:pt modelId="0" type="doc"><dgm:prSet/><dgm:spPr/><dgm:t><a:bodyPr/><a:p/></dgm:t></dgm:pt>']
    for i, text in enumerate(texts, 1):
        points.append('<dgm:pt modelId="%d"><dgm:prSet/><dgm:spPr/><dgm:t><a:bodyPr/><a:p><a:r><a:t>%s'
                      '</a:t></a:r></a:p></dgm:t></dgm:pt>' % (i, escape(text)))
    return (XML_HEAD + '<dgm:dataModel xmlns:dgm="%s" xmlns:a="%s"><dgm:ptLst>%s</dgm:ptLst></dgm:dataModel>'
            % (DGM_NS, A_NS, "".join(points)))


def smartart(rid):
    """A run holding a SmartArt drawing whose data part has relationship id ``rid``."""
    return ('<w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/'
            'wordprocessingDrawing"><wp:docPr id="1" name="Diagram 1"/><a:graphic><a:graphicData uri="%s">'
            '<dgm:relIds r:dm="%s" r:lo="rIdL" r:qs="rIdQ" r:cs="rIdC"/></a:graphicData></a:graphic>'
            '</wp:inline></w:drawing></w:r>' % (DGM_NS, rid))


def watermark(text):
    """A header paragraph holding Word's VML text watermark (with the shape type it uses)."""
    return ('<w:p><w:r><w:pict><v:shapetype id="_x0000_t136" coordsize="21600,21600"><v:textpath on="t" '
            'fitshape="t"/></v:shapetype><v:shape id="PowerPlusWaterMarkObject1" type="#_x0000_t136">'
            '<v:textpath style="font-family:Calibri" string="%s"/></v:shape></w:pict></w:r></w:p>' % escape(text))


def docx(body, title=None, styles=True, numbering=False, footnotes=None, endnotes=None,
         comments=None, headers=(), footers=(), pages=None, raw_document=None, extra_parts=None,
         extra_rels=()):
    """A .docx whose body is ``body`` (XML of w:p / w:tbl elements).

    ``numbering`` is True (the standard test lists) or a numbering part's XML.
    ``extra_parts`` {name: XML} and ``extra_rels`` [(id, type, target)] add parts
    related to the main document (e.g. SmartArt data).
    """
    parts = {"word/document.xml": raw_document if raw_document is not None else document_xml(body)}
    rels = list(extra_rels)
    parts.update(extra_parts or {})
    if styles:
        parts["word/styles.xml"] = styles_xml()
        rels.append(("rId1", "styles", "styles.xml"))
    if numbering:
        parts["word/numbering.xml"] = numbering if isinstance(numbering, str) else numbering_xml()
        rels.append(("rId2", "numbering", "numbering.xml"))
    if footnotes:
        parts["word/footnotes.xml"] = notes_xml("footnotes", footnotes)
        rels.append(("rId3", "footnotes", "footnotes.xml"))
    if endnotes:
        parts["word/endnotes.xml"] = notes_xml("endnotes", endnotes)
        rels.append(("rId4", "endnotes", "endnotes.xml"))
    if comments:
        parts["word/comments.xml"] = comments_xml(comments)
        rels.append(("rId5", "comments", "comments.xml"))
    for i, xml in enumerate(headers):
        parts["word/header%d.xml" % (i + 1)] = xml
        rels.append(("rIdH%d" % i, "header", "header%d.xml" % (i + 1)))
    for i, xml in enumerate(footers):
        parts["word/footer%d.xml" % (i + 1)] = xml
        rels.append(("rIdF%d" % i, "footer", "footer%d.xml" % (i + 1)))
    parts["word/_rels/document.xml.rels"] = rels_xml(rels)
    return package("word/document.xml",
                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
                   parts, title=title, pages=pages)


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------

def c_num(ref, value, style=None):
    s = ' s="%d"' % style if style is not None else ""
    return '<c r="%s"%s><v>%s</v></c>' % (ref, s, value)


def c_shared(ref, index):
    return '<c r="%s" t="s"><v>%d</v></c>' % (ref, index)


def c_inline(ref, text):
    return '<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (ref, escape(text))


def c_formula(ref, formula, cached=None, kind=None, style=None):
    t = ' t="%s"' % kind if kind else ""
    s = ' s="%d"' % style if style is not None else ""
    v = "<v>%s</v>" % escape(str(cached)) if cached is not None else ""
    return '<c r="%s"%s%s><f>%s</f>%s</c>' % (ref, t, s, escape(formula), v)


def c_bool(ref, value):
    return '<c r="%s" t="b"><v>%d</v></c>' % (ref, 1 if value else 0)


def c_error(ref, text):
    return '<c r="%s" t="e"><v>%s</v></c>' % (ref, escape(text))


def sheet_xml(rows):
    """Worksheet XML from rows: each row is (row number, [cell XML ...])."""
    body = "".join('<row r="%d">%s</row>' % (r, "".join(cells)) for r, cells in rows)
    return XML_HEAD + '<worksheet xmlns="%s"><dimension ref="A1"/><sheetData>%s</sheetData></worksheet>' % (
        S_NS, body)


def drawing_xml(*anchors):
    """A spreadsheet drawing part (xdr:wsDr) holding these anchors (see text_box_anchor)."""
    return (XML_HEAD + '<xdr:wsDr xmlns:xdr="%s" xmlns:a="%s" xmlns:r="%s" xmlns:mc="%s" xmlns:c="%s">%s'
            '</xdr:wsDr>' % (XDR_NS, A_NS, R_NS, MC_NS, C_NS, "".join(anchors)))


def xdr_shape(paragraphs, name="TextBox 1", hidden=False):
    """An xdr:sp text box with these paragraphs."""
    hide = ' hidden="1"' if hidden else ""
    paras = "".join('<a:p><a:r><a:t>%s</a:t></a:r></a:p>' % escape(t) for t in paragraphs)
    return ('<xdr:sp><xdr:nvSpPr><xdr:cNvPr id="2" name="%s"%s/><xdr:cNvSpPr txBox="1"/></xdr:nvSpPr>'
            '<xdr:spPr/><xdr:txBody><a:bodyPr/>%s</xdr:txBody></xdr:sp>' % (escape(name), hide, paras))


def anchor(content):
    """A two-cell anchor around a shape, group or chart frame."""
    return ('<xdr:twoCellAnchor><xdr:from><xdr:col>3</xdr:col><xdr:row>1</xdr:row></xdr:from><xdr:to>'
            '<xdr:col>8</xdr:col><xdr:row>6</xdr:row></xdr:to>%s<xdr:clientData/></xdr:twoCellAnchor>' % content)


CHART_FRAME = ('<xdr:graphicFrame><xdr:nvGraphicFramePr><xdr:cNvPr id="4" name="Chart 1"/><xdr:cNvGraphicFramePr/>'
               '</xdr:nvGraphicFramePr><xdr:xfrm/><a:graphic><a:graphicData uri="%s"><c:chart r:id="rId9"/>'
               '</a:graphicData></a:graphic></xdr:graphicFrame>' % C_NS)


def shared_strings_xml(strings):
    """sharedStrings.xml; a string given as a list is written as rich-text runs."""
    items = []
    for s in strings:
        if isinstance(s, (list, tuple)):
            items.append("<si>%s<rPh sb=\"0\" eb=\"1\"><t>PHONETIC</t></rPh></si>" % "".join(
                "<r><rPr><b/></rPr><t xml:space=\"preserve\">%s</t></r>" % escape(x) for x in s))
        else:
            items.append("<si><t xml:space=\"preserve\">%s</t></si>" % escape(s))
    return XML_HEAD + '<sst xmlns="%s" count="%d" uniqueCount="%d">%s</sst>' % (
        S_NS, len(strings), len(strings), "".join(items))


# cellXfs used by the tests: 0 general, 1 d/m/yyyy (14), 2 custom dd-mmm-yy, 3 time (20),
# 4 datetime (22), 5 percent (10), 6 custom "[$-409]d mmmm yyyy;@", 7 custom number 0.00
STYLES_XLSX = (XML_HEAD + '<styleSheet xmlns="%s"><numFmts count="3">'
               '<numFmt numFmtId="164" formatCode="dd\\-mmm\\-yy"/>'
               '<numFmt numFmtId="165" formatCode="[$-409]d mmmm yyyy;@"/>'
               '<numFmt numFmtId="166" formatCode="0.00"/></numFmts>'
               '<cellXfs count="8"><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="164"/>'
               '<xf numFmtId="20"/><xf numFmtId="22"/><xf numFmtId="10"/><xf numFmtId="165"/>'
               '<xf numFmtId="166"/></cellXfs></styleSheet>' % S_NS)


def xlsx(sheets, shared=None, date1904=False, title=None, styles=STYLES_XLSX, sheet_parts=None):
    """An .xlsx from sheets: [(name, sheet XML or None, state)]; state '' = visible.

    ``sheet_parts`` may give raw bytes for a sheet part (name -> bytes) instead.
    """
    sheet_els = []
    rels = []
    parts = {}
    for i, (name, xml, state) in enumerate(sheets, 1):
        st = ' state="%s"' % state if state else ""
        sheet_els.append('<sheet name="%s" sheetId="%d"%s r:id="rId%d"/>' % (escape(name), i, st, i))
        rels.append(("rId%d" % i, "worksheet", "worksheets/sheet%d.xml" % i))
        parts["xl/worksheets/sheet%d.xml" % i] = xml if xml is not None else sheet_xml([])
    if sheet_parts:
        parts.update(sheet_parts)
    pr = '<workbookPr date1904="1"/>' if date1904 else "<workbookPr/>"
    parts["xl/workbook.xml"] = (XML_HEAD + '<workbook xmlns="%s" xmlns:r="%s">%s<sheets>%s</sheets></workbook>'
                                % (S_NS, R_NS, pr, "".join(sheet_els)))
    if shared is not None:
        parts["xl/sharedStrings.xml"] = shared_strings_xml(shared)
        rels.append(("rIdS", "sharedStrings", "sharedStrings.xml"))
    if styles:
        parts["xl/styles.xml"] = styles
        rels.append(("rIdT", "styles", "styles.xml"))
    parts["xl/_rels/workbook.xml.rels"] = rels_xml(rels)
    return package("xl/workbook.xml",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                   parts, title=title)


# --------------------------------------------------------------------------
# PowerPoint
# --------------------------------------------------------------------------

def _shape(texts, ph_type=None):
    """A shape; each text is a paragraph (raw XML when it starts with '<a:p')."""
    ph = '<p:ph type="%s"/>' % ph_type if ph_type else ""
    paras = "".join(t if t.startswith("<a:p") else '<a:p><a:r><a:t>%s</a:t></a:r></a:p>' % escape(t)
                    for t in texts)
    return ('<p:sp><p:nvSpPr><p:cNvPr id="2" name="Shape"/><p:cNvSpPr/><p:nvPr>%s</p:nvPr></p:nvSpPr>'
            '<p:spPr/><p:txBody><a:bodyPr/>%s</p:txBody></p:sp>' % (ph, paras))


def _ppt_table(rows):
    trs = "".join("<a:tr>%s</a:tr>" % "".join(
        '<a:tc><a:txBody><a:bodyPr/><a:p><a:r><a:t>%s</a:t></a:r></a:p></a:txBody></a:tc>' % escape(c)
        for c in row) for row in rows)
    return ('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="9" name="Table"/><p:cNvGraphicFramePr/>'
            '<p:nvPr/></p:nvGraphicFramePr><a:graphic><a:graphicData><a:tbl>%s</a:tbl></a:graphicData>'
            '</a:graphic></p:graphicFrame>' % trs)


def autonum(text, scheme="arabicPeriod", start=None, level=0):
    """A raw a:p paragraph numbered automatically by PowerPoint (a:buAutoNum)."""
    at = ' startAt="%d"' % start if start else ""
    lvl = ' lvl="%d"' % level if level else ""
    return ('<a:p><a:pPr%s><a:buAutoNum type="%s"%s/></a:pPr><a:r><a:t>%s</a:t></a:r></a:p>'
            % (lvl, scheme, at, escape(text)))


def slide_xml(title=None, body=(), table_rows=None, footer=None, extra_shapes="", hidden=False):
    """A slide: other shapes are written first so the title is not first in tree order."""
    shapes = ""
    if body:
        shapes += _shape(body, "body")
    if table_rows:
        shapes += _ppt_table(table_rows)
    if footer:
        shapes += _shape([footer], "ftr")
    if title is not None:
        shapes += _shape([title], "title")
    shapes += extra_shapes
    show = ' show="0"' if hidden else ""
    return (XML_HEAD + '<p:sld xmlns:p="%s" xmlns:a="%s" xmlns:r="%s"%s><p:cSld><p:spTree>'
            '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
            '%s</p:spTree></p:cSld></p:sld>' % (P_NS, A_NS, R_NS, show, shapes))


def notes_slide_xml(text):
    return (XML_HEAD + '<p:notes xmlns:p="%s" xmlns:a="%s"><p:cSld><p:spTree>%s%s</p:spTree></p:cSld>'
            '</p:notes>' % (P_NS, A_NS, _shape([], "sldImg"), _shape([text], "body")))


def pptx(slides, title=None):
    """A .pptx from slides: dicts with title, body (list), table (rows), notes, footer, hidden.

    Slide parts are numbered in reverse so only presentation.xml's list gives the order.
    """
    parts = {}
    pres_rels = []
    ids = []
    n = len(slides)
    for i, s in enumerate(slides, 1):
        part_no = n - i + 1
        name = "slide%d.xml" % part_no
        parts["ppt/slides/" + name] = slide_xml(s.get("title"), s.get("body", ()), s.get("table"),
                                                s.get("footer"), hidden=s.get("hidden", False))
        srels = []
        if s.get("notes"):
            parts["ppt/notesSlides/notesSlide%d.xml" % part_no] = notes_slide_xml(s["notes"])
            srels.append(("rId2", "notesSlide", "../notesSlides/notesSlide%d.xml" % part_no))
        parts["ppt/slides/_rels/%s.rels" % name] = rels_xml(srels)
        pres_rels.append(("rId%d" % (i + 10), "slide", "slides/" + name))
        ids.append('<p:sldId id="%d" r:id="rId%d"/>' % (255 + i, i + 10))
    parts["ppt/presentation.xml"] = (XML_HEAD + '<p:presentation xmlns:p="%s" xmlns:r="%s">'
                                     '<p:sldIdLst>%s</p:sldIdLst></p:presentation>' % (P_NS, R_NS, "".join(ids)))
    parts["ppt/_rels/presentation.xml.rels"] = rels_xml(pres_rels)
    return package("ppt/presentation.xml",
                   "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
                   parts, title=title)
