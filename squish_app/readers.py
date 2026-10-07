"""Read one filed email (.msg or .eml) into an EmailRecord.

An EmailRecord is a plain JSON-friendly dict (see DESIGN.md) so that it can be
cached between runs:

    path, date, sender_name, sender_email, to, cc, subject, body, attachments,
    message_id, in_reply_to, conversation_topic, item_class, auto_reply, meeting,
    reader

Outlook .msg files are read with the optional ``extract-msg`` package when it is
installed (it is well tested on real Outlook files). If it is missing, or fails
on a particular file, the built-in reader in ``msgfile.py`` is used instead.
A .msg bigger than EXTRACT_MSG_MAX_BYTES is read with the built-in reader
first, because extract-msg loads every attachment into memory and Squish only
needs attachment names and sizes (extract-msg is tried if that fails).
.eml files are read with Python's own ``email`` package.

Every string in a record is valid Unicode: lone "surrogate" characters (raw
8-bit header bytes, half emoji) are repaired, so the record can always be
cached and written out.

The body is plain text but is NOT cleaned here: quoted history, signatures and
so on are removed later by cleaning.py.

An email attached to the filed email (forwarded "as attachment", or attached to
show what the client wrote) is read too, one level deep: its sender, date,
recipients, subject and text (capped at EMBEDDED_BODY_MAX characters) go in
that attachment's "email" entry, so the digest can show it. Every other
attachment has "email": None.

extract-msg's slow RTF de-encapsulation (RTFDE) is switched off; an RTF-only
body is converted by msgfile's own converter, whichever reader is used.

Documents (v1.1): with ``read_email(path, want_docs=True)`` the bytes of
attachments that docs.py can condense (a supported extension, at most
docs.DOC_MAX_BYTES) are kept in a transient "_data" entry, plus "sha1" (of the
bytes) and "doc_size". The engine hands "_data" to docs.extract and removes it
before the record is cached; it is never written anywhere. Attachments of an
attached email are not read (one level only). A .msg attachment that is only a
link to a shared file (an Outlook cloud attachment) gets "link": True.

Set the environment variable SQUISH_NO_EXTRACT_MSG=1 to always use the
built-in .msg reader (handy for troubleshooting).
"""

import email
import email.parser
import email.policy
import email.utils
import hashlib
import logging
import mimetypes
import os
import re
import threading
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser

from . import docs, msgfile
from .paths import long_path, short_path

SUPPORTED_EXTENSIONS = (".msg", ".eml")

# Bigger .msg files are read with the built-in reader first: extract-msg loads
# every attachment into memory (drawings can be tens of MB per email).
EXTRACT_MSG_MAX_BYTES = 2 * 1024 * 1024

# The text of an attached email is kept up to this many characters.
EMBEDDED_BODY_MAX = 30000

# PR_ATTACH_METHOD values of an attachment that is a link to a file, not the
# file (by reference, by reference resolve, by reference only, and Outlook's
# cloud attachments: OneDrive / SharePoint). Such attachments get "link": True.
LINK_METHODS = (2, 3, 4, 7)

READER_EXTRACT_MSG = "extract_msg"
READER_BUILTIN_MSG = "builtin_msg"
READER_EML = "eml"


# --------------------------------------------------------------------------
# Optional extract-msg package
# --------------------------------------------------------------------------

_extract_msg_lock = threading.Lock()
_extract_msg_state = {"checked": False, "module": None}


def _get_extract_msg():
    """The extract_msg module, or None if it is not installed (imported once)."""
    if os.environ.get("SQUISH_NO_EXTRACT_MSG"):
        return None
    with _extract_msg_lock:
        if not _extract_msg_state["checked"]:
            _extract_msg_state["checked"] = True
            try:
                import extract_msg  # optional dependency
                for name in ("extract_msg", "RTFDE", "olefile"):
                    logging.getLogger(name).setLevel(logging.CRITICAL)
                _extract_msg_state["module"] = extract_msg
            except Exception:
                _extract_msg_state["module"] = None
        return _extract_msg_state["module"]


def backend_status():
    """Short human description of how .msg files will be read."""
    module = _get_extract_msg()
    if module is not None:
        version = str(getattr(module, "__version__", "")).strip()
        return ("Outlook .msg: extract-msg %s" % version).strip()
    return "Outlook .msg: built-in reader"


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def read_email(path, want_docs=False):
    """Read a .msg or .eml file and return an EmailRecord dict.

    With ``want_docs``, attachments that are documents (see wants_doc_data) get
    "_data" (their bytes, for the engine to condense and then remove), "sha1"
    and "doc_size".

    Raises an exception (with a readable message) if the file can't be read.
    """
    display = os.path.abspath(short_path(str(path)))
    ext = os.path.splitext(display)[1].lower()
    if ext == ".msg":
        rec = _read_msg(display, want_docs)
    elif ext == ".eml":
        rec = _read_eml(display, want_docs)
    else:
        raise ValueError("not an email file (expected .msg or .eml): %s" % display)
    rec = _clean_strings(rec)
    _take_doc_data(rec, want_docs)
    return rec


def wants_doc_data(name, size):
    """True for an attachment whose bytes are kept for docs.extract: a supported
    document type (Word, Excel, PowerPoint, PDF, text, zip) of at most
    docs.DOC_MAX_BYTES."""
    return (docs.is_supported(name or "") and isinstance(size, int)
            and 0 <= size <= docs.DOC_MAX_BYTES)


def _take_doc_data(rec, want_docs):
    """Remove the raw "data" the readers left on attachments. With
    ``want_docs``, each document attachment keeps its bytes as "_data", plus
    "sha1" and "doc_size"; the bytes of everything else (inline images, the
    S/MIME container) are dropped."""
    for att in rec["attachments"]:
        data = att.pop("data", None)
        if (want_docs and isinstance(data, bytes) and not att["inline"]
                and att.get("email") is None and wants_doc_data(att["name"], len(data))):
            att["_data"] = data
            att["sha1"] = hashlib.sha1(data).hexdigest()
            att["doc_size"] = len(data)


def _empty_record(path, reader):
    return {
        "path": path,
        "date": "",
        "sender_name": "",
        "sender_email": "",
        "to": [],
        "cc": [],
        "subject": "",
        "body": "",
        "attachments": [],
        "message_id": "",
        "in_reply_to": "",
        "conversation_topic": "",
        "item_class": "",
        "auto_reply": False,
        "meeting": None,
        "reader": reader,
    }


# --------------------------------------------------------------------------
# Small text helpers
# --------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def _one_line(text):
    """Single-line field: no NULs, no line breaks, single spaces."""
    if not text:
        return ""
    return _WS.sub(" ", str(text).replace("\x00", "")).strip()


