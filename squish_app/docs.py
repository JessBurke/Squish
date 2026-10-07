"""Read one document (Word, Excel, PowerPoint, PDF, text or zip) into a DocText.

A DocText is a plain JSON-friendly dict (see DESIGN.md, "Documents (v1.1)"):

    kind, status, note, title, pages, drawing, blocks, chars, reader

``blocks`` keeps the document's structure (headings, paragraphs, list items,
table rows, sheets, pages, slides, zip members) so the documents digest can
shorten it sensibly. Nothing here is capped for the digest: caps are applied
when the digest is written. The text kept is limited to DOC_TEXT_MAX
characters.

Word, Excel and PowerPoint files are zip files of XML parts and are read with
the standard library only. PDFs are read with the optional ``pypdf`` package
when it is installed, else with Squish's built-in reader (pdftext.py).

These files arrive as email attachments, so every reader is defensive: sizes,
compression ratios, XML depth and XML element counts are limited, XML with a
DTD or entity declarations is refused (no "billion laughs"), and extract()
never raises: a problem becomes status "error" with a short note.

Set the environment variable SQUISH_NO_PYPDF=1 to always use the built-in PDF
reader (handy for troubleshooting).
"""

import csv
import io
import logging
import os
import posixpath
import re
import struct
import threading
import time
import zipfile
import zlib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from urllib.parse import unquote

from squish_app import paths

# --------------------------------------------------------------------------
# Limits
# --------------------------------------------------------------------------

DOC_MAX_BYTES = 40 * 1024 * 1024     # bigger files are not read (status "too_big")
DOC_TEXT_MAX = 400000                # characters of text kept per document
PAGE_MAX = 300                       # PDF pages read

SHEET_ROWS_MAX = 5000                # rows kept per sheet (the digest shows far fewer)
SHARED_STRINGS_MAX = 200000          # Excel shared strings loaded
SHARED_CHARS_MAX = 10000000
ZIP_NAMES_MAX = 60                   # zip member names listed
ZIP_DOCS_MAX = 40                    # documents read from inside one zip
ZIP_DOCS_BYTES_MAX = 50 * 1024 * 1024
ZIP_MEMBER_TEXT_MAX = 50000          # characters kept per document inside a zip

XML_PART_MAX = 64 * 1024 * 1024      # bytes parsed from one XML part
UNZIP_MAX = 512 * 1024 * 1024        # bytes unzipped from one file (all parts)
XML_ELEMENTS_MAX = 1500000           # XML elements parsed from one file
BODY_BLOCKS_MAX = 150000             # Word paragraphs and tables read
XML_DEPTH_MAX = 200                  # XML nesting depth
RATIO_MAX = 1100                     # deflate cannot do better than about 1032:1
ZIP_ENTRIES_MAX = 20000
PDF_TIME_MAX = 60.0                  # seconds pypdf may spend on one PDF
NO_TEXT_PER_PAGE = 20                # fewer characters per page -> scanned PDF

_CHUNK = 64 * 1024

# Extension -> kind. Old binary formats (.doc .xls .ppt) are not read.
_EXT_KIND = {
    ".docx": "docx", ".docm": "docx", ".dotx": "docx",
    ".xlsx": "xlsx", ".xlsm": "xlsx",
    ".pptx": "pptx",
    ".pdf": "pdf",
    ".txt": "text", ".csv": "text", ".md": "text", ".rtf": "text",
    ".zip": "zip",
}
SUPPORTED_EXT = tuple(sorted(_EXT_KIND))

_UNSUPPORTED_NOTES = {
    ".doc": "old Word format (.doc), not read",
    ".dot": "old Word format (.dot), not read",
    ".xls": "old Excel format (.xls), not read",
    ".xlsb": "binary Excel format (.xlsb), not read",
    ".ppt": "old PowerPoint format (.ppt), not read",
    ".dwg": "CAD drawing, not read",
    ".dxf": "CAD drawing, not read",
}

_KIND_LABEL = {"docx": "Word", "xlsx": "Excel", "pptx": "PowerPoint", "pdf": "PDF",
               "text": "text", "zip": "zip"}


class _Unsafe(Exception):
    """The file is damaged or looks hostile; its contents are not read."""


class _TooBig(Exception):
    """A size limit was reached."""


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def is_supported(name):
    """True if ``name`` has an extension extract() can read."""
    return _ext(name) in _EXT_KIND


def extract(name, data=None, path=None):
    """Read one document into a DocText dict. Never raises.

    ``name`` is the file name (its extension picks the reader; the content's
    magic bytes win when they disagree). Give the bytes as ``data`` or a file
    ``path``.
    """
    ext = _ext(name)
    kind = _EXT_KIND.get(ext)
    if kind is None:
        return _new_doc("other", "unsupported", _UNSUPPORTED_NOTES.get(ext, "file type not read"))
    try:
        if data is None:
            if not path:
                return _new_doc(kind, "error", "no file given")
            full = paths.long_path(path)
            if os.path.getsize(full) > DOC_MAX_BYTES:
                return _new_doc(kind, "too_big", _too_big_note())
            with open(full, "rb") as f:
                data = f.read(DOC_MAX_BYTES + 1)
    except Exception:
        return _new_doc(kind, "error", "could not open the file")
    if len(data) > DOC_MAX_BYTES:
        return _new_doc(kind, "too_big", _too_big_note())
    return _extract_data(name, bytes(data), _Limits(), DOC_TEXT_MAX, 0)


def backend_status():
    """Short human description of how PDFs will be read."""
    module = _get_pypdf()
    if module is not None:
        version = str(getattr(module, "__version__", "")).strip()
        return ("PDF: pypdf %s" % version).strip()
    return "PDF: built-in reader (install pypdf for best results)"


# --------------------------------------------------------------------------
# DocText building
# --------------------------------------------------------------------------

def _new_doc(kind, status="ok", note=""):
    return {"kind": kind, "status": status, "note": note, "title": "", "pages": None,
            "drawing": False, "blocks": [], "chars": 0, "reader": "builtin"}


def _too_big_note():
    return "larger than %d MB, not read" % (DOC_MAX_BYTES // (1024 * 1024))


def _ext(name):
    base = re.split(r"[\\/]", str(name or ""))[-1]
    return os.path.splitext(base)[1].lower()


class _Limits(object):
    """Guards shared by everything read from one file (zip bombs, XML bombs)."""

    def __init__(self):
        self.unzip_left = UNZIP_MAX
        self.elements_left = XML_ELEMENTS_MAX


class _Out(object):
    """Collects a document's blocks, keeping at most ``text_max`` characters."""

    def __init__(self, text_max):
        self.blocks = []
        self.chars = 0          # all text seen, kept or not
        self.stored = 0         # text kept
        self.text_max = text_max
        self.full = False

    def add(self, kind, text, level=0, **extra):
        """Add one block (text is tidied; empty text blocks are dropped)."""
        text = _tidy(text)
        if kind in ("heading", "row"):
            text = text.replace("\n", " ")
        if not text and kind in ("heading", "para", "item", "row"):
            return False
        self.chars += len(text)
        if self.full:
            return False
        room = self.text_max - self.stored
        if len(text) > room:
            # The block that reaches the limit is kept up to it (cut at a space), if worth it.
            self.full = True
            cut = text.rfind(" ", 0, room - 1)
            if room < 200 or cut < room // 2:
                return False
            text = text[:cut] + " …"
        self.stored += len(text)
        block = {"type": kind, "text": text, "level": level}
        block.update(extra)
        self.blocks.append(block)
        return True


def _extract_data(name, data, limits, text_max, depth):
    """Read ``data`` (bytes) into a DocText. Never raises."""
    kind = _sniff(name, data)
    out = _Out(text_max)
    doc = _new_doc(kind)
    try:
        if not data:
            doc["status"], doc["note"] = "no_text", "empty file"
        elif kind in ("docx", "xlsx", "pptx"):
            _read_office(kind, data, limits, out, doc)
        elif kind == "pdf":
            _read_pdf(name, data, out, doc)
        elif kind == "text":
            _read_text(name, data, out, doc)
        elif kind == "zip":
            _read_zip(data, limits, out, doc, depth)
        elif kind == "protected":
            doc["kind"] = _EXT_KIND.get(_ext(name), "other")
            doc["status"], doc["note"] = "protected", "password-protected Office file"
        elif kind == "ole":
            doc["kind"] = "other"
            doc["status"], doc["note"] = "unsupported", _old_format_note(_ext(name))
        else:
            ext_kind = _EXT_KIND.get(_ext(name), "other")
            doc["kind"] = ext_kind
            doc["status"] = "error"
            doc["note"] = "not a valid %s file" % _KIND_LABEL.get(ext_kind, "document")
    except _Unsafe as e:
        _failed(doc, out, "error", "not read: %s" % e)
    except _TooBig as e:
        _failed(doc, out, "too_big", "not read: %s" % e)
    except (zipfile.BadZipFile, zlib.error, ET.ParseError, EOFError) as e:
        _failed(doc, out, "error", "damaged file (%s)" % _short_error(e))
    except MemoryError:
        _failed(doc, out, "error", "not read: out of memory")
    except Exception as e:  # never let one document stop a run
        _failed(doc, out, "error", "could not read this file (%s)" % type(e).__name__)
    doc["blocks"] = out.blocks
    doc["chars"] = out.chars
    if doc["status"] == "ok" and not any(_has_content(b) for b in out.blocks):
        doc["status"] = "no_text"
        doc["note"] = doc["note"] or "no text found"
    return doc


def _has_content(block):
    """True for a block that carries document text (sheet names and page marks don't)."""
    if block["type"] == "member":
        return True
    return bool(block["text"]) and block["type"] not in ("sheet", "page")


def _failed(doc, out, status, note):
    """Record a failure; text already read is kept (status stays ok)."""
    if any(_has_content(b) for b in out.blocks) and doc["status"] == "ok":
        doc["note"] = "partly read: " + note.replace("not read: ", "")
        return
    if doc["status"] == "ok":
        doc["status"] = status
        doc["note"] = note


def _short_error(e):
    text = str(e).strip().split("\n")[0]
    return text[:80] if text else type(e).__name__


_OLD_FORMAT_NOTES = {
    ".docx": "old Word format (.doc) renamed .docx, not read",
    ".docm": "old Word format (.doc), not read",
    ".dotx": "old Word format (.dot), not read",
    ".xlsx": "old Excel format (.xls) renamed .xlsx, not read",
    ".xlsm": "old Excel format (.xls), not read",
    ".pptx": "old PowerPoint format (.ppt) renamed .pptx, not read",
}


def _old_format_note(ext):
    """Note for an old binary Office file (OLE) found under a newer extension."""
    return _OLD_FORMAT_NOTES.get(ext, "old Office format, not read")


# --------------------------------------------------------------------------
# Text tidying
# --------------------------------------------------------------------------

_CONTROL = re.compile("[\x00-\x08\x0e-\x1f\x7f\u00ad\u200b\u200c\u200d\u2060\ufeff\ufffe]")
_SPACES = re.compile("[ \t\u00a0\u2000-\u200a\u202f\u205f\u3000]+")
_SURROGATE = re.compile("[\ud800-\udfff]")

# Private-use characters from Symbol/Wingdings fonts, as seen in PDFs and Word.
_PUA = {
    "\uf0b7": "•", "\uf0a7": "▪", "\uf076": "•", "\uf0d8": "•", "\uf0a8": "☐",
    "\uf0fc": "✓", "\uf0fb": "✗", "\uf0fe": "☒", "\uf06e": "■", "\uf06c": "●",
    "\uf02d": "-", "\uf0b0": "°", "\uf0b1": "±", "\uf0b4": "×",
}
_PUA_RE = re.compile("[%s]" % "".join(_PUA))
_LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl",
              "\ufb05": "st", "\ufb06": "st"}
