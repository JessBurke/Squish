"""Build the Squish digest text from EmailRecord dicts.

build_digest(records, project, source_label="", now=None, outside_dates=0,
cancel=None, progress=None, also_filed=None, no_access_emails=0,
unreadable_files=0) is pure: no file
I/O, and the same input (and `now`) always gives the same output. It raises
DigestCancelled when `cancel` (a threading.Event) is set, and calls
progress(done, total) as it goes.

Steps:
  1. parse dates, clean every body (cleaning.clean_email; a Teams chat email
     becomes its chat text)
  2. drop duplicate copies (Mail Manager files one email in many folders; the
     Sent Items and Inbox copies can be a few seconds apart)
  3. drop noise (auto-replies, meeting responses, receipts, notifications)
  4. learn who is who (names <-> addresses) from senders, recipients and
     quoted headers, so senders filed without an SMTP address can be resolved
  5. group into threads by normalised subject (split where a new conversation
     starts under the same subject); apply focus keywords
  6. pick out answers typed into quoted emails ("see comments below in red"),
     then recover quoted/forwarded emails (and emails attached to emails) that
     were never filed themselves
  7. give people short ORG.Initials aliases
  8. render threads, split into parts, add a self-contained header to each part
"""

import bisect
import re
import unicodedata
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone

from . import cleaning

# --------------------------------------------------------------------------
# Tables the GUI uses. Both are ordered dicts keyed by the setting value.

PART_SIZES = OrderedDict([
    ("small", {"key": "small", "chars": 200000, "label": "Small \u2014 ~57k tokens per file",
               "description": "Several smaller files; drag in only the ones you need."}),
    ("medium", {"key": "medium", "chars": 480000, "label": "Medium \u2014 ~137k tokens per file (default)",
                "description": "Each file fits comfortably in one Claude chat."}),
    ("large", {"key": "large", "chars": 1000000, "label": "Large \u2014 ~286k tokens per file",
               "description": "Fewer, bigger files - only if Claude accepts very large files."}),
    ("single", {"key": "single", "chars": None, "label": "One file",
                "description": "Everything in a single file, however big it gets."}),
])

# attachments: "all" = every non-inline file on every email; "grouped" = a file whose
# name was already listed in the thread is counted as "N as above" (unless the
# email says it is updated/revised: a revision re-issued under the same name must
# stay visible), and 2+ phone-camera photos are shown as "N photos"; "docs" = the
# same as "grouped" for document types only.
# recover_thin_only: recover quoted emails only under emails with almost no text of
# their own, such as "FYI, see below" forwards (their substance is in the quote),
# and only the newest quoted email.
# header_note: what this level trims, said in each part's "How to read" lines.
SQUEEZE_LEVELS = OrderedDict([
    ("light", {"key": "light", "label": "Light \u2014 keep more text",
               "description": "No length limit per email; thank-you emails kept in full; every photo listed.",
               "cap": None, "ack": "keep", "max_people": 3, "attachments": "all",
               "recover": True, "recover_cap": 1500, "recover_thin_only": False,
               "header_note": ""}),
    ("standard", {"key": "standard", "label": "Standard \u2014 recommended (default)",
                  "description": "Long emails trimmed to about 1,500 characters, keeping figures and "
                                 "questions; short thank-you emails shrink to one line.",
                  "cap": 1500, "ack": "short", "max_people": 2, "attachments": "grouped",
                  "recover": True, "recover_cap": 550, "recover_thin_only": False,
                  "header_note": "emails over ~1,500 characters are cut to their opening plus the sentences with "
                                 "figures, dates and questions"}),
    ("max", {"key": "max", "label": "Max \u2014 smallest file",
             "description": "Emails trimmed to about 500 characters, keeping figures and questions; "
                            "thank-you emails left out; only document attachments listed; earlier emails "
                            "recovered only under one-line forwards.",
             "cap": 500, "ack": "drop", "max_people": 0, "attachments": "docs",
             "recover": True, "recover_cap": 250, "recover_thin_only": True,
             "header_note": "emails over ~500 characters are cut to their opening plus the sentences with "
                            "figures, dates and questions; only document attachments are listed"}),
])

DEFAULT_SQUEEZE = "standard"
DEFAULT_PART_SIZE = "medium"

RECOVER_DEPTH = 15         # quoted emails looked at per email (bounded so very long chains stay fast)
TIME_TOLERANCE_MIN = 2     # quoted "Sent:" vs filed email time
THIN_TEXT = 60             # an email with less text of its own is "thin": its substance is in the quote
THREAD_GAP_DAYS = 21       # a non-reply this long after the last email starts a new conversation
SEEN_EMAILS = 10           # a sender's earlier emails in the thread whose sentences a capped email skips
_PREFIX_LEN = 80           # body prefix used to recognise the same email
_MIN_PREFIX = 40

_INLINE_HINT = re.compile(
    r"\b(?:comments?|responses?|answers?|replies|reply|notes?|feedback|markups?|thoughts)\b[^.\n]{0,40}"
    r"\b(?:below|inline|in (?:red|blue|green|bold|orange|purple|yellow|pink)|in-line)\b"
    r"|\bbelow in (?:red|blue|green|bold|orange|purple)\b"
    r"|\b(?:red|blue|green|purple|orange) text below\b",
    re.I)


# --------------------------------------------------------------------------
# Small helpers

def parse_iso(value):
    """'2026-02-20T10:34:00+10:00' -> aware datetime (or naive if no offset); None if blank/bad."""
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        try:
            return datetime.strptime(v[:16], "%Y-%m-%dT%H:%M")
        except ValueError:
            return None


