"""Run one project: find the email files, read them (using a cache), filter by
date, build the digest and write the output files.

    run_project(project, progress=None, cancel=None) -> RunResult

``progress(stage, done, total, message)`` is called from the worker thread
(stage is "scan", "read", "documents", "digest" or "write"; total is 0 when not
yet known). ``cancel`` is a threading.Event; a cancelled run writes no output
files and no run log (the read caches are still saved, so the next run is
quicker).

Documents (v1.1): Word, Excel, PowerPoint and PDF files attached to the emails
(read with the emails, and condensed by docs.py in the read threads, so their
bytes are never all held at once) and loose files in the project's documents
folder go into a separate documents digest (docdigest.py) next to the email
digest. Documents never stop a run.

Source folders are only ever read, never changed (the output folder may not be
the email folder or inside it). The previous digest files are only replaced by
a run that produced something worth having: if the email folder stops
responding, too many files can't be read, or no emails are left after the
filters, the old files are left exactly as they were.
"""

import gzip
import hashlib
import json
import os
import queue
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from . import cleaning, docs, paths, readers
from .digest import build_digest, DigestCancelled, PART_SIZES
from .docdigest import build_documents_digest
from .digest import _keyword_pattern   # focus keyword matching, the same for documents

CACHE_VERSION = 4          # bump when the EmailRecord format changes (4: documents' sha1)
READ_WORKERS = 6           # network drives are slow; read a few files at once
PROGRESS_INTERVAL = 0.05   # seconds between progress calls (at most ~20 a second)
EMAIL_EXTENSIONS = (".msg", ".eml")
OUTPUT_PREFIX = "Squish - "
MAX_FAILED_SHARE = 0.2     # stop (keep the old digest) if more than this share of files fail...
MIN_FAILED_TO_STOP = 5     # ...and at least this many
MANIFEST_VERSION = 1
PATH_BUDGET = 250          # longest digest path wanted (Windows Explorer stops at 260)
FOCUS_TEXT_MAX = 40        # keyword text shown in a focus run's file names
CHARS_PER_TOKEN = 3.5      # digests are dense with dates, times and codes, so they
                           # split into more tokens than ordinary prose (~4 chars)
CANCEL_CHECK_S = 0.5       # how often the read stage looks at Cancel while files are read
DOCS_CACHE_VERSION = 1     # bump when the DocText format changes
DOCS_UNUSED_DAYS = 30      # extracted documents not used for this long are dropped from the cache
EXTRACT_AT_ONCE = 2        # documents condensed at once (bounds memory; the work is CPU-bound)
KIND_EMAILS = "emails"
KIND_DOCUMENTS = "documents"
# Attachment types docs.py can't read that are still listed under "Other files"
# in the documents digest (images and the like are only listed in the email digest).
OTHER_DOC_EXT = (".doc", ".dot", ".dotm", ".xls", ".xlsb", ".xltx", ".xltm", ".ppt", ".pps",
                 ".pptm", ".potx", ".dwg", ".dxf", ".dwf", ".dgn", ".rvt", ".ifc", ".nwd",
                 ".nwc", ".mpp", ".vsd", ".vsdx", ".odt", ".ods", ".odp", ".kmz", ".kml")
# DocText statuses that mean the contents could not be read ("unsupported" is expected)
UNREAD_STATUSES = ("error", "too_big", "protected", "no_text")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# The ' (only <dates>)' tag a dated run adds to its file names (see date_label)
_DATES_TAG = (r" \(only (?:\d{4}-\d{2}-\d{2} to \d{4}-\d{2}-\d{2}|from \d{4}-\d{2}-\d{2}"
              r"|up to \d{4}-\d{2}-\d{2})\)")
# What follows 'Squish - <name> - ' in a digest file name
_OUTPUT_TAIL = re.compile(
    r"^(\d{4}-\d{2}-\d{2} to \d{4}-\d{2}-\d{2}|undated)(%s)?( \(focus[^()]*\))?"
    r"( \(part \d+ of \d+\))?\.txt$" % _DATES_TAG, re.I)
# The end of a dated and/or focus run's file name (not the all-dates digest)
_FILTER_TAG = re.compile(r"(%s| \(focus[^()]*\))( \(part \d+ of \d+\))?\.txt$" % _DATES_TAG,
                         re.I)
# ' - documents - <dates>' in a documents digest's file name
_DOCUMENTS_TAG = re.compile(r" - documents - (?=\d{4}-\d{2}-\d{2} to |undated)", re.I)
_NO_ACCESS = "no access to this folder"
_NOT_OPENED = "folder could not be opened"
_DOCS_FOLDER_MISSING = "documents folder not found"
_DOCS_FOLDER_IS_OUTPUT = "documents folder not read"


class SquishError(Exception):
    """A problem to show the user in plain English (no technical details needed).

    ``log_path`` is the run log written before the run stopped ('' if none), so
    the window's View run log can show which files or folders were the problem.
    """

    log_path = ""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _is_cancelled(cancel):
    return cancel is not None and cancel.is_set()


class _Progress(object):
    """Passes progress on to the caller, at most ~20 times a second per stage."""

    def __init__(self, callback):
        self.callback = callback
        self.stage = None
        self.last = 0.0

    def __call__(self, stage, done, total, message=""):
        if self.callback is None:
            return
        now = time.monotonic()
        finished = bool(total) and done >= total
        if stage == self.stage and not finished and now - self.last < PROGRESS_INTERVAL:
            return
        self.stage = stage
        self.last = now
        try:
            self.callback(stage, done, total, message)
        except Exception:
            pass  # a broken progress display must never stop the run


def _error_text(exc):
    text = str(exc).strip()
    name = type(exc).__name__
    if not text:
        return name
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return text if name in ("MsgFileError", "ValueError") else "%s: %s" % (name, text)


def project_name(project):
    return (project.get("name") or "").strip() or "Project"


def output_folder_for(project):
    folder = paths.clean_folder_text(project.get("output_folder"))
    return folder or str(paths.default_output_folder(project_name(project)))


def _check_date(value, label):
    value = (value or "").strip()
    if value and not _DATE_RE.match(value):
        raise SquishError("The %s date must look like 2025-01-31 (year-month-day)." % label)
    if value:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise SquishError("The %s date %s isn't a real date." % (label, value))
    return value


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------

def is_email_file(name):
    """True for .msg/.eml files, but not Office "~$" temporary files."""
    return name.lower().endswith(EMAIL_EXTENSIONS) and not name.startswith("~$")


def _is_no_access(exc):
    """True when Squish really isn't allowed to open a folder.

    On Windows, Python also reports a file being locked or shared (and some
    other passing problems) as PermissionError; only "access denied" (5) and
    "network access denied" (65) count here. Anything else is treated like any
    other folder that could not be opened, which stops a run that needs it.
    """
    return isinstance(exc, PermissionError) and getattr(exc, "winerror", None) in (None, 5, 65)


def is_folder_problem(message):
    """True for a ``failed`` entry that is a folder Squish could not open
    (not an email file it could not read)."""
    return (message or "").startswith((_NO_ACCESS, _NOT_OPENED))


def is_doc_folder_problem(message):
    """True for a ``doc_problems`` entry that is a folder (the documents folder,
    or a folder inside it), not a document."""
    return (message or "").startswith((_NO_ACCESS, _NOT_OPENED, _DOCS_FOLDER_MISSING,
                                       _DOCS_FOLDER_IS_OUTPUT))


def _same_or_inside(path, folder):
    path, folder = os.path.normcase(path), os.path.normcase(folder)
    try:
        return os.path.commonpath([path, folder]) == os.path.commonpath([folder])
    except ValueError:
        return False  # e.g. on different drives


def output_inside_source(source, out_dir, resolve_links=True):
    """True when the output folder is the email folder or a folder inside it.

    Squish never writes into the email folders. Both folders are compared as
    typed and (unless ``resolve_links`` is False, which keeps it quick enough
    to check while someone types) with links / mapped drives resolved.
    """
    source = paths.clean_folder_text(source)
    out_dir = paths.clean_folder_text(out_dir)
    if not source or not out_dir:
        return False
    if _same_or_inside(os.path.abspath(out_dir), os.path.abspath(source)):
        return True
    if not resolve_links:
        return False
    try:
        return _same_or_inside(os.path.realpath(out_dir), os.path.realpath(source))
    except (OSError, ValueError):
        return False


def check_source_folder(source):
    """Raise SquishError with a plain-English message if the folder can't be used."""
    source = paths.clean_folder_text(source)
    if not source:
        raise SquishError("This project has no email folder yet. "
                          "Choose the folder that holds the project's emails.")
    folder = paths.long_path(source)
    if not os.path.isdir(folder):
        if os.path.exists(folder):
            raise SquishError("The email folder is a file, not a folder:\n%s" % source)
        raise SquishError(
            "Can't find the email folder:\n%s\n\n"
            "If it is on a network drive (like H:), check that you are connected to the "
            "office network or VPN and that the drive is available in File Explorer, "
            "then try again." % source)
    try:
        with os.scandir(folder) as entries:
            next(iter(entries), None)
    except OSError as exc:
        raise SquishError(
            "Squish can't open the email folder:\n%s\n\n%s\n\n"
            "Check that you have access to it (and, for a network drive, that you are "
            "connected to the office network or VPN)." % (source, _error_text(exc)))