_LIGATURE_RE = re.compile("[%s]" % "".join(_LIGATURES))


def _tidy(text, keep_blank=False):
    """Clean extracted text: no control characters, single spaces, trimmed lines.

    Empty lines are dropped, or with ``keep_blank`` kept as one blank line.
    """
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0b", "\n").replace("\x0c", "\n")
    text = _CONTROL.sub("", text)
    if _SURROGATE.search(text):
        text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    if _PUA_RE.search(text):
        text = _PUA_RE.sub(lambda m: _PUA[m.group(0)], text)
    if _LIGATURE_RE.search(text):
        text = _LIGATURE_RE.sub(lambda m: _LIGATURES[m.group(0)], text)
    text = _SPACES.sub(" ", text)
    if "\n" not in text:
        return text.strip()
    if keep_blank:
        text = "\n".join(line.strip() for line in text.split("\n"))
        return re.sub(r"\n{3,}", "\n\n", text).strip("\n")
    return "\n".join(line.strip() for line in text.split("\n") if line.strip())


# --------------------------------------------------------------------------
# What kind of file is it really? (magic bytes beat the extension)
# --------------------------------------------------------------------------

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ENCRYPTED_PACKAGE = "EncryptedPackage".encode("utf-16-le")


def _sniff(name, data):
    """The kind to read ``data`` as: docx/xlsx/pptx/pdf/text/zip, "ole" or "protected"."""
    ext_kind = _EXT_KIND.get(_ext(name), "other")
    if not data:
        return ext_kind
    head = data[:1024]
    if data.lstrip()[:5] == b"%PDF-" or (ext_kind != "text" and b"%PDF-" in head):
        return "pdf"
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return _zip_kind(data, ext_kind)
    if data[:8] == _OLE_MAGIC:
        # Password-protected .docx/.xlsx/.pptx files are OLE files holding an
        # "EncryptedPackage" stream; anything else is an old binary Office file.
        if ext_kind in ("docx", "xlsx", "pptx") and _ENCRYPTED_PACKAGE in data[:4 * 1024 * 1024]:
            return "protected"
        return "ole"
    if data.lstrip()[:5] == b"{\\rtf":
        return "text"
    if ext_kind in ("docx", "xlsx", "pptx", "zip", "pdf"):
        return "bad"
    return ext_kind


def _zip_kind(data, ext_kind):
    """docx/xlsx/pptx when the zip is an Office file, else zip."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        names = set(n.replace("\\", "/").lower() for n in zf.namelist()[:ZIP_ENTRIES_MAX])
    except Exception:
        return ext_kind if ext_kind in ("docx", "xlsx", "pptx", "zip") else "zip"
    if "[content_types].xml" in names:     # every Office file has one; a plain zip doesn't
        for kind, folder in (("docx", "word/"), ("xlsx", "xl/"), ("pptx", "ppt/")):
            if any(n.startswith(folder) for n in names):
                return kind
    if ext_kind in ("docx", "xlsx", "pptx"):
        return ext_kind     # a zip but not an Office file: reported as not valid
    return "zip"


# --------------------------------------------------------------------------
# Safe zip and XML reading
# --------------------------------------------------------------------------

def _open_zip(data):
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, EOFError, OSError,
            NotImplementedError, struct.error) as e:
        raise _Unsafe("damaged zip file (%s)" % _short_error(e))
    if len(infos) > ZIP_ENTRIES_MAX:
        raise _Unsafe("too many files inside")
    return zf


def _part_map(zf):
    """Lower-case part name (forward slashes) -> ZipInfo. Office part names ignore case."""
    parts = {}
    for info in zf.infolist():
        key = info.filename.replace("\\", "/").lstrip("/").lower()
        if key and not key.endswith("/"):
            parts.setdefault(key, info)
    return parts


def _zip_chunks(zf, info, limits, part_max):
    """Yield the unzipped bytes of one member in chunks, enforcing the size guards."""
    if info.flag_bits & 0x1:
        raise _Unsafe("encrypted part inside the file")
    if info.file_size > part_max:
        raise _TooBig("a part is too large (%d MB)" % (info.file_size // (1024 * 1024)))
    if info.file_size > info.compress_size * RATIO_MAX + 1024 * 1024:
        raise _Unsafe("suspicious compression (zip bomb?)")
    seen = 0
    allowed = info.compress_size * RATIO_MAX + 1024 * 1024
    with zf.open(info) as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                return
            seen += len(chunk)
            limits.unzip_left -= len(chunk)
            if seen > part_max:
                raise _TooBig("a part is too large")
            if seen > allowed:
                raise _Unsafe("suspicious compression (zip bomb?)")
            if limits.unzip_left < 0:
                raise _TooBig("too much data inside the file")
            yield chunk


def _read_member(zf, info, limits, max_bytes):
    """The whole unzipped member as bytes (at most max_bytes)."""
    return b"".join(_zip_chunks(zf, info, limits, max_bytes))


_DTD_MARKERS = [m.encode(enc) for m in ("<!DOCTYPE", "<!ENTITY")
                for enc in ("ascii", "utf-16-le", "utf-16-be")]
_XML_ENCODING = re.compile(r"""^\s*<\?xml[^>]*?encoding\s*=\s*["']([A-Za-z0-9._-]+)""")
_SAFE_ENCODINGS = set(["utf-8", "utf8", "utf-16", "utf16", "utf-16le", "utf-16be", "us-ascii",
                       "ascii", "iso-8859-1", "latin-1", "latin1", "windows-1252", "cp1252"])


def _check_xml_head(head):
    """Refuse XML whose declared encoding could hide a DTD from the byte check."""
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = head[:400].decode("utf-16", "ignore")
    elif head[:1] == b"\x00" or head[1:2] == b"\x00":
        text = head[:400].decode("utf-16-be" if head[:1] == b"\x00" else "utf-16-le", "ignore")
    else:
        text = head[:400].decode("latin-1")
    text = text.lstrip("\ufeff\u00ef\u00bb\u00bf")
    m = _XML_ENCODING.match(text)
    if m and m.group(1).lower() not in _SAFE_ENCODINGS:
        raise _Unsafe("XML in an unusual encoding")


def _xml_events(chunks, limits, part_max=XML_PART_MAX):
    """Parse XML from byte chunks, yielding ("end", element, parent) for each element.

    ("chunk", None, None) is yielded after each chunk's elements. ``parent`` is
    None for the root, so a reader can remove finished elements from their
    parent and keep memory flat. Refuses DTDs and entity declarations; limits
    size, nesting depth and the number of elements.
    """
    parser = ET.XMLPullParser(events=("start", "end"))
    stack = []
    seen = 0
    tail = b""
    first = True
    for chunk in chunks:
        if first:
            _check_xml_head(chunk)
            first = False
        window = tail + chunk
        for marker in _DTD_MARKERS:
            if marker in window:
                raise _Unsafe("XML with a DTD (possible entity bomb)")
        tail = window[-20:]
        seen += len(chunk)
        if seen > part_max:
            raise _TooBig("a part is too large")
        parser.feed(chunk)
        for event, el in parser.read_events():
            if event == "start":
                stack.append(el)
                if len(stack) > XML_DEPTH_MAX:
                    raise _Unsafe("XML nested too deeply")
                continue
            if stack:
                stack.pop()
            limits.elements_left -= 1
            if limits.elements_left < 0:
                raise _TooBig("too much XML")
            yield "end", el, (stack[-1] if stack else None)
        yield "chunk", None, None
    parser.close()