def _clean_name(name):
    name = _one_line(name)
    if len(name) >= 2 and name[0] == name[-1] and name[0] in "'\"":
        name = name[1:-1].strip()
    return name


def _smtp(address):
    """A lower-case SMTP address, or "" for X500/Exchange or junk values."""
    a = _one_line(address).strip("<>'\" ")
    if a.lower().startswith("smtp:"):
        a = a[5:]
    if "@" not in a or a.startswith("/") or " " in a or len(a) > 254:
        return ""
    return a.lower()


def _bare_name(address):
    """A display name from an address that is only a name ("Sam Brown", no @), else ""."""
    a = _clean_name(address)
    if not a or "@" in a or a.startswith("/") or ":" in a:
        return ""
    return a


def _first_smtp(candidates):
    for c in candidates or ():
        s = _smtp(c)
        if s:
            return s
    return ""


def _name_key(name):
    return _clean_name(name).lower()


_SURROGATE = re.compile("[\ud800-\udfff]")
_SURROGATE_PAIR = re.compile("[\ud800-\udbff][\udc00-\udfff]")
_ESCAPED_BYTES = re.compile("[\udc80-\udcff]+")


def _join_surrogate_pair(match):
    high, low = match.group(0)
    return chr(0x10000 + ((ord(high) - 0xD800) << 10) + (ord(low) - 0xDC00))


def _decode_escaped_bytes(match):
    raw = match.group(0).encode("utf-8", "surrogateescape")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def fix_surrogates(text):
    """Repair lone surrogate characters, which can't be saved as UTF-8.

    They come from raw 8-bit bytes in .eml headers (Python keeps those as
    U+DC80..U+DCFF) and from emoji split into two halves. Byte runs are decoded as
    UTF-8 (else Windows-1252), split pairs are joined, anything left becomes the
    replacement character.
    """
    if not _SURROGATE.search(text):
        return text
    text = _SURROGATE_PAIR.sub(_join_surrogate_pair, text)
    text = _ESCAPED_BYTES.sub(_decode_escaped_bytes, text)
    return _SURROGATE.sub("\ufffd", text)


def _clean_strings(value):
    """``value`` with fix_surrogates() applied to every string inside it."""
    if isinstance(value, str):
        return fix_surrogates(value)
    if isinstance(value, list):
        return [_clean_strings(v) for v in value]
    if isinstance(value, dict):
        return dict((k, _clean_strings(v)) for k, v in value.items())
    return value


def _normalise_newlines(text):
    if not text:
        return ""
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


def _iso(dt):
    """Aware datetime -> ISO string in this computer's local time zone.

    Naive datetimes are taken to be UTC.
    """
    if dt is None:
        return ""
    if isinstance(dt, str):
        dt = _parse_date_header(dt)
        if dt is None:
            return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        dt = dt.astimezone()
    except (OverflowError, OSError, ValueError):
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat()


def _parse_date_header(value):
    """RFC 2822 date string -> aware datetime, or None."""
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(_one_line(value))
    except Exception:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt.year < 1971 or dt.year > 2200:
        return None
    return dt


# --------------------------------------------------------------------------
# HTML -> text
# --------------------------------------------------------------------------

_SKIP_TAGS = set(["style", "script", "head", "title", "xml", "template", "svg"])
_BLOCK_TAGS = set([
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "address",
    "center", "section", "article", "header", "footer", "nav", "aside", "dl",
    "dt", "dd", "form", "fieldset", "figure", "figcaption", "main", "caption",
    "legend", "details", "summary",
])
_HTML_WS = re.compile(r"[ \t\r\n\f\v]+")
_RULE = "_" * 32


_LIST_STYLES = {"lower-alpha": "a", "lower-latin": "a", "upper-alpha": "A",
                "upper-latin": "A", "lower-roman": "i", "upper-roman": "I"}
