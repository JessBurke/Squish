"""Text cleaning for Squish: subjects, email bodies, attachments and noise.

Everything here is a plain function that takes text (or an EmailRecord dict)
and returns text, so it can be tested on its own. digest.py puts the pieces
together.

The body cleaning pipeline (see clean_email) is:

1. normalise_text   - newlines, zero-width/non-breaking spaces, smart
                      punctuation to ASCII, emoji removed
2. split_quoted     - new text above the first quoted header; the rest is
                      quoted history (parse_quoted reads the earlier emails)
3. remove banners, Teams/Zoom join blocks and disclaimers
4. cut the signature (sign-off, the sender's name, phone/address/title lines)
5. links: SafeLinks/URLs -> <link> or bare domain, <tel:>/<mailto:> dropped
6. join lines: list items with " \u2022 ", other line breaks with a space
"""

import bisect
import re
import unicodedata
from datetime import datetime
from functools import lru_cache
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urlsplit, parse_qs

# --------------------------------------------------------------------------
# Unicode tidy-up

# (U+20E3 is the keycap frame of an emoji digit such as 1-keycap: the digit is kept)
_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad\u200e\u200f\ufe0f\ufe0e\u20e3"),
                            None)
_SPACES = dict.fromkeys(map(ord, "\u00a0\u202f\u2007\u2009\u200a\u2002\u2003\u2004\u2005\u2006\u3000"), " ")
_PUNCT = {
    ord("\u2018"): "'", ord("\u2019"): "'", ord("\u201a"): "'", ord("\u201b"): "'",
    ord("\u2032"): "'", ord("\u00b4"): "'", ord("`"): "'",
    ord("\u201c"): '"', ord("\u201d"): '"', ord("\u201e"): '"', ord("\u201f"): '"',
    ord("\u2033"): '"', ord("\u00ab"): '"', ord("\u00bb"): '"',
    ord("\u2010"): "-", ord("\u2011"): "-", ord("\u2012"): "-", ord("\u2013"): "-",
    ord("\u2014"): "-", ord("\u2015"): "-", ord("\u2212"): "-", ord("\u2043"): "-",
    ord("\u2026"): "...",
    # (a middle dot is a bullet only at the start of a line - see normalise_text -
    # so 'kN\u00b7m' keeps it)
    ord("\uf0b7"): "\u2022",  # private-use bullet from Word
    ord("\uf0a7"): "\u2022",
    ord("\u25aa"): "\u2022", ord("\u25cf"): "\u2022", ord("\u25e6"): "\u2022",
    ord("\u2023"): "\u2022", ord("\u2043"): "-",
    ord("\u2705"): "\u2713",  # tick/cross emoji -> the plain marks, which are kept
    ord("\u274c"): "\u2717", ord("\u274e"): "\u2717",
    # warning sign and traffic-light colours carry meaning in status tables
    ord("\u26a0"): "(!)",
    ord("\U0001f534"): "[red]", ord("\U0001f7e5"): "[red]",
    ord("\U0001f7e0"): "[amber]", ord("\U0001f7e7"): "[amber]",
    ord("\U0001f7e1"): "[yellow]", ord("\U0001f7e8"): "[yellow]",
    ord("\U0001f7e2"): "[green]", ord("\U0001f7e9"): "[green]",
    ord("\r"): None,
}

_TABLE = {}
_TABLE.update(_ZERO_WIDTH)
_TABLE.update(_SPACES)
_TABLE.update(_PUNCT)

# Emoji and pictographs. Ticks and crosses (U+2713..2718) and the ballot boxes
# (U+2610..2612) are kept because engineers use them as yes/no marks in tables;
# the tick/cross emoji are mapped to them in _PUNCT. Technical symbols in the
# U+2300 block (diameter sign, GD&T marks) are kept; only its emoji are dropped.
# Callout numbers and arrows (U+2776..27BF, U+2B05..2B07) are kept too.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U0001FB00-\U0001FBFF"
    "\u2600-\u260f\u2613-\u26ff\u2700-\u2712\u2719-\u2775"
    "\u2b00-\u2b04\u2b08-\u2bff\u231a\u231b\u2328\u23cf\u23e9-\u23f3\u23f8-\u23fa\U000E0000-\U000E007F]"
)


_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]+")


def _fix_char(m):
    """Map a run of non-ASCII characters through _TABLE and drop emoji."""
    run = m.group(0).translate(_TABLE)
    return _EMOJI_RE.sub("", run)


_LINE_START_DOT = re.compile(r"(?m)^([ \t>]*)\u00b7")


