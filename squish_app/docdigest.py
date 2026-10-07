"""Build the Squish documents digest from condensed documents (DocText dicts).

build_documents_digest(docs, project, source_label="", now=None) is pure: no
file I/O, and the same input (and `now`) always gives the same output. See
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
SIMILAR = 0.6            # a later version this similar shows only its changes
FORMAT_SIMILAR = 0.75    # a PDF and Word file with one name this similar are one document
REPEAT_SHARE = 0.3       # a page header/footer on this share of pages is dropped
REMOVED_MAX = 120        # characters shown of a removed paragraph
SUBJECT_MAX = 70         # characters of an email subject on a From: line
TITLE_MAX = 100          # characters of a document title property
LIST_DIR_MAX = 12        # zip member names listed per folder before "+N more"

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
    # units cleaning does not know: 85 \u00b5m, 200 GPa, 12 kN/m, 35 mg/L, 70 dB, 40 \u00b0C, 15 kW
    (re.compile(r"\b\d[\d,.]*\s?(?:\u00b5m|\u03bcm|um|microns?|GPa|kN/m2?|kNm|kN\.m|mg/[lL]|ppm|dB|"
                r"\u00b0C|kW|kVA|MW)(?![\w])"), 1),
    # structural member, bar, mesh, bolt and material designations: 360UB56.7, 200PFC,
    # 150x150x9.0 SHS, N16-200, 2-N20, SL92, M24, 8.8/S, 300PLUS, C350L0, Z200-19
    (re.compile(r"\b\d{2,4}\s?(?:UB|UC|PFC|WB|WC|TFB|UBP|EA|UA)(?:\s?\d{1,3}(?:\.\d)?)?\b"
                r"|\b\d{2,3}(?:\.\d)?\s?[xX\u00d7]\s?\d{2,3}(?:\.\d)?(?:\s?[xX\u00d7]\s?\d{1,2}(?:\.\d)?)?"
                r"\s?(?:SHS|RHS|CHS|EA|UA)\b"
                r"|\b(?:\d{1,2}\s?-\s?)?N[1-3]\d(?:\s?[-@]\s?\d{2,3})?\b|\bN40\b"
                r"|\b(?:SL|RL)\d{2,4}\b|\bM[1-3]\d\b|\b\d\.\d/[ST][BF]?(?![\w/])"
                r"|\b\d{3}PLUS\b|\bC\d{3}L0\b|\b[ZC]\d{3}-?\d{2}\b"), 2),
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
    of 'As per drawing'), then its figures (totals are kept anyway)."""
    filled = [c for c in _cells(text) if c]
    numbers = sum(1 for c in filled if _NUMBER_CELL.match(c))
    score = 2 * rare + 0.5 * numbers + (1 if "$" in text else 0)
    if _TOTAL_ROW.search(text):
        score += 20
    return score


# --------------------------------------------------------------------------
# Boilerplate

_PAGE_NUMBER = re.compile(
    r"(?i)^(?:-\s*)?(?:page\s*|p\.\s*)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?(?:\s*-)?$|^page\s+\d{1,4}\b.{0,80}$"
    r"|^.{0,80}\bpage\s+\d{1,4}\s*(?:of|/)\s*\d{1,4}$|^[ivxlc]{1,4}$")
_PAGE_WORDS = re.compile(r"(?i)\bpage\s+\d{1,4}\s*(?:of|/)\s*\d{1,4}\b")
_TOC_DOTS = re.compile(r"(?:\.\s?){4,}\s*\(?[0-9ivxlc]{1,5}\)?\s*$|\u2026{2,}\s*\d{1,4}\s*$|_{4,}\s*\d{1,4}\s*$",
                       re.I)
_TOC_HEAD = re.compile(r"(?i)^(?:table of )?contents?$|^list of (?:figures|tables|appendices|drawings|"
                       r"attachments|plates|photographs)$")
_TOC_LINE = re.compile(
    r"(?i)^(?:(?:section\s+)?\d{1,2}(?:\.\d{1,2}){0,4}\.?|appendix\s+[a-z0-9]{1,3}\b[:.\-]?|"
    r"annex(?:ure)?\s+[a-z0-9]{1,3}\b[:.\-]?|[a-z]\.\d{1,2}(?:\.\d{1,2})*|"
    r"(?:table|figure|plate|drawing)\s+[a-z]?\d{1,3}(?:\.\d{1,2})?[:.\-]?)?\s*\S.{0,110}?\s(\d{1,4})$")