def _parse_part(zf, parts, name, limits, part_max=XML_PART_MAX):
    """Parse a whole XML part and return its root element (None if missing)."""
    info = parts.get((name or "").lower())
    if info is None:
        return None
    root = None
    for event, el, parent in _xml_events(_zip_chunks(zf, info, limits, part_max), limits, part_max):
        if event == "end" and parent is None:
            root = el
    return root


def _try_part(zf, parts, name, limits, part_max=16 * 1024 * 1024):
    """_parse_part, but a damaged, hostile or missing part gives None."""
    try:
        return _parse_part(zf, parts, name, limits, part_max)
    except (_Unsafe, _TooBig, ET.ParseError, zipfile.BadZipFile, zlib.error, EOFError,
            RuntimeError, ValueError, OSError):
        return None


_LOCAL = {}


def _local(tag):
    """Tag without its namespace: '{ns}p' -> 'p'."""
    try:
        return _LOCAL[tag]
    except KeyError:
        name = tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""
        if len(_LOCAL) < 5000:
            _LOCAL[tag] = name
        return name


def _attr(el, name):
    """Attribute by local name, ignoring its namespace."""
    if el is None:
        return None
    value = el.get(name)
    if value is not None:
        return value
    suffix = "}" + name
    for key, value in el.attrib.items():
        if key.endswith(suffix):
            return value
    return None


def _rid(el):
    """The relationship id (r:id) of an element, ignoring a plain 'id' attribute."""
    for key, value in el.attrib.items():
        if key.startswith("{") and key.endswith("}id"):
            return value
    return None


def _child(el, name):
    """First child element with this local name, or None."""
    if el is None:
        return None
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _children(el, name):
    return [c for c in el if _local(c.tag) == name] if el is not None else []


def _val(el, name):
    """The w:val (or val) attribute of el's child ``name``."""
    return _attr(_child(el, name), "val")


def _all_text(el):
    """Text of every 't' element below el ('br' -> new line, 'tab' -> space)."""
    out = []

    def walk(node, depth):
        if depth > XML_DEPTH_MAX:
            return
        for c in node:
            tag = _local(c.tag)
            if tag == "t":
                out.append(c.text or "")
            elif tag == "br":
                out.append("\n")
            elif tag == "tab":
                out.append(" ")
            else:
                walk(c, depth + 1)
    walk(el, 0)
    return "".join(out)


def _rels(zf, parts, part, limits):
    """Relationships of a part: list of (id, type suffix, target part name lower-cased)."""
    d, b = posixpath.split(part)
    root = _try_part(zf, parts, posixpath.join(d, "_rels", b + ".rels"), limits)
    result = []
    if root is None:
        return result
    for rel in root:
        if _local(rel.tag) != "Relationship" or (rel.get("TargetMode") or "").lower() == "external":
            continue
        target = unquote((rel.get("Target") or "").replace("\\", "/"))
        if target.startswith("/"):
            path = target.lstrip("/")
        else:
            path = posixpath.normpath(posixpath.join(d, target))
        rel_type = (rel.get("Type") or "").rsplit("/", 1)[-1]
        result.append((rel.get("Id") or "", rel_type, path.lower()))
    return result


def _rel_targets(rels, rel_type):
    return [target for _id, t, target in rels if t == rel_type]


def _main_part(zf, parts, limits, default):
    """The main document part named by the package relationships."""
    for _id, rel_type, target in _rels(zf, parts, "", limits):
        if rel_type == "officeDocument" and target in parts:
            return target
    return default


def _properties(zf, parts, limits, doc):
    """The title from docProps/core.xml."""
    package = _rels(zf, parts, "", limits)
    core = (_rel_targets(package, "core-properties") or ["docprops/core.xml"])[0]
    root = _try_part(zf, parts, core, limits, 1024 * 1024)
    if root is not None:
        for el in root:
            if _local(el.tag) == "title" and el.text:
                doc["title"] = _tidy(el.text)[:300]


def _read_office(kind, data, limits, out, doc):
    zf = _open_zip(data)
    parts = _part_map(zf)
    _properties(zf, parts, limits, doc)
    if kind == "docx":
        _read_docx(zf, parts, limits, out)
    elif kind == "xlsx":
        _read_xlsx(zf, parts, limits, out, doc)
    else:
        _read_pptx(zf, parts, limits, out, doc)


# --------------------------------------------------------------------------
# Word (.docx)
# --------------------------------------------------------------------------

# Subtrees of a paragraph that hold no visible text (or deleted text).
_W_SKIP = frozenset([
    "del", "moveFrom", "instrText", "delInstrText", "pPr", "rPr", "sdtPr", "tblPr",
    "tcPr", "trPr", "tblGrid", "Fallback", "fldData", "footnoteRef", "endnoteRef",
    "annotationRef", "commentRangeStart", "commentRangeEnd", "bookmarkStart",
    "bookmarkEnd", "proofErr", "lastRenderedPageBreak", "docPr", "object",
    "permStart", "permEnd", "customXmlPr", "smartTagPr",
])

# Wrappers that may hold paragraphs or table rows/cells.
_W_WRAPPERS = frozenset(["sdt", "sdtContent", "customXml", "smartTag", "ins", "moveTo"])

# w:sym characters in Symbol / Wingdings fonts (code minus 0xF000 when >= 0xF000).
_SYMBOL_FONT = {
    0x61: "α", 0x62: "β", 0x63: "χ", 0x64: "δ", 0x65: "ε", 0x66: "φ", 0x67: "γ",
    0x68: "η", 0x6C: "λ", 0x6D: "μ", 0x70: "π", 0x71: "θ", 0x72: "ρ", 0x73: "σ",
    0x74: "τ", 0x77: "ω", 0x44: "Δ", 0x46: "Φ", 0x53: "Σ", 0x57: "Ω", 0xA3: "≤",
    0xB3: "≥", 0xB0: "°", 0xB1: "±", 0xB4: "×", 0xB8: "÷", 0xB9: "≠", 0xBB: "≈",
    0xB7: "•", 0xD6: "√", 0xA5: "∞", 0xAE: "→", 0xDE: "⇒", 0x2D: "-", 0x3D: "=",
}
_WINGDINGS = {0xFC: "✓", 0xFB: "✗", 0xFD: "☒", 0xFE: "☑", 0xA8: "☐", 0x6F: "☐",
              0x6E: "■", 0x6C: "●", 0xA7: "▪", 0x9F: "•", 0xD8: "➢", 0xE0: "→", 0x77: "♦"}


def _sym_char(el):
    """Text for a w:sym (a character in a symbol font)."""
    font = (_attr(el, "font") or "").lower()
    try:
        code = int(_attr(el, "char") or "", 16)
    except ValueError:
        return ""
    if code >= 0xF000:
        code -= 0xF000
    if "wingdings" in font:
        return _WINGDINGS.get(code, "")
    if "symbol" in font:
        return _SYMBOL_FONT.get(code, chr(code) if 0x20 < code < 0x7F else "")
    if 0x20 <= code < 0xD800:
        return chr(code)
    return ""


class _WordContext(object):
    """What the paragraph reader needs from the rest of the package."""

    def __init__(self):
        self.styles = {}          # style id -> dict(name, based, outline, num_id, ilvl)
        self.default_style = None
        self.numbering = None
        self.comments = {}        # id -> text
        self.notes = {"f": {}, "e": {}}      # footnote/endnote id -> text
        self.note_order = {"f": [], "e": []}  # ids in order of first reference
        self.comments_done = set()
        self._resolved = {}

    def style(self, style_id):
        """Resolved style: own name; outline level and numbering inherited via basedOn."""
        key = style_id or self.default_style
        if key in self._resolved:
            return self._resolved[key]
        info = {"name": "", "outline": None, "num_id": None, "ilvl": None}
        sid = key
        hops = 0
        while sid and sid in self.styles and hops < 20:
            st = self.styles[sid]
            if hops == 0:
                info["name"] = st["name"]
            for k in ("outline", "num_id", "ilvl"):
                if info[k] is None and st[k] is not None:
                    info[k] = st[k]
            sid = st["based"]
            hops += 1
        self._resolved[key] = info
        return info

    def note_mark(self, kind, note_id):
        if note_id not in self.notes[kind]:
            return ""
        order = self.note_order[kind]
        if note_id not in order:
            order.append(note_id)
        n = order.index(note_id) + 1
        return "[%d]" % n if kind == "f" else "[e%d]" % n

    def comment_mark(self, comment_id):
        text = self.comments.get(comment_id)
        if not text or comment_id in self.comments_done:
            return ""
        self.comments_done.add(comment_id)
        return " [comment: %s]" % text


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _w_styles(root, ctx):
    if root is None:
        return
    for st in root:
        if _local(st.tag) != "style":
            continue
        sid = _attr(st, "styleId")
        if not sid:
            continue
        ppr = _child(st, "pPr")
        num = _child(ppr, "numPr")
        ctx.styles[sid] = {
            "name": (_val(st, "name") or sid).strip().lower(),
            "based": _val(st, "basedOn"),
            "outline": _int_or_none(_val(ppr, "outlineLvl")),
            "num_id": _val(num, "numId"),
            "ilvl": _int_or_none(_val(num, "ilvl")),
        }
        if _attr(st, "type") == "paragraph" and _attr(st, "default") in ("1", "true", "on"):
            ctx.default_style = sid


