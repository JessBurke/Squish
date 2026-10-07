"""Build the Squish documents digest from condensed documents (DocText dicts).

build_documents_digest(docs, project, source_label="", now=None, ...) is pure:
no file I/O, and the same input (and `now`) always gives the same output. See
DESIGN.md, "Documents digest".

For each document:
  1. tidy its blocks and drop boilerplate: page headers/footers repeated on many
     pages, page numbers, tables of contents, cover/copyright/limitation
     wording, repeated paragraphs and empty table cells
  2. cut the text into sentences and table rows, and score them for facts
     (cleaning.fact_score, plus document words: clauses, standards, shall/must,
     risks, recommendations, conclusions)
  3. keep the headings, the opening and the sentences/rows with the most facts
     that fit the squeeze level's cap, in document order, "\u2026" marking gaps
A later version of a file (the same name apart from revision marks and dates)
shows only what changed. Drawings and files that could not be read are listed
at the end. The result is split into parts that each stand alone.
"""

import difflib
import re
from collections import Counter, OrderedDict
from datetime import datetime

from . import cleaning
from . import digest
from .docs import table_row

# --------------------------------------------------------------------------
# Settings

# Caps per document by squeeze level: text characters, rows per sheet, PDF pages,
# characters of drawing notes (light only) and removed paragraphs listed in a
# version's changes.
DOC_CAPS = OrderedDict([
    ("light", {"chars": 30000, "rows": 400, "pages": 200, "notes": 600, "removed": 40}),
    ("standard", {"chars": 8000, "rows": 120, "pages": 60, "notes": 0, "removed": 20}),
    ("max", {"chars": 2500, "rows": 30, "pages": 15, "notes": 0, "removed": 8}),
])

HEADING_SHARE = 0.2      # headings kept up to this share of a document's cap
OPENING_SHARE = 0.25     # the opening (in order) gets this share of the cap
FOLD_HEADS = 3           # this many headings that differ only in their numbers show as one
SIMILAR = 0.6            # a later version this similar shows only its changes
FORMAT_SIMILAR = 0.75    # a PDF and Word file with one name this similar are one document
REPEAT_SHARE = 0.3       # a page header/footer on this share of pages is dropped
REMOVED_MAX = 120        # characters shown of a removed paragraph
SUBJECT_MAX = 70         # characters of an email subject on a From: line
TITLE_MAX = 100          # characters of a document title property
LIST_DIR_MAX = 12        # zip member names listed per folder before "+N more"
SHEETS_MAX = 20          # sheets of a drawing set listed on its Drawings line before "+N more"
OTHER_GROUP_MIN = 6      # a folder with more loose files that are not read than this gets one line
OTHER_NAMES_MAX = 3      # in that line, a type with up to this many files is named (more are counted)
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".bmp", ".tif", ".tiff", ".webp")

_HEAD_KINDS = ("head", "slide", "sheet")
_UNREAD = ("error", "too_big", "protected", "no_text")
_KIND_LABEL = {"docx": "Word", "xlsx": "Excel", "pptx": "PowerPoint", "pdf": "PDF", "text": "text",
               "zip": "zip", "other": "file"}
_STATUS_REASON = {"unsupported": "not read", "protected": "password-protected, not read",
                  "too_big": "too big to read", "no_text": "no text (scanned or image-only)",
                  "error": "could not be read"}


# --------------------------------------------------------------------------
# Small helpers

def _squash(text):
    """One line, single spaces, tidied like the email text (ASCII quotes and dashes)."""
    return re.sub(r"\s+", " ", cleaning.normalise_text(text or "")).strip()


def _cut(text, limit):
    """Text cut at a word to at most `limit` characters, with '\u2026' when cut."""
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    sp = cut.rfind(" ")
    if sp > limit * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,;:-") + "\u2026"


def _key(text):
    """Text as compared for repeats and versions: lower case, single spaces."""
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _iso_day(value):
    """'2025-03-04T09:00:00+10:00' -> '2025-03-04' ('' if not a date)."""
    value = (value or "").strip()
    return value[:10] if re.match(r"\d{4}-\d{2}-\d{2}", value) else ""


def _yymmdd(value):
    day = _iso_day(value)
    return day[2:] if day else ""


def _plural(n, word, plural=None):
    return "%d %s" % (n, word if n == 1 else (plural or word + "s"))


def _id_number(entry):
    m = re.match(r"D(\d+)$", entry["id"] or "")
    return int(m.group(1)) if m else 10 ** 9


def _size_text(size):
    """1234567 -> '1.2 MB'; '' if unknown."""
    if not isinstance(size, int) or size < 0:
        return ""
    if size >= 1024 * 1024:
        return "%.1f MB" % (size / 1048576.0)
    if size >= 1024:
        return "%d KB" % round(size / 1024.0)
    return "%d bytes" % size


# --------------------------------------------------------------------------
# Fact scoring (cleaning.fact_score plus words that matter in reports)

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
# Figures that count as hard facts in documents, besides cleaning's (amounts, refs,
# dates, quantities with units)
_DOC_HARD = [
    # dates with a year (cleaning already scores day + month)
    (re.compile(r"\b(?:19|20)\d\d-\d\d-\d\d\b|\b\d{1,2}[./]\d{1,2}[./](?:19|20)?\d\d\b"
                r"|\b" + _MONTH + r"\.?,?\s+(?:19|20)\d\d\b", re.I), 1),
    # standards and codes
    (re.compile(r"\b(?:AS/NZS|AS|NZS|ISO|EN|BS|ASTM|AASHTO|AUSTROADS|MRTS|TfNSW|QA)\s?[A-Z]?\d{2,5}\b"), 2),
    # units cleaning does not know: 85 \u00b5m, 200 GPa, 12 kN/m, 18 kN/m3, 35 mg/L, 400 mg/kg, 70 dB,
    # 40 \u00b0C, 15 kW, 2,400 ohm.cm, 120 \u00b5S/cm; and pH values (pH 5.4, pH of 6.8)
    (re.compile(r"\b\d[\d,.]*\s?(?:\u00b5m|\u03bcm|um|microns?|GPa|kN/m[23]?|kNm|kN\.m|mg/(?:[lL]|kg)|ppm|dB|"
                r"\u00b0C|kW|kVA|MW|ohm\.?\s?cm|\u00b5S/cm|uS/cm)(?![\w])|\bpH\s?(?:of\s|=\s?)?\d"), 1),
    # structural member, bar, mesh, bolt and material designations: 360UB56.7, 200PFC,
    # 150x150x9.0 SHS, N16-200, 2-N20, SL92, M24, 8.8/S, 300PLUS, C350L0, Z200-19
    (re.compile(r"\b\d{2,4}\s?(?:UB|UC|PFC|WB|WC|TFB|UBP|EA|UA)(?:\s?\d{1,3}(?:\.\d)?)?\b"
                r"|\b\d{2,3}(?:\.\d)?\s?[xX\u00d7]\s?\d{2,3}(?:\.\d)?(?:\s?[xX\u00d7]\s?\d{1,2}(?:\.\d)?)?"
                r"\s?(?:SHS|RHS|CHS|EA|UA)\b"
                r"|\b(?:\d{1,2}\s?-\s?)?N[1-3]\d(?:\s?[-@]\s?\d{2,3})?\b|\bN40\b"
                r"|\b(?:SL|RL)\d{2,4}\b|\bM[1-3]\d\b|\b\d\.\d/[ST][BF]?(?![\w/])"
                r"|\b\d{3}PLUS\b|\bC\d{3}L0\b|\b[ZC]\d{3}-?\d{2}\b"), 2),
    # sizes without units: 2100 x 2100 x 700, 150 x 50 (2+ digits each side, so not '2 x 92 kN')
    (re.compile(r"\b\d{2,5}\s?[xX\u00d7]\s?\d{2,5}(?:\s?[xX\u00d7]\s?\d{2,5})?\b"), 1),
    # limits and slopes written as ratios: span/250, height/150, 1V:1H, 1 in 100
    (re.compile(r"\b(?:span|height|length|[LHlh])\s?/\s?\d{2,4}\b"
                r"|\b\d+(?:\.\d+)?\s?[VH]\s?:\s?\d+(?:\.\d+)?\s?[VH]\b|\b1\s?in\s?\d{1,4}\b"), 2),
]
# Words that make a sentence worth more in a report
_DOC_SOFT = [
    # clause and section references (not the document's own tables and figures)
    (re.compile(r"\b(?:clause|cl|section|sect|condition|specification|spec)\.?\s?[A-Z]?\d{1,3}"
                r"(?:\.\d{1,3})*\b", re.I), 1),
    # design values
    (re.compile(r"\bdesign(?:ed)? (?:for|life|loads?|speed|traffic|criteria|pressure|capacity|strength|cbr|"
                r"flows?|storm|level|wind)\b", re.I), 1),
    # requirements and limits
    (re.compile(r"\b(?:shall|must|required?|requirements?|is to be|are to be|minimum|maximum|not less than|"
                r"not more than|no more than|at least|not (?:to )?exceed\w*|no steeper|limited to|allowable)\b",
                re.I), 1),
    # risks, findings, recommendations and conclusions
    (re.compile(r"\b(?:recommend\w*|conclu\w*|risks?|hazards?|unsuitable|not suitable|critical|"
                r"non-?complian\w*|fail(?:ure|ed|s)?|defects?|crack(?:s|ing|ed)?|settlement|corrosion|"
                r"deteriorat\w*|exceed\w*|unacceptable|inadequate|insufficient|urgent\w*|should|"
                r"contaminat\w*|asbestos|groundwater)\b", re.I), 1),
]
HARD_BONUS = 3           # a sentence with a figure, date or reference comes before one without
SECTION_BONUS = 3        # added under summary/recommendation headings, taken off in appendices
ROW_SCORE_MAX = 7        # most a table row in a report (not a spreadsheet) can score
OPEN_RARE = 4.0          # added to a register row's "rare" for an open item among closed ones
# A cell saying an item is still open (matched against _key(cell), lower case). Not 'TBC':
# a 'Cost / time impact' column full of TBC would crowd the open rows out.
_OPEN_STATUS = re.compile(r"^(?:open|pending|outstanding|overdue|in progress|on hold|not started)$")
# Sections whose text is worth more: summaries, findings, conclusions, recommendations
_KEY_SECTION = re.compile(r"(?i)\b(?:summary|conclusions?|recommendations?|key (?:findings|issues|points|risks)|"
                          r"findings|outcomes?|actions?|decisions?|scope of works?|design criteria|"
                          r"assumptions)\b")
_TOTAL_ROW = re.compile(r"(?i)\b(?:sub-?)?totals?\b|\bgrand total\b")
_NUMBER_CELL = re.compile(r"^[-+(]?[$\u00a3\u20ac]?\s?\d[\d,]*(?:\.\d+)?\)?\s?%?[a-zA-Z]{0,3}$")


def doc_fact_score(text):
    """How much a sentence or row of a document is worth keeping (0 = nothing in
    particular): cleaning.fact_score, plus figures and words that matter in
    reports, plus HARD_BONUS when it holds a figure, date or reference."""
    score = cleaning.fact_score(text)
    hard = _hard_facts(text)
    for rx, weight in _DOC_HARD:
        found = len(rx.findall(text))
        score += weight * min(2, found)
        hard = hard or found > 0
    for rx, weight in _DOC_SOFT:
        score += weight * min(2, len(rx.findall(text)))
    return score + (HARD_BONUS if hard else 0)


def _row_score(text, rare):
    """A spreadsheet row's worth: how unusual its cells are in their columns (`rare`,
    see _mark_rarity: an 'Open' among many 'Closed', a response written out instead
    of 'As per drawing'), then its figures (totals are kept anyway). A sparse row's
    'header: value' cell (docs.py) counts by its value."""
    filled = [c for c in _cells(text) if c]
    numbers = sum(1 for c in filled if _NUMBER_CELL.match(c.rsplit(": ", 1)[-1]))
    score = 2 * rare + 0.5 * numbers + (1 if "$" in text else 0)
    if _TOTAL_ROW.search(text):
        score += 20
    return score


# --------------------------------------------------------------------------
# Boilerplate

_PAGE_NUMBER = re.compile(
    r"(?i)^(?:-\s*)?(?:page\s*|p\.\s*)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?(?:\s*-)?$|^page\s+\d{1,4}\b.{0,80}$"
    r"|^.{0,80}\bpage\s+\d{1,4}\s*(?:of|/)\s*\d{1,4}$|^[ivxlc]{1,4}$")
_PAGE_WORDS = re.compile(r"(?i)\b(?:page|sheet)\s+\d{1,4}\s*(?:of|/)\s*\d{1,4}\b")
# A quantity with a unit ('28.3 MPa', 'L=5.5 m', '17.9 m3'): record data, never a page header
_QUANTITY = re.compile(r"\d\s?(?:mm|m|m2|m3|kN|kNm|kPa|MPa|t|kg|%)(?!\w)")
# A digit run, and one standing on its own ('Page 3', '| 3'; not 'BH3', 'R01', 'BL-T-001', '3.2')
_DIGITS = re.compile(r"\d+")
_LONE = re.compile(r"(?<![\w./-])\d{1,4}(?![\w/-]|\.\d)")
_TOC_DOTS = re.compile(r"(?:\.\s?){4,}\s*\(?[0-9ivxlc]{1,5}\)?\s*$|\u2026{2,}\s*\d{1,4}\s*$|_{4,}\s*\d{1,4}\s*$",
                       re.I)
# A contents heading. (Not 'List of drawings' / 'List of attachments': in a report or a
# transmittal that is a real schedule, its rows ending in revisions, not page numbers.)
_TOC_HEAD = re.compile(r"(?i)^(?:table of )?contents?$|^list of (?:figures|tables|appendices|plates|photographs)$")
# A dot-leader line's last token (a page number), and the leader itself
_TOC_PAGE = re.compile(r"(?:(?:\.\s?){4,}|\u2026{2,}|_{4,})\s*\(?([0-9ivxlc]{1,5})\)?\s*$", re.I)
_LEADER = re.compile(r"(?:\.\s?){4,}|\u2026{2,}|_{4,}")
_LOWER_ROMAN = re.compile(r"^x{0,3}(?:ix|iv|v?i{0,3})$")      # front matter pages (case matters: not 'C')
TOC_FIRST_PAGE = 10      # a dot-leader contents list starts by this page ('f'c .... 32' / 'cover .... 40' does not)
_TOC_LINE = re.compile(
    r"(?i)^(?:(?:section\s+)?\d{1,2}(?:\.\d{1,2}){0,4}\.?|appendix\s+[a-z0-9]{1,3}\b[:.\-]?|"
    r"annex(?:ure)?\s+[a-z0-9]{1,3}\b[:.\-]?|[a-z]\.\d{1,2}(?:\.\d{1,2})*|"
    r"(?:table|figure|plate|drawing)\s+[a-z]?\d{1,3}(?:\.\d{1,2})?[:.\-]?)?\s*\S.{0,110}?\s(\d{1,4})$")
# A numbered contents line: '3.2 Wind 7', 'Appendix A - Borehole logs 21' (a section number, a
# title starting with a letter (group 1), the page number (group 2))
_TOC_ENTRY = re.compile(
    r"(?i)^(?:(?:section\s+)?\d{1,2}(?:\.\d{1,2}){0,4}\.?|appendix\s+[a-z0-9]{1,3}\b[:.\-]?|"
    r"annex(?:ure)?\s+[a-z0-9]{1,3}\b[:.\-]?|[a-z]\.\d{1,2}(?:\.\d{1,2})*|"
    r"(?:table|figure|plate|drawing)\s+[a-z]?\d{1,3}(?:\.\d{1,2})?[:.\-]?)(?:\s+[-:\u2013])?\s+"
    r"([a-z(\"'].{0,110}?)\s(\d{1,4})$")
# A figure of its own in a title ('100 - 200', 'L=8187', '460UB'), not part of a code like 'BH14'
_LOOSE_FIGURE = re.compile(r"(?<![A-Za-z0-9])\d")
# Wording of cover pages, copyright notices and limitation-of-liability sections.
# A strong phrase is enough on a cover page; elsewhere it takes two phrases (or three weak ones).
_BOILER_STRONG = [re.compile(p, re.I) for p in (
    r"\u00a9|\(c\)\s*(?:copyright\s*)?(?:19|20)\d\d|\(c\)\s+[\w&.,' -]{2,60}?\b(?-i:Pty|Ltd|Limited|Inc|LLC|PLC|GmbH)\b|\bcopyright\b",
    r"\ball rights reserved\b",
    r"\b(?:exclusive|sole) (?:use|benefit|reliance|purpose)\b|\bsolely for the (?:use|purpose|benefit)\b",
    r"\b(?:accepts?|assumes?|takes?|bears?|has|have) no (?:liability|responsibility|duty)\b"
    r"|\bno (?:liability|responsibility|duty of care)\b|\bliab(?:le|ility) (?:for|to) any\b",
    r"\blimitations? of liability\b|\bindemnif\w*|\bdisclaim\w*",
    r"\b(?:must|may|shall) not be (?:reproduced|copied|relied)\b|\breproduc\w* in (?:whole|part|full)\b",
    r"\bremains? the property of\b|\bunauthori[sz]ed (?:use|copying|reproduction)\b"
    r"|\b(?:may|shall|must|is to|are to) only be used for the purposes?\b",
    r"\btotal liability\b|\bconsequential loss\b|\blimited to the (?:amount of the )?fees?\b",
    r"\baccredited for compliance with\b|\bresults relate only to\b",
    r"\bmatter of information only\b|\bconfers no rights\b|\bdoes not amend, extend or alter\b"
    r"|\bstandard terms and conditions\b",
)]
# 'without the consent of ...' is a disclaimer only when it is about using, copying or
# relying on the document; 'Props shall not be removed without the approval of the
# engineer' is a site instruction (a hold point), and is kept.
_WITHOUT_CONSENT = re.compile(r"(?i)\bwithout (?:the )?(?:prior )?(?:express )?(?:written )?"
                              r"(?:consent|permission|approval) of\b")
