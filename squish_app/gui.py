"""The Squish window (tkinter + ttk).

Left: the saved projects. Right: the selected project's settings (saved
automatically), the big Squish! button with progress, and the files it made.

The real work (engine.run_project) runs in a background thread. Threads never
touch the window: they put messages on a queue, and the window reads the queue
every 100 ms (SquishApp.poll_queue). So the window never freezes, even on a
slow network drive.
"""

import copy
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

from . import __version__, paths, projects
from .digest import PART_SIZES, SQUEEZE_LEVELS

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"

POLL_MS = 100            # how often the window checks for news from background threads
AUTOSAVE_MS = 500        # settings are saved this long after the last change
SAVE_RETRY_MS = 5000     # after a failed save, try again this often
COUNT_DELAY_MS = 400     # wait this long after typing a folder before counting its emails
CLOSE_WAIT_S = 120       # closing during a run: wait this long for it to stop cleanly
CLICK_GAP_S = 0.8        # a button's second click within this time is the rest of a double-click
QUICK_CLOSE_S = 3.0      # closing otherwise: a moment for helper threads to finish
CREATE_NO_WINDOW = 0x08000000

COLOURS = {
    "accent": "#4B34B8",        # the Squish! button and selections (purple from the icon)
    "accent_active": "#3B2A8C",
    "cancel": "#5B6472",
    "cancel_active": "#454C57",
    "on_accent": "#FFFFFF",
    "header_bg": "#FFFFFF",
    "body_bg": "#F3F4F6",       # used with the 'clam' theme (Linux)
    "field_bg": "#FFFFFF",
    "line": "#D5D9E0",
    "text": "#1F2933",
    "hint": "#5F6B7A",
    "error": "#B3261E",
    "warn": "#8A5300",
    "good": "#1E7B34",
}

TAGLINE = "Squash a folder of emails into one file you can drop into Claude"
PASTE_KEY = "Cmd+V" if IS_MAC else "Ctrl+V"
TIP = ("Click Show in folder, then drag the file into Claude - or click Copy file, then press %s "
       "in Claude." % PASTE_KEY)
TIP_NO_COPY = ("Click Show in folder, then drag the file into Claude - or use Copy text and "
               "paste it.")
LONG_PATH_ADVICE = ("This file's full path is too long for Windows Explorer - use Copy text, "
                    "or pick a shorter 'Save digests to' folder.")
ONE_CHAT_TOKENS = 150000  # several files this small together still fit in one Claude chat
EXPLORER_PATH_LIMIT = 260  # File Explorer and drag-and-drop can't handle longer paths

DEFAULT_NAME_RE = re.compile(r"^%s( \d+)?$" % re.escape(projects.DEFAULT_NAME), re.I)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# The ' (only <dates>)' tag a dated run adds to its file names (see engine.output_filenames)
DATES_TAG = (r" \(only (?:\d{4}-\d{2}-\d{2} to \d{4}-\d{2}-\d{2}|from \d{4}-\d{2}-\d{2}"
             r"|up to \d{4}-\d{2}-\d{2})\)")
OUTPUT_NAME_RE = re.compile(  # see engine.output_filenames
    r"^Squish - .+? - (?=(\d{4}-\d{2}-\d{2} to \d{4}-\d{2}-\d{2}|undated)"
    r"(%s)?( \(focus[^()]*\))?( \(part \d+ of \d+\))?\.txt$)" % DATES_TAG, re.I)
GENERIC_FOLDER_RE = paths.GENERIC_FOLDER_RE  # '01 Emails', 'Correspondence' ...

HOW_TO = """How to use Squish

1. Make a project
   Click New (or press Ctrl+N) and give it a name - for example the job name.

2. Point it at the emails
   Click Browse... next to "Emails folder" and choose the folder where the
   project's emails are filed: a folder of emails, or a folder of folders.
   (Or paste the folder's address from File Explorer, or a file:// link.)
   Squish only reads this folder. It never changes, moves or deletes emails.

3. Squish!
   Click Squish! (or press F5). Squish reads every email, cuts out quoted
   history, signatures and disclaimers, and writes one compact text file
   (or a few, if there is a lot of email).

4. Give it to Claude
   Click "Show in folder" and drag the file into Claude, or click
   "Copy file" and press Ctrl+V in Claude's message box. If there are
   several files, Squish says whether they fit in one Claude chat; if not,
   start a new chat for each one (the Dates column shows what each file
   covers). Then ask, e.g.:
     "List every open action or request, who owes it, and since when."
     "Summarise what was agreed about <topic>, with dates."

Tips
 - Only need a period? Fill in the From / To dates (Emails tab), or point
   Squish at a smaller folder (one month, one subject). A dated run is saved
   as its own file, whose name ends "(only <dates>)", so the all-dates
   digest is kept.
 - Only need one topic? Add focus keywords (Squeeze tab), e.g. culvert,
   RFI 12. The file name then says "(focus ...)", and the all-dates digest
   is kept.
 - Dates and focus keywords stay set for the project until you clear them.
   Clear them and click Squish! to bring the full digest up to date.
 - Claude says the file is too big? Choose Squeeze "Max", or File size
   "Small" and start a new Claude chat for each file (or drag in only the
   ones you need).
   Tokens are how Claude measures text, roughly 3.5 characters each; one
   Claude chat holds about 150k tokens.
 - Squish remembers emails it has already read, so the next run is quicker.
 - Settings save automatically. Deleting a project never deletes emails or
   digest files.
"""

# --------------------------------------------------------------------------
# Small helpers (no tkinter)
# --------------------------------------------------------------------------

def squeeze_options():
    """[(key, label, description)] from digest.SQUEEZE_LEVELS."""
    return [(k, v["label"], v.get("description", "")) for k, v in SQUEEZE_LEVELS.items()]


def part_size_options():
    """[(key, label, description)] from digest.PART_SIZES."""
    return [(k, v["label"], v.get("description", "")) for k, v in PART_SIZES.items()]


def fmt_int(n):
    return format(int(n or 0), ",")


def fmt_tokens(n):
    n = int(n or 0)
    if n >= 1000:
        return "%sk" % fmt_int(round(n / 1000.0))
    return str(n)


def plural(n, word, many=None):
    return "%s %s" % (fmt_int(n), word if n == 1 else (many or word + "s"))


def short_name(name, limit=30):
    """A project name cut to ``limit`` characters ('Riverside Depot Stormwat…'),
    so a message that names the project still fits on the one status line."""
    if len(name) <= limit:
        return name
    return name[:limit - 2].rstrip() + "…"


def date_problem(text):
    """'' if ``text`` is blank or a real YYYY-MM-DD date, else a short explanation."""
    text = (text or "").strip()
    if not text:
        return ""
    if not DATE_RE.match(text):
        return "Use year-month-day, e.g. 2025-01-31"
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return "%s isn't a real date" % text
    return ""


def org_codes_problem(text):
    """'' if every line looks like domain=CODE, else a short explanation."""
    for raw in re.split(r"[\n;,]+", text or ""):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        domain, _, code = line.partition("=")
        domain = domain.strip().lstrip("@")
        if not code.strip() or "." not in domain or " " in domain:
            return "\"%s\" should look like domain=CODE, e.g. example.com=EX" % line
    return ""


def guess_project_name(folder):
    """A project name from a folder path, skipping generic names like '01 Emails'.

    The folder name is kept whole, so a job number at its start stays in the
    name ('1234 Riverside Depot Upgrade'); the number is only ignored when
    deciding whether the name is generic.
    """
    parts = [p for p in re.split(r"[\\/]+", folder or "") if p and not p.endswith(":")]
    for part in reversed(parts[-4:]):
        plain = re.sub(r"^[\d\s._-]+", "", part).strip()
        if plain and not GENERIC_FOLDER_RE.match(plain):
            return part.strip()[:60].rstrip()
    return ""


def is_email_name(name):
    """Same rule as engine.is_email_file: .msg/.eml but not Office '~$' temp files."""
    return name.lower().endswith((".msg", ".eml")) and not name.startswith("~$")


def count_email_files(folder, include_subfolders, stop, report, skipped=None):
    """Count the email files in ``folder``. Calls report(count) now and then.

    Returns the count, or None if ``stop`` was set. Raises OSError if the
    folder itself can't be opened. Subfolders that can't be opened are left
    out of the count and added to ``skipped`` (a list), if given. Runs in a
    background thread (no tkinter here).
    """
    root = paths.long_path(folder)
    if not os.path.isdir(root):
        raise FileNotFoundError(folder)
    count = 0
    last = time.monotonic()
    if include_subfolders:
        top = os.path.normcase(os.path.abspath(root))

        def on_walk_error(err):
            # os.walk would quietly skip the folder itself too: report that one.
            failed = getattr(err, "filename", None) or ""
            if os.path.normcase(os.path.abspath(failed)) == top:
                raise err
            if skipped is not None:
                skipped.append(paths.short_path(failed))

        for _dirpath, _dirnames, filenames in os.walk(root, onerror=on_walk_error):
            if stop.is_set():
                return None
            count += sum(1 for n in filenames if is_email_name(n))
            if time.monotonic() - last > 0.3:
                report(count)
                last = time.monotonic()
    else:
        with os.scandir(root) as entries:
            for entry in entries:
                if stop.is_set():
                    return None
                if is_email_name(entry.name):
                    count += 1
    return count


def fit_one_chat(files, part_size=None):
    """True if these digest files together are small enough for one Claude chat.

    A Small run never counts as one chat: people choose Small when Claude said
    the file was too big.
    """
    if part_size == "small":
        return False
    return sum(int(f.get("est_tokens") or 0) for f in files) <= ONE_CHAT_TOKENS


def done_message(elapsed_s, files, can_copy, other_project="", filters="", part_size=None):
    """The status line after a run that wrote ``files``: what to do next.

    It carries the same advice as the tip under the table (which the compact
    layout leaves out). ``other_project`` names the run's project when another
    one is on screen; ``filters`` is filter_summary() of the run's settings;
    ``part_size`` is the run's File size setting (see fit_one_chat).
    """
    text = "Done in %.0f s - " % (elapsed_s or 0)
    if len(files) == 1:
        text += "1 file ready. Click Show in folder and drag it into Claude"
        if can_copy and not (other_project or filters):
            text += " (or Copy file, then %s)" % PASTE_KEY
    elif fit_one_chat(files, part_size):
        text += ("%d files ready - they fit in one Claude chat. Click Show in folder and drag "
                 "them in" % len(files))
    else:
        text += ("%d files ready - use a new Claude chat for each file. Click Show in folder, "
                 "then drag one in" % len(files))
    if filters:
        text += " - %s" % filters
    if other_project:
        text += " (project \"%s\")" % other_project
    return text + "."


def keyword_words(text):
    """The focus keywords as a list (split like cleaning.keyword_list: commas,
    semicolons and new lines), as typed."""
    return [w.strip() for w in re.split(r"[,;\n\r]+", text or "") if w.strip()]


def filter_summary(values):
    """'' when a project (or the form's values) has no date or keyword filter, else
    e.g. 'only 2025-02-01 to 2025-02-28 and only conversations mentioning pump, culvert'."""
    date_from = (values.get("date_from") or "").strip()
    date_to = (values.get("date_to") or "").strip()
    words = keyword_words(values.get("focus_keywords"))
    bits = []
    if date_from and date_to:
        bits.append("only %s to %s" % (date_from, date_to))
    elif date_from:
        bits.append("only from %s" % date_from)
    elif date_to:
        bits.append("only up to %s" % date_to)
    if words:
        bits.append("only conversations mentioning %s" % ", ".join(words))
    return " and ".join(bits)


def split_status(text):
    """(line shown, details) for a status message.

    Only the first line is shown under the Squish! button (more lines would take
    the room the Digest files table needs); the whole message is kept as the
    details, or '' when there is nothing more to see. A first line ending in ':'
    (e.g. "Can't find the email folder:" then the path) loses the colon.
    """
    text = (text or "").strip()
    first, _, rest = text.partition("\n")
    first = " ".join(first.split())
    if not rest.strip():
        return first, ""
    return first.rstrip(":").rstrip(), text