class _Numbering(object):
    """Works out list labels ("1.", "a)", "6.2", "•") the way Word shows them."""

    def __init__(self, root, ctx):
        self.levels = {}      # abstractNumId -> {ilvl: (start, fmt, text, legal)}
        self.links = {}       # abstractNumId -> numbering style id (numStyleLink)
        self.nums = {}        # numId -> (abstractNumId, {ilvl: start override})
        self.counters = {}    # abstractNumId -> [value or None] * 9
        self.used = set()     # numIds already seen (start overrides apply once)
        self.ctx = ctx
        if root is None:
            return
        for el in root:
            tag = _local(el.tag)
            if tag == "abstractNum":
                aid = _attr(el, "abstractNumId")
                levels = {}
                for lvl in _children(el, "lvl"):
                    ilvl = _int_or_none(_attr(lvl, "ilvl"))
                    if ilvl is None or not 0 <= ilvl <= 8:
                        continue
                    start = _int_or_none(_val(lvl, "start"))
                    levels[ilvl] = (1 if start is None else start, _val(lvl, "numFmt") or "decimal",
                                    _val(lvl, "lvlText") or "", _child(lvl, "isLgl") is not None)
                self.levels[aid] = levels
                link = _val(el, "numStyleLink")
                if link:
                    self.links[aid] = link
            elif tag == "num":
                overrides = {}
                for ov in _children(el, "lvlOverride"):
                    ilvl = _int_or_none(_attr(ov, "ilvl"))
                    start = _int_or_none(_val(ov, "startOverride"))
                    if ilvl is not None and start is not None:
                        overrides[ilvl] = start
                self.nums[_attr(el, "numId")] = (_val(el, "abstractNumId"), overrides)

    def _abstract(self, num_id):
        """abstractNumId for a numId, following one numStyleLink hop."""
        entry = self.nums.get(num_id)
        if entry is None:
            return None
        aid = entry[0]
        if not self.levels.get(aid) and aid in self.links:
            style = self.ctx.styles.get(self.links[aid])
            if style and style["num_id"] in self.nums:
                aid = self.nums[style["num_id"]][0]
        return aid

    def label(self, num_id, ilvl):
        """Advance the list counter and return the paragraph's label ("" if none)."""
        aid = self._abstract(num_id)
        levels = self.levels.get(aid)
        if not levels or ilvl not in levels:
            return ""
        counters = self.counters.setdefault(aid, [None] * 9)
        if num_id not in self.used:
            self.used.add(num_id)
            for lvl, start in self.nums[num_id][1].items():
                if 0 <= lvl <= 8:
                    counters[lvl] = start - 1
        start, fmt, text, legal = levels[ilvl]
        counters[ilvl] = start if counters[ilvl] is None else counters[ilvl] + 1
        for deeper in range(ilvl + 1, 9):
            counters[deeper] = None
        if fmt == "bullet":
            return "•"
        if fmt == "none" or not text:
            return ""

        def number(m):
            k = int(m.group(1)) - 1
            if not 0 <= k <= 8:
                return ""
            lvl = levels.get(k, (1, "decimal", "", False))
            value = counters[k] if counters[k] is not None else lvl[0]
            return _format_number(value, "decimal" if (legal and k < ilvl) else lvl[1])
        return re.sub(r"%([1-9])", number, text).strip()