_OUTPUT_FOLDER_ADVICE = ("Choose another folder with Browse..., or clear the 'Save digests "
                         "to' box to use the default Documents\\Squish\\<project> folder.")


def check_output_folder(out_dir):
    """Raise SquishError with a plain-English message if Squish can't save
    into ``out_dir`` (it is created if it doesn't exist yet).

    Checked before the emails are read, so a mistyped or disconnected output
    folder is found in seconds rather than after the whole read stage. Never
    call this for a folder inside the email folder (run_project checks that
    first): it writes and removes a small test file."""
    folder = paths.long_path(out_dir)
    test_file = os.path.join(folder, ".squish_test_%s.tmp" % uuid.uuid4().hex[:12])
    try:
        os.makedirs(folder, exist_ok=True)
        with open(test_file, "wb") as fh:
            fh.write(b"squish")
    except OSError as exc:
        raise SquishError("Squish can't save into this folder:\n%s\n\n(%s)\n\n%s"
                          % (out_dir, _error_text(exc), _OUTPUT_FOLDER_ADVICE))
    try:
        os.remove(test_file)
    except OSError:
        pass   # saving works, which is what matters (e.g. a virus scanner holds it)


def scan_folder(source, include_subfolders=True, cancel=None, progress=None, errors=None,
                stats=None):
    """List the email files in ``source`` (display paths, sorted).

    Returns None if cancelled. Subfolders that can't be opened are skipped and
    added to ``errors`` (a list) as [folder, message]. If ``stats`` (a dict) is
    given, it is filled with {path: (mtime, size)} from the folder listing, so
    unchanged files can be taken from the cache without asking the file server
    about each one again.
    """
    root = paths.long_path(source)
    found = []

    def folder_error(folder, exc):
        if errors is not None:
            what = _NO_ACCESS if _is_no_access(exc) else _NOT_OPENED
            errors.append([paths.short_path(folder), "%s: %s" % (what, _error_text(exc))])

    def add(entry):
        path = paths.short_path(entry.path)
        found.append(path)
        if stats is not None:
            try:
                st = entry.stat()
                stats[path] = (st.st_mtime, st.st_size)
            except OSError:
                pass  # the file is looked at again when it is read

    if not include_subfolders:
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    try:
                        if is_email_file(entry.name) and entry.is_file():
                            add(entry)
                    except OSError:
                        continue
        except OSError as exc:
            folder_error(root, exc)
        found.sort(key=lambda p: p.lower())
        return found

    pending = [root]
    while pending:
        if _is_cancelled(cancel):
            return None
        folder = pending.pop()
        subfolders = []
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            subfolders.append(entry.path)
                        elif is_email_file(entry.name) and entry.is_file():
                            add(entry)
                    except OSError:
                        continue
        except OSError as exc:
            folder_error(folder, exc)
            continue
        # pop() takes from the end, so add the subfolders in reverse name order.
        subfolders.sort(key=lambda p: p.lower(), reverse=True)
        pending.extend(subfolders)
        if progress:
            progress("scan", len(found), 0, "Looking for emails... %d found" % len(found))
    found.sort(key=lambda p: p.lower())
    return found


# --------------------------------------------------------------------------
# Cache: <cache>/<project key>.json.gz = {"version", "files": {path: {mtime, size, record}}}
# --------------------------------------------------------------------------

def _project_key(project):
    key = re.sub(r"[^A-Za-z0-9_-]+", "_", str(project.get("id") or "")).strip("_")
    if not key:
        key = "project_" + re.sub(r"[^A-Za-z0-9_-]+", "_", paths.safe_filename(project_name(project)))
    return key[:100]


def cache_path(project):
    return paths.cache_dir() / ("%s.json.gz" % _project_key(project))