# Wording of cover pages, copyright notices and limitation-of-liability sections.
# A strong phrase is enough on a cover page; elsewhere it takes two phrases (or three weak ones).
_BOILER_STRONG = [re.compile(p, re.I) for p in (
    r"\u00a9|\(c\)\s*(?:copyright\s*)?(?:19|20)\d\d|\(c\)\s+[\w&.,' -]{2,60}?\b(?-i:Pty|Ltd|Limited|Inc|LLC|PLC|GmbH)\b|\bcopyright\b",
    r"\ball rights reserved\b",
    r"\b(?:exclusive|sole) (?:use|benefit|reliance|purpose)\b|\bsolely for the (?:use|purpose|benefit)\b",
    r"\b(?:accepts?|assumes?|takes?|bears?|has|have) no (?:liability|responsibility|duty)\b"
    r"|\bno (?:liability|responsibility|duty of care)\b|\bliab(?:le|ility) (?:for|to) any\b",
    r"\blimitations? of liability\b|\bindemnif\w*|\bdisclaim\w*",
    r"\bwithout (?:the )?(?:prior )?(?:express )?(?:written )?(?:consent|permission|approval) of\b",
    r"\b(?:must|may|shall) not be (?:reproduced|copied|relied)\b|\breproduc\w* in (?:whole|part|full)\b",
    r"\bremains? the property of\b|\bunauthori[sz]ed (?:use|copying|reproduction)\b"
    r"|\b(?:may|shall|must|is to|are to) only be used for the purposes?\b",
    r"\btotal liability\b|\bconsequential loss\b|\blimited to the (?:amount of the )?fees?\b",
    r"\baccredited for compliance with\b|\bresults relate only to\b",
    r"\bmatter of information only\b|\bconfers no rights\b|\bdoes not amend, extend or alter\b"
    r"|\bstandard terms and conditions\b",
)]
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
_CONTACT = re.compile(r"(?i)\babn:?\s*\d|\bacn:?\s*\d|\bwww\.|\b[\w.-]+@[\w-]+\.[\w.]+|"
                      r"(?:^|\s)(?:t|p|ph|phone|tel|f|fax|m|mob|mobile)\s*[:.]\s*\+?\(?\d|\bp\.?o\.? box\b|"
                      r"\blevel \d+,")
_BOILER_HEAD = re.compile(
    r"(?i)^(?:\d{1,2}(?:\.\d{1,2})*\.?\s+|appendix\s+\w{1,3}\s*[:.\-]?\s*)?"
    r"(?:(?:statement of |report |general )?limitations?(?: of (?:this report|liability|use))?|"
    r"disclaimers?|copyright(?: notice)?|reliance|basis of (?:this )?report|confidentiality|terms of use)\s*:?$"
    r"|^(?:appendix\s+\w{1,3}\s*[:.\-]?\s*)?important (?:information|notice)\b")


def _hard_facts(text):
    """True when text has a figure, date, reference or amount (cleaning's hard facts)."""
    return any(rx.search(text) for rx, _w in cleaning._FACTS)


def _boilerplate(text, in_boiler_section, near_cover):
    """True for cover, copyright, limitation-of-liability and contact wording."""
    hard = _hard_facts(text)
    if in_boiler_section and not hard:
        return True
    strong = sum(1 for rx in _BOILER_STRONG if rx.search(text))
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
                text = _CLASSIFICATION.sub("", text).strip(" |")
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
    _mark_headers(paras)
    _mark_rarity(paras)
    _number_sections(paras)
    return paras


def _page_capped(paras, doc, caps):
    """A PDF's paragraphs up to the level's page cap, then a note of the pages
    left out."""
    limit = caps["pages"]
    if doc.get("kind") != "pdf" or not isinstance(doc.get("pages"), int) or doc["pages"] <= limit:
        return paras
    end = next((n for n, p in enumerate(paras) if (p.get("page") or 0) > limit), len(paras))
    return paras[:end] + [{"kind": "note", "text": "(pages %d-%d not shown)" % (limit + 1, doc["pages"]),
                           "level": 0, "sec": -1, "key": 0}]


