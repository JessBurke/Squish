"""Run Squish without its window.

    python Squish.pyw run "Project name"            run a saved project
    python Squish.pyw run --source DIR [--out DIR] [--name NAME] [--squeeze standard]
                          [--part-size medium] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
                          [--keywords "a,b"] [--org "slrconsulting.com=SLR"] [--no-subfolders]
    python Squish.pyw list                           list saved projects

Options given with a saved project apply to that run only (they are not saved).
--source, --name and --no-subfolders can't be combined with a saved project name
(exit code 2): that run would reuse the project's cache and replace its digest
files with a digest of something else. A saved project's run (without --out)
that wrote files is recorded as its last run, so the window lists the new files
(projects.record_last_run; not while a Squish window is open).

Without --name, a --source run is named after the folder; a generic folder name
such as "01 Emails" gets its parent folder's name in front ("Riverside Depot -
01 Emails"), so two projects' email folders never share digest files.
A --source run whose name gives the same file names as a saved project
(projects.same_file_name) is refused (exit code 2): it would replace that
project's digest files and run log. Run the saved project by name, or use --name.

``main(argv)`` returns the process exit code: 0 ok, 1 error (including a run
that left no emails to write), 2 bad arguments, 130 cancelled with Ctrl+C.
"""

import argparse
import hashlib
import os
import re
import sys
import threading
import time

from . import engine, paths, projects
from .digest import PART_SIZES, SQUEEZE_LEVELS


def _print(text="", err=False):
    """print() that never fails (no console under pythonw, odd console encodings)."""
    stream = sys.stderr if err else sys.stdout
    if stream is None:
        return
    try:
        stream.write(text + "\n")
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        stream.write(text.encode(encoding, "replace").decode(encoding, "replace") + "\n")
    try:
        stream.flush()
    except Exception:
        pass


def build_parser():
    parser = argparse.ArgumentParser(
        prog="Squish",
        description="Turn a folder of filed emails into a compact text digest for Claude.")
    sub = parser.add_subparsers(dest="command", metavar="{run,list}")
    sub.required = True

    run = sub.add_parser("run", help="run a saved project, or a folder given with --source")
    run.add_argument("project", nargs="?", help="name of a saved project")
    run.add_argument("--source", help="folder of .msg/.eml emails (not with a saved project)")
    run.add_argument("--out", help="output folder (default: Documents\\Squish\\<name>)")
    run.add_argument("--name", help="project name used in the file names (default: the "
                                    "folder name, with its parent's for names like '01 Emails'; "
                                    "not with a saved project)")
    run.add_argument("--squeeze", choices=list(SQUEEZE_LEVELS))
    run.add_argument("--part-size", dest="part_size", choices=list(PART_SIZES))
    run.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD")
    run.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD")
    run.add_argument("--keywords", help='focus keywords, e.g. "culvert, RFI 12"')
    run.add_argument("--org", action="append", metavar="DOMAIN=CODE",
                     help='organisation codes, e.g. "slrconsulting.com=SLR" (repeat or comma-separate)')
    run.add_argument("--no-subfolders", dest="no_subfolders", action="store_true",
                     help="only read the top folder (not with a saved project)")

    sub.add_parser("list", help="list saved projects")
    return parser


def main(argv=None):
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.command == "list":
        return cmd_list()
    return cmd_run(args)


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------

def _load_projects():
    """The saved projects. A problem with the project list is reported on stderr."""
    items, problem = projects.load_projects_report()
    if problem:
        if problem.get("kind") == "damaged":
            _print("The saved project list was damaged; it has been set aside as %s"
                   % problem.get("backup"), err=True)
        else:
            _print("Could not read the saved project list %s: %s"
                   % (problem.get("path"), problem.get("error")), err=True)
    return items