def normalise_text(text):
    """Tidy unicode: \\n line endings, plain spaces, ASCII quotes/dashes, no emoji.

    Accents are composed (NFC), so a name typed on a Mac ('e' + accent) reads the
    same as one typed on Windows. A middle dot starting a line (Outlook's
    Symbol-font bullet) becomes a bullet; elsewhere ('kN\u00b7m') it is kept."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.isascii():
        text = unicodedata.normalize("NFC", text)
        if "\u00b7" in text:
            text = _LINE_START_DOT.sub("\\1\u2022", text)
        text = _NON_ASCII_RE.sub(_fix_char, text)
    # stray control characters (keep \n and \t)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return text


# --------------------------------------------------------------------------
# Subjects

_SUBJ_PREFIX = re.compile(
    r"^\s*(?:"
    r"(?:re|fw|fwd|aw|wg|tr|sv|vs|antw|rif|r)\s*(?:\[\d+\]|\(\d+\))?\s*:"  # reply/forward
    r"|\[\s*external\s*\]|external\s*:|\[\s*ext\s*\]"                      # external tags
    r"|\[\s*pending\s*\]"
    r"|(?:accepted|declined|tentative|tentatively accepted|new time proposed|cancell?ed)\s*:"  # meeting replies
    r")\s*",
    re.I,
)
# Mail Manager filing tags, e.g. [Filed 24 Nov 2025 10:50] or (Filed 24 Nov 2025 10:50),
# and document-management filing tags such as [MCR-W.FID4557780].
_FILED_TAG = re.compile(
    r"\s*(?:\[\s*filed\b[^\]]*\]|\(\s*filed\b[^)]*\)|\[[A-Z0-9-]+(?:\.[A-Z0-9-]+)*\.FID\d+\])",
    re.I,
)


def clean_subject(subject):
    """Subject as shown in the digest: no RE:/FW: prefixes, no filing/[EXTERNAL] tags."""
    s = normalise_text(subject or "").replace("\t", " ").replace("\n", " ")
    s = _FILED_TAG.sub(" ", s)
    previous = None
    while previous != s:
        previous = s
        s = _SUBJ_PREFIX.sub("", s)
        s = s.strip()
    s = re.sub(r"\s+", " ", s).strip(" -:")
    return s or "(no subject)"


def thread_key(subject):
    """Key used to group emails into threads (case-insensitive clean subject)."""
    return clean_subject(subject).casefold()


_TAG_PREFIX = re.compile(r"^\s*(?:\[\s*external\s*\]|external\s*:|\[\s*ext\s*\]|\[\s*pending\s*\])\s*", re.I)
_REPLY_PREFIX = re.compile(
    r"^\s*(?:(?:re|aw|sv|vs|antw|rif|r)\s*(?:\[\d+\]|\(\d+\))?\s*:"
    r"|(?:accepted|declined|tentative|tentatively accepted|new time proposed|cancell?ed)\s*:)", re.I)


def is_reply_subject(subject):
    """True when the subject starts with a reply prefix (RE:, AW:, Accepted: ...).

    Only the outermost prefix counts: 'FW: RE: x' starts a new conversation,
    'RE: FW: x' continues one. Plain subjects and FW: are not replies."""
    s = _FILED_TAG.sub(" ", normalise_text(subject or ""))
    previous = None
    while previous != s:
        previous = s
        s = _TAG_PREFIX.sub("", s)
    return bool(_REPLY_PREFIX.match(s))


# --------------------------------------------------------------------------
# Names and addresses

_EMAIL_RE = re.compile(r"[\w.+'&-]+@[\w-]+(?:\.[\w-]+)+")


@lru_cache(maxsize=20000)
def tidy_display_name(name, email=""):
    """Make a display name presentable: 'Smith, Jane (ABC)' -> 'Jane Smith'.

    Falls back to the email's local part ('jane.smith@x' -> 'Jane Smith').
    """
    n = normalise_text(name or "").replace("\t", " ").strip()
    n = n.strip("\"' ")
    n = re.sub(r"<[^>]*>", "", n)                    # stray <email>
    n = re.sub(r"\s+in Teams$", "", n, flags=re.I)
    n = n.split(" | ")[0].split("|")[0]                 # 'Jane Citizen | Company'
    n = re.sub(r"\s+" + _EMAIL_RE.pattern + r"\s*$", "", n)  # 'Jane Citizen jane@x.com'
    n = re.sub(r"^(?:EXT|EXTERNAL)\b[\s:-]*", "", n, flags=re.I)
    n = re.sub(r"\[[^\]]*\]", "", n)                 # [EXT], [SLR]
    n = re.sub(r"\([^)]*\)", "", n).strip()          # (Head Office), (ABC)
    n = n.strip("\"' ")
    if _EMAIL_RE.fullmatch(n or "-"):
        email = email or n
        n = ""
    if "," in n:
        last, _, first = n.partition(",")
        if first.strip() and len(last.split()) <= 3 and len(first.split()) <= 3:
            n = first.strip() + " " + last.strip()
    # JaneCitizen -> Jane Citizen
    if " " not in n and re.fullmatch(r"[A-Z][a-z]+(?:[A-Z][a-z]+)+", n):
        n = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", n)
    n = re.sub(r"\s+", " ", n).strip(" .,;")
    if not n and email:
        local = email.split("@")[0]
        parts = [p for p in re.split(r"[._\-+]+", local) if p and not p.isdigit()]
        n = " ".join(p.capitalize() for p in parts) or local
    return n


def fold_letters(text):
    """Letters only, lower case, accents removed ('Jos\u00e9' -> 'jose'); other scripts kept."""
    text = unicodedata.normalize("NFKD", text or "").casefold()
    return "".join(ch for ch in text if ch.isalpha())


def name_key(name):
    """Comparable form of a person's name: letters only, lower case, no accents,
    'Last, First' reordered."""
    return fold_letters(tidy_display_name(name))


def parse_address_list(text):
    """Parse 'Name <a@b>; Other <c@d>' (or names only) into [[name, email], ...]."""
    out = []
    if not text:
        return out
    text = normalise_text(text).replace("\n", " ")
    # strip <mailto:...> wrappers produced by HTML-to-text conversion
    text = re.sub(r"<mailto:[^>]*>", "", text, flags=re.I)
    text = re.sub(r"\[mailto:([^\]]*)\]", r"<\1>", text, flags=re.I)
    for part in _split_addresses(text):
        part = part.strip().strip(",")
        if not part:
            continue
        has_at = "@" in part   # (the address searches are slow on long text without an '@')
        m = re.search(r"<\s*([^<>\s]+@[^<>\s]+?)\s*>", part) if has_at else None
        if m:
            email = m.group(1).strip().lower()
            name = part[: m.start()].strip().strip("\"'").strip()
        else:
            m2 = _EMAIL_RE.search(part) if has_at else None
            if m2:
                email = m2.group(0).lower()
                name = (part[: m2.start()] + " " + part[m2.end():]).strip().strip("\"'()[] ")
            else:
                email, name = "", part.strip("\"' ")
        if name or email:
            out.append([name, email])
    # 'Surname, First <x@y>' was split at the comma: glue name-only fragments back
    merged = []
    for name, email in out:
        if merged and not merged[-1][1] and email and name and " " not in merged[-1][0].strip() \
                and " " not in name.strip() and not merged[-1][0].strip().count("@"):
            prev = merged.pop()
            merged.append([prev[0] + ", " + name, email])
        else:
            merged.append([name, email])
    return merged


def _split_addresses(text):
    """Split an address list at ';' (and ',' between addresses), ignoring separators
    inside quotes or <...>."""
    parts, buf, quote, angle = [], [], "", 0
    seen_at = seen_gt = nonblank = False   # what the current item holds so far
    for ch in text:
        if quote:
            if ch == quote:
                quote = ""
        elif ch == '"' or (ch == "'" and not nonblank):
            quote = ch
        elif ch == "<":
            angle += 1
        elif ch == ">":
            angle = max(0, angle - 1)
        elif angle == 0 and (ch == ";" or (ch == "," and (seen_gt or seen_at))):
            parts.append("".join(buf))
            buf = []
            seen_at = seen_gt = nonblank = False
            continue
        buf.append(ch)
        if ch == "@":
            seen_at = True
        elif ch == ">":
            seen_gt = True
        if not ch.isspace():
            nonblank = True
    parts.append("".join(buf))
    return parts


# --------------------------------------------------------------------------
# Dates inside quoted headers ("Sent: Thursday, 23 October 2025 10:08 AM")

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def parse_loose_date(text):
    """Parse the many date styles found in quoted headers. Returns a naive
    datetime (wall-clock time as written) or None."""
    if not text:
        return None
    t = normalise_text(text).strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2})", t)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        return _safe_dt(y, mo, d, h, mi)
    if re.search(r"\d{1,2}:\d{2}:\d{2}\s*[+-]\d{4}", t):
        try:
            dt = parsedate_to_datetime(t)
            return _safe_dt(dt.year, dt.month, dt.day, dt.hour, dt.minute)
        except Exception:
            pass
    tl = t.lower()
    tm = re.search(r"(\d{1,2})[:.](\d{2})(?::\d{2})?\s*([ap]\.?m\.?)?", tl)
    hour = minute = 0
    if tm:
        hour, minute = int(tm.group(1)), int(tm.group(2))
        ampm = (tm.group(3) or "").replace(".", "")
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        tl_date = tl[: tm.start()] + " " + tl[tm.end():]
    else:
        tl_date = tl
    # 23 October 2025 / October 23, 2025 / 23 Oct 2025
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\.?,?\s+(\d{4})", tl_date)
    if m and m.group(2)[:3] in _MONTHS:
        return _safe_dt(int(m.group(3)), _MONTHS[m.group(2)[:3]], int(m.group(1)), hour, minute)
    m = re.search(r"\b([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", tl_date)
    if m and m.group(1)[:3] in _MONTHS:
        return _safe_dt(int(m.group(3)), _MONTHS[m.group(1)[:3]], int(m.group(2)), hour, minute)
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})", tl_date)
    if m:  # Australian day-first
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        return _safe_dt(y, int(m.group(2)), int(m.group(1)), hour, minute)
    return None


def _safe_dt(y, mo, d, h=0, mi=0):
    """datetime(y, mo, d, h, mi), or None when it is not a real date. Placeholder
    dates are not real dates either: system exports write year 1 or 9999, Outlook
    writes 4501 for 'no date' (the same range as readers._parse_date_header)."""
    if y < 1971 or y > 2200:
        return None
    try:
        return datetime(y, mo, d, h, mi)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Quoted history

_LEAD = r"^[ \t>*]*"            # leading quote marks, tabs, bold markers
_FROM_LINE = re.compile(_LEAD + r"(?:from|von|de|van)\s*:\s*\**\s*(.*)$", re.I)
_HEADER_FIELD = re.compile(
    _LEAD + r"(from|sent|date|to|cc|bcc|subject|when|where|importance|attachments|"
    r"reply-to|von|gesendet|an|betreff|de|envoy[e\u00e9]|objet|\u00e0)\s*:\s*\**\s*(.*)$", re.I)
_SEPARATOR = re.compile(
    _LEAD + r"(?:-{2,}\s*(?:original message|original appointment|forwarded message|"
    r"forwarded by|message transf[e\u00e9]r[e\u00e9])\s*-*|begin forwarded message:?|"
    r"_{8,}|-{8,})\s*$", re.I)
# "On Mon, 2 Jun 2025 at 1:17 PM Jane <j@x> wrote:" (may wrap onto a second line).
# The middle starts and ends on a non-space so long runs of spaces cannot make
# the search slow.
_ON_WROTE = re.compile(
    r"^[ \t>]*(?:-{2,}[ \t]*)?On[ \t]+(\S.{2,298}?\S)\s+(?:wrote|writes|a [e\u00e9]crit)[ \t]*:?[ \t]*(?:-{2,})?[ \t]*$",
    re.I | re.M | re.S)
_WROTE_WORD = re.compile(r"wrote|writes|crit", re.I)


def _on_wrote_matches(text):
    """'On ... wrote:' attributions in text (skipped quickly when there are none)."""
    if not _WROTE_WORD.search(text):
        return []
    return list(_ON_WROTE.finditer(text))


_SENT_KEYS = ("sent", "date", "gesendet", "envoye", "envoy\u00e9")
_EXPLICIT_SEP = re.compile(
    _LEAD + r"(?:-{2,}\s*(?:original message|original appointment|forwarded message|forwarded by[^\n]*?)\s*-*"
    r"|begin forwarded message:?)\s*$", re.I)


_CLOCK_TIME = re.compile(r"\b\d{1,2}[:.]\d{2}\b|\b\d{1,2}\s*[ap]\.?m\b", re.I)


def _is_attribution(inner):
    """True if the middle of 'On ... wrote:' looks like a reply header: on one
    paragraph and holding an email address or a clock time. So a sentence like
    'On 3 March the contractor wrote:' stays part of the message."""
    return "\n\n" not in inner and ("@" in inner or bool(_CLOCK_TIME.search(inner)))


def _value_at(lines, j, value):
    """A header value, or the next non-empty line when the label stands alone ('Date:')."""
    if value.strip():
        return value.strip()
    for k in range(j + 1, min(len(lines), j + 4)):
        s = lines[k].strip(" \t>*")
        if s:
            return "" if _HEADER_FIELD.match(lines[k]) else s
    return ""


# A 'From:' value that is only a time, date or number ('7:00am', '12/03/2025', '0900',
# 'Tuesday 18 March'), not a sender
_MONTH_NAME = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
               r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b")
_NOT_A_SENDER = re.compile(
    r"^[\d\s:./,\-]*(?:[ap]\.?m\.?)?[\d\s:./,\-]*$|^(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?\b.*\d"
    r"|^\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH_NAME + r"|^" + _MONTH_NAME + r"\.?\s+\d", re.I)


def _could_be_sender(value):
    """False for a 'From:' value that is a time, date or number ('From: 7:00am');
    a display name with a digit in it ('Level 3 Reception', '3D Visuals') is a sender."""
    return not value or "@" in value or not re.search(r"\d", value) or not _NOT_A_SENDER.match(value.strip())


def _is_header_block(lines, i):
    """True if lines[i] is 'From: ...' followed within a few lines by 'Sent:' or 'Date:'
    holding a clock time (a real quoted header always has one). So a crane booking
    ('From: 7:00am / To: 3:00pm / Date: Tuesday 18 March') or travel details
    ('From: Sydney / To: Wollongong / Date: 12/03/2025') stay part of the message."""
    m = _FROM_LINE.match(lines[i])
    if not m or not _could_be_sender(_value_at(lines, i, m.group(1))):
        return False
    seen = 0
    for j in range(i + 1, min(len(lines), i + 14)):
        s = lines[j].strip(" \t>*")
        if not s:
            continue
        seen += 1
        f = _HEADER_FIELD.match(lines[j])
        if f and f.group(1).lower() in _SENT_KEYS:
            return bool(_CLOCK_TIME.search(_value_at(lines, j, f.group(2))))
        if (not f and len(_words(s)) >= 8) or seen > 8:
            return False   # prose follows: 'From:' was just a word in a sentence
    return False


def _first_field_after(lines, i):
    """Index of the first header field line within 4 non-empty lines after i, else -1."""
    seen = 0
    for j in range(i + 1, min(len(lines), i + 12)):
        if not lines[j].strip(" \t>*"):
            continue
        if _HEADER_FIELD.match(lines[j]):
            return j
        seen += 1
        if seen >= 2:
            break
    return -1


_FROM_ANYWHERE = re.compile(
    r"^[ \t>*]*(?:(?:from|von|de|van)\s*:|begin forwarded message|-{2,}\s*(?:original|forwarded))", re.I | re.M)


def _candidate_lines(text, lines):
    """Indexes of lines that could start a header block (fast pre-filter)."""
    found = []
    if not _FROM_ANYWHERE.search(text):
        return found
    pos = 0
    starts = {}
    for i, ln in enumerate(lines):
        starts[pos] = i
        pos += len(ln) + 1
    for m in _FROM_ANYWHERE.finditer(text):
        i = starts.get(m.start())
        if i is None:
            i = text.count("\n", 0, m.start())
        found.append(i)
    return sorted(set(found))


def _header_starts(text, lines):
    """Quoted-email headers in the text: list of (cut line, first header field line)."""
    heads = []
    for i in _candidate_lines(text, lines):
        if heads and heads[-1][1] >= i:
            continue  # already part of the previous header
        if _EXPLICIT_SEP.match(lines[i]):
            j = _first_field_after(lines, i)
            if j < 0:
                if re.match(_LEAD + r"begin forwarded message", lines[i], re.I):
                    heads.append((i, i + 1))
                continue
            heads.append((i, j))
        elif _is_header_block(lines, i):
            cut = i
            # include a ____ rule or separator line just above
            k = i - 1
            while k >= 0 and not lines[k].strip(" \t>*"):
                k -= 1
            if k >= 0 and _SEPARATOR.match(lines[k]) and i - k <= 3:
                cut = k
            heads.append((cut, i))
    return heads


def _find_quote_starts(text):
    """Return sorted character offsets where quoted emails begin."""
    lines = text.split("\n")
    offsets = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    starts = [offsets[cut] for cut, _ in _header_starts(text, lines)]
    for m in _on_wrote_matches(text):
        if _is_attribution(m.group(1)):
            starts.append(m.start())
    return sorted(set(starts))


def split_quoted(text):
    """Split normalised text into (new_text, quoted_text) at the first quoted email."""
    starts = _find_quote_starts(text)
    if not starts:
        return text, ""
    return text[: starts[0]], text[starts[0]:]


def _parse_header_lines(lines, i):
    """Read header fields starting at lines[i]. Returns (fields dict, next line index)."""
    fields = {}
    current = None
    j = i
    blank_run = 0
    while j < len(lines):
        raw = lines[j]
        s = raw.strip(" \t>*")
        if not s:
            blank_run += 1
            j += 1
            if blank_run > 2 and fields:
                break
            continue
        blank_run = 0
        f = _HEADER_FIELD.match(raw)
        if f:
            key = f.group(1).lower()
            key = {"von": "from", "de": "from", "gesendet": "sent", "an": "to", "betreff": "subject",
                   "envoy\u00e9": "sent", "envoye": "sent", "objet": "subject", "\u00e0": "to"}.get(key, key)
            if key == "date":
                key = "sent"
            if key in fields and key in ("from", "sent"):
                break  # the next header block
            fields[key] = f.group(2).strip()
            current = key
            j += 1
            continue
        if current and not fields.get(current):
            fields[current] = s           # value on the line after the label
            j += 1
            continue
        if current in ("to", "cc") and ("@" in s or ";" in s) and len(s) < 400:
            fields[current] += " " + s    # wrapped recipient list
            j += 1
            continue
        break
    return fields, j


_MAX_ADDRESS_TEXT = 4000   # longest To:/Cc: value read from a quoted header


def parse_quoted(quoted_text, max_emails=3):
    """Read earlier emails out of quoted history.

    Returns a list (newest first) of dicts:
    {"sender_name", "sender_email", "date" (naive datetime or None), "date_text",
     "to": [[name, email]], "cc": [...], "subject", "when", "where", "body" (raw text)}
    ("when"/"where" are a quoted meeting invite's When: and Where: lines, else "".)
    """
    if not quoted_text:
        return []
    lines = quoted_text.split("\n")
    heads = _header_starts(quoted_text, lines)
    # "On <date>, <name> wrote:" headers
    text_offsets = []
    pos = 0
    for ln in lines:
        text_offsets.append(pos)
        pos += len(ln) + 1
    wrote = []
    for m in _on_wrote_matches(quoted_text):
        inner = m.group(1)
        if not _is_attribution(inner):
            continue
        line_no = quoted_text.count("\n", 0, m.start())
        end_line = quoted_text.count("\n", 0, m.end())
        wrote.append((line_no, end_line, inner))
    blocks = []
    for cut, first_field in heads:
        fields, body_start = _parse_header_lines(lines, first_field)
        blocks.append((cut, body_start, fields))
    for line_no, end_line, inner in wrote:
        if any(b[0] <= line_no < b[1] for b in blocks):
            continue
        fields = _parse_on_wrote(inner)
        blocks.append((line_no, end_line + 1, fields))
    blocks.sort(key=lambda b: b[0])
    out = []
    for n, (start, body_start, fields) in enumerate(blocks[:max_emails]):
        end = blocks[n + 1][0] if n + 1 < len(blocks) else len(lines)
        # (a '>' before a number or '=' is a value such as '>95%', not a quote mark)
        body_lines = [re.sub(r"^[ \t]*>+ ?(?![ \t]*[\d=])", "", ln) for ln in lines[body_start:end]]
        body = "\n".join(body_lines)
        # drop a trailing separator that belongs to the next header
        body = re.sub(r"\n[ \t>*]*(?:-{2,}\s*original (?:message|appointment)\s*-*|_{8,}|-{8,})\s*$", "",
                      body.rstrip(), flags=re.I)
        sender = parse_address_list(fields.get("from", "")[:_MAX_ADDRESS_TEXT])
        sname, semail = (sender[0] if sender else ["", ""])
        out.append({
            "sender_name": tidy_display_name(sname, semail) if (sname or semail) else "",
            "sender_email": semail,
            "date": parse_loose_date(fields.get("sent", "")),
            "date_text": fields.get("sent", ""),
            "to": parse_address_list(fields.get("to", "")[:_MAX_ADDRESS_TEXT]),
            "cc": parse_address_list(fields.get("cc", "")[:_MAX_ADDRESS_TEXT]),
            "subject": fields.get("subject", ""),
            "when": fields.get("when", ""),
            "where": fields.get("where", ""),
            "body": body,
        })
    return out


def _parse_on_wrote(inner):
    """Fields from the middle of 'On Mon, 2 Jun 2025 at 1:17 PM, Jane <j@x> wrote:'."""
    inner = re.sub(r"\s+", " ", inner)
    inner = re.sub(r"<mailto:[^>]*>", "", inner, flags=re.I)
    email = ""
    m = _EMAIL_RE.search(inner)
    if m:
        email = m.group(0).lower()
    # name: text between the time/date and the email
    before = inner[: m.start()] if m else inner
    before = before.rstrip(" <(\"'")
    name = ""
    nm = re.search(r"(?:\d{1,2}[:.]\d{2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?|\d{4}|[+-]\d{2}:?\d{2})[,\s]*([^,\d]*)$",
                   before, re.I)
    if nm:
        name = nm.group(1).strip(" ,\"'<")
    return {"from": (name + (" <" + email + ">" if email else "")).strip(), "sent": inner}


# --------------------------------------------------------------------------
# Line classifiers

_BANNER_RE = re.compile(
    r"^(?=.*\b(?:e-?mails?|messages?|sender|organi[sz]ation|click|links?)\b)"   # about email, not the site
    r"\W*(?:caution|warning|external|\[?external(?: email| sender)?\]?|attention)\b.{0,40}"
    r"(?:originat|outside|external|sender|links|attachments)"
    r"|you don'?t often get email from|learn why this is important"
    r"|this (?:e-?mail|message) (?:originated|was sent|is) from (?:outside|an external)"
    r"|^\W*\[?external(?: email)?\]?\W*$"
    r"|some people who received this message don'?t often get email"
    r"|^\W*this is an external email",
    re.I)

# Footer phrases that only ever appear in email disclaimers: one hit drops the line.
_DISCLAIMER_RE = re.compile(
    r"intended (?:only |solely )?for the (?:addressee|named|recipient)"
    r"|if you (?:have )?received this (?:e-?mail|message|communication|transmission) in error"
    r"|(?:e-?mail|message|communication|transmission)(?: and any (?:attachments?|files?)(?: transmitted with it)?)? "
    r"(?:is|are|may be|may contain|contains?) (?:strictly )?(?:confidential|privileged|subject to)"
    r"|subject to (?:a claim of )?legal(?: professional)? privilege"
    r"|(?:free (?:of|from)|scan(?:ned)? for|for the presence of) (?:any )?(?:computer )?virus"
    r"|please (?:consider|think of) the environment"
    r"|traditional (?:owners|custodians)(?: and (?:custodians|owners))? of (?:this |the )?(?:country|lands?|waters?)"
    r"|elders past|acknowledgement of country"
    r"|responsible and ethical use of"
    r"|(?:notify|inform) (?:the sender|the author)(?: immediately)? (?:by|and)"
    r"|delete (?:this|the) (?:e-?mail|message)(?: and any)?(?: copies)?(?: immediately| from your system)"
    r"|^\W*(?:read our )?privacy (?:policy|statement|notice)\W*(?:<link>)?\W*$"
    r"|copyright .{0,40}all rights reserved|this (?:e-?mail|message) has been scanned",
    re.I)
# Phrases that also turn up in normal site or contract talk ("not responsible for any
# damage", "unauthorised access to the site"): they drop a line only when it is also
# about the email itself.
_DISCLAIMER_WEAK_RE = re.compile(
    r"intended (?:only |solely )?for the (?:use|person|individual)|intended recipient"
    r"|accepts? no (?:liability|responsibility)|not (?:be )?(?:responsible|liable) for (?:any )?(?:loss|damage|changes|virus)"
    r"|legally privileged|(?:views|opinions) expressed in this|do(?:es)? not necessarily (?:represent|reflect)"
    r"|(?:contain|check(?:ed)? for) (?:any )?(?:computer )?virus|viruses|before printing"
    r"|traditional (?:custodians|owners)|acknowledge(?:s)? (?:the )?people"
    r"|unauthori[sz]ed (?:use|access|review|disclosure|copying)"
    r"|(?:notify|inform) us(?: immediately)? (?:by|and)"
    r"|is not to be distributed without|privacy (?:policy|statement|notice)[: ]",
    re.I)
_EMAIL_CONTEXT_RE = re.compile(
    r"\b(?:e-?mails?|messages?|transmissions?|communications?|addressee|sender|confidential|privileged)\b", re.I)


def _is_disclaimer_line(s):
    """True for an email footer line (confidentiality notice, Country acknowledgement, etc.)."""
    if _DISCLAIMER_RE.search(s):
        return True
    return bool(_DISCLAIMER_WEAK_RE.search(s) and _EMAIL_CONTEXT_RE.search(s))


# Microsoft 365 sharing notifications and booking-page lines (the shared item's name
# is already in the subject; the typed note between these lines is kept).
_SHARE_BOILERPLATE_RE = re.compile(
    r"this (?:invite|link) (?:will )?only works? for (?:you|the direct recipients)"
    r"|this e-?mail is generated through .{0,80}use of microsoft 365"
    r"|^\W*here'?s the (?:file|folder|document) that .{0,60} shared with you\W*$"
    r"|^\W*(?:(?:open|share|view|download)\s*<link>\s*)+\W*$"
    r"|^\W*book time to meet with me\b",
    re.I)
_SHARE_INVITED_RE = re.compile(r"^\W*(?:[\w'.-]+ ){1,4}invited you to (?:view|edit) (?:a|the) (?:file|folder)\b", re.I)


def _is_share_boilerplate(s):
    """Fixed text of a 'shared with you' notification or a 'Book time to meet with me' line."""
    if len(s) >= 300:
        return False
    return bool(_SHARE_BOILERPLATE_RE.search(s) or (len(s) <= 120 and _SHARE_INVITED_RE.match(s)))

_TEAMS_START = re.compile(
    r"^\W*(?:microsoft teams(?: meeting)?(?: need help\?)?|join (?:zoom meeting|the meeting now|on your computer"
    r"|microsoft teams meeting|teams meeting)|click here to join the meeting|you(?:'ve| have) been invited to a "
    r"(?:zoom|microsoft teams|webex)|join webex meeting|google meet joining info)\b", re.I)
_TEAMS_LINE = re.compile(
    r"^\W*(?:microsoft teams|join\b|click here to join|meeting id|passcode|password|dial[- ]?in|dial by|"
    r"find a local number|find your local number|phone conference id|conference id|for organi[sz]ers|"
    r"meeting options|reset (?:dial-in )?pin|learn more|help\b|privacy and security|one tap mobile|"
    r"or call in|video conference id|alternate vtc|vtc |tenant key|video id|more info|"
    r"meeting link|join by|need help|system reference|\+?\(?\d[\d\s(),#*+.-]{6,}|australia|united states|new zealand|"
    r"sydney|brisbane|melbourne|toll|access code|participant code|webex|zoom|meet\.google|"
    r"no[- ]?reply|\.{3,}|_{3,}|-{3,}|<[^>]+>|\|)", re.I)

_SENT_FROM = re.compile(
    r"^\W*(?:sent from (?:my|mail for|outlook|yahoo|samsung)|get outlook for|sent with|sent via|sent using)\b", re.I)

_PHONE_RE = re.compile(r"(?<![\w/])(?:\+?\d[\d ()-]{6,}\d)(?![\w/])")
_LABEL_WORDS = (r"t|p|m|o|d|f|e|w|a|ph|phone|tel|telephone|mob|mobile|cell|fax|direct|dir|office|email|e-mail|"
                r"web|website|www|main|reception|switch|switchboard|landline|skype|linkedin")
_CONTACT_STRIP = re.compile(
    r"<(?:tel|mailto|https?|file):[^>]*>|\[cid:[^\]]*\]|https?://\S+|www\.\S+|[\w.+'-]+@[\w-]+(?:\.[\w-]+)+"
    r"|\b(?:" + _LABEL_WORDS + r")\b\.?|[:|/,.()\-\s]+|\+?\d[\d ()-]{5,}\d", re.I)
_PRONOUN_RE = re.compile(r"\b(?:she/her|he/him|they/them|she/they|he/they|pronouns?\s*:)", re.I)
_WORKDAYS_RE = re.compile(
    r"\b(?:my (?:usual |normal )?work(?:ing)? days are|i work (?:part[- ]time|monday|tuesday|wednesday|thursday|friday)"
    r"|i(?:'m| am) (?:in the office|working) (?:on )?(?:monday|tuesday|wednesday|thursday|friday)s?\b.{0,40}only"
    r"|please note my work(?:ing)? days|my working hours|not working on (?:mondays|fridays|wednesdays))", re.I)
_CREDENTIALS_RE = re.compile(
    r"\b(?:CPEng|RPEQ|RPEV|NER|MIEAust|FIEAust|BEng|MEng|BE|BSc|MSc|PhD|MBA|GAICD|RPEng|CEnvP|AusIMM|"
    r"MAusIMM|CGeol|DIC|Dip|MEngSc|BE\(Hons\)|BEng\(Hons\))\b")


def _is_credentials_line(s):
    """'CPEng NER RPEQ' style line (two or more post-nominals, not a sentence)."""
    return len(set(_CREDENTIALS_RE.findall(s))) >= 2 and not _is_prose(s) and "?" not in s
_COMPANY_RE = re.compile(r"\b(?:pty\.? ?ltd|pty limited|limited|inc\.|llc|abn\s*\d|acn\s*\d)\b", re.I)
_ADDRESS_RE = re.compile(
    r"\b(?:QLD|NSW|VIC|SA|WA|TAS|ACT|NT|Queensland|New South Wales|Victoria)\b,?\s*(?:Australia,?\s*)?\d{4}\b"
    r"|\b(?:PO Box|Locked Bag|GPO Box)\s*\d+"
    r"|^\W*(?:level|suite|unit|ground floor)\s*\d*,?\s+\d*.{0,60}\b(?:street|st|road|rd|avenue|ave|drive|dr|"
    r"parade|pde|place|pl|way|terrace|tce|highway|hwy|lane|campus|building)\b", re.I)
_SIGNOFF_RE = re.compile(
    r"^\W*(?:(?:thank you|thanks|many thanks)\s+(?:and|&)\s+)?(?:(?:kind|best|warm|warmest|many|with|thanks)\s+)?"
    r"(?:regards|thanks|thank\s*you|thankyou|thank-you|cheers|rgds|kr|ta|thx|tks|sincerely|"
    r"yours (?:sincerely|faithfully|truly)|all the best|best wishes|talk soon|speak soon|"
    r"chat soon|many thanks|thanks heaps|much appreciated|respectfully)\b"
    r"(?:[ ,!.-]+(?:again|in advance|all|mate|team|guys|heaps|so much|very much|everyone|chat soon|"
    r"talk soon|speak soon|and regards|& regards|for (?:your|the) (?:help|time|assistance|support)))*"
    r"[ ,!.-]*(?P<name>.*)$",
    re.I)
# Sentences that live in signatures rather than in messages
_SIG_PROSE_RE = re.compile(
    r"accreditations|newsletter|subscribe|follow us|connect with us|carbon neutral|"
    r"money transfers?|cyber ?fraud|bank (?:account|details)|pay (?:my|our) respects?|"
    r"lands? (?:and|&) (?:seas|waters)|traditional (?:owners|custodians)|elders|"
    r"work(?:ing)? days|part[- ]time|this (?:e-?mail|message) (?:is|was|and)|"
    r"(?:sent|written) (?:from|on) my|consider the environment|award[- ]winning|proud(?:ly)? ",
    re.I)
# Closings that never carry content on their own ("Kind regards", unlike "Thanks")
_CLOSING_ONLY = re.compile(
    r"^\W*(?:(?:kind|best|warm|warmest|with)\s+)?(?:regards|rgds|sincerely|yours (?:sincerely|faithfully|truly)|"
    r"best wishes)\b", re.I)
_GREETING_START = re.compile(
    r"^\W*(?:hi|hello|hey|dear|good (?:morning|afternoon|evening|day)|morning|afternoon|evening|g'?day|hiya|"
    r"greetings)\b", re.I)
_GREETING_WORDS = {"all", "team", "both", "everyone", "guys", "folks", "mate", "there", "and", "&"}
_GREETING_TOKEN = re.compile(r"\s*([A-Za-z][\w'.-]*|&)")
# Capitalised words that start the message rather than name someone
_NOT_NAMES = set(
    "thanks thank please can could would will just i i'm i've i'll we we've we're the this that these as yes no "
    "ok okay see attached here hope following further regarding re fyi sorry apologies good great quick any "
    "is are do did does have has had if when what how why not also our my your it its let looks sounds "
    "perfect noted received confirming confirmed approved all "
    # one-word answers and days, so 'Hi Sam Rejected.' keeps its answer
    "agreed agree disagree accepted rejected declined correct proceed done nope option "
    "monday tuesday wednesday thursday friday saturday sunday today tomorrow".split())


def greeting_length(text):
    """Length of a leading greeting such as 'Hi Sam,' or 'Hello all' (0 if none).

    Only names (capitalised words) and words like 'all'/'team' may follow the
    greeting word, so 'Hi, please call me' has just 'Hi,' as its greeting."""
    m = _GREETING_START.match(text or "")
    if not m:
        return 0
    pos = m.end()
    for _ in range(4):
        t = _GREETING_TOKEN.match(text, pos)
        if not t:
            break
        w = t.group(1)
        low = w.lower().rstrip(".")
        if low in _GREETING_WORDS and low != "all" or low == "all":
            pass
        elif not w[0].isupper() or low in _NOT_NAMES:
            break
        pos = t.end()
        if text[pos:pos + 1] in (",", "!", ":", ";"):
            break
    p = re.match(r"\s*[,!:;.-]?", text[pos:])
    return pos + p.end()


def strip_greeting(text):
    """'Hi Jane, Thanks' -> 'Thanks'."""
    return (text or "")[greeting_length(text or ""):].lstrip()


def _is_greeting_line(s):
    """The whole line is a greeting ('Hi Sam,', 'Morning all')."""
    return bool(s) and greeting_length(s) >= len(s.rstrip())


_TITLES = {"mr", "mrs", "ms", "miss", "dr", "prof"}


def _is_bare_greeting(s):
    """The line is only a greeting ('Hi Sam,', 'Hello all', 'Dear Mr Smith',
    'Hi Sam and Jo'). A greeting with more than one name per person and no closing
    comma ('Hi Sam Rejected.', 'Hi Sam Agreed') may be a one-line reply."""
    s = (s or "").rstrip()
    if not _is_greeting_line(s):
        return False
    if s.endswith((",", ":", ";")):
        return True
    rest = s[_GREETING_START.match(s).end():]
    for part in re.split(r",|&|\band\b", rest):
        names = [w for w in re.findall(r"[A-Za-z][\w'.-]*", part)
                 if w.lower().rstrip(".") not in _GREETING_WORDS | _TITLES]
        if len(names) > 1:
            return False
    return True


def _starts_new_message(s):
    """'Hi Sam,' / 'Hi Sam, From our chat...' - a greeting that opens an embedded message."""
    n = greeting_length(s)
    return n > 0 and (n >= len(s.rstrip()) or s[:n].rstrip().endswith((",", ":", "!")))


_FUNCTION_WORDS = set(
    "the a an and or to of in on for is are was were be been will would can could should we i you it this that "
    "please with as at by from have has had do does did not if our your their they he she there here them us "
    "me my so but which when what how also any all need needs".split())

_BULLET_RE = re.compile(
    r"^[ \t]*(?:[*\u2022\u25cb\u25a1\u25a0\u2219\uf0d8\uf0fc\uf076\u27a2\u2192\u21d2\u00a7]"
    r"|-(?=[ \t])|o(?=\t| {2,}))[ \t]*")
_NUMBERED_RE = re.compile(r"^[ \t]*(?:\(?\d{1,2}[.)]|\(?[a-hj-z][.)]|\(?(?:i|ii|iii|iv|v|vi|vii|viii|ix|x)[.)])[ \t]+\S")


def strip_bullet(line):
    """A list line without its bullet ('\u2022 Noted' -> 'Noted', '- yes' -> 'yes')."""
    return _BULLET_RE.sub("", line or "", 1).strip()


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")


def _words(s):
    return _WORD_RE.findall(s)


def _is_prose(line):
    """A line that reads like a real sentence (not a signature/address fragment)."""
    words = _words(line)
    if len(words) < 6:
        return False
    fw = sum(1 for w in words if w.lower() in _FUNCTION_WORDS and not w[0].isupper())
    if fw < 2:
        return False
    if _is_disclaimer_line(line) or _BANNER_RE.search(line) or _SIG_PROSE_RE.search(line):
        return False
    return True


@lru_cache(maxsize=4096)
def _name_parts(sender_name):
    n = tidy_display_name(sender_name or "")
    parts = tuple(p for p in re.split(r"[\s.]+", n) if p)
    return n, parts


def _is_name_line(s, sender_name):
    """Line is (or starts with) the sender's own name, e.g. 'Jane Citizen | Engineer' or 'Jane'."""
    full, parts = _name_parts(sender_name)
    if not parts:
        return False
    st = s.strip(" ,.-|*\t")
    low = st.lower()
    if len(parts) >= 2 and low.startswith(full.lower()):
        rest = st[len(full):]
        rest_words = _words(rest)
        if len(rest_words) <= 12 and not any(w.lower() in _FUNCTION_WORDS and w.islower() and w not in ("and", "of")
                                             for w in rest_words):
            return True
        return False
    tokens = [t for t in re.split(r"[\s,.]+", st) if t]
    if not tokens or len(tokens) > 3:
        return False
    first = parts[0].lower()
    initials = "".join(p[0] for p in parts).lower()
    allowed = {p.lower() for p in parts} | {initials, initials[:1] + initials[-1:]}
    for t in tokens:
        tl = t.lower()
        if tl in allowed:
            continue
        if len(tl) >= 3 and first.startswith(tl):   # 'Alex' for Alexandra
            continue
        return False
    return True


_LABEL_ONLY = re.compile(r"^\W*(?:" + _LABEL_WORDS + r")\W{0,3}$", re.I)


def _phone_digits(s):
    """Largest run of phone-number-looking digits in s (0 if none)."""
    best = 0
    for m in _PHONE_RE.finditer(s):
        best = max(best, len(re.sub(r"\D", "", m.group(0))))
    return best


def _is_contact_line(s):
    """Phone / email / web lines from a signature block, or a lone label such as 'M' or 'E:'.

    Plain numbers, dates and dollar amounts (table cells) are NOT contact lines.
    """
    s = s.strip()
    if not s or len(s) > 200:
        return False
    if _LABEL_ONLY.match(s) and len(s) <= 10:
        return True
    has_contact = bool(_phone_digits(s) >= 8 or "<tel:" in s or "mailto:" in s.lower() or _EMAIL_RE.search(s)
                       or re.search(r"https?://|www\.", s, re.I))
    if not has_contact:
        return False
    left = _CONTACT_STRIP.sub("", s)
    return not re.search(r"[A-Za-z]", left)


_TITLE_RE = re.compile(
    r"\b(?:manager|director|engineer|consultant|officer|coordinator|co-ordinator|lead|principal|associate|"
    r"partner|founder|ceo|cfo|coo|planner|estimator|supervisor|administrator|assistant|analyst|specialist|"
    r"technician|surveyor|designer|drafter|draftsperson|architect|geologist|scientist|advis[oe]r|executive|"
    r"president|head of|team leader|graduate|student|accountant|controller)\b", re.I)


def _is_title_line(s):
    """Short job-title line such as 'Senior Project Manager'."""
    return bool(_TITLE_RE.search(s)) and len(_words(s)) <= 8 and not _is_prose(s) and not s.endswith("?")


# 'Client: Example Pty Ltd' / 'Site: Unit 4, ... NSW 2526' - a labelled value in the
# message, unlike the 'A: 17 Smith St ...' or 'Address: ...' lines of a signature.
_FIELD_RE = re.compile(r"^([A-Z][\w#&/ -]{0,30}):\s+\S")
_SIG_FIELD_LABELS = set(_LABEL_WORDS.split("|")) | {
    "address", "addr", "postal", "postal address", "street address", "head office", "registered office",
    "location", "abn", "acn"}


def _is_field_line(s):
    """'Label: value' where the label is not a signature label (phone, address ...)."""
    m = _FIELD_RE.match(s)
    return bool(m) and m.group(1).strip().lower() not in _SIG_FIELD_LABELS


def _is_signature_line(s, sender_name):
    """True if a (stripped) line looks like part of a signature block."""
    if not s or len(s) > 200:
        return False
    if _SIGNOFF_RE.match(s) and _signoff_ok(s):
        return True
    if _is_name_line(s, sender_name):
        return True
    if _is_contact_line(s):
        return True
    if _PRONOUN_RE.search(s) or _WORKDAYS_RE.search(s):
        return True
    if _SENT_FROM.match(s):
        return True
    words = _words(s)
    if " | " in s or s.startswith("|") or s.endswith("|"):
        segs = [x for x in s.split("|") if x.strip()]
        if segs and all(len(_words(x)) <= 6 for x in segs):
            return True
    if len(words) <= 14 and (_is_credentials_line(s) or ((_COMPANY_RE.search(s) or _ADDRESS_RE.search(s))
                                                          and not _is_field_line(s))):
        return True
    if re.fullmatch(r"[\W_]{1,5}", s):   # lone '-', '|' etc.
        return True
    return False


def _signoff_ok(s):
    """The part after a sign-off phrase must look like a name, not a sentence."""
    m = _SIGNOFF_RE.match(s)
    if not m:
        return False
    rest = m.group("name").strip(" ,.!-")
    if not rest:
        return True
    toks = rest.split()
    if len(toks) > 3:
        return False
    return all(re.fullmatch(r"[A-Z][\w'-]*\.?|[A-Z]{1,4}", t) for t in toks)


# --------------------------------------------------------------------------
# Links

def _decode_safelink(url):
    if "safelinks.protection.outlook.com" in url.lower():
        try:
            q = parse_qs(urlsplit(url).query)
            if q.get("url"):
                return unquote(q["url"][0])
        except Exception:
            pass
    return url


def _domain(url):
    url = _decode_safelink(url)
    try:
        host = urlsplit(url if "//" in url else "http://" + url).hostname or ""
    except Exception:
        host = ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def replace_links(text):
    """<url> after link text -> ' <link>' (or nothing for mail/phone/file links and
    links to a bare homepage); bare URLs -> their domain."""
    def bracketed(m):
        url = m.group(1).strip()
        low = url.lower()
        if low.startswith(("mailto:", "tel:", "file:", "cid:", "callto:", "sip:")):
            return ""
        try:
            parts = urlsplit(_decode_safelink(url))
            if parts.path in ("", "/") and not parts.query:
                return ""      # a bare homepage (a logo or a company link): no information
        except ValueError:
            pass
        src = text_holder[0]
        before = src[src.rfind("\n", 0, m.start()) + 1: m.start()].strip()
        last = before.rsplit(None, 1)[-1] if before else ""   # (only the last word can match)
        if not before or re.search(r"(?:https?://\S+|www\.\S+|\.[a-z]{2,4}|@[\w.-]+)$", last, re.I):
            return ""  # image link, or link text that is already the address
        return " <link>"

    text_holder = [text]
    # (a match may start only where a run of spaces/tabs starts, so a long run is scanned once)
    text = re.sub(r"(?<![ \t])[ \t]*<((?:https?|mailto|tel|file|cid|callto|sip):[^>\s]*)\s*>", bracketed, text,
                  flags=re.I)
    text = re.sub(r"\[cid:[^\]]*\]", "", text, flags=re.I)
    text = re.sub(r"\[mailto:[^\]]*\]", "", text, flags=re.I)
    text = re.sub(r"\bmailto:", "", text, flags=re.I)

    def bare(m):
        d = _domain(m.group(0))
        return d or "<link>"
    text = re.sub(r"\bhttps?://[^\s<>\"]+", bare, text, flags=re.I)
    text = re.sub(r"\bwww\.([\w-]+(?:\.[\w-]+)+)[^\s<>\"]*", lambda m: m.group(1), text, flags=re.I)
    return text


# --------------------------------------------------------------------------
# The main body cleaner

def _remove_teams_blocks(lines):
    """Drop Teams/Zoom/Webex join instructions (from the header line to the closing rule)."""
    out = []
    i = 0
    n = len(lines)
    while i < n:
        s = lines[i].strip()
        if _TEAMS_START.match(s) and not _is_prose(s):   # not 'Microsoft Teams is down today, ...'
            # drop an opening ____ rule we already kept
            while out and (not out[-1].strip() or re.fullmatch(r"[_\-=*.\s]{5,}", out[-1].strip())):
                out.pop()
            # Teams puts a long ____ rule after the block: skip to it when it is near
            close = -1
            for j in range(i + 1, min(n, i + 60)):
                if re.fullmatch(r"\s*_{10,}\s*", lines[j]):
                    close = j
            if close > 0 and not any(_is_prose(lines[k].strip()) and not _TEAMS_LINE.match(lines[k].strip())
                                     for k in range(i + 1, close)):
                i = close + 1
                continue
            j = i + 1
            while j < n:
                t = lines[j].strip()
                if not t or _TEAMS_LINE.match(t) or _is_contact_line(t) or re.fullmatch(r"[_\-=*.\s]{3,}", t) \
                        or (len(_words(t)) <= 12 and not _is_prose(t)):
                    j += 1
                    continue
                break
            i = j
            continue
        out.append(lines[i])
        i += 1
    return out


def _has_contact(s):
    """A non-sentence line carrying a phone number or email address."""
    if len(s) > 300 or _is_prose(s):
        return False
    return bool(_phone_digits(s) >= 8 or _EMAIL_RE.search(s) or "<tel:" in s)


# A folder path such as \\server\projects\1234 or C:\Jobs\1234 (content, not signature)
_PATH_LINE_RE = re.compile(r"(?:^|[\s(\"'])(?:\\\\[\w.$-]+\\|[A-Za-z]:\\)\S")
# A P.S., NB, 'Note:' or 'Update:' line, which people write after their sign-off
_POSTSCRIPT_RE = re.compile(
    r"^\W{0,3}(?:P\.?\s?S\.?|N\.?B\.?)(?=[\s:.,-])|^\W{0,3}(?:note|update)\b[^:\n]{0,20}?(?::|\s-\s)", re.I)


def _is_postscript(s):
    """'PS: crane booked for 14 March.' / 'Note: the footing is now 900 mm deep.' -
    content even after the sign-off (but not a 'Note: my working days are ...' line)."""
    return bool(_POSTSCRIPT_RE.match(s)) and len(_words(s)) >= 3 and not (
        _WORKDAYS_RE.search(s) or _PRONOUN_RE.search(s) or _SIG_PROSE_RE.search(s) or _COMPANY_RE.search(s)
        or _ADDRESS_RE.search(s) or _is_disclaimer_line(s) or _has_contact(s))


# A table cell holding only a number, amount or quantity ('12', '$320.00', '6.5 hrs')
_TABLE_VALUE_RE = re.compile(r"^[-+]?\$?\s?\d[\d,]*(?:\.\d+)?\s*(?:%|mm|m|hrs?|h|kn|kpa|mpa)?$", re.I)
# A bullet character on a line of its own (the list item text follows on the next line)
_BULLET_GLYPHS = {"*", "\u2022", "o", "\u25cb", "\u25a1", "\u25a0", "\u2219", "\uf0d8", "\uf0fc", "\uf076",
                  "\u27a2", "\u2192", "\u21d2", "\u00a7"}


def _certain_name_block(stripped, i, sender_name, next_nonempty):
    """The sender's name line followed within 3 lines by a title, credentials or contact line."""
    s = stripped[i]
    if not _is_name_line(s, sender_name) or _SIGNOFF_RE.match(s):
        return False
    return any(_is_title_line(stripped[j]) or _is_credentials_line(stripped[j]) or _is_contact_line(stripped[j])
               or _has_contact(stripped[j]) for j in next_nonempty(i + 1, 3))


def _is_table_row(s):
    """A ' | ' table row; returns its number of non-empty cells (0 if not a row)."""
    if " | " not in s:
        return 0
    cells = [c for c in s.split("|") if c.strip()]
    return len(cells) if len(cells) >= 2 else 0


# 'Please see their details below.' / "Kim's contact details are underneath ..."
_CONTACT_INTRO_RE = re.compile(
    r"\b(?:details|contacts|numbers|e-?mail addresses|phone numbers?|mobile numbers?|contact info(?:rmation)?)\b"
    r"[^.?!]{0,40}?\b(?:below|underneath|beneath|as follows)\b", re.I)


def _content_rows(stripped, sig, sender_name, next_nonempty):
    """Indexes of lines that are message content even though they look like
    signature lines:

    - the paragraph after a line ending in ':' ('Quotes received:' then a table,
      'call her directly:' then a number, 'Attendees:' then names), up to a
      sign-off or the sender's own name/title block;
    - everything after a line that passes on contact details ('Please see their
      details below.'), up to the sender's own sign-off or name block;
    - tables: 2+ consecutive ' | ' rows with the same number of cells, holding a
      number or '$' and no phone, email, web or street address.
    """
    n = len(stripped)
    out = set()
    for i in range(n):
        s = stripped[i]
        if not s or sig[i] or _is_greeting_line(s):
            continue
        if not s.endswith(":"):
            if _CONTACT_INTRO_RE.search(s):
                # keep every line up to the sender's own sign-off or name block
                for j in range(i + 1, n):
                    t = stripped[j]
                    if not t:
                        continue      # crosses blank lines: HTML blocks put one between every line
                    if j in out or _starts_new_message(t) or (_SIGNOFF_RE.match(t) and _signoff_ok(t)) \
                            or _certain_name_block(stripped, j, sender_name, next_nonempty):
                        break
                    out.add(j)
            continue
        j = i + 1
        while j < n and not stripped[j]:
            j += 1
        while j < n and stripped[j]:
            if j in out:
                break   # an earlier ':' line already covered the rest of this paragraph
            t = stripped[j]
            if (_SIGNOFF_RE.match(t) and _signoff_ok(t)) or _certain_name_block(stripped, j, sender_name,
                                                                                  next_nonempty):
                break
            out.add(j)
            j += 1
    i = 0
    while i < n:
        cells = _is_table_row(stripped[i])
        if not cells:
            i += 1
            continue
        j = i
        while j < n and _is_table_row(stripped[j]) == cells:
            j += 1
        run = stripped[i:j]
        if j - i >= 2 and any(re.search(r"[\d$]", r) for r in run) and not any(
                _phone_digits(r) >= 8 or _EMAIL_RE.search(r) or re.search(r"https?://|www\.", r, re.I)
                or _ADDRESS_RE.search(r) for r in run):
            out.update(range(i, j))
        i = max(j, i + 1)
    return out


def _signature_only(stripped, i, sig, sender_name, next_nonempty):
    """True when the body from line i on is only the sender's own signature block:
    the name line, a title/contact line straight after it, a phone or email in
    the block, and no sentence-like line anywhere after it."""
    if not _certain_name_block(stripped, i, sender_name, next_nonempty):
        return False
    contact = False
    for j in range(i + 1, len(stripped)):
        t = stripped[j]
        if not t:
            continue
        if _is_contact_line(t) or _has_contact(t):
            contact = True
        elif not sig[j] and (t.endswith((".", "?", "!")) or "?" in t) and len(_words(t)) >= 2:
            return False     # a sentence after the block: a real (short) message
    return contact


def _strip_signature(lines, sender_name):
    """Remove signature blocks.

    Strong evidence (a sign-off followed by the sender's own name, the sender's
    name/title line, or a sign-off followed by a phone/email block) removes
    everything up to the next greeting ("Hi Sam," starting an embedded draft)
    or the end; folder paths and P.S./NB/Note/Update lines written after the
    sign-off are kept. Weaker
    signature-looking lines cut the rest only when nothing that reads like a
    sentence (or a folder path) follows them, and no sign-off is still to come.
    Content that merely looks like a signature (a table, the paragraph after a
    line ending in ':', a list item, a lone table cell such as 'A' or 'JP') is
    never treated as one.
    """
    n = len(lines)
    stripped = [ln.strip() for ln in lines]
    sig = [_is_signature_line(s, sender_name) if s else False for s in stripped]

    def next_nonempty(i, count):
        out = []
        j = i
        while j < n and len(out) < count:
            if stripped[j]:
                out.append(j)
            j += 1
        return out

    def next_greeting(i):
        for j in range(i, n):
            if stripped[j] and _starts_new_message(stripped[j]):
                return j
        return n

    for i in _content_rows(stripped, sig, sender_name, next_nonempty):
        sig[i] = False
    for i in range(n):
        if sig[i] and ((_BULLET_RE.match(lines[i]) and len(stripped[i]) > 2) or stripped[i] in _BULLET_GLYPHS
                       or _is_postscript(stripped[i])):
            sig[i] = False           # a list item (or a P.S.) is never a signature
    # A bare email address with nothing signature-like before it was given on
    # purpose ('Tracey Smith' then 'tracey@example.com', or a body of addresses).
    prev = -1
    for i in range(n):
        if not stripped[i]:
            continue
        if sig[i] and _EMAIL_RE.fullmatch(stripped[i]) and (prev < 0 or not sig[prev]):
            sig[i] = False
        prev = i
    # A lone label ('A', 'M', 'd)') is a signature label only when a contact or
    # signature line follows it; otherwise it is a table cell or list letter.
    # The sender's name or initials followed by a number is a table cell too.
    for i in range(n - 1, -1, -1):
        s = stripped[i]
        if not sig[i]:
            continue
        nxt = next_nonempty(i + 1, 1)
        if _LABEL_ONLY.match(s):
            if not nxt or not (sig[nxt[0]] or _has_contact(stripped[nxt[0]])):
                sig[i] = False
        elif _is_name_line(s, sender_name) and not _SIGNOFF_RE.match(s):
            if nxt and _TABLE_VALUE_RE.match(stripped[nxt[0]]) and not _has_contact(stripped[nxt[0]]):
                sig[i] = False
    # prose_after[i]: a sentence-like line (or folder path) at or after i.
    # content_then_signoff[i]: a content line at or after i that is followed later in
    # the same message (before the next greeting) by a sign-off or signature start.
    prose_after = [False] * (n + 1)
    content_then_signoff = [False] * (n + 1)
    signoff_later = False
    for i in range(n - 1, -1, -1):
        s = stripped[i]
        prose_after[i] = prose_after[i + 1] or (bool(s) and not sig[i] and (
            _is_prose(s) or bool(_PATH_LINE_RE.search(s)) or _is_postscript(s)))
        if s and _starts_new_message(s):
            signoff_later = False
            continue
        content_then_signoff[i] = content_then_signoff[i + 1] or (bool(s) and not sig[i] and signoff_later)
        if sig[i] and not signoff_later and ((_SIGNOFF_RE.match(s) and _signoff_ok(s))
                                             or _strong_signature(stripped, i, sender_name, next_nonempty)):
            signoff_later = True

    def _contact_soon(i):
        # a phone/email line just after i: a signature block (perhaps a colleague's)
        return any(_is_contact_line(stripped[j]) or _has_contact(stripped[j]) for j in next_nonempty(i + 1, 3))

    out = []
    i = 0
    # A signature pasted above the message (name line first, greeting later)
    first = next_nonempty(0, 1)
    if first and _is_name_line(stripped[first[0]], sender_name) and not _SIGNOFF_RE.match(stripped[first[0]]):
        g = next_greeting(first[0])
        if g < min(n, first[0] + 40) and all(
                not stripped[k] or sig[k] or _is_name_line(stripped[k], sender_name) or _is_title_line(stripped[k])
                or _is_contact_line(stripped[k]) or _has_contact(stripped[k]) for k in range(first[0], g)):
            i = g     # (a heading such as 'Jane Citizen - leave dates' and its dates is kept)
    content_seen = False
    while i < n:
        raw, s = lines[i], stripped[i]
        if not s:
            out.append(raw)
            i += 1
            continue
        if sig[i] and not content_seen and not prose_after[i + 1] and _signature_only(
                stripped, i, sig, sender_name, next_nonempty):
            break    # nothing but the sender's signature (e.g. a forward with no comment)
        if sig[i] and (content_seen or _CLOSING_ONLY.match(s)):
            if _strong_signature(stripped, i, sender_name, next_nonempty):
                end = next_greeting(i + 1)
                # keep folder paths and P.S./Note lines the sender put after the sign-off
                out.extend(lines[k] for k in range(i + 1, end)
                           if _PATH_LINE_RE.search(stripped[k]) or _is_postscript(stripped[k]))
                if end >= n:
                    break
                i = end
                continue
            if not prose_after[i + 1] and not (content_then_signoff[i + 1] and not _SIGNOFF_RE.match(s)
                                                and not _contact_soon(i)):
                break
        if sig[i] and (_is_contact_line(s) or _SENT_FROM.match(s) or _PRONOUN_RE.search(s)
                       or _WORKDAYS_RE.search(s)):
            i += 1
            continue  # junk even mid-email
        out.append(raw)
        if not _is_greeting_line(s):
            content_seen = True
        i += 1
    return out


def _strong_signature(stripped, i, sender_name, next_nonempty):
    """True when the signature-looking line at i certainly starts a signature block."""
    s = stripped[i]
    if _is_name_line(s, sender_name) and not _SIGNOFF_RE.match(s):
        # the sender's name followed by a title, contact or company line
        following = next_nonempty(i + 1, 3)
        return any(_is_title_line(stripped[j]) or _is_contact_line(stripped[j]) or _has_contact(stripped[j])
                   or _is_signature_line(stripped[j], "") for j in following)
    if _is_credentials_line(s) and len(_words(s)) <= 14:
        return True
    m = _SIGNOFF_RE.match(s)
    if m and _signoff_ok(s):
        rest = m.group("name").strip(" ,.!-")
        if rest and _is_name_line(rest, sender_name):
            return True          # "Cheers, Alex" from Alex
        following = next_nonempty(i + 1, 8)
        if following and _is_name_line(stripped[following[0]], sender_name):
            return True          # "Kind regards," then "Alex"
        if any(_is_contact_line(stripped[j]) or _has_contact(stripped[j]) for j in following[:6]):
            return True          # sign-off, then a phone/email block
    return False


def _is_listy(s):
    """Short line without sentence punctuation: likely a list item or table cell."""
    if len(s) > 90 or s.endswith((".", "!", "?", ",", ";", ":")) or _is_greeting_line(s):
        return False
    return 0 < len(s.split()) <= 10


# Words a hard-wrapped sentence often breaks after ('... note that' / 'the headwall ...')
_WRAP_WORDS = frozenset(
    "the a an and or of to in on for with that is are be by from at as this we will not".split())


def _looks_wrapped(prev, nxt):
    """True when two lines read as one sentence broken by a hard wrap: the next line
    starts with a lower-case word (not a list letter such as 'b)'), or the line ends
    on a word such as 'the' or 'of'."""
    if re.match(r"[a-z]+\b(?![.)])", nxt):
        return True
    words = prev.split()
    return bool(words) and words[-1].lower() in _WRAP_WORDS


_TABLE_CELL_MAX = 200    # longest paragraph still read as a cell after an Outlook table's header run


def _table_cells_end(rows, gaps, j):
    """After a header run of short cells, each its own paragraph (how Outlook's plain
    text writes a table), mark the following paragraphs as cells too, even when a cell
    is a sentence, up to a long paragraph, or a line that carries on the sentence of
    the line before it (hard-wrapped text pasted with a blank line after every line).
    Returns the index after the last cell."""
    while (j < len(rows) and not rows[j][1] and gaps[j] and len(rows[j][0]) <= _TABLE_CELL_MAX
           and not _is_greeting_line(rows[j][0]) and not _wrap_continues(rows[j - 1][0], rows[j][0])):
        rows[j][1] = True
        j += 1
    return j


def _wrap_continues(prev, line):
    """`line` carries on the sentence of a long `prev` line that was hard-wrapped
    ('... which has given him a good working' / 'knowledge of the ...')."""
    return len(prev) >= 50 and not prev.endswith((".", "!", "?", ":", ";")) and _looks_wrapped(prev, line)


def _join_lines(lines):
    """Join into one line: list items with ' \u2022 ', other line breaks with a space.

    Bulleted and numbered lines are list items. So are runs of short unpunctuated
    lines (3 or more, or 2 after a line ending in ':'), which is how lists and
    table cells come out of HTML emails - unless most of the lines in the run
    read as one hard-wrapped sentence (plain-text emails wrapped at 60-70 columns).
    In an Outlook table (a run of 3+ short cells, each its own paragraph) the
    paragraphs that follow, up to _TABLE_CELL_MAX characters each, are cells too,
    so each row's comment does not run into the next row's task.
    """
    rows = []
    gaps = []          # gaps[k]: a blank line came before rows[k]
    gap = True
    glyph = False      # the previous line was a bullet character on its own
    for raw in lines:
        s = re.sub(r"\t+", " ", raw.strip())
        if not s:
            gap = True
            continue
        if s in _BULLET_GLYPHS:
            glyph = True
            continue
        bullet = glyph
        glyph = False
        b = _BULLET_RE.match(raw)
        if b and len(s) > 1:
            s = raw[b.end():].strip()
            bullet = True
        elif _NUMBERED_RE.match(raw):
            bullet = True
        if s:
            rows.append([s, bullet])
            gaps.append(gap)
        gap = False
    # runs of short lines become list items
    i = 0
    while i < len(rows):
        if rows[i][1] or not _is_listy(rows[i][0]):
            i += 1
            continue
        j = i
        while j < len(rows) and not rows[j][1] and _is_listy(rows[j][0]):
            j += 1
        after_colon = i > 0 and rows[i - 1][0].endswith(":")
        wrapped = sum(1 for k in range(i, j - 1) if _looks_wrapped(rows[k][0], rows[k + 1][0]))
        if (j - i >= 3 or (j - i >= 2 and after_colon)) and not (j - i >= 3 and wrapped * 2 >= j - i - 1):
            for k in range(i, j):
                rows[k][1] = True
            if j - i >= 3 and all(gaps[i + 1:j]):
                j = _table_cells_end(rows, gaps, j)
        i = max(j, i + 1)
    parts = []
    for s, bullet in rows:
        if bullet:
            parts.append((" \u2022 " if parts else "\u2022 ") + s)
        else:
            parts.append((" " if parts else "") + s)
    text = "".join(parts)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"(?:\s*\u2022\s*){2,}", " \u2022 ", text)
    return text.strip()


