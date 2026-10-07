"""The list of saved projects (projects.json in the Squish data folder).

A project is a plain dict (see DESIGN.md, "Project"). This module only loads,
saves and creates them; it never touches the email folders or digests.

    new_project(name)          -> dict with every setting at its default
    load_projects()            -> list of dicts (missing settings filled in)
    load_projects_report()     -> (list, problem or None) - the GUI uses this to say
                                  what went wrong when the file couldn't be loaded
    save_projects(items)       -> writes projects.json safely (temp file + replace)
    duplicate_project(project) -> a copy with a new id and " (copy)" on the name
    same_file_name(a, b)       -> True if two project names give the same file names
    take_window_lock()         -> lets only one Squish window edit the list at a time
    last_run_from_result(r)    -> the part of a run's result saved as the project's last_run
    record_last_run(id, r)     -> save a finished run as last_run when no window is open
                                  (used by the command line)

A projects.json that can't be opened for a moment (a virus scanner or sync
tool has it open, a network blip) is tried again a few times and is never
renamed or overwritten. Only damaged content (not JSON, not a list) is set
aside, as projects.json.bad-1 (bad-2, ...), so nothing is lost.
"""

import copy
import json
import os
import time
import uuid

from . import paths

FILE_VERSION = 1
DEFAULT_NAME = "New project"
DEFAULT_ORG_CODES = "slrconsulting.com=SLR"
COPY_BASE_MAX = 60       # longest name kept in front of " (copy)" (file names keep 80)
WINDOW_LOCK_NAME = "squish-window.lock"

# Allowed values (the same keys as digest.SQUEEZE_LEVELS / digest.PART_SIZES;
# repeated here so the project list still loads if the digest code is broken).
SQUEEZE_CHOICES = ("light", "standard", "max")
PART_SIZE_CHOICES = ("small", "medium", "large", "single")

_TEXT_KEYS = ("name", "source_folder", "output_folder", "date_from", "date_to",
              "focus_keywords", "org_codes", "docs_folder")
_BOOL_KEYS = ("include_subfolders", "drop_noise", "recover_quoted",
              "docs_from_attachments", "docs_include_subfolders")
_FOLDER_KEYS = ("source_folder", "output_folder", "docs_folder")


def default_settings():
    """Every project setting at its default value (no id)."""
    return {
        "id": "",
        "name": DEFAULT_NAME,
        "source_folder": "",
        "include_subfolders": True,
        "output_folder": "",
        "date_from": "",
        "date_to": "",
        "squeeze": "standard",
        "part_size": "medium",
        "focus_keywords": "",
        "org_codes": DEFAULT_ORG_CODES,
        "drop_noise": True,
        "recover_quoted": True,
        # Documents (v1.1): condense attached documents, and optionally a folder
        # of loose documents, into a separate documents digest.
        "docs_from_attachments": True,
        "docs_folder": "",
        "docs_include_subfolders": True,
        "last_run": None,
    }


def new_id():
    return uuid.uuid4().hex


def new_project(name=DEFAULT_NAME):
    """A new project with a fresh id and all the default settings."""
    project = default_settings()
    project["id"] = new_id()
    project["name"] = (name or "").strip() or DEFAULT_NAME
    return project


def normalise_project(raw):
    """Return a clean copy of a saved project dict.

    Missing settings get their defaults, wrong types are corrected and unknown
    keys (from a newer Squish) are kept as they are. Folder paths lose the
    quotes Windows' 'Copy as path' adds.
    """
    project = default_settings()
    project.update(copy.deepcopy(raw))
    for key in _TEXT_KEYS:
        value = project.get(key)
        project[key] = "" if value is None else str(value)
    for key in _FOLDER_KEYS:
        project[key] = paths.clean_folder_text(project[key])
    for key in _BOOL_KEYS:
        value = project.get(key)
        project[key] = True if value is None else bool(value)
    if project["squeeze"] not in SQUEEZE_CHOICES:
        project["squeeze"] = "standard"
    if project["part_size"] not in PART_SIZE_CHOICES:
        project["part_size"] = "medium"
    if not isinstance(project.get("last_run"), dict):
        project["last_run"] = None
    if not isinstance(project.get("id"), str) or not project["id"].strip():
        project["id"] = new_id()
    if not project["name"].strip():
        project["name"] = DEFAULT_NAME
    return project


