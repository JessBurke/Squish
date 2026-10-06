# Squish

**Squash a folder of emails into one file you can drop into Claude.**

Your filed project emails (the `.msg` files Mail Manager saves into the project
folders) are far too big and messy to give to Claude as they are. Squish reads a
whole folder of them - or a folder of folders - and writes one compact text file:
every conversation in date order, who wrote to whom and when, and only the *new*
text of each email. Quoted replies, signatures, disclaimers, logos and duplicate
copies are cut out. A project's worth of email usually shrinks to a small fraction
of its size.

You drag that file into Claude and ask things like *"What's still outstanding,
and who owes it?"* A big project may give a few files: **start a new Claude chat
for each file** (Squish tells you when they are small enough to share one chat).

You can set up as many projects as you like; Squish remembers each one.

---

## One-time setup

1. **Put the Squish folder somewhere permanent**, for example
   `Documents\Squish App`. (Not your Downloads folder, and not inside a zip file -
   if you received a zip, right-click it and choose *Extract All...* first.
   The installer stops if it is run from Downloads or straight from an Outlook
   attachment, because Windows may clean those folders up later.)

   *Tip:* before extracting, right-click the zip, choose *Properties*, tick
   **Unblock** and click *OK*. Windows then won't stop you when you run the
   `.bat` files.
2. **Double-click `Install Squish.bat`.** It:
   - checks that Python is installed,
   - installs an optional helper for reading Outlook files (if your network
     blocks this, that's fine - Squish has its own reader built in),
   - puts a **Squish** shortcut on your Desktop and in the Start Menu,
   - offers to open Squish.

   If Windows shows **"Windows protected your PC"**, click *More info* and then
   *Run anyway*. If it says **"The publisher could not be verified"**, click
   *Run*. Squish is a plain script that runs on your computer and uploads
   nothing.
3. That's it. From now on, open Squish from the Desktop or Start Menu.

**Don't move the Squish folder after installing.** The shortcuts point at it. If
you do move it, just run `Install Squish.bat` again.

### If Python isn't installed

The installer will tell you. Get Python 3 from
[python.org/downloads](https://www.python.org/downloads/) (or from your company
software portal if it has one). In the installer, **tick "Add python.exe to
PATH"** on the first screen, then click *Install Now* - this installs it just for
you and doesn't need admin rights. Then run `Install Squish.bat` again.

If typing `python` opens the Microsoft Store, that's only a placeholder, not
Python itself.

---

## Everyday use

1. **New project** - click **New** (or press Ctrl+N) and give it a name, e.g.
   the job name.
2. **Emails folder** - click **Browse...** and pick the folder where the
   project's emails are filed (or paste the folder's address from File
   Explorer, or a `file://` link to it). Squish shows how many email files it
   found. Leave *Include subfolders* ticked for a folder of folders.
3. Click **Squish!** (or press F5). A progress bar shows how it's going. The
   first run of a big folder on a network drive can take a few minutes; after
   that Squish remembers what it has read, so re-runs are much quicker.
4. **Give the file to Claude**, either way:
   - click **Show in folder** and drag the file into the Claude window, or
   - click **Copy file**, click in Claude's message box and press **Ctrl+V**.

   If there are several files, use **one new Claude chat per file** - each file
   stands alone, and its name shows the dates it covers. When the run finishes,
   the *Done* message (and the tip under the file list) says what to do next,
   including when the files are small enough to go into one chat together.

The digest files are saved in `Documents\Squish\<project name>` unless you
choose another folder (not the emails folder or a folder inside it - Squish
never writes there). Running a project again replaces its old digest files.
A run with *From* / *To* dates or *Focus keywords* is saved as its own file,
whose name ends `(only <dates>)` or `(focus ...)`, so the all-dates digest is
kept.

Settings save automatically. Deleting a project only removes it from Squish's
list - it never deletes emails or digest files.

---

## Tips

- **Only need a period of time?** Fill in the *From* / *To* dates (YYYY-MM-DD),
  or point Squish at a smaller folder, such as one month's subfolder. A dated
  run is saved as its own file, whose name ends `(only <dates>)`, for example
  `(only 2025-01-01 to 2025-03-31)` or `(only from 2025-01-01)`; your all-dates
  digest is kept.
- **Only need one topic?** Add *Focus keywords* (Squeeze tab), e.g.
  `culvert, pump station, RFI 12`. Squish keeps only the conversations that
  mention at least one of them (`RFI 12` also finds `RFI-012`, `RFI_12` and
  `RFI #12`). The file name then ends `(focus ...)`, and your all-dates digest
  is kept.