def _only_greeting(lines, sender_name):
    """The lines are a greeting line followed only by the sender's own name or
    initials, or a closing such as 'Regards' ('Hi Sam' / 'Jane')."""
    rows = [ln.strip() for ln in lines if ln.strip()]
    if not rows or not _is_bare_greeting(rows[0]):
        return False
    return all(_is_name_line(r, sender_name) or _CLOSING_ONLY.match(r) for r in rows[1:])


def _end_greeting_line(lines):
    """End a greeting that has a line to itself with a comma ('Hi Sam' -> 'Hi Sam,'),
    so that once lines are joined the greeting stops at the end of its own line:
    'Hi Sam, Agreed.' rather than one long greeting 'Hi Sam Agreed.'."""
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        if _is_greeting_line(s) and not s.endswith((",", "!", ":", ";", ".", "-")) \
                and any(x.strip() for x in lines[i + 1:]):
            lines = list(lines)
            lines[i] = ln.rstrip() + ","
        break
    return lines


def _is_quote_line(s):
    """A line marked as quoted with '>' ('> I think we should', '>> earlier text').
    A '>' before a number, '=' or '<' is a value ('>95% compaction', '> 600 mm',
    '>=5'), not a quote mark."""
    if not s.startswith(">"):
        return False
    rest = s.lstrip("> \t")
    return not rest or rest[0] not in "0123456789=.<"