def _utc_key(dt):
    """Sort key: seconds since epoch (naive datetimes treated as UTC)."""
    if dt is None:
        return float("inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _wall(dt):
    """Wall-clock (naive) time as written in the record."""
    return dt.replace(tzinfo=None) if dt is not None else None


def parse_org_codes(text):
    """'example-consulting.com=EC' lines -> [(domain, CODE), ...] in the order given."""
    out = []
    for line in re.split(r"[\n;,]+", text or ""):
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        dom, code = line.split("=", 1)
        dom = dom.strip().lower().lstrip("@").strip(".")
        code = re.sub(r"\s+", "", code)
        if dom and code:
            out.append((dom, code))
    return out


_GENERIC_LABELS = set(
    "com net org edu gov au nz uk co asn id info biz io qld nsw vic wa sa tas nt act mail email eu us ca "
    "de fr int ac sch local online global group govt gob gouv ne or go ltd plc nic mil".split())
# Dropped labels that tell two organisations of the same name apart (riverside.com.au,
# the company, and riverside.nsw.gov.au, the council): the state, else 'gov'
_STATE_LABELS = ("nsw", "vic", "qld", "wa", "sa", "tas", "nt", "act")
_GOV_LABELS = ("gov", "govt", "gob", "gouv", "go")


def _domain_parts(domain):
    """(organisation label, [labels dropped]) of a domain: 'mail.acmepumps.com.au' ->
    ('acmepumps', ['au', 'com']). A final two-letter country code (.sg .ie .za .nz)
    and second-level labels such as com, co, gov, govt and the states are dropped."""
    domain = (domain or "").lower().strip(".")
    labels = [x for x in domain.split(".") if x]
    dropped = []
    if len(labels) > 1 and len(labels[-1]) == 2 and labels[-1].isalpha():
        dropped.append(labels.pop())     # country code: .sg .ie .za .jp .nz ...
    while len(labels) > 1 and labels[-1] in _GENERIC_LABELS:
        dropped.append(labels.pop())
    return re.sub(r"[^a-z0-9]", "", labels[-1] if labels else domain), dropped


def _domain_label(domain):
    """The organisation part of a domain: 'mail.acmepumps.com.au' -> 'acmepumps',
    'nzta.govt.nz' -> 'nzta', 'acmepiling.com.sg' -> 'acmepiling' (country codes and
    labels such as com, co, gov and govt are skipped)."""
    return _domain_parts(domain)[0]


def _org_key(domain):
    """What tells one organisation from another: the domain label, plus the state (or
    'gov') when the domain has one: 'riverside.nsw.gov.au' -> 'riverside/nsw',
    'transport.vic.gov.au' -> 'transport/vic', 'riverside.com.au' -> 'riverside'."""
    label, dropped = _domain_parts(domain)
    qual = next((x for x in dropped if x in _STATE_LABELS), "") or (
        "gov" if any(x in _GOV_LABELS for x in dropped) else "")
    return label + "/" + qual if qual else label


def _configured_code(domain, org_map):
    """The org_codes code for a domain (subdomains match), else None."""
    domain = (domain or "").lower().strip(".")
    for dom, code in org_map:
        if domain == dom or domain.endswith("." + dom):
            return code
    return None


def org_for_domain(domain, org_map):
    """Org code for an email domain: configured code (subdomains match) or the
    second-level domain upper-cased, max 6 characters. (People.org_for_email also
    keeps two organisations whose codes would clash apart.)"""
    domain = (domain or "").lower().strip(".")
    if not domain:
        return "?"
    code = _configured_code(domain, org_map)
    if code:
        return code
    return _domain_label(domain).upper()[:6] or "?"


def unique_org_codes(keys, configured=()):
    """{org key: code} with no two organisations sharing a code. Keys are domain
    labels, or label/qualifier as _org_key gives ('riverside/nsw').

    A label keeps its 6-letter code when no other label (or configured code) has
    it; otherwise each label in the clash gets as many more letters of its name as
    it takes to tell them apart ('cityofnorthvale', 'cityofsouthvale' -> CITYOFN,
    CITYOFS; 'zorbex', 'zorbexwater' -> ZORBEX, ZORBEXW), else a digit. Domains
    with the same label (slrexample.com and slrexample.org) share one code. When
    one label comes with different qualifiers (riverside.com.au and
    riverside.nsw.gov.au; transport.nsw.gov.au and transport.vic.gov.au), each
    qualified one gets its qualifier added (RIVERS, RIVERSNSW; TRANSPNSW,
    TRANSPVIC); a label seen with one qualifier only keeps the plain code."""
    taken = set(configured) | {"?"}
    quals = {}
    for key in keys:
        if key:
            label, _, qual = key.partition("/")
            quals.setdefault(label, set()).add(qual)
    codes = _label_codes(quals, taken)
    out = {}
    for label in sorted(quals):
        if len(quals[label]) == 1:
            qual = next(iter(quals[label]))
            out[label + "/" + qual if qual else label] = codes[label]
            continue
        for qual in sorted(quals[label]):
            if not qual:
                out[label] = codes[label]
                continue
            code = codes[label] + qual.upper()
            k = 2
            while code in taken:
                code = codes[label] + qual.upper() + str(k)
                k += 1
            out[label + "/" + qual] = code
            taken.add(code)
    return out


def _label_codes(labels, taken):
    """{domain label: code} as unique_org_codes describes, codes added to `taken`."""
    groups = {}
    for label in labels:
        if label:
            groups.setdefault(label.upper()[:6], []).append(label)
    out = {}
    for short in sorted(groups):
        members = sorted(set(groups[short]))
        if len(members) == 1 and short not in taken:
            out[members[0]] = short
            taken.add(short)
            continue
        for label in members:
            up = label.upper()
            others = [m.upper() for m in members if m != label]
            code = ""
            for n in range(min(7, len(up)), len(up) + 1):
                cand = up[:n]
                if cand not in taken and (n == len(up) or not any(o[:n] == cand for o in others)):
                    code = cand
                    break
            k = 2
            while not code:
                if short + str(k) not in taken:
                    code = short + str(k)
                k += 1
            out[label] = code
            taken.add(code)
    return out


# Shared-mailbox names: a name-only sender called this is never matched to an outside org
_ROLE_NAMES = set(
    "accounts accountspayable accountsreceivable admin administration enquiries enquiry info "
    "reception office projects project sales support finance payables receivables records "
    "tenders estimating helpdesk noreply donotreply mail team".split())


def _prefix_key(text):
    t = re.sub(r"[^a-z0-9]", "", (text or "").lower())
    return t[:_PREFIX_LEN] if len(t) >= _MIN_PREFIX else ""


# --------------------------------------------------------------------------
# People

class People:
    """Who is who: maps (name, email) observations to people and aliases."""

    def __init__(self, org_map):
        self.org_map = org_map
        self.name_emails = {}      # name key -> Counter(email)
        self.local_emails = {}     # letters of the address before '@' -> Counter(email)
        self.email_names = {}      # email -> Counter(display name)
        self.uses = Counter()      # identity -> times shown
        self.alias = {}            # identity -> "ORG.XX"
        self._merge = {}           # email -> canonical identity
        self.label_codes = {}      # org key (see _org_key) -> org code with no clashes (set by finish)

    def learn(self, name, email):
        email = (email or "").strip().lower()
        if email and not cleaning._EMAIL_RE.fullmatch(email):
            email = ""
        disp = cleaning.tidy_display_name(name or "", "")
        key = cleaning.fold_letters(disp)
        if email:
            local = re.sub(r"[^a-z]", "", email.split("@")[0])
            if len(local) >= 4:
                self.local_emails.setdefault(local, Counter())[email] += 1
            names = self.email_names.setdefault(email, Counter())
            if disp and not cleaning._EMAIL_RE.fullmatch((name or "").strip().strip("'\"")):
                names[disp] += 1
                if len(key) >= 3:
                    self.name_emails.setdefault(key, Counter())[email] += 1

    def assign_org_codes(self):
        """Give every organisation seen its own code (see unique_org_codes)."""
        labels = set()
        for email in self.email_names:
            dom = email.split("@", 1)[1] if "@" in email else ""
            if dom and _configured_code(dom, self.org_map) is None:
                labels.add(_org_key(dom))
        self.label_codes = unique_org_codes(labels, set(code for _, code in self.org_map))

    def finish(self):
        """Fix the org codes, then merge addresses that clearly belong to one person
        (same org, same name)."""
        self.assign_org_codes()
        groups = {}
        for email in sorted(self.email_names):
            nm = self.display_for_email(email)
            key = (self.org_for_email(email), cleaning.fold_letters(nm))
            if key[1] and " " in nm:
                groups.setdefault(key, []).append(email)
        for emails in groups.values():
            canon = max(emails, key=lambda e: (sum(self.email_names[e].values()), -len(e), e))
            for e in emails:
                self._merge[e] = canon

    def org_for_email(self, email):
        dom = email.split("@", 1)[1] if "@" in email else ""
        if not dom:
            return "?"
        code = _configured_code(dom, self.org_map)
        if code:
            return code
        return self.label_codes.get(_org_key(dom)) or org_for_domain(dom, self.org_map)

    def display_for_email(self, email):
        names = self.email_names.get(email)
        if names:
            # prefer names with a space (real 'First Last'), then most common
            best = sorted(names.items(), key=lambda kv: (" " not in kv[0], -kv[1], kv[0]))[0][0]
            return best
        return cleaning.tidy_display_name("", email)

    def identity(self, name, email):
        """Identity string for an observation: 'e:<email>' or '?:<name key>' ('' if nothing known)."""
        email = (email or "").strip().lower()
        if email and not cleaning._EMAIL_RE.fullmatch(email):
            email = ""
        if not email:
            if cleaning._EMAIL_RE.fullmatch((name or "").strip()):
                email = name.strip().lower()
        if not email:
            key = cleaning.name_key(name or "")
            if not key:
                return ""
            cands = self.name_emails.get(key) or self.local_emails.get(key) or {}
            # A sender with no SMTP address is usually internal: prefer addresses in an
            # org_codes org, and never guess an outside org for a shared mailbox name.
            home = dict((e, n) for e, n in cands.items()
                        if _configured_code(e.split("@", 1)[-1], self.org_map) is not None)
            if home or key in _ROLE_NAMES:
                cands = home
            if cands:
                email = sorted(cands.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            else:
                return "?:" + key + "|" + cleaning.tidy_display_name(name or "")
        email = self._merge.get(email, email)
        return "e:" + email

    def first_name_match(self, name):
        """The address of the one person (in an org_codes org, if any) whose first name
        is `name` ('Jo' -> jo.planner@...), else ''. For Teams chats, which give only
        the first name of each person in a group chat."""
        key = cleaning.fold_letters(name or "")
        if not key:
            return ""
        found = set()
        for email in self.email_names:
            words = self.display_for_email(email).split()
            if len(words) >= 2 and cleaning.fold_letters(words[0]) == key:
                found.add(self._merge.get(email, email))
        home = set(e for e in found if _configured_code(e.split("@", 1)[-1], self.org_map) is not None)
        pick = home or found
        return sorted(pick)[0] if len(pick) == 1 else ""

    def info(self, ident):
        """(org code, display name, domain) for an identity."""
        if ident.startswith("e:"):
            email = ident[2:]
            return self.org_for_email(email), self.display_for_email(email), email.split("@", 1)[-1]
        name = ident.split("|", 1)[1] if "|" in ident else ident[2:]
        return "?", name, ""

    def assign_aliases(self, idents):
        """Give each identity a unique ORG.Initials alias (busiest people first); no two
        aliases differ only in letter case."""
        taken = set()      # (lower case: 'SLR.MA' and 'SLR.Ma' would be easy to mix up)
        order = sorted(idents, key=lambda i: (-self.uses[i], self.info(i)[1].lower(), i))
        for ident in order:
            org, name, _ = self.info(ident)
            for cand in _alias_candidates(name):
                alias = org + "." + cand
                if alias.lower() not in taken:
                    break
            taken.add(alias.lower())
            self.alias[ident] = alias

    def org_of(self, ident):
        return self.info(ident)[0]


def _alias_candidates(name):
    """'Jane Citizen' -> JC, JCi, JCit, ..., JC2, JC3 ... (accented and non-Latin letters kept).
    A one-word name ('Reception') -> Re, Rec, Rece, ..., Re2, Re3 ..."""
    name = unicodedata.normalize("NFC", name or "")
    letters = "".join(ch if (ch.isalpha() or ch in "' -") else " " for ch in name)
    tokens = [t for t in letters.split() if any(ch.isalpha() for ch in t)]
    tokens = [t.strip("'-") for t in tokens if t.strip("'-")]
    if len(tokens) == 1:
        tok = "".join(ch for ch in tokens[0] if ch.isalpha())   # ("O'Neil" -> ONeil)
        base = tok[:1].upper() + tok[1:2].lower()
        yield base
        for k in range(3, len(tok) + 1):
            yield tok[:1].upper() + tok[1:k].lower()
        n = 2
        while True:
            yield base + str(n)
            n += 1
    if not tokens:
        base, surname = "X", ""
    else:
        # letters only, so a longer alias never takes an apostrophe or hyphen ('MOn' for O'Neil)
        surname = "".join(ch for ch in tokens[-1] if ch.isalpha())
        first = "".join(ch for ch in tokens[0] if ch.isalpha())
        base = ((first or "X")[0] + (surname or "X")[0]).upper()
    yield base
    for k in range(2, len(surname) + 1):
        yield base + surname[1:k].lower()
    n = 2
    while True:
        yield base + str(n)
        n += 1


# --------------------------------------------------------------------------
# Index of emails (to recognise quoted emails that are filed in their own right)

class EmailIndex:
    """Emails seen so far, looked up by sender and time (or by the start of the text)."""

    MAX_HOURS = 15            # furthest time-zone difference considered

    def __init__(self, same_text=False):
        self.same_text = same_text   # True: a time match also needs the texts to open the same way
        self.by_sender = {}   # sender key -> ([naive dt, sorted], [(cleaned text, payload)] in the same order)
        self.by_prefix = {}   # body prefix -> [(cleaned text, payload)] in the order added

    def add(self, sender_keys, wall_dt, prefix, payload=True, text=""):
        if wall_dt is not None:
            for k in sender_keys:
                if k:
                    times, entries = self.by_sender.setdefault(k, ([], []))
                    pos = bisect.bisect_right(times, wall_dt)
                    times.insert(pos, wall_dt)
                    entries.insert(pos, (text, payload))
        if prefix:
            self.by_prefix.setdefault(prefix, []).append((text, payload))

    def _closest(self, sender_keys, wall_dt, text, exclude=None):
        """(minutes apart, time, payload) of the closest email from this sender, else None.

        Times match within TIME_TOLERANCE_MIN, unless the two texts plainly differ
        (people send two emails a minute apart, or forward their own email to the
        team straight after sending it). A whole-hour difference (up to 15 h) also
        matches, because the quoting computer's time zone may differ from the filing
        one, but only when the two texts open the same way and a short one is not
        just the opening of a longer one (_same_whole_text): a busy sender nearly
        always has some other email a whole number of hours away. `exclude` (the
        email that contains the quote) never matches. Keys are walked in a fixed
        order so the result never depends on set ordering. An empty `text` means a
        match on time alone."""
        best = None
        bounds = _time_window(wall_dt, self.MAX_HOURS)
        if bounds is None:
            return None
        for k in sorted(k for k in sender_keys if k):
            if k not in self.by_sender:
                continue
            times, entries = self.by_sender[k]
            lo = bisect.bisect_left(times, bounds[0])
            hi = bisect.bisect_right(times, bounds[1])
            for n in range(lo, hi):
                if exclude is not None and entries[n][1] is exclude:
                    continue      # an email is never its own quoted email
                dt = times[n]
                diff = abs((dt - wall_dt).total_seconds()) / 60.0
                if diff > TIME_TOLERANCE_MIN:
                    if min(diff % 60, 60 - diff % 60) > TIME_TOLERANCE_MIN \
                            or not _same_whole_text(text, entries[n][0]):
                        continue
                elif self.same_text and text and not _same_start(text, entries[n][0]):
                    continue
                elif _clearly_different(text, entries[n][0]):
                    continue      # same sender, a minute or two apart, but a different email
                if best is None or diff < best[0]:
                    best = (diff, dt, entries[n][1])
        return best

    def find(self, sender_keys, wall_dt, prefix, text="", exclude=None, whole_text=True):
        """Payload of the matching email closest in time, else of one with the same
        body prefix and the same whole text (equal, or one is the start of the
        other: a templated weekly report that opens like a filed one is a different
        email), or None. `whole_text=False`: the prefix alone is enough (a quoted
        copy with answers typed into it still finds its original). `exclude` is
        never returned."""
        if wall_dt is not None:
            best = self._closest(sender_keys, wall_dt, text, exclude)
            if best is not None:
                return best[2]
        for other, payload in (self.by_prefix.get(prefix, []) if prefix else []):
            if payload is exclude:
                continue
            if not whole_text or not text or _same_body(text, other):
                return payload
        return None

    def any_whole_hours_away(self, sender_keys, wall_dt):
        """True when this sender has an email a whole number of hours (1 to 15) from
        wall_dt, within TIME_TOLERANCE_MIN: only then can a time-zone offset match."""
        bounds = _time_window(wall_dt, self.MAX_HOURS)
        if bounds is None:
            return False
        for k in sender_keys:
            if not k or k not in self.by_sender:
                continue
            times = self.by_sender[k][0]
            for n in range(bisect.bisect_left(times, bounds[0]), bisect.bisect_right(times, bounds[1])):
                diff = abs((times[n] - wall_dt).total_seconds()) / 60.0
                if diff > TIME_TOLERANCE_MIN and min(diff % 60, 60 - diff % 60) <= TIME_TOLERANCE_MIN:
                    return True
        return False

    def find_offset(self, sender_keys, wall_dt, text=""):
        """Whole hours from a quoted header's time to its filed copy's time, or None."""
        if wall_dt is None:
            return None
        best = self._closest(sender_keys, wall_dt, text)
        if best is None:
            return None
        return int(round((best[1] - wall_dt).total_seconds() / 3600.0))


def _time_window(wall_dt, hours):
    """(wall_dt - hours, wall_dt + hours), or None at the very ends of the calendar."""
    try:
        span = timedelta(hours=hours)
        return wall_dt - span, wall_dt + span
    except OverflowError:
        return None


def _letters(text):
    """Letters and digits only, lower case (for comparing email texts)."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _same_body(a, b):
    """True when two cleaned texts are the same email text (letters and digits,
    links ignored): equal, or one is the start of the other (a copy cleaned from a
    cut body)."""
    a = _letters((a or "").replace("<link>", ""))
    b = _letters((b or "").replace("<link>", ""))
    if len(a) > len(b):
        a, b = b, a
    return bool(a) and b.startswith(a)


def _clearly_different(a, b):
    """True when two cleaned texts are plainly two different emails: both have text,
    they do not open the same way and they part within the first 20 letters/digits
    (the greeting). A copy with answers typed into it still opens like its original.
    False means "can't tell" (e.g. one text is empty)."""
    a = _letters((a or "").replace("<link>", ""))
    b = _letters((b or "").replace("<link>", ""))
    if not a or not b or _same_start(a, b):
        return False
    n = 0
    while n < min(len(a), len(b), 20) and a[n] == b[n]:
        n += 1
    return n < 20


def _opens_within(text, raw):
    """True when the opening of a cleaned text (letters and digits, first 40) appears
    near the start of a raw quoted body: a cheap 'same email' test before cleaning.
    Short texts ('Thanks all') never pass, so a closing line inside another email
    can't make them look alike."""
    t = _letters(text)[:40]
    return len(t) >= 40 and t in _letters((raw or "")[:3000])


def _same_whole_text(a, b):
    """True when two cleaned texts can be one email seen from two time zones: they
    open the same way (_same_start) and, when either has under 40 letters/digits,
    the shorter has at least 60% of the longer's. So a short 'Please see the
    attached' never stands for a longer email that merely opens the same way."""
    la, lb = len(_letters(a)), len(_letters(b))
    if min(la, lb) < 40 and min(la, lb) < 0.6 * max(la, lb):
        return False
    return _same_start(a, b)


def _same_start(a, b):
    """True when two cleaned email texts open the same way (letters and digits only,
    first 40), or when one contains the other's opening (e.g. an '(accepted)' tag)."""
    a = re.sub(r"[^a-z0-9]", "", (a or "").lower())
    b = re.sub(r"[^a-z0-9]", "", (b or "").lower())
    n = min(len(a), len(b), 40)
    if n == 0:
        return False
    if a[:n] == b[:n]:
        return True
    return n >= 15 and (a[:n] in b or b[:n] in a)


def _sender_keys(people, name, email):
    keys = set()
    email = (email or "").lower()
    if email:
        keys.add("e:" + email)
    nk = cleaning.name_key(name or "")
    if nk:
        keys.add("n:" + nk)
    ident = people.identity(name, email)
    if ident:
        keys.add(ident)
    return keys


# --------------------------------------------------------------------------
# Main entry point

class DigestCancelled(Exception):
    """Raised by build_digest when its `cancel` event is set."""


def build_digest(records, project, source_label="", now=None, outside_dates=0, cancel=None, progress=None,
                 also_filed=None, no_access_emails=0, unreadable_files=0):
    """Turn EmailRecord dicts into digest parts. See DESIGN.md for the format.

    outside_dates: how many email files the engine's date filter left out (shown in
    the header so a date-limited digest is not mistaken for the whole history).
    cancel: a threading.Event; when it is set, DigestCancelled is raised.
    progress: called as progress(done, total) while the emails are cleaned (first
    half of the count) and quoted emails recovered (second half).
    also_filed: EmailRecords that are filed but left out of this digest (the
    engine's date filter): never shown, only used so that an email quoted from
    them is not 'recovered' as if it had never been filed.
    no_access_emails: how many emails the engine left out because Squish has no
    access to their folder (said in the header, like outside_dates).
    unreadable_files: how many email files the engine could not read (said in the
    header, so Claude knows emails may be missing)."""
    project = project or {}
    total = len(records)

    def step(done):
        """Check for Cancel and report progress every 50 emails."""
        if done % 50:
            return
        if cancel is not None and cancel.is_set():
            raise DigestCancelled()
        if progress is not None:
            progress(min(done, total), total)

    level_key = project.get("squeeze") or DEFAULT_SQUEEZE
    level = SQUEEZE_LEVELS.get(level_key, SQUEEZE_LEVELS[DEFAULT_SQUEEZE])
    size_key = project.get("part_size") or DEFAULT_PART_SIZE
    part_limit = PART_SIZES.get(size_key, PART_SIZES[DEFAULT_PART_SIZE])["chars"]
    org_map = parse_org_codes(project.get("org_codes", ""))
    drop_noise = project.get("drop_noise", True)
    recover = bool(project.get("recover_quoted", True)) and level["recover"]
    keywords = cleaning.keyword_list(project.get("focus_keywords", ""))
    now = now or datetime.now()

    stats = {"emails_in": len(records), "emails_used": 0, "duplicates": 0, "noise_dropped": 0,
             "acks_dropped": 0, "filtered_out": 0, "threads": 0, "recovered_quoted": 0,
             "raw_chars": 0, "output_chars": 0, "acks": 0}
    noise_counts = Counter()

    # 1. parse and clean ------------------------------------------------------
    items = []
    for idx, rec in enumerate(records):
        body = rec.get("body") or ""
        stats["raw_chars"] += len(body)
        dt = parse_iso(rec.get("date") or "")
        items.append({"rec": rec, "idx": idx, "dt": dt, "wall": _wall(dt)})
    items.sort(key=lambda it: (_utc_key(it["dt"]), it["rec"].get("path") or "", it["idx"]))

    # 2a. duplicates by Message-ID
    best_by_mid = OrderedDict()
    rest = []
    for it in items:
        mid = (it["rec"].get("message_id") or "").strip().lower()
        if mid:
            if mid in best_by_mid:
                stats["duplicates"] += 1
                best_by_mid[mid] = _better_copy(best_by_mid[mid], it)
                continue
            best_by_mid[mid] = it
        rest.append(it)
    items = [best_by_mid.get((it["rec"].get("message_id") or "").strip().lower(), it)
             if (it["rec"].get("message_id") or "").strip() else it for it in rest]

    for n, it in enumerate(items):
        step(n // 2)
        rec = it["rec"]
        cleaned = cleaning.clean_email(rec.get("body") or "", rec.get("sender_name") or "", RECOVER_DEPTH)
        it["text"] = cleaned["text"]
        it["quoted"] = cleaned["quoted"]
        it["new_raw"] = cleaned["new_raw"]
        it["attached"] = _attached_emails(rec)
        if cleaning.is_teams_chat(rec):
            _use_teams_chat(it)

    # 2b. duplicates without a Message-ID: same sender, start of cleaned body,
    # attachment names, subject, first quoted email and meeting, within
    # TIME_TOLERANCE_MIN minutes and with the same To/Cc. The Sent Items copy and a
    # received copy of one email can be seconds apart across a minute boundary;
    # two "FYI" forwards of different emails, or two emails with different
    # Message-IDs (a corrected resend), stay apart.
    groups = OrderedDict()     # key -> [(utc seconds or None, recipients, index in kept)]
    kept = []
    for it in items:
        rec = it["rec"]
        who = cleaning.name_key(rec.get("sender_name") or "") or (rec.get("sender_email") or "").lower()
        atts = tuple(sorted(((a.get("name") if isinstance(a, dict) else a) or "").lower()
                            for a in (rec.get("attachments") or []) if a and not cleaning.is_inline_attachment(a)))
        first_q = it["quoted"][0] if it["quoted"] else None
        quoted_from = ((first_q["date"].strftime("%Y%m%d%H%M") if first_q.get("date") else ""),
                       cleaning.name_key(first_q.get("sender_name") or "")) if first_q else ("", "")
        key = (who, re.sub(r"\s+", " ", it["text"][:200]).lower(), atts,
               cleaning.thread_key(rec.get("subject") or ""), quoted_from, meeting_label(rec))
        when = _utc_key(it["dt"]) if it["dt"] else None
        rcpts = frozenset((e or "").lower() for _, e in (rec.get("to") or []) + (rec.get("cc") or []) if e)
        match = None
        for t, r, slot in groups.get(key, []):
            same_time = (t is None and when is None) or (
                t is not None and when is not None and abs(when - t) <= TIME_TOLERANCE_MIN * 60)
            if same_time and (not r or not rcpts or r == rcpts) and not _different_ids(kept[slot], it):
                match = slot
                break
        if match is not None:
            stats["duplicates"] += 1
            kept[match] = _better_copy(kept[match], it)
        else:
            groups.setdefault(key, []).append((when, rcpts, len(kept)))
            kept.append(it)
    items = kept
    items.sort(key=lambda it: (_utc_key(it["dt"]), it["rec"].get("path") or "", it["idx"]))

    # 3. noise ------------------------------------------------------------------
    people = People(org_map)
    for it in items:
        _learn_record(people, it)
    for rec in also_filed or []:
        people.learn(rec.get("sender_name"), rec.get("sender_email"))
    kept = []
    for it in items:
        kind = cleaning.noise_kind(it["rec"]) if drop_noise else ""
        if kind == "meeting responses" and _has_comment(it["text"]):
            kind = ""   # an accept/decline with a typed comment is a real reply
            it["text"] = "(%s) %s" % (cleaning.meeting_response(it["rec"]) or "meeting reply", it["text"])
        if kind:
            noise_counts[kind] += 1
            stats["noise_dropped"] += 1
            continue
        kept.append(it)
    items = kept
    people.finish()

    # 4. threads ------------------------------------------------------------------
    threads = OrderedDict()
    for it in items:
        rec = it["rec"]
        subj = rec.get("subject") or rec.get("conversation_topic") or ""
        key = cleaning.thread_key(subj)
        it["meeting"] = meeting_label(rec)
        it["ack"] = not it["meeting"] and cleaning.is_ack_reply(it["text"])
        if it["ack"]:
            stats["acks"] += 1
        threads.setdefault(key, {"key": key, "items": [], "subjects": []})
        threads[key]["items"].append(it)
        threads[key]["subjects"].append(subj)
    thread_list = []
    for th in threads.values():
        th["items"].sort(key=lambda it: (_utc_key(it["dt"]), it["idx"]))
        thread_list.extend(_split_conversations(th, people))
    for th in thread_list:
        th["title"] = cleaning.clean_subject(th["subjects"][0] if th["subjects"] else "")
        for s in th["subjects"]:   # cleanest original: one without RE:/FW:
            if cleaning.clean_subject(s) and not re.match(r"^\s*(re|fw|fwd)\s*:", s or "", re.I):
                th["title"] = cleaning.clean_subject(s)
                break
        th["first"] = _utc_key(th["items"][0]["dt"])
    thread_list.sort(key=lambda th: (th["first"], th["key"]))

    # 5. focus keywords -------------------------------------------------------
    # The body searched is the cleaned text of each email and of its quoted emails,
    # so words found only in signatures and disclaimers do not count. Subjects are
    # cleaned too (no [EXTERNAL] or [Filed ...] tags). Accents and runs of spaces
    # are folded on both sides, and a keyword matches from the start of a word.
    if keywords:
        patterns = [_keyword_pattern(cleaning.fold_for_match(k).strip()) for k in keywords]
        kept_threads = []
        for th in thread_list:
            blob = [th["title"]]
            for it in th["items"]:
                rec = it["rec"]
                blob.append(cleaning.clean_subject(rec.get("subject") or ""))
                blob.append(it["text"])
                blob.append(it.get("meeting") or "")
                blob.extend(_quoted_clean(q) for q in it["quoted"])
                blob.extend(_quoted_clean(q) for q in it["attached"])
                blob.extend((a.get("name") if isinstance(a, dict) else str(a)) or ""
                            for a in (rec.get("attachments") or []) if a and not cleaning.is_inline_attachment(a))
            hay = "\n".join(cleaning.fold_for_match(b) for b in blob)
            if any(p.search(hay) for p in patterns):
                kept_threads.append(th)
            else:
                stats["filtered_out"] += len(th["items"])
        thread_list = kept_threads

    # 6. recover quoted emails -----------------------------------------------------
    filed = EmailIndex()
    for it in items:
        rec = it["rec"]
        filed.add(_sender_keys(people, rec.get("sender_name"), rec.get("sender_email")), it["wall"],
                  _prefix_key(it["text"]), it, it["text"])
    for rec in also_filed or []:
        _add_also_filed(filed, people, rec)
    shown = [it for th in thread_list for it in th["items"]]
    # inline replies first, oldest email first, so an answer already shown under an
    # earlier email is not shown again under a later one
    for it in sorted(shown, key=lambda x: (_utc_key(x["dt"]), x["rec"].get("path") or "", x["idx"])):
        it["inline_extra"] = ""
        if it["quoted"]:
            _inline_replies(it, people, filed)
    zones = _learn_writer_offsets(items, people, filed) if recover else {}
    recovered = EmailIndex(same_text=True)
    seen_undated = set()
    done = 0
    for it in shown:
        done += 1
        step(total // 2 + done // 2)
        it["recovered"] = []
        if not (it["quoted"] or it["attached"]) or not recover:
            continue
        quoted = it["quoted"][:RECOVER_DEPTH]
        attached = it["attached"][:RECOVER_DEPTH]
        if level["recover_thin_only"]:
            if not _is_thin(it):
                continue
            quoted = quoted[:1]   # newest quoted email only
            attached = attached[:1]
        hours = _quote_offset(quoted, people, filed)
        candidates = [(q, writer, hours) for q, writer in zip(quoted, _header_writers(it, quoted, people))]
        candidates += _attached_candidates(attached, people, filed)
        newer = it["wall"]        # time of the newer email shown above this quoted one
        for q, writer, base_hours in candidates:
            if q.get("exact"):
                newer = it["wall"]    # an attached email: its own chain starts here
            if not (q["sender_name"] or q["sender_email"]):
                continue
            keys = _sender_keys(people, q["sender_name"], q["sender_email"])
            hit = filed.find(keys, q["date"], None, exclude=it)
            if hit is not None and _opens_within(hit["text"], q["body"]):
                continue      # filed at the same time (cheap check before cleaning)
            qclean = _quoted_clean(q)
            qtext = qclean
            if q.get("when"):
                label = "When: " + q["when"].rstrip(" .")
                place = _quoted_place(q.get("where", ""))
                if place:
                    label += " @ " + place
                qtext = (label + ". " + qtext).strip()
            if not qtext.strip():
                continue
            prefix = _prefix_key(qclean)
            if filed.find(keys, q["date"], prefix, qclean, exclude=it) is not None:
                continue
            if q.get("exact"):
                shift = 0     # an attached email's time is read from the file itself
            else:
                # A quoted header's time is in the time zone of the person who quoted it
                shift = _writer_offset(zones, writer, q["date"])
                if shift is None:
                    shift = base_hours
            qdate = q["date"]
            if shift and qdate is not None:
                try:
                    qdate = q["date"] + timedelta(hours=shift)
                except OverflowError:
                    qdate = q["date"]
            if shift and newer is not None and q["date"] is not None and qdate > newer >= q["date"]:
                qdate = q["date"]     # a quoted email is never newer than the one quoting it
            if qdate is not None:
                newer = qdate
            if recovered.find(keys, qdate, prefix, qtext) is not None:
                continue
            if qdate is None and not prefix:
                # undated and short: recognise a repeat by sender and text
                skey = (people.identity(q["sender_name"], q["sender_email"]) or min(keys or [""]),
                        _norm_line(qtext))
                if skey in seen_undated:
                    continue
                seen_undated.add(skey)
            recovered.add(keys, qdate, prefix, True, qtext)
            if level["ack"] != "keep" and cleaning.is_ack_reply(qtext):
                continue
            ident = people.identity(q["sender_name"], q["sender_email"])
            entry = {"ident": ident, "date": qdate, "text": qtext}
            if q.get("from_attachment"):
                it["recovered"].append(entry)
            else:
                it["recovered"].insert(0, entry)
            stats["recovered_quoted"] += 1
        if any(q.get("from_attachment") for q, _w, _h in candidates):
            # attached and quoted emails interleaved, oldest first (undated last)
            it["recovered"].sort(key=lambda r: (r["date"] is None, r["date"] or datetime.min))

    # 7. aliases for everybody who will be shown --------------------------------
    for it in shown:
        rec = it["rec"]
        if it.get("teams_from"):     # the colleague, not the shared noreply address
            email = people.first_name_match(it["teams_from"]) if " " not in it["teams_from"] else ""
            it["from"] = ("e:" + email) if email else people.identity(it["teams_from"], "")
        else:
            it["from"] = people.identity(rec.get("sender_name"), rec.get("sender_email"))
        to = [people.identity(n, e) for n, e in (rec.get("to") or [])]
        cc = [people.identity(n, e) for n, e in (rec.get("cc") or [])]
        to = [t for t in _unique(to) if t]
        cc = [c for c in _unique(cc) if c]
        others = [t for t in to if t != it["from"]] or to
        it["to"] = others if others else [c for c in cc if c != it["from"]]
        people.uses[it["from"]] += 1
        for t in it["to"]:
            people.uses[t] += 1
        for r in it["recovered"]:
            people.uses[r["ident"]] += 1
    used = set(i for i in people.uses if i)
    people.assign_aliases(used)

    # 8. render ----------------------------------------------------------------
    blocks = []
    for th in thread_list:
        block = _render_thread(th, people, level, stats)
        if block["entries"]:
            blocks.append(block)
    stats["threads"] = len(blocks)
    stats["emails_used"] = sum(b["emails"] for b in blocks)

    all_dates = [d for b in blocks for d in b["span_dates"]]     # recovered emails' dates included
    first_date = min(all_dates).strftime("%Y-%m-%d") if all_dates else ""
    last_date = max(all_dates).strftime("%Y-%m-%d") if all_dates else ""

    header_info = {
        "name": project.get("name") or "Emails",
        "source": source_label or "",
        "squeeze": level["key"],
        "made": now.strftime("%Y-%m-%d %H:%M"),
        "first": first_date, "last": last_date,
        "emails": stats["emails_used"], "threads": stats["threads"],
        "dropped": _dropped_text(stats, noise_counts, level),
        "date_filter": _date_filter_text(project, outside_dates),
        "no_access": _no_access_text(no_access_emails),
        "unreadable": _unreadable_text(unreadable_files),
        "keywords": keywords,
        "recover": recover, "ack": level["ack"],
    }

    def check():
        """Stop when the run is cancelled (called while the parts are packed)."""
        if cancel is not None and cancel.is_set():
            raise DigestCancelled()

    parts = _split_parts(blocks, part_limit, header_info, people, check)
    stats["output_chars"] = sum(len(p["text"]) for p in parts)
    if progress is not None:
        progress(total, total)
    return {"parts": parts, "stats": stats}


def _split_conversations(th, people):
    """Split one subject's emails (sorted by date) into conversations.

    A new conversation starts with an email sent more than THREAD_GAP_DAYS after
    the previous email with that subject when it is not a reply (plain or FW:,
    not RE:/Accepted: ...), or when it is a reply whose first quoted email is not
    in the conversation so far (it answers something else, often an email that
    was never filed) - unless it quotes, at any depth, an email already in the
    conversation. So generic subjects ('Attached Image', a bare project name,
    'Invoice') do not merge exchanges from different months, while a late forward
    of the conversation stays with it. A reply with no quoted email, or whose
    quoted sender or date can't be read, stays; undated emails never start a new
    conversation."""
    groups = [[]]
    index = EmailIndex()     # the current conversation's emails (to match quoted ones)
    prev = None
    for it in th["items"]:
        if prev is not None and prev["dt"] is not None and it["dt"] is not None and \
                _utc_key(it["dt"]) - _utc_key(prev["dt"]) > THREAD_GAP_DAYS * 86400:
            rec = it["rec"]
            subj = rec.get("subject") or rec.get("conversation_topic") or ""
            if cleaning.is_reply_subject(subj):
                split = _answers_elsewhere(it, people, index) and not _quotes_conversation(it, people, index)
            else:
                split = not _quotes_conversation(it, people, index)
            if split:
                groups.append([])
                index = EmailIndex()
        groups[-1].append(it)
        rec = it["rec"]
        index.add(_sender_keys(people, rec.get("sender_name"), rec.get("sender_email")), it["wall"],
                  _prefix_key(it["text"]), it, it["text"])
        prev = it
    out = []
    for n, group in enumerate(groups):
        out.append({"key": th["key"] if n == 0 else th["key"] + "#%d" % n, "items": group,
                    "subjects": [it["rec"].get("subject") or it["rec"].get("conversation_topic") or ""
                                 for it in group]})
    return out


def _answers_elsewhere(it, people, index):
    """True when a reply's first quoted email has a sender and a date and is not
    one of the emails in `index` (matched the way quoted emails are recovered)."""
    q = it["quoted"][0] if it.get("quoted") else None
    if q is None or q["date"] is None or not (q["sender_name"] or q["sender_email"]):
        return False
    keys = _sender_keys(people, q["sender_name"], q["sender_email"])
    qclean = _quoted_clean(q)
    return index.find(keys, q["date"], _prefix_key(qclean), qclean) is None


def _quotes_conversation(it, people, index):
    """True when any email this one quotes (at any depth) has a sender and a date
    and is one of the emails in `index` (the conversation so far), matched the way
    quoted emails are recovered."""
    for q in it.get("quoted") or []:
        if q["date"] is None or not (q["sender_name"] or q["sender_email"]):
            continue
        keys = _sender_keys(people, q["sender_name"], q["sender_email"])
        qclean = _quoted_clean(q)
        if index.find(keys, q["date"], _prefix_key(qclean), qclean) is not None:
            return True
    return False


def _is_thin(it):
    """An email with almost no text of its own ('FYI, see below', 'Approved, thanks'):
    its substance is in the email it quotes or forwards."""
    return len(_strip_greeting(it["text"]).strip()) < THIN_TEXT


def _quote_offset(quoted, people, filed):
    """Whole hours to add to the times in these quoted headers so they read like the
    filed emails' times (the quoting computer may be in another time zone). Found
    from a quoted email whose filed copy is a whole number of hours away; 0 if none.
    Used only when the header writer's own offset is unknown (_writer_offset)."""
    for q in quoted:
        hours = _filed_offset(q, people, filed)
        if hours:
            return hours
    return 0


def _filed_offset(q, people, filed):
    """Whole hours from one quoted header's time to its filed copy's time (0 when
    filed at the very same time), or None when it is not filed."""
    if q["date"] is None or not (q["sender_name"] or q["sender_email"]):
        return None
    keys = _sender_keys(people, q["sender_name"], q["sender_email"])
    hours = filed.find_offset(keys, q["date"])
    if hours != 0:
        if not filed.any_whole_hours_away(keys, q["date"]):
            return None     # (nothing to compare: skip cleaning the quoted text)
        hours = filed.find_offset(keys, q["date"], _quoted_clean(q))
    if hours is None or abs(hours) > 14:
        return None
    return hours


def _header_writers(it, quoted, people):
    """Who wrote each quoted header's time: the Outlook of the person who quoted it,
    that is the sender of the email one level newer (this email's sender for the
    first quoted email)."""
    rec = it["rec"]
    writers = [people.identity(rec.get("sender_name"), rec.get("sender_email"))]
    for q in quoted[:-1]:
        writers.append(people.identity(q["sender_name"], q["sender_email"]))
    return writers


def _learn_writer_offsets(items, people, filed):
    """{(writer, 'YYYY-MM'): Counter(hours)} learned from quoted emails that are also
    filed: each person's Outlook writes quoted times in that person's time zone
    (Queensland has no daylight saving, NSW has; people travel)."""
    zones = {}
    seen = set()
    for it in items:
        quoted = (it.get("quoted") or [])[:RECOVER_DEPTH]
        for q, writer in zip(quoted, _header_writers(it, quoted, people)):
            if not writer or q["date"] is None:
                continue
            key = (writer, people.identity(q["sender_name"], q["sender_email"]), q["date"])
            if key in seen:
                continue
            seen.add(key)
            hours = _filed_offset(q, people, filed)
            if hours is not None:
                zones.setdefault((writer, q["date"].strftime("%Y-%m")), Counter())[hours] += 1
    return zones


def _writer_offset(zones, writer, date):
    """The offset this writer's quoted times usually need that month, or None when
    unknown or undecided (a tie)."""
    if not writer or date is None:
        return None
    counts = zones.get((writer, date.strftime("%Y-%m")))
    if not counts:
        return None
    top = counts.most_common(2)
    if len(top) > 1 and top[0][1] == top[1][1]:
        return None
    return top[0][0]


def _different_ids(a, b):
    """True when both emails have a Message-ID and they differ: two different emails,
    even if they look alike (copies filed by Mail Manager share one Message-ID)."""
    ida = (a["rec"].get("message_id") or "").strip().lower()
    idb = (b["rec"].get("message_id") or "").strip().lower()
    return bool(ida and idb and ida != idb)


def _use_teams_chat(it):
    """Show a Teams 'Jo sent a message' email as the chat text, from Jo."""
    rec = it["rec"]
    msgs = cleaning.teams_chat_messages(rec.get("body") or "")
    if not msgs:
        return
    text = msgs[0][1]
    for name, msg in msgs[1:]:
        text += " | %s: %s" % (name, msg)
    it["text"] = text
    it["quoted"] = []
    it["new_raw"] = text
    full = cleaning.tidy_display_name(rec.get("sender_name") or "")    # drops ' in Teams'
    first = msgs[0][0]
    it["teams_from"] = full if (full == first or full.startswith(first + " ")) else first


# How a reference number may be joined to its word: 'RFI 12', 'RFI-12', 'RFI_12',
# 'RFI #12', 'RFI12', 'RFI No. 12' ('no' only after a space or mark, so 'piano 3'
# is not 'pia 3').
_REF_JOIN = r"(?:[ _#-]*|[ _#-]+no\.?[ _#-]*)"
# a word (2+ letters) followed, with or without one of those joins, by a number, in a keyword
_KEYWORD_REF = re.compile(r"(?<=[a-z]{2})" + _REF_JOIN + r"0*(?=[0-9])")


def _keyword_pattern(keyword):
    """Regex for a folded focus keyword: it must start at a word start, and one that
    ends in a digit must not run on into a longer number ('rfi 12' is not 'rfi 125').
    Where a word is followed by a number, the join and leading zeros may differ, so
    'rfi 12' also finds 'RFI-012', 'RFI_12', 'RFI #12', 'RFI12' and 'RFI No. 12'."""
    pre = r"(?<![a-z0-9])" if keyword[:1].isalnum() else ""
    post = r"(?![0-9])" if keyword[-1:].isdigit() else ""
    pieces = _KEYWORD_REF.split(keyword)
    body = (_REF_JOIN + "0*").join(re.escape(piece) for piece in pieces)
    return re.compile(pre + body + post)


def _add_also_filed(filed, people, rec):
    """Add an email that is filed but not in this digest (outside the dates) to the
    index of filed emails, so quoting it does not 'recover' it. Only the opening of
    its text is needed, so just the start of the body is cleaned."""
    dt = parse_iso(rec.get("date") or "")
    body = cleaning.normalise_text((rec.get("body") or "")[:4000])
    text = cleaning.clean_text(cleaning.split_quoted(body)[0], rec.get("sender_name") or "")
    stub = {"rec": rec, "text": text, "quoted": [], "dt": dt, "wall": _wall(dt)}
    filed.add(_sender_keys(people, rec.get("sender_name"), rec.get("sender_email")), _wall(dt),
              _prefix_key(text), stub, text)


def _attached_emails(rec):
    """Emails attached to this email (an email forwarded as an attachment, or a
    client's approval attached as proof), as quoted-style dicts like
    cleaning.parse_quoted gives, newest first: each attached email, then the
    emails quoted inside it. The attached email itself has "exact": True (its
    time is read from the file, so no time-zone shift applies). Readers fill
    attachment["email"] one level deep (see readers.EMBEDDED_BODY_MAX)."""
    out = []
    for a in rec.get("attachments") or []:
        if not isinstance(a, dict) or cleaning.is_inline_attachment(a):
            continue
        e = a.get("email")
        if not isinstance(e, dict) or not (e.get("sender_name") or e.get("sender_email")):
            continue
        sender = e.get("sender_name") or ""
        cleaned = cleaning.clean_email(e.get("body") or "", sender, RECOVER_DEPTH)
        out.append({"sender_name": sender, "sender_email": (e.get("sender_email") or "").lower(),
                    "date": _wall(parse_iso(e.get("date") or "")), "date_text": "",
                    "to": [list(x) for x in e.get("to") or []],
                    "cc": [list(x) for x in e.get("cc") or []],
                    "subject": e.get("subject") or "", "when": "",
                    "body": cleaned["new_raw"], "clean": cleaned["text"],
                    "exact": True, "from_attachment": True})
        for q in cleaned["quoted"]:
            q["from_attachment"] = True
            out.append(q)
    return out


def _attached_candidates(attached, people, filed):
    """(quoted-style dict, header writer, fallback hours) for the emails attached to
    an email and the emails quoted inside them. A quoted header inside an attached
    email was written by the Outlook of the email one level newer in that chain."""
    hours = _quote_offset([q for q in attached if not q.get("exact")], people, filed)
    out = []
    for n, q in enumerate(attached):
        writer = None     # an attached email's own time needs no time-zone shift
        if not q.get("exact"):
            prev = attached[n - 1]   # (the list always starts with an attached email)
            writer = people.identity(prev["sender_name"], prev["sender_email"])
        out.append((q, writer, hours))
    return out


def _better_copy(a, b):
    """Of two copies of one email keep the more complete one (stable for ties)."""
    def score(it):
        rec = it["rec"]
        return (bool(rec.get("sender_email")), bool(it["dt"]), len(rec.get("attachments") or []),
                len(rec.get("body") or ""))
    return b if score(b) > score(a) else a


def _learn_record(people, it):
    rec = it["rec"]
    if not cleaning.is_teams_chat(rec):   # (one noreply address for many colleagues)
        people.learn(rec.get("sender_name"), rec.get("sender_email"))
    for n, e in (rec.get("to") or []) + (rec.get("cc") or []):
        people.learn(n, e)
    for q in (it.get("quoted") or []) + (it.get("attached") or []):
        people.learn(q["sender_name"], q["sender_email"])
        for n, e in q["to"] + q["cc"]:
            people.learn(n, e)


def _strip_greeting(text):
    return cleaning.strip_greeting(text)


def _has_comment(text):
    """A meeting response with something typed by a person (not 'Your request was accepted')."""
    t = _strip_greeting(text or "")
    if not t or cleaning.is_ack_reply(text or ""):
        return False
    return not re.match(r"^(?:your request was|this meeting|the meeting|.{0,40} has (?:accepted|declined))", t, re.I)


def _unique(seq):
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


INLINE_DEPTH = 3     # quoted levels searched for answers typed into them

_LINK_RE = re.compile(r"<(?:https?|mailto|tel|file|cid):[^>]*>|\bhttps?://\S+", re.I)


def _line_words(line):
    """[(word, key)] for the words of a line that have letters or digits. Links are
    left out: a SafeLinks address is different in every copy of an email."""
    out = []
    for w in _LINK_RE.sub(" ", line or "").split():
        k = _norm_line(w)
        if k:
            out.append((w, k))
    return out


def _known(orig):
    """What is already known of a filed email's lines: its own lines plus answers
    already shown under an earlier email. ({line key}, {first 3 word keys: [word keys]})."""
    if "inline_known" not in orig:
        orig["inline_known"] = (set(), {})
        _remember(orig["inline_known"], cleaning.normalise_text(orig["rec"].get("body") or "").split("\n"))
    return orig["inline_known"]


def _remember(known, lines):
    """Add lines to a _known structure."""
    keys, starts = known
    for ln in lines:
        wk = tuple(k for _, k in _line_words(ln))
        if not wk:
            continue
        keys.add("".join(wk))
        if len(wk) >= 3:
            starts.setdefault(wk[:3], []).append(wk)


def _added_part(line, known):
    """What a quoted line adds to the filed original: '' if nothing, the words typed
    after a known line ('re "Is the cover 50mm\u2026": no, 65mm'), else the whole line."""
    keys, starts = known
    words = _line_words(line)
    wk = tuple(k for _, k in words)
    if not wk or "".join(wk) in keys or len("".join(wk)) <= 3:
        return ""
    best = 0
    for cand in starts.get(wk[:3], []):
        if best < len(cand) < len(wk) and wk[:len(cand)] == cand:
            best = len(cand)
    if not best:
        return line
    head = " ".join(w for w, _ in words[:min(best, 6)]).rstrip(" .:?")
    return 're "%s\u2026": %s' % (head, " ".join(w for w, _ in words[best:]))


# A line that starts with its own list number or letter ('b. The updated plan ...'):
# a point added or reworded in the quoted copy, not an answer to the line above
_LIST_ITEM_START = re.compile(r"^\s*\(?(?:\d{1,2}|[a-z]|[ivx]{1,4})[.)]\s", re.I)


def _label_answers(lines, known, author):
    """The added parts of a quoted copy's lines (see _added_part), with each answer
    typed on its own line(s) straight after a line of the original (blank lines
    aside) labelled with that line's first words, as for an answer typed on the same
    line: 're "2. Is cover 65 mm\u2026": No, 50 mm at the edges.' A run of added lines
    that cleans away to nothing (a signature), or that starts with its own list
    number ('b. ...', a point added or reworded), is left unlabelled."""
    keys = known[0]
    runs = []        # [[words of the original line above, or None], [added parts]]
    point = None
    for ln in lines:
        words = _line_words(ln)
        if not words:
            continue                      # blank lines and lone bullets keep the link
        part = _added_part(ln, known)
        if not part:
            if "".join(k for _, k in words) in keys:
                point = words             # a line of the original
            continue
        if point is not None or not runs:
            runs.append([point if part == ln else None, []])   # (a same-line answer already has its 're')
        runs[-1][1].append(part)
        point = None                      # later added lines continue this answer
    out = []
    for point, parts in runs:
        if point and not _LIST_ITEM_START.match(parts[0]) and cleaning.clean_text("\n".join(parts), author):
            head = " ".join(w for w, _ in point[:6]).rstrip(" .:?")
            parts = ['re "%s\u2026": %s' % (head, cleaning.strip_bullet(parts[0]))] + parts[1:]
        out.extend(parts)
    return out


def _has_inline_hint(new_raw):
    """The email says its answers are in the quoted text ('see my comments below in
    red'); not someone else's ('I will use your response below')."""
    head = new_raw[:3000]
    for m in _INLINE_HINT.finditer(head):
        if not re.search(r"\b(?:your|his|her|their)\s+$", head[max(0, m.start() - 8):m.start()], re.I):
            return True
    return False


def _inline_replies(it, people, filed):
    """When an email says 'see comments below in red', the answers live inside the
    quoted copies of earlier emails. Each quoted email (newest first, up to
    INLINE_DEPTH, while each one is filed) is compared with its filed original,
    ignoring links; what it adds, less answers already shown under an earlier
    email, is cleaned with the quoted author's name (so their signature goes) and
    kept as it["inline_extra"]; an answer typed under a point is labelled with
    the point's first words (_label_answers)."""
    if not _has_inline_hint(it.get("new_raw") or ""):
        return
    found = []
    for q in it["quoted"][:INLINE_DEPTH]:
        keys = _sender_keys(people, q["sender_name"], q["sender_email"])
        qclean = _quoted_clean(q)
        # (whole_text=False: a copy with answers typed into it differs from its original)
        orig = filed.find(keys, q["date"], _prefix_key(qclean), qclean, exclude=it, whole_text=False)
        if orig is None or orig is it:
            break     # deeper copies may hold answers from an email that is not filed
        known = _known(orig)
        lines = q["body"].split("\n")
        added = [x for x in (_added_part(ln, known) for ln in lines) if x]
        if added:
            author = q["sender_name"] or ""
            extra = cleaning.clean_text("\n".join(added), author)
            if extra and len(extra) > 15:
                # label each answer with the point it answers (before this copy is remembered)
                found.append(cleaning.clean_text("\n".join(_label_answers(lines, known, author)), author))
        _remember(known, lines)
    if found:
        it["inline_extra"] = " ".join(found)


def _quoted_clean(q):
    """Cleaned text of a quoted email (computed once)."""
    if "clean" not in q:
        q["clean"] = cleaning.clean_text(q["body"], q["sender_name"])
    return q["clean"]


def _norm_line(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# --------------------------------------------------------------------------
# Rendering

def _fmt_date(dt):
    return dt.strftime("%y-%m-%d") if dt else ""


_PLACE_MAX = 80   # longest meeting location shown


def _short_place(text):
    """A meeting place on one line, cut at _PLACE_MAX characters with '\u2026'."""
    place = cleaning.normalise_text(text or "")
    place = re.sub(r"\s+", " ", place).strip(" ;,")
    if len(place) > _PLACE_MAX:
        place = place[:_PLACE_MAX - 1].rstrip() + "\u2026"
    return place


_ONLINE_PLACE = re.compile(r"\b(?:microsoft teams|teams meeting|zoom|webex|online)\b|https?:", re.I)


def _quoted_place(text):
    """The place of a quoted meeting invite ('Where: Site office, 1 Example St'),
    or '' for an online meeting (Teams, Zoom ...) or a blank value."""
    place = _short_place(text).rstrip(" .")
    if not re.search(r"[A-Za-z0-9]", place) or _ONLINE_PLACE.search(place):
        return ""
    return place


def meeting_label(rec):
    """'meeting 25-03-13 14:00-15:00 @ Site office' for a meeting request or
    appointment that has a start time ('meeting cancelled ...' for a
    cancellation), else ''. Times are as written in the record (local time)."""
    meeting = rec.get("meeting")
    if not isinstance(meeting, dict):
        return ""
    start = _wall(parse_iso(meeting.get("start") or ""))
    if start is None:
        return ""
    end = _wall(parse_iso(meeting.get("end") or ""))
    when = start.strftime("%y-%m-%d %H:%M")
    if end is not None and end > start:
        midnight = start.time() == end.time() == datetime.min.time()
        if midnight:      # an all-day event ends at midnight after its last day
            last = end - timedelta(days=1)
            when = start.strftime("%y-%m-%d") + (
                "" if last.date() == start.date() else " to " + last.strftime("%y-%m-%d")) + " all day"
        elif end.date() == start.date():
            when += "-" + end.strftime("%H:%M")
        else:
            when += " to " + end.strftime("%y-%m-%d %H:%M")
    place = _short_place(meeting.get("location") or "")
    cancelled = "canceled" in (rec.get("item_class") or "").lower()
    return ("meeting cancelled " if cancelled else "meeting ") + when + (" @ " + place if place else "")


def _cap_with_inline(text, extra, cap, seen=""):
    """Cap an email's own text and its inline replies separately so that together
    they stay near `cap`, and the '[inline replies: ...]' label and its closing
    bracket always survive. `seen`: what the same sender already showed earlier in
    the thread (sentences repeated from it are dropped first; see cleaning.cap_text)."""
    if not extra:
        return cleaning.cap_text(text, cap, seen)
    if cap:
        room = max(cap - len(" [inline replies: ]"), 100)
        text = cleaning.cap_text(text, max(room - len(extra), room // 2), seen)
        extra = cleaning.cap_text(extra, max(room - len(text), room // 2))
    return (text + " [inline replies: " + extra + "]").strip()


def _render_thread(th, people, level, stats):
    """Turn a thread into {"title", "entries": [entry...], "emails", "dates", "idents",
    "org_idents"}. An entry is one email (plus its recovered quoted emails) and cannot
    be split."""
    entries = []
    dates = []
    seen_att = set()
    shown_by = {}     # sender -> that sender's texts as shown so far in this thread
    for it in th["items"]:
        rec = it["rec"]
        text = it["text"]
        atts, again, again_note = _attachments(rec, level, seen_att, it["text"])
        if it["ack"]:
            if level["ack"] == "drop" and not it["recovered"]:
                if not atts:
                    stats["acks_dropped"] += 1
                    continue
                text = ""   # keep the record of the files sent, drop the thanks
            elif level["ack"] != "keep":
                text = "(ack)"
        if text != "(ack)":
            # (an unknown sender never pools with another unknown sender)
            seen = " ".join(shown_by.get(it["from"], [])[-SEEN_EMAILS:]) if it["from"] else ""
            text = _cap_with_inline(text, it.get("inline_extra") or "", level["cap"], seen)
            if it["from"]:
                shown_by.setdefault(it["from"], []).append(text)
        if it.get("meeting"):
            # after the cap, so the date, time and place are never trimmed
            text = "[%s] %s" % (it["meeting"], text) if text else "(%s)" % it["meeting"]
        elif not text and not atts and not again:
            text = "(meeting invite)" if cleaning._TEAMS_START.search(it.get("new_raw") or "") or \
                re.search(r"(?m)^\W*(?:microsoft teams|join zoom meeting)", it.get("new_raw") or "", re.I) \
                else "(no text)"
        elif not text:
            text = ""
        entry = {
            "dt": it["wall"],
            "from": it["from"],
            "to": it["to"],
            "text": text,
            "atts": atts,
            "again": again,
            "again_note": again_note,
            "recovered": [],
        }
        for r in it["recovered"]:
            rtext = cleaning.cap_text(r["text"], level["recover_cap"])
            entry["recovered"].append({"dt": r["date"], "from": r["ident"], "text": rtext})
        if it["wall"]:
            dates.append(it["wall"])
        entries.append(entry)
    idents = set()
    org_idents = set()
    for e in entries:
        e["shown"] = _shown_idents(e, level["max_people"])
        e["org_only"] = set(t for t in e["to"] if t) - e["shown"]
        idents |= e["shown"]
        org_idents |= e["org_only"]
    return {"title": th["title"], "entries": entries, "emails": len(entries), "dates": dates,
            "span_dates": _shown_dates(entries), "idents": idents, "org_idents": org_idents}


def _shown_dates(entries):
    """Dates of the emails shown in these entries, recovered (\u21b3) ones included."""
    return [e["dt"] for e in entries if e["dt"]] + [
        r["dt"] for e in entries for r in e.get("recovered", []) if r.get("dt") is not None]


def _shown_idents(entry, max_people):
    """People whose alias appears on this entry's lines (for the per-part legend)."""
    shown = {entry["from"]}
    if max_people and len(entry["to"]) <= max_people:
        shown.update(entry["to"])
    shown.update(r["from"] for r in entry["recovered"])
    shown.discard("")
    return shown


# Words saying a file is a new version even though its name is unchanged
_REVISION_HINT = re.compile(
    r"\b(?:updated?|amended|amendments?|revised|revisions?|latest|corrected|correction|new version|"
    r"re-?issued?|superseded?|mark-?ups?|marked[- ]up|rev\.? ?[a-z0-9]{1,2})\b", re.I)


def _attachments(rec, level, seen, text=""):
    """Attachment names to list for one email, how many files were left out as
    "as above" (their names were already listed earlier in the thread), and the
    numbers of the camera photos among those ('IMG_9729,9730', else '').

    Inline images are never listed. With "all" every file is listed every time.
    Otherwise a repeated name is listed again only when the email's text says the
    file is updated, revised, amended ... (a revision re-issued under the same
    name); "grouped" shows 2+ phone-camera photos as "N photos" plus their
    numbers ('9 photos IMG_9720-9728'), because people refer to site photos by
    number; "docs" lists document types only."""
    mode = level["attachments"]
    revised = bool(_REVISION_HINT.search(text or ""))
    names = []
    again = []
    again_low = set()
    for att in rec.get("attachments") or []:
        if cleaning.is_inline_attachment(att):
            continue
        name = (att.get("name") if isinstance(att, dict) else str(att)) or ""
        name = cleaning.normalise_text(name).strip()
        if mode == "docs" and not cleaning.is_document(name):
            continue
        low = name.lower()
        if mode != "all" and low in seen and not revised:
            if low not in again_low:
                again_low.add(low)
                again.append(name)
            continue
        seen.add(low)
        if name not in names:
            names.append(name)
    note = ""
    if mode == "grouped":
        photos = [n for n in names if cleaning.is_camera_photo(n)]
        if len(photos) >= 2:
            at = names.index(photos[0])
            names = [n for n in names if n not in photos]
            names.insert(at, _photo_label(photos))
        note = _photo_numbers([n for n in again if cleaning.is_camera_photo(n)])
    return names, len(again), note


_PHOTO_LIST_MAX = 6     # up to this many photo numbers are listed; more contiguous ones as a range


def _photo_numbers(photos):
    """'IMG_9720-9728' or 'IMG_9720,9722,9729': the numbers of camera photos named
    with one prefix and a number ('IMG_9720.jpeg'); '' when there are none or their
    numbers can't be read that way."""
    nums = []
    prefixes = set()
    for name in photos:
        base = re.sub(r"\s*\(\d+\)(?=\.[^.]+$)", "", name)     # 'IMG_9720 (1).jpg', a copy
        m = re.search(r"(\d+)(?=\.[^.]+$)", base)
        if not m:
            return ""
        nums.append(m.group(1))
        prefixes.add(base[:m.start()])
    if len(prefixes) != 1:
        return ""
    prefix = prefixes.pop()
    nums = sorted(set(nums), key=lambda x: (int(x), x))
    values = [int(x) for x in nums]
    contiguous = values == list(range(values[0], values[0] + len(values)))
    if len(nums) > _PHOTO_LIST_MAX and contiguous:
        return prefix + nums[0] + "-" + nums[-1]
    return prefix + ",".join(nums)


def _photo_label(photos):
    """'9 photos IMG_9720-9728', '3 photos IMG_9720,9722,9729', or '9 photos' when
    the numbers can't be read (see _photo_numbers)."""
    nums = _photo_numbers(photos)
    return "%d photos" % len(photos) + (" " + nums if nums else "")


def _who(people, ident):
    if not ident:
        return "?"
    return people.alias.get(ident, "?")


def _recipients(people, entry, max_people):
    to = entry["to"]
    if not to:
        return ""
    if max_people and len(to) <= max_people:
        return ",".join(_who(people, t) for t in to)
    orgs = []
    for t in to:
        o = people.org_of(t) if t else "?"
        if o not in orgs:
            orgs.append(o)
    return ",".join(orgs)


def _entry_lines(entry, people, level, prev_date):
    """Lines for one entry. Returns (lines, date of the last line)."""
    lines = []
    d = _fmt_date(entry["dt"])
    if entry["dt"] is None:
        stamp = "(no date)"
    elif d == prev_date:
        stamp = entry["dt"].strftime("%H:%M")
    else:
        stamp = d + " " + entry["dt"].strftime("%H:%M")
    who = _who(people, entry["from"])
    rcpt = _recipients(people, entry, level["max_people"])
    head = stamp + " " + who + (">" + rcpt if rcpt else "") + ":"
    body = entry["text"]
    shown = list(entry["atts"])
    if entry.get("again"):
        shown.append(("+" if shown else "") + "%d as above" % entry["again"]
                     + (": " + entry["again_note"] if entry.get("again_note") else ""))
    if shown:
        body = (body + " " if body else "") + "[att: " + "; ".join(shown) + "]"
    lines.append(head + (" " + body if body else ""))
    last = d if entry["dt"] else None
    for r in entry["recovered"]:
        rd = _fmt_date(r["dt"])
        rstamp = (rd + " " + r["dt"].strftime("%H:%M")) if r["dt"] else "(no date)"
        lines.append("  \u21b3 " + rstamp + " " + _who(people, r["from"]) + ": " + r["text"])
        last = rd if r["dt"] else None
    return lines, last


def _thread_header(block, continued=False):
    """'## Title (3 emails + 1 recovered, 24-10-02 to 24-10-04)'; just '## Title' for a
    thread of one email (its date opens the next line). Recovered (\u21b3) emails
    count, and their dates widen the span: the oldest is often the request itself."""
    if continued:
        return "## " + block["title"] + " (continued)"
    n = block["emails"]
    rec = [r for e in block["entries"] for r in e.get("recovered", [])]
    if n == 1 and not rec:
        return "## " + block["title"]
    ds = list(block["dates"]) + [r["dt"] for r in rec if r.get("dt") is not None]
    count = "%d email%s" % (n, "" if n == 1 else "s")
    if rec:
        count += " + %d recovered" % len(rec)
    span = ""
    if ds:
        a, b = _fmt_date(min(ds)), _fmt_date(max(ds))
        span = ", " + (a if a == b else a + " to " + b)
    return "## %s (%s%s)" % (block["title"], count, span)


def _render_entries(block, entries, people, level, continued=False):
    lines = [_thread_header(block, continued)]
    prev = None
    for e in entries:
        el, prev = _entry_lines(e, people, level, prev)
        lines.extend(el)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Parts and headers

_PLURALS = {"duplicate copies": "duplicate copy", "meeting responses": "meeting response",
            "auto-replies": "auto-reply", "receipts": "receipt", "notifications": "notification",
            "thank-you/ack emails": "thank-you/ack email",
            "emails in threads without the focus keywords": "email in threads without the focus keywords"}


def _counted(n, plural):
    """'1 receipt', '3 receipts'."""
    return "%d %s" % (n, _PLURALS.get(plural, plural) if n == 1 else plural)


def _dropped_text(stats, noise_counts, level):
    bits = []
    if stats["duplicates"]:
        bits.append(_counted(stats["duplicates"], "duplicate copies"))
    for kind in ("meeting responses", "auto-replies", "receipts", "notifications"):
        if noise_counts.get(kind):
            bits.append(_counted(noise_counts[kind], kind))
    if stats["acks_dropped"]:
        bits.append(_counted(stats["acks_dropped"], "thank-you/ack emails"))
    if stats["filtered_out"]:
        bits.append(_counted(stats["filtered_out"], "emails in threads without the focus keywords"))
    return ", ".join(bits)


def _date_filter_text(project, outside_dates):
    """'Dates: only 2025-01-20 to 2025-01-31 - 8 email files outside these dates are not
    included' when the date filter left anything out, else ''."""
    if not outside_dates:
        return ""
    a = (project.get("date_from") or "").strip()
    b = (project.get("date_to") or "").strip()
    if a and b:
        span = a + " to " + b
    elif a:
        span = "from " + a
    elif b:
        span = "up to " + b
    else:
        span = "a date range"
    return "Dates: only %s - %d email file%s outside these dates %s not included" % (
        span, outside_dates, "" if outside_dates == 1 else "s", "is" if outside_dates == 1 else "are")


def _no_access_text(count):
    """'Not included: 40 emails in folders Squish could not open (no access)' when the
    engine left out emails it has no access to, else ''."""
    if not count:
        return ""
    return "Not included: %s in folders Squish could not open (no access)" % _count(count, "email")


def _unreadable_text(count):
    """'Not included: 3 email files Squish could not read (damaged or locked), so emails
    may be missing' when the engine couldn't read some email files, else ''."""
    if not count:
        return ""
    return ("Not included: %s Squish could not read (damaged or locked), so emails may be "
            "missing" % _count(count, "email file"))


def _legend_lines(people, idents, org_idents=(), bare_unknown=False, person_info=None):
    """People lines for the part's legend. Orgs whose people are shown only as an
    org code (recipient lists) still get a 'CODE = domain' line. `bare_unknown`: the
    part has a sender shown as a bare '?' (no name or address at all).
    `person_info`: a cached people.info (the legend is rebuilt many times while
    the parts are packed)."""
    info = person_info or people.info
    by_org = OrderedDict()
    order = [code for _, code in people.org_map]
    counts = Counter(info(i)[0] for i in idents)
    for i in org_idents:
        o = info(i)[0] if i else "?"
        if o not in counts:
            counts[o] += 0
    if bare_unknown and "?" not in counts:
        counts["?"] += 0
    orgs = sorted(counts, key=lambda o: (o not in order, order.index(o) if o in order else 0,
                                          o == "?", -counts[o], o))
    for o in orgs:
        by_org[o] = []
    domains = {}
    for i in idents:
        org, name, dom = info(i)
        by_org[org].append((people.alias.get(i, "?"), name))
        if dom:
            domains.setdefault(org, set()).add(dom)
    for i in org_idents:
        org, _, dom = info(i) if i else ("?", "", "")
        if org in by_org and dom:
            domains.setdefault(org, set()).add(dom)
    if not by_org:
        return []
    lines = ["People (ORG.Initials):"]
    for org, plist in by_org.items():
        if org == "?":
            label = "? = address unknown" + (" (a bare ? = no name or address)" if bare_unknown else "")
        else:
            conf = [d for d, c in people.org_map if c == org]
            doms = conf or sorted(_shorten_domains(domains.get(org, set())))
            label = org + " = " + ", ".join(doms)
        plist.sort(key=lambda p: p[0])
        if plist:
            lines.append("  " + label + ": " + ", ".join(a.split(".", 1)[1] + "=" + n for a, n in plist))
        else:
            lines.append("  " + label)
    return lines


def _shorten_domains(doms):
    """Collapse subdomains: {'a.x.com', 'x.com'} -> {'x.com'}."""
    out = set()
    for d in doms:
        if not any(d != o and d.endswith("." + o) for o in doms):
            out.add(d)
    return out


_HOW_TO_READ = [
    'How to read: "## subject (N emails, first to last date)" starts a thread (oldest first; no count for one email).'
    ' Each line is one email:',
    "  YY-MM-DD HH:MM FROM>TO: new text only (local time; date omitted when same as the line above).",
    "  TO = To recipients only (not Cc); an org code alone (e.g. SLR) = several people there.",
]

# Markers explained in a part's header only when the part uses them
_MARKERS = OrderedDict([
    ("ack", ": (ack)"),
    ("recovered", "\n  \u21b3 "),
    ("inline", "[inline replies: "),
    ("bullet", "\u2022"),
    ("link", "<link>"),
    ("notext", ": (no text)"),
    ("invite", ": (meeting invite)"),
    ("again", (" as above]", " as above: ")),
])


def _body_markers(text):
    """The set of _MARKERS keys that appear in some digest text (a marker may be
    one string or a tuple of alternatives)."""
    return set(k for k, m in _MARKERS.items() if any(x in text for x in (m if isinstance(m, tuple) else (m,))))


def _count(n, word):
    """'1 email', '2 emails'."""
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _header_text(info, part_no, part_count, part_emails, part_threads, people, idents, body=None,
                 org_idents=(), markers=None, part_span="", bare_unknown=False, person_info=None):
    """The self-contained header of one part. Explanations of markers such as "(ack)"
    are included only when the part uses them: those found in `body`, else those in
    `markers` (a set from _body_markers, for size estimates), else all of them.
    `part_span` is the part's own date range ('2024-08-22 to 2025-03-01');
    `person_info` a cached people.info (see _legend_lines)."""
    if body is not None:
        markers = _body_markers(body)
    elif markers is None:
        markers = set(_MARKERS)
    title = "SQUISH EMAIL DIGEST | " + info["name"]
    if part_count > 1:
        title += " | part %d of %d" % (part_no, part_count)
    lines = [title]
    cover = "Covers %s to %s | %s in %s" % (
        info["first"] or "?", info["last"] or "?", _count(info["emails"], "email"),
        _count(info["threads"], "thread"))
    if part_count > 1:
        cover += " (this part: %s, %s%s)" % (_count(part_emails, "email"), _count(part_threads, "thread"),
                                             ", " + part_span if part_span else "")
    lines.append(cover)
    src = ("Source: " + info["source"] + " | ") if info["source"] else ""
    lines.append(src + "squeeze: " + info["squeeze"] + " | made " + info["made"])
    if info["keywords"]:
        lines.append("Focus keywords: " + ", ".join(info["keywords"]))
    if info.get("date_filter"):
        lines.append(info["date_filter"])
    if info.get("no_access"):
        lines.append(info["no_access"])
    if info.get("unreadable"):
        lines.append(info["unreadable"])
    if info["dropped"]:
        lines.append("Dropped: " + info["dropped"])
    lines.extend(_HOW_TO_READ)
    note = SQUEEZE_LEVELS.get(info["squeeze"], {}).get("header_note")
    lines.append('  Quoted history, signatures and disclaimers are removed; "\u2026" marks trimmed text'
                 + (" (" + note + ")" if note else "") + ".")
    extra = ['  [att: ...] attachments.']
    if info["ack"] != "keep" and "ack" in markers:
        extra[0] += ' "(ack)" = short thanks/acknowledgement.'
    if info["recover"] and "recovered" in markers:
        extra[0] += ' "\u21b3" = an earlier email'
        extra.append("  recovered from inside a reply, forward or attachment because it was not filed on its own.")
    if "again" in markers:
        extra.append('  "N as above" in [att: ...] = N files whose names were listed earlier in the thread, sent again.')
    if "inline" in markers:
        extra.append('  "[inline replies: ...]" = answers the sender typed into the quoted email;'
                     ' re "first words\u2026": = the answer to the line that starts so.')
    marks = []
    if "bullet" in markers:
        marks.append('"\u2022" = list item or table cell')
    if "link" in markers:
        marks.append("<link> = a link was here")
    if "notext" in markers:
        marks.append("(no text) = nothing typed")
    if "invite" in markers:
        marks.append("(meeting invite) = an invitation with no message")
    if marks:
        extra.append("  " + "; ".join(marks) + ".")
    lines.extend(extra)
    lines.extend(_legend_lines(people, idents, org_idents, bare_unknown, person_info))
    return "\n".join(lines) + "\n"


def _chunk_marks(text, entries):
    """_body_markers of a rendered chunk, plus "bare" when one of its senders is shown
    as a bare '?' (no name or address), which the legend then explains."""
    marks = _body_markers("\n" + text)
    if any(not e["from"] or any(not r["from"] for r in e["recovered"]) for e in entries):
        marks.add("bare")
    return marks


def _split_parts(blocks, limit, info, people, check=None):
    """Pack thread blocks into parts without splitting threads (unless one thread
    alone is too big, which then continues under '## subject (continued)'). A
    thread counts in every part it appears in. `check()` is called between
    threads (and between the emails of an oversize thread); it raises
    DigestCancelled when the run is cancelled."""
    level = SQUEEZE_LEVELS[info["squeeze"]]
    known = {}

    def person_info(ident):
        """people.info, worked out once per person (People is final by now)."""
        if ident not in known:
            known[ident] = people.info(ident)
        return known[ident]

    # Pre-render each block once to know its size (and which markers it uses)
    rendered = []
    for b in blocks:
        rendered.append(_render_entries(b, b["entries"], people, level))

    parts = []

    def new_part():
        parts.append({"texts": [], "idents": set(), "org_idents": set(), "markers": set(), "emails": 0,
                      "threads": 0, "dates": [], "size": 0})
        return parts[-1]

    def header_size(idents, org_idents, markers):
        return len(_header_text(info, 99, 99, 99999, 99999, people, idents, None, org_idents, markers,
                                "2000-01-01 to 2000-12-31", "bare" in markers, person_info)) + 2

    cur = new_part()
    for b, text in zip(blocks, rendered):
        if check is not None:
            check()
        marks = _chunk_marks(text, b["entries"])
        if limit is None:
            _add_chunk(cur, text, b["idents"], b["org_idents"], b["emails"], 1, b["span_dates"], marks)
            continue
        need = cur["size"] + len(text) + 2 + header_size(cur["idents"] | b["idents"],
                                                         cur["org_idents"] | b["org_idents"], cur["markers"] | marks)
        if need <= limit:
            _add_chunk(cur, text, b["idents"], b["org_idents"], b["emails"], 1, b["span_dates"], marks)
            continue
        alone = len(text) + 2 + header_size(b["idents"], b["org_idents"], marks)
        if alone <= limit:
            cur = new_part() if cur["texts"] else cur
            _add_chunk(cur, text, b["idents"], b["org_idents"], b["emails"], 1, b["span_dates"], marks)
            continue
        # Oversize thread: fill entry by entry, continuing in new parts. Each entry
        # is rendered once and the chunk's size kept as a running total.
        if cur["texts"] and limit - (cur["size"] + header_size(cur["idents"], cur["org_idents"], cur["markers"])) \
                < limit * 0.25:
            cur = new_part()
        pending = list(b["entries"])
        continued = False
        while pending:
            take = []
            idents = set()
            orgs = set()
            head = _thread_header(b, continued)
            size = len(head)
            marks = _body_markers("\n" + head)
            prev = None
            while pending:
                if check is not None:
                    check()
                e = pending[0]
                lines, e_prev = _entry_lines(e, people, level, prev)
                e_text = "\n" + "\n".join(lines)
                cid = idents | e["shown"]
                corg = orgs | e["org_only"]
                cmarks = marks | _chunk_marks(e_text, [e])
                total = cur["size"] + size + len(e_text) + 2 + header_size(
                    cur["idents"] | cid, cur["org_idents"] | corg, cur["markers"] | cmarks)
                if total <= limit or (not take and not cur["texts"]):
                    take.append(e)
                    idents, orgs, marks, prev = cid, corg, cmarks, e_prev
                    size += len(e_text)
                    pending.pop(0)
                    continue
                break
            if take:
                t = _render_entries(b, take, people, level, continued)
                dates = _shown_dates(take)
                _add_chunk(cur, t, idents, orgs, len(take), 1, dates, _chunk_marks(t, take))
                continued = True
            if pending:
                cur = new_part()
    if not parts[0]["texts"] and len(parts) > 1:
        parts.pop(0)

    out = []
    n = len(parts)
    for i, p in enumerate(parts, 1):
        body = "\n\n".join(p["texts"])
        ds = p["dates"]
        first = min(ds).strftime("%Y-%m-%d") if ds else ""
        last = max(ds).strftime("%Y-%m-%d") if ds else ""
        span = (first if first == last else first + " to " + last) if ds else ""
        head = _header_text(info, i, n, p["emails"], p["threads"], people, p["idents"], body, p["org_idents"],
                            part_span=span, bare_unknown="bare" in p["markers"], person_info=person_info)
        text = head + "\n" + body + ("\n" if body else "")
        out.append({
            "text": text,
            "first_date": first,
            "last_date": last,
            "emails": p["emails"],
            "threads": p["threads"],
        })
    return out


def _add_chunk(part, text, idents, org_idents, emails, threads, dates, markers):
    part["texts"].append(text)
    part["markers"] |= markers
    part["idents"] |= set(i for i in idents if i)
    part["org_idents"] |= set(i for i in org_idents if i)
    part["emails"] += emails
    part["threads"] += threads
    part["dates"].extend(dates)
    part["size"] += len(text) + 2