_DOC_USE = re.compile(r"(?i)\bthis (?:report|document|drawing|letter|certificate|proposal|publication|information)\b"
                      r"|\b(?:reproduc\w*|cop(?:y|ied|ying)|relie[ds]|rel(?:y|iance)|disclos\w*|distribut\w*|"
                      r"transmit\w*|publish\w*|herein)\b|\bother (?:party|parties|purposes?)\b|\bthird part(?:y|ies)\b")
_BOILER_WEAK = [re.compile(p, re.I) for p in (
    r"\bthird part(?:y|ies)\b",
    r"\bthis (?:report|document|proposal|letter) (?:is|was|has been) (?:prepared|produced|issued|provided)"
    r"\b.{0,60}\b(?:for|by|in accordance|on behalf|subject to)\b",
    r"\b(?:professional|engineering) judg(?:e)?ment\b|\bscope of (?:services|engagement|the engagement)\b",
    r"\bin accordance with (?:the|our) (?:agreed )?(?:scope|terms|agreement|proposal|contract|engagement)\b",
    r"\b(?:rel(?:y|ies|ied|iance) (?:up)?on)\b.{0,60}\b(?:report|document|information)\b",
    r"\bprivileged\b|\bconfidential\b|\bin confidence\b",
    r"\bterms of (?:engagement|appointment)\b",
)]
_STREET = (r"(?:st|street|rd|road|ave?|avenue|pde|parade|hwy|highway|dr|drive|pl|place|tce|terrace|"
           r"la|lane|ln|bvd|blvd|boulevard|way|cres|crescent|ct|court|cct|circuit|sq|square|esp|esplanade|"
           r"cl|close)")
# Contact details: ABN/ACN, web and email addresses, PO boxes, a phone label with a
# phone number (8+ digits, so 'p. 12' and 'm: 2' are not), and a floor in a street
# address ('Level 3, 100 Smith Street'; not 'cracking at level 2, near grid C4').
_CONTACT = re.compile(r"(?i)\babn:?\s*\d|\bacn:?\s*\d|\bwww\.|\b[\w.-]+@[\w-]+\.[\w.]+|"
                      r"(?:^|\s)(?:t|p|ph|phone|tel|f|fax|m|mob|mobile)\s*[:.]\s*\+?\(?\d(?:[\s().-]*\d){7}|"
                      r"\bp\.?o\.? box\b|"
                      r"\blevel \d{1,3},\s*(?:suite \d+,?\s*)?\d{1,5}[a-z]?(?:-\d{1,5})?\s+(?:[a-z'-]+\s+){1,3}?"
                      + _STREET + r"\b")
_BOILER_HEAD = re.compile(
    r"(?i)^(?:\d{1,2}(?:\.\d{1,2})*\.?\s+|appendix\s+\w{1,3}\s*[:.\-]?\s*)?"
    r"(?:(?:statement of |report |general )?limitations?(?: of (?:this report|use))?|"
    r"disclaimers?|copyright(?: notice)?|reliance|basis of (?:this )?report|confidentiality|terms of use)\s*:?$"
    r"|^(?:appendix\s+\w{1,3}\s*[:.\-]?\s*)?important (?:information|notice)\b")


# A clause of a contract, fee proposal or specification: a named party's duty or liability
# ('The Subcontractor shall indemnify the Contractor against claims by third parties ...').
# Kept even with liability or approval wording, unless it is about this report / document
# (a disclaimer) or under a Limitations / Disclaimer heading. (No re.I: defined terms only.)
_PARTY = re.compile(r"\b(?:Sub-?contractor|Contractor|Sub-?consultant|Consultant|Client|Principal|"
                    r"Superintendent|Engineer|Owner|Purchaser|Supplier|Vendor|Employer|Proprietor)s?\b")
_DUTY = re.compile(r"(?i)\b(?:shall|must|agrees? to|(?:is|are) liable|liability (?:\w+ ){0,6}?"
                   r"(?:is|are|will be|shall be) limited)\b")
_ABOUT_DOC = re.compile(r"(?i)\bthis (?:report|document|drawing|letter|certificate|proposal)\b")


def _hard_facts(text):
    """True when text has a figure, date, reference or amount (cleaning's hard facts)."""
    return any(rx.search(text) for rx, _w in cleaning._FACTS)


def _clause(text):
    """True for a contract or specification clause (see _PARTY)."""
    return bool(_PARTY.search(text) and _DUTY.search(text) and not _ABOUT_DOC.search(text))


def _boilerplate(text, in_boiler_section, near_cover):
    """True for cover, copyright, limitation-of-liability and contact wording (a
    contract or specification clause is not: see _clause)."""
    hard = _hard_facts(text)
    if in_boiler_section and not hard:
        return True
    if _clause(text):
        return False
    strong = sum(1 for rx in _BOILER_STRONG if rx.search(text))
    if _WITHOUT_CONSENT.search(text) and _DOC_USE.search(text):
        strong += 1       # ('must not be copied without the written consent of ...')
    weak = sum(1 for rx in _BOILER_WEAK if rx.search(text))
    if strong + weak >= 3 or (strong and strong + weak >= 2 and not hard) or \
            (strong and near_cover and not hard and len(text) < 600):
        return True
    contact = len(_CONTACT.findall(text))
    return (contact >= 2 and len(text) < 300) or (contact and len(text) < 120 and not hard)


# --------------------------------------------------------------------------
# A document's text as paragraphs

def _paragraphs(doc):
    """The whole document as a list of paragraphs, boilerplate removed. Each is
    a dict: kind ("head", "slide", "sheet", "text", "item", "row", or "meta" for
    a Word header/footer line), text, level, page (PDFs), sec (index of the
    heading it is under), and for rows group (the sheet or table) and header
    (True for a header row); a sheet also has rows (all its rows) and hidden."""
    kind = doc.get("kind") or ""
    paras = []
    page = 0
    group = 0
    last_kind = ""
    in_sheet = False
    for block in doc.get("blocks") or []:
        btype = block.get("type")
        if btype == "member":
            continue
        if btype == "page":
            page = block.get("level") or (page + 1)
            continue
        text = _squash(block.get("text"))
        if kind == "pdf":
            text = _WRAPPED_CODE.sub("", text)
        level = block.get("level") or 0
        if btype == "sheet":
            group += 1
            in_sheet = True
            paras.append({"kind": "sheet", "text": text or "Sheet %d" % level, "level": level,
                          "rows": block.get("rows"), "hidden": bool(block.get("hidden")), "group": group})
        elif btype == "slide":
            paras.append({"kind": "slide", "text": "Slide %d" % level + (": " + text if text else ""),
                          "level": 1, "title": text})
        elif btype == "row":
            if not in_sheet and (last_kind != "row" or not level):
                group += 1      # a new Word/PDF/slide table
            row = _tidy_row(text, in_sheet)
            if row:
                paras.append({"kind": "row", "text": row, "level": level, "group": group, "page": page})
        elif text:
            in_sheet = False
            if btype == "heading":
                paras.append({"kind": "head", "text": text, "level": max(1, min(9, level or 1)), "page": page})
            elif btype == "item":
                paras.append({"kind": "item", "text": text, "level": level, "page": page})
            elif kind == "docx" and _META.match(text):
                text = _EMPTY_WATERMARK.sub(" ", _CLASSIFICATION.sub("", text)).strip(" |")
                if len(text) > 8:
                    paras.append({"kind": "meta", "text": _cut(text, 300), "level": 0, "page": page})
            else:
                paras.append({"kind": "text", "text": text, "level": 0, "page": page})
        last_kind = btype
    if kind == "pdf":
        paras = _drop_page_furniture(paras)
    paras = _drop_contents(paras, kind, doc.get("pages"))
    paras = _drop_boilerplate(paras)
    paras = _drop_repeats(paras)
    if kind == "pdf":
        paras = _join_pdf_lines(paras)
    paras = _drop_continued(paras)
    _mark_headers(paras)
    _mark_rarity(paras)
    _number_sections(paras)
    return paras


def _page_capped(paras, doc, caps):
    """A PDF's paragraphs up to the level's page cap, then a note of the pages
    left out. A paragraph joined across pages counts by the last page it
    reaches, so no text from a page left out is shown."""
    limit = caps["pages"]
    if doc.get("kind") != "pdf" or not isinstance(doc.get("pages"), int) or doc["pages"] <= limit:
        return paras
    end = next((n for n, p in enumerate(paras) if (p.get("last_page") or p.get("page") or 0) > limit),
               len(paras))
    return paras[:end] + [{"kind": "note", "text": "(pages %d-%d not shown)" % (limit + 1, doc["pages"]),
                           "level": 0, "sec": -1, "key": 0}]


# A heading carried onto a new page: 'C-01 DESIGN BASIS (CONT.)', 'Table 3 (continued)'
_CONTINUED = re.compile(r"(?i)(?:\s*[(\[]\s*|\s+[-\u2013]?\s*)(?:cont(?:inued|'d|\u2019d|d)?|continuation)\.?"
                        r"\s*[)\]]?\s*$")


def _drop_continued(paras):
    """Drop a heading that only repeats the heading before it with '(cont.)' or
    '(continued)' after it: the text under it belongs to the first one."""
    out = []
    last = None
    for p in paras:
        if p["kind"] == "head":
            base = _CONTINUED.sub("", p["text"])
            if base != p["text"] and last is not None and _key(base) == _key(last):
                continue
            last = p["text"]
        out.append(p)
    return out


# Word's page header / footer lines (docs.py adds them once, after the body)
_META = re.compile(r"^(?:Header|Footer): ")
# Classification labels taken out of those lines
_CLASSIFICATION = re.compile(r"(?i)\s*\|?\s*\b(?:commercial[- ]in[- ]confidence|strictly confidential|"
                             r"privileged and confidential|confidential|in confidence|page \d+(?: of \d+)?)\b")
# A watermark label left empty once its classification was taken out
_EMPTY_WATERMARK = re.compile(r"\s*\bWatermark:\s*(?:\|\s*|$)")
# 'ST- 1200': a drawing or document number split where a PDF line wrapped
_WRAPPED_CODE = re.compile(r"(?<=[A-Z0-9]-) (?=[A-Z0-9])")


def _cells(text):
    """'Total | | 45' -> ['Total', '', '45']."""
    return [c.strip() for c in re.split(r" ?\| ?", text)]


def _join_cells(cells):
    """['Total', '', '45'] -> 'Total | | 45' (as docs.py writes rows)."""
    out = cells[0] if cells else ""
    for c in cells[1:]:
        out += " |" + (" " + c if c else "")
    return out.lstrip()


def _tidy_row(text, sheet):
    """A table row without empty cells (a sheet row keeps the empty cells between
    filled ones so its columns line up). '' for an empty row."""
    cells = _cells(text)
    if not any(cells):
        return ""
    if sheet:
        while cells and not cells[-1]:
            cells.pop()
        return _join_cells(cells)
    return _join_cells([c for c in cells if c])


def _edge_shape(text):
    """(shape, numbers, lone) of a line at the top or bottom of a page. The shape is
    (its text with page numbers ('Page 3 of 12') taken out and each other digit run
    as '#', how many digit runs it had); then those digit runs, and for each
    whether it stands on its own ('| 3', not 'BH3')."""
    key = _key(_PAGE_WORDS.sub("#", text))
    lone = set(m.start() for m in _LONE.finditer(key))
    runs = list(_DIGITS.finditer(key))
    # The shape keeps how many digit runs the line had, so lines whose '#'s came
    # from different things ('Calcs 1' / 'Calcs Page 2 of 3', 'Lot # 12' / 'Lot 5 12')
    # are never grouped together and every copy has the same numbers to compare.
    return (_DIGITS.sub("#", key), len(runs)), [int(m.group()) for m in runs], [m.start() in lone for m in runs]


def _only_page_numbers(copies):
    """True when the copies of one line shape (a list of (page, numbers, lone)) differ
    only in a page number: each number is the same on every copy, or stands on its
    own and goes up with the page (number - page stays the same)."""
    for j in range(len(copies[0][1])):
        if len(set(nums[j] for _pg, nums, _l in copies)) == 1:
            continue
        if not all(lone[j] for _pg, _n, lone in copies):
            return False
        if len(set(nums[j] - pg for pg, nums, _l in copies)) != 1:
            return False
    return True


def _some_number_fixed(copies):
    """True when one of the numbers in a line shape is the same on every copy (a
    job number or a date in a running header, not a borehole or lot number)."""
    return any(len(set(nums[j] for _pg, nums, _l in copies)) == 1 for j in range(len(copies[0][1])))


def _is_comment(text):
    """True for a PDF review comment paragraph ('[comment: ...]', see docs.py),
    which comes after its page's own lines."""
    return text.startswith("[comment: ")


def _drop_page_furniture(paras):
    """Drop PDF page headers/footers and page numbers. A header/footer is one of
    the first or last three lines of a page that comes back on REPEAT_SHARE of
    the pages or more, word for word or with only its page number changed. A
    page's very first or last line, or a line holding 'Page 3 of 12', is also a
    header/footer when other figures change too, if some figure stays the same
    (a running header naming the calc: 'Job 24117 | Calc C-03 | 02/05/25') and
    it has no quantity with a unit. Other lines whose figures change from page to
    page (borehole, lot, certificate numbers, test results) are page content.
    Review comments at the end of a page are not counted as its last lines."""
    by_page = OrderedDict()
    for n, p in enumerate(paras):
        if p["kind"] in ("text", "head", "item") and not _is_comment(p["text"]):
            by_page.setdefault(p.get("page") or 0, []).append(n)
    edge, outer = set(), set()
    copies = {}       # shape -> {page: (page, numbers, lone)}, one copy per page
    for page, idx in by_page.items():
        edge.update(idx[:3] + idx[-3:])
        outer.update(idx[:1] + idx[-1:])
        for n in idx[:3] + idx[-3:]:
            if len(paras[n]["text"]) < 200:
                shape, nums, lone = _edge_shape(paras[n]["text"])
                copies.setdefault(shape, OrderedDict()).setdefault(page, (page, nums, lone))
    pages = len(by_page)
    need = max(2, int(REPEAT_SHARE * pages + 0.999))
    often = dict((shape, list(seen.values())) for shape, seen in copies.items() if len(seen) >= need)
    fixed = set(shape for shape, seen in often.items() if _only_page_numbers(seen))
    running = set(shape for shape, seen in often.items() if _some_number_fixed(seen))
    out = []
    for n, p in enumerate(paras):
        text = p["text"]
        if p["kind"] != "row" and (_PAGE_WORDS.fullmatch(text) or (n in edge and _PAGE_NUMBER.match(text))):
            continue
        if n in edge and pages >= 2 and len(text) < 200:
            shape = _edge_shape(text)[0]
            page_words = bool(_PAGE_WORDS.search(text))
            if shape in fixed or (shape in often and (n in outer or page_words) and
                                  (page_words or shape in running) and not _QUANTITY.search(text)):
                if text[:1].islower() and out and out[-1].get("page") == p.get("page"):
                    out[-1]["ends_sentence"] = True     # ('... except in' / 'full.' on every page)
                continue
        out.append(p)
    return out


def _page_no(text, max_page, dots_only):
    """The page number at the end of a contents line, or None: digits up to
    `max_page`, or 0 for a lower-case roman numeral (front matter comes first).
    The number follows a dot leader, or (unless dots_only) ends a numbered line
    ('3.2 Wind 7'). 'Site class .......... C' and '... Civil' end in no page number."""
    m = _TOC_PAGE.search(text)
    if m:
        token = m.group(1)
    elif dots_only:
        return None
    else:
        m = _TOC_LINE.match(text)
        if not m:
            return None
        token = m.group(1)
    if token.isdigit():
        return int(token) if int(token) <= max_page else None
    return 0 if _LOWER_ROMAN.match(token) else None


def _contents_run(paras, n, max_page, dots_only):
    """The index just past the run of contents lines starting at paras[n]: short
    lines (dot leaders not counted) ending in page numbers that never fall."""
    end, last = n, -1
    while end < len(paras) and paras[end]["kind"] in ("text", "head", "item") and \
            len(_LEADER.sub(" ", paras[end]["text"])) < 140:
        page_no = _page_no(paras[end]["text"], max_page, dots_only)
        if page_no is None or page_no < last:
            break
        last = page_no
        end += 1
    return end


