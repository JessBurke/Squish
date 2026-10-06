"""Create the Squish shortcut on the Windows Desktop and in the Start Menu.

    create_desktop_shortcut()     -> (ok, message)
    create_start_menu_shortcut()  -> (ok, message)

The shortcut ("Squish.lnk") starts Squish.pyw with pythonw.exe, so no black
console window appears, and uses assets/squish.ico as its icon.

How it is made: Windows PowerShell asks Windows where the Desktop (or Start
Menu) is - this copes with a OneDrive-redirected Desktop - and writes the
shortcut with the WScript.Shell COM object. If PowerShell is locked down (for
example "Constrained Language Mode" on a managed laptop), a small VBScript run
by cscript.exe does the same job.

The PowerShell and VBScript code never has file paths pasted into it: every
value is handed over in an environment variable (SQUISH_SC_*), so names like
O'Brien, "R&D (2025)" or non-English characters can't break the script.

The PowerShell way also gives the shortcut Squish's taskbar id (paths.APP_ID),
so a pinned shortcut and the open window share one taskbar button.
"""

import ntpath
import os
import subprocess
import sys
import tempfile

from . import paths

SHORTCUT_NAME = "Squish.lnk"
DESCRIPTION = "Squish - email digests for Claude"
NOT_WINDOWS_MESSAGE = "Desktop shortcuts are only created on Windows"
CREATE_NO_WINDOW = 0x08000000
TIMEOUT_S = 60

# Where each shortcut goes: (name used by Windows, name shown to the user)
DESKTOP = ("Desktop", "Desktop")
START_MENU = ("Programs", "Start Menu")

ENV_NAMES = ("SQUISH_SC_FOLDER", "SQUISH_SC_NAME", "SQUISH_SC_TARGET", "SQUISH_SC_ARGS",
             "SQUISH_SC_WORKDIR", "SQUISH_SC_ICON", "SQUISH_SC_DESC")
# Only used by the PowerShell script (the VBScript fallback can't set the app id).
APP_ID_ENV_NAMES = ("SQUISH_SC_APPID", "SQUISH_SC_CS")

# The Squish window tells Windows it is "Squish.EmailDigest" (paths.APP_ID, see
# gui.windows_setup). The shortcut must carry the same id, otherwise the taskbar
# treats the shortcut and the window as two different programs: a pinned
# shortcut gets a second taskbar button, and pinning the window can't start
# Squish again. WScript.Shell can't set that id, so this small C# helper does
# it (System.AppUserModel.ID = {9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}, 5).
# PowerShell compiles it with Add-Type. It is passed in SQUISH_SC_CS, and if
# it fails (a locked-down PC), the shortcut still works without the id.
APP_ID_CSHARP = r"""
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

public static class SquishShortcutId
{
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    private class ShellLink { }

    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
     Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
    private interface IPropertyStore
    {
        void GetCount(out uint count);
        void GetAt(uint index, out PropertyKey key);
        void GetValue(ref PropertyKey key, out PropVariant value);
        void SetValue(ref PropertyKey key, ref PropVariant value);
        void Commit();
    }

    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    private struct PropertyKey
    {
        public Guid FormatId;
        public uint PropertyId;
    }

    [StructLayout(LayoutKind.Explicit, Size = 24)]
    private struct PropVariant
    {
        [FieldOffset(0)] public ushort VarType;
        [FieldOffset(8)] public IntPtr Pointer;
    }

    public static void SetAppId(string lnkPath, string appId)
    {
        object link = new ShellLink();
        try
        {
            ((IPersistFile)link).Load(lnkPath, 2);
            IPropertyStore store = (IPropertyStore)link;
            PropertyKey key = new PropertyKey();
            key.FormatId = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
            key.PropertyId = 5;
            PropVariant value = new PropVariant();
            value.VarType = 31;
            value.Pointer = Marshal.StringToCoTaskMemUni(appId);
            try
            {
                store.SetValue(ref key, ref value);
                store.Commit();
            }
            finally
            {
                Marshal.FreeCoTaskMem(value.Pointer);
            }
            ((IPersistFile)link).Save(lnkPath, true);
        }
        finally
        {
            Marshal.ReleaseComObject(link);
        }
    }
}
"""

