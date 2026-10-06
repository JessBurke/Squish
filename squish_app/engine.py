"""Run one project: find the email files, read them (using a cache), filter by
date, build the digest and write the output files.

    run_project(project, progress=None, cancel=None) -> RunResult

``progress(stage, done, total, message)`` is called from the worker thread
(stage is "scan", "read", "digest" or "write"; total is 0 when not yet known).
``cancel`` is a threading.Event; a cancelled run writes no output files and no
run log (the read cache is still saved, so the next run is quicker).

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
from datetime import datetime

from . import cleaning, paths, readers
from .digest import build_digest, DigestCancelled, PART_SIZES

CACHE_VERSION = 3          # bump when the EmailRecord format changes
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
_NO_ACCESS = "no access to this folder"
_NOT_OPENED = "folder could not be opened"


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
    path = str(path)
    tmp = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
    try:
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as fh:
            json.dump({"version": CACHE_VERSION, "files": entries}, fh,
                      ensure_ascii=True, separators=(",", ":"))
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


def _read_one(path, cached, known_stat=None):
    """Worker thread: (path, cache entry or None, error text or None, came from
    cache, error was the file being out of reach).

    ``known_stat`` is (mtime, size) from the folder listing, if known; then an
    unchanged file costs no extra trip to the file server. "Out of reach" means
    the file couldn't be opened or read (OSError, e.g. the network dropped), as
    opposed to a file that was read but isn't a proper email.
    """
    if known_stat is not None:
        mtime, size = known_stat
    else:
        try:
            st = os.stat(paths.long_path(path))
        except OSError as exc:
            return path, None, _error_text(exc), False, True
        mtime, size = st.st_mtime, st.st_size
    if _usable_cache_entry(cached, mtime, size):
        return path, cached, None, True, False
    try:
        record = readers.read_email(path)
    except Exception as exc:
        return path, None, _error_text(exc), False, isinstance(exc, OSError)
    record["path"] = path
    return path, {"mtime": mtime, "size": size, "record": record}, None, False, False


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


def output_filenames(name, parts, focus="", folder="", dates=""):
    """File names for the digest parts, e.g. 'Squish - X - 2024-01-02 to 2024-09-30 (part 1 of 2).txt'.

    Each part is named with its own date range. The project name is shortened
    by paths.output_name when it is long. ``dates`` (from date_label) and
    ``focus`` (from focus_label) are added in brackets after the dates, so a
    dated run or a run with focus keywords never overwrites the project's full
    digest: '... 2025-01-02 to 2025-01-30 (only 2025-01-01 to 2025-01-31) (focus pump).txt'.
    When ``folder`` (the output folder) is given and a path would be longer
    than PATH_BUDGET characters, the focus text is shortened (see _short_focus)
    so the files can still be dragged from Explorer.
    """
    safe = paths.output_name(name)
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


def is_old_output(filename, name):
    """True for a digest file this project wrote earlier ('Squish - <name> - <dates>....txt').

    <name> is the project name as output_filenames writes it, or (for files
    written before long names were shortened) the whole safe file name."""
    for shown in (paths.output_name(name), paths.safe_filename(name, "Project")):
        prefix = "%s%s - " % (OUTPUT_PREFIX, shown)
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


def _write_parts(out_dir, parts, names, report, cancel=None):
    """Write the digest parts as ``names`` in ``out_dir``; returns the file info list.

    All parts are written to temp files first, then the files they replace are
    moved aside, then the new ones are put in place. If anything fails, the
    folder is put back the way it was and SquishError is raised. Cancel is
    checked only while the temp files are written (returns None, nothing
    changed), so the swap itself is all or nothing.
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
    for part, target in zip(parts, targets):
        text = part.get("text") or ""
        chars = len(text)
        written.append({
            "path": target,
            "chars": chars,
            "bytes": len(text.encode("utf-8", "replace")),
            "est_tokens": int(round(chars / CHARS_PER_TOKEN)),
            "first_date": part.get("first_date") or "",
            "last_date": part.get("last_date") or "",
            "emails": int(part.get("emails") or 0),
            "threads": int(part.get("threads") or 0),
        })
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


def _previous_files_here(project, folder_key, filtered):
    """Names of the files the project's last run wrote (``previous_files``, passed
    in by the GUI) that are in this output folder and are the same kind of run
    as this one (``filtered``: with a date filter or focus keywords, or neither)."""
    names = []
    for path in project.get("previous_files") or []:
        path = str(path or "")
        if not path or os.path.normcase(os.path.abspath(os.path.dirname(path))) != folder_key:
            continue
        filename = os.path.basename(path)
        if bool(_FILTER_TAG.search(filename)) == filtered:
            names.append(filename)
    return names


def _remove_older_outputs(project, out_dir, new_names, run_key):
    """Delete this project's older digest files that the new ones replace.

    Returns (removed names, error lines). Replaced: the files the previous run
    of the same kind wrote (whatever the project was called then, so renaming a
    project leaves no stale files behind); and, for a run without date or
    keyword filters, any other older 'Squish - <name> - <dates>.txt' file that
    no filtered run wrote. If the list of what each run wrote is missing, the
    project's ``previous_files`` (from the GUI) stand in for it. Only files
    whose names follow the digest pattern are ever deleted.
    """
    name = project_name(project)
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
    if not owners:
        filtered = run_key != ""
        ours.update(_previous_files_here(project, folder_key, filtered))
    candidates = set(ours)
    if run_key == "":
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
        if not (is_digest_file_name(filename) if filename in ours else is_old_output(filename, name)):
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
# The run
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