def clean_text(text, sender_name=""):
    """Clean the *new* part of an email body into a single line of text."""
    if not text:
        return ""
    text = replace_links(text)
    lines = text.split("\n")
    # drop banners, disclaimers and lines that are only rules
    kept = []
    for ln in lines:
        s = ln.strip()
        if len(s) > 6 and (_BANNER_RE.search(s) or (len(s) < 2000 and _is_disclaimer_line(s))):
            continue
        if s and _is_share_boilerplate(s):
            continue
        if s and re.fullmatch(r"[_\-=*~.\s]{3,}", s):
            kept.append("")
            continue
        if s and ((_SENT_FROM.match(s) and len(s.split()) <= 8) or _is_quote_line(s)):
            continue  # phone footers and '>' quoted lines (not '>95%' or '> 600 mm')
        kept.append(ln)
    kept = _remove_teams_blocks(kept)
    kept = _strip_signature(kept, sender_name)
    if _only_greeting(kept, sender_name):
        return ""     # 'Hi Sam' then just the sender's name ('Jane') or 'Regards'
    kept = _end_greeting_line(kept)
    out = _join_lines(kept)
    out = re.sub(r"\s+([,.;:!?])(?=\s|$)", r"\1", out)
    out = re.sub(r"\(\s*\)|<\s*>", "", out)
    out = re.sub(r"\s{2,}", " ", out).strip(" |")
    if _is_bare_greeting(out):
        return ""     # only 'Hi Sam,' was left (the rest was a signature)
    return out