# One line, no double quotes: passed to powershell.exe as a single argument.
# Every value comes from $env:SQUISH_SC_* - nothing is pasted in.
POWERSHELL_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "try { "
    "try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }; "
    "$dir = [Environment]::GetFolderPath($env:SQUISH_SC_FOLDER); "
    "if (-not $dir) { throw 'Windows did not say where that folder is.' }; "
    "[System.IO.Directory]::CreateDirectory($dir) | Out-Null; "
    "$lnk = [System.IO.Path]::Combine($dir, $env:SQUISH_SC_NAME); "
    "$shell = New-Object -ComObject WScript.Shell; "
    "$sc = $shell.CreateShortcut($lnk); "
    "$sc.TargetPath = $env:SQUISH_SC_TARGET; "
    "$sc.Arguments = $env:SQUISH_SC_ARGS; "
    "$sc.WorkingDirectory = $env:SQUISH_SC_WORKDIR; "
    "if ($env:SQUISH_SC_ICON) { $sc.IconLocation = $env:SQUISH_SC_ICON }; "
    "$sc.Description = $env:SQUISH_SC_DESC; "
    "$sc.Save(); "
    "if ($env:SQUISH_SC_APPID -and $env:SQUISH_SC_CS) { try { "
    "Add-Type -TypeDefinition $env:SQUISH_SC_CS; "
    "[SquishShortcutId]::SetAppId($lnk, $env:SQUISH_SC_APPID) "
    "} catch { } }; "
    "Write-Output ('OK ' + $lnk) "
    "} catch { Write-Output ('FAILED ' + $_.Exception.Message); exit 1 }"
)

# Fallback for when PowerShell is blocked. Plain ASCII, values from the environment.
VBS_SCRIPT = "\r\n".join([
    "Option Explicit",
    "On Error Resume Next",
    "Dim shell, env, fso, folder, lnkPath, sc",
    "Set shell = CreateObject(\"WScript.Shell\")",
    "If Err.Number <> 0 Then Fail \"WScript.Shell is not available: \" & Err.Description",
    "Set env = shell.Environment(\"PROCESS\")",
    "folder = shell.SpecialFolders(env(\"SQUISH_SC_FOLDER\"))",
    "If Err.Number <> 0 Or folder = \"\" Then Fail \"Windows did not say where that folder is.\"",
    "Set fso = CreateObject(\"Scripting.FileSystemObject\")",
    "If Not fso.FolderExists(folder) Then fso.CreateFolder folder",
    "lnkPath = fso.BuildPath(folder, env(\"SQUISH_SC_NAME\"))",
    "Set sc = shell.CreateShortcut(lnkPath)",
    "If Err.Number <> 0 Then Fail \"Could not start the shortcut: \" & Err.Description",
    "sc.TargetPath = env(\"SQUISH_SC_TARGET\")",
    "sc.Arguments = env(\"SQUISH_SC_ARGS\")",
    "sc.WorkingDirectory = env(\"SQUISH_SC_WORKDIR\")",
    "If env(\"SQUISH_SC_ICON\") <> \"\" Then sc.IconLocation = env(\"SQUISH_SC_ICON\")",
    "sc.Description = env(\"SQUISH_SC_DESC\")",
    "sc.Save",
    "If Err.Number <> 0 Then Fail \"Could not save the shortcut: \" & Err.Description",
    "WScript.Echo \"OK \" & lnkPath",
    "WScript.Quit 0",
    "",
    "Sub Fail(message)",
    "  WScript.Echo \"FAILED \" & message",
    "  WScript.Quit 1",
    "End Sub",
    "",
])


# --------------------------------------------------------------------------
# Working out what the shortcut should run (pure functions, unit-tested)
# --------------------------------------------------------------------------

def is_windows():
    return os.name == "nt"