def _make_digest(kept, project, source, outside, cancel, report, no_access=0, unreadable=0):
    """build_digest with Cancel and progress. Returns None if the run was
    cancelled while the digest was being built. ``outside`` is the list of
    emails the date filter left out (counted in the header, and used only so
    that quoting them does not 'recover' them); ``no_access`` is the number of
    emails left out because Squish can't open their folder, and ``unreadable``
    the number of email files that couldn't be read (both said in the header,
    so Claude knows emails may be missing)."""

    def digest_progress(done, total):
        report("digest", done, total, "Squishing emails... %d of %d" % (done, total))

    try:
        return build_digest(kept, project, source_label=source, outside_dates=len(outside),
                            cancel=cancel, progress=digest_progress, also_filed=outside,
                            no_access_emails=no_access, unreadable_files=unreadable)
    except DigestCancelled:
        return None


def run_project(project, progress=None, cancel=None):
    """Scan, read, filter, digest and write one project. Returns a RunResult dict.

    Raises SquishError for problems the user can fix (missing folder, bad dates,
    output folder not writable, the folder stopping responding part way).
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
    timings["scan"] = time.time() - t0

    # 2. Read (or take from the cache).
    t0 = time.time()
    cache_file = cache_path(project)
    old_cache = load_cache(cache_file)
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
            future = pool.submit(_read_one, p, old_cache.get(p), listed.get(p))
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

    # Save the cache (also when cancelled, so the next run doesn't start again).
    merged = _merged_cache(old_cache, new_cache, files, not scan_errors)
    if result["files_read"] or (not cancelled and set(old_cache) != set(merged)):
        if not save_cache(cache_file, merged):
            notes.append("The read cache could not be saved, so the next run will read "
                         "every email again.")
    if cancelled:
        return _cancelled_result(result, started)
    failed.sort(key=lambda item: item[0].lower())
    result["failed"] = scan_errors + failed

    # Stop here, keeping the previous digest, if this run is missing too much.
    reason = _incomplete_reason(source, files, records, scan_errors, failed, out_of_reach,
                                old_cache)
    if reason:
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

    # 3. Filter by date and build the digest.
    t0 = time.time()
    records.sort(key=lambda r: r.get("path", "").lower())
    kept, outside = [], []
    for r in records:
        (kept if in_date_range(r, date_from, date_to) else outside).append(r)
    report("digest", 0, 1, "Squishing %d emails..." % len(kept))
    # Email files that couldn't be read (not folders: those are in scan_errors).
    unreadable = len(failed)
    digest = _make_digest(kept, project, source, outside, cancel, report, no_access, unreadable)
    if digest is None or _is_cancelled(cancel):
        return _cancelled_result(result, started)
    report("digest", 1, 1, "Digest ready")
    timings["digest"] = time.time() - t0
    stats = dict(digest.get("stats") or {})
    stats["outside_dates"] = len(outside)
    stats["no_access_emails"] = no_access
    stats["unreadable_files"] = unreadable
    result["stats"] = stats

    # 4. Write the parts, then remove the older digest files they replace.
    t0 = time.time()
    parts = digest.get("parts") or []
    removed, remove_errors = [], []
    if not stats.get("emails_used"):
        # Nothing survived the date / keyword filters: write nothing and keep the
        # previous digest files instead of replacing them with an empty header.
        parts = []
        notes.append("No emails were left after the date and focus keyword filters, so no "
                     "digest was written and the earlier digest files were kept.")
    if parts:
        names = output_filenames(name, parts, focus_label(keywords), out_dir,
                                 dates=date_label(date_from, date_to))
        written = _write_parts(out_dir, parts, names, report, cancel)
        if written is None:
            return _cancelled_result(result, started)
        result["files"] = written
        removed, remove_errors = _remove_older_outputs(
            project, out_dir, names, filter_key(date_from, date_to, keywords))
    timings["write"] = time.time() - t0

    _finish(result, started)
    result["log_path"] = _write_log(project, result, timings, removed, remove_errors,
                                    date_from, date_to, notes)
    return result


def _write_log(project, result, timings, removed, remove_errors, date_from, date_to, notes=()):
    """Write '<project> - last run.txt' in the logs folder; returns its path ('' on failure)."""
    name = project_name(project)
    log_path = paths.logs_dir() / ("%s - last run.txt" % paths.safe_filename(name))

    def yes_no(value):
        return "yes" if value else "no"

    stats = result.get("stats") or {}
    folder_problems = [f for f in result["failed"] if is_folder_problem(f[1])]
    file_problems = [f for f in result["failed"] if not is_folder_problem(f[1])]
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
        "",
        "Files found: %d | read: %d | from cache: %d | could not read: %d"
        % (result["files_found"], result["files_read"], result["from_cache"], len(file_problems)),
        "Times: " + ", ".join("%s %.1f s" % (k, v) for k, v in timings.items())
        + ", total %.1f s" % result["elapsed_s"],
        "",
        "Digest: " + ", ".join("%s %s" % (k.replace("_", " "), v) for k, v in sorted(stats.items())),
    ]
    if notes:
        lines.append("")
        lines.extend(notes)
    lines.append("")
    lines.append("Files written (%d):" % len(result["files"]))
    for f in result["files"]:
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
