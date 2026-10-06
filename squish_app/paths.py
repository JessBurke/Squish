"""Where Squish keeps its files.

Everything Squish writes for itself lives in per-user folders: the project
list in the data folder (on Windows %APPDATA%\\Squish, which may roam with the
user's profile), and the bulky scan cache and run logs in the local data folder
(on Windows %LOCALAPPDATA%\\Squish, which never roams; elsewhere it is the same
as the data folder). The data folder also holds squish-window.lock, which
stops a second Squish window from editing the project list at the same time.
Digests go to each project's output folder, which defaults to
Documents/Squish/<project name>.

Set the SQUISH_DATA_DIR environment variable to use a different data folder
for everything (the tests do this so they never touch the real one).
"""

import hashlib
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote

APP_NAME = "Squish"
APP_ID = "Squish.EmailDigest"  # Windows taskbar grouping id


def app_dir():
    """Folder that contains Squish.pyw, the assets folder and squish_app."""
    return Path(__file__).resolve().parent.parent


def assets_dir():
    return app_dir() / "assets"


def app_folder(local, environ, os_name, platform, home):
    """The per-user Squish folder as a string (not created).

    ``local`` picks the non-roaming folder on Windows. The other arguments are
    os.environ, os.name, sys.platform and the home folder, passed in so the
    choice can be tested for every platform.
    """
    override = environ.get("SQUISH_DATA_DIR")
    if override:
        return override
    if os_name == "nt":
        if local:
            base = environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        else:
            base = environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        return os.path.join(base, APP_NAME)
    if platform == "darwin":
        return os.path.join(home, "Library", "Application Support", APP_NAME)
    return os.path.join(environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share"),
                        "squish")


def _made(folder):
    path = Path(folder)
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir():
    """Folder for the project list (roams with the Windows profile)."""
    return _made(app_folder(False, os.environ, os.name, sys.platform, str(Path.home())))


def local_data_dir():
    """Folder for the scan cache and run logs (never roams on Windows)."""
    return _made(app_folder(True, os.environ, os.name, sys.platform, str(Path.home())))


def projects_file():
    return data_dir() / "projects.json"


def cache_dir():
    return _made(local_data_dir() / "cache")


def logs_dir():
    return _made(local_data_dir() / "logs")