def windowless_python(executable, exists=os.path.exists, localappdata=None):
    """The pythonw.exe that matches ``executable`` (normally sys.executable).

    python.exe -> pythonw.exe and python3.12.exe -> pythonw3.12.exe in the same
    folder (this also covers virtual environments), py.exe -> pyw.exe, and a
    Microsoft Store Python under "Program Files\\WindowsApps" (which can't be
    started directly - this applies to its pythonw.exe too) -> its alias in
    %LOCALAPPDATA%\\Microsoft\\WindowsApps.
    Falls back to ``executable`` itself if no windowless version exists.
    """
    if not executable:
        return ""
    folder, name = ntpath.split(executable)
    lower = name.lower()
    candidates = []
    is_windowless = lower.startswith("pythonw") or lower.startswith("pyw")
    if lower.startswith("python") and "\\program files\\windowsapps\\" in executable.lower():
        windowless = name if is_windowless else name[:6] + "w" + name[6:]
        candidates.extend(_store_alias_candidates(folder, windowless, localappdata))
    if not is_windowless:
        if lower.startswith("python"):
            candidates.append(ntpath.join(folder, name[:6] + "w" + name[6:]))
        elif lower in ("py.exe", "py"):
            candidates.append(ntpath.join(folder, "pyw" + name[2:]))
    for candidate in candidates:
        if exists(candidate):
            return candidate
    return executable


def _store_alias_candidates(folder, windowless, localappdata=None):
    """App-alias paths for a Microsoft Store Python found in Program Files\\WindowsApps."""
    localappdata = localappdata if localappdata is not None else os.environ.get("LOCALAPPDATA", "")
    if not localappdata:
        return []
    aliases = ntpath.join(localappdata, "Microsoft", "WindowsApps")
    # e.g. PythonSoftwareFoundation.Python.3.12_3.12.1008.0_x64__qbz5n2kfra8p0
    package = ntpath.basename(folder.rstrip("\\/"))
    result = []
    pieces = package.split("_")
    if len(pieces) >= 2 and pieces[0].lower().startswith("pythonsoftwarefoundation.python."):
        family = "%s_%s" % (pieces[0], pieces[-1])
        result.append(ntpath.join(aliases, family, "pythonw.exe"))
        version = pieces[0][len("PythonSoftwareFoundation.Python."):]  # "3.12"
        if version:
            result.append(ntpath.join(aliases, "pythonw%s.exe" % version))
    result.append(ntpath.join(aliases, windowless))
    result.append(ntpath.join(aliases, "pythonw.exe"))
    return result


def quote_argument(path):
    """Wrap a path in double quotes for a shortcut's Arguments field."""
    return '"%s"' % str(path).replace('"', "")


def shortcut_values(folder_key, app_dir=None, executable=None, frozen=None,
                    exists=os.path.exists):
    """The SQUISH_SC_* environment values for one shortcut (a dict of strings)."""
    app_dir = str(app_dir if app_dir is not None else paths.app_dir())
    executable = executable if executable is not None else (sys.executable or "")
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if frozen:
        target, arguments = executable, ""
    else:
        target = windowless_python(executable, exists=exists)
        arguments = quote_argument(ntpath.join(app_dir, "Squish.pyw"))
    icon = ntpath.join(app_dir, "assets", "squish.ico")
    return {
        "SQUISH_SC_FOLDER": folder_key,
        "SQUISH_SC_NAME": SHORTCUT_NAME,
        "SQUISH_SC_TARGET": target,
        "SQUISH_SC_ARGS": arguments,
        "SQUISH_SC_WORKDIR": app_dir,
        "SQUISH_SC_ICON": (icon + ",0") if exists(icon) else "",
        "SQUISH_SC_DESC": DESCRIPTION,
        "SQUISH_SC_APPID": paths.APP_ID,
        "SQUISH_SC_CS": APP_ID_CSHARP,
    }


def child_environment(values, base=None):
    """A copy of the current environment with ``values`` added."""
    env = dict(os.environ if base is None else base)
    env.update(values)
    return env


