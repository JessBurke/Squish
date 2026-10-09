"""Create the Squish shortcut on the Windows Desktop and in the Start Menu.

    create_desktop_shortcut()     -> (ok, message)
    create_start_menu_shortcut()  -> (ok, message)

The shortcut ("Squish.lnk") starts Squish.pyw with pythonw.exe, so no black
console window appears, and uses assets/squish.ico as its icon.

How it is made, trying each way in turn until one works:

1. Windows' own shortcut API (IShellLinkW), called from Python with ctypes.
   It needs no PowerShell or script host, so the usual lock-downs on managed
   laptops (Constrained Language Mode, AppLocker script rules) don't stop it.
   It runs in a separate Python process, so a failure there can never crash
   Squish itself.
2. Windows PowerShell, which writes the shortcut with the WScript.Shell COM
   object.
3. A small VBScript run by cscript.exe.

Each way asks Windows where the Desktop (or Start Menu) is, so a
OneDrive-redirected Desktop works.

The scripts and the helper process never have file paths pasted into them: every
value is handed over in an environment variable (SQUISH_SC_*), so names like
O'Brien, "R&D (2025)" or non-English characters can't break the script.

The API and PowerShell ways also give the shortcut Squish's taskbar id (paths.APP_ID),
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


# --------------------------------------------------------------------------
# The native way: Windows' shortcut API, called through ctypes in a helper
# Python process (so a crash there can't take Squish down)
# --------------------------------------------------------------------------

CLSID_SHELL_LINK = "00021401-0000-0000-C000-000000000046"
IID_SHELL_LINK_W = "000214F9-0000-0000-C000-000000000046"
IID_PERSIST_FILE = "0000010B-0000-0000-C000-000000000046"
IID_PROPERTY_STORE = "886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"
PKEY_APP_USER_MODEL = ("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3", 5)   # System.AppUserModel.ID

# Where each folder key points: CSIDL_DESKTOPDIRECTORY and CSIDL_PROGRAMS.
CSIDL_FOLDERS = {"Desktop": 0x0010, "Programs": 0x0002}
CSIDL_FLAG_CREATE = 0x8000

# Method numbers in the COM interfaces' function tables (after IUnknown's 0-2).
SHELL_LINK_SET_DESCRIPTION = 7
SHELL_LINK_SET_WORKING_DIRECTORY = 9
SHELL_LINK_SET_ARGUMENTS = 11
SHELL_LINK_SET_SHOW_CMD = 15
SHELL_LINK_SET_ICON_LOCATION = 17
SHELL_LINK_SET_PATH = 20
PERSIST_FILE_SAVE = 6
PROPERTY_STORE_SET_VALUE = 6
PROPERTY_STORE_COMMIT = 7

# Run by the helper process: -I keeps it clear of the user's Python settings.
NATIVE_BOOTSTRAP = ("import os, sys; sys.path.insert(0, os.environ['SQUISH_SC_APPDIR']); "
                    "from squish_app import shortcut; sys.exit(shortcut.native_main())")


def console_python(executable, exists=os.path.exists):
    """The python.exe that matches ``executable`` (pythonw.exe -> python.exe).

    The helper process needs a console Python so its output can be read back.
    Falls back to ``executable`` itself.
    """
    if not executable:
        return ""
    folder, name = ntpath.split(executable)
    lower = name.lower()
    if lower.startswith("pythonw"):
        candidate = ntpath.join(folder, name[:6] + name[7:])
        if exists(candidate):
            return candidate
    return executable


def native_command(executable=None):
    """The command line (a list) that makes the shortcut with Windows' own API."""
    python = console_python(executable if executable is not None else (sys.executable or ""))
    return [python, "-I", "-c", NATIVE_BOOTSTRAP]


def split_icon_location(text):
    """'C:\\x\\squish.ico,0' -> ('C:\\x\\squish.ico', 0)."""
    path, sep, index = (text or "").rpartition(",")
    if sep and index.strip().lstrip("-").isdigit():
        return path, int(index)
    return text or "", 0


