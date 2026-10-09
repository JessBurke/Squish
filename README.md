# Squish

**Squash a project's emails and documents into small files you can drop into Claude.**

Your filed project emails (the `.msg` files Mail Manager saves into the project
folders) are far too big and messy to give to Claude as they are. Squish reads a
whole folder of them - or a folder of folders - and writes a compact text digest:
every conversation in date order, who wrote to whom and when, and only the *new*
text of each email. Quoted replies, signatures, disclaimers, logos and duplicate
copies are cut out. A project's worth of email usually shrinks to a small fraction
of its size.

You drag that file into Claude and ask things like *"What's still outstanding,
and who owes it?"* A big project may give a few files: **start a new Claude chat
for each file** (Squish tells you when they are small enough to share one chat).

Squish also condenses the **Word, Excel, PowerPoint and PDF documents** attached
to the emails (and, if you like, a folder of reports) into a second, separate file -
see [Documents](#documents). (Documents came in Squish 1.1; the version shows at the
bottom right of the Squish window and in *Help > About*.)

You can set up as many projects as you like; Squish remembers each one.

---

## One-time setup

1. **Unzip the download.** Right-click the zip and choose *Extract All...* -
   anywhere is fine, your Downloads folder included.

   *Tip:* before extracting, right-click the zip, choose *Properties*, tick
   **Unblock** and click *OK*. Windows then won't stop you when you run the
   `.bat` files.
2. **Double-click `Install Squish.bat`** in the unzipped folder. It:
   - checks that Python is installed,
   - installs optional helpers for reading Outlook files and PDFs (if your
     network blocks this, that's fine - Squish has its own readers built in),
   - copies Squish into your user folder (`AppData\Local\Programs\Squish` - no
     admin rights needed),
   - puts a **Squish** shortcut on your Desktop and in the Start Menu,
   - offers to open Squish.

   If Windows shows **"Windows protected your PC"**, click *More info* and then
   *Run anyway*. If it says **"The publisher could not be verified"**, click
   *Run*. Squish is a plain script that runs on your computer and uploads
   nothing.
3. That's it. From now on, open Squish from the Desktop or Start Menu. You can
   delete the downloaded zip and folder.

**Updating Squish:** download the new version, unzip it and run
`Install Squish.bat` again. Your projects and settings are kept. (A copy of the
installer is also kept with Squish, in `%LOCALAPPDATA%\Programs\Squish` - paste
that into File Explorer's address bar to find it.)

Double-click the installer normally - don't use *Run as administrator* with an IT
account, or the shortcuts end up on that account's Desktop instead of yours.

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
   that Squish remembers what it has read, so re-runs are much quicker. (The
   first run after updating from Squish 1.0 reads every email again, because it
   now reads the attached documents too. After updating from Squish 1.0, run
   `Install Squish.bat` again to add the PDF reader.)
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

## Documents

Emails often say little more than "see attached". So Squish also reads the
**documents** - Word, Excel, PowerPoint and PDF files, plus text, CSV and zip
files - and condenses them into a **separate documents file** next to the email
digest (its name has `documents` in it). The emails file stays small; you add the
documents file only when you need it.

Two switches, on the **Documents** tab of each project:

- **Condense Word, Excel, PowerPoint and PDF attachments** - the files attached to
  the emails (on by default). A file attached to many emails is included once.
- **Documents folder** (optional) - also condense the files in a folder, for
  example the project's `04 Reports` folder. Squish shows how many documents it
  found there.

Long documents keep their headings and the sentences and table rows with figures,
dates and requirements; a later revision of a report shows only what changed
from the earlier one. Drawings get one line each (title and revision). In the
email file, an attachment that is in the documents file is marked with its
number, e.g. `[att: Geotech report.pdf =D12]`, so Claude can match them up -
**but only when both files are in the same Claude chat**. If they are too big to
share one chat, narrow the run with *From* / *To* dates or *Focus keywords* to get
a pair that fits (or, if you chose File size *Small*, try a larger size).

**Not read** (only listed, so Claude knows they exist): old `.doc`,
`.xls` and `.ppt` files (save them as `.docx`, `.xlsx` or `.pptx` if you need
them), scanned PDFs that have no text in them, CAD files, and anything on a
drawing beyond its title block (Squeeze *Light* also keeps a few lines of a
drawing's notes).

**Drag in the emails file first; add the documents file when you need what the
documents say.** Then try:

- *"Using the emails and documents digests, list every requirement in the
  geotech report and whether the emails show it was addressed."*
- *"Compare Rev B and Rev C of the drainage report. What changed, and did anyone
  email about the change?"*
- *"Which documents did the client send us, and which of them have we not
  replied to?"*

The squeeze level, file size, dates and focus keywords apply to the documents
file too (except that the dates don't apply to the documents folder: its files
have no email date).

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

**The shortcut didn't appear.** Run `Install Squish.bat` again and read what it
says under "Creating the shortcuts". Squish tries three ways to make a shortcut
(Windows' own shortcut function, PowerShell, then Windows Script Host). If your
computer blocks all three, the installer says so and opens the folder Squish is
installed in: double-click `Squish.pyw` there to start Squish, or right-click it
and choose *Send to > Desktop (create shortcut)* (on Windows 11, click *Show more
options* first). Inside Squish, *Tools > Create desktop shortcut* tries again.
Also check the Start Menu: search for **Squish**.

**Double-clicking the Squish shortcut does nothing.** Python may have been
removed or upgraded. Run `Install Squish.bat` again to remake the shortcuts.

**"Can't find the email folder".** If it's on a network drive (like `H:`), make
sure you're connected to the office network or VPN and that the drive opens in
File Explorer.

**Some emails couldn't be read.** The summary says how many; click **View run
log** for the list. Usually these are damaged or unusual files; the rest of the
digest is still fine, and its header tells Claude how many files couldn't be
read.

**Some documents couldn't be read.** Usually these are scanned PDFs (pictures of
pages, with no text in them) or password-protected files. They are listed by
name in the documents file; **View run log** gives the reason for each. PDFs
that are only locked against editing or copying are read normally; PDFs that
need a password just to open them can't be read.

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
  installer). The optional `extract-msg` (Outlook files) and `pypdf` (PDFs)
  packages are listed in `requirements.txt`.
- Settings live in `%APPDATA%\Squish` (*Tools > Open data folder*); run logs,
  crash reports and the read cache live in `%LOCALAPPDATA%\Squish` (*Tools >
  Open logs folder*), so they don't roam with your Windows profile.
- Long project names and long focus keyword lists are shortened in folder and
  file names (the end becomes a short code such as `~3f9a1c`), to keep paths
  within Windows' limits.
- It can also run without its window:
  `python Squish.pyw run "Project name"` or `python Squish.pyw list`
  (`--no-docs` leaves the attachments out, `--docs-folder DIR` adds a
  documents folder for that run).
- How it works, for maintainers: see `DESIGN.md`.