_LIST_STYLE_RE = re.compile(r"list-style(?:-type)?\s*:\s*([a-z-]+)", re.I)
_ROMAN = ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
          (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"))


def _int_attr(value):
    """An HTML number attribute (start=, value=) as an int, or None."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _list_style(attrs):
    """Numbering style of an <ol>: "1", "a", "A", "i" or "I" (type=, else CSS)."""
    kind = (attrs.get("type") or "").strip()
    if kind in ("1", "a", "A", "i", "I"):
        return kind
    m = _LIST_STYLE_RE.search(attrs.get("style") or "")
    return _LIST_STYLES.get(m.group(1).lower(), "1") if m else "1"


def _list_label(n, style):
    """Label of item ``n`` of a numbered list: 3 -> "3", "c", "C", "iii" or "III"."""
    if n <= 0 or style not in ("a", "A", "i", "I"):
        return str(n)
    if style in ("a", "A"):
        letters = ""
        while n > 0:
            n, rest = divmod(n - 1, 26)
            letters = chr(ord("a") + rest) + letters
        label = letters
    else:
        if n >= 4000:
            return str(n)
        label = ""
        for value, numeral in _ROMAN:
            while n >= value:
                label += numeral
                n -= value
    return label.upper() if style in ("A", "I") else label


class _HtmlToText(HTMLParser):
    """Collects readable text from HTML.

    Block elements start new lines, <br> is a line break, table rows become one
    line with cells joined by " | " (layout tables whose cells hold several
    lines are written out line by line instead), and list items get a bullet
    or number. Styles, scripts and the <head> are skipped.
    """

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.root = []
        self.buf = self.root
        self.skip = 0
        self.pre = 0
        self.lists = []
        self.tables = []
        self.links = []   # open <a> tags: [href, [text pieces]]

    # -- output helpers --
    def _newline(self):
        if self.buf and not self.buf[-1].endswith("\n"):
            self.buf.append("\n")

    def _end_cell(self):
        table = self.tables[-1]
        if table["cell"] is not None:
            if table["row"] is None:
                table["row"] = []
            table["row"].append("".join(table["cell"]))
            table["cell"] = None
        self.buf = table["parent"]

    def _start_cell(self):
        self._end_cell()
        table = self.tables[-1]
        if table["row"] is None:
            table["row"] = []
        table["cell"] = []
        self.buf = table["cell"]

    def _end_row(self):
        self._end_cell()
        table = self.tables[-1]
        row = table["row"]
        table["row"] = None
        if not row:
            return
        cells = [_cell_lines(c) for c in row]
        if not any(cells):
            return
        parent = table["parent"]
        if parent and not parent[-1].endswith("\n"):
            parent.append("\n")
        if all(len(c) <= 1 for c in cells):
            values = [c[0] if c else "" for c in cells]
            while values and not values[-1]:
                values.pop()
            while values and not values[0]:
                values.pop(0)
            parent.append(" | ".join(values) + "\n")
        else:
            for c in cells:
                for line in c:
                    parent.append(line + "\n")

    def _end_table(self):
        self._end_row()
        table = self.tables.pop()
        self.buf = table["parent"]
        self._newline()

    def finish(self):
        while self.tables:
            self._end_table()
        return "".join(self.root)

    # -- parser callbacks --
    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip += 1
            return
        if tag == "body":
            self.skip = 0  # an unclosed <head> must not hide the body
            return
        if self.skip:
            return
        if tag == "br":
            self.buf.append("\n")
        elif tag == "hr":
            self._newline()
            self.buf.append(_RULE + "\n")
        elif tag == "table":
            self._newline()
            self.tables.append({"parent": self.buf, "row": None, "cell": None})
        elif tag == "tr":
            if self.tables:
                self._end_row()
                self.tables[-1]["row"] = []
            else:
                self._newline()
        elif tag in ("td", "th"):
            if self.tables:
                self._start_cell()
            else:
                self.buf.append(" ")
        elif tag == "ul":
            self._newline()
            self.lists.append(None)
        elif tag == "ol":
            # A numbered list: {"n": number of the last item, "style": "1", "a", "i"...}
            self._newline()
            named = dict((key, value) for key, value in attrs if key)
            start = _int_attr(named.get("start"))
            self.lists.append({"n": (1 if start is None else start) - 1,
                               "style": _list_style(named)})
        elif tag == "li":
            self._newline()
            numbered = self.lists[-1] if self.lists else None
            if numbered is not None:
                value = _int_attr(dict(attrs).get("value"))
                numbered["n"] = numbered["n"] + 1 if value is None else value
                self.buf.append(_list_label(numbered["n"], numbered["style"]) + ". ")
            else:
                self.buf.append("• ")
        elif tag == "pre":
            self._newline()
            self.pre += 1
        elif tag == "a":
            href = ""
            for key, value in attrs:
                if key == "href" and value:
                    href = value.strip()
            if len(self.links) < 20:
                self.links.append([href, []])
        elif tag in _BLOCK_TAGS:
            self._newline()

    def _end_link(self):
        """Like Outlook's plain text: "link text <https://...>" (unless they match)."""
        href, pieces = self.links.pop()
        text = _HTML_WS.sub(" ", "".join(pieces)).strip()
        if not text or not re.match(r"(?i)(https?|ftp)://", href):
            return
        bare = re.sub(r"(?i)^(https?|ftp)://", "", href).rstrip("/").lower()
        if text.rstrip("/").lower() in (href.rstrip("/").lower(), bare):
            return
        self.buf.append(" <%s>" % href)

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in ("td", "th"):
            if self.tables:
                self._end_cell()
        elif tag == "tr":
            if self.tables:
                self._end_row()
        elif tag == "table":
            if self.tables:
                self._end_table()
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self._newline()
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self._newline()
        elif tag == "a":
            if self.links:
                self._end_link()
        elif tag == "li" or tag in _BLOCK_TAGS:
            self._newline()

    def handle_data(self, data):
        if self.skip or not data:
            return
        if self.pre:
            self.buf.append(data)
            return
        text = _HTML_WS.sub(" ", data)
        if text.startswith(" ") and (not self.buf or self.buf[-1].endswith(("\n", " "))):
            text = text[1:]
        if text:
            self.buf.append(text)
            if self.links:
                self.links[-1][1].append(text)


def _cell_lines(text):
    lines = []
    for line in text.split("\n"):
        line = line.strip(" \t")
        if line.strip(" \t ​"):
            lines.append(line)
    return lines


def _tidy_text(text):
    """Trim each line, blank out whitespace-only lines, max one blank line in a row."""
    lines = []
    for line in text.split("\n"):
        line = line.strip(" \t")
        if not line.strip(" \t ​"):
            line = ""
        lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")


_TAG_RE = re.compile(r"<[^>]*>")


def html_to_text(html_text):
    """Convert an HTML document or fragment to readable plain text."""
    if not html_text:
        return ""
    html_text = html_text.replace("\x00", "")
    parser = _HtmlToText()
    try:
        parser.feed(html_text)
        parser.close()
        text = parser.finish()
    except Exception:
        text = ""
    if not text.strip():
        # Fall back to crude tag stripping (e.g. very broken HTML).
        crude = re.sub(r"(?is)<(style|script|head|title)[^>]*>.*?</\1\s*>", " ", html_text)
        crude = re.sub(r"(?i)<br\s*/?>|</p\s*>|</div\s*>|</tr\s*>", "\n", crude)
        text = unescape(_TAG_RE.sub(" ", crude))
    return _tidy_text(text)


# --------------------------------------------------------------------------
# Shared rules: inline attachments, auto-replies
# --------------------------------------------------------------------------

_IMAGE_EXTENSIONS = set([
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".emz", ".wmz",
    ".svg", ".webp", ".heic", ".ico",
])
_GENERIC_INLINE_NAME = re.compile(
    r"^(image\d{1,4}\.\w{2,4}"
    r"|outlook-[\w.\-]+\.(png|jpe?g|gif)"
    r"|~wrl\d+\.tmp"
    r"|att\d{5}\.(htm|html|txt)"
    r"|oledata\.mso"
    r")$",
    re.I,
)
ATTACH_RENDERED_IN_BODY = 0x4


_SIGNATURE_MIME = set([
    "application/pkcs7-signature", "application/x-pkcs7-signature",
    "application/pkcs7-mime", "application/x-pkcs7-mime",
])


def is_inline_attachment(name, mime="", content_id="", hidden=False, flags=0,
                         disposition=None, html_cids=None):
    """True for attachments that are part of the body (logos, pasted images).

    Inline = flagged hidden / rendered-in-body, an Outlook-generated name such
    as image001.png, an image whose Content-ID is referenced from the HTML, or
    a digital signature (smime.p7s) - not something anyone needs to see listed.
    """
    if hidden or (flags or 0) & ATTACH_RENDERED_IN_BODY:
        return True
    name = (name or "").strip()
    if _GENERIC_INLINE_NAME.match(name):
        return True
    if (mime or "").lower() in _SIGNATURE_MIME or name.lower().endswith(".p7s"):
        return True
    ext = os.path.splitext(name)[1].lower()
    is_image = (mime or "").lower().startswith("image/") or ext in _IMAGE_EXTENSIONS
    if not is_image:
        return False
    cid = (content_id or "").strip().strip("<>").strip().lower()
    return bool(cid and html_cids and cid in html_cids)


_AUTO_SUBJECT = re.compile(
    r"^\s*(automatic reply|auto[- ]?reply|auto[- ]?response|autoreply"
    r"|out of (the )?office( auto ?reply| reply)?)\s*:",
    re.I,
)


def is_auto_reply(item_class, subject, get_header):
    """Out-of-office replies and other machine-generated mail.

    ``get_header(name)`` returns a header value or "". Uses the message class
    (Outlook OOF templates), "Automatic reply:"-style subjects and the
    Auto-Submitted, Precedence, X-Autoreply and X-Auto-Response-Suppress
    headers (the last is only set on machine-generated mail; it is ignored on
    meeting items).
    """
    ic = (item_class or "").lower()
    if ic.startswith("ipm.note.rules.") or ".oof" in ic:
        return True
    if _AUTO_SUBJECT.match(subject or ""):
        return True
    auto = _one_line(get_header("Auto-Submitted")).lower()
    if auto and auto != "no":
        return True
    if _one_line(get_header("Precedence")).lower() in ("auto_reply", "junk"):
        return True
    if get_header("X-Autoreply") or get_header("X-Autorespond"):
        return True
    if _one_line(get_header("X-Auto-Response-Suppress")) and not ic.startswith("ipm.schedule"):
        return True
    return False


# --------------------------------------------------------------------------
# .msg files
# --------------------------------------------------------------------------

def _read_msg(display, want_docs=False):
    """Read a .msg: extract-msg first (if installed and the file is not big),
    else the built-in reader; whichever was not tried first is the fallback.
    With ``want_docs`` the readers also keep the bytes of document attachments
    (raw "data"; read_email turns it into "_data")."""
    path = long_path(display)
    module = _get_extract_msg()
    try:
        big = os.path.getsize(path) > EXTRACT_MSG_MAX_BYTES
    except OSError:
        big = False
    # Only pass the extra argument when it is needed (keeps the plain call unchanged).
    extra = {"want_data": wants_doc_data} if want_docs else {}
    eight_bit = False
    if module is not None and not big:
        try:
            fields = _fields_via_extract_msg(module, path, **extra)
            return _record_from_msg_fields(display, fields, READER_EXTRACT_MSG, want_docs)
        except _EightBitMessage:
            eight_bit = True   # the built-in reader reads 8-bit text better
        except Exception:
            pass  # fall back to the built-in reader below
    try:
        fields = msgfile.read_msg(path, **extra)
        return _record_from_msg_fields(display, fields, READER_BUILTIN_MSG, want_docs)
    except Exception as exc:
        if module is None or not (big or eight_bit):
            raise
        first_error = exc
    try:
        fields = _fields_via_extract_msg(module, path, allow_8bit=True, **extra)
        return _record_from_msg_fields(display, fields, READER_EXTRACT_MSG, want_docs)
    except Exception:
        raise first_error


def _parse_transport_headers(text):
    """Outlook's copy of the internet headers -> email.message.Message or None.

    Older Exchange servers put a "Microsoft Mail Internet Headers Version 2.0"
    line in front of the headers; that line (and any other leading line that is
    not a header) is skipped, or every header would be read as body text.
    """
    if not text:
        return None
    text = text.lstrip("\ufeff").lstrip("\r\n")
    while True:
        first, sep, rest = text.partition("\n")
        if not sep or not rest.strip():
            break   # keep the last line, whatever it is
        if ":" in first and not first.lstrip().lower().startswith("microsoft mail internet headers"):
            break
        text = rest.lstrip("\r\n")
    if ":" not in text:
        return None
    try:
        return email.parser.HeaderParser(policy=email.policy.compat32).parsestr(text)
    except Exception:
        return None


def _header_getter(headers):
    def get(name):
        if headers is None:
            return ""
        try:
            values = headers.get_all(name) or []
        except Exception:
            return ""
        return " ".join(msgfile.decode_header_text(str(v)) for v in values)
    return get


def _header_raw_getter(headers):
    """Like _header_getter, but the values are left encoded (RFC 2047) and
    joined with commas, so several To/Cc lines stay separate addresses."""
    def get(name):
        if headers is None:
            return ""
        try:
            values = headers.get_all(name) or []
        except Exception:
            return ""
        return " , ".join(str(v) for v in values)
    return get


def _addresses_from_raw(raw):
    """[(display name, address), ...] from a raw address header value.

    The addresses are split first and each name decoded afterwards, so an
    encoded "Müller, Jörg" stays one name instead of becoming two addresses."""
    if not raw:
        return []
    try:
        pairs = email.utils.getaddresses([raw])
    except Exception:
        return []
    return [(msgfile.decode_header_text(name), addr) for name, addr in pairs]


def _header_addresses(get_raw_header, *names):
    """Addresses from the named headers; ``get_raw_header`` is a _header_raw_getter."""
    out = []
    for name in names:
        out.extend(_addresses_from_raw(get_raw_header(name)))
    return out


def _address_book(get_raw_header):
    """Display name -> SMTP address, from the internet headers."""
    book = {}
    for name, addr in _header_addresses(get_raw_header, "From", "Sender", "Reply-To", "To", "Cc"):
        addr = _smtp(addr)
        key = _name_key(name)
        if addr and key and key not in book:
            book[key] = addr
    return book


def _record_from_msg_fields(display, f, reader, want_docs=False):
    """Turn the raw fields of a .msg (from either reader) into an EmailRecord.

    Attachments whose bytes the reader kept get them as raw "data" (read_email
    keeps or drops it); ``want_docs`` is passed on to an S/MIME email's MIME."""
    rec = _empty_record(display, reader)
    headers = _parse_transport_headers(f.get("headers") or "")
    get_header = _header_getter(headers)
    get_raw_header = _header_raw_getter(headers)
    book = _address_book(get_raw_header)

    rec["subject"] = _one_line(f.get("subject"))
    rec["item_class"] = _one_line(f.get("item_class")) or "IPM.Note"
    rec["conversation_topic"] = _one_line(f.get("conversation_topic") or get_header("Thread-Topic"))
    rec["message_id"] = _one_line(f.get("message_id") or get_header("Message-ID"))
    rec["in_reply_to"] = _one_line(f.get("in_reply_to") or get_header("In-Reply-To"))

    # Sender: "on behalf of" (PR_SENT_REPRESENTING_*) is who the email is from;
    # PR_SENDER_* is whoever actually pressed Send (often the same person).
    header_from = _header_addresses(get_raw_header, "From")
    hdr_name, hdr_addr = (header_from[0] if header_from else ("", ""))
    obo_name = _clean_name(f.get("on_behalf_name"))
    obo_addr = _first_smtp(f.get("on_behalf_addresses"))
    snd_name = _clean_name(f.get("sender_name"))
    snd_addr = _first_smtp(f.get("sender_addresses"))
    name = obo_name or _clean_name(hdr_name) or snd_name
    addr = obo_addr or _smtp(hdr_addr)
    if not addr and (not obo_name or obo_name.lower() == snd_name.lower()):
        addr = snd_addr
    if not addr:
        addr = book.get(_name_key(name), "") or _smtp(name)
    rec["sender_name"] = name or addr
    rec["sender_email"] = addr

    # Recipients.
    to, cc, seen = [], [], set()
    recipients = f.get("recipients") or []
    for r in recipients:
        rname = _clean_name(r.get("name"))
        raddr = _first_smtp(r.get("addresses")) or book.get(_name_key(rname), "") or _smtp(rname)
        rtype = r.get("type") or 1
        if rtype not in (1, 2) or not (rname or raddr):
            continue
        key = raddr or rname.lower()
        if key in seen:
            continue
        seen.add(key)
        (to if rtype == 1 else cc).append([rname or raddr, raddr])
    if not recipients:
        for target, hname, display_names in ((to, "To", f.get("display_to")),
                                             (cc, "Cc", f.get("display_cc"))):
            pairs = [(_clean_name(n), _smtp(a)) for n, a in _header_addresses(get_raw_header, hname)]
            if not pairs and display_names:
                pairs = [(_clean_name(n), book.get(_name_key(n), "") or _smtp(n))
                         for n in display_names.split(";")]
            for n, a in pairs:
                if (n or a) and (a or n.lower()) not in seen:
                    seen.add(a or n.lower())
                    target.append([n or a, a])
    rec["to"], rec["cc"] = to, cc

    # Date: sent time, else delivery time, else the header Date, else creation time.
    for dt in (None if f.get("unsent") else f.get("submit_time"),
               f.get("delivery_time"),
               _parse_date_header(get_header("Date")),
               f.get("creation_time")):
        if dt is not None:
            rec["date"] = _iso(dt)
            if rec["date"]:
                break

    # Body: plain text, else HTML converted to text, else RTF.
    body = _normalise_newlines(f.get("body") or "")
    if not body.strip() and f.get("html"):
        body = html_to_text(f["html"])
    if not body.strip() and f.get("rtf"):
        kind, text = f["rtf"]
        body = html_to_text(text) if kind == "html" else _tidy_text(_normalise_newlines(text))
    rec["body"] = body if body.strip() else ""

    html_cids = f.get("html_cids") or set()
    for a in f.get("attachments") or []:
        aname = _one_line(a.get("name")) or "attachment"
        if a.get("embedded") and not aname.lower().endswith((".msg", ".eml")):
            aname += ".msg"
        size = a.get("size")
        inline = is_inline_attachment(aname, a.get("mime") or "", a.get("content_id") or "",
                                      bool(a.get("hidden")), a.get("flags") or 0,
                                      None, html_cids)
        attached = None
        if isinstance(a.get("message"), dict) and not inline:
            try:
                attached = _attached_email(_record_from_msg_fields(display, a["message"], reader))
            except Exception:
                attached = None  # an unreadable attached email: keep its name only
        entry = {
            "name": aname,
            "size": int(size) if isinstance(size, int) else None,
            "inline": inline,
            "email": attached,
        }
        if isinstance(a.get("data"), bytes) and not a.get("embedded"):
            entry["data"] = a["data"]   # raw bytes; read_email keeps documents' only
        if a.get("method") in LINK_METHODS and not a.get("embedded"):
            entry["link"] = True        # a link to a shared file (OneDrive, SharePoint), no bytes
        rec["attachments"].append(entry)

    rec["auto_reply"] = is_auto_reply(rec["item_class"], rec["subject"], get_header)
    rec["meeting"] = _meeting_from_fields(f, rec["item_class"])
    _unpack_signed_msg(rec, f, want_docs)
    return rec


def _attached_email(sub):
    """The "email" entry of an attachment, from the EmailRecord of the attached
    email; None when it has neither text nor a sender."""
    if not (sub["body"].strip() or sub["sender_name"] or sub["sender_email"]):
        return None
    return {
        "sender_name": sub["sender_name"],
        "sender_email": sub["sender_email"],
        "date": sub["date"],
        "to": sub["to"],
        "cc": sub["cc"],
        "subject": sub["subject"],
        "body": sub["body"][:EMBEDDED_BODY_MAX],
    }


def _meeting_from_fields(f, item_class):
    """{"start", "end", "location"} for a meeting request, cancellation or
    appointment (local ISO times, end may be ""), else None."""
    start = f.get("meeting_start")
    if start is None or not item_class.lower().startswith(msgfile.MEETING_CLASSES):
        return None
    start_iso = _iso(start)
    if not start_iso:
        return None
    end = f.get("meeting_end")
    return {"start": start_iso, "end": _iso(end) if end is not None else "",
            "location": _one_line(f.get("meeting_location"))}


def _unpack_signed_msg(rec, f, want_docs=False):
    """Clear-signed S/MIME email (IPM.Note.SMIME...): Outlook keeps the text and
    the real attachments as MIME inside one smime.p7m attachment. Take the body
    and attachments from there; the .msg's own sender, date etc. are kept.
    Anything unexpected leaves the record as it was. (msgfile keeps the bytes
    of smime.p7m only, and only up to a size limit.)"""
    if not rec["item_class"].lower().startswith("ipm.note.smime") or rec["body"].strip():
        return
    visible = [raw for raw, att in zip(f.get("attachments") or [], rec["attachments"])
               if not att["inline"]]
    if len(visible) != 1:
        return
    att = visible[0]
    data = att.get("data")
    name = _one_line(att.get("name")).lower()
    if not isinstance(data, bytes):
        return
    if name != "smime.p7m" and (att.get("mime") or "").lower() != "multipart/signed":
        return
    try:
        inner = email.message_from_bytes(data, policy=email.policy.default)
        if not inner.get_content_type().startswith("multipart/"):
            return  # opaque-signed or encrypted: can't be read without keys/ASN.1
        unpacked = _record_from_eml_message(inner, rec["path"], rec["reader"],
                                            want_docs=want_docs)
    except Exception:
        return
    if unpacked["body"].strip() or unpacked["attachments"]:
        rec["body"] = unpacked["body"]
        rec["attachments"] = unpacked["attachments"]


# ---- extract-msg ---------------------------------------------------------

def _em_call(func, default=None):
    """Call ``func()``; return ``default`` on any error (older versions differ)."""
    try:
        value = func()
    except Exception:
        return default
    return default if value is None else value


def _em_method(obj, name):
    """obj.name, or obj._name in older extract-msg versions (0.41 had private names)."""
    return getattr(obj, name, None) or getattr(obj, "_" + name, None)


def _em_string(obj, prop_id):
    """String property ``prop_id`` (4 hex digits) of an extract-msg object."""
    getter = _em_method(obj, "getStringStream")
    if getter is None:
        return ""
    value = _em_call(lambda: getter("__substg1.0_" + prop_id), "")
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value or "").replace("\x00", "")