def duplicate_project(project, existing_names=None):
    """A copy of ``project`` with a new id, " (copy)" added to the name and no last run.

    If ``existing_names`` is given and "<name> (copy)" is already taken, the
    copy is called "<name> (copy 2)", "<name> (copy 3)" and so on.
    """
    copy_of = normalise_project(project)
    copy_of["id"] = new_id()
    copy_of["last_run"] = None
    base = (project.get("name") or "").strip() or DEFAULT_NAME
    if len(paths.safe_filename(base)) > COPY_BASE_MAX:
        # File names keep only the first 80 characters of a name, so shorten a
        # long name first: otherwise " (copy)" would be cut off and the copy's
        # digest files would get mixed up with the original's.
        base = base[:COPY_BASE_MAX].rstrip(" .-")
    name = base + " (copy)"
    if existing_names is not None:
        taken = set(n.strip().lower() for n in existing_names)
        n = 2
        while name.lower() in taken:
            name = "%s (copy %d)" % (base, n)
            n += 1
    copy_of["name"] = name
    return copy_of


def unique_name(base, existing_names):
    """``base`` if no project uses it, else "base 2", "base 3", ..."""
    taken = set((n or "").strip().lower() for n in existing_names)
    if base.strip().lower() not in taken:
        return base
    n = 2
    while ("%s %d" % (base, n)).lower() in taken:
        n += 1
    return "%s %d" % (base, n)


def same_file_name(a, b):
    """True if two project names would give the same digest/log file names.

    The run log uses paths.safe_filename(name), which drops : / \\ * ? " < > |
    and trailing dots and keeps only the first 80 characters - so "Bridge:
    Stage 1" and "Bridge Stage 1" would share their files. (The output folder
    and digest file names use paths.output_name, which is made from the same
    safe name, so it can't tell two names apart that this treats as one.)
    """
    ka, kb = (a or "").strip().lower(), (b or "").strip().lower()
    if ka == kb:
        return True
    return paths.safe_filename(a, "Project").lower() == paths.safe_filename(b, "Project").lower()


def find_project(items, project_id):
    """The project with this id, or None."""
    for project in items:
        if project.get("id") == project_id:
            return project
    return None


# --------------------------------------------------------------------------
# Loading and saving
# --------------------------------------------------------------------------