def clean_email(body, sender_name="", max_quoted=3):
    """Clean a raw body. Returns {"text": new text (one line), "quoted": [earlier emails],
    "new_raw": the uncleaned new part}."""
    text = normalise_text(body or "")
    new, quoted = split_quoted(text)
    return {
        "text": clean_text(new, sender_name),
        "quoted": parse_quoted(quoted, max_quoted) if quoted else [],
        "new_raw": new,
    }


# --------------------------------------------------------------------------
# Acknowledgements

_ACK_WORDS = set(
    "thanks thank thankyou thx ta cheers noted received receipt acknowledged ack appreciated appreciate "
    "appreciation much many very heaps so you all mate mates team guys everyone again great perfect awesome "
    "amazing excellent brilliant lovely wonderful fantastic legend fab cool nice good beauty "
    "ok okay k sounds looks got it that this the info information email update updates for your "
    "and too easy no worries problem probs a lot of kind regards best cheers champion "
    "much appreciated have weekend day afternoon morning evening hi hello hey dear".split())


# Words that turn a thank-you into an answer or a decision ("Approved, thanks",
# "Yes, thanks", "Looks good thanks"): such replies are kept as text.
_ACK_DECISION = re.compile(
    r"\b(?:yes|yep|yeah|nope|no(?!\s+(?:worries|worry|problems?|probs|dramas?|stress)\b)"
    r"|ok(?!\s*[.,]?\s*noted)|okay|approv\w*|agree\w*|confirm\w*|accept\w*|declin\w*|reject\w*"
    r"|proceed\w*|go ahead|cancel\w*|correct|done|will do|all good|happy|fine"
    r"|(?:looks?|sounds?)\s+(?:good|great|fine|ok|okay|right))\b", re.I)