def _em_stream(obj, name):
    """Raw bytes of stream ``name`` of an extract-msg object, or None."""
    getter = _em_method(obj, "getStream")
    return _em_call(lambda: getter(name)) if getter is not None else None


def _em_prop(obj, tag):
    """Fixed-size property (8 hex digit tag, e.g. '0E200003') of an extract-msg object."""
    if hasattr(obj, "getPropertyVal"):
        return _em_call(lambda: obj.getPropertyVal(tag))
    props = _em_call(lambda: obj.props)
    if props is None:
        return None
    prop = _em_call(lambda: props.get(tag))
    return _em_call(lambda: prop.value) if prop is not None else None


def _em_datetime(value):
    """A date from extract-msg, or None for a missing or placeholder date.

    Same rule as msgfile.filetime_to_datetime: Outlook's "None" date (year
    4500/4501, extract-msg's NullDate) and a zero FILETIME (1601) are not real
    dates, so the next date in line (delivery, header, creation) is used.
    """
    if isinstance(value, str):
        value = _parse_date_header(value)
    if not isinstance(value, datetime):
        return None
    try:
        if value.tzinfo is None:
            utc = value.replace(tzinfo=timezone.utc)
        else:
            utc = value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None
    if utc.year < 1971 or utc.year >= 4500:
        return None
    return value