def results_tip(files, can_copy, part_size=None):
    """The hint under the Digest files table: how to hand the file(s) to Claude.
    ``part_size``: the run's File size setting (see fit_one_chat)."""
    if len(files) <= 1:
        return TIP if can_copy else TIP_NO_COPY
    how = ("click Show in folder and drag a file into Claude, or click Copy file then press %s"
           % PASTE_KEY if can_copy else
           "click Show in folder and drag a file into Claude, or use Copy text and paste it")
    start = ("%d files, oldest conversations first (the Dates column shows what each covers). "
             % len(files))
    if fit_one_chat(files, part_size):
        return start + ("Together they are small enough for one Claude chat: %s, one after the "
                        "other." % (how[0].lower() + how[1:]))
    if part_size == "small":
        return start + ("Start a new Claude chat for each file, or drag in only the ones you "
                        "need. To use one, %s." % how)
    return start + ("Start a new Claude chat for each file - together they are too big for one "
                    "chat. To use one, %s." % how)


def path_too_long_for_explorer(path):
    """True on Windows if File Explorer (and dragging from it) can't handle ``path``."""
    return IS_WINDOWS and len(os.path.abspath(paths.short_path(path))) >= EXPLORER_PATH_LIMIT


def fit_middle(text, measure, width):
    """``text`` shortened in the middle ('Riverside Depot…Stage 2') so that
    measure(text) <= width. ``measure`` gives a text's width in pixels."""
    full = measure(text)
    if width <= 0 or full <= width:
        return text
    keep = min(len(text) - 1, int(len(text) * width / float(full)) + 1)  # a first guess
    while keep > 1:
        short = text[:(keep + 1) // 2].rstrip() + "\u2026" + text[len(text) - keep // 2:].lstrip()
        if measure(short) <= width:
            return short
        keep -= 1
    return text[:1] + "\u2026"


def short_file_name(path):
    """The digest file name without the 'Squish - <project> - ' start (the table is per project)."""
    name = os.path.basename(path)
    short = OUTPUT_NAME_RE.sub("", name)
    return short if short != name else name


# A dated ' (only ...)' and/or ' (focus ...)' run's file name (see engine.output_filenames)
FILTER_TAG_RE = re.compile(r"(%s( \(focus[^()]*\))?| \(focus[^()]*\))( \(part \d+ of \d+\))?"
                           r"\.txt$" % DATES_TAG, re.I)


def run_was_filtered(result):
    """True if a run (or saved last_run) used focus keywords or left out dates, so
    its files are not the all-dates digest."""
    if (result.get("stats") or {}).get("outside_dates"):
        return True
    return any(FILTER_TAG_RE.search(os.path.basename(f.get("path") or ""))
               for f in result.get("files") or [])


# The part of a RunResult saved with the project (lives in projects.py so the
# command line can save it too without loading the window code).
last_run_from_result = projects.last_run_from_result


def summary_text(result):
    """One or two plain-English sentences describing a run."""
    stats = result.get("stats") or {}
    files = result.get("files") or []
    tokens = sum(int(f.get("est_tokens") or 0) for f in files)
    found = result.get("files_found") or stats.get("emails_in") or 0
    if files:
        text = "%s → %s used in %s, %s, ~%s tokens." % (
            plural(found, "email"), fmt_int(stats.get("emails_used", 0)),
            plural(stats.get("threads", 0), "thread"), plural(len(files), "file"),
            fmt_tokens(tokens))
    else:
        text = ("%s read, but none were left to write - check the dates and focus keywords."
                % plural(found, "email"))
    skipped = []
    if stats.get("duplicates"):
        skipped.append(plural(stats["duplicates"], "duplicate"))
    if stats.get("noise_dropped"):
        n = stats["noise_dropped"]
        skipped.append("%s auto-repl%s or notification%s" % (
            fmt_int(n), "y" if n == 1 else "ies", "" if n == 1 else "s"))
    if stats.get("acks_dropped"):
        skipped.append("%s short thank-yous" % fmt_int(stats["acks_dropped"]))
    if stats.get("outside_dates"):
        skipped.append("%s outside the dates" % fmt_int(stats["outside_dates"]))
    if stats.get("filtered_out"):
        skipped.append("%s without the focus keywords" % fmt_int(stats["filtered_out"]))
    if skipped:
        joined = skipped[0] if len(skipped) == 1 else ", ".join(skipped[:-1]) + " and " + skipped[-1]
        text += " %s skipped." % joined
    files_failed, folders_failed = failure_counts(result)
    if files_failed:
        text += " %s couldn't be read (see run log)." % plural(files_failed, "file")
    if folders_failed:
        text += " %s couldn't be opened (see run log)." % plural(folders_failed, "folder")
    if stats.get("no_access_emails"):
        text += " %s in a folder Squish can't open %s left out (see run log)." % (
            plural(stats["no_access_emails"], "email"),
            "is" if stats["no_access_emails"] == 1 else "are")
    return text


def failure_counts(result):
    """(email files that couldn't be read, folders that couldn't be opened) for a
    RunResult or a saved last_run (which keeps only the counts)."""
    failed = result.get("failed")
    if failed is None:
        total = int(result.get("failed_count") or 0)
        folders = min(total, int(result.get("failed_folders") or 0))
        return total - folders, folders
    from . import engine
    folders = sum(1 for item in failed if engine.is_folder_problem(item[1]))
    return len(failed) - folders, folders


def output_folder_problem(source, out_dir, resolve_links=False):
    """A message when the output folder is the emails folder or inside it, else ''.

    The folders are compared only as typed: resolving links opens both paths,
    which can hang for a long time on a disconnected network drive, so the
    window never does it. The engine repeats the check with links resolved in
    the run's own thread, before anything is written."""
    from . import engine
    if engine.output_inside_source(source, out_dir, resolve_links=resolve_links):
        return ("This folder is inside the emails folder. Squish never writes into the email "
                "folders - choose an output folder somewhere else (or leave it blank).")
    return ""


def open_with_system(path):
    """Open a file or folder with its usual program. Raises OSError on failure."""
    if IS_WINDOWS:
        os.startfile(path)  # noqa - Windows only
    elif IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def show_in_file_manager(path):
    """Open the folder window with ``path`` selected (just the folder on Linux)."""
    if IS_WINDOWS:
        subprocess.Popen('explorer /select,"%s"' % os.path.normpath(path))
    elif IS_MAC:
        subprocess.Popen(["open", "-R", path])
    else:
        open_with_system(os.path.dirname(path))


def can_copy_files():
    return IS_WINDOWS or IS_MAC


def copy_file_to_clipboard(path):
    """Put the file itself on the clipboard (for Ctrl+V into Claude). Returns (ok, message).

    Slow (starts PowerShell), so call it from a background thread.
    """
    name = os.path.basename(path)
    try:
        if IS_WINDOWS:
            from .shortcut import powershell_exe
            # The path is passed in an environment variable, never pasted into the command.
            command = [powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                       "Bypass", "-Command", "Set-Clipboard -LiteralPath $env:SQUISH_CLIP_PATH"]
            env = dict(os.environ)
            env["SQUISH_CLIP_PATH"] = path
            done = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=30, creationflags=CREATE_NO_WINDOW)
            if done.returncode != 0:
                lines = [line.strip() for line in done.stderr.decode("utf-8", "replace").splitlines()
                         if line.strip() and not line.startswith("#<")]
                return False, "Couldn't copy the file (%s). Use Show in folder and drag it instead." % (
                    lines[0] if lines else "exit code %d" % done.returncode)
        elif IS_MAC:
            done = subprocess.run(
                ["osascript", "-e", "on run argv", "-e",
                 "set the clipboard to (POSIX file (item 1 of argv))", "-e", "end run", path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
            if done.returncode != 0:
                return False, "Couldn't copy the file. Use Show in folder and drag it instead."
        else:
            return False, "Copy file only works on Windows and Mac. Use Show in folder instead."
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "Couldn't copy the file (%s). Use Show in folder and drag it instead." % exc
    return True, "Copied %s - now click in Claude's message box and press %s." % (name, PASTE_KEY)


def run_in_background(run_id, project, cancel, post):
    """Thread body: run the project and post ("done"|"error"|"crash", run_id, ...) messages."""
    def progress(stage, done, total, message):
        post(("progress", run_id, stage, done, total, message))

    try:
        from . import engine
    except Exception:
        post(("crash", run_id, traceback.format_exc()))
        return
    try:
        post(("done", run_id, engine.run_project(project, progress=progress, cancel=cancel)))
    except engine.SquishError as exc:
        # log_path: the run log written before the run stopped ('' if none)
        post(("error", run_id, str(exc), getattr(exc, "log_path", "") or ""))
    except Exception:
        post(("crash", run_id, traceback.format_exc()))


def reader_status():
    """readers.backend_status(), or 'reader unavailable' if the reader can't load."""
    try:
        from . import readers
        return readers.backend_status()
    except Exception:
        return "Outlook .msg: reader unavailable"


def windows_setup():
    """Sharp text on high-DPI screens and the Squish icon on the taskbar (Windows only)."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(paths.APP_ID)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Small windows
# --------------------------------------------------------------------------

class TextWindow(tk.Toplevel):
    """A simple read-only text window (How to use, run log, a message's details).

    ``plain``: a short message (smaller window, no bold headings).
    """

    def __init__(self, parent, title, text, open_path=None, plain=False):
        tk.Toplevel.__init__(self, parent)
        self.title(title)
        self.transient(parent)
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill="both", expand=True)
        body = tk.Text(frame, wrap="word", width=70 if plain else 90, height=12 if plain else 30,
                       relief="flat", borderwidth=0, highlightthickness=1,
                       highlightbackground=COLOURS["line"], padx=10, pady=8,
                       font="TkFixedFont" if open_path else "TkTextFont")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=body.yview)
        body.configure(yscrollcommand=scroll.set)
        body.insert("1.0", text)
        if not open_path and not plain:
            # help text: lines that don't start with a space are headings
            base = tkfont.nametofont("TkTextFont").actual()
            body.tag_configure("heading", font=(base["family"], base["size"], "bold"))
            for number, line in enumerate(text.splitlines(), start=1):
                if line[:1].strip():
                    body.tag_add("heading", "%d.0" % number, "%d.end" % number)
        body.configure(state="disabled")
        body.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, columnspan=2, sticky="e", pady=(10, 0))
        if open_path:
            ttk.Button(buttons, text="Open in editor",
                       command=lambda: self._open(open_path)).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="left")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.bind("<Escape>", lambda e: self.destroy())
        body.focus_set()

    def _open(self, path):
        try:
            open_with_system(path)
        except OSError as exc:
            messagebox.showerror("Squish", "Couldn't open the file:\n%s" % exc, parent=self)


# --------------------------------------------------------------------------
# The settings form
# --------------------------------------------------------------------------

class SettingsForm(ttk.Notebook):
    """Tabs with one project's settings. Calls app.on_setting_changed(field) on each edit."""

    TAB_EMAILS, TAB_SQUEEZE, TAB_ADVANCED = 0, 1, 2

    def __init__(self, parent, app):
        ttk.Notebook.__init__(self, parent)
        self.app = app
        self.px = app.px
        self.loading = False
        self.compact = getattr(app, "compact", False)   # short screen: tighter spacing
        self.wraps = []          # (label, container, margin): labels that wrap to the width
        self.keep_hints = []     # hints shown even in the compact layout (see set_hint)
        self.squeeze_choices = squeeze_options()
        self.size_choices = part_size_options()

        self.name_var = tk.StringVar()
        self.source_var = tk.StringVar()
        self.subfolders_var = tk.BooleanVar(value=True)
        self.output_var = tk.StringVar()
        self.from_var = tk.StringVar()
        self.to_var = tk.StringVar()
        self.squeeze_var = tk.StringVar(value="standard")
        self.size_var = tk.StringVar()
        self.keywords_var = tk.StringVar()
        self.noise_var = tk.BooleanVar(value=True)
        self.recover_var = tk.BooleanVar(value=True)

        self._build_emails_tab()
        self._build_squeeze_tab()
        self._build_advanced_tab()

        watch = [("name", self.name_var), ("source_folder", self.source_var),
                 ("include_subfolders", self.subfolders_var), ("output_folder", self.output_var),
                 ("date_from", self.from_var), ("date_to", self.to_var),
                 ("squeeze", self.squeeze_var), ("part_size", self.size_var),
                 ("focus_keywords", self.keywords_var), ("drop_noise", self.noise_var),
                 ("recover_quoted", self.recover_var)]
        for field, var in watch:
            var.trace_add("write", lambda *_args, f=field: self._changed(f))
        self.org_text.bind("<<Modified>>", self._org_modified)

    # ---- layout helpers ----------------------------------------------------

    def _tab(self, title):
        pad_y = self.px(5 if self.compact else 12)
        frame = ttk.Frame(self, padding=(self.px(14), pad_y))
        frame.columnconfigure(0, minsize=self.px(130))   # same label column on every tab
        frame.columnconfigure(1, weight=1)
        self.add(frame, text="  %s  " % title)
        frame.bind("<Configure>", self._rewrap)
        return frame

    def _wrap(self, label, container, margin):
        """Make ``label`` wrap to the width of ``container`` minus ``margin`` pixels."""
        self.wraps.append((label, container, margin))

    def _label(self, tab, row, text):
        label = ttk.Label(tab, text=text)
        label.grid(row=row, column=0, sticky="nw", padx=(0, self.px(12)), pady=(self.px(5), 0))
        return label

    def _hint(self, tab, row, text="", column=1, columnspan=2, keep=False):
        """A hint line under a setting. ``keep``: shown in the compact layout too."""
        hint = ttk.Label(tab, text=text, style="Hint.TLabel", justify="left")
        hint.grid(row=row, column=column, columnspan=columnspan, sticky="nw",
                  pady=(0, self.px(2)) if self.compact else (self.px(1), self.px(6)))
        self._wrap(hint, tab, self.px(170))
        if keep:
            self.keep_hints.append(hint)
        self.set_hint(hint, text)
        return hint

    def _rewrap(self, event):
        for label, container, margin in self.wraps:
            if container is event.widget:
                label.configure(wraplength=max(event.width - margin, self.px(200)))

    def set_hint(self, label, text, kind="Hint"):
        """Show ``text`` under a setting. ``kind``: Hint (grey), Good, Warn or Error.

        The compact layout (short screens) leaves out plain grey hints, so the
        Digest files table keeps its room; warnings, errors and the email count
        still show.
        """
        label.configure(text=text, style="%s.TLabel" % kind)
        if getattr(self.app, "compact", False) and kind == "Hint" and label not in self.keep_hints:
            label.grid_remove()
        else:
            label.grid()

    # ---- tabs --------------------------------------------------------------

    def _build_emails_tab(self):
        tab = self._tab("Emails")
        px = self.px
        self._label(tab, 0, "Project name")
        self.name_entry = ttk.Entry(tab, textvariable=self.name_var)
        self.name_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(px(3), 0))
        self.name_hint = self._hint(tab, 1)

        self._label(tab, 2, "Emails folder")
        self.source_entry = ttk.Entry(tab, textvariable=self.source_var)
        self.source_entry.grid(row=2, column=1, sticky="ew", pady=(px(3), 0))
        ttk.Button(tab, text="Browse...", command=self.browse_source).grid(
            row=2, column=2, sticky="w", padx=(px(6), 0), pady=(px(3), 0))
        self.source_hint = self._hint(tab, 3, keep=True)   # the live 'N email files found'
        self.source_entry.bind("<FocusOut>", self._source_left)
        self.source_entry.bind("<Return>", self._source_left)
        ttk.Checkbutton(tab, text="Include subfolders (a folder of folders)",
                        variable=self.subfolders_var).grid(
            row=4, column=1, columnspan=2, sticky="w", pady=(0, px(3 if self.compact else 8)))

        self._label(tab, 5, "Save digests to")
        self.output_entry = ttk.Entry(tab, textvariable=self.output_var)
        self.output_entry.grid(row=5, column=1, sticky="ew", pady=(px(3), 0))
        ttk.Button(tab, text="Browse...", command=self.browse_output).grid(
            row=5, column=2, sticky="w", padx=(px(6), 0), pady=(px(3), 0))
        self.output_hint = self._hint(tab, 6)
        self.output_entry.bind("<FocusOut>", lambda e: self._tidy_folder(self.output_var,
                                                                         self.output_entry))
        for entry in (self.source_entry, self.output_entry):
            entry.bind("<Configure>", self._folder_box_resized, add="+")

        self._label(tab, 7, "Dates (optional)")
        dates = ttk.Frame(tab)
        dates.grid(row=7, column=1, columnspan=2, sticky="w", pady=(px(3), 0))
        ttk.Label(dates, text="From").pack(side="left")
        self.from_entry = ttk.Entry(dates, textvariable=self.from_var, width=12)
        self.from_entry.pack(side="left", padx=(px(6), px(12)))
        ttk.Label(dates, text="To").pack(side="left")
        self.to_entry = ttk.Entry(dates, textvariable=self.to_var, width=12)
        self.to_entry.pack(side="left", padx=(px(6), px(12)))
        ttk.Label(dates, text="YYYY-MM-DD", style="Hint.TLabel").pack(side="left")
        self.dates_hint = self._hint(tab, 8)

    def _build_squeeze_tab(self):
        tab = self._tab("Squeeze")
        px = self.px
        self._label(tab, 0, "Squeeze level")
        levels = ttk.Frame(tab)
        levels.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(px(3), px(4)))
        levels.columnconfigure(0, weight=1)
        row = 0
        for key, label, description in self.squeeze_choices:
            ttk.Radiobutton(levels, text=label, value=key, variable=self.squeeze_var).grid(
                row=row, column=0, sticky="w")
            note = ttk.Label(levels, text=description, style="Hint.TLabel", justify="left")
            note.grid(row=row + 1, column=0, sticky="w", padx=(px(24), 0), pady=(0, px(3)))
            self._wrap(note, levels, px(30))
            row += 2
        levels.bind("<Configure>", self._rewrap)

        self._label(tab, 1, "File size")
        self.size_combo = ttk.Combobox(tab, textvariable=self.size_var, state="readonly",
                                       values=[label for _, label, _ in self.size_choices],
                                       width=44)
        self.size_combo.grid(row=1, column=1, sticky="w", pady=(px(3), 0))
        self.size_hint = self._hint(tab, 2)

        self._label(tab, 3, "Focus keywords")
        self.keywords_entry = ttk.Entry(tab, textvariable=self.keywords_var)
        self.keywords_entry.grid(row=3, column=1, columnspan=2, sticky="ew", pady=(px(3), 0))
        self._hint(tab, 4, "Optional. Keep only conversations that mention any of these "
                           "words, e.g. culvert, pump station, RFI 12 (RFI 12 also finds "
                           "RFI-012, RFI_12 and RFI #12). They stay set for this project until "
                           "you clear them.")

    def _build_advanced_tab(self):
        tab = self._tab("Who's who & clean-up")
        px = self.px
        self._label(tab, 0, "Who's who")
        holder = ttk.Frame(tab)
        holder.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(px(3), 0))
        holder.columnconfigure(0, weight=1)
        self.org_text = tk.Text(holder, height=4, width=40, wrap="none", undo=True,
                                font="TkTextFont", relief="flat", borderwidth=0,
                                highlightthickness=1, padx=px(6), pady=px(4),
                                highlightbackground=COLOURS["line"],
                                highlightcolor=COLOURS["accent"],
                                background=COLOURS["field_bg"], foreground=COLOURS["text"],
                                insertbackground=COLOURS["text"])
        self.org_text.grid(row=0, column=0, sticky="ew")
        self.org_text.bind("<Control-Return>", self._run_from_text)
        self.org_hint = self._hint(tab, 1)

        self._label(tab, 2, "Clean-up")
        ttk.Checkbutton(tab, text="Drop noise", variable=self.noise_var).grid(
            row=2, column=1, columnspan=2, sticky="w", pady=(px(5), 0))
        self._hint(tab, 3, "Skip out-of-office replies, meeting accepts/declines, read receipts "
                           "and system notifications.")
        ttk.Checkbutton(tab, text="Recover quoted emails", variable=self.recover_var).grid(
            row=4, column=1, columnspan=2, sticky="w")
        self._hint(tab, 5, "Pick up earlier emails that only appear inside someone's reply or "
                           "forward (they weren't filed on their own).")

    # ---- reading and writing values -----------------------------------------

    def load(self, project):
        """Show ``project`` in the form (without triggering autosave)."""
        self.loading = True
        try:
            self.name_var.set(project.get("name", ""))
            self.source_var.set(project.get("source_folder", ""))
            self.subfolders_var.set(bool(project.get("include_subfolders", True)))
            self.output_var.set(project.get("output_folder", ""))
            self.from_var.set(project.get("date_from", ""))
            self.to_var.set(project.get("date_to", ""))
            self.squeeze_var.set(project.get("squeeze", "standard"))
            self.size_var.set(self._size_label(project.get("part_size", "medium")))
            self.keywords_var.set(project.get("focus_keywords", ""))
            self.noise_var.set(bool(project.get("drop_noise", True)))
            self.recover_var.set(bool(project.get("recover_quoted", True)))
            self.org_text.delete("1.0", "end")
            self.org_text.insert("1.0", project.get("org_codes", ""))
            self.org_text.edit_reset()
            self.org_text.edit_modified(False)
        finally:
            self.loading = False
        self.refresh_hints()
        self.show_folder_ends()

    def show_folder_ends(self):
        """Scroll the folder boxes to the end of their paths: the last folder names
        ('...\\04 Correspondence\\01 Emails') say which folder was picked."""
        for entry in (self.source_entry, self.output_entry):
            entry.icursor("end")
            entry.xview_moveto(1.0)

    def _folder_box_resized(self, event):
        """Keep showing the end of a folder path when its box is laid out or resized
        (unless the user is typing in it)."""
        try:
            typing = self.focus_get() is event.widget
        except (KeyError, tk.TclError):
            typing = False
        if not typing:
            event.widget.xview_moveto(1.0)

    def values(self):
        """The settings as they are in the form (a partial project dict)."""
        return {
            "name": self.name_var.get().strip(),
            # without the quotes Explorer's 'Copy as path' adds
            "source_folder": paths.clean_folder_text(self.source_var.get()),
            "include_subfolders": bool(self.subfolders_var.get()),
            "output_folder": paths.clean_folder_text(self.output_var.get()),
            "date_from": self.from_var.get().strip(),
            "date_to": self.to_var.get().strip(),
            "squeeze": self.squeeze_var.get() or "standard",
            "part_size": self._size_key(self.size_var.get()),
            "focus_keywords": self.keywords_var.get().strip(),
            "org_codes": self.org_text.get("1.0", "end").strip(),
            "drop_noise": bool(self.noise_var.get()),
            "recover_quoted": bool(self.recover_var.get()),
        }

    @staticmethod
    def _tidy_folder(var, entry=None):
        """Show a pasted path without its quotes once the user leaves the box
        (scrolled to its end, where the folder names that matter are)."""
        tidy = paths.clean_folder_text(var.get())
        if tidy != var.get():
            var.set(tidy)
        if entry is not None:
            entry.xview_moveto(1.0)

    def _source_left(self, _event=None):
        """The user has finished typing or pasting the emails folder."""
        self._tidy_folder(self.source_var, self.source_entry)
        self.maybe_guess_name(self.source_var.get())

    def maybe_guess_name(self, folder, select=False):
        """Name a project still called 'New project' after its emails folder.

        A name the user typed is never replaced. ``select``: select the new
        name, so the next key press replaces all of it (after Browse).
        """
        if not DEFAULT_NAME_RE.match(self.name_var.get().strip() or projects.DEFAULT_NAME):
            return False
        guess = guess_project_name(paths.clean_folder_text(folder))
        if not guess or self.app.name_taken(guess):
            return False
        self.name_var.set(guess)
        if select:
            self.name_entry.selection_range(0, "end")
            self.name_entry.icursor("end")
        return True

    def _size_label(self, key):
        for k, label, _ in self.size_choices:
            if k == key:
                return label
        return self.size_choices[1][1] if len(self.size_choices) > 1 else ""

    def _size_key(self, label):
        for key, l, _ in self.size_choices:
            if l == label:
                return key
        return "medium"

    def _changed(self, field):
        if self.loading:
            return
        self.refresh_hints(field)
        self.app.on_setting_changed(field)

    def _org_modified(self, _event=None):
        if not self.org_text.edit_modified():
            return
        self.org_text.edit_modified(False)
        self._changed("org_codes")

    def _run_from_text(self, _event):
        self.app.start_run()
        return "break"

    # ---- hints and validation ------------------------------------------------

    def refresh_hints(self, field=None):
        """Update the grey/red hint lines under the fields."""
        v = self.values()
        if field in (None, "name"):
            if not v["name"]:
                self.set_hint(self.name_hint, "Give the project a name.", "Error")
            elif self.app.name_taken(v["name"]):
                self.set_hint(self.name_hint, "Another project already has this name, or one that "
                              "gives the same file name (Squish leaves out : / \\ * ? \" < > | "
                              "and trailing dots, and uses only the first 80 characters). Give "
                              "each project its own name so their files don't get mixed up.",
                              "Error")
            else:
                self.set_hint(self.name_hint, "Used in the digest file names.")
        if field in (None, "name", "output_folder", "source_folder"):
            problem = output_folder_problem(v["source_folder"], v["output_folder"])
            if problem:
                self.set_hint(self.output_hint, problem, "Error")
            elif v["output_folder"]:
                self.set_hint(self.output_hint, "The digest files are saved in this folder.")
            else:
                default = paths.default_output_folder(v["name"] or projects.DEFAULT_NAME)
                self.set_hint(self.output_hint, "Leave blank to use %s" % default)
        if field in (None, "date_from", "date_to", "focus_keywords"):
            # The Emails tab (the one Squish opens on) says when a filter is on,
            # so a one-off topic or date run doesn't quietly become the normal run.
            problem = date_problem(v["date_from"]) or date_problem(v["date_to"])
            if not problem and v["date_from"] and v["date_to"] and v["date_from"] > v["date_to"]:
                problem = "The From date is after the To date."
            words = keyword_words(v["focus_keywords"])
            if problem:
                self.set_hint(self.dates_hint, problem, "Error")
            elif words:
                self.set_hint(self.dates_hint, "Focus keywords are on (Squeeze tab): %s - each "
                              "Squish! makes only that topic's file. Clear them for the full "
                              "digest." % ", ".join(words), "Warn")
            elif v["date_from"] or v["date_to"]:
                self.set_hint(self.dates_hint, "Only these dates are squished, into a file whose "
                              "name ends \"(only <dates>)\". Clear both boxes for the full "
                              "digest; it is kept meanwhile.")
            else:
                self.set_hint(self.dates_hint, "Leave blank for all dates. To pick one topic, add "
                              "Focus keywords (Squeeze tab). A dated or keyword run is saved as "
                              "its own file; the all-dates digest is kept.")
        if field in (None, "part_size"):
            key = v["part_size"]
            text = [d for k, _, d in self.size_choices if k == key]
            self.set_hint(self.size_hint, (text[0] if text else "") +
                          " Too big for Claude? Choose Small and use one chat per file.")
        if field in (None, "org_codes"):
            problem = org_codes_problem(v["org_codes"])
            if problem:
                self.set_hint(self.org_hint, problem, "Warn")
            else:
                self.set_hint(self.org_hint, "One line per organisation: email domain = short code, "
                              "e.g. slrconsulting.com=SLR. People then show as CODE.Initials "
                              "(SLR.AB). Other organisations get a code from their email "
                              "domain automatically.")

    def blocking_problem(self):
        """(tab index, widget, message) for the first setting that must be fixed, or None."""
        v = self.values()
        if not v["name"]:
            return self.TAB_EMAILS, self.name_entry, "Give the project a name."
        if self.app.name_taken(v["name"]):
            return (self.TAB_EMAILS, self.name_entry,
                    "Another project already has this name (or the same file name).")
        if not v["source_folder"]:
            self.set_hint(self.source_hint, "Choose the folder that holds this project's emails.",
                          "Error")
            return self.TAB_EMAILS, self.source_entry, "Choose the emails folder first."
        # As typed only: never touch a (possibly disconnected) network drive on the
        # window's thread. The run checks again with links resolved.
        problem = output_folder_problem(v["source_folder"], v["output_folder"],
                                        resolve_links=False)
        if problem:
            self.set_hint(self.output_hint, problem, "Error")
            return (self.TAB_EMAILS, self.output_entry,
                    "The output folder is inside the emails folder - choose another one.")
        for var, entry in ((v["date_from"], self.from_entry), (v["date_to"], self.to_entry)):
            if date_problem(var):
                return self.TAB_EMAILS, entry, "Check the dates: %s." % date_problem(var)
        if v["date_from"] and v["date_to"] and v["date_from"] > v["date_to"]:
            return self.TAB_EMAILS, self.from_entry, "The From date is after the To date."
        return None

    # ---- browse buttons ------------------------------------------------------

    def browse_source(self):
        folder = filedialog.askdirectory(
            parent=self, title="Choose the folder that holds the project's emails",
            initialdir=paths.clean_folder_text(self.source_var.get()) or None, mustexist=True)
        if not folder:
            return
        folder = os.path.normpath(folder)
        # New project selected the old name; select the new one instead, so the
        # next key press replaces all of it, not just part of it.
        self.maybe_guess_name(folder, select=True)
        self.source_var.set(folder)
        self.show_folder_ends()

    def browse_output(self):
        current = paths.clean_folder_text(self.output_var.get())
        if not current:
            current = str(paths.documents_dir())
        folder = filedialog.askdirectory(parent=self, title="Choose where to save the digests",
                                         initialdir=current)
        if folder:
            self.output_var.set(os.path.normpath(folder))
            self.show_folder_ends()