def is_ack_reply(text):
    """True when an email's cleaned text is a pure acknowledgement once its greeting
    is set aside ('Hi Sam, Thanks'); never when it carries a decision word anywhere,
    greeting included ('Hi Sam Agreed, thanks')."""
    return is_ack(strip_greeting(text)) and not _ACK_DECISION.search(text or "")


def is_ack(text):
    """True for a pure acknowledgement such as 'Thanks JP', 'Noted.', 'Received, thank you'.
    Not for answers or decisions such as 'Yes, thanks' or 'Approved, thanks'."""
    t = (text or "").strip()
    if not t or len(t) > 90 or "?" in t or re.search(r"\d", t):
        return False
    if _ACK_DECISION.search(t):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z']*", t)
    if not words or len(words) > 12:
        return False
    if not any(w.lower() in ("thanks", "thank", "thankyou", "thx", "ta", "cheers", "noted", "received",
                             "appreciated", "acknowledged", "perfect", "great", "awesome", "excellent",
                             "brilliant", "legend", "ok", "okay") for w in words):
        return False
    names = 0
    for w in words:
        if w.lower() in _ACK_WORDS:
            continue
        if w[0].isupper():
            names += 1
            continue
        return False
    return names <= 2


# --------------------------------------------------------------------------
# Capping long text