def _no_deencapsulation(rtf, body_type):
    """extract-msg hook: tell it the RTF body holds no text/HTML, so it never runs
    its slow RTF de-encapsulator (RTFDE) while opening the file. Squish converts
    the RTF itself with msgfile."""
    return None


def _open_with_extract_msg(module, path):
    kwargs = {"strict": False}
    enums = getattr(module, "enums", None)
    behaviour = getattr(enums, "ErrorBehavior", None) if enums is not None else None
    if behaviour is not None:
        flags = 0
        for name in ("ATTACH_SUPPRESS_ALL", "RTFDE", "STANDARDS_VIOLATION", "NAMED_NAME_STREAM"):
            flags |= int(getattr(behaviour, name, 0))
        kwargs["errorBehavior"] = behaviour(flags)
    # Without this, extract-msg runs RTFDE while opening every email that has
    # only an RTF body (seconds and hundreds of MB each). Attached emails get
    # the same options.
    kwargs["deencapsulationFunc"] = _no_deencapsulation
    open_msg = getattr(module, "openMsg", None) or getattr(module, "Message")
    return open_msg(path, **kwargs)


class _EightBitMessage(ValueError):
    """An old 8-bit .msg (or attached email in one): the built-in reader reads
    its text better, so the whole file is handed to it."""