def _system_tool(*parts):
    """Full path of a Windows tool in System32 if it is there, else just its name."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    full = os.path.join(root, "System32", *parts)
    return full if os.path.exists(full) else parts[-1]


def powershell_exe():
    """Windows PowerShell 5.1 (powershell.exe), which every Windows 10/11 PC has."""
    return _system_tool("WindowsPowerShell", "v1.0", "powershell.exe")


def powershell_command(powershell=None):
    """The command line (a list) that runs POWERSHELL_SCRIPT."""
    exe = powershell or powershell_exe()
    return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-Command", POWERSHELL_SCRIPT]


def cscript_command(vbs_path, cscript=None):
    """The command line (a list) that runs the fallback VBScript file."""
    exe = cscript or _system_tool("cscript.exe")
    return [exe, "//nologo", "//T:%d" % (TIMEOUT_S - 5), vbs_path]


def parse_output(text):
    """(ok, detail) from the script's output: 'OK <path>' or 'FAILED <reason>'."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("OK "):
            return True, line[3:].strip()
        if line.startswith("FAILED "):
            return False, line[7:].strip()
    return False, (lines[-1] if lines else "no response")


# --------------------------------------------------------------------------
# Running it (Windows only)
# --------------------------------------------------------------------------

def _decode(data):
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("mbcs" if is_windows() else "latin-1", "replace")


def _run(command, env):
    """Run a helper program without a console window. Returns (ok, detail)."""
    try:
        done = subprocess.run(
            command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=TIMEOUT_S,
            creationflags=CREATE_NO_WINDOW if is_windows() else 0)
    except subprocess.TimeoutExpired:
        return False, "it did not finish within %d seconds" % TIMEOUT_S
    except OSError as exc:
        return False, "it could not be started (%s)" % (exc.strerror or exc)
    ok, detail = parse_output(_decode(done.stdout))
    if done.returncode != 0:
        ok = False
        if detail == "no response":
            err = _decode(done.stderr).strip().splitlines()
            detail = err[-1].strip() if err else "exit code %d" % done.returncode
    return ok, detail


def _run_vbs(env):
    fd, vbs_path = tempfile.mkstemp(prefix="squish-shortcut-", suffix=".vbs")
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="") as fh:
            fh.write(VBS_SCRIPT)
        return _run(cscript_command(vbs_path), env)
    except OSError as exc:
        return False, "could not write a temporary script (%s)" % (exc.strerror or exc)
    finally:
        try:
            os.remove(vbs_path)
        except OSError:
            pass


def create_shortcut(where=DESKTOP, app_dir=None):
    """Create Squish.lnk in ``where`` (DESKTOP or START_MENU). Returns (ok, message)."""
    folder_key, label = where
    if not is_windows():
        return False, NOT_WINDOWS_MESSAGE
    values = shortcut_values(folder_key, app_dir=app_dir)
    if not values["SQUISH_SC_TARGET"]:
        return False, "Squish couldn't work out which Python program to use for the shortcut."
    env = child_environment(values)

    ok, ps_detail = _run(powershell_command(), env)
    if ok:
        return True, "%s shortcut created:\n%s" % (label, ps_detail)
    ok, vbs_detail = _run_vbs(env)
    if ok:
        return True, "%s shortcut created:\n%s" % (label, vbs_detail)
    return False, failure_message(label, ps_detail, vbs_detail)


def failure_message(label, ps_detail, vbs_detail):
    """Plain-English explanation when neither method worked."""
    return (
        "Squish couldn't create the %s shortcut automatically - this computer blocks "
        "the tools it uses.\n"
        "  PowerShell: %s\n"
        "  Windows Script Host: %s\n\n"
        "You can make one yourself: open the Squish folder in File Explorer, right-click "
        "Squish.pyw and choose Send to > Desktop (create shortcut). On Windows 11, click "
        "'Show more options' first. Or just double-click Squish.pyw to start Squish."
        % (label, ps_detail, vbs_detail))


def create_desktop_shortcut(app_dir=None):
    """Put a Squish shortcut on the Desktop. Returns (ok, message)."""
    return create_shortcut(DESKTOP, app_dir)


def create_start_menu_shortcut(app_dir=None):
    """Put a Squish shortcut in the Start Menu. Returns (ok, message)."""
    return create_shortcut(START_MENU, app_dir)


def create_all_shortcuts(app_dir=None):
    """Desktop and Start Menu shortcuts. Returns a list of two (ok, message) pairs."""
    if not is_windows():
        return [(False, NOT_WINDOWS_MESSAGE)]
    return [create_desktop_shortcut(app_dir), create_start_menu_shortcut(app_dir)]