# --------------------------------------------------------------------------
# The results table
# --------------------------------------------------------------------------

class ResultsPanel(ttk.LabelFrame):
    """The files made by the last run, with buttons to hand them to Claude."""

    COLUMNS = (("file", "File", 220, True), ("size", "Size (KB)", 72, False),
               ("tokens", "~Tokens", 70, False), ("dates", "Dates", 195, False),
               ("emails", "Emails", 60, False))

    def __init__(self, parent, app):
        compact = getattr(app, "compact", False)       # short screen: tighter spacing
        ttk.LabelFrame.__init__(self, parent, text=" Digest files ",
                                padding=(app.px(10), app.px(3 if compact else 6)))
        self.app = app
        px = app.px
        self.files = []
        self.output_folder = ""
        self.log_path = ""

        self.summary = ttk.Label(self, text="", justify="left")
        self.summary.grid(row=0, column=0, columnspan=2, sticky="ew",
                          pady=(0, px(3 if compact else 6)))

        self.tree = ttk.Treeview(self, columns=[c[0] for c in self.COLUMNS], show="headings",
                                 height=4, selectmode="browse")
        for key, title, width, stretch in self.COLUMNS:
            self.tree.heading(key, text=title, anchor="w" if key in ("file", "dates") else "e")
            self.tree.column(key, width=px(width), minwidth=px(50), stretch=stretch,
                             anchor="w" if key in ("file", "dates") else "e")
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")
        self.tree.bind("<Double-1>", lambda e: self.show_in_folder())
        self.tree.bind("<Return>", lambda e: self.show_in_folder())

        buttons = ttk.Frame(self)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew",
                     pady=(px(4), 0) if compact else (px(8), px(2)))
        self.show_button = ttk.Button(buttons, text="Show in folder", command=self.show_in_folder)
        self.copy_file_button = ttk.Button(buttons, text="Copy file", command=self.copy_file)
        self.copy_text_button = ttk.Button(buttons, text="Copy text", command=self.copy_text)
        self.folder_button = ttk.Button(buttons, text="Open folder", command=self.open_folder)
        self.log_button = ttk.Button(buttons, text="View run log", command=self.view_log)
        for i, b in enumerate((self.show_button, self.copy_file_button, self.copy_text_button,
                               self.folder_button)):
            b.pack(side="left", padx=(0 if i == 0 else px(6), 0))
        self.log_button.pack(side="right")

        self.tip = ttk.Label(self, text=results_tip([], can_copy_files()),
                             style="Hint.TLabel", justify="left")
        if not compact:  # on short screens the table needs the room
            self.tip.grid(row=3, column=0, columnspan=2, sticky="ew")

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.bind("<Configure>", self._rewrap)
        self.clear("")

    def _rewrap(self, event):
        width = max(event.width - self.app.px(30), self.app.px(200))
        self.summary.configure(wraplength=width)
        self.tip.configure(wraplength=width)

    # ---- filling -------------------------------------------------------------

    def clear(self, message):
        self.files = []
        self.output_folder = ""
        self.log_path = ""
        self.tree.delete(*self.tree.get_children())
        self.summary.configure(text=message, style="Hint.TLabel")
        self.tip.configure(text=results_tip([], can_copy_files()))
        self._update_buttons()

    def show(self, result, heading="", exists=None, reachable=True, part_size=None):
        """Show a RunResult (or a saved last_run).

        ``exists`` (one True/False per file) leaves out files that have since been
        moved or deleted; None shows them all. ``reachable`` False: none of the
        files were found because their folder can't be reached (e.g. a network
        drive while off the VPN), so they are probably still there.
        ``part_size``: the run's File size setting (see results_tip).
        """
        self.output_folder = result.get("output_folder") or ""
        self.log_path = result.get("log_path") or ""
        files = result.get("files") or []
        if exists is not None:
            files = [f for f, ok in zip(files, exists) if ok]
        self.files = files
        self.tree.delete(*self.tree.get_children())
        for i, f in enumerate(self.files):
            first, last = f.get("first_date") or "", f.get("last_date") or ""
            if first and last and first != last:
                dates = "%s → %s" % (first, last)
            else:
                dates = first or last or "undated"
            self.tree.insert("", "end", iid=str(i), values=(
                short_file_name(f["path"]), fmt_int(round((f.get("bytes") or 0) / 1024.0)),
                fmt_tokens(f.get("est_tokens")), dates, fmt_int(f.get("emails"))))
        if self.files:
            self.tree.selection_set("0")
            self.tree.focus("0")
            text = summary_text(result)
            if run_was_filtered(result):
                text += (" %s only part of the emails - for the full digest, clear the dates "
                         "and focus keywords and click Squish!"
                         % ("This file has" if len(self.files) == 1 else "These files have"))
        elif result.get("files") and not reachable:
            folder = self.output_folder or os.path.dirname(result["files"][0].get("path") or "")
            text = ("Can't reach %s. If it's on a network drive, check you're connected to the "
                    "office network or VPN. Your digest files are probably still there."
                    % paths.short_path(folder))
        elif result.get("files"):
            text = "The files from this run have been moved or deleted. Click Squish! to make them again."
        else:
            text = summary_text(result)
        self.summary.configure(text=(heading + text) if heading else text, style="TLabel")
        self.tip.configure(text=results_tip(self.files, can_copy_files(), part_size))
        self._update_buttons()

    def set_log_path(self, path):
        self.log_path = path
        self._update_buttons()

    def _update_buttons(self):
        have = "!disabled" if self.files else "disabled"
        for b in (self.show_button, self.copy_text_button):
            b.state([have])
        self.copy_file_button.state([have if can_copy_files() else "disabled"])
        has_folder = self.output_folder and self.files
        self.folder_button.state(["!disabled" if has_folder else "disabled"])
        self.log_button.state(["!disabled" if self.log_path else "disabled"])

    def selected_path(self):
        if not self.files:
            return ""
        chosen = self.tree.selection()
        index = int(chosen[0]) if chosen else 0
        return self.files[index]["path"] if index < len(self.files) else ""

    # ---- buttons -------------------------------------------------------------

    def show_in_folder(self):
        path = self.selected_path()
        if not path:
            return
        try:
            if path_too_long_for_explorer(path):
                open_with_system(os.path.dirname(path))
                self.app.set_status(LONG_PATH_ADVICE, "Warn")
                return
            show_in_file_manager(path)
            self.app.set_status("Opened the folder - drag %s into Claude." % os.path.basename(path))
        except OSError as exc:
            self.app.set_status("Couldn't open the folder: %s" % exc, "Error")

    def copy_file(self):
        path = self.selected_path()
        if path and path_too_long_for_explorer(path):
            self.app.set_status(LONG_PATH_ADVICE, "Warn")
        elif path:
            self.app.set_status("Copying %s..." % os.path.basename(path))
            self.app.start_thread(lambda: self.app.post(("clip",) + copy_file_to_clipboard(path)))

    def copy_text(self):
        path = self.selected_path()
        if not path:
            return
        try:
            with open(paths.long_path(path), "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            self.app.set_status("Couldn't read %s: %s" % (os.path.basename(path), exc), "Error")
            return
        root = self.winfo_toplevel()
        root.clipboard_clear()
        root.clipboard_append(text)
        from . import engine
        tokens = len(text) / engine.CHARS_PER_TOKEN
        self.app.set_status("Copied the text of %s (~%s tokens) - paste it into Claude with %s."
                            % (os.path.basename(path), fmt_tokens(tokens), PASTE_KEY), "Good")

    def open_folder(self):
        folder = self.output_folder
        if not folder:
            return
        try:
            open_with_system(folder)
        except OSError as exc:
            self.app.set_status("Couldn't open the folder: %s" % exc, "Error")

    def view_log(self):
        path = self.log_path
        try:
            with open(paths.long_path(path), "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            self.app.set_status("The run log isn't there any more. It is written again on the "
                                "next run.", "Warn")
            return
        TextWindow(self.winfo_toplevel(), "Squish run log - %s" % os.path.basename(path), text,
                   open_path=path)


# --------------------------------------------------------------------------
# The main window
# --------------------------------------------------------------------------

class SquishApp(object):
    """The whole Squish window. Create it with a Tk root, then call root.mainloop()."""

    def __init__(self, root):
        self.root = root
        self.queue = queue.Queue()
        self.scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.projects, self.load_problem = projects.load_projects_report()
        # A project list that couldn't be read must not be saved over this session.
        self.save_blocked = bool(self.load_problem and self.load_problem["kind"] == "unreadable")
        self.unsaved = False     # True after a failed save, until a save succeeds
        self.compact = False     # short screen: leave out the header band (see _size_window)
        self.current_id = None
        self.list_ids = []
        self.save_job = None
        self.save_note_job = None
        self.count_job = None
        self.count_token = 0
        self.count_stop = threading.Event()
        self.files_token = 0     # bumped whenever the results table shows something new
        self.run = None          # dict while a run is going: id, project_id, cancel, name
        self.last_click = {}     # button key -> time.monotonic() of its last accepted click
        # project id -> (status text, run log path) of its last run that stopped
        # with an error, so the reason is still there when the project is shown again
        self.stopped = {}
        self.run_counter = 0
        self.closing = False
        self.poll_job = None
        self.status_details = ""  # the whole status message when only its first line shows
        self.images = {}         # keep PhotoImages alive

        root.title("Squish")
        root.report_callback_exception = self.on_tk_error
        self._setup_style()
        self._set_window_icon()
        self._size_window()
        self._build_menu()
        self._build_layout()
        self._bind_keys()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_project_list()
        if self.projects:
            self.select_project(self.projects[0]["id"])
        else:
            self.show_empty_state()
        self.start_thread(lambda: self.post(("backend", reader_status())))
        self.poll_job = root.after(POLL_MS, self.poll_queue)
        if self.load_problem:
            root.after(50, self.report_load_problem)

    # ---- small utilities -----------------------------------------------------

    def px(self, n):
        """``n`` pixels at 100% scaling, scaled for this screen."""
        return int(round(n * self.scale))

    def post(self, message):
        """Thread-safe: queue a message for the window (see poll_queue)."""
        self.queue.put(message)

    def start_thread(self, target, name="squish-helper"):
        thread = threading.Thread(target=target, name=name)
        thread.daemon = True
        thread.start()
        return thread

    def current_project(self):
        return projects.find_project(self.projects, self.current_id) if self.current_id else None

    def name_taken(self, name):
        """True if another project has this name, or one that gives the same file names."""
        return any(p["id"] != self.current_id and projects.same_file_name(p.get("name", ""), name)
                   for p in self.projects)

    def _double_click(self, key):
        """True for a repeat click on the same button within CLICK_GAP_S (ignore it).

        Tk runs a button's command for both clicks of a double-click; people
        often double-click big buttons, and the second click must not cancel
        the run the first one started (or make a second project).
        """
        now = time.monotonic()
        last = self.last_click.get(key)
        if last is not None and now - last < CLICK_GAP_S:
            return True
        self.last_click[key] = now
        return False

    # ---- appearance ------------------------------------------------------------

    def _setup_style(self):
        style = ttk.Style(self.root)
        names = style.theme_names()
        if IS_WINDOWS and "vista" in names:
            style.theme_use("vista")
        elif IS_MAC and "aqua" in names:
            style.theme_use("aqua")
        elif "clam" in names:
            style.theme_use("clam")
        base = tkfont.nametofont("TkDefaultFont")
        size = int(base.actual("size")) or 9
        family = base.actual("family")

        def sized(step):
            """The base size plus ``step`` points (negative Tk sizes are pixels)."""
            return max(size + step, 7) if size > 0 else min(size - step, -9)

        self.fonts = {
            "title": tkfont.Font(family=family, size=sized(7), weight="bold"),
            "welcome": tkfont.Font(family=family, size=sized(6), weight="bold"),
            "big": tkfont.Font(family=family, size=sized(3), weight="bold"),
            "bold": tkfont.Font(family=family, size=sized(0), weight="bold"),
            "hint": tkfont.Font(family=family, size=sized(-1)),
            "list": tkfont.Font(family=family, size=sized(1)),
        }
        line = self.fonts["list"].metrics("linespace")
        style.configure("Hint.TLabel", foreground=COLOURS["hint"], font=self.fonts["hint"])
        style.configure("Error.TLabel", foreground=COLOURS["error"], font=self.fonts["hint"])
        style.configure("Warn.TLabel", foreground=COLOURS["warn"], font=self.fonts["hint"])
        style.configure("Good.TLabel", foreground=COLOURS["good"], font=self.fonts["hint"])
        style.configure("Status.TLabel", foreground=COLOURS["hint"])
        style.configure("StatusError.TLabel", foreground=COLOURS["error"])
        style.configure("StatusWarn.TLabel", foreground=COLOURS["warn"])
        style.configure("StatusGood.TLabel", foreground=COLOURS["good"])
        style.configure("Section.TLabel", font=self.fonts["bold"])
        style.configure("Treeview", rowheight=int(line * 1.5))
        style.configure("TNotebook.Tab", padding=(self.px(10), self.px(4)))
        if style.theme_use() == "clam":
            bg = COLOURS["body_bg"]
            style.configure(".", background=bg)
            style.configure("TEntry", fieldbackground=COLOURS["field_bg"])
            style.configure("TCombobox", fieldbackground=COLOURS["field_bg"])
            style.map("TCombobox", fieldbackground=[("readonly", COLOURS["field_bg"])])
            style.configure("Treeview", fieldbackground=COLOURS["field_bg"],
                            background=COLOURS["field_bg"])
            style.configure("TNotebook", background=bg)
            style.map("TNotebook.Tab", background=[("selected", bg)])
            style.configure("TButton", padding=(self.px(10), self.px(4)))
            # clam draws these at a fixed pixel size; scale them for high-DPI screens
            style.configure("TCheckbutton", indicatorsize=self.px(12))
            style.configure("TRadiobutton", indicatorsize=self.px(12))
            style.configure("TScrollbar", arrowsize=self.px(14))
            style.configure("TCombobox", arrowsize=self.px(14))
            style.configure("Horizontal.TProgressbar", background=COLOURS["accent"],
                            troughcolor=COLOURS["line"], bordercolor=COLOURS["line"],
                            lightcolor=COLOURS["accent"], darkcolor=COLOURS["accent"])
            self.root.configure(background=bg)
        style.map("Treeview", background=[("selected", COLOURS["accent"])],
                  foreground=[("selected", COLOURS["on_accent"])])

    def _image(self, size):
        """The Squish icon at about ``size`` pixels as a PhotoImage, or None."""
        if size in self.images:
            return self.images[size]
        assets = paths.assets_dir()
        image = None
        for name in ("squish-%d.png" % size, "squish.png", "squish-64.png", "squish-32.png"):
            path = assets / name
            if not path.exists():
                continue
            try:
                image = tk.PhotoImage(file=str(path))
            except tk.TclError:
                continue
            factor = image.width() // size
            if factor >= 2:
                image = image.subsample(factor)
            break
        self.images[size] = image
        return image

    def _set_window_icon(self):
        ico = paths.assets_dir() / "squish.ico"
        try:
            if IS_WINDOWS and ico.exists():
                self.root.iconbitmap(default=str(ico))
                return
            images = [i for i in (self._image(64), self._image(32)) if i is not None]
            if images:
                self.root.iconphoto(True, *images)
        except tk.TclError:
            pass

    def _work_area(self):
        """(left, top, width, height) of the main screen without the taskbar."""
        if IS_WINDOWS:
            try:
                import ctypes
                from ctypes import wintypes
                rect = wintypes.RECT()
                # SPI_GETWORKAREA; physical pixels, as windows_setup made Squish DPI aware
                if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                    width, height = rect.right - rect.left, rect.bottom - rect.top
                    if width > 0 and height > 0:
                        return rect.left, rect.top, width, height
            except Exception:
                pass
        return (0, 0, self.root.winfo_screenwidth(),
                self.root.winfo_screenheight() - self.px(48))

    def _size_window(self):
        """Fit the window above the taskbar; on short screens use the compact layout.

        A tall screen gives the window up to ~900 px, so the Digest files table
        has room for its rows even under a long message.
        """
        left, top, aw, ah = self._work_area()
        frame = self.px(70)   # title bar + menu bar, which geometry() doesn't count on Windows
        width = min(self.px(1180), int(aw * 0.95))
        height = max(min(self.px(900), ah - frame), self.px(300))
        self.compact = height < self.px(700)
        self.root.minsize(min(self.px(980), width), min(self.px(560), height))
        x = left + max((aw - width) // 2, 0)
        y = top + max((ah - frame - height) // 3, 0)
        self.root.geometry("%dx%d+%d+%d" % (width, height, x, y))
        if self.compact and IS_WINDOWS:
            try:   # use the whole work area on a short laptop screen
                self.root.state("zoomed")
            except tk.TclError:
                pass

    # ---- building the window ---------------------------------------------------

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        project_menu = tk.Menu(menubar, tearoff=False)
        project_menu.add_command(label="New project", accelerator="Ctrl+N", command=self.new_project)
        project_menu.add_command(label="Duplicate", command=self.duplicate_project)
        project_menu.add_command(label="Delete...", command=self.delete_project)
        project_menu.add_separator()
        project_menu.add_command(label="Squish!", accelerator="F5", command=self.start_run)
        project_menu.add_separator()
        project_menu.add_command(label="Exit", command=self.on_close)
        menubar.add_cascade(label="Project", menu=project_menu)

        tools = tk.Menu(menubar, tearoff=False)
        tools.add_command(label="Create desktop shortcut",
                          command=lambda: self.make_shortcut("desktop"))
        tools.add_command(label="Create Start Menu shortcut",
                          command=lambda: self.make_shortcut("start"))
        tools.add_separator()
        tools.add_command(label="Open data folder", command=self.open_data_folder)
        tools.add_command(label="Open logs folder", command=self.open_logs_folder)
        menubar.add_cascade(label="Tools", menu=tools)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="How to use", command=self.show_how_to)
        help_menu.add_command(label="About Squish", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self.root.configure(menu=menubar)

    def _build_layout(self):
        px = self.px
        root = self.root
        if self.compact:   # short screen: tighter rows, so the Digest files table shows more
            style = ttk.Style(root)
            line = self.fonts["list"].metrics("linespace")
            style.configure("Treeview", rowheight=int(line * 1.3))
            style.configure("TNotebook.Tab", padding=(px(10), px(2)))
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        # Header (left out on short screens: the title bar still says Squish)
        header = tk.Frame(root, background=COLOURS["header_bg"])
        if not self.compact:
            header.grid(row=0, column=0, sticky="ew")
        icon = self._image(32 if self.scale < 1.5 else 64)
        if icon is not None:
            tk.Label(header, image=icon, background=COLOURS["header_bg"]).pack(
                side="left", padx=(px(16), px(8)), pady=px(7))
        tk.Label(header, text="Squish", font=self.fonts["title"], background=COLOURS["header_bg"],
                 foreground=COLOURS["text"]).pack(side="left", padx=(0 if icon else px(16), px(12)),
                                                  pady=px(7))
        tk.Label(header, text=TAGLINE, font=self.fonts["hint"], background=COLOURS["header_bg"],
                 foreground=COLOURS["hint"]).pack(side="left", pady=(px(12), px(7)), anchor="s")
        if not self.compact:
            ttk.Separator(root, orient="horizontal").grid(row=1, column=0, sticky="ew")

        body = ttk.Frame(root, padding=(px(12), px(10), px(12), px(8)))
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        self._build_sidebar(body)
        self.main = ttk.Frame(body)
        self.main.grid(row=0, column=1, sticky="nsew", padx=(px(12), 0))
        self.main.columnconfigure(0, weight=1)
        self.main.rowconfigure(0, weight=1)
        self._build_empty_state(self.main)
        self._build_editor(self.main)

        # Status bar
        ttk.Separator(root, orient="horizontal").grid(row=3, column=0, sticky="ew")
        bar = ttk.Frame(root, padding=(px(12), px(3)))
        bar.grid(row=4, column=0, sticky="ew")
        self.backend_label = ttk.Label(bar, text="Outlook .msg: checking reader...",
                                       style="Hint.TLabel")
        self.backend_label.pack(side="left")
        self.save_label = ttk.Label(bar, text="", style="Hint.TLabel")
        self.save_label.pack(side="left", padx=(px(16), 0))
        ttk.Label(bar, text="Squish %s" % __version__, style="Hint.TLabel").pack(side="right")

    def _build_sidebar(self, body):
        px = self.px
        body.columnconfigure(0, minsize=px(230))
        side = ttk.Frame(body)
        side.grid(row=0, column=0, sticky="nsew")
        side.rowconfigure(1, weight=1)
        side.columnconfigure(0, weight=1)
        ttk.Label(side, text="Projects", style="Section.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, px(6)))
        holder = tk.Frame(side, background=COLOURS["line"], padx=1, pady=1)
        holder.grid(row=1, column=0, sticky="nsew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        self.project_list = tk.Listbox(
            holder, activestyle="none", exportselection=False, borderwidth=0,
            highlightthickness=0, width=1, font=self.fonts["list"],
            background=COLOURS["field_bg"], foreground=COLOURS["text"],
            selectbackground=COLOURS["accent"], selectforeground=COLOURS["on_accent"])
        self.project_list.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.project_list.yview)
        self.project_list.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")
        self.project_list.bind("<<ListboxSelect>>", self._on_list_select)
        self.list_width = 0
        self.project_list.bind("<Configure>", self._on_list_resize)

        buttons = ttk.Frame(side)
        buttons.grid(row=2, column=0, sticky="ew", pady=(px(8), 0))
        for i in range(3):
            buttons.columnconfigure(i, weight=1)
        # width=0: as wide as the text needs (themes otherwise make every button ~11 characters)
        # A double-click makes one project, not two (see _double_click).
        self.new_button = ttk.Button(
            buttons, text="New", width=0,
            command=lambda: self._double_click("new") or self.new_project())
        self.new_button.grid(row=0, column=0, sticky="ew")
        self.dup_button = ttk.Button(
            buttons, text="Duplicate", width=0,
            command=lambda: self._double_click("dup") or self.duplicate_project())
        self.dup_button.grid(row=0, column=1, sticky="ew", padx=px(4))
        self.del_button = ttk.Button(buttons, text="Delete", width=0, command=self.delete_project)
        self.del_button.grid(row=0, column=2, sticky="ew")

    def _build_empty_state(self, parent):
        px = self.px
        self.empty = ttk.Frame(parent)
        inner = ttk.Frame(self.empty)
        inner.place(relx=0.5, rely=0.42, anchor="center")
        image = self._image(64 if self.scale < 1.5 else 128)
        if image is not None:
            ttk.Label(inner, image=image).pack(pady=(0, px(12)))
        ttk.Label(inner, text="Welcome to Squish", font=self.fonts["welcome"]).pack()
        ttk.Label(inner, justify="center", wraplength=px(460), text=(
            "Squish reads a folder of saved Outlook emails and squashes them into one small "
            "text file you can drag into Claude - so Claude can track the correspondence and "
            "the actions for you.\n\nMake a project for each job you want to keep an eye on.")
        ).pack(pady=(px(8), px(18)))
        tk.Button(inner, text="Create your first project", command=self.new_project,
                  font=self.fonts["big"], background=COLOURS["accent"],
                  foreground=COLOURS["on_accent"], activebackground=COLOURS["accent_active"],
                  activeforeground=COLOURS["on_accent"], relief="flat", borderwidth=0,
                  cursor="hand2", padx=px(22), pady=px(9)).pack()
        ttk.Label(inner, text="Squish only reads your emails. It never changes or deletes them.",
                  style="Hint.TLabel").pack(pady=(px(14), 0))

    def _build_editor(self, parent):
        px = self.px
        self.editor = ttk.Frame(parent)
        self.editor.columnconfigure(0, weight=1)
        self.editor.rowconfigure(2, weight=1)

        self.form = SettingsForm(self.editor, self)
        self.form.grid(row=0, column=0, sticky="ew")

        self.run_row = run = ttk.Frame(self.editor)
        run.grid(row=1, column=0, sticky="ew",
                 pady=(px(6), px(4)) if self.compact else (px(10), px(8)))
        run.columnconfigure(1, weight=1)
        self.run_button = tk.Button(
            run, text="Squish!", width=12, command=self.on_run_button, font=self.fonts["big"],
            background=COLOURS["accent"], foreground=COLOURS["on_accent"],
            activebackground=COLOURS["accent_active"], activeforeground=COLOURS["on_accent"],
            disabledforeground="#D8DEE9", relief="flat", borderwidth=0, cursor="hand2",
            padx=px(18), pady=px(5) if self.compact else px(8))
        self.run_button.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, px(14)))
        self.progress = ttk.Progressbar(run, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(px(4), px(4)))
        # One line of status; a longer message keeps the rest behind Details...
        # (more lines would squeeze the Digest files table on a laptop screen).
        self.status = ttk.Label(run, text="", style="Status.TLabel", justify="left")
        self.status.grid(row=1, column=1, columnspan=2, sticky="new")
        self.details_button = ttk.Button(run, text="Details...", width=0,
                                         command=self.show_status_details)
        self.details_button.grid(row=1, column=2, sticky="ne", padx=(px(8), 0))
        self.details_button.grid_remove()
        run.bind("<Configure>", lambda e: self._wrap_status())

        self.results = ResultsPanel(self.editor, self)
        self.results.grid(row=2, column=0, sticky="nsew")

    def _bind_keys(self):
        def new(_event):
            # (a held-down Ctrl+N repeats: make at most one project per CLICK_GAP_S)
            if not self._double_click("new"):
                self.new_project()

        self.root.bind_all("<Control-n>", new)
        self.root.bind_all("<Control-N>", new)
        self.root.bind_all("<Control-Return>", lambda e: self.start_run())
        self.root.bind_all("<F5>", lambda e: self.start_run())

    # ---- the project list --------------------------------------------------------

    def _list_text(self, project):
        """A project's name as shown in the list: long names lose their middle
        ('Riverside Depot…Upgrade Stage 2') so the end stays visible."""
        text = "  " + (project.get("name") or "(no name)")
        width = self.list_width - self.px(8)
        if width < self.px(60):     # not drawn yet
            return text
        return fit_middle(text, self.fonts["list"].measure, width)

    def _on_list_resize(self, event):
        if abs(event.width - self.list_width) > 2:
            self.list_width = event.width
            self.refresh_project_list()

    def refresh_project_list(self):
        self.list_ids = [p["id"] for p in self.projects]
        self.project_list.delete(0, "end")
        for p in self.projects:
            self.project_list.insert("end", self._list_text(p))
        if self.current_id in self.list_ids:
            index = self.list_ids.index(self.current_id)
            self.project_list.selection_clear(0, "end")
            self.project_list.selection_set(index)
            self.project_list.see(index)
        state = ["!disabled"] if self.current_id else ["disabled"]
        self.dup_button.state(state)
        self.del_button.state(state)

    def _rename_in_list(self, project):
        if project["id"] in self.list_ids:
            index = self.list_ids.index(project["id"])
            self.project_list.delete(index)
            self.project_list.insert(index, self._list_text(project))
            self.project_list.selection_set(index)

    def _on_list_select(self, _event=None):
        chosen = self.project_list.curselection()
        if chosen and chosen[0] < len(self.list_ids):
            project_id = self.list_ids[chosen[0]]
            if project_id != self.current_id:
                self.select_project(project_id)

    def show_empty_state(self):
        self.current_id = None
        self.editor.grid_remove()
        self.empty.grid(row=0, column=0, sticky="nsew")
        self.refresh_project_list()

    def select_project(self, project_id):
        """Show the project with this id in the form."""
        self.flush_save()
        project = projects.find_project(self.projects, project_id)
        if project is None:
            return
        self.current_id = project_id
        self.empty.grid_remove()
        self.editor.grid(row=0, column=0, sticky="nsew")
        self.form.load(project)
        self.refresh_project_list()
        # A run of this project that stopped while another one was on screen.
        stop = self.stopped.get(project_id)
        if self.run and self.run["project_id"] == project_id:
            self.files_token += 1
            self.results.clear("Working on it...")
        else:
            self.show_last_run(project)
            if stop and stop[1]:     # the stopped run's log, e.g. the files it couldn't read
                self.results.set_log_path(stop[1])
        self.schedule_count(delay=0)
        if not self.run:
            if stop and stop[0]:
                self.set_status(stop[0], "Error")
            else:
                self.set_status("")
            self.progress.configure(mode="determinate", value=0)
        elif not self.run["cancel"].is_set():
            self.run_button.configure(text=self._run_label())

    def show_last_run(self, project):
        """Show the project's last run, then check in the background that its files still
        exist (the output folder may be on a slow or disconnected network drive)."""
        self.files_token += 1
        last = project.get("last_run")
        if not isinstance(last, dict) or not last:
            self.results.clear("No digest yet - click Squish! to make one.")
            return
        heading = "Last run %s: " % (last.get("finished_at") or "")
        part_size = project.get("part_size")
        self.results.show(last, heading, part_size=part_size)
        token = self.files_token
        files = [f.get("path", "") for f in last.get("files") or []]
        folder = last.get("output_folder") or (os.path.dirname(files[0]) if files else "")

        def work():
            exists = [bool(p) and os.path.exists(paths.long_path(p)) for p in files]
            # None found: is the folder itself out of reach (a network drive off the VPN)?
            reachable = any(exists) or not folder or os.path.isdir(paths.long_path(folder))
            self.post(("files_checked", token, last, heading, exists, reachable, part_size))

        self.start_thread(work)

    def _files_checked(self, token, last, heading, exists, reachable=True, part_size=None):
        if token == self.files_token and not all(exists):
            # Keep the panel's run log: it may be a stopped run's newer log.
            log_path = self.results.log_path
            self.results.show(last, heading, exists, reachable, part_size)
            self.results.set_log_path(log_path)

    # ---- project commands -----------------------------------------------------------

    def new_project(self):
        name = projects.unique_name(projects.DEFAULT_NAME, [p["name"] for p in self.projects])
        project = projects.new_project(name)
        self.projects.append(project)
        self.save_now()
        self.select_project(project["id"])
        self.form.select(SettingsForm.TAB_EMAILS)
        self.form.name_entry.focus_set()
        self.form.name_entry.select_range(0, "end")
        self.set_status("New project made. Give it a name and choose its emails folder.")

    def duplicate_project(self):
        project = self.current_project()
        if project is None:
            return
        self.flush_save()
        copy_of = projects.duplicate_project(project, [p["name"] for p in self.projects])
        self.projects.insert(self.projects.index(project) + 1, copy_of)
        self.save_now()
        self.select_project(copy_of["id"])
        self.set_status("Made a copy. Change what you need - for example the dates or the folder.")

    def delete_project(self):
        project = self.current_project()
        if project is None:
            return
        if self.run and self.run["project_id"] == project["id"]:
            self.set_status("This project is running. Cancel the run before deleting it.", "Warn")
            return
        sure = messagebox.askyesno(
            "Delete project",
            "Delete the project \"%s\" from Squish?\n\nThis only removes it from the list. "
            "It never deletes any emails or digest files." % project.get("name", ""),
            parent=self.root, icon="warning", default="no")
        if not sure:
            return
        self.cancel_save()
        self.stopped.pop(project["id"], None)
        index = self.projects.index(project)
        self.projects.remove(project)
        self.save_now()
        if self.projects:
            self.select_project(self.projects[min(index, len(self.projects) - 1)]["id"])
        else:
            self.show_empty_state()

    # ---- settings changes and autosave --------------------------------------------

    def on_setting_changed(self, field):
        project = self.current_project()
        if project is None:
            return
        project.update(self.form.values())
        if not self.run:
            self.set_status("")      # an earlier message may no longer apply
        if project["id"] in self.stopped:   # nor a stopped run's reason (keep its log)
            self.stopped[project["id"]] = ("", self.stopped[project["id"]][1])
        if field == "name":
            self._rename_in_list(project)
        if field in ("source_folder", "include_subfolders"):
            self.schedule_count()
        self.schedule_save()

    def schedule_save(self):
        self.cancel_save()
        self.save_job = self.root.after(AUTOSAVE_MS, self.save_now)

    def cancel_save(self):
        if self.save_job is not None:
            self.root.after_cancel(self.save_job)
            self.save_job = None

    def flush_save(self):
        """Save now if a save is waiting or an earlier one failed (before switching
        project or closing). Returns False if the settings couldn't be saved."""
        if self.save_job is not None or self.unsaved:
            return self.save_now()
        return True

    def save_now(self):
        """Save all projects now. Returns True if saved.

        On failure the error stays on show and Squish tries again every few
        seconds (closing the window asks before throwing the changes away).
        """
        self.cancel_save()
        if self.save_note_job is not None:   # its 'clear the label' must not hide an error
            self.root.after_cancel(self.save_note_job)
            self.save_note_job = None
        if self.save_blocked:
            self.unsaved = True
            self.save_label.configure(text="Not saved - your project list couldn't be read when "
                                      "Squish started", style="Error.TLabel")
            return False
        project = self.current_project()
        if project is not None and not project.get("name"):
            project["name"] = projects.DEFAULT_NAME
        try:
            projects.save_projects(self.projects)
        except OSError as exc:
            self.unsaved = True
            self.save_label.configure(text="Couldn't save settings: %s - will keep trying"
                                      % (exc.strerror or exc), style="Error.TLabel")
            if not self.closing:
                self.save_job = self.root.after(SAVE_RETRY_MS, self.save_now)
            return False
        self.unsaved = False
        self.save_label.configure(text="Settings saved", style="Hint.TLabel")
        self.save_note_job = self.root.after(2500, self._clear_save_note)
        return True

    def _clear_save_note(self):
        self.save_note_job = None
        self.save_label.configure(text="")

    def report_load_problem(self):
        """Tell the user if projects.json couldn't be loaded at start-up."""
        problem = self.load_problem
        while problem and problem["kind"] == "unreadable":
            retry = messagebox.askretrycancel(
                "Squish can't read your project list",
                "Squish couldn't open its list of projects:\n%s\n\n%s\n\nAnother program may be "
                "using it, or the drive may be unavailable. Your projects have not been changed."
                % (problem["path"], problem["error"]), parent=self.root, icon="warning")
            if not retry:
                self.set_status("Your project list couldn't be read, so changes won't be saved "
                                "this session. Restart Squish to try again.", "Error")
                self.save_label.configure(text="Project list couldn't be read - changes won't "
                                          "be saved this session", style="Error.TLabel")
                return
            items, problem = projects.load_projects_report()
            if problem is None:
                self.projects, self.load_problem = items, None
                self.save_blocked = False
                self.unsaved = False
                self.save_label.configure(text="")
                self.refresh_project_list()
                if self.projects:
                    self.select_project(self.projects[0]["id"])
                return
            self.load_problem = problem
            self.save_blocked = problem["kind"] == "unreadable"
        if problem and problem["kind"] == "damaged":
            name = os.path.basename(problem["backup"] or "")
            self.set_status("Your project list couldn't be read and was set aside as %s. "
                            "Tools > Open data folder shows it." % name, "Warn")
            self.save_label.configure(text="Damaged project list set aside as %s" % name,
                                      style="Warn.TLabel")
            messagebox.showwarning(
                "Squish", "Your project list was damaged, so Squish has started with an empty "
                "list.\n\nThe old file was kept as:\n%s" % problem["backup"], parent=self.root)

    # ---- counting emails in the chosen folder ---------------------------------------

    def schedule_count(self, delay=COUNT_DELAY_MS):
        if self.count_job is not None:
            self.root.after_cancel(self.count_job)
        self.count_job = self.root.after(delay, self.start_count)

    def start_count(self):
        """Count the emails in the folder in a background thread (network drives can be slow)."""
        self.count_job = None
        self.count_stop.set()                # stop the previous count, if any
        self.count_token += 1
        token = self.count_token
        values = self.form.values()
        folder = values["source_folder"]
        if not folder:
            self.form.set_hint(self.form.source_hint, "Choose the folder where this project's emails "
                               "are filed - a folder of emails, or a folder of folders.")
            return
        stop = threading.Event()
        self.count_stop = stop
        subfolders = values["include_subfolders"]
        self.form.set_hint(self.form.source_hint, "Counting email files...")

        def work():
            skipped = []
            try:
                count = count_email_files(folder, subfolders, stop,
                                          lambda n: self.post(("count", token, "progress", n)),
                                          skipped)
                if count is not None:
                    self.post(("count", token, "done", count, len(skipped)))
            except FileNotFoundError:
                self.post(("count", token, "missing", 0))
            except OSError as exc:
                self.post(("count", token, "error", exc.strerror or str(exc)))

        self.start_thread(work)

    def _show_count(self, token, kind, value, skipped=0):
        if token != self.count_token:
            return  # the folder has changed since this count started
        hint = self.form.source_hint
        subfolders = self.form.subfolders_var.get()
        not_opened = (" (%s couldn't be opened)" % plural(skipped, "subfolder")) if skipped else ""
        if kind == "progress":
            self.form.set_hint(hint, "Counting email files... %s so far" % fmt_int(value))
        elif kind == "done" and value:
            where = "incl. subfolders" if subfolders else "this folder only"
            self.form.set_hint(hint, "%s found (%s)%s" % (plural(value, "email file"), where,
                                                          not_opened),
                               "Warn" if skipped else "Good")
        elif kind == "done":
            extra = "" if subfolders else " Tick 'Include subfolders' to look inside its folders."
            self.form.set_hint(hint, "No .msg or .eml email files here%s.%s" % (not_opened, extra),
                               "Warn")
        elif kind == "missing":
            self.form.set_hint(hint, "Can't find this folder. If it's on a network drive (like H:), "
                               "check you're connected to the office network or VPN.", "Error")
        else:
            self.form.set_hint(hint, "Can't open this folder: %s" % value, "Error")

    # ---- running ----------------------------------------------------------------------

    def on_run_button(self):
        """The big button: Squish! starts a run, Cancel stops it. The second click
        of a double-click is ignored (it would cancel the run just started)."""
        if self._double_click("run"):
            return
        if self.run:
            self.cancel_run()
        else:
            self.start_run()

    def start_run(self):
        if self.closing:
            return
        if self.run:
            self.set_status("Already squishing \"%s\" - wait for it to finish or cancel it first."
                            % short_name(self.run["name"]), "Warn")
            return
        project = self.current_project()
        if project is None:
            return
        # A pasted folder (no Browse) still names a project called 'New project'.
        self.form.maybe_guess_name(self.form.source_var.get())
        problem = self.form.blocking_problem()
        if problem:
            tab, widget, message = problem
            self.form.select(tab)
            widget.focus_set()
            self.progress.configure(mode="determinate", maximum=100, value=0)
            self.set_status(message, "Error")
            return
        project.update(self.form.values())
        self.flush_save()
        self.run_counter += 1
        self.run = {"id": self.run_counter, "project_id": project["id"],
                    "name": project["name"], "cancel": threading.Event(), "stage": "scan",
                    "started": time.time(), "filters": filter_summary(project),
                    "part_size": project.get("part_size")}
        self.stopped.pop(project["id"], None)   # an earlier stop no longer applies
        work_copy = copy.deepcopy(project)
        # The files the last run wrote, so the engine can tidy them up even if
        # the project has been renamed since.
        work_copy["previous_files"] = [f.get("path", "") for f in
                                       (project.get("last_run") or {}).get("files") or []
                                       if f.get("path")]
        work_copy["last_run"] = None
        run_id, cancel = self.run["id"], self.run["cancel"]
        self.start_thread(lambda: run_in_background(run_id, work_copy, cancel, self.post),
                          name="squish-run")
        self.run_button.configure(text=self._run_label(), background=COLOURS["cancel"],
                                  activebackground=COLOURS["cancel_active"], state="normal")
        self.files_token += 1
        self.results.clear("Working on it...")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.set_status("Starting...")

    def _run_prefix(self, run):
        """'"<name>": ' when the run's project is not the one on screen, else ''.
        (A long name is shortened, so the message stays on one line.)"""
        return "" if run["project_id"] == self.current_id else '"%s": ' % short_name(run["name"])

    def _run_label(self):
        """The big button's text while a run is going: it cancels that run."""
        if not self.run or self.run["project_id"] == self.current_id:
            return "Cancel"
        return 'Cancel "%s"' % short_name(self.run["name"], 18)

    def cancel_run(self):
        if not self.run:
            return
        self.run["cancel"].set()
        self.run_button.configure(text="Cancelling...", state="disabled")
        stage = self.run.get("stage")
        if stage == "read":
            self.set_status("Cancelling - finishing the emails being read now...")
        elif stage == "digest":
            self.set_status("Cancelling - stopping the digest step...")
        else:
            self.set_status("Cancelling...")

    def _run_finished(self):
        self.run = None
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.run_button.configure(text="Squish!", background=COLOURS["accent"],
                                  activebackground=COLOURS["accent_active"], state="normal")

    def _show_progress(self, run_id, stage, done, total, message):
        if not self.run or run_id != self.run["id"]:
            return
        self.run["stage"] = stage
        if self.run["cancel"].is_set():
            return
        # While another project is shown, say which project the progress is for.
        prefix = self._run_prefix(self.run)
        counted = total > 1 if stage == "digest" else stage in ("read", "write") and total > 0
        if counted:
            if str(self.progress.cget("mode")) != "determinate":
                self.progress.stop()
                self.progress.configure(mode="determinate")
            self.progress.configure(maximum=total, value=done)
            if stage == "read":
                message = "Reading %s of %s emails..." % (fmt_int(done), fmt_int(total))
            self.set_status(prefix + (message or ("Saving..." if stage == "write" else "Working...")))
        else:
            if str(self.progress.cget("mode")) != "indeterminate":
                self.progress.configure(mode="indeterminate", value=0)
                self.progress.start(12)
            self.set_status(prefix + (message or "Working..."))

    def _run_done(self, run_id, result):
        if not self.run or run_id != self.run["id"]:
            return
        run = self.run
        self._run_finished()
        project = projects.find_project(self.projects, run["project_id"])
        on_screen = project is not None and project["id"] == self.current_id
        # While another project is shown, every message names the run's project.
        prefix = self._run_prefix(run)
        if result.get("cancelled"):
            self.set_status(prefix + "Cancelled - nothing was written.", "Warn")
            if on_screen:
                self.show_last_run(project)
            return
        files = result.get("files") or []
        self.progress.configure(maximum=100, value=100 if files else 0)
        if not files:
            # Nothing was written, so any earlier digest files are still there:
            # keep showing them (last_run is left as it was).
            had_files = project is not None and (project.get("last_run") or {}).get("files")
            self.set_status(prefix + "Finished, but no emails were left to write (check the "
                            "dates and focus keywords)%s." % (
                                " - your earlier digest files were kept" if had_files else ""),
                            "Warn")
            if on_screen:
                self.show_last_run(project)
            return
        self.stopped.pop(run["project_id"], None)
        if project is not None:
            project["last_run"] = last_run_from_result(result)
            self.save_now()
        if on_screen:
            self.files_token += 1
            self.results.show(result, part_size=run.get("part_size"))
        other = "" if project is None or on_screen else short_name(run["name"])
        # Amber, not green, when emails may be missing (files that couldn't be
        # read, folders that couldn't be opened): the summary says which.
        missing = any(failure_counts(result)) or (result.get("stats") or {}).get("no_access_emails")
        self.set_status(done_message(result.get("elapsed_s", 0), files, can_copy_files(), other,
                                     run.get("filters", ""), run.get("part_size")),
                        "Warn" if missing else "Good")

    def _run_error(self, run_id, message, log_path=""):
        """The run stopped with a SquishError. ``log_path``: the run log it wrote, if any."""
        if not self.run or run_id != self.run["id"]:
            return
        run = self.run
        self._run_finished()
        log_path = log_path or self._fresh_run_log(run)
        on_screen = run["project_id"] == self.current_id
        project = projects.find_project(self.projects, run["project_id"])
        if on_screen and project is not None:
            self.show_last_run(project)
            if log_path:   # e.g. the list of files that couldn't be read
                self.results.set_log_path(log_path)
        # The first line shows (with the run's project, if another is on screen);
        # the advice that follows it is behind Details...
        head, _, rest = message.strip().partition("\n\n")
        first, _, more = head.strip().partition("\n")
        more = "\n" + more if more else ""
        rest = "\n\n" + rest if rest else ""
        note = " View run log has the details." if log_path and not more else ""
        # Kept for when the run's project is shown again, with its log on View run log.
        self.stopped[run["project_id"]] = (first + note + more + rest, log_path)
        if on_screen:
            self.set_status(first + note + more + rest, "Error")
        else:
            # Not attached to the project on screen. A long name is shortened on the
            # status line; the full name is behind Details...
            full = "" if short_name(run["name"]) == run["name"] else "\n\nProject: " + run["name"]
            self.set_status(self._run_prefix(run) + first + more + rest + full, "Error")

    def _fresh_run_log(self, run):
        """The run log the engine wrote for this run before it stopped, or ''.

        (For an engine whose SquishError doesn't carry ``log_path``: the log is
        '<project> - last run.txt' in the logs folder, written after the run began.)
        """
        try:
            name = paths.safe_filename((run.get("name") or "").strip() or "Project")
            path = str(paths.logs_dir() / ("%s - last run.txt" % name))
            if os.path.getmtime(path) >= run.get("started", time.time()) - 2:
                return path
        except (OSError, ValueError):
            pass
        return ""

    def _run_crash(self, run_id, details):
        if not self.run or run_id != self.run["id"]:
            return
        run = self.run
        self._run_finished()
        log = self._write_crash_log(run["name"], details)
        last = details.strip().splitlines()[-1] if details.strip() else "unknown error"
        text = "Something went wrong: %s. The details are in the run log." % last
        self.stopped[run["project_id"]] = (text, log)   # for when it is shown again
        if run["project_id"] != self.current_id:
            # Don't attach the report to the project on screen: say where it is.
            full = "" if short_name(run["name"]) == run["name"] else "\n\nProject: " + run["name"]
            self.set_status(self._run_prefix(run) + "Something went wrong: %s. The details are "
                            "in its run log (Tools > Open logs folder)." % last + full, "Error")
            return
        self.set_status(text, "Error")
        project = self.current_project()
        if project is not None:
            self.show_last_run(project)
        if log:
            self.results.set_log_path(log)

    @staticmethod
    def _write_crash_log(project_name, details):
        """Save an unexpected error as the project's run log. Returns the path or ''."""
        try:
            path = paths.logs_dir() / ("%s - last run.txt" % paths.safe_filename(project_name))
            with open(str(path), "w", encoding="utf-8") as fh:
                fh.write("Squish %s - the run stopped with an unexpected error\n" % __version__)
                fh.write("Project: %s\nTime: %s\n\n%s\n" % (
                    project_name, datetime.now().strftime("%Y-%m-%d %H:%M"), details))
            return str(path)
        except OSError:
            return ""

    # ---- messages from background threads --------------------------------------------

    def poll_queue(self):
        """Handle everything the background threads have posted, then check again soon
        (also while closing, so a run that finishes then still saves its last_run)."""
        self.drain_queue()
        self.poll_job = self.root.after(POLL_MS, self.poll_queue)

    def drain_queue(self):
        """Handle every message waiting in the queue."""
        try:
            while True:
                message = self.queue.get_nowait()
                try:
                    self.handle_message(message)
                except Exception:
                    self.on_tk_error(*sys.exc_info())
        except queue.Empty:
            pass

    def handle_message(self, message):
        kind = message[0]
        if kind == "progress":
            self._show_progress(*message[1:])
        elif kind == "done":
            self._run_done(message[1], message[2])
        elif kind == "error":
            self._run_error(*message[1:])
        elif kind == "crash":
            self._run_crash(message[1], message[2])
        elif kind == "count":
            self._show_count(*message[1:])
        elif kind == "files_checked":
            self._files_checked(*message[1:])
        elif kind == "backend":
            self.backend_label.configure(text=message[1])
        elif kind == "clip":
            self.set_status(message[2], "Good" if message[1] else "Error")
        elif kind == "shortcut":
            ok, text = message[1], message[2]
            if ok:
                messagebox.showinfo("Squish shortcut", text, parent=self.root)
            else:
                messagebox.showwarning("Squish shortcut", text, parent=self.root)

    def set_status(self, text, kind=""):
        """Show a message under the Squish! button. ``kind``: '', Good, Warn or Error.

        Only the first line shows; a longer message gets a Details... button.
        """
        first, self.status_details = split_status(text)
        self.status.configure(text=first, style="Status%s.TLabel" % kind)
        showing = bool(self.details_button.winfo_manager())
        if bool(self.status_details) != showing:   # (progress lines come often: no relayout)
            if self.status_details:
                self.status.grid_configure(columnspan=1)
                self.details_button.grid()
            else:
                self.details_button.grid_remove()
                self.status.grid_configure(columnspan=2)
            self._wrap_status()

    def _wrap_status(self):
        """Wrap the status line to the room beside the Squish! button (and Details...)."""
        room = self.run_row.winfo_width() - self.run_button.winfo_width() - self.px(20)
        if self.status_details:
            room -= self.details_button.winfo_reqwidth() + self.px(8)
        self.status.configure(wraplength=max(room, self.px(200)))

    def show_status_details(self):
        if self.status_details:
            TextWindow(self.root, "Squish", self.status_details, plain=True)

    # ---- menus --------------------------------------------------------------------------

    def make_shortcut(self, which):
        from . import shortcut
        if not shortcut.is_windows():
            messagebox.showinfo("Squish shortcut", shortcut.NOT_WINDOWS_MESSAGE, parent=self.root)
            return
        maker = (shortcut.create_desktop_shortcut if which == "desktop"
                 else shortcut.create_start_menu_shortcut)
        self.set_status("Creating the shortcut...")
        self.start_thread(lambda: self.post(("shortcut",) + tuple(maker())))

    def open_data_folder(self):
        """The folder with the project list (projects.json)."""
        self._open_folder(paths.data_dir)

    def open_logs_folder(self):
        """The folder with the run logs and crash reports (on Windows this is in
        %LOCALAPPDATA%, not next to the project list)."""
        self._open_folder(paths.logs_dir)

    def _open_folder(self, which):
        try:
            folder = str(which())
            open_with_system(folder)
        except OSError as exc:
            messagebox.showerror("Squish", "Couldn't open the folder\n\n%s" % exc, parent=self.root)

    def show_how_to(self):
        TextWindow(self.root, "How to use Squish", HOW_TO)

    def show_about(self):
        messagebox.showinfo("About Squish", (
            "Squish %s\n%s.\n\n%s\nPython %s, Tk %s\n\nSettings are kept in:\n%s\n"
            "Run logs and the read cache:\n%s\n\n"
            "Everything runs on this computer. Nothing is uploaded - only the files you "
            "choose to drag into Claude leave it."
            % (__version__, TAGLINE, self.backend_label.cget("text"), sys.version.split()[0],
               tk.TkVersion, paths.data_dir(), paths.local_data_dir())), parent=self.root)

    # ---- errors and closing ---------------------------------------------------------------

    def on_tk_error(self, exc_type, exc, tb):
        """Unexpected error inside the window: log it and say so, but keep running."""
        details = "".join(traceback.format_exception(exc_type, exc, tb))
        from .__main__ import write_crash_log  # shared with the start-up crash handler
        log = write_crash_log(details)
        try:
            messagebox.showerror("Squish", "Something went wrong: %s: %s\n\nThe details are saved "
                                 "in:\n%s" % (exc_type.__name__, exc, log or "(could not save)"),
                                 parent=self.root)
        except tk.TclError:
            pass

    def on_close(self):
        if self.closing:
            # Clicked again while a run is stopping: close now (a hung network
            # read must never trap the user).
            self._destroy()
            return
        if not self.flush_save():
            if not messagebox.askyesno(
                    "Settings not saved",
                    "Squish couldn't save your latest settings changes.\n\n"
                    "Close anyway and lose them?",
                    parent=self.root, icon="warning", default="no"):
                return
        run = self.run   # the run may finish while the question below is open
        if run:
            stop = messagebox.askyesno(
                "Squish is still working",
                "Squish is still working on \"%s\".\n\nStop it and close Squish?" % run["name"],
                parent=self.root, icon="warning")
            if not stop:
                return
            run["cancel"].set()     # harmless if it has finished meanwhile
        self.flush_save()
        self.count_stop.set()
        self.closing = True
        if self._run_thread_alive():
            # Let the run save the emails it has read (the read cache) and finish
            # or undo any file it is writing; the window stays open meanwhile.
            self.run_button.configure(text="Stopping...", state="disabled")
            self.set_status("Stopping - saving the emails already read so the next run is "
                            "quicker. Squish will close by itself (click X again to close now).",
                            "Warn")
            self._close_when_idle(time.monotonic() + CLOSE_WAIT_S)
        else:
            self._close_when_idle(time.monotonic() + QUICK_CLOSE_S)

    @staticmethod
    def _run_thread_alive():
        return any(t.is_alive() for t in threading.enumerate() if t.name.startswith("squish-run"))

    def _close_when_idle(self, deadline):
        """Wait for a cancelled run to stop cleanly (up to ``deadline``), then close."""
        if self._run_thread_alive() and time.monotonic() < deadline:
            self.root.after(100, self._close_when_idle, deadline)
            return
        # A run that finished meanwhile has posted its result: handle it, so a
        # finished run still saves its last_run.
        self.drain_queue()
        self._destroy()

    def _destroy(self):
        try:
            if self.poll_job is not None:
                self.root.after_cancel(self.poll_job)
                self.poll_job = None
            self.progress.stop()
        except tk.TclError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def show_open_window():
    """Bring an already-open Squish window to the front (Windows only). True if found."""
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW("TkTopLevel", "Squish")
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE: un-minimise
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def main():
    """Open the Squish window and run until it is closed.

    Only one Squish window may be open at a time (each one saves the whole
    project list, so two would overwrite each other's changes).
    """
    windows_setup()
    lock = projects.take_window_lock()
    if lock is None:
        if not show_open_window():
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo("Squish", "Squish is already open.\n\nLook for the Squish "
                                "window on the taskbar.", parent=root)
            root.destroy()
        return 0
    try:
        root = tk.Tk()
        SquishApp(root)
        root.mainloop()
    finally:
        lock.release()
    return 0