_ABBREV = re.compile(r"(?:\b(?:e\.g|i\.e|etc|approx|incl|no|dr|mr|mrs|ms|st|vs|ref|fig|cl|cls|min|max|est|"
                     r"rev|dwg|dia|nom|ea|vol|pp|p|sect|sec|attn|qty|n\.b|a\.m|p\.m)|\b[A-Z])$", re.I)


# When a long email is trimmed, the opening is kept and then the later
# sentences (or table rows) that carry facts, so figures, dates, references and
# questions near the end are not lost. Gaps are marked with " … ".
CAP_HEAD_SHARE = 0.4     # share of the limit always given to the opening
_MONTH_WORD = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
# Hard facts: a sentence or table row that has one of these is worth keeping
_FACTS = [
    (re.compile(r"\$\s?\d"), 3),                                                         # dollar amounts
    (re.compile(r"\b(?:RFI|NCR|TQ|EOT|VO|SI|CN)[- ]?#?\s?\d{1,5}\b", re.I), 3),            # RFI 12, NCR-007
    (re.compile(r"\b[A-Z]{1,5}-\d{2,5}\b|\b\d{3,}\.\d{4,}\b"
                r"|\b(?:rev(?:ision)?|dwg|drawing|sheet|detail)\.?\s?#?\s?[A-Z]?\d", re.I), 2),  # drawing/doc refs
    (re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH_WORD + r"\b"
                r"|\b" + _MONTH_WORD + r"\s+\d{1,2}(?:st|nd|rd|th)?\b"
                r"|\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b", re.I), 2),            # dates and days
    (re.compile(r"\b\d[\d,.]*\s?(?:mm|cm|m|km|m2|m3|kn|kpa|mpa|kg|t|tonnes?|l/s|lps|%|hrs?|hours?|days?|"
                r"weeks?|months?|years?|ha|deg)(?![\w])", re.I), 1),                       # numbers with units
]
_SOFT_FACTS = [
    (re.compile(r"\?"), 2),                                                               # questions
    (re.compile(r"\b(?:please|can you|could you|would you|will you|we will|we'll|i will|i'll|needs? to|must|"
                r"required?|confirm\w*|approv\w*|deadline|due|tomorrow|next week|this week|cob|eod)\b", re.I), 1),
    # decisions and hold points: stop / hold / reject instructions
    (re.compile(r"\b(?:rejected|unapproved|not (?:be )?(?:accepted|approved|acceptable)|on hold|hold (?:off|point)"
                r"|no further|nothing further|(?:shall|must) not|(?:is|are) not to|not to (?:proceed|be)"
                r"|should not proceed|stop work)\b", re.I), 2),
    # outcomes
    (re.compile(r"\b(?:accepted|until|closed|cancell?ed|withdrawn|variations?)\b", re.I), 1),
]
_TOTAL_WORD = re.compile(r"\btotal", re.I)
# A sentence whose subject is in the sentence before it ('This brings the total to ...',
# 'These come to $4,500', 'Which means ...')
_REFERS_BACK = re.compile(
    r"(?:this|these|that|those)\s+(?:is|are|was|were|would|will|means?|equals?|comes?|brings?|takes?|gives?"
    r"|includes?|represents?|totals?|adds? up)\b|(?:which|they|he|she)\b", re.I)
_LOW_INFO = re.compile(r"let me know if|give me a call|(?:any|further) (?:questions|queries)|happy to (?:discuss|help)"
                       r"|hope (?:you|this|all|that)|feel free|thanks|thank you|regards", re.I)
_SENT_END = re.compile(r"[.!?][\"')\]]*(?=\s)")
_BULLET_GAP = re.compile(r"\s•\s")
_LIST_NUMBER = re.compile(r"•\s?\(?(?:\d{1,2}|[a-z]|[ivx]{1,4})$", re.I)


def _is_list_number(text, pos):
    """The '.' at pos ends a list number such as '• 5.' or '1.' at the start."""
    if _LIST_NUMBER.search(text[max(0, pos - 8):pos]):
        return True
    return pos <= 4 and re.fullmatch(r"\s*\(?(?:\d{1,2}|[a-z]|[ivx]{1,4})", text[:pos], re.I) is not None


def _fact_score(piece):
    """How much a sentence or table row is worth keeping (0 = nothing in particular)."""
    hard = sum(w * min(2, len(rx.findall(piece))) for rx, w in _FACTS)
    soft = sum(w * min(2, len(rx.findall(piece))) for rx, w in _SOFT_FACTS)
    if not hard and _LOW_INFO.search(piece):
        return 0     # 'Let me know if you have any questions.'
    if hard and "$" in piece and _TOTAL_WORD.search(piece):
        hard += 3    # a total is worth more than one row of a fee table
    return hard + soft


def _cap_units(text, item_max=0):
    """Split text into (start, end) units: sentences, or table/list runs that end at
    the last of one or more cells holding a fact, so a table row keeps its label
    ('\u2022 Total \u2022 13.1 \u2022 $3,567.50', '\u2022 Stage 2 \u2022 $9,000 \u2022 $6,960').
    The sentences of one list item of at most `item_max` characters stay one unit,
    so an outcome stays with its number ('\u2022 RFI-12, slab edge. Accepted.')."""
    cuts = set()
    for m in _SENT_END.finditer(text):
        if m.group(0).startswith(".") and (_ABBREV.search(text[max(0, m.start() - 6): m.start()])
                                           or _is_list_number(text, m.start())):
            continue
        cuts.add(m.end())
    for m in _BULLET_GAP.finditer(text):
        cuts.add(m.start())
    cuts.add(len(text))
    pieces = []
    cur = 0
    for c in sorted(cuts):
        if c > cur:
            if text[cur:c].strip():
                pieces.append((cur, c))
            cur = c
    has_fact = [any(rx.search(text[a:b]) for rx, _ in _FACTS) for a, b in pieces]
    units = []
    start = pieces[0][0] if pieces else 0
    for n, (a, b) in enumerate(pieces):
        sentence = bool(_SENT_END.match(text[a:b].rstrip()[-1:] + " "))
        row_end = has_fact[n] and not (n + 1 < len(pieces) and has_fact[n + 1])
        if sentence or row_end or b - start >= 200 or n == len(pieces) - 1:
            units.append((start, b))
            start = b
    return _join_list_items(text, units, item_max)


def _join_list_items(text, units, item_max):
    """Merge the units that make up one short list item (at most `item_max`
    characters, from its bullet to the next) into one unit. Table rows, where
    each cell starts at a bullet, and long items are left as they are."""
    if not item_max:
        return units
    starts = [m.start() for m in _BULLET_GAP.finditer(text)]
    if not starts:
        return units
    first = set(starts)
    out = []
    prev_item = None
    for a, b in units:
        n = bisect.bisect_right(starts, b - 1) - 1      # the item this unit ends in
        if out and n >= 0 and a not in first and starts[n] < a and prev_item == n:
            end = starts[n + 1] if n + 1 < len(starts) else len(text)
            if end - starts[n] <= item_max:
                out[-1] = (out[-1][0], b)
                continue
        out.append((a, b))
        prev_item = n
    return out


# Where a long table or list run with facts may be split: a list item, or an amount
# followed by a capitalised word (a line break the cleaning joined: '= $64,250.00 This ...';
# not a date such as '14 March')
_FIGURE_BREAK = re.compile(r"(?:(?<=[.,]\d\d)|(?<=,\d\d\d))\s(?=[A-Z][a-z])")
# Clause breaks inside a long sentence (a comma needs a space after it, so '$48,000' stays whole)
_CLAUSE_BREAK = re.compile(
    r"[;:,](?=\s)|\s[-\u2013\u2014]\s"
    r"|\s(?=(?:and|but|which|so|provided|while|whereas|however|although|because)\b)")


def _split_at_items(text, a, b, size):
    """(start, end) chunks of text[a:b], each at most `size` where the list items allow,
    cut at list items (or after a figure where a line break was joined)."""
    cuts = sorted(set([m.start() for m in _BULLET_GAP.finditer(text, a + 1, b)]
                      + [m.start() for m in _FIGURE_BREAK.finditer(text, a + 1, b)]))
    chunks = []
    start = last = a
    for c in cuts + [b]:
        if c - start > size and last > start:
            chunks.append((start, last))
            start = last
        last = c
    chunks.append((start, b))
    return chunks


def _split_at_clauses(text, a, b, size):
    """(start, end) pieces of text[a:b], each at most `size`: cut at the first clause
    break once a piece is about a third of `size` long, so a fact keeps its own short
    clause; with no clause break in reach, at the last space."""
    breaks = [m.end() for m in _CLAUSE_BREAK.finditer(text, a, b)]
    out = []
    start = a
    while b - start > size:
        cut = next((x for x in breaks if start + size * 0.3 <= x <= start + size), -1)
        if cut < 0:
            sp = text.rfind(" ", start, start + size + 1) - start
            cut = start + (sp if sp > size * 0.35 else size)
        out.append((start, cut))
        start = cut
    if text[start:b].strip():
        out.append((start, b))
    return out


def _fine_units(text, units, size):
    """Split every unit after the opening that is longer than `size` and holds facts:
    first at its list items, then (for a piece still too long, such as one long
    run-on sentence) at clause breaks, so its figures and dates can be kept without
    taking the whole budget. Plain prose without facts is left whole.
    Returns (pieces, whole) where whole[i] is the number of the unit piece i came from."""
    out = [units[0]]
    whole = [0]
    for n, (a, b) in enumerate(units[1:], 1):
        if b - a <= size or not _fact_score(text[a:b]):
            pieces = [(a, b)]
        else:
            pieces = []
            for x, y in _split_at_items(text, a, b, size):
                if y - x > size and _fact_score(text[x:y]):
                    pieces.extend(_split_at_clauses(text, x, y, size))
                else:
                    pieces.append((x, y))
        out.extend(pieces)
        whole.extend([n] * len(pieces))
    return out, whole


def _render_units(text, units, keep):
    """Kept units in order; runs of neighbours keep their own spacing, gaps become ' … '."""
    runs = []
    for k in sorted(keep):
        a, b = units[k]
        if runs and runs[-1][1] == k - 1:
            runs[-1] = (runs[-1][0], k)
        else:
            runs.append((k, k))
    pieces = [text[units[a][0]:units[b][1]].strip() for a, b in runs]
    out = " … ".join(p.rstrip(" ,;:-") if i < len(pieces) - 1 else p for i, p in enumerate(pieces))
    if runs and runs[-1][1] < len(units) - 1:
        out = _drop_list_marker(out.rstrip(" ,;:-")) + " …"
    return out


def _drop_list_marker(s):
    """'... • 5.' -> '...': a trimmed text never ends on an empty list item."""
    return re.sub(r"\s*•\s*(?:\(?(?:\d{1,2}|[a-z]|[ivx]{1,4})[.)])?$", "", s, flags=re.I).rstrip(" ,;:-")


def _squash(s):
    """Text with runs of spaces made one, for comparing sentences."""
    return " ".join(s.split()).strip(" \u2022\u2026")


