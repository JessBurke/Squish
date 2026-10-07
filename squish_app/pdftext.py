"""Built-in PDF text reader. Standard library only.

Squish reads PDFs with the optional ``pypdf`` package when it is installed.
Locked-down work laptops often cannot install packages, so this module can
pull the text out of most PDFs on its own. It is best-effort: it aims to get
the words and lines right, not the exact page layout.

How a PDF is put together (ISO 32000, "PDF 1.7"), in the order this module
reads it:

* The file is a list of numbered *objects*: numbers, strings, names
  (``/Type``), arrays, dictionaries (``<< /Key value >>``) and *streams*
  (a dictionary followed by data, usually zlib-compressed).
* A cross-reference table (``xref``) at the end of the file, or a compressed
  "xref stream" in newer files, says where each object starts. Newer files
  also pack small objects into *object streams*. A file that was edited has
  extra sections at the end that point back to the older ones (``/Prev``).
  If the table is damaged, the file is scanned for ``N 0 obj`` headers.
* The *trailer* points to the document catalog, the catalog to the *page
  tree*. Pages inherit ``/MediaBox`` and ``/Resources`` from their parents.
* Each page has a *content stream*: a little program of drawing operators.
  Text is shown by ``Tj``, ``TJ``, ``'`` and ``"`` between ``BT`` and ``ET``,
  and placed by ``Td``, ``TD``, ``Tm`` and ``T*``. ``Do`` draws a "Form
  XObject", a reusable piece of content with its own resources.
* The bytes of a string are character *codes* in the current font. A font
  turns codes into Unicode with its ``/ToUnicode`` map, or with a named
  encoding (WinAnsi, MacRoman, Standard) adjusted by ``/Differences``, a list
  of glyph names such as ``/eacute``.

The reader works out where each piece of text lands on the page, starts a new
line when the text moves to another line, and adds a space when there is a gap
between words.

Damaged or hostile files never raise out of ``extract_pdf`` and never hang:
nesting depth, objects read, decompressed bytes, operators per page, text
characters and run time are all capped. Encrypted files are reported as
"protected" (their text is not decrypted).

Main entry point: ``extract_pdf(data=None, path=None, max_pages=300)``.
"""

import array
import binascii
import bisect
import collections
import html
import math
import re
import struct
import time
import zlib

try:
    import unicodedata
except ImportError:  # pragma: no cover - part of every normal Python install
    unicodedata = None

__all__ = ["extract_pdf", "PdfError"]

# --------------------------------------------------------------------------
# Limits. Every loop that depends on the file's contents is bounded by one of
# these, so a damaged or hostile file costs at most a few seconds.
# --------------------------------------------------------------------------

MAX_FILE_BYTES = 64 * 1024 * 1024       # bigger files are not read (Squish passes <= 40 MB)
TIME_BUDGET = 20.0                      # seconds per document; text read so far is kept
MAX_STREAM_BYTES = 64 * 1024 * 1024     # decompressed size of one stream (big CAD drawings)
MAX_TOTAL_DECODED = 200 * 1024 * 1024   # decompressed bytes per document
MAX_OBJECTS = 500000                    # objects loaded per document
MAX_NESTING = 100                       # arrays/dictionaries inside each other
MAX_ITEMS = 200000                      # items in one array or dictionary
MAX_OPS_PER_PAGE = 3000000              # content operators per page (forms included)
MAX_PAGE_CHARS = 200000                 # text characters per page
MAX_TOTAL_CHARS = 2000000               # text characters per document
MAX_FORM_DEPTH = 10                     # forms drawn inside forms
MAX_PAGES_TOTAL = 100000                # pages listed from the page tree
MAX_CMAP_ENTRIES = 300000               # entries in one font's character map
MAX_SCAN_HITS = 1000000                 # "N 0 obj" headers indexed when rebuilding
_CHUNK = 1024 * 1024                    # decompress in pieces of this size

# Layout: distances in units of the font size.
SPACE_GAP = 0.15     # a gap wider than this between two pieces of text is a space
WIDE_GAP = 1.6       # ... and wider than this (table columns) is written as two spaces
LINE_GAP = 0.6       # a sideways move bigger than this starts a new line


class PdfError(Exception):
    """The data cannot be read as a PDF (used inside this module)."""


class _OutOfBudget(Exception):
    """A time or size budget for the whole document ran out."""

    def __init__(self, reason):
        Exception.__init__(self, reason)
        self.reason = reason     # "time", "data", "objects" or "chars"


class _PageFull(Exception):
    """One page has too many operators or characters; keep what was read."""