def load_cache(path):
    """Cached entries by source path; {} if missing, out of date or damaged."""
    try:
        with gzip.open(str(path), "rt", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {}
    files = data.get("files")
    return files if isinstance(files, dict) else {}


def save_cache(path, entries):
    """Write the cache atomically (temp file + replace). Returns True if saved.

    Written as plain ASCII (ensure_ascii), so odd file names (which Python may
    hold as lone surrogates) are saved and read back unchanged.
    """
    return _save_json_gz(path, {"version": CACHE_VERSION, "files": entries})


def _save_json_gz(path, data):
    """Write ``data`` as gzipped ASCII JSON via a temp file. Returns True if saved."""
    path = str(path)
    tmp = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
    try:
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as fh:
            json.dump(data, fh, ensure_ascii=True, separators=(",", ":"))
        os.replace(tmp, path)
    except (OSError, ValueError, TypeError):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    _remove_stale_temps(path)
    return True


def _remove_stale_temps(path, older_than_s=3600):
    """Delete '<cache>.tmp-*' files left behind by a save that never finished
    (Squish was closed or crashed while saving). Recent ones are left alone in
    case another Squish is saving right now."""
    folder, base = os.path.split(path)
    try:
        names = os.listdir(folder or ".")
    except OSError:
        return
    now = time.time()
    for name in names:
        if not name.startswith(base + ".tmp-"):
            continue
        full = os.path.join(folder, name)
        try:
            if now - os.path.getmtime(full) > older_than_s:
                os.remove(full)
        except OSError:
            pass


def _usable_cache_entry(entry, mtime, size):
    return (isinstance(entry, dict)
            and entry.get("mtime") == mtime
            and entry.get("size") == size
            and isinstance(entry.get("record"), dict)
            and "date" in entry["record"])


def _read_one(path, cached, known_stat=None, store=None, cancel=None):
    """Worker thread: (path, cache entry or None, error text or None, came from
    cache, error was the file being out of reach).

    ``known_stat`` is (mtime, size) from the folder listing, if known; then an
    unchanged file costs no extra trip to the file server. "Out of reach" means
    the file couldn't be opened or read (OSError, e.g. the network dropped), as
    opposed to a file that was read but isn't a proper email.

    ``store`` (a _DocStore) is given when documents attached to the emails are
    wanted: the email is then read with its documents' bytes, which are
    condensed here (in this thread) and dropped. A cached email read without
    its documents (or whose documents are no longer in the store) is read again.
    """
    if known_stat is not None:
        mtime, size = known_stat
    else:
        try:
            st = os.stat(paths.long_path(path))
        except OSError as exc:
            return path, None, _error_text(exc), False, True
        mtime, size = st.st_mtime, st.st_size
    if _usable_cache_entry(cached, mtime, size) and (store is None or _docs_ready(cached, store)):
        return path, cached, None, True, False
    try:
        if store is None:
            record = readers.read_email(path)
        else:
            record = readers.read_email(path, want_docs=True)
    except Exception as exc:
        return path, None, _error_text(exc), False, isinstance(exc, OSError)
    record["path"] = path
    entry = {"mtime": mtime, "size": size, "record": record, "docs": False}
    if store is not None:
        try:
            entry["docs"] = _extract_attachments(record, store, cancel)
        except Exception:
            entry["docs"] = False    # documents never stop a run; tried again next time
        _drop_doc_bytes(record)
    return path, entry, None, False, False


def _merged_cache(old_cache, new_cache, files, scan_complete):
    """The cache to save: this run's entries plus older ones still worth keeping.

    Entries for files that failed this time are kept (they are only used again
    if the file is unchanged). Entries for files that are gone are dropped, but
    only after a complete scan - a folder that couldn't be opened is not gone.
    """
    if scan_complete:
        scanned = set(files)
        merged = dict((p, e) for p, e in old_cache.items() if p in scanned)
    else:
        merged = dict(old_cache)
    merged.update(new_cache)
    return merged


# --------------------------------------------------------------------------
# Documents cache: <cache>/<project key>.docs.json.gz =
#   {"version", "docs": {sha1: {"doc": DocText, "used": "YYYY-MM-DD"}},
#    "files": {loose file path: {"size", "mtime", "sha1"}}}
# --------------------------------------------------------------------------

def docs_cache_path(project):
    return paths.cache_dir() / ("%s.docs.json.gz" % _project_key(project))


def load_docs_cache(path):
    """(docs by sha1, loose files by path) from the documents cache; empty if
    missing, out of date or damaged."""
    try:
        with gzip.open(str(path), "rt", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}, {}
    if not isinstance(data, dict) or data.get("version") != DOCS_CACHE_VERSION:
        return {}, {}
    found = data.get("docs") if isinstance(data.get("docs"), dict) else {}
    files = data.get("files") if isinstance(data.get("files"), dict) else {}
    kept = dict((k, v) for k, v in found.items()
                if isinstance(v, dict) and isinstance(v.get("doc"), dict) and "status" in v["doc"])
    files = dict((k, v) for k, v in files.items() if isinstance(v, dict) and v.get("sha1"))
    return kept, files


def save_docs_cache(path, found, files, today=None):
    """Save the documents cache (atomically), leaving out documents not used for
    DOCS_UNUSED_DAYS days and loose files whose document is gone. Returns True if saved."""
    today = today or date.today()
    cutoff = (today - timedelta(days=DOCS_UNUSED_DAYS)).isoformat()
    found = dict((k, v) for k, v in found.items() if str(v.get("used") or "") >= cutoff)
    files = dict((k, v) for k, v in files.items() if v.get("sha1") in found)
    return _save_json_gz(path, {"version": DOCS_CACHE_VERSION, "docs": found, "files": files})


class _DocStore(object):
    """The documents condensed so far, by sha1 of their bytes: loaded from the
    documents cache, shared by the read threads (hence the lock), saved at the
    end. Also remembers the sha1 of each loose file by path, size and time."""

    def __init__(self, path):
        self.path = path
        self.docs, self.files = load_docs_cache(path)
        self.lock = threading.Lock()
        self.busy = set()            # sha1s being condensed right now
        self.slots = threading.BoundedSemaphore(EXTRACT_AT_ONCE)
        self.changed = False
        self.extracted = 0           # documents condensed this run (not from the cache)
        self.today = date.today().isoformat()

    def has(self, sha1):
        with self.lock:
            return sha1 in self.docs

    def get(self, sha1):
        with self.lock:
            entry = self.docs.get(sha1)
        return entry["doc"] if entry else None

    def claim(self, sha1):
        """True if the caller should condense this document: nobody has, or is doing, it."""
        with self.lock:
            if sha1 in self.docs or sha1 in self.busy:
                return False
            self.busy.add(sha1)
            return True

    def put(self, sha1, doc):
        """Store a condensed document (None: it wasn't done) and release the claim."""
        with self.lock:
            self.busy.discard(sha1)
            if doc is not None:
                self.docs[sha1] = {"doc": doc, "used": self.today}
                self.extracted += 1
                self.changed = True

    def mark_used(self, sha1s):
        """Mark documents as used today, so the cache keeps them."""
        with self.lock:
            for sha1 in sha1s:
                entry = self.docs.get(sha1)
                if entry is not None and entry.get("used") != self.today:
                    entry["used"] = self.today
                    self.changed = True

    def file_sha1(self, path, size, mtime):
        """The sha1 of an unchanged loose file seen before, else ''."""
        with self.lock:
            entry = self.files.get(path)
        if isinstance(entry, dict) and entry.get("size") == size and entry.get("mtime") == mtime:
            return str(entry.get("sha1") or "")
        return ""

    def remember_file(self, path, size, mtime, sha1):
        with self.lock:
            self.files[path] = {"size": size, "mtime": mtime, "sha1": sha1}
            self.changed = True

    def save(self):
        """Save the cache if anything changed. Returns False if it couldn't be saved."""
        if not self.changed:
            return True
        with self.lock:
            found, files = dict(self.docs), dict(self.files)
        if save_docs_cache(self.path, found, files):
            self.changed = False
            return True
        return False


def _doc_without_contents(name, status="", note=""):
    """A DocText for a document whose contents were not read: a type docs.py
    can't read (status "unsupported", from docs.extract), or ``status``/``note``
    (e.g. "too_big") for one that was not read for another reason."""
    doc = docs.extract(name)          # with no data or path this only names the kind
    if status and doc.get("status") != "unsupported":
        doc["status"], doc["note"] = status, note
    return doc


def _too_big_note():
    return "larger than %d MB, not read" % (docs.DOC_MAX_BYTES // (1024 * 1024))


def _extract_document(name, data, store, cancel):
    """docs.extract on ``data``, at most EXTRACT_AT_ONCE at a time; None when
    the run was cancelled first."""
    while not store.slots.acquire(timeout=0.2):
        if _is_cancelled(cancel):
            return None
    try:
        if _is_cancelled(cancel):
            return None
        try:
            return docs.extract(name, data=data)
        except Exception as exc:   # docs.extract never raises; a document must never stop a run
            return _doc_without_contents(name, "error", "could not read this file (%s)"
                                         % type(exc).__name__)
    finally:
        store.slots.release()


def _extract_attachments(record, store, cancel):
    """Read-thread work: condense the documents attached to a freshly read email
    (their bytes are in "_data", see readers.read_email) and drop the bytes.
    A document already in the store (or being done by another thread) is not
    done again. Returns False if the run was cancelled before all were done."""
    complete = True
    for att in record.get("attachments") or []:
        data = att.pop("_data", None)
        sha1 = att.get("sha1")
        if data is None or not sha1 or not complete or not store.claim(sha1):
            continue
        doc = _extract_document(att.get("name") or "", data, store, cancel)
        store.put(sha1, doc)
        complete = doc is not None
    return complete


def _drop_doc_bytes(record):
    """Make sure no attachment bytes are left in a record (it is cached as JSON)."""
    for att in record.get("attachments") or []:
        att.pop("_data", None)


def _docs_ready(entry, store):
    """True when a cached email was read with its documents and all of them are in the store."""
    if not entry.get("docs"):
        return False
    for att in entry["record"].get("attachments") or []:
        if isinstance(att, dict) and att.get("sha1") and not store.has(att["sha1"]):
            return False
    return True


# --------------------------------------------------------------------------
# Documents folder (loose files)
# --------------------------------------------------------------------------

_HIDDEN_NAMES = ("thumbs.db", "desktop.ini", ".ds_store")
_HIDDEN_OR_SYSTEM = 0x2 | 0x4      # Windows FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM


def _is_hidden(entry):
    """Hidden and system files and folders (and '~$' Office temp files) are skipped."""
    name = entry.name
    if name.startswith((".", "~$")) or name.lower() in _HIDDEN_NAMES:
        return True
    if os.name != "nt":
        return False      # elsewhere a leading dot is what makes a file hidden
    try:
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & _HIDDEN_OR_SYSTEM)


def scan_documents(folder, include_subfolders=True, skip_folder="", cancel=None, progress=None,
                   errors=None):
    """List the loose documents in ``folder``: [(display path, mtime, size)], by path.

    Skips email files (they are read as emails), Squish digest files, Office
    '~$' temp files, hidden and system files and folders, and ``skip_folder``
    (the output folder) with everything in it. Folders that can't be opened are
    added to ``errors`` as [folder, message]. Returns None if cancelled.
    """
    skip = os.path.normcase(os.path.abspath(skip_folder)) if skip_folder else ""
    found = []
    pending = [paths.long_path(folder)]
    while pending:
        if _is_cancelled(cancel):
            return None
        current = pending.pop()
        subfolders = []
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if _is_hidden(entry):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            shown = paths.short_path(entry.path)
                            if include_subfolders and not (skip and _same_or_inside(
                                    os.path.abspath(shown), skip)):
                                subfolders.append(entry.path)
                        elif entry.is_file() and not is_email_file(entry.name) \
                                and not is_digest_file_name(entry.name):
                            st = entry.stat()
                            found.append((paths.short_path(entry.path), st.st_mtime, st.st_size))
                    except OSError:
                        continue
        except OSError as exc:
            if errors is not None:
                what = _NO_ACCESS if _is_no_access(exc) else _NOT_OPENED
                errors.append([paths.short_path(current), "%s: %s" % (what, _error_text(exc))])
            continue
        subfolders.sort(key=lambda p: p.lower(), reverse=True)
        pending.extend(subfolders)
        if progress:
            progress("scan", len(found), 0, "Looking for documents... %d found" % len(found))
    found.sort(key=lambda item: item[0].lower())
    return found


def _loose_document(item, store, cancel):
    """Documents-thread work for one loose file: (item, key, sha1, DocText or
    None if cancelled). Unchanged files and identical contents come from the
    store; only supported types up to docs.DOC_MAX_BYTES are read."""
    path, mtime, size = item
    name = os.path.basename(path)
    if not docs.is_supported(name):
        return item, _name_key(name, size), "", _doc_without_contents(name)
    if size > docs.DOC_MAX_BYTES:
        return item, _name_key(name, size), "", _doc_without_contents(name, "too_big",
                                                                       _too_big_note())
    sha1 = store.file_sha1(path, size, mtime)
    doc = store.get(sha1) if sha1 else None
    if doc is not None:
        return item, sha1, sha1, doc
    try:
        with open(paths.long_path(path), "rb") as fh:
            data = fh.read(docs.DOC_MAX_BYTES + 1)
    except OSError as exc:
        return item, _name_key(name, size), "", _doc_without_contents(
            name, "error", "could not be opened (%s)" % _error_text(exc))
    sha1 = hashlib.sha1(data).hexdigest()
    store.remember_file(path, size, mtime, sha1)
    doc = store.get(sha1)
    if doc is None:
        doc = _extract_document(name, data, store, cancel)
        if doc is not None:
            store.put(sha1, doc)
    return item, sha1, sha1, doc


def _name_key(name, size):
    """The key of a document whose bytes were not read: its name and size."""
    return "name:%s|%s" % ((name or "").lower(), size if size is not None else "?")


# --------------------------------------------------------------------------
# Is this run complete enough to replace the previous digest?
# --------------------------------------------------------------------------

def _under(path, folder):
    folder = os.path.normcase(folder).rstrip("\\/")
    return bool(folder) and os.path.normcase(path).startswith(folder + os.sep)


def _incomplete_reason(source, files, records, scan_errors, failed, out_of_reach, old_cache):
    """Why this run must not replace the previous digest ('' if it may).

    ``out_of_reach`` counts the files that couldn't be opened or read (not the
    ones that were read but aren't proper emails: those fail every time, so
    they must not block the run for good).
    """
    if not scan_errors and not failed:
        return ""
    try:
        check_source_folder(source)
    except SquishError:
        return "the email folder stopped responding while the emails were being read"
    for folder, message in scan_errors:
        if message.startswith(_NO_ACCESS):
            continue  # no permission: trying again won't change that, so don't block
        had = sum(1 for p in old_cache if _under(p, folder))
        if had:
            return ("the folder %s could not be opened, and it held %d email%s last time"
                    % (folder, had, "" if had == 1 else "s"))
    if not records:
        return "none of the %d email files could be read" % len(files)
    if out_of_reach >= MIN_FAILED_TO_STOP and out_of_reach > MAX_FAILED_SHARE * len(files):
        return "%d of the %d email files could not be opened" % (out_of_reach, len(files))
    return ""


def _no_access_losses(scan_errors, old_cache):
    """[(scan error entry, emails it held last time)] for the folders Squish has
    no access to that held emails last time (their emails are left out)."""
    losses = []
    for item in scan_errors:
        if item[1].startswith(_NO_ACCESS):
            had = sum(1 for p in old_cache if _under(p, item[0]))
            if had:
                losses.append((item, had))
    return losses


def _plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _stopped_message(reason, had_previous=True, damaged_only=False):
    """The message for a run that stopped instead of writing an incomplete digest.

    The first line is always 'Squish stopped because <reason>.' (the window shows
    only that line). ``had_previous``: an earlier run read this project's emails,
    so its digest files are still there. ``damaged_only``: every problem file was
    opened but isn't a readable email, so the network advice would not help."""
    if had_previous:
        kept = "Nothing was changed - your previous digest files are still there."
    else:
        kept = "No digest was written."
    if damaged_only:
        advice = ("These files look damaged or aren't Outlook emails - see View run log for "
                  "details.")
    else:
        advice = ("If the emails are on a network drive, check the office network or VPN "
                  "connection and try again (emails already read are remembered, so it will "
                  "be quicker).")
    return "Squish stopped because %s.\n\n%s %s" % (reason, kept, advice)


# --------------------------------------------------------------------------
# Dates and file names
# --------------------------------------------------------------------------

def email_local_date(iso_text):
    """'YYYY-MM-DD' of a record's date as written (records are in local time); '' if unknown."""
    text = (iso_text or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text[:10] if _DATE_RE.match(text[:10]) else ""


def in_date_range(record, date_from, date_to):
    """Inclusive local-date filter; undated emails are always kept."""
    if not date_from and not date_to:
        return True
    day = email_local_date(record.get("date", ""))
    if not day:
        return True
    if date_from and day < date_from:
        return False
    if date_to and day > date_to:
        return False
    return True


def focus_label(keywords):
    """Short file-name text for the focus keywords ('' when there are none).

    Keywords longer than FOCUS_TEXT_MAX characters are cut and given a
    6-character code made from all of them, so two keyword lists that start the
    same way never share file names."""
    words = cleaning.keyword_list(keywords)
    if not words:
        return ""
    joined = ", ".join(words)
    text = paths.safe_filename(re.sub(r"[()]+", "", joined), "")
    if len(text) > FOCUS_TEXT_MAX:
        # The code is made from the whole list (not the cut, file-safe text).
        code = hashlib.sha1(joined.encode("utf-8", "surrogatepass")).hexdigest()[:6]
        text = "%s ~%s" % (text[:FOCUS_TEXT_MAX - 8].rstrip(" .,"), code)
    text = text.rstrip(" .,")
    return "focus " + text if text else "focus"


def date_label(date_from, date_to):
    """File-name text for the date filter ('' when there is none):
    'only 2025-01-01 to 2025-01-31', 'only from 2025-01-15' or 'only up to 2025-01-31'.
    It goes in brackets after the dates, so a dated run is never taken for the
    full digest (and never overwrites it)."""
    if date_from and date_to:
        return "only %s to %s" % (date_from, date_to)
    if date_from:
        return "only from %s" % date_from
    if date_to:
        return "only up to %s" % date_to
    return ""


def _part_span(part):
    first, last = part.get("first_date") or "", part.get("last_date") or ""
    if first and last:
        return "%s to %s" % (first, last)
    return "undated"


def output_filenames(name, parts, focus="", folder="", dates="", kind=KIND_EMAILS):
    """File names for the digest parts, e.g. 'Squish - X - 2024-01-02 to 2024-09-30 (part 1 of 2).txt'.

    Each part is named with its own date range. The project name is shortened
    by paths.output_name when it is long. ``dates`` (from date_label) and
    ``focus`` (from focus_label) are added in brackets after the dates, so a
    dated run or a run with focus keywords never overwrites the project's full
    digest: '... 2025-01-02 to 2025-01-30 (only 2025-01-01 to 2025-01-31) (focus pump).txt'.
    When ``folder`` (the output folder) is given and a path would be longer
    than PATH_BUDGET characters, the focus text is shortened (see _short_focus)
    so the files can still be dragged from Explorer. A documents digest
    (``kind`` "documents") is 'Squish - X - documents - <dates>....txt'.
    """
    safe = paths.output_name(name)
    if kind == KIND_DOCUMENTS:
        safe += " - documents"
    dates_tag = " (%s)" % dates if dates else ""

    def names_with(focus_text):
        tag = dates_tag + (" (%s)" % focus_text if focus_text else "")
        if len(parts) == 1:
            return ["%s%s - %s%s.txt" % (OUTPUT_PREFIX, safe, _part_span(parts[0]), tag)]
        n = len(parts)
        return ["%s%s - %s%s (part %d of %d).txt" % (OUTPUT_PREFIX, safe, _part_span(p), tag, i + 1, n)
                for i, p in enumerate(parts)]

    names = names_with(focus)
    if focus and folder:
        over = len(str(folder)) + 1 + max(len(n) for n in names) - PATH_BUDGET
        if over > 0:
            names = names_with(_short_focus(focus, len(focus) - over))
    return names


def _short_focus(focus, room):
    """The focus text cut at a word to fit ``room`` characters, plus ' ~' and a
    6-character code made from the whole text, so different keyword sets still
    get different files: 'focus culvert, headwall ~1a2b3c'."""
    code = " ~" + hashlib.sha1(focus.lower().encode("utf-8", "surrogatepass")).hexdigest()[:6]
    keep = max(room - len(code), len("focus"))
    text = focus[:keep]
    if len(focus) > keep and focus[keep] != " " and " " in text:
        text = text.rsplit(" ", 1)[0]       # don't end in the middle of a word
    return text.rstrip(" ,.") + code


def is_old_output(filename, name, kind=KIND_EMAILS):
    """True for a digest file this project wrote earlier ('Squish - <name> - <dates>....txt',
    or for ``kind`` "documents" 'Squish - <name> - documents - <dates>....txt').

    <name> is the project name as output_filenames writes it, or (for files
    written before long names were shortened) the whole safe file name."""
    middle = " - documents - " if kind == KIND_DOCUMENTS else " - "
    for shown in (paths.output_name(name), paths.safe_filename(name, "Project")):
        prefix = "%s%s%s" % (OUTPUT_PREFIX, shown, middle)
        if filename.lower().startswith(prefix.lower()) and _OUTPUT_TAIL.match(filename[len(prefix):]):
            return True
    return False


def is_digest_file_name(filename):
    """True for any Squish digest file name ('Squish - <any name> - <dates>....txt')."""
    if not filename.lower().startswith(OUTPUT_PREFIX.lower()):
        return False
    rest = filename[len(OUTPUT_PREFIX):]
    at = rest.find(" - ")
    while at > 0:
        if _OUTPUT_TAIL.match(rest[at + 3:]):
            return True
        at = rest.find(" - ", at + 1)
    return False


def digest_file_kind(filename):
    """"documents" for a documents digest file name, "emails" for an email digest
    file name, "" for anything else."""
    if not is_digest_file_name(filename):
        return ""
    return KIND_DOCUMENTS if _DOCUMENTS_TAG.search(filename) else KIND_EMAILS


def filter_key(date_from, date_to, keywords):
    """'' for a run over all dates without focus keywords, else a text naming
    the filters. Runs with the same key replace each other's digest files."""
    words = sorted(set(cleaning.keyword_list(keywords)))
    if not (date_from or date_to or words):
        return ""
    return "from %s | to %s | focus %s" % (date_from or "start", date_to or "end",
                                           ", ".join(words) or "-")


# --------------------------------------------------------------------------
# Writing files
# --------------------------------------------------------------------------

def _write_text(path, text):
    """Write UTF-8 text with \\n line endings (unsavable characters become '?')."""
    with open(paths.long_path(path), "w", encoding="utf-8", errors="replace",
              newline="\n") as fh:
        fh.write(text)


def _remove_quietly(path_list):
    for path in path_list:
        try:
            os.remove(paths.long_path(path))
        except OSError:
            pass


def _write_text_atomic(path, text):
    """Write a file via a temp file, so a failure never leaves half a file."""
    tmp = "%s.squish-tmp" % path
    try:
        _write_text(tmp, text)
        os.replace(paths.long_path(tmp), paths.long_path(path))
    except BaseException:
        _remove_quietly([tmp])
        raise


def _put_back(placed, moved, tmps):
    """Undo a half-finished write: remove the new files, restore the old ones.
    Returns True if every old file is back."""
    _remove_quietly(placed + tmps)
    ok = True
    for target, backup in moved:
        try:
            os.replace(paths.long_path(backup), paths.long_path(target))
        except OSError:
            ok = False
    return ok


def _write_parts(out_dir, parts, names, report, cancel=None, kinds=None):
    """Write the digest parts as ``names`` in ``out_dir``; returns the file info list.

    All parts are written to temp files first, then the files they replace are
    moved aside, then the new ones are put in place. If anything fails, the
    folder is put back the way it was and SquishError is raised. Cancel is
    checked only while the temp files are written (returns None, nothing
    changed), so the swap itself is all or nothing. ``kinds`` gives each
    part's kind ("emails" or "documents"; all "emails" if not given), so the
    email and documents digests are written in one all-or-nothing swap.
    """
    if not parts:
        return []
    try:
        os.makedirs(paths.long_path(out_dir), exist_ok=True)
    except OSError as exc:
        raise SquishError("Can't create the output folder:\n%s\n\n%s\n\n%s"
                          % (out_dir, _error_text(exc), _OUTPUT_FOLDER_ADVICE))
    targets = [os.path.join(out_dir, n) for n in names]
    tmps = [t + ".squish-tmp" for t in targets]
    backups = [t + ".squish-old" for t in targets]
    unchanged = "Your previous digest files have not been changed."

    # (a) Write every part to a temp file.
    for i, (part, filename, tmp) in enumerate(zip(parts, names, tmps)):
        if _is_cancelled(cancel):
            _remove_quietly(tmps)
            return None
        report("write", i, len(parts), "Saving %s" % filename)
        try:
            _write_text(tmp, part.get("text") or "")
        except OSError as exc:
            _remove_quietly(tmps)
            raise SquishError("Couldn't save %s\nin %s\n\n%s\n\n%s"
                              % (filename, out_dir, _error_text(exc), unchanged))

    if _is_cancelled(cancel):
        _remove_quietly(tmps)
        return None

    # (b) Move aside the files that are about to be replaced. A file that is
    # open in another program usually can't be moved, so this is where that
    # shows up - before anything has changed.
    moved = []
    for filename, target, backup in zip(names, targets, backups):
        if not os.path.exists(paths.long_path(target)):
            continue
        try:
            os.replace(paths.long_path(target), paths.long_path(backup))
        except OSError as exc:
            restored = _put_back([], moved, tmps)
            raise SquishError("Couldn't replace %s\nin %s\n\n%s\n\nIs the file open in another "
                              "program? %s" % (filename, out_dir, _error_text(exc),
                                               unchanged if restored else ""))
        moved.append((target, backup))

    # (c) Put the new parts in place.
    placed = []
    for filename, tmp, target in zip(names, tmps, targets):
        try:
            os.replace(paths.long_path(tmp), paths.long_path(target))
        except OSError as exc:
            if _put_back(placed, moved, tmps):
                tail = unchanged
            else:
                tail = ("The folder may now hold a mix of old and new digest files - "
                        "click Squish! again.")
            raise SquishError("Couldn't save %s\nin %s\n\n%s\n\n%s"
                              % (filename, out_dir, _error_text(exc), tail))
        placed.append(target)

    # (d) The old copies are no longer needed.
    _remove_quietly([backup for _target, backup in moved])
    report("write", len(parts), len(parts), "Saved %d file(s)" % len(parts))

    written = []
    for i, (part, target) in enumerate(zip(parts, targets)):
        text = part.get("text") or ""
        chars = len(text)
        info = {
            "path": target,
            "chars": chars,
            "bytes": len(text.encode("utf-8", "replace")),
            "est_tokens": int(round(chars / CHARS_PER_TOKEN)),
            "first_date": part.get("first_date") or "",
            "last_date": part.get("last_date") or "",
            "emails": int(part.get("emails") or 0),
            "threads": int(part.get("threads") or 0),
            "kind": kinds[i] if kinds else KIND_EMAILS,
        }
        if info["kind"] == KIND_DOCUMENTS:
            info["documents"] = int(part.get("documents") or 0)
        written.append(info)
    return written


# --------------------------------------------------------------------------
# Which digest files belong to which kind of run
#
# <cache>/<project key>.outputs.json remembers, per output folder, the files
# each kind of run wrote ("" = all dates, no focus keywords; otherwise the
# filter_key). A run replaces only the files of the same kind, so a run for one
# month or with focus keywords never deletes the full digest.
# --------------------------------------------------------------------------

def manifest_path(project):
    return paths.cache_dir() / ("%s.outputs.json" % _project_key(project))


def _load_manifest(path):
    try:
        with open(str(path), encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
        return {}
    folders = data.get("folders")
    return folders if isinstance(folders, dict) else {}


def _save_manifest(path, folders):
    try:
        _write_text_atomic(str(path), json.dumps({"version": MANIFEST_VERSION, "folders": folders},
                                                 ensure_ascii=True, indent=1))
        return True
    except OSError:
        return False


def _previous_files_here(project, folder_key, filtered, kind=KIND_EMAILS):
    """Names of the files the project's last run wrote (``previous_files``, passed
    in by the GUI) that are in this output folder and are the same kind of run
    as this one (``filtered``: with a date filter or focus keywords, or neither;
    ``kind``: an email or a documents digest)."""
    names = []
    for path in project.get("previous_files") or []:
        path = str(path or "")
        if not path or os.path.normcase(os.path.abspath(os.path.dirname(path))) != folder_key:
            continue
        filename = os.path.basename(path)
        if bool(_FILTER_TAG.search(filename)) == filtered and digest_file_kind(filename) == kind:
            names.append(filename)
    return names


def manifest_key(filter_text, kind=KIND_EMAILS):
    """The key under which .outputs.json records a kind of run: the filter_key for
    the email digest, "documents" (plus " | " and the filter_key) for the
    documents digest, so the two never replace each other's files."""
    if kind != KIND_DOCUMENTS:
        return filter_text
    return KIND_DOCUMENTS + (" | " + filter_text if filter_text else "")


def _kind_of_key(key):
    """The kind of digest ("emails" or "documents") a manifest_key belongs to."""
    if key == KIND_DOCUMENTS or str(key).startswith(KIND_DOCUMENTS + " | "):
        return KIND_DOCUMENTS
    return KIND_EMAILS


def _remove_older_outputs(project, out_dir, new_names, filter_text, kind=KIND_EMAILS):
    """Delete this project's older digest files that the new ones replace.

    Returns (removed names, error lines). Replaced: the files the previous run
    of the same kind wrote (whatever the project was called then, so renaming a
    project leaves no stale files behind); and, for a run without date or
    keyword filters, any other older 'Squish - <name> - <dates>.txt' file (or
    'Squish - <name> - documents - <dates>.txt' for the documents digest) that
    no filtered run wrote. If the list of what each run wrote is missing, the
    project's ``previous_files`` (from the GUI) stand in for it. Only files
    whose names follow the digest pattern are ever deleted. ``filter_text`` is
    the run's filter_key.
    """
    name = project_name(project)
    run_key = manifest_key(filter_text, kind)
    manifest_file = manifest_path(project)
    folders = _load_manifest(manifest_file)
    folder_key = os.path.normcase(os.path.abspath(out_dir))
    owners = folders.get(folder_key)
    if not isinstance(owners, dict):
        owners = {}
    new_lower = set(n.lower() for n in new_names)
    others = set()
    for key, names in owners.items():
        if key != run_key and isinstance(names, list):
            others.update(str(n).lower() for n in names)

    ours = set(str(n) for n in owners.get(run_key) or [])
    if not any(_kind_of_key(key) == kind for key in owners):
        # No record of this kind of digest here (lost, or written before records were kept)
        filtered = filter_text != ""
        ours.update(_previous_files_here(project, folder_key, filtered, kind))
    candidates = set(ours)
    if filter_text == "":
        try:
            existing = os.listdir(paths.long_path(out_dir))
        except OSError:
            existing = []
        candidates.update(n for n in existing if not _FILTER_TAG.search(n))

    removed, errors, still_there = [], [], []
    for filename in sorted(candidates):
        low = filename.lower()
        if low in new_lower or low in others:
            continue
        if filename in ours:
            if not is_digest_file_name(filename):
                continue
        elif not is_old_output(filename, name, kind):
            continue
        path = os.path.join(out_dir, filename)
        if not os.path.exists(paths.long_path(path)):
            continue
        try:
            os.remove(paths.long_path(path))
            removed.append(filename)
        except OSError as exc:
            errors.append("%s: %s" % (filename, _error_text(exc)))
            still_there.append(filename)

    # Remember what this run wrote. A file belongs to the run that wrote it last;
    # forget files that are no longer there.
    for key in list(owners):
        names = owners[key] if isinstance(owners[key], list) else []
        kept = [n for n in names if key != run_key and str(n).lower() not in new_lower
                and os.path.exists(paths.long_path(os.path.join(out_dir, str(n))))]
        if kept:
            owners[key] = kept
        else:
            del owners[key]
    owners[run_key] = list(new_names) + still_there
    folders[folder_key] = owners
    _save_manifest(manifest_file, folders)
    return removed, errors


# --------------------------------------------------------------------------
# Run results
# --------------------------------------------------------------------------

def _empty_result(output_folder):
    return {
        "cancelled": False,
        "files": [],
        "output_folder": output_folder,
        "files_found": 0,
        "files_read": 0,
        "from_cache": 0,
        "failed": [],
        "doc_problems": [],
        "stats": {},
        "elapsed_s": 0.0,
        "log_path": "",
        "finished_at": "",
    }


def _finish(result, started):
    result["elapsed_s"] = round(time.time() - started, 2)
    result["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")


def _cancelled_result(result, started):
    result["cancelled"] = True
    result["files"] = []
    _finish(result, started)
    return result


def _make_digest(kept, project, source, outside, cancel, report, no_access=0, unreadable=0,
                 doc_ids=None):
    """build_digest with Cancel and progress. Returns None if the run was
    cancelled while the digest was being built. ``outside`` is the list of
    emails the date filter left out (counted in the header, and used only so
    that quoting them does not 'recover' them); ``no_access`` is the number of
    emails left out because Squish can't open their folder, and ``unreadable``
    the number of email files that couldn't be read (both said in the header,
    so Claude knows emails may be missing). ``doc_ids`` (only passed on when
    there are documents) marks attachments with their documents digest ID."""

    def digest_progress(done, total):
        report("digest", done, total, "Squishing emails... %d of %d" % (done, total))

    extra = {"doc_ids": doc_ids} if doc_ids else {}
    try:
        return build_digest(kept, project, source_label=source, outside_dates=len(outside),
                            cancel=cancel, progress=digest_progress, also_filed=outside,
                            no_access_emails=no_access, unreadable_files=unreadable, **extra)
    except DigestCancelled:
        return None


# --------------------------------------------------------------------------
# Documents: which ones, their IDs, and where they came from
# --------------------------------------------------------------------------

def _scan_documents_folder(folder, project, out_dir, cancel, report, problems):
    """The loose files in the documents folder (see scan_documents), or None if
    cancelled. A folder that can't be used is added to ``problems`` and gives
    no files: documents never stop a run."""
    if not os.path.isdir(paths.long_path(folder)):
        problems.append([folder, "%s: if it is on a network drive, check that you are "
                                 "connected to the office network or VPN" % _DOCS_FOLDER_MISSING])
        return []
    if _same_or_inside(os.path.abspath(folder), os.path.abspath(out_dir)):
        problems.append([folder, "%s: it is the output folder (or inside it)"
                                 % _DOCS_FOLDER_IS_OUTPUT])
        return []
    report("scan", 0, 0, "Looking for documents...")
    errors = []
    found = scan_documents(folder, project.get("docs_include_subfolders", True), out_dir,
                           cancel, report, errors)
    problems.extend(errors)
    return found


def _read_loose_documents(items, store, cancel, report):
    """Condense the loose files, a few at a time (cached ones are quick).
    Returns [(item, key, sha1, DocText)] by path, or None if cancelled."""
    total = len(items)
    results = []
    report("documents", 0, total, "Reading documents...")
    if not total:
        return results
    finished = queue.Queue()
    with ThreadPoolExecutor(max_workers=READ_WORKERS) as pool:
        futures = {}
        for item in items:
            future = pool.submit(_loose_document, item, store, cancel)
            future.add_done_callback(finished.put)
            futures[future] = item
        while len(results) < total:
            try:
                future = finished.get(timeout=CANCEL_CHECK_S)
            except queue.Empty:
                future = None
            if future is not None:
                try:
                    results.append(future.result())
                except Exception as exc:   # documents never stop a run
                    item = futures[future]
                    name = os.path.basename(item[0])
                    results.append((item, _name_key(name, item[2]), "", _doc_without_contents(
                        name, "error", "could not read this file (%s)" % type(exc).__name__)))
                report("documents", len(results), total,
                       "Reading documents... %d of %d" % (len(results), total))
            if _is_cancelled(cancel):
                for f in futures:
                    f.cancel()
                return None
    if any(doc is None for _item, _key, _sha1, doc in results):
        return None     # cancelled while a document was waiting its turn
    results.sort(key=lambda r: r[0][0].lower())
    return results


def _email_time(rec):
    """Seconds since 1970 of an email's date, for ordering (undated emails last)."""
    text = (rec.get("date") or "").strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return float("inf")


def _attachment_document(att, store):
    """(key, sha1, DocText) for an email attachment that belongs in the documents
    digest, or None (images, attached emails and other files that are only
    listed in the email digest)."""
    if not isinstance(att, dict) or att.get("inline") or att.get("email") is not None:
        return None
    name = att.get("name") or ""
    sha1 = att.get("sha1") or ""
    if sha1:
        doc = store.get(sha1)
        if doc is None:
            doc = _doc_without_contents(name, "error", "not read")
        return sha1, sha1, doc
    size = att.get("size")
    if docs.is_supported(name):
        if isinstance(size, int) and size > docs.DOC_MAX_BYTES:
            return (_name_key(name, size), "",
                    _doc_without_contents(name, "too_big", _too_big_note()))
        return _name_key(name, size), "", _doc_without_contents(
            name, "error", "the attachment's contents could not be read")
    if os.path.splitext(name)[1].lower() in OTHER_DOC_EXT:
        return _name_key(name, size), "", _doc_without_contents(name)
    return None


def _collect_documents(records, loose, store):
    """The run's documents, each once, in order of first appearance: the
    attachments of ``records`` by email date, then the loose files by path.
    Identical contents (same sha1; same name and size for files that were not
    read) are one document. Each is a dict: id ("D1"... for documents whose
    contents were read, else ""), key, name, sha1, size, doc (DocText), emails
    [(record, attachment index)] and files [(path, mtime, size)]."""
    found = {}
    for rec in sorted(records, key=lambda r: (_email_time(r), (r.get("path") or "").lower())):
        for index, att in enumerate(rec.get("attachments") or []):
            item = _attachment_document(att, store)
            if item is None:
                continue
            key, sha1, doc = item
            if key not in found:
                found[key] = {"key": key, "name": att.get("name") or "", "sha1": sha1,
                              "size": att.get("doc_size") or att.get("size"), "doc": doc,
                              "emails": [], "files": []}
            found[key]["emails"].append((rec, index))
    for (path, mtime, size), key, sha1, doc in loose:
        if key not in found:
            found[key] = {"key": key, "name": os.path.basename(path), "sha1": sha1,
                          "size": size, "doc": doc, "emails": [], "files": []}
        found[key]["files"].append((path, mtime, size))
    numbered = 0
    for d in found.values():
        d["id"] = ""
        if d["doc"].get("status") == "ok":
            numbered += 1
            d["id"] = "D%d" % numbered
    return list(found.values())


def _doc_ids(found):
    """{(record path, attachment index): "D12"} for the email digest's '=D12' marks."""
    ids = {}
    for d in found:
        if d["id"]:
            for rec, index in d["emails"]:
                ids[(rec.get("path") or "", index)] = d["id"]
    return ids


def _used_sha1s(records, loose):
    """Every document sha1 this project refers to (also outside the dates), so the
    documents cache keeps them."""
    used = set(sha1 for _item, _key, sha1, _doc in loose if sha1)
    for rec in records:
        for att in rec.get("attachments") or []:
            if isinstance(att, dict) and att.get("sha1"):
                used.add(att["sha1"])
    return used


_SHOWN_ID = re.compile(r"(?<= )=(D\d+)(?![0-9A-Za-z])")


def _shown_doc_ids(email_parts):
    """The document IDs ('D12') the email digest shows in its '[att: ...]' lists
    (only below the header, whose "How to read" line has an example)."""
    shown = set()
    for part in email_parts or []:
        text = part.get("text") or ""
        start = text.find("\n## ")
        if start < 0:
            continue
        for line in text[start:].split("\n"):
            at = line.find("[att: ")
            if at >= 0:
                shown.update(_SHOWN_ID.findall(line[at:]))
    return shown


def _doc_search_text(name, doc):
    """A document's name, title and text in one string (zip members' documents too)."""
    pieces = [name or "", doc.get("title") or ""]
    for block in doc.get("blocks") or []:
        pieces.append(block.get("text") or "")
        if isinstance(block.get("doc"), dict):
            pieces.append(_doc_search_text("", block["doc"]))
    return "\n".join(pieces)


def _focus_documents(found, keywords, email_parts):
    """The documents a run with focus keywords keeps: a keyword in the document's
    name or text (matched like the email digest's keywords), or its '=Dn' mark
    shown in the email digest (it is attached to an email in a kept thread).
    Without keywords, all of them."""
    words = cleaning.keyword_list(keywords)
    if not words:
        return list(found)
    patterns = [_keyword_pattern(cleaning.fold_for_match(w).strip()) for w in words]
    shown = _shown_doc_ids(email_parts)
    kept = []
    for d in found:
        if d["id"] and d["id"] in shown:
            kept.append(d)
            continue
        text = cleaning.fold_for_match(_doc_search_text(d["name"], d["doc"]))
        if any(p.search(text) for p in patterns):
            kept.append(d)
    return kept


def _local_iso(timestamp):
    """A file time as local ISO 8601 with offset, like EmailRecord dates ('' if unknown)."""
    try:
        return datetime.fromtimestamp(timestamp).astimezone().replace(microsecond=0).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _documents_for_digest(found, aliases):
    """The ``docs`` list for docdigest.build_documents_digest (see DESIGN.md):
    id, name, sha1, size, doc and sources (emails oldest first, one per email
    even when Mail Manager filed it in several folders; then loose files)."""
    out = []
    for d in found:
        sources, seen = [], set()
        for rec, _index in d["emails"]:
            subject = rec.get("subject") or ""
            sender = (rec.get("sender_email") or rec.get("sender_name") or "").lower()
            key = ((rec.get("date") or "")[:16], sender, cleaning.thread_key(subject))
            if key in seen:
                continue
            seen.add(key)
            sources.append({"kind": "email", "date": rec.get("date") or "",
                            "sender_alias": aliases.get(rec.get("path") or "", ""),
                            "sender_name": rec.get("sender_name") or "",
                            "sender_email": rec.get("sender_email") or "",
                            "subject": subject, "thread": cleaning.clean_subject(subject),
                            "path": rec.get("path") or ""})
        for path, mtime, _size in d["files"]:
            sources.append({"kind": "file", "path": path, "mtime": _local_iso(mtime)})
        out.append({"id": d["id"], "name": d["name"], "sha1": d["sha1"], "size": d["size"],
                    "doc": d["doc"], "sources": sources})
    return out


def _documents_source_label(source, want_attachments, docs_folder):
    """'H:\\Jobs\\01 Emails (attachments) + H:\\Jobs\\04 Reports', for the documents
    digest's header."""
    labels = []
    if want_attachments:
        labels.append("%s (attachments)" % source)
    if docs_folder:
        labels.append(docs_folder)
    return " + ".join(labels)


def _make_documents_digest(shown, aliases, project, source_label, notes):
    """build_documents_digest for the documents kept, or None (with a note for
    the run log) if it failed: documents never stop a run. ``aliases`` is the
    email digest's {record path: sender alias} (its result's "aliases")."""
    try:
        return build_documents_digest(_documents_for_digest(shown, aliases), project,
                                      source_label=source_label)
    except Exception as exc:
        notes.append("The documents digest could not be made (%s: %s), so only the email digest "
                     "was written." % (type(exc).__name__, exc))
        return None


def _document_problems(found):
    """[[where, reason]] for the documents whose contents could not be read."""
    problems = []
    for d in found:
        doc = d["doc"]
        if doc.get("status") not in UNREAD_STATUSES:
            continue
        if d["files"]:
            where = d["files"][0][0]
        else:
            where = "%s (attached to %s)" % (d["name"], d["emails"][0][0].get("path") or "")
        problems.append([where, doc.get("note") or doc.get("status")])
    return problems


def _document_stats(shown, digest_stats):
    """RunResult stats for the documents digest. docdigest's own numbers win
    where it has them (it decides what is a drawing or a later version)."""
    ok = [d for d in shown if d["doc"].get("status") == "ok"]
    mine = {
        "documents": sum(1 for d in ok if not d["doc"].get("drawing")),
        "doc_drawings": sum(1 for d in ok if d["doc"].get("drawing")),
        "doc_other": len(shown) - len(ok),
        "doc_versions": 0,
        "doc_failed": sum(1 for d in shown if d["doc"].get("status") in UNREAD_STATUSES),
    }
    theirs = digest_stats or {}
    return dict((k, theirs[k] if isinstance(theirs.get(k), int) else v) for k, v in mine.items())


def _save_doc_store(store, notes):
    if store is not None and not store.save():
        notes.append("The documents cache could not be saved, so the next run will read the "
                     "documents again.")


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

def run_project(project, progress=None, cancel=None):
    """Scan, read, filter, digest and write one project. Returns a RunResult dict.

    Raises SquishError for problems the user can fix (missing folder, bad dates,
    output folder not writable, the folder stopping responding part way).
    Documents (attachments and the documents folder) never stop a run.
    """
    started = time.time()
    report = _Progress(progress)
    name = project_name(project)
    source = paths.clean_folder_text(project.get("source_folder"))
    out_dir = output_folder_for(project)
    date_from = _check_date(project.get("date_from"), "'from'")
    date_to = _check_date(project.get("date_to"), "'to'")
    if date_from and date_to and date_from > date_to:
        raise SquishError("The 'from' date (%s) is after the 'to' date (%s)." % (date_from, date_to))
    keywords = project.get("focus_keywords") or ""
    want_attachments = bool(project.get("docs_from_attachments", True))
    docs_folder = paths.clean_folder_text(project.get("docs_folder"))
    result = _empty_result(out_dir)
    timings = {}
    notes = []

    # 1. Scan.
    t0 = time.time()
    report("scan", 0, 0, "Looking for emails...")
    check_source_folder(source)
    if output_inside_source(source, out_dir):
        raise SquishError(
            "The output folder is inside the email folder:\n%s\n\n"
            "Squish never writes into the email folders. Choose an output folder that "
            "is somewhere else." % out_dir)
    check_output_folder(out_dir)
    include_subfolders = project.get("include_subfolders", True)
    scan_errors = []
    listed = {}
    files = scan_folder(source, include_subfolders, cancel, report, scan_errors, listed)
    if files is None or _is_cancelled(cancel):
        return _cancelled_result(result, started)
    if not files:
        if scan_errors:
            raise SquishError(_stopped_message(
                "the email folder (or a folder inside it) could not be opened"))
        raise SquishError(
            "There are no Outlook emails (.msg or .eml files) in this folder%s:\n%s"
            % (" or its subfolders" if include_subfolders else
               " (subfolders are not included - tick 'Include subfolders' to look in them)",
               source))
    result["files_found"] = len(files)
    report("scan", len(files), len(files), "Found %d email files" % len(files))
    loose_files = []
    if docs_folder:
        loose_files = _scan_documents_folder(docs_folder, project, out_dir, cancel, report,
                                             result["doc_problems"])
        if loose_files is None or _is_cancelled(cancel):
            return _cancelled_result(result, started)
    timings["scan"] = time.time() - t0

    # 2. Read (or take from the cache). Attached documents are condensed as each
    # email is read, so their bytes are never all held at once.
    t0 = time.time()
    cache_file = cache_path(project)
    old_cache = load_cache(cache_file)
    store = _DocStore(docs_cache_path(project)) if (want_attachments or docs_folder) else None
    read_store = store if want_attachments else None
    new_cache = {}
    records = []
    failed = []
    out_of_reach = 0
    total = len(files)
    done = 0
    cancelled = False
    report("read", 0, total, "Reading emails...")
    finished = queue.Queue()   # each read lands here as soon as it is done
    with ThreadPoolExecutor(max_workers=READ_WORKERS) as pool:
        futures = []
        for p in files:
            future = pool.submit(_read_one, p, old_cache.get(p), listed.get(p), read_store, cancel)
            future.add_done_callback(finished.put)
            futures.append(future)
        while done < total:
            # Wake up now and then even while slow files are being read, so
            # Cancel only waits for the files already being read.
            try:
                future = finished.get(timeout=CANCEL_CHECK_S)
            except queue.Empty:
                future = None
            if future is not None:
                path, entry, error, hit, unreachable = future.result()
                done += 1
                if entry is None:
                    failed.append([path, error or "could not be read"])
                    out_of_reach += 1 if unreachable else 0
                else:
                    new_cache[path] = entry
                    records.append(entry["record"])
                    if hit:
                        result["from_cache"] += 1
                    else:
                        result["files_read"] += 1
                report("read", done, total, "Reading emails... %d of %d" % (done, total))
            if _is_cancelled(cancel):
                cancelled = True
                for f in futures:
                    f.cancel()
                break
    timings["read"] = time.time() - t0
    cancelled = cancelled or _is_cancelled(cancel)

    # Save the caches (also when cancelled, so the next run doesn't start again).
    merged = _merged_cache(old_cache, new_cache, files, not scan_errors)
    if result["files_read"] or (not cancelled and set(old_cache) != set(merged)):
        if not save_cache(cache_file, merged):
            notes.append("The read cache could not be saved, so the next run will read "
                         "every email again.")
    if cancelled:
        _save_doc_store(store, notes)
        return _cancelled_result(result, started)
    failed.sort(key=lambda item: item[0].lower())
    result["failed"] = scan_errors + failed

    # Stop here, keeping the previous digest, if this run is missing too much.
    reason = _incomplete_reason(source, files, records, scan_errors, failed, out_of_reach,
                                old_cache)
    if reason:
        _save_doc_store(store, notes)
        message = _stopped_message(
            reason, had_previous=bool(old_cache),
            damaged_only=(out_of_reach == 0 and not scan_errors and bool(failed)))
        _finish(result, started)
        notes.append("Stopped: " + message.split("\n")[0])
        error = SquishError(message)
        error.log_path = _write_log(project, result, timings, [], [], date_from, date_to, notes)
        raise error

    # Folders Squish has no access to don't stop the run (trying again won't
    # help), but say what is missing: in the run log, the failed list, the
    # digest header and the stats. Their cached emails are not used.
    no_access = 0
    for item, had in _no_access_losses(scan_errors, old_cache):
        no_access += had
        item[1] += " - it held %s last time; they are not in this digest" % _plural(had, "email")
        notes.append("Squish has no access to %s, which held %s last time, so they are not in "
                     "this digest. If you should have access (e.g. after a VPN or sign-in "
                     "hiccup), run it again - those emails are still remembered, so it will "
                     "be quick." % (item[0], _plural(had, "email")))

    # Filter by date (attachments go with their email; loose files are not dated).
    records.sort(key=lambda r: r.get("path", "").lower())
    kept, outside = [], []
    for r in records:
        (kept if in_date_range(r, date_from, date_to) else outside).append(r)

    # 3. Documents: condense the loose files, then put every document in order
    # of first appearance and number it (D1, D2 ...).
    found = []
    if store is not None:
        t0 = time.time()
        loose = _read_loose_documents(loose_files, store, cancel, report)
        if loose is None or _is_cancelled(cancel):
            _save_doc_store(store, notes)
            return _cancelled_result(result, started)
        found = _collect_documents(kept if want_attachments else [], loose, store)
        store.mark_used(_used_sha1s(records if want_attachments else [], loose))
        _save_doc_store(store, notes)
        report("documents", 1, 1, "Found %s" % _plural(len(found), "document"))
        timings["documents"] = time.time() - t0
        doc_line = ("Documents: %d found (%d condensed this run; the others came from the "
                    "documents cache or are file types that are only listed)"
                    % (len(found), store.extracted))
    else:
        doc_line = ""

    # 4. Build the email digest (attachments marked with their document IDs).
    t0 = time.time()
    report("digest", 0, 1, "Squishing %d emails..." % len(kept))
    # Email files that couldn't be read (not folders: those are in scan_errors).
    unreadable = len(failed)
    doc_ids = _doc_ids(found)
    digest = _make_digest(kept, project, source, outside, cancel, report, no_access, unreadable,
                          doc_ids)
    if digest is None or _is_cancelled(cancel):
        return _cancelled_result(result, started)

    # 5. Build the documents digest.
    shown_docs, doc_parts, doc_stats = [], [], {}
    if found:
        emails_used = (digest.get("stats") or {}).get("emails_used")
        email_parts = (digest.get("parts") or []) if emails_used else []
        shown_docs = _focus_documents(found, keywords, email_parts)
        if any(d["id"] for d in shown_docs):
            # (A list of files whose contents couldn't be read is not worth a digest.)
            report("digest", 0, 1, "Squishing %s..." % _plural(len(shown_docs), "document"))
            doc_digest = _make_documents_digest(
                shown_docs, digest.get("aliases") or {}, project,
                _documents_source_label(source, want_attachments, docs_folder), notes)
            if _is_cancelled(cancel):
                return _cancelled_result(result, started)
            if doc_digest is not None:
                doc_parts = doc_digest.get("parts") or []
                doc_stats = doc_digest.get("stats") or {}
        if doc_ids and not doc_parts:
            # The email digest must not point at a documents digest that isn't written.
            digest = _make_digest(kept, project, source, outside, cancel, report, no_access,
                                  unreadable)
            if digest is None or _is_cancelled(cancel):
                return _cancelled_result(result, started)
    report("digest", 1, 1, "Digest ready")
    timings["digest"] = time.time() - t0
    stats = dict(digest.get("stats") or {})
    stats["outside_dates"] = len(outside)
    stats["no_access_emails"] = no_access
    stats["unreadable_files"] = unreadable
    stats.update(_document_stats(shown_docs if doc_parts else [], doc_stats))
    result["stats"] = stats
    result["doc_problems"].extend(_document_problems(shown_docs))

    # 6. Write the parts (emails and documents in one all-or-nothing swap), then
    # remove the older digest files they replace.
    t0 = time.time()
    parts = digest.get("parts") or []
    removed, remove_errors = [], []
    if not stats.get("emails_used"):
        # Nothing survived the date / keyword filters: write no email digest and
        # keep the previous files instead of replacing them with an empty header.
        parts = []
        notes.append("No emails were left after the date and focus keyword filters, so no "
                     "email digest was written and the earlier digest files were kept.")
    focus = focus_label(keywords)
    dates = date_label(date_from, date_to)
    names = output_filenames(name, parts, focus, out_dir, dates=dates) if parts else []
    doc_names = (output_filenames(name, doc_parts, focus, out_dir, dates=dates, kind=KIND_DOCUMENTS)
                 if doc_parts else [])
    if parts or doc_parts:
        kinds = [KIND_EMAILS] * len(parts) + [KIND_DOCUMENTS] * len(doc_parts)
        written = _write_parts(out_dir, parts + doc_parts, names + doc_names, report, cancel, kinds)
        if written is None:
            return _cancelled_result(result, started)
        result["files"] = written
        key = filter_key(date_from, date_to, keywords)
        for kind, kind_names in ((KIND_EMAILS, names), (KIND_DOCUMENTS, doc_names)):
            if kind_names:
                gone, errors = _remove_older_outputs(project, out_dir, kind_names, key, kind)
                removed.extend(gone)
                remove_errors.extend(errors)
    timings["write"] = time.time() - t0

    _finish(result, started)
    result["log_path"] = _write_log(project, result, timings, removed, remove_errors,
                                    date_from, date_to, notes, doc_line)
    return result


def _write_log(project, result, timings, removed, remove_errors, date_from, date_to, notes=(),
               doc_line=""):
    """Write '<project> - last run.txt' in the logs folder; returns its path ('' on failure)."""
    name = project_name(project)
    log_path = paths.logs_dir() / ("%s - last run.txt" % paths.safe_filename(name))

    def yes_no(value):
        return "yes" if value else "no"

    stats = result.get("stats") or {}
    folder_problems = [f for f in result["failed"] if is_folder_problem(f[1])]
    file_problems = [f for f in result["failed"] if not is_folder_problem(f[1])]
    docs_on = bool(project.get("docs_from_attachments", True)) or bool(
        paths.clean_folder_text(project.get("docs_folder")))
    lines = [
        "Squish run log",
        "Project: %s" % name,
        "Finished: %s" % result["finished_at"],
        "Source: %s" % (project.get("source_folder") or ""),
        "Output: %s" % result["output_folder"],
        "Reader: %s" % readers.backend_status(),
        "Include subfolders: %s" % yes_no(project.get("include_subfolders", True)),
        "Dates: %s to %s" % (date_from or "start", date_to or "end"),
        "Squeeze: %s | part size: %s | drop noise: %s | recover quoted: %s"
        % (project.get("squeeze", "standard"), project.get("part_size", "medium"),
           yes_no(project.get("drop_noise", True)), yes_no(project.get("recover_quoted", True))),
        "Focus keywords: %s" % (_one_line(project.get("focus_keywords")) or "(none)"),
    ]
    if docs_on:
        folder = paths.clean_folder_text(project.get("docs_folder"))
        if folder:
            folder += " (include subfolders: %s)" % yes_no(project.get("docs_include_subfolders",
                                                                       True))
        lines.append("Documents: attachments %s | folder: %s | %s"
                     % (yes_no(project.get("docs_from_attachments", True)), folder or "(none)",
                        docs.backend_status()))
    lines += [
        "",
        "Files found: %d | read: %d | from cache: %d | could not read: %d"
        % (result["files_found"], result["files_read"], result["from_cache"], len(file_problems)),
        "Times: " + ", ".join("%s %.1f s" % (k, v) for k, v in timings.items())
        + ", total %.1f s" % result["elapsed_s"],
    ]
    if doc_line:
        lines.append(doc_line)
    lines += [
        "",
        "Digest: " + ", ".join("%s %s" % (k.replace("_", " "), v) for k, v in sorted(stats.items())),
    ]
    if notes:
        lines.append("")
        lines.extend(notes)
    lines.append("")
    lines.append("Files written (%d):" % len(result["files"]))
    for f in result["files"]:
        if f.get("kind") == KIND_DOCUMENTS:
            lines.append("  %s | %d chars | ~%d tokens | %s to %s | %d documents"
                         % (os.path.basename(f["path"]), f["chars"], f["est_tokens"],
                            f["first_date"] or "?", f["last_date"] or "?", f.get("documents", 0)))
            continue
        lines.append("  %s | %d chars | ~%d tokens | %s to %s | %d emails, %d threads"
                     % (os.path.basename(f["path"]), f["chars"], f["est_tokens"],
                        f["first_date"] or "?", f["last_date"] or "?", f["emails"], f["threads"]))
    if removed:
        lines.append("")
        lines.append("Older digest files removed (%d):" % len(removed))
        lines.extend("  " + n for n in removed)
    if remove_errors:
        lines.append("")
        lines.append("Older digest files that could not be removed (%d):" % len(remove_errors))
        lines.extend("  " + e for e in remove_errors)
    if folder_problems:
        lines.append("")
        lines.append("Folders that could not be opened (%d):" % len(folder_problems))
        for path, error in folder_problems:
            lines.append("  %s" % path)
            lines.append("      %s" % error)
    lines.append("")
    lines.append("Files that could not be read (%d):" % len(file_problems))
    for path, error in file_problems:
        lines.append("  %s" % path)
        lines.append("      %s" % error)
    doc_problems = result.get("doc_problems") or []
    if docs_on or doc_problems:
        lines.append("")
        lines.append("Documents that could not be read (%d):" % len(doc_problems))
        for where, error in doc_problems:
            lines.append("  %s" % where)
            lines.append("      %s" % error)
    try:
        _write_text_atomic(str(log_path), "\n".join(lines) + "\n")
    except OSError:
        return ""
    return str(log_path)


def _one_line(text):
    return re.sub(r"\s+", " ", text or "").strip()


def part_size_keys():
    """The part size names the digest understands (for the CLI and GUI)."""
    return list(PART_SIZES)