def _backup_bad_file(path):
    """Rename a damaged projects.json to projects.json.bad-<n>. Returns the new path or None."""
    n = 1
    while os.path.exists("%s.bad-%d" % (path, n)):
        n += 1
    backup = "%s.bad-%d" % (path, n)
    try:
        os.replace(path, backup)
        return backup
    except OSError:
        pass
    try:  # could not rename (file locked?): at least keep a copy
        with open(path, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
        return backup
    except OSError:
        return None


def _parse(text):
    """The list of raw project dicts in the file text. Raises ValueError if damaged."""
    if not text.strip():
        return []
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("projects")
    if not isinstance(data, list):
        raise ValueError("projects.json does not contain a list of projects")
    return data


def load_projects_report(attempts=5):
    """(projects, problem): all saved projects, and what went wrong, if anything.

    ``problem`` is None, or a dict {"kind": "damaged" | "unreadable", "path",
    "backup", "error"}:
      damaged    - the file isn't a valid project list; it was set aside as
                   ``backup`` (projects.json.bad-<n>) and the list is empty.
      unreadable - the file couldn't be opened (locked, network) even after a
                   few tries, or couldn't be set aside. It was left untouched,
                   so the caller must not save over it.
    A missing file just means no projects yet. Never raises.
    """
    path = str(paths.projects_file())
    text = None
    for attempt in range(attempts):
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                text = fh.read()
            break
        except FileNotFoundError:
            return [], None
        except ValueError:          # not UTF-8 text (UnicodeDecodeError): really damaged
            break
        except OSError as exc:      # locked or a network blip: try again shortly
            if attempt == attempts - 1:
                return [], {"kind": "unreadable", "path": path, "backup": None,
                            "error": exc.strerror or str(exc)}
            time.sleep(0.1 * (attempt + 1))
    try:
        if text is None:
            raise ValueError("projects.json is not valid UTF-8 text")
        raw_items = _parse(text)
    except ValueError as exc:       # json.JSONDecodeError is a ValueError too
        backup = _backup_bad_file(path)
        # If it couldn't be set aside, it must not be saved over either.
        kind = "damaged" if backup else "unreadable"
        return [], {"kind": kind, "path": path, "backup": backup, "error": str(exc)}

    items = []
    seen_ids = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        project = normalise_project(raw)
        if project["id"] in seen_ids:  # hand-edited file with a repeated id
            project["id"] = new_id()
        seen_ids.add(project["id"])
        items.append(project)
    return items, None


def load_projects():
    """All saved projects, with missing settings filled in. Never raises.

    The same as load_projects_report() without the problem report.
    """
    return load_projects_report()[0]


def save_projects(items):
    """Write the project list to projects.json, safely.

    The list is written to a temporary file first and then swapped in, so a
    crash or power cut can never leave a half-written projects.json.
    Raises OSError if the file can't be written.
    """
    path = str(paths.projects_file())
    data = {"version": FILE_VERSION, "projects": [dict(p) for p in items]}
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False)
    tmp = "%s.tmp-%d" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _replace_with_retry(src, dst, attempts=5):
    """os.replace, retried briefly: on Windows a virus scanner or sync tool can
    hold the file open for a moment."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1 * (attempt + 1))


# --------------------------------------------------------------------------
# The last run
# --------------------------------------------------------------------------

def last_run_from_result(result):
    """The part of a RunResult that is saved with the project as ``last_run``."""
    keep = ("finished_at", "elapsed_s", "output_folder", "log_path", "files_found",
            "files_read", "from_cache", "stats")
    saved = dict((k, copy.deepcopy(result.get(k))) for k in keep)
    saved["files"] = [dict(f) for f in result.get("files") or []]
    from . import engine   # (already loaded: a run has just finished)
    failed = result.get("failed") or []
    saved["failed_count"] = len(failed)        # files and folders
    saved["failed_folders"] = sum(1 for item in failed if engine.is_folder_problem(item[1]))
    # Documents that couldn't be read, and documents folders that couldn't be
    # opened (counts only, like the failed list).
    doc_problems = result.get("doc_problems") or []
    saved["doc_problem_count"] = len(doc_problems)
    saved["doc_problem_folders"] = sum(1 for item in doc_problems
                                       if engine.is_doc_folder_problem(item[1]))
    return saved


def record_last_run(project_id, result):
    """Save a finished run as the project's ``last_run``, for runs made without
    the window (the command line). Returns (saved, note); ``note`` says why
    nothing was saved ('' when there is nothing worth saying).

    Like the window, only a run that wrote files is saved. Nothing is saved
    while a Squish window is open (it holds the project list and would save
    over this change), nor over a project list that couldn't be read.
    """
    if not (result or {}).get("files"):
        return False, ""
    lock = take_window_lock()
    if lock is None:
        return False, ("The open Squish window will show this run's files only after the "
                       "project is squished there again.")
    try:
        items, problem = load_projects_report()
        if problem is not None:
            return False, "The project list couldn't be read, so this run wasn't recorded in it."
        project = find_project(items, project_id)
        if project is None:
            return False, ""
        project["last_run"] = last_run_from_result(result)
        try:
            save_projects(items)
        except OSError as exc:
            return False, "Couldn't record this run in the project list: %s" % (
                exc.strerror or exc)
        return True, ""
    finally:
        lock.release()


# --------------------------------------------------------------------------
# One Squish window at a time
# --------------------------------------------------------------------------

class WindowLock(object):
    """An operating-system lock on <data folder>/squish-window.lock.

    Held for as long as a Squish window is open. The system frees it when the
    process ends, even after a crash, so a leftover lock file never blocks
    anything. ``fd`` is None when the lock file couldn't be made at all.
    """

    def __init__(self, fd):
        self.fd = fd

    def release(self):
        """Free the lock (safe to call more than once)."""
        if self.fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(self.fd, 0, 0)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = None


def take_window_lock():
    """Lock the project list for this Squish window (only one window may edit it).

    Returns a WindowLock (keep it until the window closes), or None if another
    Squish window already holds the lock. If the lock file can't be made (a
    read-only folder, say) Squish opens anyway: the result is a WindowLock
    that holds nothing.
    """
    path = os.path.join(str(paths.data_dir()), WINDOW_LOCK_NAME)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return WindowLock(None)
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)   # NBLCK: don't wait (LK_LOCK retries for 10 s)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # flock: also conflicts within one process
    except OSError:
        os.close(fd)
        return None
    return WindowLock(fd)