def cap_text(text, limit, seen=""):
    """Shorten text to about `limit` characters.

    The opening (at least CAP_HEAD_SHARE of the limit, plus the rest of a list's
    intro line) is kept, then the later sentences or table rows with the most
    facts (dollar amounts, RFI and drawing numbers, dates, quantities, questions,
    requests, and decisions and hold points: rejected, not to proceed, no further,
    on hold, shall/must not; weaker: accepted, closed, until, variation), then
    whatever follows the opening while it fits. A short list item stays whole, so
    an outcome stays with its number. A long table, list or run-on sentence that
    holds facts is first split at its list items (then at clause breaks) so its
    fact rows can be kept. A sentence or row of 30 or more characters that is
    already in `seen` (text the same sender already showed above) is dropped
    first, unless it is in the opening. Dropped stretches are marked ' … ', and a
    trimmed ending ' …'. Plain prose comes out as before: cut at a sentence."""
    if not limit or len(text) <= limit:
        return text
    head = limit * CAP_HEAD_SHARE
    units = _cap_units(text, min(120, int(max(head, 120))))
    if len(units) < 2 or units[0][1] - units[0][0] > limit:
        return _cut_at(text, limit)
    units, whole = _fine_units(text, units, int(max(head, 120)))
    keep = {0}
    k = 1
    while k < len(units) and units[k][1] - units[0][0] <= head:
        keep.add(k)
        k += 1
    if k < len(units):
        # a list straddling the opening: keep its intro line and the items that fit
        a, b = units[k]
        cut = -1
        for m in _BULLET_GAP.finditer(text, a + 1, b):
            if m.start() - units[0][0] <= head:
                cut = m.start()
        if cut > a:
            units = units[:k] + [(a, cut), (cut, b)] + units[k + 1:]
            whole = whole[:k] + [whole[k], whole[k]] + whole[k + 1:]
            keep.add(k)
            k += 1

    def fits(cand):
        return len(_render_units(text, units, cand)) <= limit + 2

    scores = [_fact_score(text[a:b]) for a, b in units]
    # sentences the same sender already showed word for word above (not in the opening)
    seen = _squash(seen or "")
    shown = [False] * len(units)
    if seen:
        for n in range(k, len(units)):
            u = _squash(text[units[n][0]:units[n][1]])
            shown[n] = len(u) >= 30 and u in seen
    ranked = sorted(range(k, len(units)), key=lambda j: (-scores[j], j))
    for j in ranked:
        if scores[j] <= 0:
            break
        if not shown[j] and fits(keep | {j}):
            keep.add(j)
            # 'This brings the total to 120 hours ...': keep the sentence it refers back to
            if j - 1 >= k and j - 1 not in keep and not shown[j - 1] \
                    and _REFERS_BACK.match(text[units[j][0]:units[j][1]].lstrip(" \u2022")) \
                    and fits(keep | {j - 1}):
                keep.add(j - 1)
    j = k
    while j < len(units):    # then carry on from the opening, a whole sentence or row at a time, while it fits
        if j in keep:
            j += 1
            continue
        end = j + 1
        while end < len(units) and whole[end] == whole[j]:
            end += 1
        same = list(range(j, end))     # the pieces of one sentence or row (they are neighbours)
        if all(shown[x] for x in same):
            j = same[-1] + 1     # already shown above: leave the room to later sentences
            continue
        if not fits(keep | set(same)):
            break
        keep |= set(same)
        j = same[-1] + 1
    return _render_units(text, units, keep)


def _cut_at(text, limit):
    """Cut text at the last sentence (else list item, clause or word) before `limit`, then ' …'."""
    window = text[: limit + 1]
    best = -1
    for m in re.finditer(r"[.!?](?:[\"')\]]*)(?=\s)", window):
        end = m.end()
        if end < limit * 0.45:
            continue
        if m.group(0).startswith(".") and (_ABBREV.search(window[max(0, m.start() - 6): m.start()])
                                           or _is_list_number(window, m.start())):
            continue
        best = end
    if best < 0:
        for m in re.finditer(r"\s•\s", window):
            if m.start() >= limit * 0.45:
                best = m.start()
    if best < 0:
        for m in re.finditer(r"[;:,](?=\s)", window):
            if m.end() >= limit * 0.6:
                best = m.end()
    if best < 0:
        sp = window.rfind(" ")
        best = sp if sp > limit * 0.5 else limit
    return _drop_list_marker(text[:best].rstrip(" ,;:-")) + " …"


# --------------------------------------------------------------------------
# Attachments

# Names that only pasted images, signature logos and placeholders get.
_INLINE_NAME = re.compile(
    r"^(?:image\d*|img\d*|ATT\d+|Outlook-[\w ,.()-]*|~WR[DL]\d*|attachment|attachedimage)"
    r"(?:\.(?:png|jpe?g|gif|bmp|emz|wmz|tmp|htm|html|txt|svg))?$",
    re.I)
# Logos and social-media icons: only image files ('SWMS for signature.pdf' is a document).
_INLINE_KEYWORD_NAME = re.compile(
    r"^[\w .()-]*(?:logo|banner|signature|emailsig|linkedin|facebook|twitter|instagram|youtube|icon_[\w-]*)"
    r"[\w .()-]*\.(?:png|jpe?g|gif|bmp|emz|wmz|svg)$",
    re.I)
# Microsoft 365 share-preview images are named with a bare UUID
_UUID_NAME = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
# Phone-camera file names, where the only information is "some photos"
_CAMERA_NAME = re.compile(
    r"^(?:IMG[_-]?\d+|IMG[_-]\d{8}[_-]\w+|PXL_\d{8}_\w+|DSC[_N]?\d+|DCIM\w*|Photo[_ ]?\d{3,}"
    r"|WhatsApp Image .*|Image - \d[\dT .:-]*|processed-[0-9a-f-]{36}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})(?:\s*\(\d+\))?\.(?:jpe?g|png|heic|heif)$",
    re.I)
_DOC_EXT = {
    "pdf", "doc", "docx", "docm", "xls", "xlsx", "xlsm", "xlsb", "csv", "ppt", "pptx", "dwg", "dxf", "dwf",
    "ifc", "rvt", "nwd", "nwc", "zip", "7z", "rar", "msg", "eml", "txt", "rtf", "odt", "ods", "mpp", "xer",
    "pts", "stp", "step", "kmz", "kml", "shp", "xml", "json", "ics", "vsdx", "pod", "slbx", "daz",
}


def is_inline_attachment(att):
    """True for signature logos and pasted images that should never be listed."""
    if isinstance(att, dict):
        if att.get("inline"):
            return True
        name = att.get("name") or ""
    else:
        name = att or ""
    name = name.strip()
    if not name:
        return True
    if _UUID_NAME.fullmatch(name):
        return True
    return bool(_INLINE_NAME.match(name) or _INLINE_KEYWORD_NAME.match(name))


def is_camera_photo(name):
    """True for phone-camera names such as 'IMG_3067.jpeg' or 'WhatsApp Image ....jpg'."""
    return bool(_CAMERA_NAME.match((name or "").strip()))


def is_document(name):
    """True for document-type attachments (drawings, reports, spreadsheets, archives)."""
    name = (name or "").strip()
    if "." not in name:
        return True  # attached emails have no extension
    return name.rsplit(".", 1)[-1].lower() in _DOC_EXT


# --------------------------------------------------------------------------
# Noise (auto-replies, meeting responses, receipts, notifications)

_NOISE_SUBJECT = [
    ("meeting responses", re.compile(r"^\s*(?:accepted|declined|tentative(?:ly accepted)?|new time proposed)\s*:", re.I)),
    ("auto-replies", re.compile(r"^\s*(?:automatic reply|auto(?:matic)?[- ]?reply|autoreply|out of (?:the )?office"
                                r"(?: reply| autoreply)?|abwesenheitsnotiz|r[e\u00e9]ponse automatique)\s*:", re.I)),
    ("receipts", re.compile(r"^\s*(?:undeliverable|undelivered mail|delivery (?:status notification|has failed|failure)|"
                            r"mail delivery (?:failed|failure|system)|returned mail|read|not read|delivered|"
                            r"recall|message recall)\s*:", re.I)),
]
_NOISE_SENDER = re.compile(
    r"^(?:no-?reply|do-?not-?reply|donotreply|notifications?|mailer-daemon|postmaster|microsoft|"
    r"msonlineservicesteam|sharepoint|noreply-\w+)[\w.+-]*@", re.I)


def meeting_response(record):
    """'accepted' / 'declined' / 'tentative' / 'new time proposed' for meeting responses, else ''."""
    m = re.match(r"^\s*(accepted|declined|tentatively accepted|tentative|new time proposed)\s*:",
                 record.get("subject") or "", re.I)
    if m:
        return m.group(1).lower().replace("tentatively accepted", "tentative")
    ic = (record.get("item_class") or "").lower()
    return {"ipm.schedule.meeting.resp.pos": "accepted", "ipm.schedule.meeting.resp.neg": "declined",
            "ipm.schedule.meeting.resp.tent": "tentative"}.get(ic, "")


# Microsoft Teams 'missed chat' emails: a colleague's chat message filed on purpose,
# so they are kept (shown as one line from that person), unlike other notifications.
_TEAMS_CHAT_SENDER = re.compile(r"@(?:[\w-]+\.)*teams\.(?:mail\.microsoft|microsoft\.com)$", re.I)
_TEAMS_CHAT_SUBJECT = re.compile(r"^.+ sent (?:a message|\d+ messages)\b", re.I)
_TEAMS_CHAT_HEAD = re.compile(r"^(.+?) sent (?:a message|\d+ messages?) in\b", re.I)


def is_teams_chat(record):
    """True for a Teams 'Jo sent a message' email (chat text inside, from a noreply address)."""
    sender = (record.get("sender_email") or "").strip().lower()
    if not _TEAMS_CHAT_SENDER.search(sender):
        return False
    name = normalise_text(record.get("sender_name") or "").strip().strip("\"'")
    return bool(_TEAMS_CHAT_SUBJECT.match(normalise_text(record.get("subject") or "").strip())
                or re.search(r"\sin Teams$", name, re.I))


def teams_chat_messages(body):
    """[(name, text)] of the chat messages in a Teams notification email: the lines
    after each 'Jo sent a message in ...' line, up to a blank line, a bare link,
    'Reply in Teams' or the next such line. The Teams footer is left out."""
    lines = [ln.strip() for ln in normalise_text(body or "").split("\n")]
    out = []
    i = 0
    while i < len(lines):
        m = _TEAMS_CHAT_HEAD.match(lines[i])
        if not m:
            i += 1
            continue
        name = re.sub(r"\s*\+\s*\d+$", "", m.group(1)).strip()   # 'Jo + 1' (a group chat)
        text = []
        i += 1
        while i < len(lines):
            t = lines[i]
            if not t or re.fullmatch(r"<[^>]*>", t) or t.lower().startswith("reply in teams") \
                    or _TEAMS_CHAT_HEAD.match(t):
                break
            text.append(t)
            i += 1
        msg = re.sub(r"\s+", " ", replace_links(" ".join(text))).strip()
        if name and msg:
            out.append((name, msg))
    return out


def noise_kind(record):
    """Return '' for real correspondence, else a short category name used in the header."""
    if is_teams_chat(record):
        return ""      # a colleague's chat message, not a system notification
    item_class = (record.get("item_class") or "")
    subject = record.get("subject") or ""
    if item_class.lower().startswith("ipm.schedule.meeting.resp."):
        return "meeting responses"
    if item_class.upper().startswith("REPORT."):
        return "receipts"
    for kind, rx in _NOISE_SUBJECT:
        if rx.match(subject):
            return kind
    if record.get("auto_reply"):
        return "auto-replies"
    sender = (record.get("sender_email") or "").lower()
    if sender and (_NOISE_SENDER.match(sender) or re.search(r"@[\w.-]*\bmicrosoft(?:\.com)?$", sender)
                   and re.match(r"^(?:no-?reply|notify|notification)", sender)):
        return "notifications"
    return ""


def keyword_list(text):
    """Split the focus keyword setting (commas or new lines) into lower-case terms,
    with smart quotes and dashes made plain like the email text."""
    terms = [normalise_text(k).strip().lower() for k in re.split(r"[,\n;]+", text or "")]
    return [k for k in terms if k]


def fold_for_match(text):
    """Text as compared for focus keywords: tidied like the email text, accents
    removed ('Caf\u00e9' -> 'cafe'), lower case, runs of spaces made one."""
    t = unicodedata.normalize("NFKD", normalise_text(text or ""))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", t.lower())