def _fields_via_extract_msg(module, path, allow_8bit=False, want_data=None):
    """Read a .msg with extract-msg into the same raw fields msgfile produces.

    An 8-bit message raises _EightBitMessage unless ``allow_8bit`` (used only
    when the built-in reader has already failed on the file). ``want_data`` is
    as for msgfile.read_msg."""
    msg = _open_with_extract_msg(module, path)
    try:
        return _em_message_fields(msg, allow_8bit=allow_8bit, want_data=want_data)
    finally:
        try:
            msg.close()
        except Exception:
            pass


def _em_message_fields(msg, nested=False, allow_8bit=False, want_data=None):
    """The raw fields (as msgfile produces them) of an open extract-msg message.

    ``nested`` is True for an email attached to the filed email: then its
    meeting details are not read and its own attached emails get their name
    only (as in msgfile, one level of attached emails is read). ``want_data``
    (not used for nested emails) is as for msgfile.read_msg.
    """
    if not hasattr(msg, "recipients") or not hasattr(msg, "body"):
        raise ValueError("not an email item")  # e.g. a contact: use the built-in reader
    if not allow_8bit and _em_call(lambda: msg.areStringsUnicode) is False:
        # Old 8-bit .msg: extract-msg reads code page 28591 as strict Latin-1
        # (smart quotes and dashes become control characters) and guesses
        # ISO-8859-15 when there is no code page. The built-in reader reads
        # them as Windows-1252, or works the code page out from PR_INTERNET_CPID.
        raise _EightBitMessage("8-bit strings")
    f = {
        "subject": _em_call(lambda: msg.subject, ""),
        "conversation_topic": _em_string(msg, "0070"),
        "item_class": _em_call(lambda: msg.classType, ""),
        "message_id": _em_call(lambda: msg.messageId, ""),
        "in_reply_to": _em_call(lambda: msg.inReplyTo, ""),
        "headers": _em_call(lambda: msg.headerText, "") or _em_string(msg, "007D"),
        "sender_name": _em_string(msg, "0C1A"),
        "sender_addresses": [_em_string(msg, "5D01"), _em_string(msg, "0C1F")],
        "on_behalf_name": _em_string(msg, "0042"),
        "on_behalf_addresses": [_em_string(msg, "5D02"), _em_string(msg, "0065")],
        "display_to": _em_string(msg, "0E04"),
        "display_cc": _em_string(msg, "0E03"),
        "recipients": [],
        "attachments": [],
        "submit_time": _em_datetime(_em_call(lambda: msg.date)),
        "delivery_time": _em_datetime(_em_prop(msg, "0E060040")),
        "creation_time": _em_datetime(_em_prop(msg, "30070040")),
        "unsent": False,  # msg.date is already None for unsent items
        "body": None,
        "html": None,
        "rtf": None,
        "html_cids": set(),
        "meeting_start": None,
        "meeting_end": None,
        "meeting_location": "",
    }
    if not nested and str(f["item_class"]).lower().startswith(msgfile.MEETING_CLASSES):
        f["meeting_start"] = (_em_datetime(_em_call(lambda: msg.appointmentStartWhole))
                              or _em_datetime(_em_prop(msg, "00600040")))
        f["meeting_end"] = (_em_datetime(_em_call(lambda: msg.appointmentEndWhole))
                            or _em_datetime(_em_prop(msg, "00610040")))
        f["meeting_location"] = str(_em_call(lambda: msg.location, "") or "")

    for r in _em_call(lambda: msg.recipients, []) or []:
        rtype = _em_prop(r, "0C150003")
        if not isinstance(rtype, int):
            rtype = _em_call(lambda: int(getattr(r.type, "value", r.type)), 1)
        f["recipients"].append({
            "name": _em_call(lambda: r.name, ""),
            "addresses": [_em_call(lambda: r.smtpAddress, ""), _em_call(lambda: r.email, "")],
            "type": rtype & 0x0F,
        })

    any_cid = False
    for att in _em_call(lambda: msg.attachments, []) or []:
        a = _em_attachment(att, read_message=not nested, allow_8bit=allow_8bit,
                           want_data=None if nested else want_data)
        any_cid = any_cid or bool(a["content_id"])
        f["attachments"].append(a)

    body = _em_call(lambda: msg.body)
    if isinstance(body, bytes):
        body = body.decode("utf-8", "replace")
    f["body"] = body
    need_html = not (body and body.strip())
    if need_html or any_cid:
        cpid = _em_prop(msg, "3FDE0003")
        raw_html = _em_stream(msg, "__substg1.0_10130102")
        if raw_html is None:
            raw_html = _em_string(msg, "1013") or None
        f["html"] = msgfile.decode_html_bytes(raw_html, cpid) if raw_html else None
        f["html_cids"] = msgfile.find_cids(f["html"])
    if need_html and not (f["html"] and f["html"].strip()):
        # extract-msg's own RTF de-encapsulation (RTFDE) is switched off in
        # _open_with_extract_msg; the RTF body is converted here with msgfile's
        # converter instead (much faster, and the result is the same text).
        rtf = _em_call(lambda: msg.rtfBody)
        if rtf:
            cp = _em_prop(msg, "3FFD0003") or msgfile.ansi_codepage_for(_em_prop(msg, "3FDE0003"))
            f["rtf"] = msgfile.rtf_to_html_or_text(rtf, cp)
    return f