def documents_dir():
    """The user's Documents folder (OneDrive-redirected on Windows if applicable)."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            # CSIDL_PERSONAL = 5 (My Documents), SHGFP_TYPE_CURRENT = 0
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:
                return Path(buf.value)
        except Exception:
            pass
    docs = Path.home() / "Documents"
    return docs if docs.is_dir() else Path.home()


# Names Windows keeps for devices: a file or folder can't be called this, with
# or without an extension ("CON", "con.txt", "Con.Ltd"). COM/LPT go from 0 to 9,
# plus the superscript digits 1-3.
_RESERVED_NAMES = set(["CON", "PRN", "AUX", "NUL"]
                      + ["COM%s" % d for d in "0123456789\u00b9\u00b2\u00b3"]
                      + ["LPT%s" % d for d in "0123456789\u00b9\u00b2\u00b3"])


def safe_filename(name, fallback="project"):
    """Make a string safe to use as a Windows file or folder name.

    Bad characters become spaces, the result is at most 80 characters and never
    ends in a space or dot, and when the part before the first dot is a device
    name such as CON, a "_" is added after the device name ("CON" -> "CON_",
    "Con.Ltd" -> "Con_.Ltd").
    """
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = name[:80].rstrip(" .")
    base = name.split(".")[0]
    if name and base.strip().upper() in _RESERVED_NAMES:
        device = base.rstrip()
        name = device + "_" + name[len(device):]
    return name or fallback


def clean_folder_text(text):
    """Tidy a typed or pasted folder path: trim spaces and the double quotes that
    Explorer's 'Copy as path' adds. A double quote is never valid in a Windows path.
    A pasted file:// link (from an email or a browser, maybe in <...>) becomes the
    plain folder path, see file_link_to_path()."""
    text = (text or "").strip().strip('"').strip()
    if text.startswith("<") and text.endswith(">") and text[1:6].lower() == "file:":
        text = text[1:-1].strip()
    if text[:5].lower() == "file:":
        text = file_link_to_path(text)
    return text


_LINK_DRIVE_RE = re.compile(r"^([A-Za-z])[:|](.*)$", re.S)


def file_link_to_path(link):
    """A file:// link as a folder path:
    'file:///H:/Jobs/01%20Emails'   -> 'H:\\Jobs\\01 Emails'
    'file://server/share/Jobs'      -> '\\\\server\\share\\Jobs'  (also file:////server/...)
    'file:///home/sam/mail'         -> '/home/sam/mail'
    Text that isn't a file: link is returned unchanged."""
    if link[:5].lower() != "file:":
        return link
    rest = unquote(link[5:])
    host = ""
    if rest.startswith("//"):
        host, _sep, rest = rest[2:].partition("/")
        if host.lower() == "localhost":
            host = ""
        if _LINK_DRIVE_RE.match(host):       # file://H:/Jobs (one slash short)
            rest, host = host + "/" + rest, ""
    else:
        rest = rest.lstrip("/")
    if host:                                 # file://server/share/Jobs
        return "\\\\" + host + "\\" + rest.replace("/", "\\")
    drive = _LINK_DRIVE_RE.match(rest)
    if drive:                                # file:///H:/Jobs
        return drive.group(1) + ":" + drive.group(2).replace("/", "\\")
    if rest.startswith("\\\\"):              # file:///\\server\share
        return rest.replace("/", "\\")
    if rest.startswith("/"):                 # file:////server/share
        return "\\\\" + rest.lstrip("/").replace("/", "\\")
    return "/" + rest                        # file:///home/sam/mail


# Folder names that say nothing about the project ("01 Emails", "Correspondence").
GENERIC_FOLDER_RE = re.compile(
    r"^(general |project |filed |incoming |outgoing |sent |received )?"
    r"(e-?mails?|mail|correspondence|inbox|messages|outlook|items)$", re.I)


def default_name_for_folder(folder):
    """Project name for a folder: its own name, or 'Parent - 01 Emails' when the
    folder name is generic, so two projects' '01 Emails' folders get different names."""
    names = [n for n in re.split(r"[\\/]+", folder or "") if n]
    base = names[-1] if names else ""
    plain = re.sub(r"^[\d\s._-]+", "", base).strip()
    if base and (not plain or GENERIC_FOLDER_RE.match(plain)) and len(names) > 1:
        parent = names[-2]
        if not parent.endswith(":"):
            return "%s - %s" % (parent, base)
    return base if base and not base.endswith(":") else "Emails"


OUTPUT_NAME_MAX = 42  # longer project names are shortened in folder and file names


def output_name(project_name):
    """The project name as used in the output folder and digest file names.

    Like safe_filename(), but a name longer than OUTPUT_NAME_MAX characters is
    cut short and given a 6-character code made from the whole name
    ("Riverside Depot Upgrade Stage 2 Civil and ~3f9a1c"), so deep network
    folders don't push the digest paths past Windows' 260-character limit and
    two long names that start the same way still get different files.
    """
    safe = safe_filename(project_name, "Project")
    if len(safe) <= OUTPUT_NAME_MAX:
        return safe
    code = hashlib.sha1(safe.lower().encode("utf-8", "surrogatepass")).hexdigest()[:6]
    return "%s ~%s" % (safe[:OUTPUT_NAME_MAX].rstrip(" .-"), code)


def default_output_folder(project_name):
    return documents_dir() / APP_NAME / output_name(project_name)


def long_path(p):
    """Return a path string that gets around the Windows 260-character limit.

    No-op on other platforms. Accepts str or Path; returns str.
    """
    p = str(p)
    if os.name != "nt":
        return p
    p = os.path.abspath(p)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def short_path(p):
    """Undo long_path() for display."""
    p = str(p)
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p
