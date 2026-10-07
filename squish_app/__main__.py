"""Start Squish. ``python -m squish_app`` does exactly what Squish.pyw does.

    (no arguments)                 open the Squish window
    run ...  /  list               use Squish from the command line (see cli.py)
    --create-shortcuts             make the Desktop and Start Menu shortcuts
    --console                      print results instead of showing a message box
    --version                      print the version

If the window can't start, the error is shown in a message box and saved in
a crash log, because pythonw (used by the shortcut) has nowhere to print it.
"""

import os
import sys
import tempfile
import time
import traceback

USAGE = """Squish - squash a project's emails and documents into small files you can drop into Claude.

  Squish.pyw                         open the Squish window
  Squish.pyw run "Project name"      run a saved project without the window
  Squish.pyw run --source FOLDER     run a folder (see: Squish.pyw run --help)
  Squish.pyw list                    list saved projects
  Squish.pyw --create-shortcuts      make the Desktop and Start Menu shortcuts
             [--console]             (print the result instead of a message box)
  Squish.pyw --version
"""


def say(text):
    """Print, if there is anywhere to print to (pythonw has no console)."""
    stream = sys.stdout
    if stream is None:
        return
    try:
        stream.write(text + "\n")
        stream.flush()
    except UnicodeEncodeError:
        enc = getattr(stream, "encoding", None) or "ascii"
        stream.write(text.encode(enc, "replace").decode(enc, "replace") + "\n")
    except Exception:
        pass


def _native_box(title, text, error=False):
    """A plain Windows message box that needs no tkinter. True if it was shown.

    Used when tkinter itself is missing or broken: under pythonw there is no
    console, so without this the user would see nothing at all.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        icon = 0x10 if error else 0x40      # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), icon)
        return True
    except Exception:
        return False


def show_message(title, text, error=False):
    """A message box (a plain Windows one if tkinter is unavailable, else printed)."""
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        try:
            if error:
                messagebox.showerror(title, text, parent=root)
            else:
                messagebox.showinfo(title, text, parent=root)
        finally:
            root.destroy()
    except Exception:
        if not _native_box(title, text, error):
            say("%s\n\n%s" % (title, text))


def write_crash_log(details):
    """Save crash details to the Squish logs folder (or the temp folder). Returns the path."""
    from . import __version__
    stamp = time.strftime("%Y-%m-%d %H%M%S")
    text = "Squish %s crash report, %s\nPython %s on %s\n\n%s\n" % (
        __version__, time.strftime("%Y-%m-%d %H:%M:%S"), sys.version.split()[0],
        sys.platform, details)
    folders = []
    try:
        from . import paths
        folders.append(str(paths.logs_dir()))
    except Exception:
        pass
    folders.append(tempfile.gettempdir())
    for folder in folders:
        path = os.path.join(folder, "Squish crash %s.txt" % stamp)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            return path
        except OSError:
            continue
    return ""


def report_crash(details):
    """Tell the user Squish couldn't start, and where the details are."""
    log = write_crash_log(details)
    last = details.strip().splitlines()[-1] if details.strip() else "unknown error"
    message = "Squish hit a problem and has to close.\n\n%s" % last
    if "No module named" in last and "tkinter" in last:
        message = ("This Python doesn't include tkinter, which Squish needs for its window.\n\n"
                   "Run the Python installer from python.org again, choose Modify, tick "
                   "'tcl/tk and IDLE', then double-click 'Install Squish.bat' again.")
    if log:
        message += "\n\nThe details are saved in:\n%s" % log
    say(details)
    show_message("Squish", message, error=True)


def create_shortcuts(console):
    """Make the Desktop and Start Menu shortcuts and report how it went."""
    from . import shortcut
    results = shortcut.create_all_shortcuts()
    ok = all(result[0] for result in results)
    text = "\n\n".join(message for _, message in results)
    if console:
        say("\n".join("  " + line for line in text.splitlines()))
    else:
        show_message("Squish shortcuts", text, error=not ok)
    return 0 if ok else 1


def start_gui():
    """Open the Squish window; any crash is shown and logged instead of vanishing."""
    try:
        from . import gui
        return gui.main() or 0
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception:
        report_crash(traceback.format_exc())
        return 1


def main(argv=None):
    """Run Squish with command-line arguments ``argv`` (default sys.argv[1:])."""
    argv = list(sys.argv[1:] if argv is None else argv)
    console = "--console" in argv or sys.stdout is not None
    argv = [a for a in argv if a != "--console"]
    if not argv:
        return start_gui()
    command = argv[0]
    if command in ("run", "list"):
        from . import cli
        return cli.main(argv)
    if command == "--create-shortcuts":
        return create_shortcuts(console)
    if command in ("--version", "-V"):
        from . import __version__
        say("Squish %s" % __version__)
        return 0
    if command in ("-h", "--help", "/?", "help"):
        say(USAGE)
        return 0
    if sys.stdout is None:
        # e.g. a folder dropped onto the shortcut: just open the window
        return start_gui()
    say("Unknown option: %s\n\n%s" % (command, USAGE))
    return 2


if __name__ == "__main__":
    sys.exit(main())