def _em_attachment(att, read_message=True, allow_8bit=False, want_data=None):
    """One extract-msg attachment -> the raw attachment dict msgfile produces.

    For an attached email, "message" holds its fields when ``read_message`` is
    True (attachments of the filed email itself), else None. "data" holds the
    bytes of a file attachment that ``want_data(name, size)`` asks for, else None.
    """
    att_type = _em_call(lambda: att.type)
    type_name = str(getattr(att_type, "name", att_type) or "").lower()
    embedded = type_name in ("msg", "attachmenttype.msg")
    name = (_em_call(lambda: att.longFilename, "") or _em_call(lambda: att.shortFilename, "")
            or _em_call(lambda: att.displayName, "") or _em_string(att, "3001"))
    message = None
    if embedded:
        inner = _em_call(lambda: att.data)
        if read_message and inner is not None:
            try:
                message = _em_message_fields(inner, nested=True, allow_8bit=allow_8bit)
            except _EightBitMessage:
                raise   # read the whole file with the built-in reader instead
            except Exception:
                message = None  # an unreadable attached email: keep its name only
        subject = _em_call(lambda: inner.subject, "") if inner is not None else ""
        name = _one_line(subject) or _em_call(lambda: att.displayName, "") or name or "attached message"
    content_id = (_em_call(lambda: att.cid, "") or _em_call(lambda: att.contentId, "")
                  or _em_string(att, "3712"))
    hidden = _em_call(lambda: att.hidden)
    if hidden is None:
        hidden = bool(_em_prop(att, "7FFE000B"))
    size = _em_prop(att, "0E200003")
    if not isinstance(size, int) and not embedded:
        data = _em_call(lambda: att.data)
        size = len(data) if isinstance(data, bytes) else None
    data = None
    if want_data is not None and not embedded:
        raw = _em_call(lambda: att.data)   # extract-msg already holds it in memory
        if isinstance(raw, bytes) and want_data(str(name or "").strip(), len(raw)):
            data = raw
    return {
        "name": str(name or "").strip(),
        "size": size if isinstance(size, int) else None,
        "content_id": str(content_id or "").strip(),
        "hidden": bool(hidden),
        "flags": _em_prop(att, "37140003") or 0,
        "mime": str(_em_call(lambda: att.mimetype, "") or "").strip().lower(),
        "method": _em_prop(att, "37050003") or 0,
        "embedded": embedded,
        "message": message,
        "data": data,
    }


# --------------------------------------------------------------------------
# .eml files
# --------------------------------------------------------------------------

def _eml_header(msg, name):
    """Decoded header value(s) joined by spaces, or ""; never raises."""
    try:
        if name not in msg:  # cheap check: parsing a header is slow
            return ""
        values = msg.get_all(name) or []
        return " ".join(str(v) for v in values)
    except Exception:
        pass
    try:
        return " ".join(msgfile.decode_header_text(str(v))
                        for k, v in msg.raw_items() if k.lower() == name.lower())
    except Exception:
        return ""


def _eml_addresses(msg, name):
    """[(display name, address), ...] from an address header; never raises."""
    out = []
    try:
        if name not in msg:
            return []
        for header in msg.get_all(name) or []:
            addresses = getattr(header, "addresses", None)
            if addresses is None:
                raise ValueError("unparsed")
            for a in addresses:
                out.append((a.display_name or "", a.addr_spec or ""))
        return out
    except Exception:
        pass
    # Split the undecoded value, then decode each name (see _addresses_from_raw).
    try:
        raw = " , ".join(str(v) for k, v in msg.raw_items() if k.lower() == name.lower())
    except Exception:
        return []
    return _addresses_from_raw(raw)


def _part_text(part):
    """Decoded text of a MIME part, tolerating unknown or wrong charsets."""
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
    except Exception:
        pass
    try:
        payload = part.get_payload(decode=True) or b""
    except Exception:
        return ""
    if isinstance(payload, str):
        return payload
    charset = None
    try:
        charset = part.get_content_charset()
    except Exception:
        pass
    for enc in (charset, "utf-8", "cp1252"):
        if not enc:
            continue
        try:
            return payload.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("cp1252", "replace")


def _leaf_parts(part, out, depth=0):
    """(part, content type) for all non-multipart parts, not looking inside attached emails."""
    ctype = part.get_content_type()
    if ctype.startswith("multipart/"):
        if depth > 30:
            return
        try:
            subparts = list(part.iter_parts())
        except Exception:
            subparts = part.get_payload() if isinstance(part.get_payload(), list) else []
        for sub in subparts:
            _leaf_parts(sub, out, depth + 1)
    else:
        out.append((part, ctype))


_CALENDAR_METHOD = re.compile(r"^METHOD:\s*(\w+)", re.M | re.I)
_CALENDAR_PARTSTAT = re.compile(r"PARTSTAT=(\w+)", re.I)