def _native_create(env):
    """Make the shortcut described by the SQUISH_SC_* values in ``env``.

    Returns the shortcut's full path; raises OSError (or ValueError) if it
    can't be made. Windows only.
    """
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", ctypes.c_uint32)]

    class PROPVARIANT(ctypes.Structure):
        # vt, three reserved words, then the value (a pointer for VT_LPWSTR);
        # padded to the full 24-byte size of a 64-bit PROPVARIANT.
        _fields_ = [("vt", ctypes.c_ushort), ("reserved1", ctypes.c_ushort),
                    ("reserved2", ctypes.c_ushort), ("reserved3", ctypes.c_ushort),
                    ("value", ctypes.c_void_p), ("padding", ctypes.c_void_p)]

    def make_guid(text):
        value = uuid.UUID(text)
        guid = GUID(value.fields[0], value.fields[1], value.fields[2])
        guid.Data4 = (ctypes.c_ubyte * 8)(*bytearray(value.bytes[8:]))
        return guid

    def method(obj, index, restype, *argtypes):
        """Function number ``index`` of the COM object ``obj``, ready to call."""
        table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        function = prototype(table[index])
        return lambda *args: function(obj, *args)

    def query(obj, iid_text):
        iid = make_guid(iid_text)
        result = ctypes.c_void_p()
        method(obj, 0, ctypes.HRESULT, ctypes.POINTER(GUID),
               ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(iid), ctypes.byref(result))
        if not result.value:
            raise OSError("the shortcut object has no %s" % iid_text)
        return result

    def release(obj):
        if obj is not None and obj.value:
            method(obj, 2, ctypes.c_ulong)()

    folder_key = env.get("SQUISH_SC_FOLDER", "")
    if folder_key not in CSIDL_FOLDERS:
        raise ValueError("unknown folder %r" % folder_key)
    buffer = ctypes.create_unicode_buffer(1024)
    shell32 = ctypes.windll.shell32
    shell32.SHGetFolderPathW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE,
                                         wintypes.DWORD, wintypes.LPWSTR]
    shell32.SHGetFolderPathW.restype = ctypes.c_long
    result = shell32.SHGetFolderPathW(None, CSIDL_FOLDERS[folder_key] | CSIDL_FLAG_CREATE,
                                      None, 0, buffer)
    if result != 0 or not buffer.value:
        raise OSError("Windows did not say where the %s folder is (0x%08X)"
                      % (folder_key, result & 0xFFFFFFFF))
    folder = buffer.value
    if not os.path.isdir(folder):
        os.makedirs(folder)
    lnk_path = os.path.join(folder, env.get("SQUISH_SC_NAME") or SHORTCUT_NAME)

    ole32 = ctypes.windll.ole32
    ole32.CoInitialize.argtypes = [ctypes.c_void_p]
    ole32.CoInitialize.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoCreateInstance.restype = ctypes.c_long
    initialised = ole32.CoInitialize(None) >= 0     # S_OK or S_FALSE
    link = persist = store = None
    try:
        clsid = make_guid(CLSID_SHELL_LINK)
        iid = make_guid(IID_SHELL_LINK_W)
        link = ctypes.c_void_p()
        result = ole32.CoCreateInstance(ctypes.byref(clsid), None, 1,   # CLSCTX_INPROC_SERVER
                                        ctypes.byref(iid), ctypes.byref(link))
        if result != 0 or not link.value:
            raise OSError("Windows' shortcut object could not be created (0x%08X)"
                          % (result & 0xFFFFFFFF))
        text = wintypes.LPCWSTR
        method(link, SHELL_LINK_SET_PATH, ctypes.HRESULT, text)(env.get("SQUISH_SC_TARGET", ""))
        method(link, SHELL_LINK_SET_ARGUMENTS, ctypes.HRESULT, text)(env.get("SQUISH_SC_ARGS", ""))
        method(link, SHELL_LINK_SET_WORKING_DIRECTORY, ctypes.HRESULT, text)(
            env.get("SQUISH_SC_WORKDIR", ""))
        method(link, SHELL_LINK_SET_DESCRIPTION, ctypes.HRESULT, text)(env.get("SQUISH_SC_DESC", ""))
        icon_path, icon_index = split_icon_location(env.get("SQUISH_SC_ICON", ""))
        if icon_path:
            method(link, SHELL_LINK_SET_ICON_LOCATION, ctypes.HRESULT, text, ctypes.c_int)(
                icon_path, icon_index)
        method(link, SHELL_LINK_SET_SHOW_CMD, ctypes.HRESULT, ctypes.c_int)(1)   # SW_SHOWNORMAL

        app_id = env.get("SQUISH_SC_APPID", "")
        if app_id:
            try:        # the taskbar id is a nice-to-have: the shortcut works without it
                store = query(link, IID_PROPERTY_STORE)
                key = PROPERTYKEY(make_guid(PKEY_APP_USER_MODEL[0]), PKEY_APP_USER_MODEL[1])
                value_text = ctypes.c_wchar_p(app_id)
                value = PROPVARIANT()
                value.vt = 31                                         # VT_LPWSTR
                value.value = ctypes.cast(value_text, ctypes.c_void_p).value
                method(store, PROPERTY_STORE_SET_VALUE, ctypes.HRESULT,
                       ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(
                    ctypes.byref(key), ctypes.byref(value))
                method(store, PROPERTY_STORE_COMMIT, ctypes.HRESULT)()
            except OSError:
                pass

        persist = query(link, IID_PERSIST_FILE)
        method(persist, PERSIST_FILE_SAVE, ctypes.HRESULT, text, wintypes.BOOL)(lnk_path, True)
    finally:
        for obj in (store, persist, link):
            try:
                release(obj)
            except OSError:
                pass
        if initialised:
            ole32.CoUninitialize()

    if not os.path.isfile(lnk_path):
        raise OSError("Windows said the shortcut was saved, but it isn't there")
    tell_explorer(lnk_path)
    return lnk_path


def tell_explorer(lnk_path):
    """Ask Explorer to show a new shortcut straight away (no F5 needed)."""
    try:
        import ctypes
        shell32 = ctypes.windll.shell32
        shell32.SHChangeNotify.argtypes = [ctypes.c_long, ctypes.c_uint, ctypes.c_void_p,
                                           ctypes.c_void_p]
        shell32.SHChangeNotify.restype = None
        path = ctypes.c_wchar_p(lnk_path)
        folder = ctypes.c_wchar_p(os.path.dirname(lnk_path))
        # SHCNE_CREATE / SHCNE_UPDATEDIR with SHCNF_PATHW | SHCNF_FLUSH
        shell32.SHChangeNotify(0x00000002, 0x1005, ctypes.cast(path, ctypes.c_void_p), None)
        shell32.SHChangeNotify(0x00001000, 0x1005, ctypes.cast(folder, ctypes.c_void_p), None)
    except Exception:
        pass


def _emit(text):
    """Write one result line for the parent process (UTF-8, whatever the console says)."""
    data = (text + "\n").encode("utf-8", "replace")
    stream = getattr(sys.stdout, "buffer", None)
    try:
        if stream is not None:
            stream.write(data)
            stream.flush()
        elif sys.stdout is not None:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
    except Exception:
        pass


def native_main():
    """Entry point of the helper process: make one shortcut, print OK/FAILED."""
    try:
        lnk_path = _native_create(os.environ)
    except Exception as exc:          # anything at all: report it, the parent tries another way
        _emit("FAILED %s" % (str(exc).strip() or exc.__class__.__name__))
        return 1
    _emit("OK " + lnk_path)
    return 0


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
    """Create Squish.lnk in ``where`` (DESKTOP or START_MENU). Returns (ok, message).

    Tries Windows' own shortcut API first, then PowerShell, then VBScript, and
    only reports success once the shortcut file is really there.
    """
    folder_key, label = where
    if not is_windows():
        return False, NOT_WINDOWS_MESSAGE
    app_dir = str(app_dir if app_dir is not None else paths.app_dir())
    values = shortcut_values(folder_key, app_dir=app_dir)
    if not values["SQUISH_SC_TARGET"]:
        return False, "Squish couldn't work out which Python program to use for the shortcut."
    env = child_environment(values)
    env["SQUISH_SC_APPDIR"] = app_dir

    attempts = [
        ("Windows shortcut API", lambda: _run(native_command(), env)),
        ("PowerShell", lambda: _run(powershell_command(), env)),
        ("Windows Script Host", lambda: _run_vbs(env)),
    ]
    problems = []
    for name, attempt in attempts:
        ok, detail = attempt()
        if ok and not shortcut_exists(detail):
            ok, detail = False, "it reported success, but no shortcut appeared at %s" % detail
        if ok:
            return True, "%s shortcut created:\n%s" % (label, detail)
        problems.append((name, detail))
    return False, failure_message(label, problems)


def shortcut_exists(path):
    """True if the shortcut a helper reported is really there."""
    try:
        return bool(path) and os.path.isfile(path)
    except (OSError, ValueError):
        return False


def failure_message(label, problems):
    """Plain-English explanation when no method worked. ``problems``: [(way, detail)]."""
    lines = "".join("  %s: %s\n" % (name, detail) for name, detail in problems)
    return (
        "Squish couldn't create the %s shortcut automatically - this computer blocks "
        "the tools it uses.\n%s\n"
        "You can make one yourself: open the Squish folder in File Explorer, right-click "
        "Squish.pyw and choose Send to > Desktop (create shortcut). On Windows 11, click "
        "'Show more options' first. Or just double-click Squish.pyw to start Squish."
        % (label, lines))


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
