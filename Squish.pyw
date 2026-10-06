"""Double-click this file to start Squish.

Squish turns a folder of filed Outlook emails into one compact text file you
can drag into Claude. See README.md.

    Squish.pyw                      open the Squish window
    Squish.pyw run "Project name"   run a saved project without the window
    Squish.pyw list                 list saved projects
    Squish.pyw --create-shortcuts   make the Desktop and Start Menu shortcuts
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _tell(text):
    """Show a message box if possible, else print (used before Squish itself loads)."""
    try:
        try:
            import tkinter
            from tkinter import messagebox
        except ImportError:  # Python 2
            import Tkinter as tkinter
            import tkMessageBox as messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("Squish", text)
        root.destroy()
    except Exception:
        # No tkinter: on Windows show a plain message box (pythonw has no console).
        shown = False
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, u"" + text, u"Squish", 0x10)
                shown = True
            except Exception:
                pass
        if not shown and sys.stdout is not None:
            sys.stdout.write(text + "\n")


if __name__ == "__main__":
    if sys.version_info < (3, 8):
        _tell("Squish needs Python 3.8 or newer, but this is Python %d.%d.\n\n"
              "Install a newer Python from python.org (tick 'Add python.exe to PATH'), "
              "then double-click 'Install Squish.bat' again." % sys.version_info[:2])
        sys.exit(1)
    try:
        from squish_app.__main__ import main
    except Exception as exc:
        _tell("Squish could not start: %s: %s\n\nThe squish_app folder should be next to "
              "Squish.pyw:\n%s" % (type(exc).__name__, exc, HERE))
        sys.exit(1)
    sys.exit(main(sys.argv[1:]))
