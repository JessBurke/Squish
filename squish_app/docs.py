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
import difflib
import hashlib
import io
import logging
import os
import posixpath
import re
import struct
import threading
import time
import unicodedata
import zipfile
import zlib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

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
ZIP_TIME_MAX = 120.0                 # seconds for the documents inside one zip (checked between them)

XML_PART_MAX = 64 * 1024 * 1024      # bytes parsed from one XML part
UNZIP_MAX = 512 * 1024 * 1024        # bytes unzipped from one file (all parts)
XML_ELEMENTS_MAX = 1500000           # XML elements parsed from one file
BODY_BLOCKS_MAX = 150000             # Word paragraphs and tables read
XML_DEPTH_MAX = 200                  # XML nesting depth
RATIO_MAX = 1100                     # deflate cannot do better than about 1032:1
ZIP_ENTRIES_MAX = 20000
PDF_TIME_MAX = 60.0                  # seconds pypdf may spend on one PDF
PYPDF_WAIT_S = 0.2                   # how often a wait for pypdf looks at Cancel
PDF_HEAVY_PAGE_BYTES = 1024 * 1024   # stored content bytes that make a page heavy (CAD): left to
                                     #   the built-in reader, as pypdf takes many seconds on it
PDF_RECHECK_TIME = 10.0              # seconds the built-in reader may spend re-checking pypdf's text
NO_TEXT_PER_PAGE = 20                # fewer characters per page read -> scanned PDF
TYPED_PAGE_MIN = 200                 # visible characters that make a page typed, not a scan

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


class _Stopped(Exception):
    """The caller asked to stop reading (Cancel)."""


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def is_supported(name):
    """True if ``name`` has an extension extract() can read."""
    return _ext(name) in _EXT_KIND


def extract(name, data=None, path=None, stop=None):
    """Read one document into a DocText dict. Never raises.

    ``name`` is the file name (its extension picks the reader; the content's
    magic bytes win when they disagree). Give the bytes as ``data`` or a file
    ``path``. ``stop`` is an optional function that returns True when the
    reading should stop (Cancel): it is asked between zip members, PDF pages,
    sheets, slides and Word paragraphs, and a stopped document comes back as
    status "error", note "reading was cancelled" (it is incomplete).
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
    return _extract_data(name, bytes(data), _Limits(stop), DOC_TEXT_MAX, 0)


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
    """Guards shared by everything read from one file (zip bombs, XML bombs),
    and the caller's ``stop`` function (Cancel)."""

    def __init__(self, stop=None):
        self.unzip_left = UNZIP_MAX
        self.elements_left = XML_ELEMENTS_MAX
        self.stop = stop

    def check_stop(self):
        """Raise _Stopped when the caller asked to stop."""
        if self.stop is not None and self.stop():
            raise _Stopped()


class _Out(object):
    """Collects a document's blocks, keeping at most ``text_max`` characters."""

    def __init__(self, text_max):
        self.blocks = []
        self.chars = 0          # all text seen, kept or not
        self.stored = 0         # text kept
        self.text_max = text_max
        self.full = False

    def add(self, kind, text, level=0, **extra):
        """Add one block (text is tidied; empty text blocks are dropped).

        Once the text limit is reached only sheet and page marks are still
        added (they are labels, not text), so a workbook still lists every sheet.
        """
        text = _tidy(text)
        if kind in ("heading", "row"):
            text = text.replace("\n", " ")
        if not text and kind in ("heading", "para", "item", "row"):
            return False
        self.chars += len(text)
        if self.full:
            if kind not in ("sheet", "page"):
                return False
            block = {"type": kind, "text": text, "level": level}
            block.update(extra)
            self.blocks.append(block)
            return True
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
            _read_pdf(name, data, out, doc, limits.stop)
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
    except _Stopped:
        doc["status"], doc["note"] = "error", "reading was cancelled"
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
    if out.full and doc["status"] == "ok" and not doc["note"]:
        doc["note"] = "partly read: text limit reached"
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

# (Zero-width joiners U+200C/U+200D stay: they change spelling in Persian and
# Indic scripts and hold emoji sequences together.)
_CONTROL = re.compile("[\x00-\x08\x0e-\x1f\x7f\u00ad\u200b\u2060\ufeff\ufffe]")
_SPACES = re.compile("[ \t\u00a0\u2000-\u200a\u202f\u205f\u3000]+")
_SURROGATE = re.compile("[\ud800-\udfff]")