# Word's page header / footer lines (docs.py adds them once, after the body)
_META = re.compile(r"^(?:Header|Footer): ")
# Classification labels taken out of those lines
_CLASSIFICATION = re.compile(r"(?i)\s*\|?\s*\b(?:commercial[- ]in[- ]confidence|strictly confidential|"
                             r"privileged and confidential|confidential|in confidence|page \d+(?: of \d+)?)\b")
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


def _drop_page_furniture(paras):
    """Drop PDF page headers/footers (the first and last lines of a page that come
    back on REPEAT_SHARE of the pages or more, page numbers changed) and page
    numbers."""
    by_page = OrderedDict()
    for n, p in enumerate(paras):
        if p["kind"] in ("text", "head", "item"):
            by_page.setdefault(p.get("page") or 0, []).append(n)
    edge = set()
    for idx in by_page.values():
        edge.update(idx[:3] + idx[-3:])
    counts = Counter()
    for idx in by_page.values():
        counts.update(set(re.sub(r"\d+", "#", _key(paras[n]["text"])) for n in idx[:3] + idx[-3:]
                          if len(paras[n]["text"]) < 200))
    pages = len(by_page)
    need = max(2, int(REPEAT_SHARE * pages + 0.999))
    out = []
    for n, p in enumerate(paras):
        if n in edge:
            text = p["text"]
            if _PAGE_NUMBER.match(text) or (pages >= 2 and len(text) < 200 and
                                            counts[re.sub(r"\d+", "#", _key(text))] >= need):
                continue
        elif p["kind"] != "row" and _PAGE_WORDS.fullmatch(p["text"]):
            continue
        out.append(p)
    return out