def cmd_list():
    try:
        items = _load_projects()
    except Exception as exc:
        _print("Could not load the saved projects: %s" % exc, err=True)
        return 1
    if not items:
        _print("No saved projects yet. Open Squish to create one, or use: run --source DIR")
        return 0
    for p in items:
        last = p.get("last_run") or {}
        when = last.get("finished_at") if isinstance(last, dict) else ""
        _print("%s" % (p.get("name") or "(unnamed)"))
        _print("    emails: %s" % (p.get("source_folder") or "(no folder chosen)"))
        if when:
            _print("    last run: %s" % when)
    return 0


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def _find_project(items, wanted):
    """Saved project by name: exact (ignoring case), else a unique prefix."""
    key = wanted.strip().lower()
    exact = [p for p in items if (p.get("name") or "").strip().lower() == key]
    if exact:
        return exact[0]
    starts = [p for p in items if (p.get("name") or "").strip().lower().startswith(key)]
    return starts[0] if len(starts) == 1 else None


def _org_lines(values):
    pieces = []
    for value in values or []:
        pieces.extend(x.strip() for x in re.split(r"[,;\n]+", value) if x.strip())
    return "\n".join(pieces)


def _project_from_args(args):
    """Build the project to run from the command line. Returns (project, error message)."""
    if args.project:
        try:
            items = _load_projects()
        except Exception as exc:
            return None, "Could not load the saved projects: %s" % exc
        project = _find_project(items, args.project)
        if project is None:
            names = ", ".join('"%s"' % (p.get("name") or "") for p in items) or "(none)"
            return None, 'No saved project called "%s". Saved projects: %s' % (args.project, names)
        project = dict(project)
    else:
        if not args.source:
            return None, 'Give a saved project name, or a folder with --source. Try: run --help'
        source = os.path.abspath(paths.clean_folder_text(args.source))
        name = args.name or paths.default_name_for_folder(source)
        project = projects.new_project(name)
        project["source_folder"] = source
        # A stable id, so repeated command-line runs of the same folder reuse the cache.
        digest = hashlib.sha1(os.path.normcase(source).encode("utf-8", "replace")).hexdigest()
        project["id"] = "cli-" + digest[:16]
    if args.out:
        project["output_folder"] = os.path.abspath(paths.clean_folder_text(args.out))
    if args.squeeze:
        project["squeeze"] = args.squeeze
    if args.part_size:
        project["part_size"] = args.part_size
    if args.date_from is not None:
        project["date_from"] = args.date_from
    if args.date_to is not None:
        project["date_to"] = args.date_to
    if args.keywords is not None:
        project["focus_keywords"] = args.keywords
    if args.org:
        project["org_codes"] = _org_lines(args.org)
    if args.no_subfolders:
        project["include_subfolders"] = False
    return project, ""


class _ConsoleProgress(object):
    """Prints a progress line when the stage changes, then every couple of seconds."""

    def __init__(self):
        self.stage = None
        self.last = 0.0

    def __call__(self, stage, done, total, message):
        now = time.monotonic()
        if stage == self.stage and now - self.last < 2.0:
            return
        self.stage = stage
        self.last = now
        _print("  " + message, err=True)


def _format_summary(project, result):
    lines = []
    files = result.get("files") or []
    stats = result.get("stats") or {}
    lines.append("Squish: %s" % (project.get("name") or ""))
    failed = result.get("failed") or []
    folders = [f for f in failed if engine.is_folder_problem(f[1])]
    lines.append("Found %d email files (%d read, %d from cache, %d could not be read)"
                 % (result.get("files_found", 0), result.get("files_read", 0),
                    result.get("from_cache", 0), len(failed) - len(folders)))
    if folders:
        lines.append("%d folder%s could not be opened (see the run log)"
                     % (len(folders), "" if len(folders) == 1 else "s"))
    left_out = stats.get("no_access_emails") or 0
    if left_out:
        lines.append("%d email%s in a folder Squish can't open %s left out (see the run log)"
                     % (left_out, "" if left_out == 1 else "s", "is" if left_out == 1 else "are"))
    if stats:
        lines.append("Emails: %s in, %s used, %s duplicates, %s noise dropped, %s outside dates"
                     % (stats.get("emails_in", 0), stats.get("emails_used", 0),
                        stats.get("duplicates", 0), stats.get("noise_dropped", 0),
                        stats.get("outside_dates", 0)))
    if files:
        lines.append("Wrote %d file(s) to %s" % (len(files), result.get("output_folder", "")))
        for f in files:
            tokens = int(f.get("est_tokens") or 0)
            tokens_text = (format(int(round(tokens / 1000.0)), ",") + "k" if tokens >= 1000
                           else str(tokens))
            lines.append("  %s  (%s characters, ~%s tokens, %s to %s)"
                         % (os.path.basename(f["path"]), format(f["chars"], ","), tokens_text,
                            f.get("first_date") or "?", f.get("last_date") or "?"))
    elif stats and not stats.get("emails_used"):
        lines.append("No emails were left after the date and focus keyword filters, so no "
                     "digest was written. Your earlier digest files were kept.")
    else:
        lines.append("No digest files were written.")
    if result.get("log_path"):
        lines.append("Run log: %s" % result["log_path"])
    lines.append("Took %.1f s" % result.get("elapsed_s", 0.0))
    return "\n".join(lines)