def _drop_contents(paras, kind, pages):
    """Drop tables of contents: runs of 3+ dot-leader lines whose page numbers
    start by TOC_FIRST_PAGE and never fall; a 'Contents' / 'List of figures
    (tables, appendices ...)' heading and the lines after it while their page
    numbers never fall; and runs of 3+ numbered lines (a section number and a
    title with no other figures) ending in rising page numbers near the start of
    the document. A lone dot-leader line ('Minimum cover (mm) .......... 40') is a
    value, and table rows such as '400-500 5 6 6' or 'MARK C1-02 460UB74.6 ...
    QTY 1' are not contents lines."""
    out = []
    n = 0
    early = max(40, len(paras) // 5)
    max_page = (pages or 0) + 20 if pages else 999
    while n < len(paras):
        p = paras[n]
        if p["kind"] in ("text", "head", "item"):
            text = p["text"]
            if _TOC_DOTS.search(text):
                end = _contents_run(paras, n, max_page, True)
                if end - n >= 3 and _page_no(text, max_page, True) <= TOC_FIRST_PAGE:
                    n = end
                    continue
            if _TOC_HEAD.match(text.rstrip(" :")):
                n = _contents_run(paras, n + 1, max_page, False)
                continue
            if n < early and kind in ("pdf", "text"):
                end = n
                last = -1
                while end < len(paras) and paras[end]["kind"] in ("text", "head", "item") and \
                        len(paras[end]["text"]) < 140:
                    m = _TOC_ENTRY.match(paras[end]["text"])
                    if not m or _LOOSE_FIGURE.search(m.group(1)) or int(m.group(2)) < last or \
                            int(m.group(2)) > max_page:
                        break
                    last = int(m.group(2))
                    end += 1
                if end - n >= 3:
                    n = end
                    continue
        out.append(p)
        n += 1
    return out


def _drop_boilerplate(paras):
    """Drop cover, copyright, limitation and contact wording (see _boilerplate);
    in a 'Limitations' / 'Disclaimer' section, every paragraph without a figure.
    A boilerplate heading whose section is all dropped goes too."""
    out = []
    in_section = False
    head_at = None        # where the boilerplate section's heading is in out
    kept_in_section = 0
    total = len(paras)
    tail_of_dropped = False   # the paragraph just dropped stopped mid-sentence
    for n, p in enumerate(paras):
        if tail_of_dropped and p["kind"] == "text" and p["text"][:1].islower() and len(p["text"]) < 120:
            continue      # the end of that sentence, on a line of its own ('connection with this report.')
        tail_of_dropped = False
        if p["kind"] in _HEAD_KINDS:
            if head_at is not None and not kept_in_section:
                del out[head_at]
            in_section = p["kind"] == "head" and bool(_BOILER_HEAD.match(p["text"]))
            head_at = len(out) if in_section else None
            kept_in_section = 0
            out.append(p)
            continue
        if p["kind"] == "meta":
            if not _boilerplate(p["text"][8:], False, True):
                out.append(p)
            continue
        if p["kind"] in ("text", "item"):
            page = p.get("page") or 0
            near_cover = (0 < page <= 2) or n < 12 or n >= total - 6
            if _boilerplate(p["text"], in_section, near_cover):
                tail_of_dropped = not _SENTENCE_END.search(p["text"])
                continue
            if in_section:
                # under a Limitations heading only the sentences with figures stay
                p = dict(p, text=" ".join(x for x in _split_sentences(p["text"]) if _hard_facts(x)))
                if not p["text"]:
                    continue
        kept_in_section += 1
        out.append(p)
    if head_at is not None and not kept_in_section:
        del out[head_at]
    return out


def _drop_repeats(paras):
    """Drop a paragraph or row of 30+ characters seen word for word earlier in the
    document (rows only within their own table or sheet). The copy kept gets
    "repeated": True."""
    first = {}
    out = []
    for p in paras:
        if p["kind"] in ("text", "item", "row") and len(p["text"]) >= 30:
            k = (p["kind"] == "row" and p.get("group"), _key(p["text"]))
            if k in first:
                first[k]["repeated"] = True
                continue
            first[k] = p
        out.append(p)
    return out


_SENTENCE_END = re.compile(r"[.!?:;][\"')\]]*$")
_AMOUNT_END = re.compile(r"[$\u00a3\u20ac]\s?\d[\d,]*(?:\.\d{1,2})?\s*$")   # a priced row ends here
# A word a sentence cannot stop on, at the end of a page ('... approved by')
_OPEN_END = re.compile(r"(?:,|\b(?:a|an|the|of|to|in|on|at|by|for|from|with|and|or|as|is|are|was|were|be|"
                       r"been|shall|will|should|must|may|per|than|that|which|into|onto|under|over|between)|"
                       r"[-/])$")
_MONTH_START = re.compile(r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b")


def _continues(prev_text, text):
    """True if `text`, the first line on a new PDF page, carries on the sentence the
    last page stopped in: it starts in lower case, the page stopped on a word a
    sentence cannot end on, or a date was split ('on 12' / 'March')."""
    if text[:1].islower() or _OPEN_END.search(prev_text):
        return True
    return bool(re.search(r"\b\d{1,2}$", prev_text) and _MONTH_START.match(text))


# A line starting with a drawing or document number ('623.0001-ST-1202 Footing plan 4')
_RECORD_START = re.compile(r"^(?=\S*\d{3})[A-Z0-9.]+-[A-Z0-9.\-]+\s")


def _whole_row(text):
    """True for a PDF line that is a complete row in itself: a table row
    (docs.table_row: 'RD-ST-1202 FOOTING PLAN C A1') or a 'label .... value' line
    ('Minimum cover (mm) .......... 40')."""
    return bool(table_row(text) or _LEADER.search(text))


def _two_records(text, next_text):
    """True when two PDF lines both start with a drawing or document number: two
    rows of a list of drawings ('623.0001-ST-1001 Cover sheet 2' / '623.0001-ST-1002 ...')."""
    return bool(_RECORD_START.match(text) and _RECORD_START.match(next_text))


def _join_pdf_lines(paras):
    """Put PDF paragraphs back together where a page break or a ragged line split
    them (a long line that does not end a sentence runs on into the next
    paragraph), and gather runs of short table-cell lines into one paragraph.
    Across a page break a line is joined only when it carries on the sentence
    (see _continues), so one page's records do not run into the next page's; a
    joined paragraph remembers the last page it reaches ("last_page"). A line
    whose sentence ended in a page footer that was dropped ("ends_sentence", see
    _drop_page_furniture) does not run on, nor does a complete row (see
    _whole_row, _two_records) into a line starting with a capital or a digit
    (the next row); and a complete row is never gathered as a cell. A review
    comment ('[comment: ...]') is never joined to anything."""
    out = []
    for p in paras:
        prev = out[-1] if out else None
        if prev is not None and prev["kind"] in ("text", "item") and p["kind"] == "text" and \
                not _is_comment(prev["text"]) and not _is_comment(p["text"]):
            ptext = prev["text"]
            page = p.get("page") or 0
            new_page = page > prev.get("last_page", prev.get("page") or 0)
            if not prev.get("ends_sentence") and (not new_page or _continues(ptext, p["text"])):
                joined = None
                if (len(ptext) >= 50 or (prev["kind"] == "item" and p["text"][:1].islower())) and \
                        not _SENTENCE_END.search(ptext) and not prev.get("cells") and \
                        not _AMOUNT_END.search(ptext) and \
                        not ((_whole_row(ptext) or _two_records(ptext, p["text"])) and
                             (p["text"][:1].isupper() or p["text"][:1].isdigit())):
                    if ptext.endswith("-") and ptext[-2:-1].isalpha() and p["text"][:1].islower():
                        joined = ptext[:-1] + p["text"]
                    else:
                        joined = ptext + " " + p["text"]
                elif prev["kind"] == "item":
                    out.append(dict(p))
                    continue
                elif prev.get("cells") is not None and len(p["text"]) < 40 and \
                        not _SENTENCE_END.search(p["text"]) and len(ptext) + len(p["text"]) < 240 and \
                        not _whole_row(p["text"]) and not _two_records(ptext, p["text"]):
                    joined = ptext + " " + p["text"]
                    prev["cells"] += 1
                elif len(ptext) < 40 and len(p["text"]) < 40 and not _SENTENCE_END.search(ptext) \
                        and not _SENTENCE_END.search(p["text"]) and not _whole_row(ptext) \
                        and not _whole_row(p["text"]) and not _two_records(ptext, p["text"]):
                    joined = ptext + " " + p["text"]
                    prev["cells"] = 2
                if joined is not None:
                    prev["text"] = joined
                    prev["last_page"] = page
                    if p.get("ends_sentence"):
                        prev["ends_sentence"] = True
                    continue
        out.append(dict(p))
    return out


def _mark_headers(paras):
    """Mark the header row(s) of each sheet and table: its first row, plus the next
    one or two when the first holds a single cell (a title above the headers)."""
    by_group = OrderedDict()
    for p in paras:
        if p["kind"] == "row":
            by_group.setdefault(p["group"], []).append(p)
    for rows in by_group.values():
        for n, row in enumerate(rows[:3]):
            filled = [c for c in _cells(row["text"]) if c]
            row["header"] = True
            if len(filled) > 1 or n == 2:
                break


def _cell_shape(value):
    """A cell as compared for rarity: a phrase of three or more words with its
    numbers replaced by '#', so 'Refer to response to RFI-010.' and '... RFI-053.'
    count as the same template answer; short labels and figures stay as they are."""
    return _DIGITS.sub("#", value) if len(value.split()) >= 3 else value


def _mark_rarity(paras):
    """Give each row (not header rows) "rare": the sum over its filled cells of
    1 / how many rows of its table have the same value in that column (phrases
    are compared with their numbers ignored, see _cell_shape), so a row whose
    cells are unusual for their columns counts for more. A cell saying the
    item is still open ('Open', 'Pending', 'Overdue' ...) that fewer than half the
    column's filled cells share adds OPEN_RARE: the open items of a register
    matter most."""
    by_group = OrderedDict()
    for p in paras:
        if p["kind"] == "row" and not p.get("header"):
            by_group.setdefault(p["group"], []).append(p)
    for rows in by_group.values():
        cells = [[_key(c) for c in _cells(r["text"])] for r in rows]
        shapes = [[_cell_shape(value) for value in row] for row in cells]
        counts = Counter((col, value) for row in cells for col, value in enumerate(row) if value)
        shape_counts = Counter((col, value) for row in shapes for col, value in enumerate(row) if value)
        filled = Counter(col for row in cells for col, value in enumerate(row) if value)
        for r, row, shape in zip(rows, cells, shapes):
            r["rare"] = sum(1.0 / shape_counts[(col, value)] for col, value in enumerate(shape) if value)
            if any(value and _OPEN_STATUS.match(value) and 2 * counts[(col, value)] < filled[col]
                   for col, value in enumerate(row)):
                r["rare"] += OPEN_RARE


def _number_sections(paras):
    """Give each paragraph the index of the heading it is under (sec), and its
    weight: 2 under an executive summary / summary / conclusions / key findings
    heading, 1 under another summary / conclusion / recommendation heading (or
    one of their sub-headings), -1 in an appendix, else 0 (see _section_weight)."""
    sec = -1
    open_heads = []      # [(level, weight)] of the headings above this point
    for n, p in enumerate(paras):
        if p["kind"] in _HEAD_KINDS:
            sec = n
            level = (p.get("level") or 1) if p["kind"] == "head" else 1
            open_heads = [h for h in open_heads if h[0] < level]
            open_heads.append((level, _section_weight(p["text"])))
        p["sec"] = sec
        weights = [w for _l, w in open_heads if w]
        p["key"] = weights[-1] if weights else 0


# A report's own summary: '1 Executive Summary', 'SUMMARY OF FINDINGS', 'Conclusions and
# recommendations' (anchored, so 'Cost summary' and '6 Discussion and recommendations' are not)
_SUMMARY_HEAD = re.compile(r"(?i)^(?:\d{1,2}(?:\.\d{1,2})*\.?\s+)?(?:(?:executive|project|report|management)\s+)?"
                           r"summary\b|^(?:\d{1,2}(?:\.\d{1,2})*\.?\s+)?(?:conclusions?|key findings|"
                           r"summary of findings)\b")
_APPENDIX = re.compile(r"(?i)^(?:appendix|appendices|attachment|annex(?:ure)?|schedule)\b")
# A list of references: standards cited by number score high but say little
_REFERENCES = re.compile(r"(?i)^(?:\d{1,2}(?:\.\d{1,2})*\.?\s+)?(?:references|bibliography|"
                         r"referenced (?:documents|standards)|standards referenced)\b")


def _section_weight(heading):
    """2 for an executive summary / summary / conclusions / key findings heading, 1
    for another summary / conclusion / recommendation heading, -1 for an
    appendix or references list, else 0."""
    if _APPENDIX.match(heading) or _REFERENCES.match(heading):
        return -1
    if _SUMMARY_HEAD.match(heading):
        return 2
    return 1 if _KEY_SECTION.search(heading) else 0


# --------------------------------------------------------------------------
# Choosing what to keep

def _pieces(paras, size, score_rows=False):
    """Paragraphs cut into the pieces the cap chooses from: sentences of text and
    list items (long ones with facts split further, see cleaning.fact_pieces),
    whole rows and headings. Each piece: kind, text, para, first (True for the
    first piece of its paragraph), score, sec (the paragraph's section), for
    text "starts" (True when it begins a sentence, False for a clause cut from
    the middle of one) and for rows group/header. A table row under a summary
    heading gets only the usual SECTION_BONUS (report tables have no row cap, so
    a long 'Summary of results' table must not flood a short document); one
    under a header row counts at least as _row_floor says."""
    out = []
    headed = set(p.get("group") for p in paras if p["kind"] == "row" and p.get("header")
                 and p.get("group") is not None)
    for n, p in enumerate(paras):
        kind = p["kind"]
        if kind in ("text", "item", "add"):
            text = p["text"]
            spans, sent = cleaning.fact_pieces(text, size, sentences=True)
            if not spans:
                spans, sent = [(0, len(text))], [0]
            for k, (a, b) in enumerate(spans):
                piece = text[a:b].strip()
                if not piece:
                    continue
                score = doc_fact_score(piece) + SECTION_BONUS * (p.get("key") or 0)
                out.append({"kind": kind, "text": piece, "para": n, "first": k == 0, "score": score,
                            "level": p.get("level", 0), "starts": k == 0 or sent[k] != sent[k - 1]})
        elif kind == "row":
            if score_rows:
                score = _row_score(p["text"], p.get("rare") or 0)
            else:      # a table in a report: worth less than the sentences around it
                score = min(max(doc_fact_score(p["text"]), _row_floor(p, headed)), ROW_SCORE_MAX) + \
                    SECTION_BONUS * min(1, p.get("key") or 0)
            fixed = bool(p.get("header")) or (score_rows and bool(_TOTAL_ROW.search(p["text"])))
            out.append({"kind": "row", "text": p["text"], "para": n, "first": True, "score": score,
                        "group": p.get("group"), "header": bool(p.get("header")), "fixed": fixed,
                        "level": p.get("level", 0)})
        elif kind == "del":
            out.append({"kind": "del", "text": p["text"], "para": n, "first": True, "score": 0.5, "level": 0})
        else:
            out.append({"kind": kind, "text": p["text"], "para": n, "first": True, "score": 0,
                        "level": p.get("level", 1), "rows": p.get("rows"), "hidden": p.get("hidden"),
                        "group": p.get("group")})
    for piece in out:
        piece["sec"] = paras[piece["para"]].get("sec", -1)
    _damp_repeats(out)
    return out


# A clause cut from the middle of a sentence starts in lower case or with a figure
_CLAUSE_START = re.compile(r"[a-z0-9]")
_ROW_NUMBER = re.compile(r"^[-+]?\d[\d.,/-]*%?$")              # a bare figure in a cell: '0.55', '18', '2.8'
_ROW_CODE = re.compile(r"^[A-Z]{1,4}-?\d{1,3}[A-Za-z]?$")      # an item code: 'HP7', 'WP1', 'BH14'


def _row_floor(p, headed):
    """The least a report table row is worth when its table has a header row
    (`headed`: those tables' groups) and it is not in an appendix or references
    list: the header gives bare figures their meaning, so a row with 2+ figures
    ('Alluvial clay | 18 | 0.36 | 0.55') counts like a sentence with a figure
    (HARD_BONUS + 0.5 a figure, up to 3 more), and a row starting with an item
    code ('HP7 | 6.1 | Concrete test results | Hold') like one with a reference.
    0 for header rows, rows of fewer than 3 cells and rows in 'Changes from'."""
    if p.get("header") or p.get("group") not in headed or (p.get("key") or 0) < 0:
        return 0
    cells = [c for c in _cells(p["text"]) if c]
    if len(cells) < 3:
        return 0
    numbers = sum(1 for c in cells if _ROW_NUMBER.match(c))
    floor = HARD_BONUS + min(3.0, 0.5 * numbers) if numbers >= 2 else 0
    if _ROW_CODE.match(cells[0]):
        floor = max(floor, HARD_BONUS + 1)
    return floor


REPEAT_SHAPES = 6   # a piece sharing its words with more pieces than this counts for less


def _shape(text):
    """A piece's words with the figures taken out, as a set: the pieces of a long
    listing that only changes its figures ('Member C-07-01 L=2.2 m N*=254 kN ...',
    inspection records) share one."""
    return frozenset(re.sub(r"\d+(?:[.,/]\d+)*", "#", text.lower()).split())


def _damp_repeats(pieces):
    """Lower the score of text that repeats one pattern with only its figures
    changed (score x REPEAT_SHAPES / how many share it), so a long listing does
    not crowd out the sentences around it. The odd ends of such a listing cut
    into pieces ('m N*=249 kN M*=112 kNm util=0.58 OK': words all from a lowered
    piece of the same paragraph) are lowered as much."""
    keys = [_shape(p["text"]) if p["kind"] in ("text", "item") and len(p["text"]) >= 30 else None
            for p in pieces]
    shared = Counter(k for k in keys if k)
    factor_of = dict((k, float(REPEAT_SHAPES) / n) for k, n in shared.items() if n > REPEAT_SHAPES)
    listing = {}      # paragraph -> the lowered shapes in it
    for p, k in zip(pieces, keys):
        if k in factor_of:
            listing.setdefault(p["para"], set()).add(k)
    for p, k in zip(pieces, keys):
        if p["kind"] not in ("text", "item"):
            continue
        factors = [factor_of[k]] if k in factor_of else []
        if p["para"] in listing:
            words = k or _shape(p["text"])
            factors += [factor_of[shape] for shape in listing[p["para"]] if words <= shape]
        if factors:
            p["score"] *= min(factors)


def _cost(piece):
    return len(piece["text"]) + 3


def _fold_heads(pieces, heads):
    """The headings left out of the heading outline because FOLD_HEADS or more
    headings differ only in their numbers ('Borehole BH1' ... 'Borehole BH12'):
    the first of them stands for them all and gets "more_like" (how many others)."""
    shape_of = dict((j, re.sub(r"\d+", "#", _key(pieces[j]["text"]))) for j in heads
                    if pieces[j]["kind"] == "head")
    counts = Counter(shape_of.values())
    first, folded = set(), set()
    for j in heads:
        shape = shape_of.get(j)
        if shape is None or counts[shape] < FOLD_HEADS:
            continue
        if shape in first:
            folded.add(j)
        else:
            first.add(shape)
            pieces[j]["more_like"] = counts[shape] - 1
    return folded


def _choose(pieces, cap, row_cap=None, removed_cap=None, fold_heads=False, ride_heads=False):
    """The indexes of the pieces to keep within about `cap` characters: sheet
    names and table header rows; headings (top levels first) up to HEADING_SHARE
    of the cap; the opening up to OPENING_SHARE; then the pieces with the most
    facts (each sheet first gets its share of the room); then, in order after
    the opening, whatever else fits. With fold_heads, headings that differ only
    in their numbers show once in the outline (see _fold_heads); another of them
    is kept only together with text kept under it. With ride_heads (a version's
    changes), a heading is kept only together with a change kept under it,
    except added or removed headings, which are changes themselves."""
    keep = set()
    used = [0]
    rows_used = Counter()
    removed = [0]
    seen_text = set()
    heads = [i for i, p in enumerate(pieces) if p["kind"] in ("head", "slide")]
    folded = _fold_heads(pieces, heads) if fold_heads else set()
    if ride_heads:
        folded = set(heads)
    head_at = dict((pieces[j]["para"], j) for j in heads)

    def take(i):
        """Keep piece i: True if kept (or already kept); "skip" when it is a repeat or
        over its own limit (rows, removed text); False when there is no room. A
        repeat is the same text with the same "dedup" mark (a '- X' removed in one
        place is not a repeat of a '+ X' added in another: X moved)."""
        p = pieces[i]
        if i in keep:
            return True
        if p.get("drop"):
            return "skip"
        if p["kind"] == "row" and row_cap is not None and not p.get("fixed") and rows_used[p["group"]] >= row_cap:
            return "skip"
        if p["kind"] == "del" and removed_cap is not None and removed[0] >= removed_cap:
            return "skip"
        k = (p.get("dedup"), _key(p["text"]))
        if len(k[1]) >= 30 and p["kind"] not in _HEAD_KINDS and k in seen_text:
            return "skip"
        head = head_at.get(p.get("sec"))
        if head not in folded or head in keep or head == i:
            head = None       # (the text of a folded heading is kept with that heading)
        if used[0] + _cost(p) + (_cost(pieces[head]) if head is not None else 0) > cap:
            return False
        keep.add(i)
        used[0] += _cost(p)
        seen_text.add(k)
        if head is not None:
            keep.add(head)
            used[0] += _cost(pieces[head])
        if p["kind"] == "row" and not p.get("fixed"):
            rows_used[p["group"]] += 1
        if p["kind"] == "del":
            removed[0] += 1
        return True

    # 1. sheet names and notes, then headings by level up to the heading share
    for i, p in enumerate(pieces):
        if p["kind"] in ("sheet", "note", "meta"):
            keep.add(i)
            used[0] += _cost(p)
    head_room = HEADING_SHARE * cap
    head_used = 0
    for i in sorted(heads, key=lambda j: (pieces[j]["level"] or 1, j)):
        changed = ride_heads and (pieces[i].get("added") or pieces[i].get("removed"))
        if (i not in folded or changed) and head_used + _cost(pieces[i]) <= head_room and take(i) is True:
            head_used += _cost(pieces[i])
    # 2. header rows of tables and sheets with rows
    groups = OrderedDict()
    for i, p in enumerate(pieces):
        if p["kind"] == "row":
            groups.setdefault(p["group"], []).append(i)
    header_of = {}
    for g, idx in groups.items():
        heads_g = [i for i in idx if pieces[i].get("header")]
        for i in idx:
            header_of[i] = heads_g
    sheet_ids = set(p.get("group") for p in pieces if p["kind"] == "sheet")
    sheet_groups = [g for g in groups if g in sheet_ids]
    for g in sheet_groups:
        for i in groups[g]:
            if pieces[i].get("fixed"):
                take(i)
    # 3. the opening, in order
    opening_end = 0
    room = OPENING_SHARE * cap
    spent = 0
    for i, p in enumerate(pieces):
        if p["kind"] in _HEAD_KINDS or i in keep:
            opening_end = i + 1
            continue
        if p["kind"] == "row" and p["group"] in sheet_groups:
            break      # a spreadsheet has no opening beyond its header rows
        if spent + _cost(p) > room:
            break
        got = take(i)
        if got is False:
            break
        if got is True:
            spent += _cost(p)
        opening_end = i + 1
    # 4. facts: each sheet first gets its share of the room, then the best anywhere
    ranked = sorted((i for i in range(opening_end, len(pieces)) if pieces[i]["score"] > 0 and i not in keep),
                    key=lambda i: (-pieces[i]["score"], i))

    def lead_of(i):
        """For a clause cut from the middle of a sentence (starting lower case or
        with a figure: 'designed for 150 kPa.', '402241 Volume: 8.7 m3 ...'), the
        piece that starts its sentence, if not kept yet; else None."""
        p = pieces[i]
        if p["kind"] != "text" or p["first"] or p.get("starts", True) or not _CLAUSE_START.match(p["text"]):
            return None
        j = i - 1
        while j > 0 and not pieces[j]["first"] and not pieces[j].get("starts", True):
            j -= 1
        if pieces[j]["para"] != p["para"] or j in keep:
            return None
        return j

    def with_context(i):
        p = pieces[i]
        if p["kind"] == "row" and i not in keep:      # a row only with room for its header
            need = sum(_cost(pieces[h]) for h in header_of.get(i, []) if h not in keep)
            if need and used[0] + _cost(p) + need > cap:
                return
        lead = lead_of(i)
        if lead is not None:
            # a clause only together with the start of its sentence (both or neither)
            if i in keep or used[0] + _cost(p) + _cost(pieces[lead]) > cap:
                return
            if take(lead) is True:
                take(i)
            return
        if take(i) is not True:
            return
        if p["kind"] == "row":
            for h in header_of.get(i, []):
                take(h)
            if i + 1 < len(pieces) and pieces[i + 1].get("pair"):
                take(i + 1)       # a changed row's old cells come with it
        elif not p["first"] and i - 1 >= 0 and i - 1 not in keep and \
                cleaning._REFERS_BACK.match(p["text"].lstrip(" \u2022")):
            take(i - 1)

    if len(sheet_groups) > 1:
        share = max(0, cap - used[0]) / float(len(sheet_groups))
        for g in sheet_groups:
            start = used[0]
            for i in ranked:
                p = pieces[i]
                if p["kind"] == "row" and p["group"] == g and used[0] - start + _cost(p) <= share:
                    with_context(i)
    for i in ranked:
        with_context(i)
    # 5. then in order after the opening, while it fits (headings only as chosen above)
    for i in range(opening_end, len(pieces)):
        if i in keep or pieces[i]["kind"] in _HEAD_KINDS:
            continue
        got = take(i)
        if got is False:
            break
        if got is True and pieces[i]["kind"] == "row":
            for h in header_of.get(i, []):
                take(h)
    return keep


def _piece_text(p):
    text = p["text"]
    if p["kind"] == "item" and p["first"] and not text.startswith("\u2022"):
        text = "\u2022 " + text
    return text


def _render(pieces, keep, diff=False):
    """The kept pieces as lines: headings ('# ...'), a line per paragraph (list
    items joined with ' \u2022 ' to the line above), rows on their own lines, and
    '\u2026' where something was left out (at the end of the line before the gap,
    or on a line of its own after a row). A sheet ends with '(+N more rows)'
    when rows were left out. With diff=True, added text starts '+ ' (a line per
    paragraph) and removed text '- '."""
    lines, kinds = [], []
    sheet = {"group": None, "kept": 0, "total": 0}

    def push(kind, text):
        lines.append(text)
        kinds.append(kind)

    def mark_gap():
        if not lines or lines[-1].endswith("\u2026") or kinds[-1] == "more":
            return      # (a line cut short already ends with '\u2026')
        if kinds[-1] in ("row", "del"):
            push("gap", "\u2026")
        else:
            lines[-1] += " \u2026"

    def close_sheet():
        if sheet["group"] is None:
            return
        more = sheet["total"] - sheet["kept"]
        sheet["group"] = None
        if more > 0:
            if lines and lines[-1] == "\u2026":
                lines.pop()
                kinds.pop()
            push("more", "(+%d more rows)" % more)

    def mark(p):
        if not diff:
            return ""
        return "+ " if p.get("added") else "- " if p.get("removed") else ""

    open_line = None      # ("text", None) or ("add", paragraph) while a line is being added to
    prev = -1
    for i in sorted(keep):
        p = pieces[i]
        kind = p["kind"]
        gap = i > prev + 1 and prev >= 0
        # A paragraph is one line; list items join the line above them.
        joins = open_line is not None and (
            (kind in ("text", "item") and open_line == ("text", p["para"])) or
            (kind == "item" and p["first"] and open_line[0] == "text") or
            (kind == "add" and open_line == ("add", p["para"])))
        if joins:
            lines[-1] += (" \u2026 " if gap else " ") + _piece_text(p)
            open_line = (open_line[0], p["para"])
        else:
            if gap:
                mark_gap()
            open_line = None
            if kind in _HEAD_KINDS or kind in ("note", "meta"):
                close_sheet()
            if kind == "sheet" and not diff:
                rows = p.get("rows")
                label = "# Sheet " + p["text"]
                if p.get("hidden") and not p["text"].endswith("(hidden)"):
                    label += " (hidden)"
                elif isinstance(rows, int):
                    label += " (%s)" % _plural(rows, "row")
                push("head", label)
                sheet.update(group=p.get("group"), kept=0, total=rows if isinstance(rows, int) else 0)
            elif kind in _HEAD_KINDS:
                text = ("Sheet " + p["text"]) if kind == "sheet" else p["text"]
                if p.get("more_like"):
                    text += " (+%d more like it)" % p["more_like"]
                push("head", mark(p) + "# " + text)
            elif kind in ("note", "meta"):
                push("note", p["text"])
            elif kind == "row":
                push("row", mark(p) + p["text"])
                if p.get("group") == sheet["group"]:
                    sheet["kept"] += 1
            elif kind == "del":
                push("del", "- " + (p["text"] if p.get("pair") else _cut(p["text"], REMOVED_MAX)))
            elif kind == "add":
                push("add", "+ " + p["text"])
                open_line = ("add", p["para"])
            else:
                push("text", _piece_text(p))
                open_line = ("text", p["para"])
        prev = i
    if 0 <= prev < len(pieces) - 1:
        mark_gap()
    close_sheet()
    return lines


def _body_size(caps, share=1.0):
    """The split size for long sentences (see cleaning.fact_pieces)."""
    return int(max(120, min(400, caps["chars"] * share * 0.05)))


def condense(doc, caps, cap=None, name="", seen=None):
    """The text of one document (DocText) as lines, within about `cap` characters
    (default: the level's cap). `name` (the file name) and `seen`: see
    _condense_paras. Returns (lines, paragraphs)."""
    cap = caps["chars"] if cap is None else cap
    paras = _paragraphs(doc)
    return _condense_paras(paras, doc, caps, cap, name, seen), paras


def _condense_paras(paras, doc, caps, cap, name="", seen=None):
    """Lines for a document's paragraphs within about `cap` characters. A Word
    header/footer line whose words are all in the file name, the title or the
    opening is left out (_meta_redundant). With `seen` (a set shared by the
    documents of a digest, in output order), a plain sentence already shown in
    an earlier document is left out (see _drop_seen), and the plain sentences
    shown here are added to it."""
    paras = _page_capped(paras, doc, caps)
    if seen is not None:
        paras = _drop_seen(paras, seen)
    sheet = (doc.get("kind") == "xlsx" or any(p["kind"] == "sheet" for p in paras))
    pieces = _pieces(paras, _body_size(caps, cap / float(caps["chars"] or 1)), score_rows=sheet)
    if not pieces:
        return []
    if any(piece["kind"] == "meta" for piece in pieces):
        known = _known_words(name, doc, paras)
        pieces = [piece for piece in pieces if piece["kind"] != "meta" or not _meta_redundant(piece["text"], known)]
    keep = _choose(pieces, cap, row_cap=caps["rows"] if sheet else None, fold_heads=True)
    if seen is not None:
        seen.update(_plain_key(pieces[i]["text"]) for i in keep
                    if pieces[i]["kind"] == "text" and _plain_key(pieces[i]["text"]))
    return _render(pieces, keep)


def _known_words(name, doc, paras):
    """The words (lower case, letters and digits) of a document's file name, its
    title property and its first three paragraphs other than header/footer lines."""
    texts = [name or "", doc.get("title") or ""]
    texts += [p["text"] for p in paras if p["kind"] != "meta" and p.get("text")][:3]
    return set(re.findall(r"[a-z0-9]+", " ".join(texts).lower()))


def _meta_redundant(text, known_words):
    """True for a Word 'Header: ...' / 'Footer: ...' line that adds nothing: every
    word in it is already in `known_words` (see _known_words), as when it only
    repeats the title ('Header: Inception Meeting Minutes'). A header or footer
    with a reference, date or name found nowhere else is kept."""
    body = text.split(": ", 1)[1] if ": " in text else text
    words = [w for piece in body.lower().split("|") for w in re.findall(r"[a-z0-9]+", piece)]
    return bool(words) and all(w in known_words for w in words)


SEEN_MIN = 40      # a sentence this long already shown in an earlier document is left out


def _plain_key(text):
    """A sentence as compared across documents ('' when it may not be left out):
    lower case, single spaces; only sentences of SEEN_MIN+ characters without a
    date, amount, reference or figure with a unit (closing wording such as a
    disclaimer or 'Any corrections to these minutes should be advised ...')."""
    text = text.strip(" \u2022")
    if len(text) < SEEN_MIN or _hard_facts(text) or any(rx.search(text) for rx, _w in _DOC_HARD):
        return ""
    return _key(text)


def _drop_seen(paras, seen):
    """The paragraphs without the plain sentences (see _plain_key) in `seen`. Text
    paragraphs only: a list item said again in a later document (an action carried
    over to the next minutes) still means something there. A paragraph left with
    no sentence keeps its place with no text (paragraph indexes are used by the
    headings), so it shows nothing."""
    out = []
    for p in paras:
        if p["kind"] == "text" and seen:
            sentences = _split_sentences(p["text"])
            left = [x for x in sentences if _plain_key(x) not in seen]       # ('' is never in seen)
            if len(left) < len(sentences):
                p = dict(p, text=" ".join(left))
        out.append(p)
    return out


# --------------------------------------------------------------------------
# Versions

_REV_MARKS = [
    re.compile(r"\[[a-z]{1,2}\d{0,2}\]|\[\d{1,2}\]"),                          # [C], [P1], [2]
    re.compile(r"\((?:\d{1,2}|copy|[a-z])\)"),                                 # (1), (copy)
    re.compile(r"\b(?:rev(?:ision)?|issue|version|ver|amendment|amdt)\b\.?\s*(?:no\.?\s*)?[a-z]?\d{0,3}[a-z]?\b"),
    re.compile(r"\brev[a-z]?\d{0,2}\b|\bv\d{1,3}(?:\s\d{1,3})?\b|\br\d{1,2}\b|\bp\d{1,2}\b"),
    re.compile(r"\b(?:19|20)\d\d\s?\d\d\s?\d\d\b|\b\d{1,2}\s\d{1,2}\s(?:19|20)?\d\d\b|\b\d{6}\b"
               r"|\b(?:19|20)\d\d\s(?:0[1-9]|1[0-2])\b"                       # 2025-08 (a month)
               r"|\b\d{1,2}(?:st|nd|rd|th)?\s" + _MONTH + r"(?:\s(?:19|20)?\d\d)?\b"
               r"|\b" + _MONTH + r"\s(?:19|20)\d\d\b"),
    re.compile(r"\b(?:draft|final|issued?|ifc|for (?:review|construction|approval|comment|information|tender)|"
               r"superseded|copy of|copy|signed|updated?|clean|tracked|marked up|markup|current|latest|old)\b"),
]


def family_key(name):
    """A file name without its revision marks, dates and extension, for finding
    versions of one document ('Geotech Report Rev B.pdf' and 'Geotech report_v3.docx'
    -> 'geotech report'). '' when nothing is left."""
    base = re.split(r"[\\/]", name or "")[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    base = cleaning.normalise_text(base).lower()
    base = re.sub(r"[_.\-]+", " ", base)
    for rx in _REV_MARKS:
        base = rx.sub(" ", base)
    base = re.sub(r"[^a-z0-9]+", " ", base).strip()
    return base if len(base.replace(" ", "")) >= 3 else ""


_LIST_LABEL = re.compile(r"^(?:\u2022|[-*]|\(?(?:\d{1,2}|[a-z]|[ivx]{1,4})[.)])\s+")


def _compare_text(text):
    """Text as compared between versions: lower case, letters, digits, $ and % only
    (so re-wrapped lines, list bullets and punctuation do not count as changes)."""
    t = _LIST_LABEL.sub("", (text or "").lower())
    return re.sub(r"[^a-z0-9$%]+", " ", t).strip()


def _split_sentences(text):
    return [text[x:y].strip() for x, y in cleaning.fact_pieces(text, 10 ** 6) if text[x:y].strip()]


def version_units(paras):
    """A document as comparable units (compare key, paragraph index, text):
    headings, rows and the sentences of its text. Comparing sentences rather
    than whole paragraphs means a PDF and the Word file it was made from (or two
    PDFs whose lines wrap differently) still line up."""
    out = []
    for n, p in enumerate(paras):
        kind = p["kind"]
        if kind in ("text", "item"):
            for sentence in _split_sentences(p["text"]):
                k = _compare_text(sentence)
                if k:
                    out.append((k, n, sentence))
        elif kind in _HEAD_KINDS:
            text = p["title"] if kind == "slide" else p["text"]
            k = _compare_text(text)
            if k:
                out.append(("# " + kind + " " + k, n, text))
        elif kind == "row":
            k = _compare_text(p["text"])
            if k:
                out.append(("| " + k, n, p["text"]))
    return out


WORD_DIFF_MAX = 2000000   # old words x new words above which a changed stretch is not compared word by word


def _too_long(n_old, n_new):
    """True when a changed stretch is too long to compare word by word: difflib
    slows down sharply on long texts with many repeated words (a register whose
    every row changed, a programme whose every date moved)."""
    return n_old * n_new > WORD_DIFF_MAX


def _words_ratio(a, b):
    """How alike two texts are, by words (0..1). Very long texts (see _too_long)
    are compared by the words they share, in any order, which is quick."""
    wa, wb = a.split(), b.split()
    if not wa or not wb:
        return 0.0
    if _too_long(len(wa), len(wb)):
        shared = sum((Counter(wa) & Counter(wb)).values())
        return 2.0 * shared / (len(wa) + len(wb))
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def _plain(keys):
    """Compare keys joined, without the heading/row marks."""
    return " ".join(re.sub(r"^(?:# \w+|\|) ", "", k) for k in keys)


def compare(old_units, new_units):
    """(similarity 0..1, opcodes) of two versions (lists from version_units):
    unchanged units count in full, a changed stretch by how alike its words are.
    A stretch with the same words cut up differently (a Word heading that is a
    plain line in the PDF, a table that became lines) counts as unchanged and
    its opcode becomes "equal"."""
    a = [u[0] for u in old_units]
    b = [u[0] for u in new_units]
    total = sum(len(x) for x in a) + sum(len(x) for x in b)
    if not total:
        return 1.0, []
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    ops = []
    matched = 0.0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            matched += sum(len(x) for x in a[i1:i2]) * 2
        elif tag == "replace":
            old, new = _plain(a[i1:i2]), _plain(b[j1:j2])
            size = sum(len(x) for x in a[i1:i2]) + sum(len(x) for x in b[j1:j2])
            if old.split() == new.split():
                matched += size
                tag = "equal"
            else:
                matched += _words_ratio(old, new) * size
        ops.append((tag, i1, i2, j1, j2))
    return matched / total, ops


WORD_CONTEXT = 6         # words shown either side of a change inside a long sentence
CHANGED_SENTENCE_MAX = 300   # a changed sentence up to this long is shown whole


def _tokens(units, start, end):
    """[(compare key, word as written, unit index)] for units[start:end]."""
    out = []
    for k in range(start, end):
        for word in units[k][2].split():
            key = _compare_text(word)
            if key:
                out.append((key, word, k))
    return out


def _snippet(tokens, units, a, b, whole_max):
    """The changed words a..b of tokens: the whole sentence(s) they are in when
    that is at most `whole_max` characters, else the words with WORD_CONTEXT
    words either side, '\u2026' where cut."""
    first, last = tokens[a][2], tokens[b - 1][2]
    whole = " ".join(units[k][2] for k in range(first, last + 1))
    if len(whole) <= whole_max:
        return whole
    lo, hi = max(0, a - WORD_CONTEXT), min(len(tokens), b + WORD_CONTEXT)
    text = " ".join(t[1] for t in tokens[lo:hi])
    return ("\u2026" if lo > 0 else "") + text + ("\u2026" if hi < len(tokens) else "")


def _word_changes(old_tokens, new_tokens, old_units, new_units):
    """[(new text or '', old text or '', new token index)] for the words that
    differ between two stretches of text; changes close together are one. The
    new text is the changed sentence (or the words around the change in a long
    one); the old text is the words around what was there before."""
    a = [t[0] for t in old_tokens]
    b = [t[0] for t in new_tokens]
    changes = [op for op in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if op[0] != "equal"]
    groups = []
    for op in changes:
        if groups and op[1] - groups[-1][-1][2] <= 2 * WORD_CONTEXT and op[3] - groups[-1][-1][4] <= 2 * WORD_CONTEXT:
            groups[-1].append(op)
        else:
            groups.append([op])
    out = []
    for g in groups:
        a1, a2, b1, b2 = g[0][1], g[-1][2], g[0][3], g[-1][4]
        new = _snippet(new_tokens, new_units, b1, b2, CHANGED_SENTENCE_MAX) if b2 > b1 else ""
        old = _snippet(old_tokens, old_units, a1, a2, REMOVED_MAX) if a2 > a1 else ""
        out.append((new, old, min(b1, len(new_tokens) - 1)))
    return out


def _row_pairs(old_units, new_units, i1, i2, j1, j2, old_paras, new_paras):
    """{new unit index: old unit index} for the rows of a changed stretch that are
    the same item in both versions: the same non-empty first cell (an RFI or
    item number), once among the old rows and once among the new ones."""
    def by_first(units, a, b, paras):
        out = {}
        for k in range(a, b):
            if paras[units[k][1]]["kind"] != "row":
                continue
            first = _key(_cells(units[k][2])[0])
            if first:
                out.setdefault(first, []).append(k)
        return out
    old_by = by_first(old_units, i1, i2, old_paras)
    new_by = by_first(new_units, j1, j2, new_paras)
    pairs = {}
    for first, ks in new_by.items():
        olds = old_by.get(first, [])
        if len(ks) == 1 and len(olds) == 1:
            pairs[ks[0]] = olds[0]
    return pairs


def _old_cells(old_text, new_text):
    """A changed row's old version as its first cell and only the cells that
    changed ('RFI-077 | \u2026 | | | | Open | \u2026', '\u2026' = cells that did not
    change); None when nothing needs showing: the new row only gained cells (it
    shows the old ones), or no cell differs."""
    old, new = _cells(old_text), _cells(new_text)
    if len(old) < len(new) and [_key(c) for c in old] == [_key(c) for c in new[:len(old)]]:
        return None
    width = max(len(old), len(new))     # (a sheet row drops its empty cells at the end)
    old, new = old + [""] * (width - len(old)), new + [""] * (width - len(new))
    changed = [c for c in range(1, width) if _key(old[c]) != _key(new[c])]
    if not changed:
        return None
    lo, hi = changed[0], changed[-1]
    cells = [old[0]] + (["\u2026"] if lo > 1 else []) + old[lo:hi + 1] + (["\u2026"] if hi < width - 1 else [])
    return _join_cells(cells)


def _closes(old_text, new_text):
    """True when a row's old version had an open status cell ('Open', 'Pending' ...)
    that the new version no longer has in that column: the item was closed."""
    old, new = _cells(old_text), _cells(new_text)
    return any(_OPEN_STATUS.match(_key(c)) and (k >= len(new) or not _OPEN_STATUS.match(_key(new[k])))
               for k, c in enumerate(old))


# A heading's section number in its compare key: '# head 6 13 construction ...' (not a year)
_SECTION_NO = re.compile(r"^(# \w+ )(?:(?:appendix|section|part|chapter|schedule|annex(?:ure)?|attachment) "
                         r"(?:[a-z]|\d{1,2})(?: \d{1,2}){0,4}|\d{1,2}(?: \d{1,2}){0,4}) (?=\S)")


def _unnumbered(key):
    """A heading's compare key without its section number ('# head 6 13 further work'
    -> '# head further work'), so a heading that was only renumbered is no change."""
    return _SECTION_NO.sub(r"\1", key)


def _diff_paras(old_paras, new_paras, old_units, new_units, ops):
    """The changes as paragraphs for _pieces/_render, in the new version's order:
    the section heading each change is under (for context), added or changed
    text ("add"; rows and headings with "added") and removed text ("del";
    headings with "removed"). In a stretch of text whose words are mostly the
    same only the changed sentences are shown (see _word_changes); changed rows
    are shown whole (and so is a stretch too long to compare word by word, see
    _too_long)."""
    out = []
    shown_heads = set()

    def context(n):
        """Show the heading of new paragraph n's section once."""
        sec = new_paras[n]["sec"] if 0 <= n < len(new_paras) else -1
        if sec >= 0 and sec not in shown_heads:
            shown_heads.add(sec)
            head = new_paras[sec]
            text = ("Sheet " + head["text"]) if head["kind"] == "sheet" else head["text"]
            out.append({"kind": "head", "text": text, "level": 1})

    def at(j):
        if j < len(new_units):
            return new_units[j][1]
        return new_units[-1][1] if new_units else -1

    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        prose = all(new_paras[u[1]]["kind"] in ("text", "item") for u in new_units[j1:j2]) and \
            all(old_paras[u[1]]["kind"] in ("text", "item") for u in old_units[i1:i2])
        if tag == "replace" and prose:
            old_t, new_t = _tokens(old_units, i1, i2), _tokens(new_units, j1, j2)
            if old_t and new_t and not _too_long(len(old_t), len(new_t)) and \
                    _words_ratio(" ".join(t[0] for t in old_t), " ".join(t[0] for t in new_t)) >= SIMILAR:
                for new, old, k in _word_changes(old_t, new_t, old_units, new_units):
                    n = new_units[new_t[k][2]][1]
                    context(n)
                    if new:
                        out.append({"kind": "add", "text": new, "level": 0, "key": new_paras[n].get("key")})
                    if old:
                        out.append({"kind": "del", "text": old, "level": 0})
                continue
        first = at(j1)
        if not (j1 < j2 and new_paras[first]["kind"] in _HEAD_KINDS):
            context(first)
        pairs = _row_pairs(old_units, new_units, i1, i2, j1, j2, old_paras, new_paras)
        # headings only renumbered ('6.11 Further work' -> '6.13 Further work') are no change
        renumbered = Counter(_unnumbered(u[0]) for u in old_units[i1:i2] if u[0].startswith("# ")) & \
            Counter(_unnumbered(u[0]) for u in new_units[j1:j2] if u[0].startswith("# "))
        old_skip = renumbered.copy()
        for k in range(j1, j2):
            ukey, n, text = new_units[k]
            p = new_paras[n]
            if p["kind"] in _HEAD_KINDS and renumbered[_unnumbered(ukey)] > 0:
                renumbered[_unnumbered(ukey)] -= 1
                shown_heads.add(n)      # (shown for the changes under it, as context)
                label = ("Sheet " + p["text"]) if p["kind"] == "sheet" else p["text"]
                out.append({"kind": "head", "text": label, "level": 1})
            elif p["kind"] in _HEAD_KINDS:
                shown_heads.add(n)
                label = ("Sheet " + p["text"]) if p["kind"] == "sheet" else p["text"]
                out.append({"kind": "head", "text": label, "level": 1, "added": True})
            elif p["kind"] == "row":
                # a changed row: '+ new row', then '- ' its old changed cells; an item
                # closed since ('Open' -> 'Closed') counts like an open item
                old_text = old_units[pairs[k]][2] if k in pairs else None
                rare = (p.get("rare") or 0) + (OPEN_RARE if old_text is not None and _closes(old_text, text) else 0)
                out.append({"kind": "row", "text": text, "level": 0, "added": True, "group": None, "rare": rare})
                was = _old_cells(old_text, text) if old_text is not None else None
                if was is not None:
                    out.append({"kind": "del", "text": was, "level": 0, "pair": True})
            elif out and out[-1]["kind"] == "add" and out[-1].get("src") == n:
                out[-1]["text"] += " " + text
            else:
                out.append({"kind": "add", "text": text, "level": 0, "key": p.get("key"), "src": n})
        paired = set(pairs.values())
        for k in range(i1, i2):
            if k in paired:
                continue      # (shown with its new row)
            ukey, n, text = old_units[k]
            p = old_paras[n]
            if p["kind"] in _HEAD_KINDS and old_skip[_unnumbered(ukey)] > 0:
                old_skip[_unnumbered(ukey)] -= 1
            elif p["kind"] in _HEAD_KINDS:
                label = ("Sheet " + p["text"]) if p["kind"] == "sheet" else p["text"]
                out.append({"kind": "head", "text": _cut(label, REMOVED_MAX), "level": 1, "removed": True})
            elif out and out[-1]["kind"] == "del" and out[-1].get("src") == n:
                out[-1]["text"] += " " + text
            else:
                out.append({"kind": "del", "text": text, "level": 0, "src": n})
    return out


def _diff_lines(base, entry, caps, cap):
    """'Changes from Dn:' and the changes, capped like a document."""
    title = "Changes from %s:" % (base["id"] or base["name"])
    cut = base.get("cut") or entry.get("cut")
    if cut:
        title = "Changes from %s in the part read (the rest was not read, so not compared):" % (
            base["id"] or base["name"])
    paras = _diff_paras(base["paras"], entry["paras"], base["units"], entry["units"], entry["ops"])
    if not paras:
        return [title + (" none." if cut else " none (the text is the same).")]
    sec = -1
    for n, p in enumerate(paras):      # each change is under the heading before it
        if p["kind"] == "head":
            sec = n
        p["sec"] = sec
    sheet = entry["doc"].get("kind") == "xlsx" or any(p["kind"] == "sheet" for p in entry["paras"])
    pieces = _pieces(paras, _body_size(caps), score_rows=sheet)
    for n, piece in enumerate(pieces):
        src = paras[piece["para"]]
        piece["added"] = src.get("added")
        piece["removed"] = src.get("removed")
        if piece["kind"] in ("add", "row") and piece["score"] < 1:
            piece["score"] = 1
        if src.get("pair") and n > 0:      # a changed row's old cells rank just after its new row
            piece["score"] = max(0.5, pieces[n - 1]["score"] - 0.01)
            piece["pair"] = True
    # '- X' is not a repeat of '+ X' (X moved), unless X repeats in either version:
    # only its first copy is compared, so that copy may seem to move when it did not
    repeated = set(_key(p["text"]) for p in base["paras"] + entry["paras"] if p.get("repeated"))
    for piece in pieces:
        if piece["kind"] == "del" and _key(piece["text"]) not in repeated:
            piece["dedup"] = "del"
    # such a copy that seems to move: only the earlier of '- X' and '+ X' is surely
    # true (the later place may have held a dropped copy all along), so keep that one
    first_seen = {}
    for piece in pieces:
        k = _key(piece["text"])
        if k in repeated and piece["kind"] in ("add", "row", "del"):
            side = "del" if piece["kind"] == "del" else "add"
            if first_seen.get(k, side) != side:
                piece["drop"] = True
            first_seen.setdefault(k, side)
    keep = _choose(pieces, cap, removed_cap=caps["removed"], ride_heads=True)
    return [title] + _render(pieces, keep, diff=True)


# A DocText note saying reading stopped before the end (docs.py's 'partly read: ...', a
# PDF reader's 'stopped after N pages', 'Text limit reached; pages from N on were not
# read.', 'too large or complex to read in full', 'N pages could not be read')
_CUT_NOTE = re.compile(r"(?i)^partly read|stopped after|were not read|read in full|text limit|could not be read")


def _partly_read(doc):
    """True when reading stopped before the end of the document (a text, row,
    page or time limit), so its versions can be compared only in the part read.
    Besides the note, the blocks tell: a PDF with fewer pages read than it has,
    or a sheet with more rows than were kept (DocTexts cached without a note)."""
    if doc.get("retry") or _CUT_NOTE.search(doc.get("note") or ""):
        return True
    blocks = doc.get("blocks") or []
    pages = doc.get("pages")
    if doc.get("kind") == "pdf" and isinstance(pages, int) and \
            pages > sum(1 for b in blocks if b.get("type") == "page"):
        return True
    rows = kept = 0
    for b in blocks + [{"type": "sheet"}]:
        if b.get("type") == "sheet":
            if isinstance(rows, int) and kept < rows:
                return True       # the sheet before had more rows than were kept
            rows, kept = b.get("rows") or 0, 0
        elif b.get("type") == "row":
            kept += 1
    return False


def _find_versions(entries, cancel=None):
    """Mark later versions. A document with the same text as an earlier one gets
    entry["same_as"]. Otherwise, for documents with the same family_key (oldest
    first, see _sort_versions), a later one whose text is SIMILAR or more like the
    version before it gets entry["base"] and entry["ops"] (its changes are shown
    instead). A document read only in part (see _partly_read: entry["cut"]) is
    never "the same text" as another. Raises digest.DigestCancelled when
    `cancel` is set."""
    fingerprints = {}
    for e in entries:
        e["units"] = version_units(e["paras"]) if e["doc"].get("kind") != "zip" else []
        if e["doc"].get("kind") == "text":
            # a .csv's one sheet is named after the file: not compared
            e["units"] = [u for u in e["units"] if not u[0].startswith("# sheet ")]
        e["cut"] = _partly_read(e["doc"])
        if not e["units"] or e["cut"]:
            continue
        fp = "\n".join(u[0] for u in e["units"])
        if fp in fingerprints:
            e["same_as"] = fingerprints[fp]
        else:
            fingerprints[fp] = e
    families = OrderedDict()
    for e in entries:
        if e["doc"].get("kind") == "zip" or not e["units"]:
            continue
        key = family_key(e["name"])
        if key:
            families.setdefault(key, []).append(e)
    for members in families.values():
        _sort_versions(members)
        for n, newer in enumerate(members):
            _check_cancel(cancel)
            if n == 0 or newer.get("same_as") is not None:
                continue
            older = members[:n]
            kind = newer["doc"].get("kind")
            twin = [o for o in older if _stem(o["name"]) == _stem(newer["name"]) and o["doc"].get("kind") != kind]
            if twin:
                sim, _ops = compare(twin[-1]["units"], newer["units"])
                if sim >= FORMAT_SIMILAR:
                    newer["twin"] = twin[-1]
                    newer["similar"] = sim
                    continue
            # compare with the latest earlier version in the same format (Word with Word,
            # PDF with PDF), so differences in how the text was read do not show as changes
            same_kind = [o for o in older if o["doc"].get("kind") == kind]
            base = same_kind[-1] if same_kind else older[-1]
            sim, ops = compare(base["units"], newer["units"])
            if sim >= SIMILAR:
                newer["base"] = base
                newer["ops"] = ops
                newer["similar"] = sim


# A revision in a file name: '[C]', 'Rev B', 'rev_2', 'Issue 3', '_v2', 'P1'
_VERSION_REV = re.compile(r"\[([a-z]{1,2}\d{0,2}|\d{1,2})\]|(?:\b|_)(?:rev(?:ision)?|issue)[\s._-]*([a-z]{1,2}\d{0,2}|\d{1,3})\b"
                          r"|(?:\b|_)v(\d{1,3})\b|\b(p\d{1,2})\b", re.I)
_VERSION_DATE = re.compile(r"\b((?:19|20)\d\d)[-_.]?(\d\d)[-_.]?(\d\d)\b")
_VERSION_MONTH = re.compile(r"\b((?:19|20)\d\d)[-_. ](0[1-9]|1[0-2])\b")     # 'Monitoring 2025-08'


def _revision_key(name):
    """A revision from a file name as a sortable tuple, or None: preliminary
    revisions (P1, P2) first, then letters (A, B ... AA), then numbers (0, 1, 2,
    v2), as Australian drawing and report revisions run."""
    base = re.split(r"[\\/]", name or "")[-1]
    m = _VERSION_REV.search(base.rsplit(".", 1)[0] if "." in base else base)
    if not m:
        return None
    rev = (m.group(1) or m.group(2) or m.group(3) or m.group(4) or "").upper()
    if re.match(r"^P\d+$", rev):
        return (0, int(rev[1:]), "")
    if rev.isdigit():
        return (2, int(rev), "")
    return (1, len(rev), rev)


def _version_day(entry):
    """The date of a version: a date (or year and month) in its file name, else
    the first day it was sent or saved ('' if unknown)."""
    base = re.split(r"[\\/]", entry["name"] or "")[-1]
    m = _VERSION_DATE.search(base)
    if m:
        return "%s-%s-%s" % m.groups()
    m = _VERSION_MONTH.search(base)
    if m:
        return "%s-%s" % m.groups()
    days = _source_days(entry)
    return min(days) if days else ""


def _sort_versions(members):
    """Put one document's versions oldest first: by the revision in their names
    when every one has one, else by date (in the name, else when first sent or
    saved), then by ID. (IDs follow first appearance, and an old revision kept in
    a 'Superseded' folder appears after the new one attached to an email.)"""
    revs = [_revision_key(e["name"]) for e in members]
    with_rev = all(r is not None for r in revs)
    keys = {}
    for e, rev in zip(members, revs):
        day = _version_day(e) or "9999"
        keys[id(e)] = ((rev, day) if with_rev else (day,)) + (_id_number(e), e["order"])
    members.sort(key=lambda e: keys[id(e)])


def _stem(name):
    """A file name without its extension, lower case ('Report Rev B.pdf' -> 'report rev b')."""
    base = re.split(r"[\\/]", name or "")[-1]
    return (base.rsplit(".", 1)[0] if "." in base else base).strip().lower()


# --------------------------------------------------------------------------
# Drawings

_TB_TITLE = re.compile(r"(?i)^(?:drawing|dwg|sheet)?\s*title\s*[:\-]?\s*(.*)$")
_TB_REV_LABEL = re.compile(r"(?i)^(?:rev(?:ision)?|issue)\.?\s*(?:no\.?)?\s*[:\-]?\s*$")
_TB_REV = re.compile(r"(?i)^(?:rev(?:ision)?|issue)\.?\s*(?:no\.?)?\s*[:\-]?\s*([A-Z]{1,2}\d{0,2}|\d{1,2})$")
_REV_CODE = re.compile(r"^(?:[A-Z]{1,2}\d{0,2}|P\d{1,2}|\d{1,2})$")
_NAME_REV = re.compile(r"\[([A-Za-z]{1,2}\d{0,2})\]|\brev(?:ision)?[\s._-]*([A-Z0-9]{1,3})\b", re.I)
_TB_LABELS = re.compile(r"(?i)^(?:scale|drawn|designed|checked|approved|date|project|client|job|sheet|size|"
                        r"rev(?:ision)?|status|drawing (?:no|number)|dwg no|north|notes?)\b")
# A title or revision inside a longer title block line
_TB_TITLE_IN = re.compile(
    r"(?i)\b(?:drawing |dwg |sheet )?title\s*[:\-]?\s*(.+?)(?=\s+(?:scale|drawn|designed|checked|approved|"
    r"date|project|client|job|sheet(?=\s*(?:no\.?|number|[:\-]))|size|rev(?:ision)?|status|drawing no|"
    r"drawing number|dwg no)\b|$)")
_TB_REV_IN = re.compile(r"\b(?:REV(?:ISION)?|Rev(?:ision)?)\.?\s*(?:No\.?|NO\.?)?\s*[:\-]?\s*"
                        r"([A-Z]{1,2}\d{0,2}|P\d{1,2}|\d{1,2})\b(?![./]\d)")
# A revision table row: 'D 29.04.25 FOOTINGS REVISED TO SUIT GEOTECH REV C' (rev, date, description)
_REV_ROW = re.compile(r"^([A-Z]{1,2}\d{0,2}|P\d{1,2}|\d{1,2})\s+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s+(\S.*)$")
# 'DRAWING No. RD-ST-1202' in a title block
_DWG_NO = re.compile(r"(?i)\b(?:drawing|dwg)\s*(?:no\.?|number)\s*[:\-]?\s*([A-Z0-9]+(?:[-_.][A-Z0-9]+)+)")
# Title block cells already shown on a drawing's line (not notes)
_TB_CELL = re.compile(r"(?i)^(?:project|client|title|scale|drawn|designed|checked|approved|status|job)\b"
                      r"(?:\s*no\.?)?\s*:|\b(?:drawing|dwg)\s*(?:no\.?|number)\s*[:\-]?\s*[A-Z0-9]")
_TB_STATUS = re.compile(r"(?i)\b(?:not for construction|for construction|for tender|for approval|for review|"
                        r"for information|for coordination|for comment|preliminary|issued for [a-z]+)\b")


def _drawing_cells(doc):
    """The drawing's text as short cells (title blocks arrive as rows or lines)."""
    cells = []
    for block in doc.get("blocks") or []:
        text = _squash(block.get("text"))
        if not text:
            continue
        for cell in text.split(" | "):
            cell = cell.strip()
            if cell:
                cells.append(cell)
    return cells


def title_block(doc, name=""):
    """(title, revision, status) read from a drawing's title block text ('' when not found).
    The revision in the file name ('[H]', 'Rev H') wins."""
    cells = _drawing_cells(doc)
    title = ""
    for n, cell in enumerate(cells):
        m = _TB_TITLE.match(cell)
        if not m:
            continue
        value = m.group(1).strip(" :-")
        if not value:
            nxt = []
            for c in cells[n + 1:n + 3]:
                if _TB_LABELS.match(c) or _TB_TITLE.match(c) or not re.search(r"[A-Za-z]{3}", c):
                    break
                nxt.append(c)
            value = " ".join(nxt)
        if re.search(r"[A-Za-z]{3}", value):
            title = _cut(value, TITLE_MAX)
            break
    if not title:
        for cell in cells:
            m = _TB_TITLE_IN.search(cell)
            if m and re.search(r"[A-Za-z]{3}", m.group(1)) and not _TB_LABELS.match(m.group(1)):
                title = _cut(m.group(1).strip(" :-"), TITLE_MAX)
                break
    rev = ""
    m = _NAME_REV.search(re.split(r"[\\/]", name or "")[-1])
    if m:
        rev = (m.group(1) or m.group(2) or "").upper()
    if not rev:
        for n, cell in enumerate(cells):
            m = _TB_REV.match(cell)
            if m:
                rev = m.group(1).upper()
            elif _TB_REV_LABEL.match(cell) and n + 1 < len(cells) and _REV_CODE.match(cells[n + 1]):
                rev = cells[n + 1].upper()
            elif len(cell) <= 80:
                found = _TB_REV_IN.findall(cell)
                if found:
                    rev = found[-1].upper()
    found = _TB_STATUS.findall(" ".join(cells))
    status = found[-1].upper() if found else ""      # the last one: revision tables list the latest last
    return title, rev, status


# Where a revision row starts inside a longer text: rows can arrive merged into one
# paragraph ('D 29.04.25 FOOTINGS REVISED ... LO SB TH C 14.02.25 ISSUED FOR CONSTRUCTION LO SB TH')
_REV_ROW_START = re.compile(r"(?:^|\s)([A-Z]{1,2}\d{0,2}|P\d{1,2}|\d{1,2})\s+(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})\s+(?=\S)")
# A revision table heading with initials columns after the description: 'REV DATE DESCRIPTION BY CHK APP'
_REV_HEAD = re.compile(r"(?i)\bdescription((?:\s+(?:by|drn|drawn|des|dsg|chk|chkd|ckd|checked|app|appd|apr|"
                       r"approved|ver|verified|rvw|reviewed))+)\s*$")
_INITIALS_RUN = re.compile(r"(?:\s+[A-Z]{2,3}){2,3}$")      # ' LO SB TH' at the end of a row


def _revision_rows(doc):
    """A drawing's revision table rows as (rev, date, description, cell), with rows
    the PDF merged into one paragraph split apart and the BY / CHK / APP initials
    left off: as many as the table heading has columns for ('DESCRIPTION BY CHK
    APP'), or without a heading the same run of initials ending 2 or more rows
    (so 'ISSUED FOR DA' and 'ADDED NEW PIT' keep their last words)."""
    cells = _drawing_cells(doc)
    initials = 0
    for cell in cells:
        m = _REV_HEAD.search(cell)
        if m:
            initials = len(m.group(1).split())
    rows = []
    for cell in cells:
        if not _REV_ROW.match(cell):
            continue
        starts = list(_REV_ROW_START.finditer(cell))
        for n, m in enumerate(starts):
            end = starts[n + 1].start() if n + 1 < len(starts) else len(cell)
            rows.append([m.group(1).upper(), m.group(2), cell[m.end():end].strip(), cell])
    run_re = re.compile(r"(?:\s+[A-Z]{2,3}){%d}$" % initials) if initials else _INITIALS_RUN
    ends = []
    for r in rows:
        m = run_re.search(r[2])
        ends.append(m.group(0) if m else "")
    for r, run in zip(rows, ends):
        if run and (initials or ends.count(run) >= 2) and len(r[2]) > len(run):
            r[2] = r[2][:-len(run)].strip()
    return [tuple(r) for r in rows]


def _latest_revision(doc, rev):
    """The revision table row for revision `rev` as (rev, date, description, cell),
    e.g. ('D', '29.04.25', 'FOOTINGS REVISED TO SUIT GEOTECH REV C', ...); None when
    there is none. The drawing's own revision is matched, not the highest one:
    revision tables run top-down or bottom-up (see _revision_rows)."""
    if not rev:
        return None
    for row in _revision_rows(doc):
        if row[0] == rev:
            return row
    return None


def _rev_text(row, status):
    """A revision row's date and description ('29.04.25 FOOTINGS REVISED ...'), or ''
    when the description only repeats the status ('ISSUED FOR CONSTRUCTION')."""
    desc = row[2]
    if _letters(desc) in (_letters(status), "issued" + _letters(status)):
        return ""
    return "%s %s" % (row[1], _cut(desc, 60))


def _page_docs(doc):
    """A PDF's pages, each as a DocText of its own."""
    pages = []
    for block in doc.get("blocks") or []:
        if block.get("type") == "page":
            pages.append([])
        elif pages:
            pages[-1].append(block)
    return [dict(doc, blocks=b) for b in pages]


def _sheet_list(doc):
    """'RD-ST-1001 B COVER SHEET; RD-ST-1002 B GENERAL NOTES; ...' for a set of drawing
    sheets (up to SHEETS_MAX, then '(+N more)'); '' when fewer than two sheets show
    a drawing number or title."""
    found = []
    for page in _page_docs(doc):
        title, rev, _status = title_block(page, "")
        number = ""
        for cell in _drawing_cells(page):
            m = _DWG_NO.search(cell)
            if m:
                number = m.group(1)
        if number or title:
            found.append(" ".join(x for x in (number, rev, title) if x))
    if len(found) < 2:
        return ""
    more = len(found) - SHEETS_MAX
    return "; ".join(found[:SHEETS_MAX]) + (" (+%d more)" % more if more > 0 else "")


def _drawing_line(entry, caps, folders, prefix="", seen=None):
    """A drawing's line in the Drawings list: ID, name (sheets, where from) - title
    (or the sheets of a set), the latest revision's date and description, the
    status; light also adds notes (not grid lines, title block cells, revision
    table rows, or notes in `seen`: shown on an earlier drawing's line)."""
    doc = entry["doc"]
    pages = doc.get("pages")
    bits = []
    if isinstance(pages, int) and pages > 0:
        bits.append(_plural(pages, "sheet"))
    src = _short_source(entry, folders)
    if src:
        bits.append(src)
    line = "%s%s %s" % (prefix, entry["id"], entry["name"]) if entry["id"] else prefix + entry["name"]
    if bits:
        line += " (%s)" % ", ".join(bits)
    title, rev, status = title_block(doc, entry["name"])
    sheets = _sheet_list(doc) if isinstance(pages, int) and pages > 1 else ""
    row = None if sheets else _latest_revision(doc, rev)
    rev_text = _rev_text(row, status) if row else ""
    extra = []
    if sheets:
        extra.append("sheets: " + sheets)
    elif title and _letters(title) not in _letters(entry["name"]):
        extra.append("title: " + title)
    if rev and (rev_text or not _NAME_REV.search(re.split(r"[\\/]", entry["name"])[-1])):
        extra.append(("rev %s %s" % (rev, rev_text)).strip())      # (not repeated when the file name shows it)
    if status:
        extra.append(status.lower())
    if extra:
        line += " - " + ", ".join(extra)
    if caps["notes"]:
        # (grid lines and dimension strings - '8000 3 8000 4 A B C F1 F2' -, title block cells
        # and revision table rows are not notes)
        paras = [p for p in _paragraphs(doc) if not _grid_text(p["text"]) and not _TB_CELL.search(p["text"])
                 and not _REV_ROW.match(p["text"]) and _key(p["text"]) not in (seen or ())]
        notes = _condense_paras(paras, doc, caps, caps["notes"])
        text = " ".join(l for l in notes if not l.startswith("#"))
        if text:
            line += " | notes: " + _cut(text, caps["notes"])
    return line


def _grid_text(text):
    """True for a drawing's grid labels and dimension strings: 6+ tokens, under a
    fifth of them words (3+ letters)."""
    tokens = text.split()
    words = sum(1 for t in tokens if re.search(r"[A-Za-z]{3}", t))
    return len(tokens) >= 6 and words < 0.2 * len(tokens)


def _letters(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


# --------------------------------------------------------------------------
# Where a document came from

def _docs_folders(source_label):
    """The documents folders named in the source label ('X (attachments) + Y' -> [Y])."""
    out = []
    for label in (source_label or "").split(" + "):
        label = label.strip()
        if label and not label.endswith(" (attachments)"):
            out.append(label)
    return out


def _file_where(path, folders):
    """'documents folder', 'documents folder\\Superseded', or the file's own folder."""
    norm = (path or "").replace("\\", "/")
    best = None
    for folder in folders:
        f = folder.replace("\\", "/").rstrip("/")
        if f and norm.lower().startswith(f.lower() + "/") and (best is None or len(f) > len(best)):
            best = f
    sep = "\\" if "\\" in (path or "") else "/"
    if best is not None:
        rest = norm[len(best) + 1:]
        sub = rest.rsplit("/", 1)[0] if "/" in rest else ""
        return "documents folder" + (sep + sub.replace("/", sep) if sub else "")
    parent = norm.rsplit("/", 1)[0] if "/" in norm else ""
    return parent.replace("/", sep) or "file"


def _email_text(source, with_subject=True):
    day = _yymmdd(source.get("date")) or "(no date)"
    text = "%s email %s" % (day, source.get("alias") or "?")
    if with_subject:
        subject = source.get("thread") or cleaning.clean_subject(source.get("subject") or "")
        text += ' "%s"' % _cut(_squash(subject), SUBJECT_MAX)
    return text


def _file_text(source, folders):
    where = _file_where(source.get("path"), folders)
    day = _yymmdd(source.get("mtime"))
    return where + (" (modified %s)" % day if day else "")


def _from_line(entry, folders):
    bits = []
    emails, files = entry["emails"], entry["files"]
    if emails:
        text = _email_text(emails[0])
        if len(emails) > 1:
            text += " (+%s)" % _plural(len(emails) - 1, "more email")
        bits.append(text)
    if files:
        text = _file_text(files[0], folders)
        if len(files) > 1:
            text += " (+%s)" % _plural(len(files) - 1, "more copy", "more copies")
        bits.append(("also " if bits else "") + text)
    return "From: " + ("; ".join(bits) if bits else "(unknown)")


def _short_source(entry, folders):
    emails, files = entry["emails"], entry["files"]
    if emails:
        text = _email_text(emails[0], with_subject=False)
        if len(emails) > 1:
            text += " +%d more" % (len(emails) - 1)
        return text
    if files:
        return _file_where(files[0].get("path"), folders)
    return ""      # (a document inside a zip: its 'D30 > ' prefix says where it is)


def _aliases_in(entry):
    """The sender alias shown for a document (its first email), as a set."""
    return set(s.get("alias") or "?" for s in entry["emails"][:1])


# --------------------------------------------------------------------------
# People

class _People(object):
    """Sender aliases for the From: lines and the legend. Sources come with the
    email digest's aliases (sender_alias); a source without one gets an alias
    worked out the same way from its sender_name / sender_email."""

    def __init__(self, entries, project):
        self.org_map = digest.parse_org_codes((project or {}).get("org_codes", ""))
        self.names = {}       # alias -> Counter((name, email))
        sources = [s for e in entries for s in e["emails"]]
        missing = [s for s in sources if not (s.get("sender_alias") or "").strip()]
        made = self._fallback(sources, missing) if missing else {}
        for s in sources:
            alias = (s.get("sender_alias") or "").strip() or made.get(id(s)) or "?"
            s["alias"] = alias
            name = cleaning.tidy_display_name(s.get("sender_name") or "", s.get("sender_email") or "")
            self.names.setdefault(alias, Counter())[(name, (s.get("sender_email") or "").lower())] += 1

    def _fallback(self, sources, missing):
        """Aliases for the sources without one (as the email digest makes them)."""
        people = digest.People(self.org_map)
        for s in sources:
            people.learn(s.get("sender_name"), s.get("sender_email"))
        people.finish()
        taken = set((s.get("sender_alias") or "").strip().lower() for s in sources)
        idents = OrderedDict()
        for s in missing:
            ident = people.identity(s.get("sender_name"), s.get("sender_email"))
            idents.setdefault(ident, []).append(s)
        made = {}
        order = sorted(idents, key=lambda i: (-len(idents[i]), people.info(i)[1].lower() if i else "", i))
        for ident in order:
            if not ident:
                alias = "?"
            else:
                org, name, _dom = people.info(ident)
                for cand in digest._alias_candidates(name):
                    alias = org + "." + cand
                    if alias.lower() not in taken:
                        break
                taken.add(alias.lower())
            for s in idents[ident]:
                made[id(s)] = alias
        return made

    def legend(self, aliases):
        """'People (ORG.Initials, ...):' lines for the aliases shown in a part."""
        aliases = sorted(a for a in aliases if a)
        if not aliases:
            return []
        by_org = OrderedDict()
        domains = {}
        bare = False
        counts = Counter()
        for alias in aliases:
            if alias == "?":
                bare = True
                by_org.setdefault("?", [])
                continue
            org, _, initials = alias.partition(".")
            names = self.names.get(alias) or Counter()
            ranked = sorted(names.items(), key=lambda kv: (not kv[0][0], " " not in kv[0][0], -kv[1], kv[0]))
            name, email = ranked[0][0] if ranked else ("", "")
            by_org.setdefault(org, []).append((initials, name or email or "?"))
            counts[org] += sum(names.values())
            for (_n, e), _c in names.items():
                if "@" in e:
                    domains.setdefault(org, set()).add(e.split("@", 1)[1])
        order = [code for _d, code in self.org_map]
        orgs = sorted(by_org, key=lambda o: (o not in order, order.index(o) if o in order else 0,
                                             o == "?", -counts[o], o))
        lines = ["People (ORG.Initials, as in the email digest):"]
        for org in orgs:
            people = by_org[org]
            if org == "?":
                label = "? = address unknown" + (" (a bare ? = no name or address)" if bare else "")
            else:
                conf = [d for d, c in self.org_map if c == org]
                doms = conf or sorted(digest._shorten_domains(domains.get(org, set())))
                label = org + (" = " + ", ".join(doms) if doms else "")
            plist = [p for p in people if p[0]]
            if plist:
                lines.append("  " + label + ": " + ", ".join("%s=%s" % p for p in sorted(plist)))
            else:
                lines.append("  " + label)
        return lines


# --------------------------------------------------------------------------
# Documents as sections

def _entry(d, order):
    doc = d.get("doc") or {}
    sources = list(d.get("sources") or [])
    emails = [dict(s) for s in sources if (s or {}).get("kind") == "email"]     # (copies: aliases are added)
    emails.sort(key=lambda s: _email_sort_key(s.get("date")))
    files = [s for s in sources if (s or {}).get("kind") == "file"]
    files.sort(key=lambda s: ((s.get("path") or "").lower(), s.get("path") or ""))
    return {"id": (d.get("id") or "").strip(), "name": _squash(d.get("name")) or "(no name)", "doc": doc,
            "emails": emails, "files": files, "size": d.get("size"), "order": order,
            "sha1": d.get("sha1") or ""}


def _email_sort_key(value):
    dt = digest.parse_iso(value or "")
    if dt is None:
        return (1, 0.0, value or "")
    return (0, digest._utc_key(dt), value or "")


def _type_label(entry):
    doc = entry["doc"]
    kind = doc.get("kind") or "other"
    pages = doc.get("pages")
    label = _KIND_LABEL.get(kind, "file")
    if kind == "text":
        ext = entry["name"].rsplit(".", 1)[-1].lower() if "." in entry["name"] else ""
        label = {"csv": "CSV", "rtf": "RTF", "md": "Markdown"}.get(ext, "text")
    if kind == "pdf" and isinstance(pages, int) and pages:
        label += ", " + _plural(pages, "page")
    elif kind == "xlsx" and isinstance(pages, int) and pages:
        label += ", " + _plural(pages, "sheet")
    elif kind == "pptx" and isinstance(pages, int) and pages:
        label += ", " + _plural(pages, "slide")
    elif kind == "zip":
        label += ", " + _plural(_zip_count(doc), "file")
    return label


_JUNK_TITLE = re.compile(r"(?i)^(?:untitled|\(?anonymous\)?|unknown|document\d*|doc\d*|presentation\d*|"
                         r"powerpoint presentation|slide \d+|microsoft (?:word|excel|powerpoint)\b.*|title|"
                         r"book\d*|sheet\d*|workbook\d*|template|normal|no title|\W*|"
                         r".*\.(?:docx?|xlsx?|pptx?|pdf|dwg))$")


def _title_suffix(entry):
    title = _squash(entry["doc"].get("title"))
    if len(title) < 4 or _JUNK_TITLE.match(title) or not re.search(r"[A-Za-z]{3}", title):
        return ""
    t, n = _letters(title), _letters(entry["name"].rsplit(".", 1)[0])
    if not t or t in n or (n and n in t and len(t) - len(n) < 6):
        return ""
    return ' "%s"' % _cut(title, TITLE_MAX)


def _zip_count(doc):
    n = 0
    for b in doc.get("blocks") or []:
        if b.get("type") == "member":
            n += 1
        elif b.get("type") == "para":
            m = re.match(r"\(\+(\d+) more files?\)", b.get("text") or "")
            if m:
                n += int(m.group(1))
    return n


def _status_reason(doc):
    note = _squash(doc.get("note"))
    return note or _STATUS_REASON.get(doc.get("status") or "", "not read")


def _zip_folder(paths):
    """The folder ('IFC 2025-02-14/') that holds every path in a zip, or ''."""
    if not paths or any("/" not in p for p in paths):
        return ""
    first = paths[0].split("/")[:-1]
    common = []
    for n, part in enumerate(first):
        if all(p.split("/")[:-1][n:n + 1] == [part] for p in paths):
            common.append(part)
        else:
            break
    return "/".join(common) + "/" if common else ""


def _zip_parts(entry, caps, by_sha1=None, seen=None):
    """(files line, nested document sections, nested drawings) of a zip document.
    A folder that holds the whole zip is named once on the files line and left
    out of the names; a folder's drawings are counted (they are listed under
    Drawings). A member with the same bytes as a document with an ID
    (``by_sha1``: {sha1: entry}) is not condensed again: it says "same as D12".
    ``seen``: see _condense_paras."""
    doc = entry["doc"]
    members = [b for b in doc.get("blocks") or [] if b.get("type") == "member"]
    readable = [b for b in members if isinstance(b.get("doc"), dict) and b["doc"].get("status") == "ok"
                and not b["doc"].get("drawing")]
    cap = caps["chars"]
    member_cap = int(max(cap * 0.15, min(cap, 2.0 * cap / max(1, len(readable)))))
    top = _zip_folder([_squash(b.get("text")) or "?" for b in members])

    def shown(b):
        return (_squash(b.get("text")) or "?")[len(top):]

    folders = OrderedDict()
    for b in members:
        path = shown(b)
        sub = b.get("doc") if isinstance(b.get("doc"), dict) else None
        label, drawing = path, False
        if sub is not None and sub.get("status") != "ok":
            label += " (%s)" % _status_reason(sub)
        elif sub is not None and sub.get("drawing"):
            label += " (drawing, see Drawings)"
            drawing = True
        elif sub is not None:
            label += " (below)"
        folder = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
        folders.setdefault(folder, []).append((path, label, drawing))
    groups = []
    for folder, items in folders.items():
        photos = [p for p, label, _d in items if cleaning.is_camera_photo(p.rsplit("/", 1)[-1]) and label == p]
        drawn = [p for p, _label, d in items if d]
        if len(drawn) < 2:
            drawn = []
        rest = [(p, label) for p, label, _d in items if p not in drawn and (len(photos) < 2 or p not in photos)]
        if len(rest) + (1 if drawn else 0) + (1 if len(photos) >= 2 else 0) >= 2 and folder:
            # several entries in one folder: the folder once, then the bare names
            labels = [label[len(folder):] for _p, label in rest]
            lead = folder + ": "
        else:
            labels = [label for _p, label in rest]
            lead = ""
        if len(photos) >= 2:
            labels.append(("" if lead or not folder else folder + " ") +
                          digest._photo_label([p.rsplit("/", 1)[-1] for p in photos]))
        if drawn:
            labels.append("%d drawings (see Drawings)" % len(drawn))
        if len(labels) > LIST_DIR_MAX:
            labels = labels[:LIST_DIR_MAX] + ["(+%d more in %s)" % (len(labels) - LIST_DIR_MAX, folder or "the zip")]
        groups.append((lead, labels))
    several = any(lead for lead, _labels in groups)
    names = [lead + ", ".join(labels) for lead, labels in groups] if several else \
        [label for _lead, labels in groups for label in labels]
    for b in doc.get("blocks") or []:
        if b.get("type") == "para" and re.match(r"\(\+\d+ more files?\)", b.get("text") or ""):
            names.append(_squash(b["text"]))
    files_line = ""
    if names:
        files_line = "Files%s: %s" % (" (in %s)" % top if top else "", ("; " if several else ", ").join(names))
    nested, drawings = [], []
    for b in members:
        sub = b.get("doc")
        if not isinstance(sub, dict) or sub.get("status") != "ok":
            continue
        child = {"id": "", "name": shown(b), "doc": sub, "emails": [], "files": [],
                 "size": b.get("size"), "order": 0, "parent": entry,
                 "same_as": (by_sha1 or {}).get(b.get("sha1") or "")}
        if sub.get("drawing"):
            drawings.append(child)
            continue
        head = "## %s > %s (%s)%s" % (entry["id"] or entry["name"], child["name"], _type_label(child),
                                      _title_suffix(child))
        if child["same_as"] is not None:
            nested.append([head, "Same as %s." % child["same_as"]["id"]])
            continue
        lines, _paras = condense(sub, caps, member_cap, child["name"], seen)
        note = _squash(sub.get("note"))
        nested.append([head] + (["Note: " + note] if note else []) + lines)
    return files_line, nested, drawings


def _document_section(entry, caps, folders, by_sha1=None, seen=None):
    """A document's lines: '## D12 name (type)', 'From: ...', a note, then its text
    (or its changes from an earlier version). ``by_sha1``: see _zip_parts;
    ``seen``: the plain sentences shown in earlier documents (see _condense_paras)."""
    doc = entry["doc"]
    head = "## %s%s (%s)%s" % ((entry["id"] + " ") if entry["id"] else "", entry["name"],
                               _type_label(entry), _title_suffix(entry))
    lines = [head]
    if entry["emails"] or entry["files"]:
        lines.append(_from_line(entry, folders))
    note = _squash(doc.get("note"))
    if note:
        lines.append("Note: " + note)
    drawings = []
    if doc.get("kind") == "zip":
        files_line, nested, drawings = _zip_parts(entry, caps, by_sha1, seen)
        body = [files_line] if files_line else []
        for sub in nested:
            body.extend(sub)
    elif entry.get("same_as") is not None:
        other = entry["same_as"]
        body = ["Same text as %s." % (other["id"] or other["name"])]
    elif entry.get("twin") is not None:
        other = entry["twin"]
        if entry["similar"] > 0.995 and not (entry.get("cut") or other.get("cut")):
            body = ["Same text as %s (in another format)." % (other["id"] or other["name"])]
        else:
            body = ["The same document as %s in another format (%d%% of the text reads the same; differences "
                    "in layout and tables are not shown)."
                    % (other["id"] or other["name"], int(entry["similar"] * 100))]
    elif entry.get("base") is not None:
        body = _diff_lines(entry["base"], entry, caps, caps["chars"])
    else:
        body = _condense_paras(entry["paras"], doc, caps, caps["chars"], entry["name"], seen)
    if not body:
        body = ["(no text left after removing page headers, contents lists and boilerplate)"]
    return lines + body, drawings


def _other_line(entry, folders):
    doc = entry["doc"]
    bits = []
    size = _size_text(entry.get("size"))
    if size:
        bits.append(size)
    src = _short_source(entry, folders)
    if src:
        bits.append(src)
    bits.append(_status_reason(doc))
    return "%s%s (%s)" % ((entry["id"] + " ") if entry["id"] else "", entry["name"], "; ".join(bits))


def _loose_unread(entry):
    """True for a file from the documents folder of a type Squish doesn't read (not
    an attachment, and not a file that could not be read: those keep their own line)."""
    return entry["doc"].get("status") == "unsupported" and bool(entry["files"]) and not entry["emails"]


def _is_image(name):
    """True for a picture: a camera photo or an image file type (IMAGE_EXT)."""
    return name.lower().endswith(IMAGE_EXT) or cleaning.is_camera_photo(name)


def _ext_of(name):
    """'Plan.DWG' -> '.dwg'; '(no extension)' when there is none."""
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else "(no extension)"


def _natural_key(name):
    """A sort key that puts numbers in order: 'IMG_999.jpg' before 'IMG_1000.jpg'."""
    return re.sub(r"\d+", lambda m: m.group().zfill(12), name.lower()), name


def _photo_groups(names):
    """Camera photo names as labels, grouped by the name before their number
    ('150 photos IMG_1001-1150', '30 photos DSC01-30'); a photo alone keeps its name."""
    groups = OrderedDict()
    for name in sorted(names, key=_natural_key):
        m = re.match(r"(.*?)\d+(?:\s*\(\d+\))?\.[^.]+$", name)
        groups.setdefault((m.group(1) if m else name).lower(), []).append(name)
    labels = []
    for group in groups.values():
        if len(group) < 2:
            labels.extend(group)
            continue
        label = digest._photo_label(group)
        if len(label) > 60:     # numbers that do not run on: first and last name
            label = "%d photos %s to %s" % (len(group), group[0], group[-1])
        labels.append(label)
    return labels


def _group_line(where, group):
    """One Other files line for the loose files of one folder: 'documents
    folder\\Photos: 600 photos IMG_1000-1599, 60 .bak, Old report.doc (2.3 MB; not read)'.
    Camera photos are grouped as in the email digest; a type with up to
    OTHER_NAMES_MAX files is named, more are counted ('9 saved emails' for
    .msg and .eml files together)."""
    names = [e["name"] for e in group]
    photos = set(n for n in names if cleaning.is_camera_photo(n))
    if len(photos) < 2:
        photos = set()
    bits = _photo_groups(photos)
    by_ext = OrderedDict()
    for name in sorted((n for n in names if n not in photos), key=lambda n: (n.lower(), n)):
        ext = _ext_of(name)
        by_ext.setdefault("saved emails" if ext in (".msg", ".eml") else ext, []).append(name)
    for ext, members in sorted(by_ext.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(members) <= OTHER_NAMES_MAX:
            bits.extend(members)
        else:
            bits.append("%d %s" % (len(members), ext))
    size = _size_text(sum(e["size"] for e in group if isinstance(e.get("size"), int) and e["size"] >= 0))
    return "%s: %s (%snot read)" % (where, ", ".join(bits), (size + "; ") if size else "")


def _other_items(others, folders):
    """The Other files list: a line per file, except that the loose files of types
    Squish doesn't read in one folder share a line (see _group_line) when there
    are more than OTHER_GROUP_MIN of them, or 2 or more of them are images:
    job folders hold hundreds of photos, CAD and backup files."""
    groups = OrderedDict()
    for e in others:
        if _loose_unread(e):
            groups.setdefault(_file_where(e["files"][0].get("path"), folders), []).append(e)
    grouped = set(where for where, group in groups.items()
                  if len(group) > OTHER_GROUP_MIN or sum(1 for e in group if _is_image(e["name"])) >= 2)
    items, done = [], set()
    for e in others:
        where = _file_where(e["files"][0].get("path"), folders) if _loose_unread(e) else None
        if where not in grouped:
            items.append({"line": _other_line(e, folders), "aliases": _aliases_in(e),
                          "counts": Counter(other=1), "dates": _source_days(e)})
        elif where not in done:
            done.add(where)
            group = groups[where]
            items.append({"line": _group_line(where, group), "aliases": set(),
                          "counts": Counter(other=len(group)),
                          "dates": [d for g in group for d in _source_days(g)]})
    return items


# --------------------------------------------------------------------------
# Header

_HOW_TO_READ = [
    'How to read: "## D12 name (type)" starts a document. "From:" = where it came from: YY-MM-DD email SENDER',
    '  "subject" (+N more emails that also attached it), or the documents folder. The email digest marks the',
    '  same file [att: name =D12]. Long documents are condensed: headings ("# " lines), the opening and the',
    '  sentences/rows with figures, dates, references and requirements are kept in order; "\u2026" = text left',
    '  out; "|" separates table cells; "\u2022" = list item.',
]
_HOW_EXTRA = OrderedDict([
    ("rows", '  "(+N more rows)" = spreadsheet rows left out (header and total rows and the most informative rows'
             ' are kept, in order).'),
    ("diff", '  "Changes from D7:" = a later version of D7 (same file name apart from revision or date): only the'
             ' changes are shown, "+" = new or changed text, "-" = removed text, or for a changed row its old'
             ' cells ("\u2026" = cells that did not change).'),
    ("zip", '  "## D30 > name" = a document inside the zip file D30.'),
    ("drawings", '  "## Drawings" = one line per drawing: ID, name (sheets, where from) - title and revision from'
                 ' its title block.'),
    ("other", '  "## Other files" = files whose contents are not in this digest, and why.'),
])


def _markers(text):
    found = set()
    if "more rows)" in text:
        found.add("rows")
    if "\nChanges from " in text or text.startswith("Changes from "):
        found.add("diff")
    if re.search(r"(?m)^## [^\n]* > ", text):
        found.add("zip")
    return found


def _counts_text(counts):
    bits = []
    docs = counts.get("documents", 0)
    extra = []
    if counts.get("multi"):
        extra.append("%d attached to several emails" % counts["multi"])
    if counts.get("versions"):
        extra.append("%s shown as changes" % _plural(counts["versions"], "later version"))
    if docs or not (counts.get("drawings") or counts.get("other")):
        bits.append(_plural(docs, "document") + (" (%s)" % ", ".join(extra) if extra else ""))
    if counts.get("drawings"):
        bits.append(_plural(counts["drawings"], "drawing"))
    if counts.get("other"):
        bits.append(_plural(counts["other"], "other file"))
    return ", ".join(bits)


def _header_text(info, part_no, part_count, counts, span, aliases, markers, people):
    title = "SQUISH DOCUMENTS DIGEST | " + info["name"]
    if part_count > 1:
        title += " | part %d of %d" % (part_no, part_count)
    lines = [title]
    if info["first"]:
        cover = "Covers documents from %s to %s | %s" % (info["first"], info["last"], _counts_text(info["counts"]))
    else:
        cover = "Covers %s" % _counts_text(info["counts"])
    if part_count > 1:
        cover += " (this part: %s%s)" % (_counts_text(counts), ", " + span if span else "")
    lines.append(cover)
    src = ("Source: " + info["source"] + " | ") if info["source"] else ""
    lines.append(src + "squeeze: " + info["squeeze"] + " | made " + info["made"])
    if info["keywords"]:
        lines.append("Focus keywords: %s (documents that mention them, or attached to emails in threads that do)"
                     % ", ".join(info["keywords"]))
    if info["dates"]:
        lines.append(info["dates"])
    if info.get("not_read"):
        lines.append("Not included: documents in folders Squish could not open: " + "; ".join(info["not_read"]))
    lines.extend(_HOW_TO_READ)
    for key, text in _HOW_EXTRA.items():
        if key in markers:
            lines.append(text)
    lines.extend(people.legend(aliases))
    return "\n".join(lines) + "\n"


def _dates_text(project, has_files, has_emails):
    a = (project.get("date_from") or "").strip()
    b = (project.get("date_to") or "").strip()
    if not (a or b) or not has_emails:
        return ""
    span = (a + " to " + b) if a and b else ("from " + a) if a else ("up to " + b)
    text = "Dates: only documents attached to emails dated %s" % span
    if has_files:
        text += "; files in the documents folder are included whatever their date"
    return text


# --------------------------------------------------------------------------
# Parts

def _split_parts(sections, limit, info, people):
    """Pack sections into parts without splitting a document (unless one alone is
    too big: it then continues in the next part under '## D12 name (continued)').
    The Drawings and Other files lists continue line by line."""
    parts = []
    sizes = {}

    def new_part():
        parts.append({"texts": [], "aliases": set(), "markers": set(), "counts": Counter(), "dates": [],
                      "size": 0})
        return parts[-1]

    def header_size(aliases, markers):
        key = (frozenset(aliases), frozenset(markers))
        if key not in sizes:
            dummy = Counter(documents=99999, drawings=99999, other=99999, multi=99999, versions=99999)
            sizes[key] = len(_header_text(info, 99, 99, dummy, "2000-01-01 to 2000-12-31", aliases, markers,
                                          people)) + 2
        return sizes[key]

    def room_for(part, aliases, markers):
        """Characters left in a part (None = no limit)."""
        if limit is None:
            return None
        return limit - part["size"] - 2 - header_size(part["aliases"] | aliases, part["markers"] | markers)

    def fits(part, size, aliases, markers):
        room = room_for(part, aliases, markers)
        return room is None or size <= room

    def add(part, text, sec):
        part["texts"].append(text)
        part["aliases"] |= sec["aliases"]
        part["markers"] |= sec["markers"]
        part["counts"].update(sec["counts"])
        part["dates"].extend(sec["dates"])
        part["size"] += len(text) + 2

    cur = new_part()
    for sec in sections:
        if sec["kind"] == "doc":
            text = "\n".join(sec["lines"])
            if fits(cur, len(text), sec["aliases"], sec["markers"]):
                add(cur, text, sec)
                continue
            fresh = {"size": 0, "aliases": set(), "markers": set()}
            if fits(fresh, len(text), sec["aliases"], sec["markers"]):
                cur = new_part() if cur["texts"] else cur
                add(cur, text, sec)
                continue
            # one document bigger than a part: continue it line by line
            if cur["texts"] and limit - (cur["size"] + header_size(cur["aliases"], cur["markers"])) < limit * 0.25:
                cur = new_part()
            _add_lines(sec, cur, new_part, room_for, add, continued_title=sec["cont"])
            cur = parts[-1]
        else:
            cur = _add_list(sec, cur, new_part, fits, add)
    if not parts[0]["texts"] and len(parts) > 1:
        parts.pop(0)

    out = []
    n = len(parts)
    for i, p in enumerate(parts, 1):
        body = "\n\n".join(p["texts"])
        ds = sorted(p["dates"])
        first = ds[0] if ds else ""
        last = ds[-1] if ds else ""
        span = (first if first == last else first + " to " + last) if ds else ""
        head = _header_text(info, i, n, p["counts"], span, p["aliases"], p["markers"], people)
        text = head + "\n" + body + ("\n" if body else "")
        out.append({"text": text, "first_date": first, "last_date": last,
                    "documents": p["counts"].get("documents", 0) + p["counts"].get("drawings", 0),
                    "other_files": p["counts"].get("other", 0), "emails": 0, "threads": 0})
    return out


def _add_lines(sec, cur, new_part, room_for, add, continued_title):
    """Add an oversize document line by line, continuing in new parts under
    `continued_title`; a line too long for a part is cut at a sentence."""
    lines = list(sec["lines"])
    title = lines.pop(0)
    first = True
    while lines:
        head = title if first else continued_title
        take = [head]
        size = len(head)
        while lines:
            room = room_for(cur, sec["aliases"], sec["markers"])
            extra = len(lines[0]) + 1
            if room is None or size + extra <= room:
                take.append(lines.pop(0))
                size += extra
                continue
            left = room - size - 1
            if left >= 200 or (len(take) == 1 and not cur["texts"]):
                cut = _cut_point(lines[0], max(left, 100))
                take.append(lines[0][:cut].rstrip())
                size += cut + 1
                lines[0] = lines[0][cut:].lstrip()
            break
        if len(take) > 1:
            part_sec = dict(sec)
            if not first:
                part_sec = dict(sec, counts=Counter(documents=sec["counts"].get("documents", 0)))
            add(cur, "\n".join(take), part_sec)
            first = False
        if lines:
            cur = new_part()


def _cut_point(line, room):
    """Where to cut a line to fit `room` characters: after a sentence, else at a space."""
    if len(line) <= room:
        return len(line)
    window = line[:room]
    best = -1
    for m in re.finditer(r"[.!?;](?=\s)", window):
        best = m.end()
    if best < room * 0.3:
        best = window.rfind(" ")
    return best if best > room * 0.3 else room


def _add_list(sec, cur, new_part, fits, add):
    """Add a list section (Drawings, Other files) line by line; returns the current part."""
    title = sec["title"]
    started = False
    for item in sec["items"]:
        head = title if not started else ""
        text = (head + "\n" if head else "") + item["line"]
        if started:
            ok = fits(cur, len(item["line"]) + 1, item["aliases"], sec["markers"])
        else:
            ok = fits(cur, len(text), item["aliases"], sec["markers"])
        if not ok and (cur["texts"] or started):
            cur = new_part()
            title = sec["cont"]
            started = False
            text = title + "\n" + item["line"]
        item_sec = {"aliases": item["aliases"], "markers": sec["markers"], "counts": item["counts"],
                    "dates": item["dates"]}
        if started:
            cur["texts"][-1] += "\n" + item["line"]
            cur["aliases"] |= item["aliases"]
            cur["counts"].update(item["counts"])
            cur["dates"].extend(item["dates"])
            cur["size"] += len(item["line"]) + 1
        else:
            add(cur, text, item_sec)
            started = True
    return cur


def _source_days(entry):
    days = [_iso_day(s.get("date")) for s in entry["emails"]]
    days += [_iso_day(s.get("mtime")) for s in entry["files"]]
    return [d for d in days if d]


# --------------------------------------------------------------------------
# The digest

def _check_cancel(cancel):
    """Raise digest.DigestCancelled when `cancel` (a threading.Event) is set."""
    if cancel is not None and cancel.is_set():
        raise digest.DigestCancelled()


def build_documents_digest(docs, project, source_label="", now=None, cancel=None, progress=None,
                           not_read_folders=None, docs_folders=None):
    """Turn documents into documents digest parts. See DESIGN.md for the format.

    docs: [{"id": "D12", "name", "sha1", "size", "doc": DocText, "sources": [...]}]
    in ID order; a source is {"kind": "email", "date", "sender_alias",
    "sender_name", "sender_email", "subject", "thread"} or {"kind": "file",
    "path", "mtime"}. Returns {"parts": [...], "stats": {...}} like
    digest.build_digest; no parts when there is nothing worth reading.
    cancel: a threading.Event; when it is set, digest.DigestCancelled is raised.
    progress(done, total) is called as documents are condensed.
    not_read_folders: documents folders that could not be read (said in the header).
    docs_folders: the documents folders (default: taken from source_label, see
    _docs_folders, which cannot tell a ' + ' in a folder name from the separator)."""
    project = project or {}
    level_key = project.get("squeeze") or digest.DEFAULT_SQUEEZE
    if level_key not in DOC_CAPS:
        level_key = digest.DEFAULT_SQUEEZE
    caps = DOC_CAPS[level_key]
    size_key = project.get("part_size") or digest.DEFAULT_PART_SIZE
    part_limit = digest.PART_SIZES.get(size_key, digest.PART_SIZES[digest.DEFAULT_PART_SIZE])["chars"]
    now = now or datetime.now()
    folders = list(docs_folders) if docs_folders is not None else _docs_folders(source_label)
    _check_cancel(cancel)

    entries = [_entry(d, n) for n, d in enumerate(docs or []) if isinstance(d, dict)]
    people = _People(entries, project)
    documents, drawings, others = [], [], []
    for e in entries:
        doc = e["doc"]
        if doc.get("status") != "ok":
            others.append(e)
        elif doc.get("drawing"):
            drawings.append(e)
        else:
            documents.append(e)
    total = 2 * len(documents) + len(drawings) + 1
    done = [0]

    def step():
        _check_cancel(cancel)
        done[0] += 1
        if progress is not None:
            progress(done[0], total)

    if progress is not None:
        progress(0, total)
    for e in documents:
        e["paras"] = _paragraphs(e["doc"])
        step()
    _find_versions(documents, cancel)
    step()

    stats = {"documents": 0, "doc_drawings": 0, "doc_other": len(others), "doc_versions": 0,
             "doc_failed": sum(1 for e in others if e["doc"].get("status") in _UNREAD),
             "doc_multi_email": 0, "raw_chars": sum(int(e["doc"].get("chars") or 0) for e in entries),
             "output_chars": 0}

    sections = []
    zip_drawings = []
    by_sha1 = dict((e["sha1"], e) for e in documents + drawings if e["sha1"] and e["id"])
    seen = set()       # plain sentences shown so far (closing wording repeated in a series is shown once)
    for e in documents:
        step()
        lines, nested_drawings = _document_section(e, caps, folders, by_sha1, seen)
        zip_drawings.extend(nested_drawings)
        text = "\n".join(lines)
        counts = Counter(documents=1)
        if len(e["emails"]) > 1:
            counts["multi"] += 1
            stats["doc_multi_email"] += 1
        if e.get("base") is not None or e.get("same_as") is not None or e.get("twin") is not None:
            counts["versions"] += 1
            stats["doc_versions"] += 1
        stats["documents"] += 1
        cont = "## %s%s (continued)" % ((e["id"] + " ") if e["id"] else "", e["name"])
        sections.append({"kind": "doc", "lines": lines, "cont": cont, "aliases": _aliases_in(e),
                         "markers": _markers(text), "counts": counts, "dates": _source_days(e)})

    draw_items = []
    common = Counter()     # notes on several drawings (light shows notes): shown on the first only
    if caps["notes"]:
        for e in drawings + zip_drawings:
            if e.get("same_as") is None:
                common.update(set(_key(p["text"]) for p in _paragraphs(e["doc"]) if len(p["text"]) >= 20))
    common = set(k for k, n in common.items() if n >= 3)
    shown = set()
    for e in drawings + zip_drawings:
        if e.get("parent") is None:
            step()
        prefix = ""
        if e.get("parent") is not None:
            prefix = (e["parent"]["id"] or e["parent"]["name"]) + " > "
        if e.get("same_as") is not None:
            # a copy in a zip of a drawing listed with its own ID: named, not counted again
            draw_items.append({"line": "%s%s (same as %s)" % (prefix, e["name"], e["same_as"]["id"]),
                               "aliases": set(), "counts": Counter(), "dates": []})
            continue
        line = _drawing_line(e, caps, folders, prefix, seen=shown)
        shown |= set(k for k in common if k in _key(line))
        draw_items.append({"line": line, "aliases": _aliases_in(e), "counts": Counter(drawings=1),
                           "dates": _source_days(e)})
        stats["doc_drawings"] += 1
    if draw_items:
        sections.append({"kind": "list", "title": "## Drawings", "cont": "## Drawings (continued)",
                         "items": draw_items, "markers": set(["drawings"])})
    other_items = _other_items(others, folders)
    if other_items:
        sections.append({"kind": "list", "title": "## Other files", "cont": "## Other files (continued)",
                         "items": other_items, "markers": set(["other"])})

    if not documents and not draw_items:
        return {"parts": [], "stats": stats}

    all_days = sorted(d for e in entries for d in _source_days(e))
    counts = Counter(documents=stats["documents"], drawings=stats["doc_drawings"], other=stats["doc_other"],
                     multi=stats["doc_multi_email"], versions=stats["doc_versions"])
    info = {
        "name": project.get("name") or "Documents",
        "source": source_label or "",
        "squeeze": level_key,
        "made": now.strftime("%Y-%m-%d %H:%M"),
        "first": all_days[0] if all_days else "",
        "last": all_days[-1] if all_days else "",
        "counts": counts,
        "keywords": cleaning.keyword_list(project.get("focus_keywords", "")),
        "dates": _dates_text(project, any(e["files"] for e in entries), any(e["emails"] for e in entries)),
        "not_read": [" ".join(str(f).splitlines()) for f in (not_read_folders or []) if f],
    }
    parts = _split_parts(sections, part_limit, info, people)
    stats["output_chars"] = sum(len(p["text"]) for p in parts)
    return {"parts": parts, "stats": stats}