def _drop_contents(paras, kind, pages):
    """Drop tables of contents: lines with dot leaders, a 'Contents' heading and the
    lines after it that end in a page number, and runs of 3+ numbered lines ending
    in rising page numbers near the start of the document."""
    out = []
    n = 0
    early = max(40, len(paras) // 5)
    max_page = (pages or 0) + 20 if pages else 999
    while n < len(paras):
        p = paras[n]
        if p["kind"] in ("text", "head", "item"):
            text = p["text"]
            if _TOC_DOTS.search(text):
                n += 1
                continue
            if _TOC_HEAD.match(text.rstrip(" :")):
                end = n + 1
                while end < len(paras) and paras[end]["kind"] in ("text", "head", "item") and \
                        len(paras[end]["text"]) < 140 and \
                        (_TOC_LINE.match(paras[end]["text"]) or _TOC_DOTS.search(paras[end]["text"])):
                    end += 1
                n = end
                continue
            if n < early and kind in ("pdf", "text"):
                end = n
                last = -1
                while end < len(paras) and paras[end]["kind"] in ("text", "head", "item") and \
                        len(paras[end]["text"]) < 140:
                    m = _TOC_LINE.match(paras[end]["text"])
                    if not m or int(m.group(1)) < last or int(m.group(1)) > max_page:
                        break
                    last = int(m.group(1))
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
    document (rows only within their own table or sheet)."""
    seen = set()
    out = []
    for p in paras:
        if p["kind"] in ("text", "item", "row") and len(p["text"]) >= 30:
            k = (p["kind"] == "row" and p.get("group"), _key(p["text"]))
            if k in seen:
                continue
            seen.add(k)
        out.append(p)
    return out


_SENTENCE_END = re.compile(r"[.!?:;][\"')\]]*$")
_AMOUNT_END = re.compile(r"[$\u00a3\u20ac]\s?\d[\d,]*(?:\.\d{1,2})?\s*$")   # a priced row ends here


def _join_pdf_lines(paras):
    """Put PDF paragraphs back together where a page break or a ragged line split
    them (a long line that does not end a sentence runs on into the next
    paragraph), and gather runs of short table-cell lines into one paragraph."""
    out = []
    for p in paras:
        prev = out[-1] if out else None
        if prev is not None and prev["kind"] in ("text", "item") and p["kind"] == "text":
            ptext = prev["text"]
            if (len(ptext) >= 50 or (prev["kind"] == "item" and p["text"][:1].islower())) and \
                    not _SENTENCE_END.search(ptext) and not prev.get("cells") and \
                    not _AMOUNT_END.search(ptext):
                if ptext.endswith("-") and ptext[-2:-1].isalpha() and p["text"][:1].islower():
                    prev["text"] = ptext[:-1] + p["text"]
                else:
                    prev["text"] = ptext + " " + p["text"]
                continue
            if prev["kind"] == "item":
                out.append(dict(p))
                continue
            if prev.get("cells") is not None and len(p["text"]) < 40 and not _SENTENCE_END.search(p["text"]) \
                    and len(ptext) + len(p["text"]) < 240:
                prev["text"] = ptext + " " + p["text"]
                prev["cells"] += 1
                continue
            if len(ptext) < 40 and len(p["text"]) < 40 and not _SENTENCE_END.search(ptext) \
                    and not _SENTENCE_END.search(p["text"]):
                prev["text"] = ptext + " " + p["text"]
                prev["cells"] = 2
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


def _mark_rarity(paras):
    """Give each row (not header rows) "rare": the sum over its filled cells of
    1 / how many rows of its table have the same value in that column, so a row
    whose cells are unusual for their columns counts for more."""
    by_group = OrderedDict()
    for p in paras:
        if p["kind"] == "row" and not p.get("header"):
            by_group.setdefault(p["group"], []).append(p)
    for rows in by_group.values():
        cells = [[_key(c) for c in _cells(r["text"])] for r in rows]
        counts = Counter((col, value) for row in cells for col, value in enumerate(row) if value)
        for r, row in zip(rows, cells):
            r["rare"] = sum(1.0 / counts[(col, value)] for col, value in enumerate(row) if value)


def _number_sections(paras):
    """Give each paragraph the index of the heading it is under (sec), and its
    weight: 1 under a summary / conclusion / recommendation heading (or one of
    its sub-headings), -1 in an appendix, else 0 (see _section_weight)."""
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


_APPENDIX = re.compile(r"(?i)^(?:appendix|appendices|attachment|annex(?:ure)?|schedule)\b")


def _section_weight(heading):
    """1 for a summary / conclusion / recommendation heading, -1 for an appendix, else 0."""
    if _APPENDIX.match(heading):
        return -1
    return 1 if _KEY_SECTION.search(heading) else 0


# --------------------------------------------------------------------------
# Choosing what to keep

def _pieces(paras, size, score_rows=False):
    """Paragraphs cut into the pieces the cap chooses from: sentences of text and
    list items (long ones with facts split further, see cleaning.fact_pieces),
    whole rows and headings. Each piece: kind, text, para, first (True for the
    first piece of its paragraph), score, and for rows group/header."""
    out = []
    for n, p in enumerate(paras):
        kind = p["kind"]
        if kind in ("text", "item", "add"):
            text = p["text"]
            spans = cleaning.fact_pieces(text, size) or [(0, len(text))]
            for k, (a, b) in enumerate(spans):
                piece = text[a:b].strip()
                if not piece:
                    continue
                score = doc_fact_score(piece) + SECTION_BONUS * (p.get("key") or 0)
                out.append({"kind": kind, "text": piece, "para": n, "first": k == 0, "score": score,
                            "level": p.get("level", 0)})
        elif kind == "row":
            if score_rows:
                score = _row_score(p["text"], p.get("rare") or 0)
            else:      # a table in a report: worth less than the sentences around it
                score = min(doc_fact_score(p["text"]), ROW_SCORE_MAX) + SECTION_BONUS * (p.get("key") or 0)
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
    _damp_repeats(out)
    return out


REPEAT_SHAPES = 6   # a piece sharing its words with more pieces than this counts for less


def _shape(text):
    """A piece's words with the figures taken out, as a set: the pieces of a long
    listing that only changes its figures ('Member C-07-01 L=2.2 m N*=254 kN ...',
    inspection records) share one."""
    return frozenset(re.sub(r"\d+(?:[.,/]\d+)*", "#", text.lower()).split())


def _damp_repeats(pieces):
    """Lower the score of text that repeats one pattern with only its figures
    changed (score x sqrt(REPEAT_SHAPES / how many share it)), so a long listing
    does not crowd out the sentences around it."""
    keys = [_shape(p["text"]) if p["kind"] in ("text", "item") and len(p["text"]) >= 30 else None
            for p in pieces]
    shared = Counter(k for k in keys if k)
    for p, k in zip(pieces, keys):
        if k and shared[k] > REPEAT_SHAPES:
            p["score"] *= (float(REPEAT_SHAPES) / shared[k]) ** 0.5


def _cost(piece):
    return len(piece["text"]) + 3


def _choose(pieces, cap, row_cap=None, removed_cap=None):
    """The indexes of the pieces to keep within about `cap` characters: sheet
    names and table header rows; headings (top levels first) up to HEADING_SHARE
    of the cap; the opening up to OPENING_SHARE; then the pieces with the most
    facts (each sheet first gets its share of the room); then, in order after
    the opening, whatever else fits."""
    keep = set()
    used = [0]
    rows_used = Counter()
    removed = [0]
    seen_text = set()

    def take(i):
        """Keep piece i: True if kept (or already kept); "skip" when it is a repeat or
        over its own limit (rows, removed text); False when there is no room."""
        p = pieces[i]
        if i in keep:
            return True
        if p["kind"] == "row" and row_cap is not None and not p.get("fixed") and rows_used[p["group"]] >= row_cap:
            return "skip"
        if p["kind"] == "del" and removed_cap is not None and removed[0] >= removed_cap:
            return "skip"
        k = _key(p["text"])
        if len(k) >= 30 and p["kind"] not in _HEAD_KINDS and k in seen_text:
            return "skip"
        if used[0] + _cost(p) > cap:
            return False
        keep.add(i)
        used[0] += _cost(p)
        seen_text.add(k)
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
    heads = [i for i, p in enumerate(pieces) if p["kind"] in ("head", "slide")]
    head_room = HEADING_SHARE * cap
    head_used = 0
    for i in sorted(heads, key=lambda j: (pieces[j]["level"] or 1, j)):
        if head_used + _cost(pieces[i]) <= head_room and take(i) is True:
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

    def with_context(i):
        if take(i) is not True:
            return
        p = pieces[i]
        if p["kind"] == "row":
            for h in header_of.get(i, []):
                take(h)
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
                push("head", mark(p) + "# " + text)
            elif kind in ("note", "meta"):
                push("note", p["text"])
            elif kind == "row":
                push("row", mark(p) + p["text"])
                if p.get("group") == sheet["group"]:
                    sheet["kept"] += 1
            elif kind == "del":
                push("del", "- " + _cut(p["text"], REMOVED_MAX))
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


def condense(doc, caps, cap=None):
    """The text of one document (DocText) as lines, within about `cap` characters
    (default: the level's cap). Returns (lines, paragraphs)."""
    cap = caps["chars"] if cap is None else cap
    paras = _paragraphs(doc)
    return _condense_paras(paras, doc, caps, cap), paras


def _condense_paras(paras, doc, caps, cap):
    """Lines for a document's paragraphs within about `cap` characters."""
    paras = _page_capped(paras, doc, caps)
    sheet = (doc.get("kind") == "xlsx" or any(p["kind"] == "sheet" for p in paras))
    pieces = _pieces(paras, _body_size(caps, cap / float(caps["chars"] or 1)), score_rows=sheet)
    if not pieces:
        return []
    keep = _choose(pieces, cap, row_cap=caps["rows"] if sheet else None)
    return _render(pieces, keep)


# --------------------------------------------------------------------------
# Versions

_REV_MARKS = [
    re.compile(r"\[[a-z]{1,2}\d{0,2}\]|\[\d{1,2}\]"),                          # [C], [P1], [2]
    re.compile(r"\((?:\d{1,2}|copy|[a-z])\)"),                                 # (1), (copy)
    re.compile(r"\b(?:rev(?:ision)?|issue|version|ver|amendment|amdt)\b\.?\s*(?:no\.?\s*)?[a-z]?\d{0,3}[a-z]?\b"),
    re.compile(r"\brev[a-z]?\d{0,2}\b|\bv\d{1,3}(?:\s\d{1,3})?\b|\br\d{1,2}\b|\bp\d{1,2}\b"),
    re.compile(r"\b(?:19|20)\d\d\s?\d\d\s?\d\d\b|\b\d{1,2}\s\d{1,2}\s(?:19|20)?\d\d\b|\b\d{6}\b"
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


def _words_ratio(a, b):
    """How alike two texts are, by words (0..1)."""
    wa, wb = a.split(), b.split()
    if not wa or not wb:
        return 0.0
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


def _diff_paras(old_paras, new_paras, old_units, new_units, ops):
    """The changes as paragraphs for _pieces/_render, in the new version's order:
    the section heading each change is under (for context), added or changed
    text ("add"; rows and headings with "added") and removed text ("del";
    headings with "removed"). In a stretch of text whose words are mostly the
    same only the changed sentences are shown (see _word_changes); changed rows
    are shown whole."""
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
            if old_t and new_t and _words_ratio(" ".join(t[0] for t in old_t),
                                                " ".join(t[0] for t in new_t)) >= SIMILAR:
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
        for k in range(j1, j2):
            _key_unused, n, text = new_units[k]
            p = new_paras[n]
            if p["kind"] in _HEAD_KINDS:
                shown_heads.add(n)
                label = ("Sheet " + p["text"]) if p["kind"] == "sheet" else p["text"]
                out.append({"kind": "head", "text": label, "level": 1, "added": True})
            elif p["kind"] == "row":
                out.append({"kind": "row", "text": text, "level": 0, "added": True, "group": None,
                            "rare": p.get("rare") or 0})
            elif out and out[-1]["kind"] == "add" and out[-1].get("src") == n:
                out[-1]["text"] += " " + text
            else:
                out.append({"kind": "add", "text": text, "level": 0, "key": p.get("key"), "src": n})
        for k in range(i1, i2):
            _key_unused, n, text = old_units[k]
            p = old_paras[n]
            if p["kind"] in _HEAD_KINDS:
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
    paras = _diff_paras(base["paras"], entry["paras"], base["units"], entry["units"], entry["ops"])
    if not paras:
        return [title + " none (the text is the same)."]
    sheet = entry["doc"].get("kind") == "xlsx" or any(p["kind"] == "sheet" for p in entry["paras"])
    pieces = _pieces(paras, _body_size(caps), score_rows=sheet)
    for piece in pieces:
        src = paras[piece["para"]]
        piece["added"] = src.get("added")
        piece["removed"] = src.get("removed")
        if piece["kind"] in ("add", "row") and piece["score"] < 1:
            piece["score"] = 1
    keep = _choose(pieces, cap, removed_cap=caps["removed"])
    return [title] + _render(pieces, keep, diff=True)


def _find_versions(entries):
    """Mark later versions. A document with the same text as an earlier one gets
    entry["same_as"]. Otherwise, for documents with the same family_key (oldest
    first, see _sort_versions), a later one whose text is SIMILAR or more like the
    version before it gets entry["base"] and entry["ops"] (its changes are shown
    instead)."""
    fingerprints = {}
    for e in entries:
        e["units"] = version_units(e["paras"]) if e["doc"].get("kind") != "zip" else []
        if not e["units"]:
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
    """The date of a version: a date in its file name, else the first day it was
    sent or saved ('' if unknown)."""
    m = _VERSION_DATE.search(re.split(r"[\\/]", entry["name"] or "")[-1])
    if m:
        return "%s-%s-%s" % m.groups()
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
    r"date|project|client|job|sheet|size|rev(?:ision)?|status|drawing no|drawing number|dwg no)\b|$)")
_TB_REV_IN = re.compile(r"\b(?:REV(?:ISION)?|Rev(?:ision)?)\.?\s*(?:No\.?|NO\.?)?\s*[:\-]?\s*"
                        r"([A-Z]{1,2}\d{0,2}|P\d{1,2}|\d{1,2})\b(?![./]\d)")
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


def _drawing_line(entry, caps, folders, prefix=""):
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
    extra = []
    if title and _letters(title) not in _letters(entry["name"]):
        extra.append("title: " + title)
    if rev and not _NAME_REV.search(re.split(r"[\\/]", entry["name"])[-1]):
        extra.append("rev " + rev)       # (not repeated when the file name shows it)
    if status:
        extra.append(status.lower())
    if extra:
        line += " - " + ", ".join(extra)
    if caps["notes"]:
        # (grid lines and dimension strings - '8000 3 8000 4 A B C F1 F2' - are not notes)
        paras = [p for p in _paragraphs(doc) if not _grid_text(p["text"])]
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
            "emails": emails, "files": files, "size": d.get("size"), "order": order}


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


def _zip_parts(entry, caps):
    """(files line, nested document sections, nested drawings) of a zip document.
    A folder that holds the whole zip is named once on the files line and left
    out of the names; a folder's drawings are counted (they are listed under
    Drawings)."""
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
                 "size": b.get("size"), "order": 0, "parent": entry}
        if sub.get("drawing"):
            drawings.append(child)
            continue
        lines, _paras = condense(sub, caps, member_cap)
        head = "## %s > %s (%s)%s" % (entry["id"] or entry["name"], child["name"], _type_label(child),
                                      _title_suffix(child))
        note = _squash(sub.get("note"))
        nested.append([head] + (["Note: " + note] if note else []) + lines)
    return files_line, nested, drawings


def _document_section(entry, caps, folders):
    """A document's lines: '## D12 name (type)', 'From: ...', a note, then its text
    (or its changes from an earlier version)."""
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
        files_line, nested, drawings = _zip_parts(entry, caps)
        body = [files_line] if files_line else []
        for sub in nested:
            body.extend(sub)
    elif entry.get("same_as") is not None:
        other = entry["same_as"]
        body = ["Same text as %s." % (other["id"] or other["name"])]
    elif entry.get("twin") is not None:
        other = entry["twin"]
        if entry["similar"] > 0.995:
            body = ["Same text as %s (in another format)." % (other["id"] or other["name"])]
        else:
            body = ["The same document as %s in another format (%d%% of the text reads the same; differences "
                    "in layout and tables are not shown)."
                    % (other["id"] or other["name"], int(entry["similar"] * 100))]
    elif entry.get("base") is not None:
        body = _diff_lines(entry["base"], entry, caps, caps["chars"])
    else:
        body = _condense_paras(entry["paras"], doc, caps, caps["chars"])
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
             ' changes are shown, "+" = new or changed text, "-" = removed text.'),
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

