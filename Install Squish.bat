@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Install Squish
rem ----------------------------------------------------------------------
rem  Squish - setup. Double-click this file. Run it again to update Squish.
rem
rem  1. finds Python (the "py" launcher first, then "python")
rem  2. installs the optional Outlook and PDF reader packages (extract-msg,
rem     pypdf, and cryptography for secured PDFs)
rem  3. copies Squish to %LOCALAPPDATA%\Programs\Squish, so it doesn't matter
rem     where the download was unzipped (Downloads, Desktop, a network drive)
rem  4. makes the Squish shortcuts on the Desktop and in the Start Menu
rem
rem  Written with goto labels instead of bracketed if-blocks, so folder
rem  names with spaces or brackets, e.g. "Program Files (x86)", can't
rem  break it. pushd also copes with network drives and \\server paths.
rem ----------------------------------------------------------------------

pushd "%~dp0"
if errorlevel 1 goto :nofolder

echo.
echo  ============================================================
echo     Squish - setup
echo  ============================================================
echo.
echo  This will:
echo    1. check that Python is installed
echo    2. install the optional Outlook and PDF readers (extract-msg, pypdf)
echo    3. copy Squish into your user folder (no admin rights needed)
echo    4. put a Squish shortcut on your Desktop and in the Start Menu
echo.
echo  Nothing is uploaded, and your emails are not touched.
echo  If Squish is open, close it first.
echo.

rem Running from inside a zip file? Windows (or 7-Zip, WinRAR) unpacks just
rem this one file to a Temp folder, so check that before looking for the
rem other Squish files - otherwise the zip advice could never be shown.
echo "%~dp0" | find /i "\AppData\Local\Temp" >nul
if not errorlevel 1 goto :inzip

if not exist "Squish.pyw" goto :nosquish
if not exist "squish_app\gui.py" goto :nosquish

rem Where Squish is installed: the usual place for programs installed just
rem for one user (no admin rights needed). Projects and settings live
rem elsewhere (%APPDATA%\Squish), so updating never touches them.
set "DEST=%LOCALAPPDATA%\Programs\Squish"
if "%LOCALAPPDATA%"=="" set "DEST=%USERPROFILE%\AppData\Local\Programs\Squish"

rem ---- 1. Find Python -----------------------------------------------------
rem "py" is the Python launcher that python.org installs. "python" may be the
rem Microsoft Store placeholder, which fails with an error instead of running.
set "PY="
set "PYW="
set "COPYFAILED="
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
goto :pdfreader

:pipfailed
echo  Couldn't install it (no internet access, or blocked by IT). That's fine:
echo  Squish will use its own built-in Outlook reader instead.
echo  (The details are in squish-pip-log.txt in your Temp folder.)
goto :pdfreader

rem ---- 2b. Optional PDF reader ----------------------------------------------
:pdfreader
echo.
echo  Installing the optional PDF reader (pypdf)...
%PY% -m pip install --user --upgrade --disable-pip-version-check --quiet --timeout 20 --retries 1 pypdf >"%TEMP%\squish-pip-pdf-log.txt" 2>&1
if errorlevel 1 goto :pdffailed
echo  PDF support: installed
goto :pdfcrypto