def _calendar_item_class(text):
    m = _CALENDAR_METHOD.search(text or "")
    method = m.group(1).upper() if m else ""
    if method == "REQUEST":
        return "IPM.Schedule.Meeting.Request"
    if method == "CANCEL":
        return "IPM.Schedule.Meeting.Canceled"
    if method == "REPLY":
        p = _CALENDAR_PARTSTAT.search(text)
        status = p.group(1).upper() if p else ""
        suffix = {"ACCEPTED": "Pos", "DECLINED": "Neg", "TENTATIVE": "Tent"}.get(status, "Pos")
        return "IPM.Schedule.Meeting.Resp." + suffix
    return ""


def _received_date(msg):
    """Date from the newest Received: header (used when Date: is missing)."""
    try:
        values = msg.get_all("Received") if "Received" in msg else []
    except Exception:
        return None
    for value in values:
        text = str(value)
        if ";" in text:
            dt = _parse_date_header(text.rsplit(";", 1)[1])
            if dt is not None:
                return dt
    return None


def _read_eml(display, want_docs=False):
    with open(long_path(display), "rb") as fh:
        data = fh.read()
    msg = email.message_from_bytes(data, policy=email.policy.default)
    return _record_from_eml_message(msg, display, READER_EML, want_docs=want_docs)


def _record_from_eml_message(msg, display, reader, depth=0, want_docs=False):
    """Build an EmailRecord from a parsed email.message.Message (.eml files,
    and the MIME inside a signed .msg).

    Attached emails (message/rfc822 parts) are read too when ``depth`` is 0;
    an attached email's own attached emails get their name only. With
    ``want_docs`` (depth 0 only), document attachments keep their decoded bytes
    as raw "data" (see read_email).
    """
    rec = _empty_record(display, reader)

    def get_header(name):
        return _eml_header(msg, name)

    rec["subject"] = _one_line(get_header("Subject"))
    rec["message_id"] = _one_line(get_header("Message-ID"))
    in_reply_to = _one_line(get_header("In-Reply-To"))
    m = re.search(r"<[^<>]+>", in_reply_to)
    rec["in_reply_to"] = m.group(0) if m else in_reply_to
    rec["conversation_topic"] = _one_line(get_header("Thread-Topic"))

    senders = _eml_addresses(msg, "From") or _eml_addresses(msg, "Sender")
    if senders:
        name, addr = senders[0]
        rec["sender_email"] = _smtp(addr)
        rec["sender_name"] = _clean_name(name) or rec["sender_email"] or _bare_name(addr)
    for key, header in (("to", "To"), ("cc", "Cc")):
        seen = set()
        for name, raw_addr in _eml_addresses(msg, header):
            addr = _smtp(raw_addr)
            name = _clean_name(name) or ("" if addr else _bare_name(raw_addr))
            if (name or addr) and (addr or name.lower()) not in seen:
                seen.add(addr or name.lower())
                rec[key].append([name or addr, addr])

    rec["date"] = _iso(_parse_date_header(get_header("Date")) or _received_date(msg))

    leaves = []
    _leaf_parts(msg, leaves)
    any_cid = any("Content-ID" in part for part, _ctype in leaves)

    # Body: the plain-text part, else the HTML part converted to text.
    # (The HTML is also read when images have Content-IDs, to see which are inline.)
    plain_part = html_part = None
    try:
        first = msg.get_body(preferencelist=("plain", "html"))
        if first is not None and first.get_content_type() == "text/html":
            html_part = first
        else:
            plain_part = first
    except Exception:
        pass
    body = _part_text(plain_part) if plain_part is not None else ""
    if html_part is None and plain_part is not None and (any_cid or not body.strip()):
        try:
            html_part = msg.get_body(preferencelist=("html",))
        except Exception:
            html_part = None
    html_text = _part_text(html_part) if html_part is not None else ""
    if not body.strip() and html_text:
        body = html_to_text(html_text)
    if not body.strip() and plain_part is None and html_part is None and len(leaves) == 1 \
            and leaves[0][0] is msg and leaves[0][1].startswith("text/"):
        body = _part_text(msg)
        if leaves[0][1] == "text/html":
            body = html_to_text(body)
    rec["body"] = _normalise_newlines(body) if body.strip() else ""
    html_cids = msgfile.find_cids(html_text)

    # Attachments and message type.
    item_class = ""
    if msg.get_content_type() == "multipart/report":
        report_type = str(msg.get_param("report-type") or "").lower()
        if report_type == "delivery-status":
            item_class = "REPORT.IPM.Note.NDR"
        elif report_type == "disposition-notification":
            item_class = "REPORT.IPM.Note.IPNRN"
    for part, ctype in leaves:
        if part is plain_part or part is html_part:
            continue
        disposition = None
        filename = None
        try:
            disposition = part.get_content_disposition()
            filename = part.get_filename()
        except Exception:
            pass
        if ctype == "text/calendar" and not item_class:
            item_class = _calendar_item_class(_part_text(part))
        if not filename and disposition != "attachment":
            if ctype in ("text/plain", "text/html", "text/calendar", "text/rfc822-headers",
                         "message/delivery-status", "message/disposition-notification"):
                continue  # alternative bodies and report details, not attachments
        size = None
        payload = None
        inner = None
        if ctype == "message/rfc822":
            try:
                inner = part.get_payload(0)
            except Exception:
                inner = None
            if not filename:
                inner_subject = ""
                try:
                    inner_subject = _one_line(inner.get("Subject", ""))
                except Exception:
                    pass
                filename = (inner_subject or "attached message") + ".eml"
        else:
            try:
                payload = part.get_payload(decode=True) or b""
                size = len(payload)
            except Exception:
                size = None
        if not filename:
            ext = mimetypes.guess_extension(ctype) or ".bin"
            filename = "attachment" + ext
        name = _one_line(filename)
        content_id = _one_line(part.get("Content-ID", "")) if "Content-ID" in part else ""
        inline = is_inline_attachment(name, ctype, content_id,
                                      disposition=disposition, html_cids=html_cids)
        attached = None
        if inner is not None and depth == 0 and not inline:
            try:
                attached = _attached_email(_record_from_eml_message(inner, display, reader,
                                                                    depth=1))
            except Exception:
                attached = None  # an unreadable attached email: keep its name only
        entry = {
            "name": name,
            "size": size,
            "inline": inline,
            "email": attached,
        }
        if (want_docs and depth == 0 and not inline and isinstance(payload, bytes)
                and wants_doc_data(name, size)):
            entry["data"] = payload
        rec["attachments"].append(entry)
        payload = None

    rec["item_class"] = item_class or "IPM.Note"
    rec["auto_reply"] = is_auto_reply(rec["item_class"], rec["subject"], get_header)
    return rec