class _Limits:
    """Budgets for one document."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.deadline = time.monotonic() + seconds
        self.decoded = 0
        self.objects = 0

    def check_time(self):
        if time.monotonic() > self.deadline:
            raise _OutOfBudget("time")

    def add_decoded(self, count):
        self.decoded += count
        if self.decoded > MAX_TOTAL_DECODED:
            raise _OutOfBudget("data")

    def add_object(self):
        self.objects += 1
        if self.objects > MAX_OBJECTS:
            raise _OutOfBudget("objects")
        if self.objects & 0x3FF == 0:
            self.check_time()


# --------------------------------------------------------------------------
# Character tables
# --------------------------------------------------------------------------

def _single_byte_table(codec):
    """256 strings: what each byte means in a Python single-byte codec."""
    table = [""] * 256
    for code in range(32, 256):
        try:
            table[code] = bytes((code,)).decode(codec)
        except UnicodeDecodeError:
            pass
    table[127] = ""
    return table


_WIN_ANSI = _single_byte_table("cp1252")
_MAC_ROMAN = _single_byte_table("mac_roman")

# Adobe StandardEncoding (the default for Type 1 fonts): ASCII with curly
# quotes, plus these characters in the upper half.
_STANDARD = [""] * 32 + [chr(c) for c in range(32, 127)] + [""] * 129
_STANDARD[0x27] = "\u2019"
_STANDARD[0x60] = "\u2018"
for _code, _char in zip(
        [0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xAB, 0xAC,
         0xAD, 0xAE, 0xAF, 0xB1, 0xB2, 0xB3, 0xB4, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA,
         0xBB, 0xBC, 0xBD, 0xBF, 0xC1, 0xC2, 0xC3, 0xC4, 0xC5, 0xC6, 0xC7, 0xC8,
         0xCA, 0xCB, 0xCD, 0xCE, 0xCF, 0xD0, 0xE1, 0xE3, 0xE8, 0xE9, 0xEA, 0xEB,
         0xF1, 0xF5, 0xF8, 0xF9, 0xFA, 0xFB],
        ["\u00a1", "\u00a2", "\u00a3", "\u2044", "\u00a5", "\u0192", "\u00a7",
         "\u00a4", "'", "\u201c", "\u00ab", "\u2039", "\u203a", "fi", "fl",
         "\u2013", "\u2020", "\u2021", "\u00b7", "\u00b6", "\u2022", "\u201a",
         "\u201e", "\u201d", "\u00bb", "\u2026", "\u2030", "\u00bf", "`",
         "\u00b4", "\u02c6", "\u02dc", "\u00af", "\u02d8", "\u02d9", "\u00a8",
         "\u02da", "\u00b8", "\u02dd", "\u02db", "\u02c7", "\u2014", "\u00c6",
         "\u00aa", "\u0141", "\u00d8", "\u0152", "\u00ba", "\u00e6", "\u0131",
         "\u0142", "\u00f8", "\u0153", "\u00df"]):
    _STANDARD[_code] = _char

# The Symbol font's own encoding (Greek letters and maths signs).
_SYMBOL = [""] * 256
for _code, _char in enumerate(
        " !\u2200#\u2203%&\u220b()\u2217+,\u2212./0123456789:;<=>?\u2245"
        "\u0391\u0392\u03a7\u0394\u0395\u03a6\u0393\u0397\u0399\u03d1\u039a"
        "\u039b\u039c\u039d\u039f\u03a0\u0398\u03a1\u03a3\u03a4\u03a5\u03c2"
        "\u03a9\u039e\u03a8\u0396[\u2234]\u22a5_\u203e"
        "\u03b1\u03b2\u03c7\u03b4\u03b5\u03c6\u03b3\u03b7\u03b9\u03d5\u03ba"
        "\u03bb\u03bc\u03bd\u03bf\u03c0\u03b8\u03c1\u03c3\u03c4\u03c5\u03d6"
        "\u03c9\u03be\u03c8\u03b6{|}\u223c", 0x20):
    _SYMBOL[_code] = _char
for _code, _char in enumerate(
        "\u20ac\u03d2\u2032\u2264\u2044\u221e\u0192\u2663\u2666\u2665\u2660"
        "\u2194\u2190\u2191\u2192\u2193"
        "\u00b0\u00b1\u2033\u2265\u00d7\u221d\u2202\u2022\u00f7\u2260\u2261"
        "\u2248\u2026|\u2014\u21b5"
        "\u2135\u2111\u211c\u2118\u2297\u2295\u2205\u2229\u222a\u2283\u2287"
        "\u2284\u2282\u2286\u2208\u2209"
        "\u2220\u2207\u00ae\u00a9\u2122\u220f\u221a\u22c5\u00ac\u2227\u2228"
        "\u21d4\u21d0\u21d1\u21d2\u21d3"
        "\u25ca\u2329\u00ae\u00a9\u2122\u2211", 0xA0):
    _SYMBOL[_code] = _char
_SYMBOL[0xF1] = "\u232a"
_SYMBOL[0xF2] = "\u222b"

# Wingdings / ZapfDingbats: symbols that matter in engineering documents
# (ticks, crosses, boxes); any other symbol is shown as a bullet.
_WINGDINGS = {0x6C: "\u25cf", 0x6E: "\u25a0", 0x6F: "\u25a1", 0x71: "\u2751",
              0x76: "\u2756", 0x77: "\u25c6", 0xA7: "\u25aa", 0xA8: "\u25fb",
              0xD8: "\u27a2", 0xE8: "\u2794", 0xFB: "\u2717", 0xFC: "\u2713",
              0xFD: "\u2612", 0xFE: "\u2611"}
_DINGBATS = {0x33: "\u2713", 0x34: "\u2714", 0x35: "\u2715", 0x36: "\u2716",
             0x37: "\u2717", 0x38: "\u2718", 0x6C: "\u25cf", 0x6E: "\u25a0",
             0x75: "\u25c6"}


def _dingbat_table(special):
    table = [""] * 256
    for code in range(33, 256):
        table[code] = special.get(code, "\u2022")
    table[32] = " "
    return table


# PDFDocEncoding (used for the document title): Latin-1 with these changes.
_PDF_DOC = [chr(c) for c in range(256)]
for _code, _char in zip(range(0x18, 0x20), "\u02d8\u02c7\u02c6\u02d9\u02dd\u02db\u02da\u02dc"):
    _PDF_DOC[_code] = _char
for _code, _char in enumerate(
        "\u2022\u2020\u2021\u2026\u2014\u2013\u0192\u2044\u2039\u203a\u2212"
        "\u2030\u201e\u201c\u201d\u2018\u2019\u201a\u2122\ufb01\ufb02\u0141"
        "\u0152\u0160\u0178\u017d\u0131\u0142\u0153\u0161\u017e\ufffd\u20ac", 0x80):
    _PDF_DOC[_code] = _char

# Glyph names (Adobe Glyph List, the common Latin part) -> text.
_GLYPHS = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#", "dollar": "$",
    "percent": "%", "ampersand": "&", "quotesingle": "'", "parenleft": "(",
    "parenright": ")", "asterisk": "*", "plus": "+", "comma": ",", "hyphen": "-",
    "period": ".", "slash": "/", "colon": ":", "semicolon": ";", "less": "<",
    "equal": "=", "greater": ">", "question": "?", "at": "@", "bracketleft": "[",
    "backslash": "\\", "bracketright": "]", "asciicircum": "^", "underscore": "_",
    "grave": "`", "braceleft": "{", "bar": "|", "braceright": "}", "asciitilde": "~",
    "quoteleft": "\u2018", "quoteright": "\u2019", "quotedblleft": "\u201c",
    "quotedblright": "\u201d", "quotesinglbase": "\u201a", "quotedblbase": "\u201e",
    "quotereversed": "\u201b", "endash": "\u2013", "emdash": "\u2014",
    "figuredash": "\u2012", "afii00208": "\u2015", "bullet": "\u2022",
    "ellipsis": "\u2026", "dagger": "\u2020", "daggerdbl": "\u2021",
    "perthousand": "\u2030", "trademark": "\u2122", "copyright": "\u00a9",
    "registered": "\u00ae", "degree": "\u00b0", "plusminus": "\u00b1",
    "multiply": "\u00d7", "divide": "\u00f7", "mu": "\u00b5", "micro": "\u00b5",
    "minus": "\u2212", "fraction": "\u2044", "onehalf": "\u00bd",
    "onequarter": "\u00bc", "threequarters": "\u00be", "onesuperior": "\u00b9",
    "twosuperior": "\u00b2", "threesuperior": "\u00b3", "ordfeminine": "\u00aa",
    "ordmasculine": "\u00ba", "section": "\u00a7", "paragraph": "\u00b6",
    "periodcentered": "\u00b7", "middot": "\u00b7", "cent": "\u00a2",
    "sterling": "\u00a3", "yen": "\u00a5", "Euro": "\u20ac", "euro": "\u20ac",
    "currency": "\u00a4", "exclamdown": "\u00a1", "questiondown": "\u00bf",
    "guillemotleft": "\u00ab", "guillemotright": "\u00bb",
    "guillemetleft": "\u00ab", "guillemetright": "\u00bb",
    "guilsinglleft": "\u2039", "guilsinglright": "\u203a", "brokenbar": "\u00a6",
    "dieresis": "\u00a8", "macron": "\u00af", "acute": "\u00b4",
    "cedilla": "\u00b8", "circumflex": "\u02c6", "tilde": "\u02dc",
    "ring": "\u02da", "caron": "\u02c7", "breve": "\u02d8", "dotaccent": "\u02d9",
    "hungarumlaut": "\u02dd", "ogonek": "\u02db", "florin": "\u0192",
    "fi": "fi", "fl": "fl", "ff": "ff", "ffi": "ffi", "ffl": "ffl",
    "nbspace": " ", "nonbreakingspace": " ", "sfthyphen": "\u00ad",
    "softhyphen": "\u00ad", "logicalnot": "\u00ac", "AE": "\u00c6", "ae": "\u00e6",
    "OE": "\u0152", "oe": "\u0153", "Oslash": "\u00d8", "oslash": "\u00f8",
    "Eth": "\u00d0", "eth": "\u00f0", "Thorn": "\u00de", "thorn": "\u00fe",
    "germandbls": "\u00df", "dotlessi": "\u0131", "Lslash": "\u0141",
    "lslash": "\u0142", "Dcroat": "\u0110", "dcroat": "\u0111",
    "lessequal": "\u2264", "greaterequal": "\u2265", "notequal": "\u2260",
    "approxequal": "\u2248", "infinity": "\u221e", "radical": "\u221a",
    "summation": "\u2211", "product": "\u220f", "integral": "\u222b",
    "partialdiff": "\u2202", "increment": "\u2206", "Ohm": "\u2126",
    "lozenge": "\u25ca", "arrowright": "\u2192", "arrowleft": "\u2190",
    "arrowup": "\u2191", "arrowdown": "\u2193", "arrowboth": "\u2194",
    "checkmark": "\u2713", "check": "\u2713", "dotmath": "\u22c5",
    "prime": "\u2032", "minute": "\u2032", "second": "\u2033",
    "numero": "\u2116", "angle": "\u2220", "therefore": "\u2234",
    "similar": "\u223c", "proportional": "\u221d", "equivalence": "\u2261",
    "degreecentigrade": "\u2103", "estimated": "\u212e", "uni00A0": " ",
    "dotlessj": "\u0237", "ring1": "\u02da", "commaaccent": ",",
    "nbhyphen": "-", "hyphentwo": "-", "openbullet": "\u25e6", "filledbox": "\u25a0",
    "H22073": "\u25a1", "H18543": "\u25aa", "H18551": "\u25ab", "H18533": "\u25cf",
}
for _i, _word in enumerate("zero one two three four five six seven eight nine".split()):
    _GLYPHS[_word] = str(_i)

_ACCENTS = {"GRAVE": "grave", "ACUTE": "acute", "CIRCUMFLEX": "circumflex",
            "TILDE": "tilde", "DIAERESIS": "dieresis", "RING ABOVE": "ring",
            "CEDILLA": "cedilla", "CARON": "caron", "BREVE": "breve",
            "MACRON": "macron", "OGONEK": "ogonek", "DOT ABOVE": "dotaccent",
            "DOUBLE ACUTE": "hungarumlaut", "COMMA BELOW": "commaaccent"}


def _add_generated_glyph_names():
    """Accented Latin letters (eacute, Scaron...) and Greek letters (alpha...)."""
    if unicodedata is None:
        return
    for code in list(range(0xC0, 0x250)) + list(range(0x1E00, 0x1F00)):
        char = chr(code)
        uname = unicodedata.name(char, "")
        m = re.match(r"LATIN (SMALL|CAPITAL) LETTER ([A-Z]) WITH (.+)$", uname)
        if m and m.group(3) in _ACCENTS:
            letter = m.group(2).lower() if m.group(1) == "SMALL" else m.group(2)
            _GLYPHS.setdefault(letter + _ACCENTS[m.group(3)], char)
    for code in range(0x391, 0x3CA):
        char = chr(code)
        m = re.match(r"GREEK (SMALL|CAPITAL) LETTER ([A-Z]+)$", unicodedata.name(char, ""))
        if m:
            word = m.group(2).lower().replace("lamda", "lambda")
            if m.group(1) == "CAPITAL":
                word = word[:1].upper() + word[1:]
            _GLYPHS.setdefault(word, char)


_add_generated_glyph_names()

_UNI_NAME_RE = re.compile(r"uni((?:[0-9A-Fa-f]{4})+)$")
_U_NAME_RE = re.compile(r"u([0-9A-Fa-f]{4,6})$")
_HEX_NAME_RE = re.compile(r"[Gg]([0-9A-Fa-f]{2})$")
_DEC_NAME_RE = re.compile(r"[Cc](\d{2,3})$")


def _glyph_text(name, depth=0):
    """Glyph name -> text, or "" when the name means nothing to us."""
    text = _GLYPHS.get(name)
    if text is not None:
        return text
    if len(name) == 1:
        return name
    if depth < 2 and "." in name:              # "a.sc", "one.oldstyle"
        return _glyph_text(name.split(".", 1)[0], depth + 1)
    if depth < 2 and "_" in name:              # "f_f_i" ligature
        parts = [_glyph_text(part, depth + 1) for part in name.split("_")]
        return "".join(parts) if all(parts) else ""
    m = _UNI_NAME_RE.match(name)
    if m:
        hexes = m.group(1)
        chars = [int(hexes[i:i + 4], 16) for i in range(0, len(hexes), 4)]
        return "".join(chr(c) for c in chars if not 0xD800 <= c <= 0xDFFF)
    m = _U_NAME_RE.match(name)
    if m:
        value = int(m.group(1), 16)
        if value <= 0x10FFFF and not 0xD800 <= value <= 0xDFFF:
            return chr(value)
        return ""
    m = _HEX_NAME_RE.match(name)
    if m:
        return _WIN_ANSI[int(m.group(1), 16)]
    m = _DEC_NAME_RE.match(name)
    if m and int(m.group(1)) < 256:
        return _WIN_ANSI[int(m.group(1))]
    return ""


# Widths (in 1/1000 em) of the standard fonts for ASCII 32-126, used when a PDF
# names a standard font without giving widths. Courier is 600 throughout.
_STANDARD_WIDTHS = {
    "helvetica": "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 556 556 556 556 556 556 556 556 556 556 278 278 584 584 584 556 1015 667 667 722 722 667 611 778 722 278 500 667 556 833 722 778 667 778 722 667 611 722 667 944 667 667 611 278 278 278 469 556 333 556 556 500 556 556 278 556 556 222 222 500 222 833 556 556 556 556 333 500 278 556 500 722 500 500 500 334 260 334 584",
    "helvetica-bold": "278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278 556 556 556 556 556 556 556 556 556 556 333 333 584 584 584 611 975 722 722 722 722 667 611 778 722 278 556 722 611 833 722 778 667 778 722 667 611 722 667 944 667 667 611 333 278 333 584 556 333 556 611 556 611 556 333 611 611 278 278 556 278 889 611 611 611 611 389 556 333 611 556 778 556 556 500 389 280 389 584",
    "times": "250 333 408 500 500 833 778 180 333 333 500 564 250 333 250 278 500 500 500 500 500 500 500 500 500 500 278 278 564 564 564 444 921 722 667 667 722 611 556 722 722 333 389 722 611 889 722 722 556 722 667 556 611 722 722 944 722 722 611 333 278 333 469 500 333 444 500 444 500 444 333 500 500 278 278 500 278 778 500 500 500 500 333 389 278 500 500 722 500 500 444 480 200 480 541",
    "times-bold": "250 333 555 500 500 1000 833 278 333 333 500 570 250 333 250 278 500 500 500 500 500 500 500 500 500 500 333 333 570 570 570 500 930 722 667 722 722 667 611 778 778 389 500 778 667 944 722 778 611 778 722 556 667 722 722 1000 722 722 667 333 278 333 581 500 333 500 556 444 556 444 333 500 556 278 333 556 278 833 556 500 556 556 444 389 333 556 500 722 500 500 444 394 220 394 520",
    "times-italic": "250 333 420 500 500 833 778 214 333 333 500 675 250 333 250 278 500 500 500 500 500 500 500 500 500 500 333 333 675 675 675 500 920 611 611 667 722 611 611 722 722 333 444 667 556 833 667 722 611 722 611 500 556 722 611 833 611 556 556 389 278 389 422 500 333 500 500 444 500 444 278 500 500 278 278 444 278 722 500 500 500 500 389 389 278 500 444 667 444 444 389 400 275 400 541",
    "times-bolditalic": "250 389 555 500 500 833 778 278 333 333 500 570 250 333 250 278 500 500 500 500 500 500 500 500 500 500 333 333 570 570 570 500 832 667 667 667 722 667 667 722 778 389 500 667 611 889 722 722 611 722 667 556 611 722 667 889 667 611 611 333 278 333 570 500 333 500 556 278 278 500 278 778 556 500 500 500 389 389 278 556 444 667 500 444 389 348 220 348 570",
}


def _standard_widths(font_name):
    """Best guess at 256 glyph widths for a font that gives none."""
    name = font_name.lower()
    if "courier" in name or "mono" in name:
        return [600.0] * 256
    bold = any(word in name for word in ("bold", "black", "heavy", "semibold"))
    italic = "italic" in name or "oblique" in name
    if "times" in name or ("serif" in name and "sans" not in name):
        key = "times" + ("-bold" if bold else "") + ("-italic" if italic and not bold else "")
        if bold and italic:
            key = "times-bolditalic"
    else:
        key = "helvetica-bold" if bold else "helvetica"
    numbers = [float(w) for w in _STANDARD_WIDTHS[key].split()]
    widths = [500.0] * 256
    widths[32:127] = numbers
    return widths


# --------------------------------------------------------------------------
# Lexer: one regular expression finds the next token.
# --------------------------------------------------------------------------

Ref = collections.namedtuple("Ref", "num gen")   # "12 0 R": a reference to object 12


class _Stream:
    """A stream object: its dictionary and where its raw bytes are in the file."""

    __slots__ = ("dict", "start", "end")

    def __init__(self, dictionary, start, end):
        self.dict = dictionary
        self.start = start
        self.end = end


_REGULAR = rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]"

_TOKEN_RE = re.compile(
    rb"[\x00\t\n\x0c\r ]*(?:%[^\r\n]*[\x00\t\n\x0c\r ]*)*"   # skip spaces and comments
    rb"(?:([+-]?(?:\d+(?:\.\d*)?|\.\d+))(?!" + _REGULAR + rb")"  # 1 number
    rb"|/(" + _REGULAR + rb"*)"                                  # 2 name
    rb"|\(([^()\\]*(?:\\.[^()\\]*)*)\)"                          # 3 string, no brackets inside
    rb"|(\()"                                                    # 4 string with brackets inside
    rb"|<([0-9A-Fa-f\x00\t\n\x0c\r ]*)>"                         # 5 hex string
    rb"|(<<|>>|[\[\]{}])"                                        # 6 brackets
    rb"|(" + _REGULAR + rb"+)"                                   # 7 keyword or operator
    rb"|(\Z)"                                                    # 8 end of data
    rb"|(.))",                                                   # 9 stray character
    re.S)

_NAME_ESCAPE_RE = re.compile(rb"#([0-9A-Fa-f]{2})")
_STRING_ESCAPE_RE = re.compile(rb"\\([0-7]{1,3}|\r\n|[\r\n]|.)|\r\n?", re.S)
_STRING_SPECIAL_RE = re.compile(rb"[()\\]")
_HEX_JUNK_RE = re.compile(rb"[^0-9A-Fa-f]")
_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}
_REF_TAIL_RE = re.compile(rb"[\x00\t\n\x0c\r ]+(\d+)[\x00\t\n\x0c\r ]+R(?!" + _REGULAR + rb")")


def _number(token):
    try:
        if b"." in token:
            return float(token)
        return int(token)
    except ValueError:          # absurdly long digit strings
        return 0


def _name(raw):
    if b"#" in raw:
        raw = _NAME_ESCAPE_RE.sub(lambda m: bytes((int(m.group(1), 16),)), raw)
    return raw.decode("latin-1")


def _escape_sub(m):
    escape = m.group(1)
    if escape is None:
        return b"\n"                         # a line break inside a string
    if escape[:1].isdigit():
        return bytes((int(escape, 8) & 0xFF,))
    if escape in (b"\r\n", b"\r", b"\n"):
        return b""                           # backslash at the end of a line: continue
    return _ESCAPES.get(escape, escape)


def _unescape(raw):
    if b"\\" not in raw and b"\r" not in raw:
        return raw
    return _STRING_ESCAPE_RE.sub(_escape_sub, raw)


def _unhex(raw):
    digits = _HEX_JUNK_RE.sub(b"", raw)
    if len(digits) % 2:
        digits += b"0"
    return binascii.unhexlify(digits)


def _read_nested_string(data, start):
    """A literal string with brackets inside it, from data[start] == "(".

    Returns (bytes, end). An unterminated string runs to the end of the data.
    """
    depth = 0
    pos = start
    while True:
        m = _STRING_SPECIAL_RE.search(data, pos)
        if m is None:
            return _unescape(data[start + 1:]), len(data)
        char = data[m.start()]
        if char == 0x5C:             # backslash: skip the escaped character
            pos = m.start() + 2
            continue
        if char == 0x28:
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return _unescape(data[start + 1:m.start()]), m.end()
        pos = m.end()


def _make_dict(items):
    """[key, value, key, value...] -> dict; keys must be names. null values are dropped."""
    result = {}
    i = 0
    count = len(items)
    while i < count - 1:
        key = items[i]
        if isinstance(key, str):
            value = items[i + 1]
            if value is not None:
                result[key] = value
            i += 2
        else:
            i += 1
    return result


def _parse_object(data, pos, limits=None):
    """Read one object at ``pos``. Returns (value, end).

    Names become ``str``, strings ``bytes``, arrays ``list``, dictionaries
    ``dict``, ``12 0 R`` a ``Ref``. Arrays and dictionaries are read with an
    explicit stack (no recursion), so deep nesting cannot crash Python. A
    keyword such as ``endobj`` ends the object early (damaged files).
    """
    stack = []
    tokens = 0
    while True:
        m = _TOKEN_RE.match(data, pos)
        pos = m.end()
        kind = m.lastindex
        tokens += 1
        if limits is not None and tokens & 0xFFFF == 0:
            limits.check_time()
        if kind == 1:
            value = _number(m.group(1))
        elif kind == 2:
            value = _name(m.group(2))
        elif kind == 3:
            value = _unescape(m.group(3))
        elif kind == 4:
            value, pos = _read_nested_string(data, m.start(4))
        elif kind == 5:
            value = _unhex(m.group(5))
        elif kind == 6:
            token = m.group(6)
            if token == b"[" or token == b"<<":
                if len(stack) >= MAX_NESTING:
                    raise PdfError("objects nested too deeply")
                stack.append(([], token == b"<<"))
                continue
            if token == b"]" or token == b">>":
                if not stack:
                    return None, pos
                items, is_dict = stack.pop()
                value = _make_dict(items) if is_dict else items
            else:
                continue                      # PostScript braces: ignore
        elif kind == 7:
            word = m.group(7)
            if word == b"R" and stack:
                items = stack[-1][0]
                if len(items) >= 2 and type(items[-1]) is int and type(items[-2]) is int:
                    gen = items.pop()
                    value = Ref(items.pop(), gen)
                else:
                    continue
            elif word == b"true":
                value = True
            elif word == b"false":
                value = False
            elif word == b"null":
                value = None
            else:
                # endobj, stream, obj...: the object ends here. Close anything
                # still open (a damaged file) and hand back what was read.
                return _close_all(stack), m.start(7)
        elif kind == 8:
            if stack:
                return _close_all(stack), pos
            raise PdfError("unexpected end of data")
        else:
            continue                          # stray character
        if not stack:
            if type(value) is int:            # "12 0 R" at the top level
                tail = _REF_TAIL_RE.match(data, pos)
                if tail:
                    return Ref(value, int(tail.group(1))), tail.end()
            return value, pos
        items = stack[-1][0]
        if len(items) < MAX_ITEMS:
            items.append(value)


def _close_all(stack):
    """Close every open array/dictionary; returns the outermost one."""
    value = None
    while stack:
        items, is_dict = stack.pop()
        if value is not None and len(items) < MAX_ITEMS:
            items.append(value)
        value = _make_dict(items) if is_dict else items
    return value


# Drawing operators that never affect text: path construction and painting,
# colours, line styles, clipping. CAD drawings have millions of them, so long
# runs are skipped in one go (see _skip_drawing).
_DRAWING_OPS = frozenset(b"m l c v y h re S s f F f* B B* b b* n W W* w J j M d i ri "
                         b"g G rg RG k K".split())
# Every character those operators and their numbers are made of. The only other
# operators spelled with these characters are cm and gs (and d0/d1), which
# _skip_drawing looks for separately.
_DRAWING_CHARS_RE = re.compile(rb"[0-9.+\-\x00\t\n\x0c\r mlcvyhreSsfFBb*nWwJjMdigGkK]+")
_OP_TOKEN_RE = re.compile(rb"(?<![^\x00\t\n\x0c\r ])[A-Za-z*]+(?=[\x00\t\n\x0c\r ])")
_DRAWING_WINDOW = 8 * 1024 * 1024
_STATE_WORDS = (b"cm", b"gs", b"d0", b"d1")
_WHITE = frozenset(b"\x00\t\n\x0c\r ")


def _skip_drawing(data, pos):
    """End of a long run of drawing operators starting at ``pos`` (``pos`` if there is none).

    The run ends just after an operator, so the operands of whatever comes
    next are left in place.
    """
    m = _DRAWING_CHARS_RE.match(data, pos, pos + _DRAWING_WINDOW)
    end = m.end() if m else pos
    if end - pos < 4096:
        return pos
    for word in _STATE_WORDS:                 # stop before cm/gs: they matter for text
        k = data.find(word, pos, end)
        while k >= 0:
            if k > 0 and data[k - 1] in _WHITE and (k + 2 >= len(data) or data[k + 2] in _WHITE):
                end = k
                break
            k = data.find(word, k + 2, end)
    last = None
    for token in _OP_TOKEN_RE.finditer(data, max(pos, end - 512), end):
        last = token.end()
    return last if last is not None and last - pos >= 4096 else pos


def _content_ops(data, limits=None):
    """Yield (operator, operands) for each operator in a content stream.

    The operator is ``bytes`` (b"Tj"); operands are values as returned by
    ``_parse_object``. Inline images (BI ... ID data EI) are skipped, and so
    are long runs of drawing operators (see _DRAWING_OPS), which are not
    yielded at all.
    """
    operands = []
    stack = []
    deep = 0                 # brackets opened beyond MAX_NESTING
    pos = 0
    count = 0
    drawing = 0              # drawing operators in a row
    while True:
        jump = None
        for m in _TOKEN_RE.finditer(data, pos):
            kind = m.lastindex
            if kind == 1:
                value = _number(m.group(1))
            elif kind == 7:
                word = m.group(7)
                if word == b"true" or word == b"false":
                    value = word == b"true"
                elif word == b"null":
                    value = None
                else:
                    if stack:               # an operator inside an unclosed array
                        value = _close_all(stack)
                        deep = 0
                        if value is not None:
                            operands.append(value)
                    if word == b"BI":
                        jump = _skip_inline_image(data, m.end())
                        operands = []
                        break
                    count += 1
                    if count & 0x3FFF == 0 and limits is not None:
                        limits.check_time()
                    yield word, operands
                    operands = []
                    if word not in _DRAWING_OPS:
                        drawing = 0
                        continue
                    drawing += 1
                    if drawing < 32:
                        continue
                    drawing = 0
                    skip = _skip_drawing(data, m.end())
                    if skip == m.end():
                        continue
                    jump = skip
                    break
            elif kind == 2:
                value = _name(m.group(2))
            elif kind == 3:
                value = _unescape(m.group(3))
            elif kind == 5:
                value = _unhex(m.group(5))
            elif kind == 4:
                value, jump = _read_nested_string(data, m.start(4))
            elif kind == 6:
                token = m.group(6)
                if token == b"[" or token == b"<<":
                    if len(stack) < MAX_NESTING:
                        stack.append(([], token == b"<<"))
                    else:
                        deep += 1
                    continue
                if token == b"]" or token == b">>":
                    if deep:
                        deep -= 1
                        continue
                    if not stack:
                        continue
                    items, is_dict = stack.pop()
                    value = _make_dict(items) if is_dict else items
                else:
                    continue
            elif kind == 8:
                return
            else:
                continue
            if stack:
                items = stack[-1][0]
                if len(items) < MAX_ITEMS:
                    items.append(value)
            else:
                operands.append(value)
                if len(operands) > 1000:          # junk without operators
                    del operands[:-100]
            if jump is not None:
                break
        else:
            return
        if jump is None:
            return
        pos = jump


_INLINE_ID_RE = re.compile(rb"(?<![^\x00\t\n\x0c\r \]>)])ID[\x00\t\n\x0c\r ]")
_INLINE_EI_RE = re.compile(rb"[\x00\t\n\x0c\r ]EI(?![^\x00\t\n\x0c\r ])")


def _skip_inline_image(data, pos):
    """Position just after the EI that ends an inline image starting after BI."""
    m = _INLINE_ID_RE.search(data, pos)
    if m is None:
        return len(data)
    end = _INLINE_EI_RE.search(data, m.end())
    return len(data) if end is None else end.end()


# --------------------------------------------------------------------------
# Stream filters
# --------------------------------------------------------------------------

_IMAGE_FILTERS = {"DCTDecode", "DCT", "JPXDecode", "CCITTFaxDecode", "CCF", "JBIG2Decode"}


def _inflate(raw, limits, cap):
    """zlib data -> bytes (at most ``cap``). Damaged data gives what could be read."""
    for wbits in (15, -15):              # zlib header first, then raw deflate
        out = []
        total = 0
        decomp = zlib.decompressobj(wbits)
        pending = raw
        try:
            while True:
                chunk = decomp.decompress(pending, _CHUNK)
                if chunk:
                    out.append(chunk)
                    total += len(chunk)
                    limits.add_decoded(len(chunk))
                    if total >= cap:
                        break
                pending = decomp.unconsumed_tail
                if decomp.eof or (not pending and len(chunk) < _CHUNK):
                    break
                if not chunk and not pending:
                    break
        except zlib.error:
            if total == 0 and wbits == 15:
                continue
        return b"".join(out)[:cap]
    return b""


def _unpredict(data, parms, limits):
    """Undo a PNG or TIFF predictor (used by xref streams and some images)."""
    try:
        predictor = int(parms.get("Predictor", 1))
        colors = int(parms.get("Colors", 1))
        bits = int(parms.get("BitsPerComponent", 8))
        columns = int(parms.get("Columns", 1))
    except (TypeError, ValueError):
        return data
    if predictor < 2:
        return data
    if not (1 <= colors <= 32 and bits in (1, 2, 4, 8, 16) and 1 <= columns <= 1000000):
        return data
    bpp = max(1, (colors * bits + 7) // 8)
    row_len = (colors * bits * columns + 7) // 8
    out = bytearray()
    if predictor == 2:                       # TIFF: only the common 8-bit case
        if bits != 8:
            return data
        for start in range(0, len(data), row_len):
            row = bytearray(data[start:start + row_len])
            for i in range(bpp, len(row)):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
            out += row
        return bytes(out)
    prev = bytearray(row_len)
    rows = 0
    for start in range(0, len(data) - row_len, row_len + 1):
        kind = data[start]
        row = bytearray(data[start + 1:start + 1 + row_len])
        if kind == 1:
            for i in range(bpp, row_len):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif kind == 2:
            row = bytearray((a + b) & 0xFF for a, b in zip(row, prev))
        elif kind == 3:
            for i in range(row_len):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(row_len):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                if pa <= pb and pa <= pc:
                    near = a
                elif pb <= pc:
                    near = b
                else:
                    near = c
                row[i] = (row[i] + near) & 0xFF
        out += row
        prev = row
        rows += 1
        if rows & 0xFFF == 0:
            limits.check_time()
    return bytes(out)


def _ascii_hex(raw):
    end = raw.find(b">")
    if end >= 0:
        raw = raw[:end]
    return _unhex(raw)


_A85_DIGITS = bytes.maketrans(bytes(range(33, 118)), bytes(range(85)))


def _ascii85(raw, cap):
    """ASCII85 data -> bytes, at most ``cap`` (faster than base64.a85decode on big streams)."""
    raw = raw.strip()
    if raw.startswith(b"<~"):
        raw = raw[2:]
    end = raw.find(b"~>")
    if end >= 0:
        raw = raw[:end]
    cleaned = re.sub(rb"[^!-uz]", b"", raw)
    out = []
    total = 0
    carry = b""
    step = 5 * 65536
    try:
        for start in range(0, len(cleaned), step):
            # "z" stands for four zero bytes ("!!!!!"); expand a piece at a time.
            piece = carry + cleaned[start:start + step].replace(b"z", b"!!!!!")
            full = len(piece) // 5 * 5
            digits = piece[:full].translate(_A85_DIGITS)
            carry = piece[full:]
            words = [(((a * 85 + b) * 85 + c) * 85 + d) * 85 + e
                     for a, b, c, d, e in zip(digits[0::5], digits[1::5], digits[2::5], digits[3::5], digits[4::5])]
            out.append(struct.pack(">%dI" % len(words), *words))
            total += 4 * len(words)
            if total >= cap:
                return b"".join(out)[:cap]
        if len(carry) >= 2:                   # a last, short group
            d = (carry + b"uuuu")[:5].translate(_A85_DIGITS)
            value = (((d[0] * 85 + d[1]) * 85 + d[2]) * 85 + d[3]) * 85 + d[4]
            out.append(struct.pack(">I", value)[:len(carry) - 1])
    except struct.error:                      # a group bigger than 2**32: damaged data
        pass
    return b"".join(out)


def _lzw(data, early_change, limits, cap):
    """LZW decompression (old PDFs from the 1990s)."""
    out = bytearray()
    table = [bytes((i,)) for i in range(256)] + [b"", b""]
    width = 9
    prev = None
    bitbuf = 0
    nbits = 0
    for index, byte in enumerate(data):
        bitbuf = ((bitbuf << 8) | byte) & 0xFFFFFF
        nbits += 8
        while nbits >= width:
            nbits -= width
            code = (bitbuf >> nbits) & ((1 << width) - 1)
            if code == 256:
                table = table[:258]
                width = 9
                prev = None
                continue
            if code == 257:
                return bytes(out)
            if code < len(table):
                entry = table[code]
            elif code == len(table) and prev is not None:
                entry = prev + prev[:1]
            else:
                return bytes(out)
            out += entry
            if prev is not None and len(table) < 4096:
                table.append(prev + entry[:1])
            prev = entry
            size = len(table) + early_change
            width = 9 if size < 512 else 10 if size < 1024 else 11 if size < 2048 else 12
            if len(out) >= cap:
                return bytes(out[:cap])
        if index & 0xFFFF == 0xFFFF:
            limits.check_time()
    return bytes(out)


def _run_length(data, limits, cap):
    out = bytearray()
    i = 0
    count = len(data)
    while i < count:
        length = data[i]
        i += 1
        if length == 128:
            break
        if length < 128:
            out += data[i:i + length + 1]
            i += length + 1
        else:
            if i < count:
                out += bytes((data[i],)) * (257 - length)
            i += 1
        if len(out) >= cap:
            return bytes(out[:cap])
        if i & 0xFFFF == 0:
            limits.check_time()
    return bytes(out)


# --------------------------------------------------------------------------
# Document: cross-reference table, objects, page tree
# --------------------------------------------------------------------------

_MISSING = object()

_STARTXREF_RE = re.compile(rb"startxref[\x00\t\n\x0c\r ]*(\d+)")
_XREF_KW_RE = re.compile(rb"[\x00\t\n\x0c\r ]*xref(?!" + _REGULAR + rb")")
_SUBSECTION_RE = re.compile(
    rb"[\x00\t\n\x0c\r ]*(\d+)[\x00\t\n\x0c\r ]+(\d+)(?!\d)(?![\x00\t\n\x0c\r ]*[fn])")
_ENTRY_RE = re.compile(rb"[\x00\t\n\x0c\r ]*(\d+)[\x00\t\n\x0c\r ]+(\d+)[\x00\t\n\x0c\r ]+([fn])")
_TRAILER_KW_RE = re.compile(rb"[\x00\t\n\x0c\r ]*trailer")
_OBJ_HEADER_RE = re.compile(
    rb"[\x00\t\n\x0c\r ]*(\d+)[\x00\t\n\x0c\r ]+(\d+)[\x00\t\n\x0c\r ]+obj(?!" + _REGULAR + rb")")
_OBJ_SCAN_RE = re.compile(
    rb"(?<![0-9])(\d{1,10})[\x00\t\n\x0c\r ]+(\d{1,5})[\x00\t\n\x0c\r ]+obj(?!" + _REGULAR + rb")")
_STREAM_KW_RE = re.compile(rb"[\x00\t\n\x0c\r ]*stream(?:\r\n|\n|\r)?")
_ENDSTREAM_RE = re.compile(rb"[\x00\t\n\x0c\r ]*endstream")
_TRAILER_SCAN_RE = re.compile(rb"trailer[\x00\t\n\x0c\r ]*(?=<<)")
_XREF_TYPE_SCAN_RE = re.compile(rb"/Type[\x00\t\n\x0c\r ]*/XRef(?!" + _REGULAR + rb")")
_OBJSTM_SCAN_RE = re.compile(rb"/Type[\x00\t\n\x0c\r ]*/ObjStm(?!" + _REGULAR + rb")")
_CATALOG_SCAN_RE = re.compile(rb"/Type[\x00\t\n\x0c\r ]*/Catalog(?!" + _REGULAR + rb")")
_INFO_SCAN_RE = re.compile(rb"/(?:Producer|Creator|CreationDate|Author|Title)(?!" + _REGULAR + rb")")
_PAGE_SCAN_RE = re.compile(rb"/Type[\x00\t\n\x0c\r ]*/Page(?!" + _REGULAR + rb")")

_INHERITED = ("Resources", "MediaBox", "CropBox", "Rotate")


class _Document:
    """Finds and reads the objects of one PDF file."""

    def __init__(self, data, limits):
        self.data = data
        self.limits = limits
        self.xref = {}           # object number -> (1, offset, gen) | (2, stream number, index) | (0, 0, 0) free
        self.trailer = {}
        self.damaged = False     # the cross-reference table could not be used as it is
        self._cache = {}
        self._loading = set()
        self._scan = None        # object number -> offset, from scanning the file
        self._header_pos = None  # offsets of every "N 0 obj" found by scanning (in order)
        self._header_num = None  # ... and their object numbers
        self._objstm = {}        # object stream number -> {object number: value}
        self._compressed = None  # object number -> object stream number, from scanning
        head = data.find(b"%PDF-", 0, 1024)
        self.shift = head if head > 0 else 0   # junk before the header moves every offset

    # ---- opening ----

    def open(self):
        """Read the cross-reference sections and the trailer (rebuilding them if damaged)."""
        try:
            self._read_xref_chain()
        except _OutOfBudget:
            raise
        except Exception:
            self.damaged = True
        if not self._good_root(self.trailer.get("Root")):
            self.damaged = True
            self._rebuild_trailer()

    @property
    def catalog(self):
        root = self.resolve(self.trailer.get("Root"))
        return root if isinstance(root, dict) else {}

    @property
    def encrypted(self):
        return self.trailer.get("Encrypt") is not None

    def _good_root(self, value):
        root = self.resolve(value)
        return isinstance(root, dict) and "Pages" in root

    def _startxref(self):
        data = self.data
        i = data.rfind(b"startxref", max(0, len(data) - 4096))
        if i < 0:
            i = data.rfind(b"startxref")
        if i < 0:
            return None
        m = _STARTXREF_RE.match(data, i)
        return int(m.group(1)) if m else None

    def _read_xref_chain(self):
        offset = self._startxref()
        if offset is None:
            raise PdfError("no startxref")
        seen = set()
        while offset is not None and offset not in seen and len(seen) < 1000:
            seen.add(offset)
            trailer = self._read_xref_section(offset)
            for key, value in trailer.items():
                self.trailer.setdefault(key, value)
            hybrid = trailer.get("XRefStm")      # "hybrid" files: table + xref stream
            if type(hybrid) is int and hybrid not in seen:
                seen.add(hybrid)
                try:
                    self._read_xref_section(hybrid, hybrid=True)
                except PdfError:
                    self.damaged = True
            prev = trailer.get("Prev")
            offset = prev if type(prev) is int else None

    def _read_xref_section(self, offset, hybrid=False):
        data = self.data
        for start in (offset, offset + self.shift):
            if 0 <= start < len(data):
                m = _XREF_KW_RE.match(data, start)
                if m:
                    return self._read_xref_table(m.end())
                if _OBJ_HEADER_RE.match(data, start):
                    return self._read_xref_stream(start, hybrid)
        raise PdfError("cross-reference section not found")

    def _read_xref_table(self, pos):
        data = self.data
        xref = self.xref
        entries = 0
        while True:
            m = _TRAILER_KW_RE.match(data, pos)
            if m:
                trailer, _ = _parse_object(data, m.end(), self.limits)
                return trailer if isinstance(trailer, dict) else {}
            m = _SUBSECTION_RE.match(data, pos)
            if m is None:
                # Damaged table: keep what was read and look for the trailer.
                self.damaged = True
                i = data.find(b"trailer", pos)
                if i < 0:
                    raise PdfError("damaged cross-reference table")
                trailer, _ = _parse_object(data, i + 7, self.limits)
                return trailer if isinstance(trailer, dict) else {}
            first = int(m.group(1))
            count = int(m.group(2))
            pos = m.end()
            for i in range(count):
                e = _ENTRY_RE.match(data, pos)
                if e is None:
                    break
                pos = e.end()
                num = first + i
                if num not in xref:
                    if e.group(3) == b"n":
                        xref[num] = (1, int(e.group(1)), int(e.group(2)))
                    else:
                        xref[num] = (0, 0, 0)
                entries += 1
                if entries & 0xFFFF == 0:
                    self.limits.check_time()

    def _read_xref_stream(self, offset, hybrid=False):
        """Read a cross-reference stream. In a hybrid file (``hybrid``) its
        entries replace the "free" entries the table gives for objects that
        live in object streams."""
        stream = self._load_at(offset, None)
        if not isinstance(stream, _Stream):
            raise PdfError("bad cross-reference stream")
        info = stream.dict
        raw = self.decode_stream(stream) or b""
        widths = self.resolve(info.get("W"))
        if not isinstance(widths, list) or len(widths) < 3:
            raise PdfError("bad cross-reference stream")
        w0, w1, w2 = [w if type(w) is int and 0 <= w <= 8 else 0 for w in widths[:3]]
        row_len = w0 + w1 + w2
        size = info.get("Size")
        index = self.resolve(info.get("Index"))
        if not isinstance(index, list):
            index = [0, size if type(size) is int else 0]
        if row_len == 0:
            return info
        xref = self.xref
        pos = 0
        for k in range(0, len(index) - 1, 2):
            first, count = index[k], index[k + 1]
            if type(first) is not int or type(count) is not int:
                break
            for i in range(count):
                if pos + row_len > len(raw):
                    break
                row = raw[pos:pos + row_len]
                pos += row_len
                kind = int.from_bytes(row[:w0], "big") if w0 else 1
                field2 = int.from_bytes(row[w0:w0 + w1], "big")
                field3 = int.from_bytes(row[w0 + w1:], "big") if w2 else 0
                num = first + i
                if num not in xref or (hybrid and xref[num][0] == 0):
                    if kind == 1:
                        xref[num] = (1, field2, field3)
                    elif kind == 2:
                        xref[num] = (2, field2, field3)
                    elif kind == 0 and num not in xref:
                        xref[num] = (0, 0, 0)
                if i & 0xFFFF == 0xFFFF:
                    self.limits.check_time()
        return info

    # ---- objects ----

    def get(self, num):
        """The object with this number (None if it is missing or unreadable)."""
        try:
            return self._cache[num]
        except KeyError:
            pass
        if num in self._loading:
            return None                      # an object that refers to itself
        self._loading.add(num)
        try:
            self.limits.add_object()
            value = _MISSING
            entry = self.xref.get(num)
            if entry is not None:
                if entry[0] == 1:
                    value = self._load_at(entry[1], num)
                    if value is _MISSING and self.shift:
                        value = self._load_at(entry[1] + self.shift, num)
                elif entry[0] == 2:
                    value = self._objstm_objects(entry[1]).get(num, _MISSING)
                else:
                    value = None             # deleted object
            if value is _MISSING:
                offset = self._scan_index().get(num)
                if offset is not None:
                    value = self._load_at(offset, num)
                    if value is not _MISSING and entry is not None:
                        self.damaged = True      # the table pointed to the wrong place
            if value is _MISSING:
                stm = self._compressed_index().get(num)
                if stm is not None:
                    value = self._objstm_objects(stm).get(num, _MISSING)
            if value is _MISSING:
                value = None
        finally:
            self._loading.discard(num)
        self._cache[num] = value
        return value

    def resolve(self, value):
        """Follow references until a real value is reached."""
        hops = 0
        while isinstance(value, Ref):
            value = self.get(value.num)
            hops += 1
            if hops > 32:
                return None
        return value

    def _load_at(self, offset, num):
        """The object whose header ("12 0 obj") is at offset; _MISSING if it is not there."""
        data = self.data
        m = _OBJ_HEADER_RE.match(data, offset)
        if m is None or (num is not None and int(m.group(1)) != num):
            return _MISSING
        try:
            value, pos = _parse_object(data, m.end(), self.limits)
        except PdfError:
            return _MISSING
        if isinstance(value, dict):
            s = _STREAM_KW_RE.match(data, pos)
            if s:
                value = self._make_stream(value, s.end())
        return value

    def _make_stream(self, info, start):
        data = self.data
        length = self.resolve(info.get("Length"))
        end = None
        if type(length) is int and 0 <= length <= len(data) - start:
            if _ENDSTREAM_RE.match(data, start + length):
                end = start + length
        if end is None:                      # wrong /Length: look for "endstream"
            i = data.find(b"endstream", start)
            if i < 0:
                end = len(data)
            else:
                end = i
                if data[end - 1:end] == b"\n":
                    end -= 1
                if data[end - 1:end] == b"\r":
                    end -= 1
        return _Stream(info, start, max(start, end))

    def _objstm_objects(self, stm_num):
        """Objects packed in an object stream: {object number: value}."""
        objects = self._objstm.get(stm_num)
        if objects is not None:
            return objects
        self._objstm[stm_num] = {}           # guards against loops
        stream = self.get(stm_num)
        if not isinstance(stream, _Stream):
            return {}
        count = self.resolve(stream.dict.get("N"))
        first = self.resolve(stream.dict.get("First"))
        if type(count) is not int or type(first) is not int or first < 0:
            return {}
        raw = self.decode_stream(stream) or b""
        numbers = [int(n) for n in re.findall(rb"\d+", raw[:first])[:2 * min(count, MAX_ITEMS)]]
        objects = {}
        for k in range(0, len(numbers) - 1, 2):
            objnum, offset = numbers[k], numbers[k + 1]
            if objnum in objects:
                continue
            self.limits.add_object()
            try:
                value, _ = _parse_object(raw, first + offset, self.limits)
            except PdfError:
                continue
            objects[objnum] = value
        self._objstm[stm_num] = objects
        return objects

    def _scan_index(self):
        """{object number: offset} found by scanning the whole file (built once)."""
        if self._scan is None:
            index = {}
            self._header_pos = array.array("q")    # every header found, in file order
            self._header_num = array.array("q")
            for hit, m in enumerate(_OBJ_SCAN_RE.finditer(self.data)):
                num = int(m.group(1))
                index[num] = m.start(1)          # a later copy (an edit) wins
                self._header_pos.append(m.start(1))
                self._header_num.append(num)
                if hit >= MAX_SCAN_HITS:
                    break
                if hit & 0xFFFF == 0xFFFF:
                    self.limits.check_time()
            self._scan = index
        return self._scan

    def _enclosing_object(self, pos):
        """Number of the object whose header comes last before ``pos``."""
        self._scan_index()
        i = bisect.bisect_right(self._header_pos, pos) - 1
        return self._header_num[i] if i >= 0 else None

    def _objects_containing(self, pattern, limit):
        """Numbers of the objects (found by scanning) whose text matches ``pattern``, in file order."""
        numbers = []
        seen = set()
        for hit, m in enumerate(pattern.finditer(self.data)):
            num = self._enclosing_object(m.start())
            if num is not None and num not in seen:
                seen.add(num)
                numbers.append(num)
            if hit > limit:
                break
        return numbers

    def _compressed_index(self):
        """{object number: object stream number}, by scanning (built once)."""
        if self._compressed is None:
            self._compressed = {}
            for stm in self._objects_containing(_OBJSTM_SCAN_RE, 20000):
                for num in self._objstm_objects(stm):
                    self._compressed.setdefault(num, stm)
        return self._compressed

    def _rebuild_trailer(self):
        """Find the catalog (and Info/Encrypt) without a usable cross-reference table."""
        data = self.data
        found = {}
        positions = [m.end() for m in _TRAILER_SCAN_RE.finditer(data)][-50:]
        for pos in reversed(positions):          # newest first
            try:
                info, _ = _parse_object(data, pos, self.limits)
            except PdfError:
                continue
            if isinstance(info, dict):
                for key in ("Root", "Info", "Encrypt"):
                    if key in info and not (key == "Root" and not self._good_root(info[key])):
                        found.setdefault(key, info[key])
        hits = [m.start() for m in _XREF_TYPE_SCAN_RE.finditer(data)][-50:]
        for pos in reversed(hits):
            num = self._enclosing_object(pos)
            stream = self.get(num) if num is not None else None
            if isinstance(stream, _Stream):
                for key in ("Root", "Info", "Encrypt"):
                    if key in stream.dict and not (key == "Root" and not self._good_root(stream.dict[key])):
                        found.setdefault(key, stream.dict[key])
        if "Root" not in found:
            hits = [m.start() for m in _CATALOG_SCAN_RE.finditer(data)][-50:]
            for pos in reversed(hits):
                num = self._enclosing_object(pos)
                if num is not None and self._good_root(Ref(num, 0)):
                    found["Root"] = Ref(num, 0)
                    break
        if "Info" not in found:                  # no trailer: look for the document info
            hits = [m.start() for m in _INFO_SCAN_RE.finditer(data)][-20:]
            for pos in reversed(hits):
                num = self._enclosing_object(pos)
                info = self.get(num) if num is not None else None
                if isinstance(info, dict) and "Type" not in info and "Title" in info:
                    found["Info"] = Ref(num, 0)
                    break
            else:
                for num in sorted(self._compressed_index()):
                    info = self.get(num)
                    if isinstance(info, dict) and "Type" not in info and "Title" in info:
                        found["Info"] = Ref(num, 0)
                        break
        if "Root" not in found:
            for num in sorted(self._compressed_index()):
                value = self.get(num)
                if isinstance(value, dict) and value.get("Type") == "Catalog" and "Pages" in value:
                    found["Root"] = Ref(num, 0)
                    break
        self.trailer.update(found)

    # ---- streams ----

    def decode_stream(self, stream, cap=MAX_STREAM_BYTES):
        """Decoded bytes of a stream; None for images and unknown filters."""
        data = self.data[stream.start:stream.end]
        filters = self.resolve(stream.dict.get("Filter"))
        parms = self.resolve(stream.dict.get("DecodeParms"))
        if isinstance(filters, str):
            filters = [filters]
            parms = [parms]
        if not isinstance(filters, list):
            return data[:cap]
        if not isinstance(parms, list):
            parms = [parms] * len(filters)
        for i, name in enumerate(filters):
            name = self.resolve(name)
            parm = self.resolve(parms[i]) if i < len(parms) else None
            if not isinstance(parm, dict):
                parm = {}
            if name in ("FlateDecode", "Fl"):
                data = _unpredict(_inflate(data, self.limits, cap), parm, self.limits)
            elif name in ("ASCIIHexDecode", "AHx"):
                data = _ascii_hex(data)
            elif name in ("ASCII85Decode", "A85"):
                data = _ascii85(data, cap)
                self.limits.add_decoded(len(data))
            elif name in ("LZWDecode", "LZW"):
                early = parm.get("EarlyChange", 1)
                data = _lzw(data, 0 if early == 0 else 1, self.limits, cap)
                self.limits.add_decoded(len(data))
                data = _unpredict(data, parm, self.limits)
            elif name in ("RunLengthDecode", "RL"):
                data = _run_length(data, self.limits, cap)
                self.limits.add_decoded(len(data))
            elif name == "Crypt":
                continue
            elif name in _IMAGE_FILTERS:
                return None                      # an image: no text in it
            else:
                return None                      # a filter this module does not know
        return data[:cap]

    # ---- pages ----

    def pages(self):
        """[(page dictionary, inherited attributes)] in reading order."""
        pages = self._walk_page_tree()
        if not pages:
            pages = self._scan_for_pages()
        return pages

    def _walk_page_tree(self):
        root = self.catalog.get("Pages")
        pages = []
        stack = [(root, {}, 0)]
        seen = set()
        nodes = 0
        while stack and len(pages) < MAX_PAGES_TOTAL:
            ref, inherited, depth = stack.pop()
            key = ref.num if isinstance(ref, Ref) else id(ref)
            if key in seen:
                continue                         # a loop in the page tree
            seen.add(key)
            node = self.resolve(ref)
            if not isinstance(node, dict):
                continue
            nodes += 1
            if nodes & 0x3FF == 0:
                self.limits.check_time()
            attrs = dict(inherited)
            for name in _INHERITED:
                if name in node:
                    attrs[name] = node[name]
            kids = self.resolve(node.get("Kids"))
            kind = node.get("Type")
            if kind != "Page" and isinstance(kids, list):
                if depth < 64:
                    for kid in reversed(kids):
                        stack.append((kid, attrs, depth + 1))
            elif kind == "Page" or (kind != "Pages" and "Contents" in node):
                pages.append((node, attrs))
        return pages

    def _scan_for_pages(self):
        """Page objects found by scanning the file (when the page tree is broken)."""
        numbers = self._objects_containing(_PAGE_SCAN_RE, MAX_PAGES_TOTAL)
        numbers += sorted(self._compressed_index())
        pages = []
        seen = set()
        for num in numbers:
            if num in seen:
                continue
            seen.add(num)
            node = self.get(num)
            if isinstance(node, dict) and node.get("Type") == "Page":
                pages.append((node, self._inherited_by_parents(node)))
            if len(pages) >= MAX_PAGES_TOTAL:
                break
        return pages

    def _inherited_by_parents(self, node):
        attrs = {}
        hops = 0
        while isinstance(node, dict) and hops < 64:
            for name in _INHERITED:
                if name in node and name not in attrs:
                    attrs[name] = node[name]
            node = self.resolve(node.get("Parent"))
            hops += 1
        return attrs


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------

def _parse_cmap(data, limits):
    """Read a CMap (a /ToUnicode map, or a font's code -> CID encoding).

    Returns (codespace, text_map, cid_map): codespace is [(byte count, low,
    high)], text_map {byte count: {code: text}} and cid_map {byte count:
    {code: CID}}.
    """
    codespace = []
    text_map = {}
    cid_map = {}
    entries = 0
    for op, args in _content_ops(data, limits):
        if entries > MAX_CMAP_ENTRIES:
            break
        if op == b"endcodespacerange":
            for k in range(0, len(args) - 1, 2):
                low, high = args[k], args[k + 1]
                if isinstance(low, bytes) and isinstance(high, bytes) and 0 < len(low) == len(high) <= 4:
                    codespace.append((len(low), low, high))
        elif op == b"endbfchar":
            for k in range(0, len(args) - 1, 2):
                src, dst = args[k], args[k + 1]
                if isinstance(src, bytes) and 0 < len(src) <= 4:
                    text_map.setdefault(len(src), {})[int.from_bytes(src, "big")] = _cmap_text(dst)
                    entries += 1
        elif op == b"endbfrange":
            for k in range(0, len(args) - 2, 3):
                low, high, dst = args[k], args[k + 1], args[k + 2]
                if not (isinstance(low, bytes) and isinstance(high, bytes) and 0 < len(low) <= 4):
                    continue
                target = text_map.setdefault(len(low), {})
                first = int.from_bytes(low, "big")
                last = min(int.from_bytes(high, "big"), first + 65535)
                for offset, code in enumerate(range(first, last + 1)):
                    if isinstance(dst, list):
                        if offset >= len(dst):
                            break
                        target[code] = _cmap_text(dst[offset])
                    else:
                        target[code] = _cmap_text(dst, offset)
                    entries += 1
        elif op == b"endcidrange":
            for k in range(0, len(args) - 2, 3):
                low, high, cid = args[k], args[k + 1], args[k + 2]
                if isinstance(low, bytes) and isinstance(high, bytes) and type(cid) is int and 0 < len(low) <= 4:
                    target = cid_map.setdefault(len(low), {})
                    first = int.from_bytes(low, "big")
                    last = min(int.from_bytes(high, "big"), first + 65535)
                    for offset, code in enumerate(range(first, last + 1)):
                        target[code] = cid + offset
                    entries += last - first + 1
        elif op == b"endcidchar":
            for k in range(0, len(args) - 1, 2):
                src, cid = args[k], args[k + 1]
                if isinstance(src, bytes) and type(cid) is int and 0 < len(src) <= 4:
                    cid_map.setdefault(len(src), {})[int.from_bytes(src, "big")] = cid
                    entries += 1
    return codespace, text_map, cid_map


def _cmap_text(dst, offset=0):
    """Destination of a ToUnicode entry (UTF-16BE bytes) -> text; ``offset`` is added to the last character."""
    if isinstance(dst, str):
        return _glyph_text(dst)
    if type(dst) is int:
        dst = dst.to_bytes(2, "big") if 0 <= dst <= 0xFFFF else b""
    if not isinstance(dst, bytes) or not dst:
        return ""
    if len(dst) % 2:
        if len(dst) == 1:
            return chr(dst[0] + offset) if dst[0] + offset < 0x110000 else ""
        dst = b"\x00" + dst
    if offset:
        units = list(struct.unpack(">%dH" % (len(dst) // 2), dst))
        units[-1] += offset
        if units[-1] > 0xFFFF:
            return ""
        dst = struct.pack(">%dH" % len(units), *units)
    return dst.decode("utf-16-be", "ignore")


def _truetype_glyph_text(font_bytes):
    """{glyph id: text} from an embedded TrueType font's 'cmap' table.

    Used for Identity-H fonts that have no /ToUnicode map: the font's own
    character map says which Unicode character each glyph draws.
    """
    data = font_bytes
    result = {}
    if len(data) < 12:
        return result
    (num_tables,) = struct.unpack(">H", data[4:6])
    cmap = None
    for i in range(min(num_tables, 100)):
        entry = data[12 + 16 * i:28 + 16 * i]
        if len(entry) < 16:
            break
        if entry[:4] == b"cmap":
            offset, length = struct.unpack(">II", entry[8:16])
            cmap = data[offset:offset + length]
    if not cmap or len(cmap) < 4:
        return result
    (count,) = struct.unpack(">H", cmap[2:4])
    tables = {}
    for i in range(min(count, 50)):
        entry = cmap[4 + 8 * i:12 + 8 * i]
        if len(entry) < 8:
            break
        platform, encoding, offset = struct.unpack(">HHI", entry)
        tables[(platform, encoding)] = offset
    for key in ((3, 10), (0, 4), (3, 1), (0, 3), (0, 1), (0, 0), (3, 0), (1, 0)):
        if key in tables:
            mapping = _cmap_subtable(cmap, tables[key])
            for code in sorted(mapping):
                char = code
                if key == (3, 0) and 0xF000 <= code <= 0xF0FF:
                    char = code - 0xF000         # symbol fonts: codes moved up to U+F0xx
                elif key == (1, 0):
                    text = _MAC_ROMAN[code] if code < 256 else ""
                    if text:
                        result.setdefault(mapping[code], text)
                    continue
                if 0x20 <= char < 0x110000 and not 0xD800 <= char <= 0xDFFF:
                    result.setdefault(mapping[code], chr(char))
            if result:
                break
    return result


def _cmap_subtable(cmap, offset):
    """{character code: glyph id} from one TrueType cmap subtable (formats 0, 4, 6, 12)."""
    mapping = {}
    try:
        (fmt,) = struct.unpack(">H", cmap[offset:offset + 2])
        if fmt == 0:
            for code, gid in enumerate(cmap[offset + 6:offset + 262]):
                if gid:
                    mapping[code] = gid
        elif fmt == 4:
            (seg_x2,) = struct.unpack(">H", cmap[offset + 6:offset + 8])
            segs = seg_x2 // 2
            base = offset + 14
            ends = struct.unpack(">%dH" % segs, cmap[base:base + seg_x2])
            starts = struct.unpack(">%dH" % segs, cmap[base + seg_x2 + 2:base + 2 * seg_x2 + 2])
            deltas = struct.unpack(">%dh" % segs, cmap[base + 2 * seg_x2 + 2:base + 3 * seg_x2 + 2])
            ro_pos = base + 3 * seg_x2 + 2
            range_offsets = struct.unpack(">%dH" % segs, cmap[ro_pos:ro_pos + seg_x2])
            for s in range(segs):
                if starts[s] > ends[s] or ends[s] - starts[s] > 65535:
                    continue
                for code in range(starts[s], min(ends[s], 0xFFFE) + 1):
                    if range_offsets[s] == 0:
                        gid = (code + deltas[s]) & 0xFFFF
                    else:
                        where = ro_pos + 2 * s + range_offsets[s] + 2 * (code - starts[s])
                        if where + 2 > len(cmap):
                            continue
                        (gid,) = struct.unpack(">H", cmap[where:where + 2])
                        if gid:
                            gid = (gid + deltas[s]) & 0xFFFF
                    if gid:
                        mapping[code] = gid
                if len(mapping) > MAX_CMAP_ENTRIES:
                    break
        elif fmt == 6:
            first, count = struct.unpack(">HH", cmap[offset + 6:offset + 10])
            gids = struct.unpack(">%dH" % count, cmap[offset + 10:offset + 10 + 2 * count])
            for i, gid in enumerate(gids):
                if gid:
                    mapping[first + i] = gid
        elif fmt == 12:
            (groups,) = struct.unpack(">I", cmap[offset + 12:offset + 16])
            for g in range(min(groups, 100000)):
                start, end, gid = struct.unpack(">III", cmap[offset + 16 + 12 * g:offset + 28 + 12 * g])
                for code in range(start, min(end, start + 65535) + 1):
                    mapping[code] = gid + code - start
                if len(mapping) > MAX_CMAP_ENTRIES:
                    break
    except struct.error:
        pass
    return mapping


class _Font:
    """How one font turns character codes into text and widths."""

    def __init__(self, name=""):
        self.name = name
        self.composite = False     # Type0 font: codes of one or more bytes
        self.table = list(_WIN_ANSI)  # simple fonts: text for each byte
        self.widths = [500.0] * 256   # simple fonts: width of each byte (glyph units)
        self.codespace = None      # composite: [(byte count, low, high)]; None = 2 bytes each
        self.text_maps = {}        # composite: {byte count: {code: text}} from /ToUnicode
        self.cid_maps = None       # composite: {byte count: {code: CID}}; None = CID is the code
        self.cid_widths = {}       # composite: CID -> width
        self.default_width = 1000.0
        self.unicode_codes = False # composite: the codes are Unicode values
        self.gid_text = None       # composite: glyph id -> text from the embedded font
        self.cid_to_gid = None     # composite: bytes of /CIDToGIDMap (None = identity)
        self.guess_unicode = False # composite: no map at all; accept codes that look like text
        self.pua_table = None      # Symbol/Wingdings: fix private-use characters (U+F0xx)
        self.scale = 0.001         # glyph units -> text space
        self.em = 1.0              # font height relative to the font size

    def decode(self, s):
        """Bytes of a string -> (text, width in text space per unit size, codes, spaces)."""
        if not self.composite:
            text = s.decode("latin-1").translate(self.table)
            width = sum(map(self.widths.__getitem__, s)) * self.scale
            if self.pua_table is not None:
                text = text.translate(self.pua_table)
            return text, width, len(s), s.count(32)
        parts = []
        width = 0.0
        cid_widths = self.cid_widths
        default = self.default_width
        codes = self._split(s)
        for code, size in codes:
            cid = code
            if self.cid_maps is not None:
                cid = self.cid_maps.get(size, {}).get(code, code)
            text = self.text_maps.get(size, {}).get(code)
            if text is None:
                text = self._fallback_text(code, cid)
            parts.append(text)
            width += cid_widths.get(cid, default)
        text = "".join(parts)
        if self.pua_table is not None:
            text = text.translate(self.pua_table)
        spaces = sum(1 for code, size in codes if size == 1 and code == 32)
        return text, width * self.scale, len(codes), spaces

    def _split(self, s):
        """Split a string into (code, byte count) using the font's code space."""
        if self.codespace is None:
            count = len(s) // 2
            return [(c, 2) for c in struct.unpack(">%dH" % count, s[:2 * count])]
        codes = []
        i = 0
        total = len(s)
        shortest = min(n for n, low, high in self.codespace)
        while i < total:
            for size, low, high in self.codespace:
                chunk = s[i:i + size]
                if len(chunk) == size and all(low[k] <= chunk[k] <= high[k] for k in range(size)):
                    break
            else:
                size = shortest
                chunk = s[i:i + size]
            codes.append((int.from_bytes(chunk, "big"), size))
            i += size
        return codes

    def _fallback_text(self, code, cid):
        if self.unicode_codes:
            if 0x20 <= code < 0xD800 or 0xE000 <= code < 0x110000:
                return chr(code)
            return ""
        if self.gid_text is not None:
            gid = cid
            if self.cid_to_gid is not None:
                where = 2 * cid
                gid = int.from_bytes(self.cid_to_gid[where:where + 2], "big") if where + 2 <= len(self.cid_to_gid) else 0
            return self.gid_text.get(gid, "")
        if self.guess_unicode and 0x20 <= cid < 0xD800 and unicodedata is not None:
            char = chr(cid)
            if unicodedata.category(char)[0] in "LNPSZ":
                return char
        return ""


def _base_font_name(font_dict):
    name = font_dict.get("BaseFont")
    if not isinstance(name, str):
        name = ""
    if len(name) > 7 and name[6] == "+":       # subset prefix "ABCDEF+Arial"
        name = name[7:]
    return name


def _symbol_kind(name):
    lower = name.lower()
    if "wingding" in lower or "webding" in lower:
        return "wingdings"
    if "dingbat" in lower:
        return "dingbats"
    if "symbol" in lower:
        return "symbol"
    return None


def _pua_fix(kind):
    """translate() table for private-use characters (U+F020-U+F0FF) of symbol fonts."""
    if kind == "symbol":
        base = _SYMBOL
    elif kind == "wingdings":
        base = _dingbat_table(_WINGDINGS)
    else:
        base = _dingbat_table(_DINGBATS)
    return {0xF000 + code: base[code] for code in range(0x20, 0x100)}


def _load_font(doc, font_dict, limits):
    """Build a _Font from a font dictionary."""
    name = _base_font_name(font_dict)
    font = _Font(name)
    if font_dict.get("Subtype") == "Type0":
        _setup_composite(doc, font, font_dict, limits)
    else:
        _setup_simple(doc, font, font_dict, limits)
    kind = _symbol_kind(name)
    if kind is not None:
        font.pua_table = _pua_fix(kind)
    return font


def _named_encoding(name):
    return {"WinAnsiEncoding": _WIN_ANSI, "MacRomanEncoding": _MAC_ROMAN,
            "StandardEncoding": _STANDARD, "PDFDocEncoding": _PDF_DOC,
            "MacExpertEncoding": _STANDARD}.get(name)


def _setup_simple(doc, font, font_dict, limits):
    """Fonts with one byte per character: Type1, TrueType, Type3, MMType1."""
    resolve = doc.resolve
    subtype = font_dict.get("Subtype")
    kind = _symbol_kind(font.name)
    descriptor = resolve(font_dict.get("FontDescriptor"))
    if not isinstance(descriptor, dict):
        descriptor = {}

    # 1. Encoding: a named base encoding, changed by /Differences.
    encoding = resolve(font_dict.get("Encoding"))
    base = None
    differences = None
    if isinstance(encoding, str):
        base = _named_encoding(encoding)
    elif isinstance(encoding, dict):
        base = _named_encoding(resolve(encoding.get("BaseEncoding")))
        differences = resolve(encoding.get("Differences"))
    if base is None:
        if kind == "symbol":
            base = _SYMBOL
        elif kind == "wingdings":
            base = _dingbat_table(_WINGDINGS)
        elif kind == "dingbats":
            base = _dingbat_table(_DINGBATS)
        elif subtype in ("Type1", "MMType1"):
            base = _type1_builtin(doc, descriptor) or _STANDARD
        elif subtype == "Type3":
            base = _STANDARD
        else:
            base = _WIN_ANSI
    table = list(base)
    if isinstance(differences, list):
        code = None
        for item in differences[:4096]:
            item = resolve(item)
            if type(item) is int:
                code = item
            elif isinstance(item, str) and code is not None:
                if 0 <= code < 256:
                    text = _glyph_text(item)
                    if not text and 32 <= code < 127 and kind is None:
                        text = chr(code)         # unknown glyph name: assume ASCII
                    table[code] = text
                code += 1

    # 2. A /ToUnicode map overrides the encoding.
    to_unicode = resolve(font_dict.get("ToUnicode"))
    if isinstance(to_unicode, _Stream):
        data = doc.decode_stream(to_unicode)
        if data:
            _, text_map, _ = _parse_cmap(data, limits)
            for size in sorted(text_map, reverse=True):
                for code, text in text_map[size].items():
                    if 0 <= code < 256:
                        table[code] = text
    font.table = table

    # 3. Widths.
    widths = resolve(font_dict.get("Widths"))
    first = resolve(font_dict.get("FirstChar"))
    missing = resolve(descriptor.get("MissingWidth"))
    if isinstance(widths, list) and type(first) is int:
        values = [resolve(w) for w in widths[:256]]
        numbers = [float(w) for w in values if isinstance(w, (int, float)) and not isinstance(w, bool)]
        fallback = float(missing) if isinstance(missing, (int, float)) and missing > 0 else (
            sum(numbers) / len(numbers) if numbers else 500.0)
        font.widths = [fallback] * 256
        for i, w in enumerate(values):
            if 0 <= first + i < 256 and isinstance(w, (int, float)) and not isinstance(w, bool):
                font.widths[first + i] = float(w)
    else:
        font.widths = _standard_widths(font.name)

    # 4. Type 3 fonts draw glyphs in their own units.
    if subtype == "Type3":
        matrix = resolve(font_dict.get("FontMatrix"))
        matrix = _matrix(matrix)
        if matrix is not None:
            if matrix[0]:
                font.scale = min(max(abs(matrix[0]), 1e-6), 1.0)
            bbox = _rect(resolve(font_dict.get("FontBBox")), resolve)
            height = (bbox[3] - bbox[1]) * abs(matrix[3]) if bbox else 0.0
            font.em = min(max(height or abs(matrix[3]) * 1000, 0.2), 5.0)


_TYPE1_STANDARD_RE = re.compile(rb"/Encoding[\x00\t\n\x0c\r ]+StandardEncoding")
_TYPE1_ENCODING_RE = re.compile(rb"dup[\x00\t\n\x0c\r ]+(\d+)[\x00\t\n\x0c\r ]*/(" + _REGULAR + rb"+)[\x00\t\n\x0c\r ]+put")


def _type1_builtin(doc, descriptor):
    """The built-in encoding of an embedded Type 1 font program, or None (= Standard)."""
    program = doc.resolve(descriptor.get("FontFile"))
    if not isinstance(program, _Stream):
        return None
    data = doc.decode_stream(program, cap=4 * 1024 * 1024) or b""
    clear = doc.resolve(program.dict.get("Length1"))
    header = data[:clear] if type(clear) is int and clear > 0 else data[:65536]
    if _TYPE1_STANDARD_RE.search(header):
        return None
    pairs = _TYPE1_ENCODING_RE.findall(header)
    if not pairs:
        return None
    table = [""] * 256
    for code, name in pairs[:256]:
        code = int(code)
        if code < 256:
            table[code] = _glyph_text(_name(name))
    return table


def _setup_composite(doc, font, font_dict, limits):
    """Type0 fonts: codes of one or more bytes select CIDs in a descendant font."""
    resolve = doc.resolve
    font.composite = True
    descendants = resolve(font_dict.get("DescendantFonts"))
    desc = resolve(descendants[0]) if isinstance(descendants, list) and descendants else None
    if not isinstance(desc, dict):
        desc = {}
    default = resolve(desc.get("DW"))
    if isinstance(default, (int, float)) and not isinstance(default, bool):
        font.default_width = float(default)
    font.cid_widths = _cid_widths(resolve(desc.get("W")), resolve)

    encoding = resolve(font_dict.get("Encoding"))
    if isinstance(encoding, _Stream):
        data = doc.decode_stream(encoding)
        if data:
            codespace, _, cid_maps = _parse_cmap(data, limits)
            if codespace and not all(size == 2 for size, low, high in codespace):
                font.codespace = codespace
            if cid_maps:
                font.cid_maps = cid_maps
    elif isinstance(encoding, str) and encoding not in ("Identity-H", "Identity-V"):
        if "UCS2" in encoding or "UTF16" in encoding:
            font.unicode_codes = True

    to_unicode = resolve(font_dict.get("ToUnicode"))
    if isinstance(to_unicode, _Stream):
        data = doc.decode_stream(to_unicode)
        if data:
            codespace, text_map, _ = _parse_cmap(data, limits)
            font.text_maps = text_map
            # The /ToUnicode code space says how long the codes are only when
            # the encoding itself cannot (a named CMap other than Identity/UCS-2).
            named = isinstance(encoding, str) and encoding not in ("Identity-H", "Identity-V")
            if named and not font.unicode_codes and codespace and \
                    not all(size == 2 for size, low, high in codespace):
                font.codespace = codespace
    if font.text_maps or font.unicode_codes:
        return
    # No map to Unicode: read the embedded TrueType font's own character map,
    # or (font not embedded) accept codes that look like Unicode.
    descriptor = resolve(desc.get("FontDescriptor"))
    if not isinstance(descriptor, dict):
        descriptor = {}
    program = resolve(descriptor.get("FontFile2"))
    if isinstance(program, _Stream):
        data = doc.decode_stream(program)
        if data:
            font.gid_text = _truetype_glyph_text(data)
            mapping = resolve(desc.get("CIDToGIDMap"))
            if isinstance(mapping, _Stream):
                font.cid_to_gid = doc.decode_stream(mapping) or b""
    elif not any(key in descriptor for key in ("FontFile", "FontFile2", "FontFile3")):
        font.guess_unicode = True


def _cid_widths(array, resolve):
    """/W array of a CID font -> {CID: width}."""
    widths = {}
    if not isinstance(array, list):
        return widths
    i = 0
    total = 0
    while i < len(array) - 1 and total < MAX_CMAP_ENTRIES:
        first = resolve(array[i])
        nxt = resolve(array[i + 1])
        if type(first) is not int:
            break
        if isinstance(nxt, list):
            for k, w in enumerate(nxt[:65536]):
                w = resolve(w)
                if isinstance(w, (int, float)) and not isinstance(w, bool):
                    widths[first + k] = float(w)
            total += len(nxt)
            i += 2
        elif i + 2 < len(array):
            last = nxt
            w = resolve(array[i + 2])
            if type(last) is int and isinstance(w, (int, float)) and not isinstance(w, bool):
                for cid in range(first, min(last, first + 65535) + 1):
                    widths[cid] = float(w)
                total += max(0, last - first + 1)
            i += 3
        else:
            break
    return widths


# --------------------------------------------------------------------------
# Content streams -> positioned text -> lines
# --------------------------------------------------------------------------

_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

_TEXT_OPS = frozenset([b"Tj", b"TJ", b"'", b'"', b"Td", b"TD", b"Tm", b"T*", b"BT",
                       b"Tf", b"Tc", b"Tw", b"Tz", b"TL", b"Ts", b"cm", b"q", b"Q",
                       b"Do", b"gs"])

# Characters tidied in the final text: control characters dropped, ligatures
# spelled out, no-break space -> space.
_TIDY = {code: None for code in range(32) if code not in (9, 10)}
_TIDY.update({0x7F: None, 0x09: " ", 0xA0: " ", 0x200B: None, 0xFEFF: None,
              0xFFFD: None, 0x2028: " ", 0x2029: " ",
              0xFB00: "ff", 0xFB01: "fi", 0xFB02: "fl", 0xFB03: "ffi", 0xFB04: "ffl",
              0xFB05: "st", 0xFB06: "st"})
_PRIVATE_USE_RE = re.compile("[\ue000-\uf8ff]")
_SPACES_RE = re.compile(" {3,}")


def _mul(m1, m2):
    """Matrix product m1 x m2 of PDF matrices [a b c d e f]."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


def _translate(m, tx, ty):
    a, b, c, d, e, f = m
    return (a, b, c, d, tx * a + ty * c + e, tx * b + ty * d + f)


def _num(value):
    """An operand as a float (raises ValueError for anything else)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = float(value)
        if -1e9 < value < 1e9:
            return value
    raise ValueError("not a number")


def _matrix(value):
    if isinstance(value, list) and len(value) == 6:
        try:
            return tuple(_num(v) for v in value)
        except ValueError:
            return None
    return None


class _Line:
    """One line of text: pieces placed along a baseline."""

    __slots__ = ("ox", "oy", "ux", "uy", "pieces", "low", "high", "end_x", "end_y",
                 "size", "paragraph")

    def __init__(self, x, y, ux, uy, paragraph):
        self.ox, self.oy = x, y           # where the line starts
        self.ux, self.uy = ux, uy         # direction of the baseline
        self.pieces = []                  # (start, end, size, text) along the baseline
        self.low = self.high = 0.0        # extent along the baseline
        self.end_x, self.end_y = x, y     # where the last piece ended
        self.size = 0.0                   # size of the last piece
        self.paragraph = paragraph        # a blank line goes before this one

    def put(self, x0, y0, x1, y1, size, text):
        start = (x0 - self.ox) * self.ux + (y0 - self.oy) * self.uy
        end = (x1 - self.ox) * self.ux + (y1 - self.oy) * self.uy
        if end < start:
            start, end = end, start
        if not self.pieces:
            self.low, self.high = start, end
        self.low = min(self.low, start)
        self.high = max(self.high, end)
        self.pieces.append((start, end, size, text))
        self.end_x, self.end_y, self.size = x1, y1, size

    def across(self, x, y):
        """Sideways distance of a point from the end of this line (> 0: above it)."""
        return self.ux * (y - self.end_y) - self.uy * (x - self.end_x)

    def text(self):
        """The pieces in order along the baseline, with spaces where there are gaps."""
        pieces = sorted(self.pieces, key=lambda piece: piece[0])
        parts = []
        last_start = last_end = last_size = 0.0
        last_text = None
        for start, end, size, text in pieces:
            if last_text is not None:
                ref = size if size > last_size else last_size
                if text == last_text and abs(start - last_start) < 0.1 * ref:
                    continue                      # the same text drawn twice ("fake bold")
                gap = start - last_end
                if gap > SPACE_GAP * ref and not parts[-1][-1:].isspace() and not text[:1].isspace():
                    parts.append("  " if gap > WIDE_GAP * ref else " ")
                last_end = max(last_end, end)
            else:
                last_end = end
            parts.append(text)
            last_start, last_size, last_text = start, size, text
        return "".join(parts)


class _TextCollector:
    """Collects positioned pieces of text and joins them into lines.

    Lines keep the order in which the PDF draws them (generators draw
    columns one after the other), but the pieces of one line are put in
    order along the baseline, so a character drawn later (a symbol from
    another font, a superscript) still lands in its place.
    """

    def __init__(self):
        self.lines = []          # _Line objects, in the order they were started
        self.step = None         # usual distance from one line to the next
        self.chars = 0
        self.attach = False      # form field values: join any line on the same baseline

    def add(self, x0, y0, x1, y1, ux, uy, size, text):
        if size < 0.5:
            size = 0.5
        lines = self.lines
        line = lines[-1] if lines else None
        if self.attach:
            line = self._same_baseline(x0, y0, ux, uy, size)
            if line is not None:
                line.put(x0, y0, x1, y1, size, text)
                self.chars += len(text)
                return
            line = lines[-1] if lines else None
        if line is not None and self._fits(line, x0, y0, ux, uy, size):
            line.put(x0, y0, x1, y1, size, text)
        else:
            for older in lines[-5:-1]:          # late text for a recent line
                if self._fits(older, x0, y0, ux, uy, size) and \
                        older.low - 2 * size <= self._along(older, x0, y0) <= older.high + size:
                    older.put(x0, y0, x1, y1, size, text)
                    break
            else:
                paragraph = False
                if line is not None and line.ux * ux + line.uy * uy >= 0.95:
                    ref = size if size > line.size else line.size
                    paragraph = self._is_paragraph(-line.across(x0, y0), ref)
                line = _Line(x0, y0, ux, uy, paragraph)
                line.put(x0, y0, x1, y1, size, text)
                lines.append(line)
        self.chars += len(text)
        if self.chars > MAX_PAGE_CHARS:
            raise _PageFull()

    def _same_baseline(self, x, y, ux, uy, size):
        """The line whose baseline passes closest to (x, y), if one is close enough."""
        best = None
        best_across = None
        for line in self.lines:
            if self._fits(line, x, y, ux, uy, size) and self._along(line, x, y) >= line.low - 2 * size:
                across = abs(line.across(x, y))
                if best is None or across < best_across:
                    best, best_across = line, across
        return best

    @staticmethod
    def _along(line, x, y):
        return (x - line.ox) * line.ux + (y - line.oy) * line.uy

    @staticmethod
    def _fits(line, x, y, ux, uy, size):
        """True when a piece starting at (x, y) belongs on this line."""
        if line.ux * ux + line.uy * uy < 0.95:      # turned: another line
            return False
        ref = size if size > line.size else line.size
        across = line.across(x, y)
        return -LINE_GAP * ref <= across <= LINE_GAP * ref

    def _is_paragraph(self, step, ref):
        """True when the move down to the next line is bigger than a normal line step."""
        if step <= 0:
            return step < -1.5 * ref                 # jumped up: a new column or block
        usual = self.step if self.step else 1.25 * ref
        if step > 1.35 * usual and step > 1.3 * ref:
            return True
        if step < 3 * ref:
            self.step = step
        return False

    def text(self):
        out = []
        for line in self.lines:
            if line.paragraph:
                out.append("")
            out.append(line.text())
        return _tidy_lines(out)


def _tidy_lines(lines):
    """Clean characters, join words hyphenated across lines, drop extra blank lines."""
    cleaned = []
    for line in lines:
        line = line.translate(_TIDY)
        if "\ue000" <= max(line, default="") and _PRIVATE_USE_RE.search(line):
            line = _PRIVATE_USE_RE.sub("", line)
        if "   " in line:
            line = _SPACES_RE.sub("  ", line)
        cleaned.append(line.strip())
    for i in range(len(cleaned) - 1):
        line = cleaned[i]
        nxt = cleaned[i + 1]
        if line is None or len(line) < 2 or not nxt:
            continue
        soft = line[-1] == "\u00ad"
        if soft or (line[-1] in "-\u2010" and line[-2].islower() and nxt[0].islower()):
            word, _, rest = nxt.partition(" ")
            cleaned[i] = line[:-1] + word
            cleaned[i + 1] = rest.lstrip() if rest.strip() else None
    out = []
    for line in cleaned:
        if line is None:
            continue
        if "\u00ad" in line:
            line = line.replace("\u00ad", "")
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


class _GState:
    """The parts of the graphics state that matter for text."""

    __slots__ = ("ctm", "font", "size", "char_space", "word_space", "scale",
                 "leading", "rise")

    def __init__(self, ctm=_IDENTITY):
        self.ctm = ctm
        self.font = None
        self.size = 0.0
        self.char_space = 0.0
        self.word_space = 0.0
        self.scale = 1.0         # horizontal scaling (Tz / 100)
        self.leading = 0.0
        self.rise = 0.0

    def copy(self):
        other = _GState(self.ctm)
        other.font = self.font
        other.size = self.size
        other.char_space = self.char_space
        other.word_space = self.word_space
        other.scale = self.scale
        other.leading = self.leading
        other.rise = self.rise
        return other


class _PageReader:
    """Runs content streams and collects their text."""

    def __init__(self, doc, limits):
        self.doc = doc
        self.limits = limits
        self.fonts = {}
        self.default_font = _Font("default")
        self.out = None
        self.ops = 0
        self.forms = set()
        self.stopped = None      # reason a budget ran out, if it did
        self.incomplete = 0      # pages cut short (too complex)

    def page_text(self, page, attrs):
        """Text of one page. A budget running out keeps what was read so far."""
        self.out = _TextCollector()
        self.ops = 0
        self.forms = set()
        resolve = self.doc.resolve
        resources = resolve(attrs.get("Resources"))
        if not isinstance(resources, dict):
            resources = {}
        try:
            data = self._contents(resolve(page.get("Contents")))
            self._run(data, resources, _GState(), 0)
            self._annotations(page)
        except _PageFull:
            self.incomplete += 1
        except _OutOfBudget as exc:
            self.stopped = exc.reason
        except Exception:                  # damaged content: keep what was read
            self.incomplete += 1
        return self.out.text()

    def _contents(self, contents):
        resolve = self.doc.resolve
        if isinstance(contents, _Stream):
            return self.doc.decode_stream(contents) or b""
        if isinstance(contents, list):
            parts = []
            for item in contents[:10000]:
                item = resolve(item)
                if isinstance(item, _Stream):
                    parts.append(self.doc.decode_stream(item) or b"")
            return b"\n".join(parts)
        return b""

    def _font(self, fonts, name):
        ref = fonts.get(name) if isinstance(fonts, dict) else None
        key = ("ref", ref.num) if isinstance(ref, Ref) else ("id", id(ref))
        font = self.fonts.get(key)
        if font is None:
            font_dict = self.doc.resolve(ref)
            if isinstance(font_dict, dict):
                try:
                    font = _load_font(self.doc, font_dict, self.limits)
                except (_OutOfBudget, _PageFull):
                    raise
                except Exception:
                    font = self.default_font
            else:
                font = self.default_font
            if ref is not None:
                self.fonts[key] = font
        return font

    def _run(self, data, resources, state, depth):
        """Interpret one content stream (a page, a form or an appearance)."""
        if not data:
            return
        resolve = self.doc.resolve
        fonts = resolve(resources.get("Font"))
        st = state.copy()
        saved = []
        tm = tlm = _IDENTITY
        for op, args in _content_ops(data, self.limits):
            self.ops += 1
            if self.ops > MAX_OPS_PER_PAGE:
                raise _PageFull()
            if op not in _TEXT_OPS:
                continue
            try:
                if op == b"Tj":
                    tm = self._show(st, tm, args[-1])
                elif op == b"TJ":
                    tm = self._show_array(st, tm, args[-1])
                elif op == b"Td":
                    tlm = _translate(tlm, _num(args[-2]), _num(args[-1]))
                    tm = tlm
                elif op == b"TD":
                    ty = _num(args[-1])
                    st.leading = -ty
                    tlm = _translate(tlm, _num(args[-2]), ty)
                    tm = tlm
                elif op == b"Tm":
                    tm = tlm = tuple(_num(v) for v in args[-6:])
                    if len(tm) != 6:
                        tm = tlm = _IDENTITY
                elif op == b"T*":
                    tlm = _translate(tlm, 0.0, -st.leading)
                    tm = tlm
                elif op == b"'":
                    tlm = _translate(tlm, 0.0, -st.leading)
                    tm = self._show(st, tlm, args[-1])
                elif op == b'"':
                    st.word_space = _num(args[-3])
                    st.char_space = _num(args[-2])
                    tlm = _translate(tlm, 0.0, -st.leading)
                    tm = self._show(st, tlm, args[-1])
                elif op == b"BT":
                    tm = tlm = _IDENTITY
                elif op == b"Tf":
                    st.font = self._font(fonts, args[-2])
                    st.size = _num(args[-1])
                elif op == b"Tc":
                    st.char_space = _num(args[-1])
                elif op == b"Tw":
                    st.word_space = _num(args[-1])
                elif op == b"Tz":
                    st.scale = _num(args[-1]) / 100.0
                elif op == b"TL":
                    st.leading = _num(args[-1])
                elif op == b"Ts":
                    st.rise = _num(args[-1])
                elif op == b"cm":
                    matrix = tuple(_num(v) for v in args[-6:])
                    if len(matrix) == 6:
                        st.ctm = _mul(matrix, st.ctm)
                elif op == b"q":
                    if len(saved) < 1000:
                        saved.append(st.copy())
                elif op == b"Q":
                    if saved:
                        st = saved.pop()
                elif op == b"Do":
                    self._draw_form(resources, args[-1], st, depth)
                elif op == b"gs":
                    self._ext_gstate(resources, args[-1], st)
            except (IndexError, ValueError, TypeError, OverflowError, ZeroDivisionError):
                continue                       # malformed operands: skip the operator

    def _show(self, st, tm, s):
        """Show one string; returns the text matrix after it."""
        if not isinstance(s, bytes):
            return tm
        font = st.font or self.default_font
        text, width, codes, spaces = font.decode(s)
        advance = (width * st.size + st.char_space * codes + st.word_space * spaces) * st.scale
        if text:
            self._emit(st, font, tm, 0.0, advance, text)
        return _translate(tm, advance, 0.0)

    def _show_array(self, st, tm, items):
        """TJ: strings and position adjustments.

        Small adjustments (kerning) keep the text in one piece; a big one
        ends the piece, so the line can put a space (or another piece of
        text drawn later) in the gap.
        """
        if not isinstance(items, list):
            return tm
        font = st.font or self.default_font
        parts = []
        advance = 0.0
        start = None             # where the current piece's text starts
        end = 0.0                # where its last text ends
        for item in items:
            if isinstance(item, bytes):
                text, width, codes, spaces = font.decode(item)
                if text and start is None:
                    start = advance
                advance += (width * st.size + st.char_space * codes + st.word_space * spaces) * st.scale
                if text:
                    parts.append(text)
                    end = advance
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                if -1e7 < item < 1e7:
                    advance -= item / 1000.0 * st.size * st.scale
                    if parts and (-item > SPACE_GAP * 1000 * font.em or item > 500 * font.em):
                        self._emit(st, font, tm, start, end, "".join(parts))
                        parts = []
                        start = None
        if parts:
            self._emit(st, font, tm, start, end, "".join(parts))
        return _translate(tm, advance, 0.0)

    def _emit(self, st, font, tm, begin, end, text):
        a, b, c, d, e, f = _mul(tm, st.ctm)
        x = c * st.rise + e
        y = d * st.rise + f
        scale_x = math.hypot(a, b)
        if scale_x > 0:
            ux, uy = a / scale_x, b / scale_x
        else:
            ux, uy = 1.0, 0.0
        size = abs(st.size) * math.hypot(c, d) * font.em
        self.out.add(x + a * begin, y + b * begin, x + a * end, y + b * end, ux, uy, size, text)

    def _draw_form(self, resources, name, st, depth):
        """Do: run a Form XObject's content with its own resources."""
        resolve = self.doc.resolve
        xobjects = resolve(resources.get("XObject"))
        if not isinstance(xobjects, dict) or not isinstance(name, str):
            return
        ref = xobjects.get(name)
        form = resolve(ref)
        if not isinstance(form, _Stream) or resolve(form.dict.get("Subtype")) != "Form":
            return
        key = ref.num if isinstance(ref, Ref) else id(form)
        if depth >= MAX_FORM_DEPTH or key in self.forms:
            return
        self._run_form(form, resources, st, depth, key, None)

    def _run_form(self, form, resources, st, depth, key, outer_matrix):
        resolve = self.doc.resolve
        try:
            data = self.doc.decode_stream(form)
        except (_OutOfBudget, _PageFull):
            raise
        except Exception:
            return
        if not data:
            return
        matrix = _matrix(resolve(form.dict.get("Matrix"))) or _IDENTITY
        own = resolve(form.dict.get("Resources"))
        inner = st.copy()
        inner.ctm = _mul(matrix, outer_matrix or st.ctm)
        self.forms.add(key)
        try:
            self._run(data, own if isinstance(own, dict) else resources, inner, depth + 1)
        except (_OutOfBudget, _PageFull):
            raise
        except Exception:
            pass                               # a broken form never loses the page
        finally:
            self.forms.discard(key)

    def _ext_gstate(self, resources, name, st):
        """gs with a /Font entry (rare) sets the font."""
        resolve = self.doc.resolve
        states = resolve(resources.get("ExtGState"))
        if not isinstance(states, dict):
            return
        gs = resolve(states.get(name))
        if isinstance(gs, dict):
            font = resolve(gs.get("Font"))
            if isinstance(font, list) and len(font) == 2:
                st.font = self._font({"F": font[0]}, "F")
                st.size = _num(resolve(font[1]))

    def _annotations(self, page):
        """Text drawn by form fields and typed-on comments (their appearance streams)."""
        resolve = self.doc.resolve
        annots = resolve(page.get("Annots"))
        if not isinstance(annots, list):
            return
        for annot in annots[:2000]:
            annot = resolve(annot)
            if not isinstance(annot, dict) or annot.get("Subtype") not in ("Widget", "FreeText"):
                continue
            flags = annot.get("F")
            if type(flags) is int and flags & 2:     # hidden
                continue
            appearance = resolve(annot.get("AP"))
            normal = resolve(appearance.get("N")) if isinstance(appearance, dict) else None
            if isinstance(normal, dict):            # several states: use the current one
                normal = resolve(normal.get(annot.get("AS")))
            if not isinstance(normal, _Stream):
                continue
            place = self._annotation_matrix(annot, normal)
            if place is not None:
                self.out.attach = True
                try:
                    self._run_form(normal, {}, _GState(), 0, ("annot", id(normal)), place)
                finally:
                    self.out.attach = False

    def _annotation_matrix(self, annot, form):
        """Matrix that puts an appearance stream's box onto the annotation's /Rect."""
        resolve = self.doc.resolve
        rect = _rect(resolve(annot.get("Rect")), resolve)
        bbox = _rect(resolve(form.dict.get("BBox")), resolve)
        if rect is None or bbox is None:
            return None
        matrix = _matrix(resolve(form.dict.get("Matrix"))) or _IDENTITY
        corners = [(bbox[0], bbox[1]), (bbox[2], bbox[1]), (bbox[0], bbox[3]), (bbox[2], bbox[3])]
        xs = [x * matrix[0] + y * matrix[2] + matrix[4] for x, y in corners]
        ys = [x * matrix[1] + y * matrix[3] + matrix[5] for x, y in corners]
        width = max(xs) - min(xs)
        height = max(ys) - min(ys)
        if width <= 0 or height <= 0:
            return None
        sx = (rect[2] - rect[0]) / width
        sy = (rect[3] - rect[1]) / height
        # The form's own /Matrix is applied by _run_form, so this maps the
        # transformed box onto the rectangle.
        return (sx, 0.0, 0.0, sy, rect[0] - min(xs) * sx, rect[1] - min(ys) * sy)


def _rect(value, resolve):
    """[x1 y1 x2 y2] -> normalised (left, bottom, right, top), or None."""
    if not isinstance(value, list) or len(value) != 4:
        return None
    try:
        x1, y1, x2, y2 = (_num(resolve(v)) for v in value)
    except ValueError:
        return None
    left, right = min(x1, x2), max(x1, x2)
    bottom, top = min(y1, y2), max(y1, y2)
    if right - left <= 0 or top - bottom <= 0:
        return None
    return (left, bottom, right, top)


def _page_size(doc, page, attrs):
    """(width, height) in points as the page is shown (crop box, rotation)."""
    resolve = doc.resolve
    media = _rect(resolve(attrs.get("MediaBox")), resolve) or (0.0, 0.0, 612.0, 792.0)
    crop = _rect(resolve(attrs.get("CropBox")), resolve)
    box = media
    if crop is not None:
        left, bottom = max(crop[0], media[0]), max(crop[1], media[1])
        right, top = min(crop[2], media[2]), min(crop[3], media[3])
        if right > left and top > bottom:
            box = (left, bottom, right, top)
    width, height = box[2] - box[0], box[3] - box[1]
    unit = resolve(page.get("UserUnit"))
    if isinstance(unit, (int, float)) and not isinstance(unit, bool) and 0 < unit < 1000:
        width *= unit
        height *= unit
    rotate = resolve(attrs.get("Rotate"))
    if type(rotate) is int and rotate % 180 == 90:
        width, height = height, width
    return (round(width, 1), round(height, 1))


# --------------------------------------------------------------------------
# Title
# --------------------------------------------------------------------------

def _text_string(raw):
    """A PDF text string (title, etc.) -> str."""
    if raw.startswith(b"\xfe\xff"):
        text = raw[2:].decode("utf-16-be", "ignore")
    elif raw.startswith(b"\xff\xfe"):
        text = raw[2:].decode("utf-16-le", "ignore")
    elif raw.startswith(b"\xef\xbb\xbf"):
        text = raw[3:].decode("utf-8", "ignore")
    else:
        text = "".join(_PDF_DOC[b] for b in raw)
    return " ".join(text.translate(_TIDY).split())


_XMP_TITLE_RE = re.compile(rb"<dc:title>(.*?)</dc:title>", re.S)
_XMP_ITEM_RE = re.compile(rb"<rdf:li[^>]*>(.*?)</rdf:li>", re.S)


def _title(doc):
    resolve = doc.resolve
    info = resolve(doc.trailer.get("Info"))
    title = ""
    if isinstance(info, dict):
        raw = resolve(info.get("Title"))
        if isinstance(raw, bytes):
            title = _text_string(raw)
    if not title:
        metadata = resolve(doc.catalog.get("Metadata"))
        if isinstance(metadata, _Stream):
            xml = doc.decode_stream(metadata, cap=1024 * 1024) or b""
            m = _XMP_TITLE_RE.search(xml)
            if m:
                item = _XMP_ITEM_RE.search(m.group(1))
                raw = item.group(1) if item else m.group(1)
                raw = re.sub(rb"<[^>]*>", b"", raw)
                title = " ".join(html.unescape(raw.decode("utf-8", "ignore")).split())
    return title[:300]


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def extract_pdf(data=None, path=None, max_pages=300, time_budget=None):
    """Read the text of a PDF. Never raises; a time budget keeps it from hanging.

    Give either ``data`` (bytes) or ``path``. ``max_pages`` 0 or None reads
    every page; ``time_budget`` is in seconds (default TIME_BUDGET). Returns
    a dict:

    * ``pages``: text of each page read (at most ``max_pages``), lines joined
      with "\\n", a blank line between paragraphs/blocks and two spaces for a
      wide gap on a line (table columns). Pages not reached because a limit
      ran out are "";
    * ``page_sizes``: (width, height) in points of the same pages, as shown
      (crop box, rotation applied);
    * ``page_count``: number of pages in the document;
    * ``title``: the document title property, "" if none;
    * ``status``: "ok", "protected" (encrypted: no text, but page_count and
      page_sizes are filled in when readable) or "error";
    * ``note``: a plain-English reason when the status is not "ok", or when
      the text is incomplete (damaged file, time limit...); else "".

    A scanned PDF without a text layer gives status "ok" and empty pages.
    """
    result = {"pages": [], "page_sizes": [], "page_count": 0, "title": "",
              "status": "error", "note": ""}
    seconds = TIME_BUDGET if time_budget is None else time_budget
    try:
        if data is None:
            if path is None:
                result["note"] = "No PDF given."
                return result
            with open(path, "rb") as handle:
                data = handle.read(MAX_FILE_BYTES + 1)
        if not isinstance(data, bytes):
            data = bytes(data)
        if len(data) > MAX_FILE_BYTES:
            result["note"] = "The PDF is too big to read (over %d MB)." % (MAX_FILE_BYTES // (1024 * 1024))
            return result
        _extract(data, max_pages, seconds, result)
    except _OutOfBudget as exc:
        result["status"] = "error"
        result["pages"] = []
        result["note"] = _budget_note(exc.reason, None, seconds)
    except OSError as exc:
        result["status"] = "error"
        result["note"] = "The PDF could not be opened (%s)." % (exc.strerror or exc.__class__.__name__)
    except MemoryError:
        result["status"] = "error"
        result["note"] = "Not enough memory to read this PDF."
    except Exception as exc:  # never let a strange file stop a run
        result["status"] = "error"
        result["note"] = "The PDF could not be read (%s)." % exc.__class__.__name__
    return result


def _budget_note(reason, page, seconds):
    where = "" if page is None else "; pages from %d on were not read" % page
    if reason == "time":
        return "Stopped after %g seconds (very complex PDF)%s." % (seconds, where)
    if reason == "chars":
        return "Text limit reached%s." % where
    return "The PDF is too large or complex to read in full%s." % where


def _extract(data, max_pages, seconds, result):
    limits = _Limits(seconds)
    if data.find(b"%PDF-", 0, 1024) < 0 and not _OBJ_SCAN_RE.search(data):
        result["note"] = "This is not a PDF file."
        return
    doc = _Document(data, limits)
    doc.open()
    pages = doc.pages()
    result["page_count"] = len(pages)
    count = len(pages) if not max_pages or max_pages < 0 else min(len(pages), max_pages)
    sizes = []
    for page, attrs in pages[:count]:
        try:
            sizes.append(_page_size(doc, page, attrs))
        except _OutOfBudget:
            raise
        except Exception:
            sizes.append((612.0, 792.0))
    result["page_sizes"] = sizes
    if doc.encrypted:
        result["status"] = "protected"
        result["note"] = "The PDF is encrypted (password-protected or locked), so its text cannot be read."
        return
    if not pages:
        result["note"] = "No pages found (the PDF may be damaged)."
        return
    result["title"] = _title(doc)
    reader = _PageReader(doc, limits)
    texts = []
    total = 0
    failed = 0
    stopped = None
    stopped_at = None
    for number, (page, attrs) in enumerate(pages[:count], 1):
        if stopped:
            texts.append("")
            continue
        try:
            text = reader.page_text(page, attrs)
        except _OutOfBudget as exc:
            reader.stopped = exc.reason
            text = ""
        except Exception:
            failed += 1
            text = ""
        if total + len(text) > MAX_TOTAL_CHARS:
            text = text[:max(0, MAX_TOTAL_CHARS - total)]
            reader.stopped = "chars"
        total += len(text)
        texts.append(text)
        if reader.stopped:
            stopped = reader.stopped
            stopped_at = number + 1
    result["pages"] = texts
    result["status"] = "ok"
    notes = []
    if doc.damaged:
        notes.append("The PDF is damaged; text was recovered where possible.")
    if stopped:
        notes.append(_budget_note(stopped, stopped_at if stopped_at <= count else None, seconds))
    if failed:
        notes.append("%d page%s could not be read." % (failed, "" if failed == 1 else "s"))
        if failed == count:
            result["status"] = "error"
    if reader.incomplete:
        notes.append("%d very complex page%s may be incomplete." % (
            reader.incomplete, "" if reader.incomplete == 1 else "s"))
    result["note"] = " ".join(notes)