def _format_number(n, fmt):
    """A list number in Word's numFmt style."""
    if fmt in ("lowerLetter", "upperLetter") and n > 0:
        s = chr(ord("a") + (n - 1) % 26) * ((n - 1) // 26 + 1)
        return s.upper() if fmt == "upperLetter" else s
    if fmt in ("lowerRoman", "upperRoman") and 0 < n < 4000:
        s = _roman(n)
        return s.upper() if fmt == "upperRoman" else s
    if fmt == "decimalZero":
        return "%02d" % n
    return str(n)


def _roman(n):
    out = []
    for value, letters in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"),
                           (90, "xc"), (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"),
                           (4, "iv"), (1, "i")):
        while n >= value:
            out.append(letters)
            n -= value
    return "".join(out)


def _w_inline(el, ctx, out, extra, depth):
    """Collect a paragraph's text into ``out``; text boxes found go to ``extra``."""
    if depth > XML_DEPTH_MAX:
        return
    for child in el:
        tag = _local(child.tag)
        if tag in _W_SKIP:
            continue
        if tag == "t":
            if child.text:
                out.append(child.text)
        elif tag in ("tab", "ptab"):
            out.append(" ")
        elif tag in ("br", "cr"):
            out.append("\n")
        elif tag == "noBreakHyphen":
            out.append("-")
        elif tag == "softHyphen":
            continue
        elif tag == "sym":
            out.append(_sym_char(child))
        elif tag == "footnoteReference":
            out.append(ctx.note_mark("f", _attr(child, "id")))
        elif tag == "endnoteReference":
            out.append(ctx.note_mark("e", _attr(child, "id")))
        elif tag == "commentReference":
            out.append(ctx.comment_mark(_attr(child, "id")))
        elif tag == "txbxContent":
            for block in child:
                _w_block(block, ctx, lambda k, t, l: extra.append((k, t, l)), depth + 1)
        elif tag == "AlternateContent":
            choice = _child(child, "Choice")
            if choice is not None:
                _w_inline(choice, ctx, out, extra, depth + 1)
        else:
            _w_inline(child, ctx, out, extra, depth + 1)


_HEADING_NAME = re.compile(r"^heading ([1-9])$")
_TOC_NAME = re.compile(r"^(toc [1-9]|contents [1-9]|table of figures)$")


def _w_paragraph(p, ctx, depth):
    """(kind, text, level, extra blocks) for a w:p, or None for a contents-list entry."""
    ppr = _child(p, "pPr")
    info = ctx.style(_val(ppr, "pStyle"))
    name = info["name"]
    if _TOC_NAME.match(name):
        return None
    texts, extra = [], []
    _w_inline(p, ctx, texts, extra, depth + 1)
    text = "".join(texts)

    num = _child(ppr, "numPr")
    num_id = _val(num, "numId") if num is not None else None
    ilvl = _int_or_none(_val(num, "ilvl")) if num is not None else None
    if num_id is None:
        num_id = info["num_id"]
    if ilvl is None:
        ilvl = info["ilvl"] if info["ilvl"] is not None else 0
    label = ""
    if num_id and num_id != "0" and ctx.numbering is not None:
        label = ctx.numbering.label(num_id, max(0, min(8, ilvl)))

    outline = _int_or_none(_val(ppr, "outlineLvl"))
    if outline is None:
        outline = info["outline"]
    level = 0
    m = _HEADING_NAME.match(name)
    if m:
        level = int(m.group(1))
    elif name == "title":
        level = 1
    elif outline is not None and 0 <= outline <= 8:
        level = outline + 1
    clean = _tidy(text)
    if label and clean:
        clean = label + " " + clean
    if level and clean and len(clean) <= 300:
        return ("heading", clean, level, extra)
    if label and clean:
        return ("item", clean, max(0, min(8, ilvl)), extra)
    return ("para", clean, 0, extra)


def _w_block(el, ctx, emit, depth):
    """Emit blocks for a body-level element (paragraph, table or a wrapper)."""
    if depth > XML_DEPTH_MAX:
        return
    tag = _local(el.tag)
    if tag == "p":
        result = _w_paragraph(el, ctx, depth)
        if result is None:
            return
        kind, text, level, extra = result
        if text:
            emit(kind, text, level)
        for block in extra:
            emit(*block)
    elif tag == "tbl":
        _w_table(el, ctx, emit, depth)
    elif tag == "sdt" and _w_is_toc(el):
        return      # a table of contents repeats the headings
    elif tag in _W_WRAPPERS:
        for child in el:
            _w_block(child, ctx, emit, depth + 1)
    elif tag == "AlternateContent":
        choice = _child(el, "Choice")
        if choice is not None:
            for child in choice:
                _w_block(child, ctx, emit, depth + 1)


def _w_is_toc(sdt):
    """True for a content control holding a table of contents (Word and LibreOffice)."""
    gallery = _child(_child(_child(sdt, "sdtPr"), "docPartObj"), "docPartGallery")
    return "table of contents" in (_attr(gallery, "val") or "").lower()


def _w_unwrap(el, name, depth=0):
    """Children called ``name``, looking through content-control wrappers."""
    found = []
    for c in el:
        tag = _local(c.tag)
        if tag == name:
            found.append(c)
        elif tag in _W_WRAPPERS and depth < 10:
            found.extend(_w_unwrap(c, name, depth + 1))
    return found


def _w_table(tbl, ctx, emit, depth):
    """Emit one "row" block per table row: non-empty cells joined with ' | '."""
    index = 0
    for tr in _w_unwrap(tbl, "tr"):
        cells = []
        for tc in _w_unwrap(tr, "tc"):
            tcpr = _child(tc, "tcPr")
            merged = [m for m in (_child(tcpr, "vMerge"), _child(tcpr, "hMerge")) if m is not None]
            if merged and _attr(merged[0], "val") != "restart":
                continue  # continuation of a merged cell (its text is in the first cell)
            items = []
            for child in tc:
                _w_block(child, ctx, lambda k, t, l: items.append((k, t)), depth + 1)
            cell = " ".join((t + ";") if k == "row" else t for k, t in items)
            cell = _tidy(cell.rstrip(";")).replace("\n", " ")
            if cell:
                cells.append(cell)
        if cells:
            emit("row", " | ".join(cells), index)
            index += 1


def _w_part_texts(root, ctx):
    """Texts of the paragraphs and table rows in a header, footer or note."""
    texts = []
    if root is not None:
        for child in root:
            _w_block(child, ctx, lambda k, t, l: texts.append(t), 1)
    return texts


_PAGE_NUMBER_LINE = re.compile(r"(?i)^(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?$")


def _read_docx(zf, parts, limits, out):
    main = _main_part(zf, parts, limits, "word/document.xml")
    if main not in parts:
        raise _Unsafe("not a valid Word file")
    rels = _rels(zf, parts, main, limits)
    ctx = _WordContext()
    _w_styles(_try_part(zf, parts, (_rel_targets(rels, "styles") or ["word/styles.xml"])[0], limits), ctx)
    ctx.numbering = _Numbering(_try_part(zf, parts, (_rel_targets(rels, "numbering") or
                                                     ["word/numbering.xml"])[0], limits), ctx)
    for target in _rel_targets(rels, "comments"):
        root = _try_part(zf, parts, target, limits)
        for c in (root if root is not None else []):
            if _local(c.tag) == "comment":
                text = " ".join(_w_part_texts(c, ctx))
                if text:
                    ctx.comments[_attr(c, "id")] = _tidy(text).replace("\n", " ")
    for kind, rel_type in (("f", "footnotes"), ("e", "endnotes")):
        for target in _rel_targets(rels, rel_type):
            root = _try_part(zf, parts, target, limits)
            for note in (root if root is not None else []):
                if _local(note.tag) in ("footnote", "endnote") and \
                        (_attr(note, "type") or "normal") == "normal":
                    text = _tidy(" ".join(_w_part_texts(note, ctx))).replace("\n", " ")
                    if text:
                        ctx.notes[kind][_attr(note, "id")] = text

    # The body, streamed so a huge document does not fill memory.
    blocks = 0
    try:
        info = parts[main]
        for event, el, parent in _xml_events(_zip_chunks(zf, info, limits, UNZIP_MAX), limits,
                                             XML_PART_MAX):
            if event == "end" and parent is not None and _local(parent.tag) == "body":
                _w_block(el, ctx, out.add, 0)
                parent.remove(el)
                blocks += 1
                if out.full or blocks >= BODY_BLOCKS_MAX:
                    break       # text limit reached: the rest is not read
    except (_TooBig, ET.ParseError) as e:
        if not out.blocks:
            raise
        out.add("para", "(rest of the document not read: %s)" % _short_error(e))

    # Footnotes and endnotes, in the order they are referenced.
    for kind in ("f", "e"):
        for n, note_id in enumerate(ctx.note_order[kind], 1):
            mark = "[%d]" % n if kind == "f" else "[e%d]" % n
            out.add("para", "%s %s" % (mark, ctx.notes[kind][note_id]))

    # Headers and footers once each (page-number-only lines dropped).
    for label, rel_type in (("Header", "header"), ("Footer", "footer")):
        lines = []
        for target in _rel_targets(rels, rel_type):
            for text in _w_part_texts(_try_part(zf, parts, target, limits), ctx):
                for line in _tidy(text).split("\n"):
                    if line and line not in lines and not _PAGE_NUMBER_LINE.match(line):
                        lines.append(line)
        if lines:
            out.add("para", "%s: %s" % (label, " | ".join(lines))[:2000])


# --------------------------------------------------------------------------
# Excel (.xlsx, .xlsm)
# --------------------------------------------------------------------------

_BUILTIN_FORMATS = {9: "percent:0", 10: "percent:2", 14: "date", 15: "date", 16: "date", 17: "date",
                    18: "time", 19: "time", 20: "time", 21: "time", 22: "datetime",
                    45: "time", 46: "elapsed", 47: "time"}


def _format_kind(code):
    """"date", "datetime", "time", "elapsed", "percent:<decimals>" or None for a format code."""
    if not code:
        return None
    c = re.sub(r'"[^"]*"', "", code)
    c = re.sub(r"\\.", "", c)
    c = re.sub(r"[_*].", "", c)
    elapsed = bool(re.search(r"(?i)\[(h+|m+|s+)\]", c))
    c = re.sub(r"\[[^\]]*\]", "", c)
    section = c.split(";")[0].lower().replace("general", "")
    has_date = bool(re.search(r"[dy]", section)) or (
        "m" in section and not re.search(r"[0#?hs]", section))
    has_time = bool(re.search(r"[hs]", section))
    if elapsed:
        return "elapsed"
    if has_date and has_time:
        return "datetime"
    if has_date:
        return "date"
    if has_time:
        return "time"
    if "%" in section:
        decimals = re.search(r"\.([0#?]+)", section)
        return "percent:%d" % (len(decimals.group(1)) if decimals else 0)
    return None


def _xlsx_styles(root):
    """Kind of number format for each cell style index (cellXfs)."""
    if root is None:
        return []
    custom = {}
    kinds = []
    for el in root:
        tag = _local(el.tag)
        if tag == "numFmts":
            for f in el:
                fid = _int_or_none(f.get("numFmtId"))
                if fid is not None:
                    custom[fid] = f.get("formatCode") or ""
        elif tag == "cellXfs":
            for xf in el:
                fid = _int_or_none(xf.get("numFmtId")) or 0
                if fid in custom:
                    kinds.append(_format_kind(custom[fid]))
                else:
                    kinds.append(_BUILTIN_FORMATS.get(fid))
    return kinds


_XL_ESCAPE = re.compile(r"_x([0-9A-Fa-f]{4})_")


def _xl_unescape(text):
    """Undo Excel's _xHHHH_ escapes (e.g. _x000D_ for a carriage return)."""
    if "_x" not in text:
        return text
    return _XL_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)


def _si_text(si):
    """Text of a shared string or inline string (rich text runs joined, phonetics skipped)."""
    parts = []
    for c in si:
        tag = _local(c.tag)
        if tag == "t":
            parts.append(c.text or "")
        elif tag == "r":
            t = _child(c, "t")
            if t is not None:
                parts.append(t.text or "")
    return _xl_unescape("".join(parts))


def _xlsx_shared_strings(zf, parts, name, limits):
    info = parts.get(name)
    strings = []
    if info is None:
        return strings
    chars = 0
    try:
        for event, el, parent in _xml_events(_zip_chunks(zf, info, limits, UNZIP_MAX), limits,
                                             UNZIP_MAX):
            if event == "end" and _local(el.tag) == "si":
                text = _si_text(el)
                strings.append(text)
                chars += len(text)
                el.clear()
                if parent is not None:
                    parent.remove(el)
                if len(strings) >= SHARED_STRINGS_MAX or chars >= SHARED_CHARS_MAX:
                    break
    except (_TooBig, _Unsafe, ET.ParseError):
        pass    # strings not loaded show as empty cells
    return strings


def format_number(text):
    """Excel's stored number without float noise: '0.30000000000000004' -> '0.3'."""
    try:
        f = float(text)
    except (TypeError, ValueError):
        return (text or "").strip()
    if f != f or f in (float("inf"), float("-inf")):
        return text.strip()
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    s = "%.10g" % f
    if "e" in s and 1e-6 <= abs(f) < 1e15:
        try:
            s = format(Decimal(s), "f")
        except InvalidOperation:
            pass
    if "." in s and "e" not in s:
        s = s.rstrip("0").rstrip(".")
    return s