:pdffailed
echo  PDF support: Squish's built-in reader will be used
echo  (Couldn't install pypdf - the details are in squish-pip-pdf-log.txt in your Temp folder.)
goto :copyapp

rem ---- 2c. Optional support for secured PDFs (pypdf + cryptography) -------
rem A separate step, not "pypdf[crypto]": if cryptography can't be installed,
rem pypdf must still be. Without it Squish's built-in reader opens those PDFs.
:pdfcrypto
%PY% -m pip install --user --upgrade --disable-pip-version-check --quiet --timeout 20 --retries 1 cryptography >"%TEMP%\squish-pip-crypto-log.txt" 2>&1
if errorlevel 1 goto :cryptofailed
echo  Secured PDF support: installed
goto :copyapp

:cryptofailed
echo  Secured PDF support: Squish's built-in reader will be used for those
echo  (Couldn't install cryptography - the details are in squish-pip-crypto-log.txt in your Temp folder.)
goto :copyapp

rem ---- 3. Copy Squish to its permanent folder --------------------------------
rem Skipped when this installer is already running from there. "%~dp0." (with
rem the dot) stops robocopy reading the folder's last backslash as an escape.
:copyapp
echo.
if /i "%~dp0"=="%DEST%\" goto :installed
echo  Copying Squish to "%DEST%" ...
if not exist "%DEST%\" mkdir "%DEST%" >nul 2>&1
rem robocopy /MIR also removes files an older Squish had that this one doesn't.
rem If robocopy is missing or fails (exit code 8 or more), xcopy is tried.
where robocopy >nul 2>&1
if errorlevel 1 goto :xcopy
robocopy "%~dp0." "%DEST%" /MIR /XD __pycache__ .git /XF *.pyc /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >"%TEMP%\squish-copy-log.txt" 2>&1
if errorlevel 8 goto :xcopy
goto :checkcopy

:xcopy
xcopy "%~dp0*" "%DEST%\" /E /I /Y /Q >>"%TEMP%\squish-copy-log.txt" 2>&1
if errorlevel 1 goto :copyfailed
goto :checkcopy

:checkcopy
if not exist "%DEST%\Squish.pyw" goto :copyfailed
if not exist "%DEST%\squish_app\gui.py" goto :copyfailed
echo  Copied.

:installed
rem ---- 4. Shortcuts ---------------------------------------------------------
echo.
echo  Creating the shortcuts...
%PY% "%DEST%\Squish.pyw" --create-shortcuts --console >"%TEMP%\squish-shortcut-log.txt" 2>&1
set "SHORTCUTRESULT=%errorlevel%"
type "%TEMP%\squish-shortcut-log.txt"
if not "%SHORTCUTRESULT%"=="0" goto :shortcutproblem
echo.
echo  All done! Squish is on your Desktop and in the Start Menu
echo  (search for Squish in the Start Menu if you can't see the Desktop icon).
echo.
if "%COPYFAILED%"=="1" goto :offerlaunch
echo  Squish is installed in: "%DEST%"
echo  You can delete the folder you downloaded. To update Squish later, download
echo  the new version and run "Install Squish.bat" again - your projects are kept.
goto :offerlaunch

:shortcutproblem
echo.
echo  The shortcuts couldn't be made automatically (see the message above).
echo  Squish itself is installed - you can start it by double-clicking
echo  Squish.pyw in this folder (opening now):
echo    "%DEST%"
echo  To make a Desktop shortcut yourself: right-click Squish.pyw there, choose
echo  "Show more options" (Windows 11), then Send to, then Desktop (create shortcut).
start "" explorer "%DEST%"
goto :offerlaunch

:offerlaunch
echo.
choice /c YN /n /m "  Open Squish now? [Y/N] "
if errorlevel 2 goto :finish
start "" %PYW% "%DEST%\Squish.pyw"
goto :finish

rem ---- Problems -------------------------------------------------------------
:copyfailed
echo.
echo  Squish couldn't be copied to:
echo    "%DEST%"
echo  (If Squish is open, close it and run this installer again. The details
echo  are in squish-copy-log.txt in your Temp folder.)
echo.
echo  Making the shortcuts point at this folder instead - so don't delete or
echo  move this folder:
echo    "%~dp0"
set "DEST=%~dp0."
set "COPYFAILED=1"
goto :installed

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
echo  the squish_app folder. Unzip the whole Squish download first (right-click
echo  the zip, Extract All...), then run "Install Squish.bat" from that folder.
goto :finish

:inzip
echo.
echo  It looks like you opened this from inside a zip file.
echo  First extract the whole Squish folder (right-click the zip, Extract All...),
echo  then run "Install Squish.bat" from the extracted folder.
goto :finish

:nofolder
echo.
echo  Couldn't open the folder this installer is in. Copy the Squish folder
echo  to your computer (e.g. your Downloads folder) and try again from there.
pause
endlocal
exit /b 1

:finish
echo.
popd
pause
endlocal
