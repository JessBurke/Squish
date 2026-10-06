@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Install Squish
rem ----------------------------------------------------------------------
rem  Squish - one-time setup. Double-click this file.
rem
rem  1. finds Python (the "py" launcher first, then "python")
rem  2. installs the optional Outlook reader package (extract-msg)
rem  3. makes the Squish shortcuts on the Desktop and in the Start Menu
rem
rem  Written with goto labels instead of bracketed if-blocks, so folder
rem  names with spaces or brackets, e.g. "Program Files (x86)", can't
rem  break it. pushd also copes with network drives and \\server paths.
rem ----------------------------------------------------------------------

pushd "%~dp0"
if errorlevel 1 goto :nofolder

echo.
echo  ============================================================
echo     Squish - one-time setup
echo  ============================================================
echo.
echo  This will:
echo    1. check that Python is installed
echo    2. install the optional Outlook reader (extract-msg)
echo    3. put a Squish shortcut on your Desktop and in the Start Menu
echo.
echo  Nothing is uploaded, and your emails are not touched.
echo.

rem Running from inside a zip file? Windows (or 7-Zip, WinRAR) unpacks just
rem this one file to a Temp folder, so check that before looking for the
rem other Squish files - otherwise the zip advice could never be shown.
echo "%~dp0" | find /i "\AppData\Local\Temp" >nul
if not errorlevel 1 goto :inzip

rem Downloads, or the folder where Outlook opens an attachment (INetCache),
rem get cleaned up by Windows or by hand - and the shortcuts made below
rem would then silently do nothing. Stop here instead. Each test takes the
rem folder name out of HERE (ignoring case): if HERE changed, it was there.
rem (Not find: it can misread a search text that ends in a backslash.)
set "HERE=%~dp0"
if not "%HERE:\Downloads\=%"=="%HERE%" goto :badplace
if not "%HERE:\INetCache\=%"=="%HERE%" goto :badplace

if not exist "Squish.pyw" goto :nosquish
if not exist "squish_app\gui.py" goto :nosquish

rem ---- 1. Find Python -----------------------------------------------------
rem "py" is the Python launcher that python.org installs. "python" may be the
rem Microsoft Store placeholder, which fails with an error instead of running.
set "PY="
set "PYW="
py -3 -c "import sys" >nul 2>&1
if errorlevel 1 goto :trypython
set "PY=py -3"
set "PYW=pyw -3"
goto :checkversion

:trypython
python -c "import sys" >nul 2>&1
if errorlevel 1 goto :nopython
set "PY=python"
set "PYW=pythonw"
goto :checkversion

:checkversion
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)"
if errorlevel 1 goto :oldpython
%PY% -c "import sys; print('  Found Python ' + sys.version.split()[0] + ' at ' + sys.executable)"
%PY% -c "import tkinter" >nul 2>&1
if errorlevel 1 goto :notkinter
echo  Python's window toolkit (tkinter) is there too. Good.

rem ---- 2. Optional Outlook reader -------------------------------------------
echo.
echo  Installing the optional Outlook reader (extract-msg).
echo  This can take a minute - and it's fine if it doesn't work...
%PY% -m pip install --user --upgrade --disable-pip-version-check --quiet --timeout 20 --retries 1 extract-msg >"%TEMP%\squish-pip-log.txt" 2>&1
if errorlevel 1 goto :pipfailed
echo  Outlook reader installed.
goto :shortcuts

:pipfailed
echo  Couldn't install it (no internet access, or blocked by IT). That's fine:
echo  Squish will use its own built-in Outlook reader instead.
echo  (The details are in squish-pip-log.txt in your Temp folder.)
goto :shortcuts

rem ---- 3. Shortcuts ---------------------------------------------------------
:shortcuts
echo.
echo  Creating the shortcuts...
%PY% "%~dp0Squish.pyw" --create-shortcuts --console
if errorlevel 1 goto :shortcutproblem
echo.
echo  All done! Squish is now on your Desktop and in the Start Menu.
echo  Tip: don't move this folder now. If you do, run this installer again.
goto :offerlaunch

:shortcutproblem
echo.
echo  The shortcuts couldn't be made automatically (see the message above).
echo  You can still start Squish by double-clicking Squish.pyw in this folder,
echo  or right-click Squish.pyw and choose Send to, then Desktop (create shortcut).
goto :offerlaunch

:offerlaunch
echo.
choice /c YN /n /m "  Open Squish now? [Y/N] "
if errorlevel 2 goto :finish
start "" %PYW% "%~dp0Squish.pyw"
goto :finish

rem ---- Problems -------------------------------------------------------------
:nopython
echo.
echo  Python isn't installed on this computer (or Windows can't find it).
echo.
echo  To install it:
echo    1. Go to https://www.python.org/downloads/ and download Python 3 for
echo       Windows - or install "Python 3" from your company's software portal.
echo    2. Run the installer and TICK "Add python.exe to PATH" on the first
echo       screen. "Install Now" installs it just for you - no admin needed.
echo    3. Double-click "Install Squish.bat" again.
echo.
echo  Note: if typing "python" opens the Microsoft Store, that is only a
echo  placeholder, not Python itself.
goto :finish

:oldpython
echo.
echo  Squish needs Python 3.8 or newer, but this computer has:
%PY% --version
echo.
echo  Install a newer Python from https://www.python.org/downloads/
echo  (tick "Add python.exe to PATH"), then run this installer again.
goto :finish

:notkinter
echo.
echo  Python is installed, but without tkinter - the part Squish uses to draw
echo  its window.
echo.
echo  Fix: run the Python installer again, choose "Modify", tick
echo  "tcl/tk and IDLE", finish, then run this installer again.
goto :finish

:nosquish
echo.
echo  This installer must stay in the Squish folder, next to Squish.pyw and
echo  the squish_app folder. Put the whole Squish folder somewhere permanent
echo  (for example Documents\Squish App) and run it from there.
goto :finish

:inzip
echo.
echo  It looks like you opened this from inside a zip file.
echo  First extract the whole Squish folder somewhere permanent (right-click
echo  the zip, Extract All..., e.g. into Documents\Squish App), then run
echo  "Install Squish.bat" from the extracted folder.
goto :finish

:badplace
echo.
echo  Squish is in a temporary place (your Downloads folder, or a folder
echo  where Outlook opened an attachment). Windows may clean it up later,
echo  and then the Squish shortcuts would stop working.
echo.
echo  Move the whole Squish folder somewhere permanent, for example
echo  Documents\Squish App, then run "Install Squish.bat" again from there.
echo.
popd
pause
endlocal
exit /b 1

:nofolder
echo.
echo  Couldn't open the folder this installer is in. Copy the Squish folder
echo  to your computer (e.g. Documents\Squish App) and try again from there.
pause
endlocal
exit /b 1

:finish
echo.
popd
pause
endlocal