def excel_date(serial, date1904=False, kind="date"):
    """Text for an Excel date/time serial number, or None if it is out of range."""
    if serial < 0 or serial > 2958465:
        return None
    if date1904:
        base = datetime(1904, 1, 1)
    elif serial < 61:
        base = datetime(1899, 12, 31)   # Excel thinks 1900 was a leap year
    else:
        base = datetime(1899, 12, 30)
    days = int(serial)
    seconds = int(round((serial - days) * 86400))
    if seconds >= 86400:
        days, seconds = days + 1, 0
    if kind == "elapsed":
        total = int(round(serial * 86400))
        return "%d:%02d" % (total // 3600, (total % 3600) // 60)
    if kind == "time":
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return "%02d:%02d:%02d" % (h, m, s) if s else "%02d:%02d" % (h, m)
    try:
        dt = base + timedelta(days=days, seconds=seconds)
    except OverflowError:
        return None
    if kind == "datetime" and seconds:
        return dt.strftime("%Y-%m-%d %H:%M")
    return "%04d-%02d-%02d" % (dt.year, dt.month, dt.day)


def _cell_text(c, shared, styles, date1904):
    """Displayed value of a worksheet cell (cached values only, never formulas)."""
    t = c.get("t") or "n"
    v = None
    inline = None
    for child in c:
        tag = _local(child.tag)
        if tag == "v":
            v = child.text
        elif tag == "is":
            inline = child
    if t == "inlineStr":
        return _si_text(inline) if inline is not None else (v or "")
    if v is None:
        return ""
    if t == "s":
        i = _int_or_none(v.strip())
        return shared[i] if i is not None and 0 <= i < len(shared) else ""
    if t == "b":
        return "TRUE" if v.strip() in ("1", "true") else "FALSE"
    if t in ("str", "e"):
        return _xl_unescape(v)
    if t == "d":
        m = re.match(r"(\d{4}-\d{2}-\d{2})(?:T(\d{2}:\d{2}))?", v.strip())
        if not m:
            return v.strip()
        if m.group(2) and m.group(2) != "00:00":
            return "%s %s" % (m.group(1), m.group(2))
        return m.group(1)
    style = _int_or_none(c.get("s"))
    kind = styles[style] if style is not None and 0 <= style < len(styles) else None
    if kind:
        try:
            f = float(v)
        except ValueError:
            return v.strip()
        if kind.startswith("percent:"):
            return format_number("%.*f" % (int(kind[8:]), f * 100)) + "%"
        text = excel_date(f, date1904, kind)
        if text is not None:
            return text
    return format_number(v)


def _col_index(ref):
    """Zero-based column from a cell reference like 'AB12' (None if absent)."""
    n = 0
    for ch in ref or "":
        if "A" <= ch <= "Z":
            n = n * 26 + ord(ch) - 64
        elif "a" <= ch <= "z":
            n = n * 26 + ord(ch) - 96
        else:
            break
    return n - 1 if n else None


def _count_rows(chunks):
    """Count the remaining non-empty rows in raw worksheet XML without parsing it."""
    count = 0
    tails = {b"</row>": b"", b":row>": b""}
    try:
        for chunk in chunks:
            for pat in tails:
                window = tails[pat] + chunk
                count += window.count(pat)
                tails[pat] = window[-(len(pat) - 1):]
    except (_TooBig, _Unsafe, zlib.error, zipfile.BadZipFile, EOFError, RuntimeError):
        pass
    return count


def _xlsx_sheet(zf, info, shared, styles, date1904, limits, out):
    """Rows of one worksheet, streamed: (rows kept as lists of cells, total non-empty rows)."""
    kept = []
    total = 0
    counting = False
    sheet_chars = 0
    room = out.text_max - out.stored
    row = {}
    next_col = 0
    chunks = _zip_chunks(zf, info, limits, UNZIP_MAX)
    try:
        for event, el, parent in _xml_events(chunks, limits, XML_PART_MAX):
            if event == "chunk":
                if counting:
                    break
                continue
            tag = _local(el.tag)
            if tag == "c":
                col = _col_index(el.get("r"))
                if col is None:
                    col = next_col
                next_col = col + 1
                if counting:
                    if _child(el, "v") is not None or _child(el, "is") is not None:
                        row[col] = "x"
                else:
                    text = _tidy(_cell_text(el, shared, styles, date1904)).replace("\n", " ")
                    if text:
                        row[col] = text
                el.clear()
            elif tag == "row":
                if row:
                    total += 1
                    if not counting:
                        sheet_chars += sum(len(x) + 3 for x in row.values())
                        kept.append(row)
                        if len(kept) >= SHEET_ROWS_MAX or sheet_chars >= room:
                            counting = True
                el.clear()
                if parent is not None:
                    parent.remove(el)
                row = {}
                next_col = 0
            elif tag == "sheetData":
                break
    except _TooBig:
        counting = True
    if counting:
        total += _count_rows(chunks)
    return kept, total


def _render_rows(rows):
    """Lists of cells for rows: fully empty columns dropped, trailing empty cells trimmed."""
    used = sorted(set(col for row in rows for col in row))
    result = []
    for row in rows:
        cells = [row.get(col, "") for col in used]
        while cells and not cells[-1]:
            cells.pop()
        result.append(" | ".join(cells).strip())
    return result


def _read_xlsx(zf, parts, limits, out, doc):
    main = _main_part(zf, parts, limits, "xl/workbook.xml")
    wb = _parse_part(zf, parts, main, limits, 16 * 1024 * 1024)
    if wb is None:
        raise _Unsafe("not a valid Excel file")
    rels = dict((rid, (t, target)) for rid, t, target in _rels(zf, parts, main, limits))
    date1904 = False
    sheets = []
    for el in wb:
        tag = _local(el.tag)
        if tag == "workbookPr":
            date1904 = (el.get("date1904") or "").lower() in ("1", "true")
        elif tag == "sheets":
            for s in el:
                if _local(s.tag) == "sheet":
                    sheets.append((s.get("name") or "", (s.get("state") or "visible").lower(),
                                   _rid(s)))
    shared_name = ([target for t, target in rels.values() if t == "sharedStrings"]
                   or ["xl/sharedstrings.xml"])[0]
    styles_name = ([target for t, target in rels.values() if t == "styles"] or ["xl/styles.xml"])[0]
    shared = _xlsx_shared_strings(zf, parts, shared_name, limits)
    styles = _xlsx_styles(_try_part(zf, parts, styles_name, limits))
    doc["pages"] = len(sheets)
    for n, (name, state, rid) in enumerate(sheets, 1):
        rel_type, target = rels.get(rid, ("", ""))
        info = parts.get(target)
        if state in ("hidden", "veryhidden"):
            out.add("sheet", name, n, rows=0, hidden=True)
            continue
        if rel_type != "worksheet" or info is None:
            out.add("sheet", name, n, rows=0)
            continue
        if out.full:
            out.add("sheet", name, n, rows=_count_rows(_safe_chunks(zf, info, limits)))
            continue
        rows, total = _xlsx_sheet(zf, info, shared, styles, date1904, limits, out)
        out.add("sheet", name, n, rows=total)
        for i, text in enumerate(_render_rows(rows)):
            out.add("row", text, i)


def _safe_chunks(zf, info, limits):
    try:
        for chunk in _zip_chunks(zf, info, limits, UNZIP_MAX):
            yield chunk
    except (_TooBig, _Unsafe):
        return


# --------------------------------------------------------------------------
# PowerPoint (.pptx)
# --------------------------------------------------------------------------

_PPT_SKIP_PLACEHOLDERS = frozenset(["dt", "ftr", "sldNum", "hdr", "sldImg"])


def _a_paragraphs(tx_body):
    """Texts of the a:p paragraphs in a DrawingML text body."""
    texts = []
    for p in _children(tx_body, "p"):
        text = _tidy(_all_text(p))
        if text:
            texts.append(text)
    return texts


def _ppt_shapes(tree, title, items, depth):
    """Walk a slide's shape tree: title placeholder text to ``title``, the rest to ``items``."""
    if depth > 50:
        return
    for sp in tree:
        tag = _local(sp.tag)
        if tag == "sp":
            nv = _child(sp, "nvSpPr")
            ph = _child(_child(nv, "nvPr"), "ph")
            ph_type = ph.get("type") if ph is not None else None
            if ph_type in _PPT_SKIP_PLACEHOLDERS:
                continue
            texts = _a_paragraphs(_child(sp, "txBody"))
            if ph_type in ("title", "ctrTitle") and not title:
                title.append(" ".join(texts))
            else:
                items.extend(("para", t) for t in texts)
        elif tag == "grpSp":
            _ppt_shapes(sp, title, items, depth + 1)
        elif tag == "graphicFrame":
            for tbl in sp.iter():
                if _local(tbl.tag) == "tbl":
                    for tr in _children(tbl, "tr"):
                        cells = []
                        for tc in _children(tr, "tc"):
                            if tc.get("hMerge") in ("1", "true") or tc.get("vMerge") in ("1", "true"):
                                continue
                            cell = " ".join(_a_paragraphs(_child(tc, "txBody")))
                            if cell:
                                cells.append(cell.replace("\n", " "))
                        if cells:
                            items.append(("row", " | ".join(cells)))
                    break
        elif tag == "AlternateContent":
            choice = _child(sp, "Choice")
            if choice is not None:
                _ppt_shapes(choice, title, items, depth + 1)


def _ppt_tree(root):
    """The p:spTree of a slide or notes slide."""
    return _child(_child(root, "cSld"), "spTree")


def _read_pptx(zf, parts, limits, out, doc):
    main = _main_part(zf, parts, limits, "ppt/presentation.xml")
    pres = _parse_part(zf, parts, main, limits, 16 * 1024 * 1024)
    if pres is None:
        raise _Unsafe("not a valid PowerPoint file")
    rels = dict((rid, target) for rid, _t, target in _rels(zf, parts, main, limits))
    slide_ids = [_rid(s) for s in _children(_child(pres, "sldIdLst"), "sldId")]
    doc["pages"] = len(slide_ids)
    for n, rid in enumerate(slide_ids, 1):
        if out.full:
            break
        target = rels.get(rid)
        slide = _try_part(zf, parts, target, limits) if target else None
        title, items = [], []
        notes = []
        if slide is not None:
            tree = _ppt_tree(slide)
            if tree is not None:
                _ppt_shapes(tree, title, items, 0)
            for _id, rel_type, part in _rels(zf, parts, target, limits):
                if rel_type == "diagramData":     # SmartArt text
                    data = _try_part(zf, parts, part, limits)
                    if data is not None:
                        for p in data.iter():
                            if _local(p.tag) == "p":
                                text = _tidy(_all_text(p))
                                if text:
                                    items.append(("para", text))
                elif rel_type == "notesSlide":
                    notes_root = _try_part(zf, parts, part, limits)
                    tree = _ppt_tree(notes_root) if notes_root is not None else None
                    for sp in (tree if tree is not None else []):
                        ph = _child(_child(_child(sp, "nvSpPr"), "nvPr"), "ph")
                        if ph is not None and ph.get("type") == "body":
                            notes.extend(_a_paragraphs(_child(sp, "txBody")))
        out.add("slide", (title[0] if title else "").replace("\n", " "), n)
        row_index = 0
        for kind, text in items:
            if kind == "row":
                out.add("row", text, row_index)
                row_index += 1
            else:
                out.add("para", text)
                row_index = 0
        if notes:
            out.add("para", "Notes: " + " ".join(notes))


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

_pypdf_lock = threading.Lock()
_pypdf_state = {"checked": False, "module": None}


def _get_pypdf():
    """The pypdf module, or None if it is not installed (imported once)."""
    if os.environ.get("SQUISH_NO_PYPDF"):
        return None
    with _pypdf_lock:
        if not _pypdf_state["checked"]:
            _pypdf_state["checked"] = True
            try:
                import pypdf  # optional dependency
                logging.getLogger("pypdf").setLevel(logging.CRITICAL)
                _pypdf_state["module"] = pypdf
            except Exception:
                _pypdf_state["module"] = None
        return _pypdf_state["module"]


def _pdf_with_pypdf(module, data):
    """Pages read with pypdf: dict like pdftext's result, or None if pypdf can't open it.

    pypdf runs in a helper thread so that a PDF it gets stuck on cannot hang
    Squish: after PDF_TIME_MAX seconds the pages read so far are used.
    """
    state = {"result": None, "partial": None, "stop": False}
    worker = threading.Thread(target=_pypdf_worker, args=(module, data, state), name="squish-pypdf")
    worker.daemon = True
    worker.start()
    worker.join(PDF_TIME_MAX * 1.1 + 1)
    if not worker.is_alive():
        return state["result"]
    state["stop"] = True
    partial = state["partial"]
    if partial is None:
        return {"pages": [], "page_sizes": [], "status": "error", "title": "", "count": 0,
                "note": "PDF took too long to open"}
    result = dict(partial)
    result["pages"] = list(partial["pages"])
    result["page_sizes"] = list(partial["page_sizes"])[:len(result["pages"])]
    result["note"] = "stopped after %d pages (slow to read)" % len(result["pages"])
    return result


def _pypdf_worker(module, data, state):
    """Read the pages with pypdf into state["partial"]; the finished result goes in state["result"]."""
    try:
        reader = module.PdfReader(io.BytesIO(data), strict=False)
        encrypted = bool(reader.is_encrypted)
    except Exception:
        return
    result = {"pages": [], "page_sizes": [], "status": "ok", "note": "", "title": "", "count": 0}
    if encrypted:
        try:
            ok = reader.decrypt("")
        except Exception:
            ok = 0
        if not ok:
            result["status"], result["note"] = "protected", "password-protected PDF"
            state["result"] = result
            return
    try:
        result["count"] = len(reader.pages)
    except Exception:
        return
    try:
        meta = reader.metadata
        result["title"] = str((meta.title if meta is not None else "") or "")
    except Exception:
        pass
    state["partial"] = result
    started = time.time()
    for i in range(min(result["count"], PAGE_MAX)):
        if state["stop"]:
            return
        if time.time() - started > PDF_TIME_MAX:
            result["note"] = "stopped after %d pages (slow to read)" % i
            break
        try:
            page = reader.pages[i]
        except Exception:
            break
        try:
            box = page.mediabox
            size = (abs(float(box.width)), abs(float(box.height)))
        except Exception:
            size = (0.0, 0.0)
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        result["page_sizes"].append(size)
        result["pages"].append(text)
    state["result"] = result


def _pdf_with_pdftext(data):
    """Pages read with the built-in reader (pdftext.py), or None if it is missing."""
    try:
        from squish_app import pdftext
    except Exception:
        return None
    try:
        res = pdftext.extract_pdf(data, max_pages=PAGE_MAX)
    except Exception as e:
        return {"pages": [], "page_sizes": [], "status": "error", "title": "", "count": 0,
                "note": "damaged PDF (%s)" % type(e).__name__}
    res = dict(res or {})
    pages = list(res.get("pages") or [])
    sizes = list(res.get("page_sizes") or [])
    count = res.get("page_count") or res.get("count") or max(len(pages), len(sizes))
    return {"pages": pages, "page_sizes": sizes, "status": res.get("status") or "ok",
            "note": res.get("note") or "", "title": res.get("title") or "", "count": count}


_TITLE_PREFIX = re.compile(r"^(Microsoft (Word|Excel|PowerPoint) - )", re.I)


def _read_pdf(name, data, out, doc):
    result = None
    module = _get_pypdf()
    if module is not None:
        result = _pdf_with_pypdf(module, data)
        doc["reader"] = "pypdf"
        if result is not None and result["status"] == "protected":
            # pypdf may lack the AES code for some encrypted PDFs: try the built-in reader.
            other = _pdf_with_pdftext(data)
            if other is not None and other["status"] == "ok" and any(p.strip() for p in other["pages"]):
                result = other
                doc["reader"] = "pdftext"
    if result is None:
        result = _pdf_with_pdftext(data)
        doc["reader"] = "pdftext"
    if result is None:
        doc["status"], doc["note"] = "unsupported", "PDF reader not available"
        return
    doc["title"] = _TITLE_PREFIX.sub("", _tidy(result.get("title") or "")).strip()[:300]
    doc["pages"] = result["count"] or len(result["pages"])
    sizes = result["page_sizes"]
    doc["drawing"] = is_drawing(name, doc["pages"], sizes)
    if result["status"] not in ("ok", "no_text"):
        doc["status"], doc["note"] = result["status"], result["note"] or "could not read this PDF"
        return
    pages = [_tidy(p, keep_blank=True) for p in result["pages"]]
    for n, text in enumerate(pages, 1):
        if out.full:
            out.chars += len(text)
            continue
        out.add("page", "", n)
        _pdf_page_blocks(text, out)
    letters = sum(len(re.sub(r"\s", "", p)) for p in pages)
    if not pages or letters < NO_TEXT_PER_PAGE * len(pages):
        doc["status"], doc["note"] = "no_text", "scanned or image-only PDF"
    elif result.get("note"):
        doc["note"] = result["note"]


# Drawing names: discipline codes and sheet numbers ("-ST-", "ST-1200", "SK01",
# "C-001", "A101"), drawing words, and revision markers next to a job number.
_DRAWING_CODE = re.compile(
    r"(?:^|[\s_.\-])(?:ST|CI|CV|SK|DR|AR|EL|HY)[\-_ ]?\d{1,5}[A-Z]?(?=$|[\s_.\-\[(])"
    r"|(?:^|[\s_.\-])[CSA][\-_]?\d{3,4}(?=$|[\s_.\-\[(])"
    r"|[\-_](?:ST|CI|CV|SK|DR|AR|EL|ME|HY|LA|GE|C|S|A|E|M|H)[\-_][A-Z]{0,4}\d")
_DRAWING_WORD = re.compile(r"(?i)(?:^|[^a-z])(?:dwgs?|drgs?|drawings?|sketch(?:es)?)(?=$|[^a-z])")
_REVISION = re.compile(r"\[[A-Z]{1,2}\d{0,2}\]|(?i:\brev(?:ision)?[\s._\-]*[A-Z0-9]{1,3}\b)")
_JOB_NUMBER = re.compile(r"\d{3,}")
_NOT_DRAWING = re.compile(
    r"(?i)(?:^|[^a-z])(?:report|memo|letter|minutes|specs?|specification|calcs?|calculations?|"
    r"proposal|invoice|quote|quotation|rfi|transmittal|register|programme|program|"
    r"certificate|checklist|swms|itp|fee|variation|claim|minutes|agenda|photos?)(?=$|[^a-z])")


def looks_like_drawing_name(name):
    """True if a file name looks like a drawing ("623.0001-ST-1200 ... [H].pdf", "SK01.pdf")."""
    base = os.path.splitext(re.split(r"[\\/]", name or "")[-1])[0]
    if _DRAWING_CODE.search(base):
        return True
    if _NOT_DRAWING.search(base):
        return False
    if _DRAWING_WORD.search(base):
        return True
    return bool(_REVISION.search(base) and _JOB_NUMBER.search(_REVISION.sub("", base)))


def is_drawing(name, pages, page_sizes):
    """A PDF whose pages are mostly A3 or larger, or a short PDF named like a drawing."""
    big = sum(1 for w, h in page_sizes if min(w, h) >= 820)
    if page_sizes and big * 2 > len(page_sizes):
        return True
    return bool(pages) and pages <= 5 and looks_like_drawing_name(name)


# Turning a PDF page's lines into paragraphs, headings and list items.
_BULLET = re.compile(r"^(?:[•●▪■◦○‣∙·*–\-]|o(?=\s)|\(?[a-zA-Z]\)|\(?(?:i|ii|iii|iv|v|vi|vii|viii|ix|x)\)"
                     r"|\d{1,2}[.)](?!\d))\s+")
_NUM_HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,4})\.?\s+([A-Z][^\n]{1,90})$")
_NOT_NUM_HEADING = re.compile(
    r"(?i)^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s+\d{2,4}\b"
    r"|[$\u00a3\u20ac]\s?\d|\d\.\d\d\s*$")