# Private-use characters from Symbol/Wingdings fonts, as seen in PDFs and Word.
_PUA = {
    "\uf0b7": "•", "\uf0a7": "▪", "\uf076": "•", "\uf0d8": "•", "\uf0a8": "☐",
    "\uf0fc": "✓", "\uf0fb": "✗", "\uf0fe": "☑", "\uf0fd": "☒", "\uf06e": "■", "\uf06c": "●",
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
    if data.lstrip()[:5] == b"%PDF-":
        return "pdf"
    # Zip and OLE come before the looser PDF test below: a zip whose first file
    # is a PDF stored uncompressed has "%PDF-" in its first KB.
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return _zip_kind(data, ext_kind)
    if data[:8] == _OLE_MAGIC:
        # Password-protected .docx/.xlsx/.pptx files are OLE files holding an
        # "EncryptedPackage" stream; anything else is an old binary Office file.
        if ext_kind in ("docx", "xlsx", "pptx") and _ENCRYPTED_PACKAGE in data[:4 * 1024 * 1024]:
            return "protected"
        return "ole"
    if ext_kind != "text" and b"%PDF-" in data[:1024]:
        return "pdf"        # a PDF after a short junk prefix (PDF readers allow up to 1 KB)
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
# Wingdings 2: the tick boxes and crosses of forms.
_WINGDINGS2 = {0x4F: "✗", 0x50: "✓", 0x51: "☒", 0x52: "☑", 0x53: "☒", 0x54: "☒", 0xA3: "☐"}


def _symbol_font_char(code):
    """A character of the Symbol font (Greek letters, maths signs) by its code."""
    if code in _SYMBOL_FONT:
        return _SYMBOL_FONT[code]
    try:
        from squish_app import pdftext      # it has the whole Symbol encoding
        return pdftext.SYMBOL_ENCODING[code] if 0 <= code < 256 else ""
    except ImportError:
        return chr(code) if 0x20 < code < 0x7F else ""


# Word fonts whose text runs hold codes of the font's own encoding (not
# Unicode): LibreOffice writes Symbol characters this way, and so does text
# typed in the Symbol font ("m" shows as μ).
_SYMBOL_RUN_FONTS = {"symbol": "symbol", "symbolmt": "symbol", "wingdings": "wingdings",
                     "wingdings2": "wingdings2"}


def _run_symbol_kind(rpr):
    """"symbol", "wingdings" or "wingdings2" when a run's font (w:rFonts) is one
    of those symbol fonts, else None."""
    fonts = _child(rpr, "rFonts")
    if fonts is None:
        return None
    for name in ("ascii", "hAnsi"):
        font = _attr(fonts, name)
        if font:
            return _SYMBOL_RUN_FONTS.get(font.lower().replace(" ", "").replace("-", ""))
    return None


def _symbol_run_text(text, kind):
    """Text of a run set in a symbol font, in real characters ("85 m" in Symbol -> "85 μ").

    Codes 0x20-0xFF (and the same codes moved to U+F020-U+F0FF) are looked
    up in the font's table, as for w:sym; other characters are kept.
    """
    out = []
    table = None
    for ch in text:
        code = ord(ch)
        if not (0x20 <= code <= 0xFF or 0xF020 <= code <= 0xF0FF):
            out.append(ch)
            continue
        c = code & 0xFF
        if c == 0x20:
            out.append(" ")
            continue
        found = _symbol_code_char(kind, c)
        if not found:
            if table is None:
                try:
                    from squish_app import pdftext
                    table = pdftext.symbol_pua_table(kind)
                except ImportError:
                    table = {}
            found = table.get(0xF000 + c, ch)
        out.append(found)
    return "".join(out)


def _symbol_code_char(kind, code):
    """A character of a symbol font by its code ("" if not known)."""
    if kind == "wingdings2":
        return _WINGDINGS2.get(code, "")
    if kind == "wingdings":
        return _WINGDINGS.get(code, "")
    if kind == "symbol":
        return _symbol_font_char(code)
    return ""


def _sym_char(el):
    """Text for a w:sym (a character in a symbol font)."""
    font = (_attr(el, "font") or "").lower()
    try:
        code = int(_attr(el, "char") or "", 16)
    except ValueError:
        return ""
    if code >= 0xF000:
        code -= 0xF000
    if "wingdings2" in font.replace(" ", "").replace("-", ""):
        return _WINGDINGS2.get(code, "")
    if "wingdings" in font:
        return _WINGDINGS.get(code, "")
    if "symbol" in font:
        return _symbol_font_char(code)
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
        self.diagrams = {}        # relationship id -> SmartArt texts (the main document's)
        self.raised_at = None     # where the last superscript number was written (see _w_raised)
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
        self.lvl_overrides = {}   # numId -> {ilvl: (start, fmt, text, legal)} from a lvlOverride's own w:lvl
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
                    start, fmt, text, legal = _parse_lvl(lvl)
                    levels[ilvl] = (1 if start is None else start, fmt or "decimal", text or "", legal)
                self.levels[aid] = levels
                link = _val(el, "numStyleLink")
                if link:
                    self.links[aid] = link
            elif tag == "num":
                overrides = {}
                own = {}
                for ov in _children(el, "lvlOverride"):
                    ilvl = _int_or_none(_attr(ov, "ilvl"))
                    if ilvl is None or not 0 <= ilvl <= 8:
                        continue
                    start = _int_or_none(_val(ov, "startOverride"))
                    if start is not None:
                        overrides[ilvl] = start
                    lvl = _child(ov, "lvl")
                    if lvl is not None:       # the level redefined for this list (format, text)
                        own[ilvl] = _parse_lvl(lvl)
                num_id = _attr(el, "numId")
                self.nums[num_id] = (_val(el, "abstractNumId"), overrides)
                if own:
                    self.lvl_overrides[num_id] = own

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

    def _level(self, num_id, ilvl, levels):
        """(start, fmt, text, legal) of a list level: the list's own override
        (gaps filled from the abstract level), else the abstract level, else None."""
        base = levels.get(ilvl)
        own = self.lvl_overrides.get(num_id, {}).get(ilvl)
        if own is None:
            return base
        start, fmt, text, legal = own
        if base is not None:
            fmt = base[1] if fmt is None else fmt
            text = base[2] if text is None else text
        return (1 if start is None else start, fmt or "decimal", text or "", legal)

    def label(self, num_id, ilvl):
        """Advance the list counter and return the paragraph's label ("" if none)."""
        aid = self._abstract(num_id)
        levels = self.levels.get(aid) or {}
        defn = self._level(num_id, ilvl, levels)
        if defn is None:
            return ""
        counters = self.counters.setdefault(aid, [None] * 9)
        if num_id not in self.used:
            self.used.add(num_id)
            starts = self.nums[num_id][1]
            for lvl in self.lvl_overrides.get(num_id, {}):
                if lvl not in starts:      # a redefined level starts again from its own start
                    counters[lvl] = self._level(num_id, lvl, levels)[0] - 1
            for lvl, start in starts.items():
                if 0 <= lvl <= 8:
                    counters[lvl] = start - 1
        start, fmt, text, legal = defn
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
            lvl = self._level(num_id, k, levels) or (1, "decimal", "", False)
            value = counters[k] if counters[k] is not None else lvl[0]
            return _format_number(value, "decimal" if (legal and k < ilvl) else lvl[1])
        return re.sub(r"%([1-9])", number, text).strip()


def _parse_lvl(lvl):
    """(start, numFmt, lvlText, isLgl) of a w:lvl; None for a value it does not give."""
    text = _child(lvl, "lvlText")
    return (_int_or_none(_val(lvl, "start")), _val(lvl, "numFmt"),
            _attr(text, "val") if text is not None else None, _child(lvl, "isLgl") is not None)


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


def _w_hidden(rpr):
    """True when run properties hide the text (w:vanish: Word neither shows nor prints it)."""
    if rpr is None:
        return False
    for name in ("vanish", "specVanish"):
        el = _child(rpr, name)
        if el is not None and (_attr(el, "val") or "true").lower() not in ("false", "0", "off"):
            return True
    return False


def _w_inline(el, ctx, out, extra, depth, kind=None):
    """Collect a paragraph's text into ``out``; text boxes, SmartArt and
    watermarks found go to ``extra``. Hidden runs (w:vanish) are left out.
    ``kind`` is the symbol font of the run being read (see _run_symbol_kind)."""
    if depth > XML_DEPTH_MAX:
        return
    for child in el:
        tag = _local(child.tag)
        if tag in _W_SKIP:
            continue
        if tag in ("oMath", "oMathPara"):
            out.append(_m_text(child, ctx, depth + 1))
            continue
        if tag == "r":
            rpr = _child(child, "rPr")
            if _w_hidden(rpr) or _w_raised(child, rpr, ctx, out):
                continue
            _w_inline(child, ctx, out, extra, depth + 1, _run_symbol_kind(rpr))
            continue
        if tag == "t":
            if child.text:
                out.append(_symbol_run_text(child.text, kind) if kind else child.text)
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
        elif tag == "fldChar":
            out.append(_w_form_field(child))
        elif tag == "sdt" and _w_sdt_placeholder(child):
            out.append("[blank]")         # an empty content control shows only its prompt
        elif tag == "relIds":             # SmartArt: its text is in the diagram data part
            for text in ctx.diagrams.get(_attr(child, "dm"), []):
                extra.append(("item", text, 0))
        elif tag == "textpath":           # a WordArt watermark ("DRAFT", "NOT FOR CONSTRUCTION")
            text = _tidy(_attr(child, "string") or "").replace("\n", " ")
            if text:
                extra.append(("para", "Watermark: " + text, 0))
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


def _w_raised(r, rpr, ctx, out):
    """Write a superscript number that follows a digit as "^n" ("10^6",
    "10^-7"): run together it would read as another number ("106"). An
    exponent split over several runs stays together. True when done.

    Only digits (and a sign) raised right after a digit are changed, so
    ordinals ("1st"), units ("m2") and reference marks keep their text.
    """
    if _val(rpr, "vertAlign") != "superscript":
        return False
    parts = []
    for c in r:
        tag = _local(c.tag)
        if tag == "t":
            parts.append(c.text or "")
        elif tag not in _W_SKIP:
            return False              # a field mark, symbol, note reference...: not a plain number
    text = "".join(parts).strip()
    if not text or not _RAISED_NUMBER.match(text):
        return False
    if ctx.raised_at == (id(out), len(out)):
        out.append(text)              # the same exponent, carried on in the next run
    else:
        last = next((piece for piece in reversed(out) if piece), "")
        if not last[-1:].isdigit():
            return False
        out.append("^" + text)
    ctx.raised_at = (id(out), len(out))
    return True


_RAISED_NUMBER = re.compile("^[-+\u2212]?[0-9]*$")


def _w_form_field(fld_char):
    """Text a legacy (Word 97-2003) form field shows that no run holds: a check
    box as ☒ or ☐, a drop-down as its chosen entry. "" for anything else."""
    if _attr(fld_char, "fldCharType") != "begin":
        return ""
    ff = _child(fld_char, "ffData")
    box = _child(ff, "checkBox")
    if box is not None:
        checked = _child(box, "checked")
        if checked is not None:
            on = (_attr(checked, "val") or "true").lower() not in ("false", "0", "off")
        else:
            on = (_val(box, "default") or "0").lower() in ("true", "1", "on")
        return "☒" if on else "☐"
    dd = _child(ff, "ddList")
    if dd is not None:
        entries = [_attr(e, "val") or "" for e in _children(dd, "listEntry")]
        index = _int_or_none(_val(dd, "result")) or 0
        return entries[index] if 0 <= index < len(entries) else ""
    return ""


def _w_sdt_placeholder(sdt):
    """True for a content control that is empty: Word shows its grey prompt
    ("Click or tap here to enter text."), which is not content."""
    flag = _child(_child(sdt, "sdtPr"), "showingPlcHdr")
    return flag is not None and (_attr(flag, "val") or "true").lower() not in ("0", "false", "off")


# --------------------------------------------------------------------------
# Word equations (Office Math), written on one line
# --------------------------------------------------------------------------

_M_SIMPLE = re.compile("^[-+\u2212]?[\\w.'\u2032^]+$")
_M_BRACKETS = {"(": ")", "[": "]", "{": "}"}
_M_ROOTS = {"": "\u221a", "2": "\u221a", "3": "\u221b", "4": "\u221c"}


def _m_one_group(text):
    """True when the whole text is one bracketed group: "(a+b)" but not "(a)+(b)"."""
    if len(text) < 2 or text[0] not in _M_BRACKETS or text[-1] != _M_BRACKETS[text[0]]:
        return False
    depth = 0
    for i, ch in enumerate(text):
        if ch == text[0]:
            depth += 1
        elif ch == text[-1]:
            depth -= 1
            if depth == 0 and i < len(text) - 1:
                return False
    return depth == 0


def _m_group(text):
    """A part of an equation, bracketed unless it is one simple term: "8", "wL^2", "(a+b)"."""
    text = text.strip()
    if not text or _M_SIMPLE.match(text) or _m_one_group(text):
        return text
    return "(" + text + ")"


def _m_chr(pr, name, default):
    """A character property of an equation part (begChr, sepChr, chr ...): val="" means none."""
    el = _child(pr, name)
    value = _attr(el, "val") if el is not None else None
    return default if value is None else value


def _m_text(el, ctx, depth):
    """Linear text of an Office Math element, so that numbers never run
    together: fractions as "P/A", powers and indices as "wL^2" and "V_uc",
    roots as "√(f'c)", sums as "∑_(i=1)^n x". (ASCII ^ and _ keep the digest's
    number patterns working.)"""
    if el is None or depth > XML_DEPTH_MAX:
        return ""
    tag = _local(el.tag)
    if tag in _W_SKIP or tag.endswith("Pr"):
        return ""

    def part(name):
        return _m_text(_child(el, name), ctx, depth + 1)

    if tag == "r":
        if any(_w_hidden(c) for c in el if _local(c.tag) == "rPr"):
            return ""
        pieces = []
        _w_inline(el, ctx, pieces, [], depth + 1)
        return "".join(pieces)
    if tag == "f":
        if _m_chr(_child(el, "fPr"), "type", "") == "noBar":
            return "(%s; %s)" % (part("num").strip(), part("den").strip())
        return _m_group(part("num")) + "/" + _m_group(part("den"))
    if tag in ("sSup", "sSub", "sSubSup", "sPre"):
        sub, sup = part("sub").strip(), part("sup").strip()
        marks = ("_" + _m_group(sub) if sub else "") + ("^" + _m_group(sup) if sup else "")
        base = _m_group(part("e"))
        return marks + base if tag == "sPre" else base + marks
    if tag == "rad":
        degree = part("deg").strip()
        sign = _M_ROOTS.get(degree) or _m_group(degree) + "\u221a"
        body = part("e").strip()
        if not re.match(r"^[0-9.]+$", body) and not _m_one_group(body):
            body = "(" + body + ")"
        return sign + body
    if tag == "d":
        pr = _child(el, "dPr")
        inner = _m_chr(pr, "sepChr", "|").join(_m_text(e, ctx, depth + 1).strip()
                                                 for e in _children(el, "e"))
        return _m_chr(pr, "begChr", "(") + inner + _m_chr(pr, "endChr", ")")
    if tag == "nary":
        sub, sup = part("sub").strip(), part("sup").strip()
        return (_m_chr(_child(el, "naryPr"), "chr", "\u222b") + ("_" + _m_group(sub) if sub else "")
                + ("^" + _m_group(sup) if sup else "") + " " + part("e").strip())
    if tag in ("limLow", "limUpp"):
        return part("e").strip() + ("_" if tag == "limLow" else "^") + _m_group(part("lim"))
    if tag == "func":
        return part("fName").strip() + " " + part("e").strip()
    if tag == "eqArr":
        return "\n".join(_m_text(e, ctx, depth + 1).strip() for e in _children(el, "e"))
    if tag == "oMathPara":
        return "\n".join(_m_text(m, ctx, depth + 1).strip() for m in _children(el, "oMath"))
    if tag == "m":
        rows = [", ".join(_m_text(e, ctx, depth + 1).strip() for e in _children(mr, "e"))
                for mr in _children(el, "mr")]
        return "[" + "; ".join(rows) + "]"
    if tag == "AlternateContent":
        return _m_text(_child(el, "Choice"), ctx, depth + 1)
    return "".join(_m_text(c, ctx, depth + 1) for c in el)


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
    ctx.raised_at = None
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
    # A list item deleted with tracked changes (its text and its paragraph
    # mark) is gone in the final document: it takes no number.
    deleted = _child(_child(ppr, "rPr"), "del") is not None and not _tidy(text)
    if num_id and num_id != "0" and ctx.numbering is not None and not deleted:
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
    elif tag == "sdt" and _w_sdt_placeholder(el):
        emit("para", "[blank]", 0)        # an empty content control: its prompt is not content
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


def _w_unwrap(el, name, depth=0, blank=False):
    """(child, blank) for the children called ``name``, looking through
    content-control wrappers; ``blank`` is True inside an empty content
    control (its text is only the prompt)."""
    found = []
    for c in el:
        tag = _local(c.tag)
        if tag == name:
            found.append((c, blank))
        elif tag in _W_WRAPPERS and depth < 10:
            found.extend(_w_unwrap(c, name, depth + 1,
                                   blank or (tag == "sdt" and _w_sdt_placeholder(c))))
    return found


def _w_table(tbl, ctx, emit, depth):
    """Emit one "row" block per table row: non-empty cells joined with ' | '."""
    index = 0
    for tr, blank_row in _w_unwrap(tbl, "tr"):
        cells = []
        for tc, blank in _w_unwrap(tr, "tc", blank=blank_row):
            tcpr = _child(tc, "tcPr")
            merged = [m for m in (_child(tcpr, "vMerge"), _child(tcpr, "hMerge")) if m is not None]
            if merged and _attr(merged[0], "val") != "restart":
                continue  # continuation of a merged cell (its text is in the first cell)
            if blank:
                cells.append("[blank]")
                continue
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

    for rid, rel_type, target in rels:
        if rel_type == "diagramData":     # SmartArt text, written where the diagram is
            ctx.diagrams[rid] = _diagram_texts(_try_part(zf, parts, target, limits))

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
                if blocks & 0xFF == 0:
                    limits.check_stop()
    except (_TooBig, ET.ParseError) as e:
        if not out.blocks:
            raise
        out.add("para", "(rest of the document not read: %s)" % _short_error(e))
    ctx.diagrams = {}        # (header and note parts have their own relationship ids)

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


_FORMAT_SECTION = re.compile(r';(?=(?:[^"]*"[^"]*")*[^"]*$)')     # a ';' outside quotes
_FORMAT_COLOUR = re.compile(r"(?i)^(black|blue|cyan|green|magenta|red|white|yellow|colou?r\s*\d{1,2})$")
_FORMAT_LITERAL = "$-+():!^&'~{}<>= "


def _number_affixes(code):
    """(prefix, zeros, suffix) that a number format writes around the number,
    or None when it adds nothing Squish shows.

    '"RFI-"000' -> ('RFI-', 3, ''), '0.0" kN"' -> ('', 1, ' kN'), '"$"#,##0.00'
    -> ('$', 1, ''). ``zeros`` is the number of digits the whole-number part is
    padded to. Formats that do more than add text and leading zeros
    (fractions, exponents, conditions, scaling commas, masks such as
    '00"-"000') give None: the plain number is shown.
    """
    if not code:
        return None
    section = _FORMAT_SECTION.split(code)[0]
    prefix, suffix = [], []
    started = ended = False       # number placeholders seen / literal text after them
    whole = True                  # still before the decimal point
    zeros = 0
    i = 0
    while i < len(section):
        ch = section[i]
        literal = None
        if ch == '"':
            end = section.find('"', i + 1)
            end = len(section) if end < 0 else end
            literal = section[i + 1:end]
            i = end + 1
        elif ch == "\\":
            literal = section[i + 1:i + 2]
            i += 2
        elif ch in "_*":
            i += 2                    # padding to a character's width, or a fill character
            continue
        elif ch == "[":
            end = section.find("]", i)
            if end < 0:
                return None
            inner = section[i + 1:end]
            i = end + 1
            if inner.startswith("$"):
                literal = inner[1:].split("-", 1)[0]      # [$€-407] -> €; [$-409] (a locale) -> nothing
            elif _FORMAT_COLOUR.match(inner):
                continue
            else:
                return None           # a condition such as [>=1000]
        elif section[i:i + 7].lower() == "general" or ch in "0#?":
            if ended:
                return None           # a mask like 00"-"000
            started = True
            if ch == "0" and whole:
                zeros += 1
            i += 7 if ch in "Gg" else 1
            continue
        elif ch == "." and started and not ended:
            whole = False
            i += 1
            continue
        elif ch == "," and started and not ended:
            if section[i + 1:i + 2] not in ("0", "#", "?", ","):
                return None           # a trailing comma divides by 1000
            i += 1
            continue
        elif ch in _FORMAT_LITERAL or ord(ch) > 127:
            literal = ch
            i += 1
        else:
            return None               # an exponent, a fraction, text (@), a date part ...
        (suffix if started else prefix).append(literal)
        ended = ended or started
    prefix, suffix = "".join(prefix), "".join(suffix)
    if not started or (not prefix and not suffix and zeros <= 1):
        return None
    return (prefix, zeros, suffix)


def _with_affixes(v, affixes):
    """A cell's number with its format's text and leading zeros: '-35.2 kN', 'RFI-007'.
    (Thousands separators and rounding are not applied: the full number is kept.)"""
    prefix, zeros, suffix = affixes
    try:
        float(v)
    except (TypeError, ValueError):
        return (v or "").strip()
    text = format_number(v)
    sign = ""
    if text.startswith("-"):
        sign, text = "-", text[1:]
    if zeros > 1 and "e" not in text.lower():
        whole, dot, fraction = text.partition(".")
        text = whole.zfill(zeros) + dot + fraction
    return sign + prefix + text + suffix


def _xlsx_styles(root):
    """Kind of number format for each cell style index (cellXfs): a _format_kind
    string, a (prefix, zeros, suffix) tuple from _number_affixes, or None."""
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
                    code = f.get("formatCode") or ""
                    custom[fid] = _format_kind(code) or _number_affixes(code)
        elif tag == "cellXfs":
            for xf in el:
                fid = _int_or_none(xf.get("numFmtId")) or 0
                if fid in custom:
                    kinds.append(custom[fid])
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
    s = "%.15g" % f        # as many digits as Excel shows; float noise beyond them goes
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
    if isinstance(kind, tuple):
        return _with_affixes(v, kind)
    if kind:
        try:
            f = float(v)
        except ValueError:
            return v.strip()
        if kind.startswith("percent:"):
            return _percent(f, int(kind[8:]))
        text = excel_date(f, date1904, kind)
        if text is not None:
            return text
    return format_number(v)


def _percent(f, decimals):
    """A percent as Excel shows it: 0.125 with 0% -> '13%' (half away from zero)."""
    try:
        value = (Decimal("%.15g" % f) * 100).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
        return format_number(format(value, "f")) + "%"
    except (InvalidOperation, ValueError):
        return format_number("%.*f" % (decimals, f * 100)) + "%"


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


_XL_ROW_END = re.compile(rb"</(?:[A-Za-z0-9_.\-]+:)?row>")
_XL_VALUE = re.compile(rb"<(?:[A-Za-z0-9_.\-]+:)?(?:v|is)[\s>]")


def _count_rows(chunks, pending=False):
    """Count the remaining rows that contain a cell value, in raw worksheet XML,
    without parsing it. Rows holding only formatting (Excel writes those down
    a column that has borders or a fill) are not counted. ``pending`` says the
    row being read when counting started already had a value."""
    count = 0
    buf = b""
    try:
        for chunk in chunks:
            buf += chunk
            pos = 0
            for m in _XL_ROW_END.finditer(buf):
                if pending or _XL_VALUE.search(buf, pos, m.start()):
                    count += 1
                pending = False
                pos = m.end()
            buf = buf[pos:]
            if len(buf) > 2 * _CHUNK:             # a very long row: keep only its end
                pending = pending or bool(_XL_VALUE.search(buf))
                buf = buf[-64:]
    except (_TooBig, _Unsafe, zlib.error, zipfile.BadZipFile, EOFError, RuntimeError):
        pass
    return count


_THREADED_PREAMBLE = re.compile(r"(?s)^\[Threaded comment\].*?\bComment:\s*")
_THREADED_REPLY = re.compile(r"\s*\bReply:\s*")


def _xlsx_notes(zf, parts, sheet_part, limits):
    """Cell notes and comments of a worksheet: {"C3": text}.

    Excel 365 also writes each threaded comment as a legacy note (with a
    "[Threaded comment] ... Comment:" preamble, which is removed), so the
    legacy notes part is enough. A damaged part is skipped.
    """
    notes = {}
    for target in _rel_targets(_rels(zf, parts, sheet_part, limits), "comments"):
        root = _try_part(zf, parts, target, limits)
        for comment_list in _children(root, "commentList"):
            for c in _children(comment_list, "comment"):
                ref = (c.get("ref") or "").replace("$", "").upper()
                text = _THREADED_PREAMBLE.sub("", _si_text(_child(c, "text")) if _child(c, "text") is not None else "")
                text = _tidy(_THREADED_REPLY.sub(" / ", text)).replace("\n", " ")[:500]
                if ref and text:
                    notes[ref] = text
                if len(notes) >= 2000:
                    return notes
    return notes


def _xlsx_sheet(zf, info, shared, styles, date1904, limits, out, notes=None):
    """Rows of one worksheet, streamed: (rows kept as lists of cells, total non-empty rows).

    A cell's note is added to its text as "[note: ...]"; notes on cells that
    have no value are listed in a last "Notes:" row. Manually hidden columns
    are left out (like hidden sheets); hidden rows are kept (filters and
    collapsed groups hide real data).
    """
    notes = dict(notes or {})
    hidden_cols = []          # (first, last) zero-based column ranges
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
                note = notes.pop((el.get("r") or "").upper(), None)
                if any(lo <= col <= hi for lo, hi in hidden_cols):
                    el.clear()
                    continue
                if counting:
                    if _child(el, "v") is not None or _child(el, "is") is not None:
                        row[col] = "x"
                else:
                    text = _tidy(_cell_text(el, shared, styles, date1904)).replace("\n", " ")
                    if note:
                        text = (text + " [note: %s]" % note).strip()
                    if text:
                        row[col] = text
                el.clear()
            elif tag == "col":
                lo, hi = _int_or_none(el.get("min")), _int_or_none(el.get("max"))
                if (el.get("hidden") or "").lower() in ("1", "true") and lo and hi and \
                        not _int_or_none(el.get("outlineLevel")):
                    hidden_cols.append((lo - 1, hi - 1))     # (a collapsed group's columns are kept)
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
        total += _count_rows(chunks, bool(row))
    elif notes:
        # Notes on cells without a value (Excel leaves such cells out).
        kept.append({0: ("Notes: " + "; ".join("%s: %s" % item for item in sorted(notes.items())))[:2000]})
    return kept, total


SPARSE_RUN = 8       # a row with a longer run of empty cells is written as "header: value"
LABEL_MAX = 40       # characters of a header cell used as a label


def _col_letter(col):
    """Column letter(s) of a zero-based column: 0 -> 'A', 27 -> 'AB'."""
    letters = ""
    col += 1
    while col > 0:
        col, rest = divmod(col - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _longest_gap(cells):
    """Length of the longest run of empty cells."""
    longest = run = 0
    for cell in cells:
        run = 0 if cell else run + 1
        longest = max(longest, run)
    return longest


def _render_rows(rows):
    """Text of each row: cells joined with ' | ', fully empty columns dropped,
    trailing empty cells trimmed. Empty cells between filled ones are kept so
    the columns line up, except in a sparse row below the header row (more
    than SPARSE_RUN empty cells in a run, as in a programme or a matrix):
    after its first cells (its label) each filled cell is written with its
    column's header, "W97: x", so it does not need counting. The header row
    is the fullest of the first 10 rows."""
    used = sorted(set(col for row in rows for col in row))
    top = rows[:10]
    head_at = top.index(max(top, key=len)) if top else 0
    head = rows[head_at] if rows else {}
    result = []
    for n, row in enumerate(rows):
        cells = [row.get(col, "") for col in used]
        while cells and not cells[-1]:
            cells.pop()
        if n > head_at and _longest_gap(cells) > SPARSE_RUN:
            lead = 0
            while lead < len(cells) and not cells[lead]:
                lead += 1                 # (leading empty cells stay)
            while lead < len(cells) and cells[lead]:
                lead += 1                 # the row's label cells
            labelled = ["%s: %s" % ((head.get(col) or _col_letter(col))[:LABEL_MAX].strip(), row[col])
                        for col in used[lead:] if row.get(col)]
            cells = cells[:lead] + labelled
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
        limits.check_stop()
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
        rows, total = _xlsx_sheet(zf, info, shared, styles, date1904, limits, out,
                                  _xlsx_notes(zf, parts, target, limits))
        out.add("sheet", name, n, rows=total)
        if total > len(rows) and not doc["note"]:
            if len(rows) >= SHEET_ROWS_MAX:
                doc["note"] = "partly read: first %d of %d rows of sheet %s" % (len(rows), total, name)
            else:
                doc["note"] = "partly read: text limit reached at sheet %s" % name
        for i, text in enumerate(_render_rows(rows)):
            out.add("row", text, i)
        for drawing in _rel_targets(_rels(zf, parts, target, limits), "drawing"):
            texts = []
            _drawing_texts(_try_part(zf, parts, drawing, limits), texts)
            for text in texts[:20]:
                out.add("para", "Text box: " + text[:2000])
        if out.full and not doc["note"]:
            doc["note"] = "partly read: text limit reached at sheet %s" % name


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


def _drawing_texts(el, texts, depth=0):
    """Texts of the text boxes and shapes in a drawing part (a sheet's notes
    typed into a text box), one per shape. Charts and pictures hold none;
    hidden shapes (copies of old form controls) and fallback copies are skipped."""
    if el is None or depth > 50:
        return
    for c in el:
        tag = _local(c.tag)
        if tag == "Fallback":
            continue
        if tag == "sp" and (_attr(_child(_child(c, "nvSpPr"), "cNvPr"), "hidden") or "").lower() in ("1", "true"):
            continue
        if tag == "txBody":
            text = " ".join(_a_paragraphs(c)).replace("\n", " ")
            if text:
                texts.append(text)
            continue
        _drawing_texts(c, texts, depth + 1)


def _diagram_texts(root):
    """Texts of a SmartArt diagram data part (its points), in order."""
    texts = []
    if root is not None:
        for p in root.iter():
            if _local(p.tag) == "p":
                text = _tidy(_all_text(p))
                if text:
                    texts.append(text)
    return texts


def _a_paragraphs(tx_body):
    """Texts of the a:p paragraphs in a DrawingML text body. Automatically
    numbered paragraphs (a:buAutoNum) get their number ("3.", "b)", "(ii)")."""
    texts = []
    counters = {}       # level -> (numbering scheme, last number)
    for p in _children(tx_body, "p"):
        text = _tidy(_all_text(p))
        if not text:
            continue            # (PowerPoint does not number empty paragraphs)
        ppr = _child(p, "pPr")
        level = _int_or_none(_attr(ppr, "lvl")) or 0
        bu = _child(ppr, "buAutoNum")
        if bu is None:
            for deeper in [k for k in counters if k >= level]:
                del counters[deeper]          # an unnumbered paragraph ends the list at its level
        else:
            for deeper in [k for k in counters if k > level]:
                del counters[deeper]
            scheme = bu.get("type") or "arabicPeriod"
            last = counters.get(level)
            start = _int_or_none(bu.get("startAt")) or 1
            number = last[1] + 1 if last and last[0] == scheme else start
            counters[level] = (scheme, number)
            text = _autonum_label(scheme, number) + " " + text
        texts.append(text)
    return texts


def _autonum_label(scheme, n):
    """A PowerPoint automatic number: 'arabicPeriod' 3 -> '3.', 'alphaLcParenR' 2 -> 'b)'."""
    if scheme.startswith("alphaLc"):
        number = _format_number(n, "lowerLetter")
    elif scheme.startswith("alphaUc"):
        number = _format_number(n, "upperLetter")
    elif scheme.startswith("romanLc"):
        number = _format_number(n, "lowerRoman")
    elif scheme.startswith("romanUc"):
        number = _format_number(n, "upperRoman")
    else:
        number = str(n)
    if scheme.endswith("ParenBoth"):
        return "(%s)" % number
    if scheme.endswith("ParenR"):
        return number + ")"
    if scheme.endswith("Plain"):
        return number
    return number + "."


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
        limits.check_stop()
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
                    items.extend(("para", text) for text in
                                 _diagram_texts(_try_part(zf, parts, part, limits)))
                elif rel_type == "notesSlide":
                    notes_root = _try_part(zf, parts, part, limits)
                    tree = _ppt_tree(notes_root) if notes_root is not None else None
                    for sp in (tree if tree is not None else []):
                        ph = _child(_child(_child(sp, "nvSpPr"), "nvPr"), "ph")
                        if ph is not None and ph.get("type") == "body":
                            notes.extend(_a_paragraphs(_child(sp, "txBody")))
        heading = (title[0] if title else "").replace("\n", " ")
        hidden = slide is not None and (slide.get("show") or "").lower() in ("0", "false")
        if hidden:      # (kept: a hidden slide can still hold facts; marked like a hidden sheet)
            heading = (heading + " (hidden)").strip()
        out.add("slide", heading, n, **({"hidden": True} if hidden else {}))
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
_pypdf_slot = threading.Semaphore(1)  # one pypdf read at a time (pypdf is pure Python: no loss)
_pypdf_abandoned = []                 # pypdf reads given up on (PDF_TIME_MAX) and maybe still running

_CRYPTO_NOTE = "secured PDF (AES): needs the optional cryptography package - run Install Squish.bat again"
_GARBLED_NOTE = "some text may be garbled (PDF fonts without a text map)"


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


def _visible_chars(pages):
    """Number of characters other than spaces in a list of page texts."""
    return sum(len(re.sub(r"\s", "", p or "")) for p in pages)


def _pages_read(result):
    """How many of a reader result's pages were read (the rest were not reached)."""
    read = result.get("read")
    count = len(result["pages"])
    return count if type(read) is not int else max(0, min(read, count))


_PYPDF_BUSY = "busy"     # _pdf_with_pypdf's answer when pypdf was busy with another PDF


def _pdf_with_pypdf(module, data, name=None, stop=None):
    """Pages read with pypdf: a dict like _pdf_with_pdftext's, None if pypdf
    can't open the PDF (or on Cancel), or _PYPDF_BUSY when pypdf is still busy
    with a PDF it was too slow to read (the built-in reader is used instead).

    pypdf runs in a helper thread so that a PDF it gets stuck on cannot hang
    Squish: after PDF_TIME_MAX seconds, or at once on Cancel, the thread is
    left to finish its current page in the background and the pages read so
    far are used (marked "cut" and "timed"). With ``name``, a PDF that
    is_drawing() from its name and page sizes is not read: its status is "drawing".
    """
    with _pypdf_lock:
        _pypdf_abandoned[:] = [t for t in _pypdf_abandoned if t.is_alive()]
        busy = bool(_pypdf_abandoned)
    if busy:
        return _PYPDF_BUSY      # an abandoned read still holds pypdf
    if not _take_pypdf_slot(stop):
        return None if stop is not None and stop() else _PYPDF_BUSY
    state = {"result": None, "partial": None, "stop": False, "name": name, "cancel": stop}
    worker = threading.Thread(target=_pypdf_worker, args=(module, data, state), name="squish-pypdf")
    worker.daemon = True
    try:
        worker.start()
    except Exception:
        _pypdf_slot.release()
        return None
    deadline = time.time() + PDF_TIME_MAX * 1.1 + 1
    while worker.is_alive() and time.time() < deadline and not (stop is not None and stop()):
        worker.join(PYPDF_WAIT_S)
    if not worker.is_alive():
        return state["result"]
    state["stop"] = True
    with _pypdf_lock:
        _pypdf_abandoned.append(worker)
    if stop is not None and stop():
        return None       # Cancel: the worker stops after its current page
    partial = state["partial"]
    if partial is None:
        return {"pages": [], "page_sizes": [], "status": "error", "title": "", "count": 0,
                "note": "PDF too slow to read", "cut": True, "timed": True}
    result = dict(partial)
    result["pages"] = list(partial["pages"])
    result["page_sizes"] = list(partial["page_sizes"])[:len(result["pages"])]
    result["annotated"] = [i for i in partial.get("annotated", []) if i < len(result["pages"])]
    result["read"] = len(result["pages"])
    result["note"] = ("stopped after %d pages (slow to read)" % len(result["pages"]) if result["pages"]
                      else "PDF too slow to read")
    result["cut"] = result["timed"] = True
    return result


def _take_pypdf_slot(stop):
    """Wait up to PDF_TIME_MAX for the one pypdf slot, in short waits so Cancel
    is noticed. True when the slot was taken (the caller releases it)."""
    deadline = time.time() + PDF_TIME_MAX
    while not (stop is not None and stop()):
        if _pypdf_slot.acquire(timeout=PYPDF_WAIT_S):
            return True
        if time.time() >= deadline:
            return False
    return False


def _pypdf_worker(module, data, state):
    """Helper thread: read with pypdf, then let the next pypdf read start."""
    try:
        _pypdf_read(module, data, state)
    except Exception:
        pass              # (state["result"] stays None: the built-in reader is used)
    finally:
        _pypdf_slot.release()


def _needs_cryptography(module, exc):
    """True when pypdf failed because the optional 'cryptography' package is missing (AES)."""
    error = getattr(getattr(module, "errors", None), "DependencyError", None)
    return isinstance(error, type) and isinstance(exc, error)


def _pypdf_read(module, data, state):
    """Read the pages with pypdf into state["partial"]; the finished result goes in state["result"].

    Besides the pages, the result lists the pages with form fields, stamps or
    review comments ("annotated") and says whether a page uses a composite
    font, or a Symbol/Wingdings font, without a text map ("unmapped_fonts"):
    pypdf's text misses or garbles those, so _read_with_pypdf checks them with
    the built-in reader.
    """
    result = {"pages": [], "page_sizes": [], "status": "ok", "note": "", "title": "", "count": 0,
              "annotated": [], "unmapped_fonts": False}

    def protected(note):
        result["status"], result["note"] = "protected", note
        state["result"] = result

    try:
        reader = module.PdfReader(io.BytesIO(data), strict=False)
        encrypted = bool(reader.is_encrypted)
    except Exception as exc:
        if _needs_cryptography(module, exc):
            protected(_CRYPTO_NOTE)
        return
    if encrypted:
        try:
            ok = reader.decrypt("")
        except Exception as exc:
            if _needs_cryptography(module, exc):
                return protected(_CRYPTO_NOTE)
            ok = 0
        if not ok:
            return protected("password-protected PDF")
    try:
        result["count"] = len(reader.pages)
    except Exception as exc:
        if _needs_cryptography(module, exc):
            protected(_CRYPTO_NOTE)
        return
    try:
        meta = reader.metadata
        result["title"] = str((meta.title if meta is not None else "") or "")
    except Exception:
        pass
    count = min(result["count"], PAGE_MAX)
    if state.get("name") is not None:
        # Drawings: decided from the page sizes alone (no page content is read).
        sizes = [_pypdf_page_size(reader, i) for i in range(count)]
        if is_drawing(state["name"], result["count"], sizes):
            result["status"], result["page_sizes"] = "drawing", sizes
            state["result"] = result
            return
    state["partial"] = result
    started = time.time()
    failed = 0
    cancel = state.get("cancel")
    for i in range(count):
        if state["stop"] or (cancel is not None and cancel()):
            return
        if time.time() - started > PDF_TIME_MAX:
            result["note"] = "stopped after %d pages (slow to read)" % i
            result["cut"] = result["timed"] = True
            break
        try:
            page = reader.pages[i]
        except Exception:
            result["cut"] = True
            break
        size = _pypdf_page_size(reader, i)
        if _pypdf_content_bytes(page) > PDF_HEAVY_PAGE_BYTES:
            result["annotated"].append(i)      # the built-in reader's text is used for this page
            result["page_sizes"].append(size)
            result["pages"].append("")
            continue
        seen = []
        try:
            try:
                text = page.extract_text(visitor_text=_symbol_visitor(seen)) or ""
            except TypeError:           # an old pypdf without visitor_text
                del seen[:]
                text = page.extract_text() or ""
        except Exception as exc:
            if _needs_cryptography(module, exc):
                return protected(_CRYPTO_NOTE)
            failed += 1                 # e.g. one of pypdf's size limits
            text = ""
        text = _fix_symbol_chars(text, seen)
        if _has_typed_annotations(page):
            result["annotated"].append(i)
        if not result["unmapped_fonts"] and _unmapped_font(page):
            result["unmapped_fonts"] = True
        result["page_sizes"].append(size)
        result["pages"].append(text)
    if failed:
        result["cut"] = True
        if encrypted and failed == len(result["pages"]):
            return protected(_CRYPTO_NOTE)   # a secured PDF whose pages pypdf could not decrypt
        if not result["note"]:
            result["note"] = "%d page%s could not be read (too large or complex)" % (
                failed, "" if failed == 1 else "s")
    result["read"] = len(result["pages"])
    state["result"] = result


def _pypdf_content_bytes(page):
    """Stored (compressed) bytes of a pypdf page's content streams and of the
    forms it draws (one level, nothing decompressed); 0 when unknown."""
    def stored(obj):
        try:
            obj = obj.get_object()
            data = getattr(obj, "_data", None)
            if isinstance(data, bytes):
                return len(data)
            return int(obj.get("/Length") or 0)
        except Exception:
            return 0

    try:
        total = 0
        contents = page.get("/Contents")
        contents = contents.get_object() if contents is not None else None
        if isinstance(contents, list):
            total += sum(stored(c) for c in contents[:500])
        elif contents is not None:
            total += stored(contents)
        resources = page.get("/Resources")
        resources = resources.get_object() if resources is not None else None
        xobjects = resources.get("/XObject") if resources is not None else None
        xobjects = xobjects.get_object() if xobjects is not None else None
        for key in list(xobjects or {})[:500]:
            xobj = xobjects[key].get_object()
            if xobj.get("/Subtype") == "/Form":
                total += stored(xobj)
        return total
    except Exception:
        return 0


def _pypdf_page_size(reader, i):
    """(width, height) of page i from its media box (no page content is read)."""
    try:
        box = reader.pages[i].mediabox
        return (abs(float(box.width)), abs(float(box.height)))
    except Exception:
        return (0.0, 0.0)


def _has_typed_annotations(page):
    """True when a pypdf page has form fields, typed-on text, stamps or review
    comments (notes, clouds, highlights ... with /Contents), whose text pypdf's
    extract_text() leaves out."""
    try:
        annots = page.get("/Annots")
        annots = annots.get_object() if annots is not None else None
        for item in list(annots or [])[:2000]:
            annot = item.get_object()
            subtype = annot.get("/Subtype")
            if subtype in ("/Widget", "/FreeText", "/Stamp"):
                return True
            if subtype not in ("/Link", "/Popup") and str(annot.get("/Contents") or "").strip():
                return True
    except Exception:
        pass
    return False


def _unmapped_font(holder, depth=0):
    """True when a pypdf page (or form) uses a font pypdf cannot decode: a
    composite font without a /ToUnicode map (pypdf turns its glyph numbers
    into punctuation), or a Symbol/Wingdings font without one (pypdf gives
    plain letters: "R" for ☑, "m" for μ)."""
    try:
        resources = holder.get("/Resources")
        resources = resources.get_object() if resources is not None else None
        if resources is None:
            return False
        fonts = resources.get("/Font")
        fonts = fonts.get_object() if fonts is not None else {}
        for n, ref in enumerate(fonts.values()):
            if n >= 200:
                break
            font = ref.get_object()
            if _symbol_without_map(font):
                return True
            if font.get("/Subtype") == "/Type0" and "/ToUnicode" not in font:
                encoding = font.get("/Encoding")
                encoding = encoding.get_object() if encoding is not None else None
                # (A named CMap such as UniJIS-UCS2-H is decoded fine by pypdf.)
                if encoding in ("/Identity-H", "/Identity-V") or hasattr(encoding, "get_data"):
                    return True
        if depth < 2:
            xobjects = resources.get("/XObject")
            xobjects = xobjects.get_object() if xobjects is not None else {}
            for n, ref in enumerate(xobjects.values()):
                if n >= 200:
                    break
                form = ref.get_object()
                if form.get("/Subtype") == "/Form" and _unmapped_font(form, depth + 1):
                    return True
    except Exception:
        return False
    return False


def _symbol_without_map(font):
    """True for a simple Symbol or Wingdings font (pypdf font dictionary) with
    no /ToUnicode map, other than the standard Symbol and ZapfDingbats fonts
    (pypdf decodes those two itself)."""
    try:
        if font.get("/Subtype") not in ("/TrueType", "/Type1", "/MMType1") or "/ToUnicode" in font:
            return False
        base = str(font.get("/BaseFont") or "")
        if base in ("/Symbol", "/ZapfDingbats"):
            return False
        from squish_app import pdftext
        return bool(pdftext.symbol_font_kind(base))
    except Exception:
        return False


_PDF_PUA = re.compile("[\uf020-\uf0ff]")


def _symbol_visitor(seen):
    """pypdf visitor: list each private-use character with the kind of symbol font it came from."""
    def visit(text, cm=None, tm=None, font=None, size=None):
        if not text or not _PDF_PUA.search(text):
            return
        try:
            from squish_app import pdftext
            name = str(font.get("/BaseFont") or "") if font is not None else ""
            kind = pdftext.symbol_font_kind(name)
        except Exception:
            kind = None
        seen.extend((ch, kind) for ch in _PDF_PUA.findall(text))
    return visit


def _fix_symbol_chars(text, seen):
    """Turn the private-use codes U+F020-U+F0FF (how Word's PDFs write Symbol and
    Wingdings characters) into real characters, using the font each came from.
    If pypdf's text and the visitor's list disagree, the text is left to _tidy."""
    if not seen or [ch for ch, _kind in seen] != _PDF_PUA.findall(text):
        return text
    from squish_app import pdftext
    tables = {}
    kinds = iter(seen)

    def swap(m):
        kind = next(kinds)[1]
        if not kind:
            return m.group(0)
        if kind not in tables:
            tables[kind] = pdftext.symbol_pua_table(kind)
        return tables[kind].get(ord(m.group(0)), m.group(0))
    return _PDF_PUA.sub(swap, text)


def _pdf_with_pdftext(data, time_budget=None, stop=None):
    """Pages read with the built-in reader (pdftext.py), or None if it is missing.

    ``read`` is how many pages were read; ``cut`` says a time or size limit
    stopped the reading early, ``timed`` that it was the time limit.
    """
    try:
        from squish_app import pdftext
    except Exception:
        return None
    try:
        res = pdftext.extract_pdf(data, max_pages=PAGE_MAX, time_budget=time_budget, stop=stop)
    except Exception as e:
        return {"pages": [], "page_sizes": [], "status": "error", "title": "", "count": 0,
                "note": "damaged PDF (%s)" % type(e).__name__}
    res = dict(res or {})
    pages = list(res.get("pages") or [])
    sizes = list(res.get("page_sizes") or [])
    count = res.get("page_count") or res.get("count") or max(len(pages), len(sizes))
    read = res.get("pages_read")
    read = len(pages) if type(read) is not int else max(0, min(read, len(pages)))
    stopped = res.get("stopped") or ""
    return {"pages": pages, "page_sizes": sizes, "status": res.get("status") or "ok",
            "note": res.get("note") or "", "title": res.get("title") or "", "count": count,
            "read": read, "cut": bool(stopped) or read < len(pages), "timed": stopped == "time"}


def _read_with_pypdf(module, name, data, doc, stop=None):
    """Read a PDF with pypdf, using the built-in reader where it does better.

    Returns a reader result, or None to read the PDF with the built-in reader alone.
    """
    doc["reader"] = "pypdf"
    result = _pdf_with_pypdf(module, data, name, stop)
    if result is _PYPDF_BUSY:
        doc["retry"] = True     # read without pypdf only because it was busy: read again next run
        return None
    drawing = result is not None and result["status"] == "drawing"
    other = None
    if drawing:
        # pypdf is very slow on CAD drawings (millions of drawing operators,
        # minutes and gigabytes for one sheet); the built-in reader skips them.
        # Its reading is kept whenever it found text; pypdf reads the drawing
        # only when the built-in reader found none.
        other = _pdf_with_pdftext(data, stop=stop)
        if other is not None and other["status"] == "ok" and (
                _visible_chars(other["pages"]) or (not other["note"] and not other.get("cut"))):
            doc["reader"] = "pdftext"
            return other
        result = _pdf_with_pypdf(module, data, stop=stop)
        if result is _PYPDF_BUSY:
            doc["retry"] = True
            result = None
        if result is None:
            doc["reader"] = "pdftext"
            return other
    if result is None:
        return None
    letters = _visible_chars(result["pages"])
    weak = (result["status"] in ("protected", "error") or result.get("cut") or
            (result["status"] == "ok" and letters < NO_TEXT_PER_PAGE * max(1, _pages_read(result))))
    if weak:
        # pypdf could not decrypt it (AES without 'cryptography'), stopped early
        # (time or one of its size limits) or found next to no text: the
        # built-in reader may do better.
        if not drawing:
            other = _pdf_with_pdftext(data, stop=stop)
        if other is not None and other["status"] == "ok" and _visible_chars(other["pages"]) > letters:
            doc["reader"] = "pdftext"
            return other
        return result
    if result["status"] == "ok" and not drawing:
        return _recheck_with_builtin(data, result, doc, stop)
    return result


def _recheck_with_builtin(data, result, doc, stop=None):
    """Correct pypdf's text with the built-in reader's reading of the same PDF.

    pypdf turns composite fonts without a text map into punctuation (the
    built-in reader decodes them from the embedded font), leaves out form-field
    values and typed comments, and moves a character drawn after the rest of
    its line (LibreOffice's font fallback for a symbol) to the line end
    ("85 m ... μ"). The built-in reader puts each piece where it is on the line.
    """
    other = _pdf_with_pdftext(data, time_budget=PDF_RECHECK_TIME, stop=stop)
    if other is None or other["status"] != "ok":
        if result.get("unmapped_fonts"):
            result["note"] = result["note"] or _GARBLED_NOTE
        return result
    if result.get("unmapped_fonts"):
        if not other.get("cut") and \
                _visible_chars(other["pages"]) >= max(1, _visible_chars(result["pages"]) // 2):
            doc["reader"] = "pdftext"
            return other
        result["note"] = result["note"] or _GARBLED_NOTE
    pages = result["pages"]
    read = min(_pages_read(other), len(pages))
    replaced = set()
    for i in result.get("annotated", []):
        if i < read and _visible_chars([other["pages"][i]]) > _visible_chars([pages[i]]):
            pages[i] = other["pages"][i]       # (form-field values and typed comments)
            replaced.add(i)
    for i in range(read):
        if i not in replaced:
            pages[i] = _fix_glyph_order(pages[i], other["pages"][i])
    return result


def _glyph_key(line):
    """A line's characters for comparing two readings: no spaces, and look-alike
    characters made the same (NFKC: the micro sign some pypdf versions give is
    the Greek mu, a ligature is its letters)."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", _tidy(line)))


def _moved_glyphs(line, other):
    """True when two readings of one line hold the same characters in another order.
    Lines with right-to-left script are left alone (pypdf keeps those in reading order)."""
    a = _glyph_key(line)
    b = _glyph_key(other)
    if a == b or sorted(a) != sorted(b):
        return False
    return not any(unicodedata.bidirectional(ch) in ("R", "AL") for ch in a)


def _moved_block(mine, theirs):
    """True when two runs of lines (spaces removed) hold the same characters and
    differ only by a few characters drawn elsewhere, not by whole lines in another order."""
    a, b = "".join(mine), "".join(theirs)
    if a == b or sorted(a) != sorted(b) or any(unicodedata.bidirectional(ch) in ("R", "AL") for ch in a):
        return False
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    return sum(i2 - i1 for tag, i1, i2, _j1, _j2 in ops if tag != "equal") <= 8


def _fix_glyph_order(page, other):
    """pypdf's page text with each line (or short run of lines) whose characters
    the built-in reader has in another order taken from the built-in reader
    ("85 m on steel. μ" -> "85 μm on steel."). The two readings are lined up
    line by line first, so lines that differ elsewhere on the page do not matter."""
    lines = page.split("\n")
    mine = [i for i, line in enumerate(lines) if line.strip()]
    theirs = [line for line in other.split("\n") if line.strip()]
    if not mine or not theirs or max(len(mine), len(theirs)) > 3000:
        return page
    a = [_glyph_key(lines[i]) for i in mine]
    b = [_glyph_key(line) for line in theirs]
    swaps = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "replace":
            continue
        if i2 - i1 <= 12 and j2 - j1 <= 12 and _moved_block(a[i1:i2], b[j1:j2]):
            swaps.append((i1, i2, theirs[j1:j2]))      # a whole short run (a split line, a hyphen pair)
        elif i2 - i1 == j2 - j1:
            swaps.extend((i1 + k, i1 + k + 1, [theirs[j1 + k]]) for k in range(i2 - i1)
                         if _moved_glyphs(lines[mine[i1 + k]], theirs[j1 + k]))
    for k1, k2, new in reversed(swaps):
        lines[mine[k1]:mine[k2 - 1] + 1] = new
    return "\n".join(lines) if swaps else page


_TITLE_PREFIX = re.compile(r"^(Microsoft (Word|Excel|PowerPoint) - )", re.I)


def _read_pdf(name, data, out, doc, stop=None):
    result = None
    module = _get_pypdf()
    if module is not None:
        result = _read_with_pypdf(module, name, data, doc, stop)
    if result is None and not (stop is not None and stop()):
        result = _pdf_with_pdftext(data, stop=stop)
        doc["reader"] = "pdftext"
    if stop is not None and stop():
        raise _Stopped()
    if result is None:
        doc["status"], doc["note"] = "unsupported", "PDF reader not available"
        return
    if result.get("timed"):
        doc["retry"] = True      # cut short by a time limit: worth reading again next run
    doc["title"] = _TITLE_PREFIX.sub("", _tidy(result.get("title") or "")).strip()[:300]
    doc["pages"] = result["count"] or len(result["pages"])
    sizes = result["page_sizes"]
    page_texts = [_PDF_COMMENT_LINE.sub("", p or "") for p in result["pages"]]
    doc["drawing"] = is_drawing(name, doc["pages"], sizes, page_texts)
    # The name-independent parts of "drawing", for drawing_for_name():
    doc["large_pages"] = _large_pages(sizes)
    doc["prose_pages"] = doc["large_pages"] and _prose_pages(page_texts)
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
    read = _pages_read(result)
    if pages and _visible_chars(pages) >= NO_TEXT_PER_PAGE * max(1, read):
        if result.get("note"):
            doc["note"] = result["note"]
    elif _has_typed_page(pages[:read]):
        # Typed pages among scanned ones (a letter with scanned mark-ups): the text is kept.
        doc["note"] = "; ".join(x for x in (result.get("note"), _scanned_pages_note(pages[:read])) if x)
    elif result.get("cut"):
        # Cut short by a time or size limit before any text was found: not a scan.
        doc["status"] = "error"
        doc["note"] = result.get("note") or "too large or complex to read in time"
    else:
        doc["status"] = "no_text"
        doc["note"] = result.get("note") or "scanned or image-only PDF"
    if doc["status"] == "ok" and not doc["note"] and doc["pages"] > read:
        doc["note"] = "partly read: first %d of %d pages" % (read, doc["pages"])


def _has_typed_page(pages):
    """True when at least one page holds TYPED_PAGE_MIN or more visible characters."""
    return any(_visible_chars([p]) >= TYPED_PAGE_MIN for p in pages)


def _scanned_pages_note(pages):
    """Note naming the pages without text: "pages 2-26 have no text (scanned?)",
    "page 3 has no text (scanned?)", or "25 of 26 pages ..." when they are scattered."""
    empty = [n for n, p in enumerate(pages, 1) if _visible_chars([p]) < NO_TEXT_PER_PAGE]
    if not empty:
        return ""
    ranges = []
    for n in empty:
        if ranges and ranges[-1][1] == n - 1:
            ranges[-1][1] = n
        else:
            ranges.append([n, n])
    if len(ranges) > 3:
        return "%d of %d pages have no text (scanned?)" % (len(empty), len(pages))
    names = ", ".join(str(a) if a == b else "%d-%d" % (a, b) for a, b in ranges)
    if len(empty) == 1:
        return "page %s has no text (scanned?)" % names
    return "pages %s have no text (scanned?)" % names


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


def _large_pages(page_sizes):
    """True when most pages are A3 or larger."""
    big = sum(1 for w, h in page_sizes if min(w, h) >= 820)
    return bool(page_sizes) and big * 2 > len(page_sizes)


def is_drawing(name, pages, page_sizes, page_texts=None):
    """A PDF whose pages are mostly A3 or larger (unless it reads like a
    document, see reads_like_document), or a short PDF named like a drawing."""
    if _large_pages(page_sizes) and not reads_like_document(name, page_texts or []):
        return True
    return bool(pages) and pages <= 5 and looks_like_drawing_name(name)


def drawing_for_name(doc, name):
    """is_drawing() for a PDF's DocText shown under ``name``, from the parts of
    it that don't depend on the name ("large_pages", "prose_pages"), so a PDF
    cached by content is classed by the name it is shown under. A DocText
    without them (an older cache entry, or not a PDF) keeps its "drawing"."""
    if "large_pages" not in doc:
        return bool(doc.get("drawing"))
    if doc["large_pages"] and not (_named_like_document(name) or doc.get("prose_pages")):
        return True
    pages = doc.get("pages")
    return bool(pages) and pages <= 5 and looks_like_drawing_name(name)


# A "sentence line" on a PDF page: 8+ words, at least half of them starting lower case.
_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")


def _sentence_lines(text):
    """The page's lines that read like running sentences (stripped)."""
    found = []
    for line in (text or "").splitlines():
        words = _WORD.findall(line)
        if len(words) >= 8 and sum(1 for w in words if w[0].islower()) * 2 >= len(words):
            found.append(line.strip())
    return found


def reads_like_document(name, page_texts):
    """True for a large-page PDF that is really a document (an A3 report): named
    like one (report, programme, register ...) without a drawing number, or most
    pages hold 300+ characters of sentences that are not repeated on other pages
    (title block notes repeat)."""
    return _named_like_document(name) or _prose_pages(page_texts)


def _named_like_document(name):
    """True for a name like a document's (report, programme ...) without a drawing number."""
    base = os.path.splitext(re.split(r"[\\/]", name or "")[-1])[0]
    return bool(_NOT_DRAWING.search(base) and not _DRAWING_CODE.search(base))


def _prose_pages(page_texts):
    """True when most pages hold 300+ characters of sentences not repeated on other pages."""
    pages = [_sentence_lines(t) for t in page_texts]
    seen = {}
    for lines in pages:
        for line in set(lines):
            seen[line] = seen.get(line, 0) + 1
    prose = sum(1 for lines in pages
                if sum(len(x) for x in lines if seen[x] == 1 or len(pages) == 1) >= 300)
    return bool(pages) and prose * 2 > len(pages)


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
# A line that starts with a drawing or document number ('RD-ST-1002 ...', '623.0001-ST-1200 ...').
_CODE_START = re.compile(r"^(?=\S*\d{3})[A-Z0-9.]+-[A-Z0-9.\-]+\s")
# A table row's last cell: a figure (with its unit), a date, or a revision and sheet size ('B A1').
_ROW_END = re.compile(r"(?:\d(?:[.,]\d+)?\s?(?:mm|m|m2|m3|kN|kPa|MPa|kg|t|%|days?)?|\d{1,2}/\d{1,2}/\d{2,4}"
                      r"|\b[A-Z0-9]{1,2}\s+A[0-4])\)?$")
_ROW_REV_SIZE = re.compile(r"\b[A-Z0-9]{1,2}\s+A[0-4]$")
_ROW_NUMBER = re.compile(r"(?:^|\s)\d")
# The start of a table row: a number or a code ('12', 'F4', 'BL-LC-5509-1').
_ROW_START = re.compile(r"^(?:\d|[A-Z]{1,4}[-.]?\d|[A-Z]{1,4}-[A-Z]{1,4}-?\d)")


def table_row(line):
    """True for a PDF line that looks like a table row: it ends in a figure,
    unit, date or revision/sheet size and holds 3 or more numbers, or ends in a
    revision and sheet size ("RD-ST-1002 GENERAL NOTES B A1")."""
    if not _ROW_END.search(line):
        return False
    return bool(_ROW_REV_SIZE.search(line)) or len(_ROW_NUMBER.findall(line)) >= 3


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
    if _CODE_START.match(line):
        return 0          # a register row ('RD-ST-1002 GENERAL NOTES B A1'), not a heading
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
    or a short line ends a paragraph, and so does a table row (a line ending
    in a figure with 3+ numbers) before a line starting with a capital or a
    digit, or a line ending ")" (a header row) before a coded row. The first
    and last lines of the page (usually its header and footer) stay on their
    own, as do contents-list lines, so the digest can recognise and drop them.
    Review comments ("[comment: ...]" lines, see pdftext) come last, each as
    its own paragraph; they play no part in finding the header and footer.
    """
    lines = text.split("\n")
    comments = [line for line in lines if line.startswith(_PDF_COMMENT)]
    if comments:
        lines = [line for line in lines if not line.startswith(_PDF_COMMENT)]
        while lines and not lines[-1]:
            lines.pop()
    _pdf_body_blocks(lines, out)
    for comment in comments:
        out.add("para", comment)


_PDF_COMMENT = "[comment: "
_PDF_COMMENT_LINE = re.compile(r"(?m)^\[comment: .*$")


def _pdf_body_blocks(lines, out):
    """_pdf_page_blocks for the page's own lines."""
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
        if joinable and ((table_row(lines[i - 1]) and (line[:1].isupper() or line[:1].isdigit()))
                         or (lines[i - 1].endswith(")") and _ROW_START.match(line))):
            joinable = False      # a table row ends at its last figure; a header row before a coded row
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
        if _read_csv(name, text, out, doc):
            return
    _text_blocks(text, out, markdown=(ext == ".md"))


def _read_rtf(data, out):
    from squish_app import msgfile
    try:
        mode, text = msgfile.rtf_to_html_or_text(data, symbol_char=_rtf_symbol_char)
    except msgfile.MsgFileError as e:
        raise _Unsafe(str(e))
    if mode == "html":
        from squish_app import readers
        text = readers.html_to_text(text)
    for line in _tidy(text).split("\n"):
        line = line.strip(" |")       # (a table row's text ends with its cell separator)
        if line:
            out.add("item" if _MD_ITEM.match(line) else "para", line)


def _rtf_symbol_char(font_name, code):
    """For msgfile's RTF reader: the character a code (a byte, or U+F020-U+F0FF)
    shows in a Symbol or Wingdings font, as in Word runs; None for other fonts."""
    kind = _SYMBOL_RUN_FONTS.get((font_name or "").lower().replace(" ", "").replace("-", ""))
    if kind is None:
        return None
    return _symbol_run_text(chr(code), kind)


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


def _read_csv(name, text, out, doc):
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
    if total > len(rows):
        doc["note"] = "partly read: first %d of %d rows" % (len(rows), total)
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
    started = time.time()
    for info in infos:
        limits.check_stop()
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
        same = {}         # the member's sha1, when read (the digest spots copies of other documents)
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
        elif time.time() - started > ZIP_TIME_MAX:
            member = _new_doc(kind, "error", "not read: the zip took too long to read")
            doc["retry"] = True
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
                same["sha1"] = hashlib.sha1(member_data).hexdigest()
                room = max(0, min(ZIP_MEMBER_TEXT_MAX, out.text_max - out.stored))
                member = _extract_data(name, member_data, limits, room, depth + 1)
                out.stored += sum(len(b["text"]) for b in member["blocks"])
                out.chars += member["chars"]
                if member.get("retry"):
                    doc["retry"] = True
        out.add("member", name, 0, size=info.file_size, doc=member, **same)
    if hidden:
        out.add("para", "(+%d more files)" % hidden)
    doc["pages"] = None