- **Dates and focus keywords stay set for the project until you clear them.**
  While they are on, the *Emails* tab says so and each Squish! makes only that
  dated or topic file. Clear them and click **Squish!** to bring the full
  digest up to date.
- **Squeeze level** - *Standard* suits most jobs. *Light* keeps more of each
  email. *Max* makes the smallest file.
- **File size** - large projects are split into several files so each fits in a
  Claude conversation. **If Claude says the file is too large**, choose File
  size *Small* (or Squeeze *Max*), run it again, and start a new Claude chat for
  each file (or drag in only the ones you need).
  Sizes are shown in *tokens*: tokens are how Claude measures text, roughly 3.5
  characters each; one Claude chat holds about 150k tokens.
- **Who's who** - people appear as short codes like `SLR.AB` (organisation,
  then initials), with a key at the top of each file. Add a line such as
  `example.com=EX` to choose the code for another organisation's email domain.

---

## Things to ask Claude

Drag in the digest file, then try:

1. *"List every open action or request in these emails: what it is, who owes it,
   who asked, and since when. Oldest first."*
2. *"Give me a timeline of the key decisions and agreements, with dates and who
   made them."*
3. *"What has the client asked for that we haven't clearly answered yet?"*
4. *"Summarise everything about [topic], including any changes of position over
   time."*
5. *"Draft a short status update for the project team based on the last month
   of emails."*
6. *"Which documents and drawings were sent, by whom and when? Flag any that were
   superseded."*

Claude can only see what's in the file, so if something seems missing, check the
dates, focus keywords and folder you used.

---

## Privacy

Everything runs on your own computer. Squish doesn't connect to the internet
(apart from the one-off helper download during setup) and only ever *reads* your
email folders - it never changes, moves or deletes an email. Nothing leaves your
computer except the files you choose to drag into Claude, so treat those like
the emails themselves.

---

## Troubleshooting

**The shortcut didn't appear.** In Squish, use *Tools > Create desktop shortcut*.
If that's blocked on your computer, you can always double-click `Squish.pyw` in
the Squish folder, or right-click it and choose *Send to > Desktop (create
shortcut)* (on Windows 11, click *Show more options* first).

**Double-clicking the Squish shortcut does nothing.** The Squish folder was
probably moved or deleted. Put it back, or run `Install Squish.bat` again from
its new place to remake the shortcuts.

**"Can't find the email folder".** If it's on a network drive (like `H:`), make
sure you're connected to the office network or VPN and that the drive opens in
File Explorer.

**Some emails couldn't be read.** The summary says how many; click **View run
log** for the list. Usually these are damaged or unusual files; the rest of the
digest is still fine, and its header tells Claude how many files couldn't be
read.

**Very long folder paths** (over 260 characters) are handled - Squish uses
Windows' long-path support when reading.

**Squish closed unexpectedly.** It saves a crash report in its logs folder
(*Tools > Open logs folder*). That file helps whoever maintains Squish to fix
it.

**"Squish stopped because ...".** The email folder stopped responding, or too
many emails couldn't be opened, so Squish kept your previous digest files
("Nothing was changed") rather than replace them with an incomplete one - or,
on a first run, wrote nothing. Click **Details...** next to the message for the
full explanation, and **View run log** for the list of folders and files that
were the problem. Check the network or VPN and click **Squish!** again - emails
already read are remembered. If it says the files look damaged or aren't
Outlook emails, the folder holds files Squish can't read at all; the run log
lists them.

**"... emails in a folder Squish can't open are left out".** You don't have
access to one of the email subfolders (the run log names it). The digest is
written without those emails and its header says how many are missing. If you
should have access, sort that out and run it again.

---

## For the curious

- Squish needs Python 3.8 or newer with tkinter (included in the python.org
  installer). The optional `extract-msg` package is listed in `requirements.txt`.
- Settings live in `%APPDATA%\Squish` (*Tools > Open data folder*); run logs,
  crash reports and the read cache live in `%LOCALAPPDATA%\Squish` (*Tools >
  Open logs folder*), so they don't roam with your Windows profile.
- Long project names and long focus keyword lists are shortened in folder and
  file names (the end becomes a short code such as `~3f9a1c`), to keep paths
  within Windows' limits.
- It can also run without its window:
  `python Squish.pyw run "Project name"` or `python Squish.pyw list`.
- How it works, for maintainers: see `DESIGN.md`.