def build_documents_digest(docs, project, source_label="", now=None):
    """Turn documents into documents digest parts. See DESIGN.md for the format.

    docs: [{"id": "D12", "name", "sha1", "size", "doc": DocText, "sources": [...]}]
    in ID order; a source is {"kind": "email", "date", "sender_alias",
    "sender_name", "sender_email", "subject", "thread"} or {"kind": "file",
    "path", "mtime"}. Returns {"parts": [...], "stats": {...}} like
    digest.build_digest; no parts when there is nothing worth reading."""
    project = project or {}
    level_key = project.get("squeeze") or digest.DEFAULT_SQUEEZE
    if level_key not in DOC_CAPS:
        level_key = digest.DEFAULT_SQUEEZE
    caps = DOC_CAPS[level_key]
    size_key = project.get("part_size") or digest.DEFAULT_PART_SIZE
    part_limit = digest.PART_SIZES.get(size_key, digest.PART_SIZES[digest.DEFAULT_PART_SIZE])["chars"]
    now = now or datetime.now()
    folders = _docs_folders(source_label)

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
    for e in documents:
        e["paras"] = _paragraphs(e["doc"])
    _find_versions(documents)

    stats = {"documents": 0, "doc_drawings": 0, "doc_other": len(others), "doc_versions": 0,
             "doc_failed": sum(1 for e in others if e["doc"].get("status") in _UNREAD),
             "doc_multi_email": 0, "raw_chars": sum(int(e["doc"].get("chars") or 0) for e in entries),
             "output_chars": 0}

    sections = []
    zip_drawings = []
    for e in documents:
        lines, nested_drawings = _document_section(e, caps, folders)
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
    for e in drawings + zip_drawings:
        prefix = ""
        if e.get("parent") is not None:
            prefix = (e["parent"]["id"] or e["parent"]["name"]) + " > "
        line = _drawing_line(e, caps, folders, prefix)
        draw_items.append({"line": line, "aliases": _aliases_in(e), "counts": Counter(drawings=1),
                           "dates": _source_days(e)})
    stats["doc_drawings"] = len(draw_items)
    if draw_items:
        sections.append({"kind": "list", "title": "## Drawings", "cont": "## Drawings (continued)",
                         "items": draw_items, "markers": set(["drawings"])})
    other_items = []
    for e in others:
        other_items.append({"line": _other_line(e, folders), "aliases": _aliases_in(e),
                            "counts": Counter(other=1), "dates": _source_days(e)})
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
    }
    parts = _split_parts(sections, part_limit, info, people)
    stats["output_chars"] = sum(len(p["text"]) for p in parts)
    return {"parts": parts, "stats": stats}