_AMOUNT_END = re.compile(r"[$\u00a3\u20ac]\s?\d[\d,]*(?:\.\d{1,2})?\s*$")
_DOT_LEADER = re.compile(r"\.{4,}|…{2,}|(?:\. ){4,}|\s\d{1,3}$")
_HEADING_WORDS = re.compile(
    r"(?i)^(executive summary|summary|introduction|background|scope( of works?)?|conclusions?|"
    r"recommendations?|references|limitations|discussion|methodology|results|contents|"
    r"table of contents|appendix [a-z0-9]{1,3}\b.*)\s*:?$")


def _pdf_heading(line):
    """Heading level (1-5) if a PDF line looks like a heading, else 0."""
    if len(line) > 100 or line.endswith((".", ",", ";")) or _DOT_LEADER.search(line):
        return 0
    m = _NUM_HEADING.match(line)
    if m:
        rest = m.group(2)
        if _NOT_NUM_HEADING.search(rest):
            return 0      # a date ('13 December 2024') or a priced row ('1 Site walkover $2,450.00')
        digits = sum(ch.isdigit() for ch in rest)
        if len(rest.split()) <= 14 and digits * 3 < len(rest):
            return min(5, m.group(1).count(".") + 1)
        return 0
    if _HEADING_WORDS.match(line):
        return 1
    letters = [ch for ch in line if ch.isalpha()]
    if len(letters) >= 4 and len(line.split()) <= 10 and len(line) <= 70 and \
            sum(ch.isupper() for ch in letters) >= 0.9 * len(letters) and \
            sum(ch.isdigit() for ch in line) * 3 < len(line):
        return 1
    return 0