def _saved_project_conflict(args):
    """Options that can't be used with a saved project name ([] if none).

    They would make the run reuse the saved project's cache and digest files
    for something else: the next run of the project would re-read every email,
    and its full digest would be replaced (or deleted)."""
    if not args.project:
        return []
    return [flag for flag, given in (("--source", args.source), ("--name", args.name),
                                     ("--no-subfolders", args.no_subfolders)) if given]


def _clashing_saved_project(project):
    """The saved project whose file names are the same as this --source run's
    (projects.same_file_name), or None.

    Such a run would delete that project's digest in the all-dates clean-up
    and overwrite its run log."""
    try:
        items = _load_projects()
    except Exception:
        return None          # can't read the list: don't block the run
    for saved in items:
        if projects.same_file_name(saved.get("name"), project.get("name")):
            return saved
    return None


def _same_folder(a, b):
    """True if two folder paths name the same folder (ignoring case on Windows)."""
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def cmd_run(args):
    conflict = _saved_project_conflict(args)
    if conflict:
        _print("%s can't be used with a saved project (\"%s\"): the run would replace that "
               "project's digest files and its cache. To squish another folder, use: "
               "run --source DIR [--name NAME]" % (" and ".join(conflict), args.project), err=True)
        return 2   # bad arguments
    project, error = _project_from_args(args)
    if project is None:
        _print(error, err=True)
        return 1
    if not args.project:
        saved = _clashing_saved_project(project)
        if saved is not None:
            if _same_folder(saved.get("source_folder"), project.get("source_folder")):
                hint = 'To run it, use: run "%s"' % saved.get("name")
            else:
                hint = "Give this run another name with --name."
            _print('This run would replace the digest files of the saved project "%s". %s'
                   % (saved.get("name"), hint), err=True)
            return 2   # bad arguments
    cancel = threading.Event()
    outcome = {}

    def work():
        try:
            outcome["result"] = engine.run_project(project, _ConsoleProgress(), cancel)
        except engine.SquishError as exc:
            outcome["error"] = str(exc)
        except Exception as exc:  # unexpected: show it rather than a silent failure
            outcome["error"] = "Unexpected error: %s: %s" % (type(exc).__name__, exc)

    _print("Squishing %s ..." % (project.get("source_folder") or ""), err=True)
    worker = threading.Thread(target=work, name="squish-run")
    worker.daemon = True
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.2)
    except KeyboardInterrupt:
        cancel.set()
        _print("Cancelling...", err=True)
        worker.join()
        return 130

    if "error" in outcome:
        _print(outcome["error"], err=True)
        return 1
    result = outcome.get("result") or {}
    if result.get("cancelled"):
        _print("Cancelled - nothing was written.", err=True)
        return 130
    _print(_format_summary(project, result))
    if args.project and not args.out and result.get("files"):
        # Like the window: the saved project remembers the files this run wrote, so
        # the window lists them (a run with --out wrote somewhere else: not recorded).
        _saved, note = projects.record_last_run(project["id"], result)
        if note:
            _print(note, err=True)
    return 0 if result.get("files") else 1


if __name__ == "__main__":
    sys.exit(main())