def _pdf_page_blocks(text, out):
    """Add a PDF page's text as heading/para/item blocks.

    Wrapped lines are joined into paragraphs: a line that runs (nearly) the
    full width continues into the next one. A blank line, a heading, a bullet
    or a short line ends a paragraph. The first and last lines of the page
    (usually its header and footer) stay on their own, as do contents-list
    lines, so the digest can recognise and drop them.
    """
    lines = text.split("\n")
    filled = [i for i, line in enumerate(lines) if line]
    if not filled:
        return
    lengths = sorted(len(lines[i]) for i in filled)
    full = max(30, int(lengths[int(len(lengths) * 0.8)] * 0.75))
    alone = set([filled[0], filled[-1]])
    if len(filled) == len(lines):
        # No blank lines to mark blocks: a short second or second-last line
        # may be part of a two-line header or footer.
        for i in (filled[1:2] + filled[-2:-1]):
            if len(lines[i]) < full:
                alone.add(i)
    block = None   # [kind, text, level]
    joinable = False
    for i, line in enumerate(lines):
        if not line:
            joinable = False
            continue
        level = _pdf_heading(line)
        standalone = i in alone or bool(_DOT_LEADER.search(line))
        if level and len(line) < full * 1.2:
            if block:
                out.add(*block)
            out.add("heading", line, level)
            block, joinable = None, False
            continue
        bullet = _BULLET.match(line)
        if block and joinable and not bullet and not standalone:
            if block[1].endswith("-") and line[:1].islower():
                block[1] += line
            else:
                block[1] += " " + line
        else:
            if block:
                out.add(*block)
            block = ["item" if bullet else "para", line, 0]
        joinable = (len(line) >= full and not line.endswith(":") and not standalone
                    and not _AMOUNT_END.search(line))     # (a priced table row ends there)
    if block:
        out.add(*block)


# --------------------------------------------------------------------------
# Text, Markdown, CSV and RTF
# --------------------------------------------------------------------------

def _decode_text(data):
    """Bytes of a text file -> str (UTF-8 with or without BOM, UTF-16, else Windows-1252)."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    if data[:200].count(b"\x00") > 40:
        return data.decode("utf-16-be" if data[:1] == b"\x00" else "utf-16-le", "replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_ITEM = re.compile(r"^\s*(?:[-*+•]|\d{1,3}[.)])\s+")
_MD_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _read_text(name, data, out, doc):
    ext = _ext(name)
    if data.lstrip()[:5] == b"{\\rtf":
        _read_rtf(data, out)
        return
    text = _decode_text(data)
    if ext == ".csv":
        if _read_csv(name, text, out):
            return
    _text_blocks(text, out, markdown=(ext == ".md"))


def _read_rtf(data, out):
    from squish_app import msgfile
    try:
        mode, text = msgfile.rtf_to_html_or_text(data)
    except msgfile.MsgFileError as e:
        raise _Unsafe(str(e))
    if mode == "html":
        from squish_app import readers
        text = readers.html_to_text(text)
    for line in _tidy(text).split("\n"):
        line = line.strip(" |")       # (a table row's text ends with its cell separator)
        if line:
            out.add("item" if _MD_ITEM.match(line) else "para", line)


def _text_blocks(text, out, markdown=False):
    """Plain text or Markdown: blank-line paragraphs, list items, '#' headings, '|' tables."""
    para = []
    row_index = 0

    def flush():
        if para:
            out.add("para", "\n".join(para))
            del para[:]

    for raw in text.splitlines():
        if out.full:
            out.chars += len(raw)
            continue
        line = raw.strip()
        if not line:
            flush()
            row_index = 0
            continue
        heading = _MD_HEADING.match(line) if markdown else None
        if heading:
            flush()
            out.add("heading", heading.group(2), len(heading.group(1)))
        elif markdown and line.startswith("|"):
            flush()
            if not _MD_TABLE_RULE.match(line):
                cells = [c.strip() for c in line.strip("|").split("|")]
                out.add("row", " | ".join(c for c in cells if c), row_index)
                row_index += 1
        elif _MD_ITEM.match(line):
            flush()
            out.add("item", line, 0)
        else:
            para.append(line)
            if len(para) > 200:
                flush()
    flush()


def _read_csv(name, text, out):
    """A .csv file as one sheet of rows. False if it doesn't parse as CSV."""
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = []
    total = 0
    try:
        for cells in csv.reader(io.StringIO(text), dialect):
            cells = [_tidy(c).replace("\n", " ") for c in cells]
            while cells and not cells[-1]:
                cells.pop()
            if not cells:
                continue
            total += 1
            if len(rows) < SHEET_ROWS_MAX:
                rows.append(cells)
    except csv.Error:
        return False
    out.add("sheet", re.split(r"[\\/]", name)[-1], 1, rows=total)
    for i, cells in enumerate(rows):
        out.add("row", " | ".join(cells).strip(), i)
    return True


# --------------------------------------------------------------------------
# Zip files
# --------------------------------------------------------------------------

_ZIP_JUNK = re.compile(r"(?i)(^|/)(__MACOSX/|\.DS_Store$|Thumbs\.db$|desktop\.ini$)")


def _read_zip(data, limits, out, doc, depth):
    """List a zip's files; documents inside (one level) are read as nested DocTexts."""
    zf = _open_zip(data)
    infos = [i for i in zf.infolist() if not i.is_dir() and not _ZIP_JUNK.search(i.filename)]
    bytes_left = ZIP_DOCS_BYTES_MAX
    docs_read = 0
    listed = 0
    hidden = 0
    for info in infos:
        name = info.filename.replace("\\", "/")
        kind = _EXT_KIND.get(_ext(name))
        is_doc = kind is not None and kind != "zip" and depth == 0
        if not is_doc:
            if listed >= ZIP_NAMES_MAX:
                hidden += 1
                continue
            listed += 1
            out.add("member", name, 0, size=info.file_size)
            continue
        listed += 1
        if info.flag_bits & 0x1:
            member = _new_doc(kind, "protected", "encrypted inside the zip")
        elif docs_read >= ZIP_DOCS_MAX:
            member = _new_doc(kind, "too_big", "not read: more than %d documents in the zip" % ZIP_DOCS_MAX)
        elif info.file_size > DOC_MAX_BYTES:
            member = _new_doc(kind, "too_big", _too_big_note())
        elif info.file_size > bytes_left:
            member = _new_doc(kind, "too_big", "not read: the zip holds too much to unpack")
        elif out.text_max - out.stored < 1000:
            member = _new_doc(kind, "too_big", "not read: the zip holds too much text")
        else:
            try:
                member_data = _read_member(zf, info, limits, min(DOC_MAX_BYTES, bytes_left))
            except _TooBig as e:
                member = _new_doc(kind, "too_big", "not read: %s" % e)
            except (_Unsafe, zipfile.BadZipFile, zlib.error, EOFError, RuntimeError,
                    NotImplementedError, OSError, ValueError) as e:
                member = _new_doc(kind, "error", "not read: %s" % _short_error(e))
            else:
                bytes_left -= len(member_data)
                docs_read += 1
                room = max(0, min(ZIP_MEMBER_TEXT_MAX, out.text_max - out.stored))
                member = _extract_data(name, member_data, limits, room, depth + 1)
                out.stored += sum(len(b["text"]) for b in member["blocks"])
                out.chars += member["chars"]
        out.add("member", name, 0, size=info.file_size, doc=member)
    if hidden:
        out.add("para", "(+%d more files)" % hidden)
    doc["pages"] = None
