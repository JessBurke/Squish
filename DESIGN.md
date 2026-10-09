# Squish — design contract

Squish is a small desktop app that turns a folder (or folder of folders) of filed
Outlook emails (`.msg`, plus `.eml`) into a dense plain-text digest that can be dragged
into Claude Desktop. Since v1.1 it also condenses the Word, Excel, PowerPoint and PDF
documents attached to those emails (and, optionally, a documents folder) into a separate
documents digest next to it - see Documents (v1.1). It replaces the one-off
`compress_emails.py` / `extract_emails.py` scripts with a multi-project app.

This document is the contract between the modules. Change it deliberately.

## Constraints

- **Python 3.8+**, standard library only at runtime, plus the *optional* `extract-msg`
  and `pypdf` packages. If `extract-msg` is missing or fails on a file, the built-in `.msg`
  reader is used; without `pypdf`, the built-in PDF reader (`pdftext.py`). No `match`, no
  `X | Y` type unions at runtime, no 3.9+ only stdlib APIs (`str.removeprefix`, `zoneinfo`,
  `functools.cache`, etc).
- GUI is **tkinter/ttk** (ships with python.org Windows Python).
- Primary platform is **Windows** (work laptop, Mail Manager filing to network drives
  like `H:\Projects\...`, paths often > 260 chars). Must also run on macOS/Linux.
- Runs without a console (`pythonw`) — never rely on `print`/`input` in the GUI path.
- Never modifies or deletes anything in the source email folders. Read-only.

## Layout

```
squish/
  Squish.pyw              launcher: no args -> GUI; "--create-shortcuts" -> shortcuts; "run ..." -> CLI
                          (thin wrapper around squish_app/__main__.py; "python -m squish_app" is the same)
  Install Squish.bat      Windows installer: optional pip installs, copies Squish to
                          %LOCALAPPDATA%\Programs\Squish, creates Desktop + Start Menu shortcuts
  requirements.txt        extract-msg, pypdf, cryptography (all optional)
  README.md               user guide
  DESIGN.md               this file
  assets/                 squish.ico, squish.png (256), squish-64.png + squish-32.png (window
                          icon and header), squish.svg (+ make_icon.py, dev only, needs Pillow)
  squish_app/
    __init__.py           __version__ ("1.1.1": shown in the status bar, Help > About, --version)
    __main__.py           launcher logic (argument handling, GUI crash report + crash log)
    paths.py              data/cache/log/output folders, output_name(), clean_folder_text(), long_path()
    readers.py            read_email(path) -> EmailRecord ; .msg (extract-msg or built-in) and .eml
    msgfile.py            built-in OLE/CFB .msg reader (fallback, no dependencies)
    cleaning.py           body cleaning: quote splitting, signatures, disclaimers, noise
    digest.py             build_digest(records, project) -> DigestResult (pure, no I/O)
    docdigest.py          build_documents_digest(docs, project) -> parts (pure, no I/O) (v1.1)
    docs.py               extract(name, data|path) -> DocText ; Word/Excel/PowerPoint/PDF/text/zip (v1.1)
    pdftext.py            built-in PDF text reader (no dependencies; used when pypdf is missing) (v1.1)
    engine.py             run_project(project, progress, cancel): scan, cache, read, filter, digest, write
    projects.py           load/save project list
    shortcut.py           Windows desktop / Start Menu shortcut creation
    gui.py                tkinter app
    cli.py                headless runner
  tests/                  unittest; run from squish/: python -m unittest discover -s tests -t .
                          (test_end_to_end.py: synthetic folder -> real readers + digest -> files;
                          test_launcher.py: the message box used when tkinter is missing;
                          doc_builder.py: stdlib-only .docx/.xlsx/.pptx/.zip fixtures;
                          pdf_builder.py: stdlib-only PDF writer for test fixtures)
```

## EmailRecord (readers.py -> everything else)

A plain JSON-serialisable dict (so it can be cached):

```python
{
  "path": str,                 # absolute source path (display form, no \\?\ prefix)
  "date": str,                 # ISO 8601 with UTC offset, e.g. "2026-02-20T10:34:00+10:00"; "" if unknown
                               # (in this computer's local time zone; naive source times are taken as UTC)
  "sender_name": str,          # "Sam Brown"
  "sender_email": str,         # "sam.brown@riverside.example" lowercased; "" if only an X500/EX address is known
  "to":  [[name, email], ...], # email "" when unknown
  "cc":  [[name, email], ...],
  "subject": str,
  "body": str,                 # plain text; from HTML (or RTF) if no plain body. Raw: NOT cleaned
  "attachments": [{"name": str, "size": int_or_None, "inline": bool,
                   # (v1.1, documents read: "sha1", "doc_size"; "link": True for a .msg
                   #  attachment that is only a link to a shared file - see Documents)
                   "email": None or {      # an email attached to this one (not inline), one level deep:
                       "sender_name": str, "sender_email": str, "date": str,  # as the record's own fields
                       "to": [...], "cc": [...], "subject": str,
                       "body": str}}],     # raw, at most readers.EMBEDDED_BODY_MAX (30,000) characters
  "message_id": str,           # Internet Message-ID, "" if none
  "in_reply_to": str,
  "conversation_topic": str,   # PR_CONVERSATION_TOPIC for .msg (subject without prefixes), "" if none
  "item_class": str,           # "IPM.Note", "IPM.Schedule.Meeting.Request", "IPM.Schedule.Meeting.Resp.Pos", "REPORT.IPM.Note.NDR" ...
  "auto_reply": bool,          # Auto-Submitted / X-Auto-Response-Suppress / OOF heuristics at read time
  "meeting": None or {         # meeting requests, cancellations and appointments with a start time;
      "start": str,            #   local ISO 8601 like "date"
      "end": str,              #   "" if unknown
      "location": str},        #   "" if none. None for every other item (responses, notes ...)
  "reader": str,               # "extract_msg" | "builtin_msg" | "eml"
}
```

Attached emails (a `.msg` attached to a `.msg`, a `message/rfc822` part of an `.eml`) are read
by all three readers, one level only (emails attached inside an attached email are not read).
Every other attachment has `"email": None` (consumers use `a.get("email")`).

Every string in a record is valid Unicode: no lone surrogates (raw 8-bit header bytes are
decoded as UTF-8, else Windows-1252; split emoji pairs are joined; anything left becomes
U+FFFD), so a record can always be cached and written out. Consumers read the meeting with
`rec.get("meeting") or {}`.

`readers.read_email(path)` raises on unreadable files; the engine records the failure and
carries on. `readers.backend_status()` returns a short human string, e.g.
`"Outlook .msg: extract-msg 0.56.1"` or `"Outlook .msg: built-in reader"`.
A .msg that extract-msg can't read, or reads badly (non-email items such as contacts), is read
with the built-in reader instead. Any 8-bit .msg (or one with an 8-bit attached email) and a
.msg bigger than 2 MB (`readers.EXTRACT_MSG_MAX_BYTES`) are read with the built-in reader
**first**, and extract-msg is tried only if that fails: extract-msg can decode 8-bit text with
the wrong code page, and it loads every attachment into memory while Squish only needs
attachment names and sizes. The built-in reader reads a .msg over 2 MB in place (64 KB reads),
so attachment data is not downloaded from the network drive. It never decodes 8-bit text as
UTF-16/UTF-32, and reads text labelled ASCII, or labelled UTF-8 but not valid UTF-8, as
Windows-1252. Address lists (.msg transport headers and .eml) are split before encoded names are
decoded, so an encoded `"Brown, Sam"` stays one person. HTML lists keep their numbering
(`start=`, `value=`, `type=` and CSS `list-style-type`, including letters and roman numerals).
Clear-signed S/MIME emails (`IPM.Note.SMIME*`) are unpacked from their `smime.p7m` by the
built-in reader (MIME only, size-capped); `.p7s` / pkcs7 signature files count as inline (never
listed). Set the environment variable `SQUISH_NO_EXTRACT_MSG=1` to always use the built-in
reader. extract-msg is opened with its own RTF de-encapsulation switched off (its RTFDE
converter can take tens of seconds and hundreds of MB on one RTF-only email); Squish converts
the RTF body with msgfile's converter instead. Dates extract-msg reports for Outlook's "None"
(year 4500/4501) or a zero FILETIME (1601) are ignored, the same rule as the built-in reader
(before 1971 or from 4500 on is not a date), so the next date field is used. Transport headers
that start with older Exchange's `Microsoft Mail Internet Headers Version 2.0` line (or a BOM or
blank lines) are parsed without it.

## Project (projects.py, engine.py, gui.py)

```python
{
  "id": str,                   # uuid4 hex
  "name": str,
  "source_folder": str,
  "include_subfolders": True,
  "output_folder": str,        # "" -> paths.default_output_folder(name)
  "date_from": str,            # "" or "YYYY-MM-DD" (inclusive, local date)
  "date_to": str,
  "squeeze": "standard",       # "light" | "standard" | "max"
  "part_size": "medium",       # "small" | "medium" | "large" | "single"
  "focus_keywords": str,       # comma/newline separated; "" = everything. Thread kept if ANY keyword appears anywhere in it
  "org_codes": str,            # lines "domain=CODE", e.g. "slrconsulting.com=SLR"; subdomains match
  "drop_noise": True,          # drop auto-replies, meeting responses, delivery/read receipts, system notifications
  "recover_quoted": True,      # recover quoted/forwarded emails that are not filed themselves
  "last_run": {...} or None,   # RunResult summary saved by the GUI
}
```

`projects.new_project(name)` returns a dict with all defaults (org_codes default
`"slrconsulting.com=SLR"`). `projects.load_projects()` fills missing keys with defaults so
old files keep working; `projects.save_projects(list)` writes atomically
(temp file + replace) to `paths.projects_file()`. The file is
`{"version": 1, "projects": [...]}` (a bare list is also accepted).
`projects.load_projects_report()` returns `(projects, problem)`; `problem` is None or
`{"kind", "path", "backup", "error"}`: **damaged** (not a valid project list: it is set aside
as `projects.json.bad-<n>`, the list is empty and the GUI warns, naming the backup) or
**unreadable** (couldn't be opened even after a few tries, e.g. locked or a network blip: it
is left untouched, and the caller must not save over it — the GUI offers Retry/Cancel and
refuses saves for that session). `load_projects()` is the same without the report. The CLI
prints the problem on stderr.
`projects.duplicate_project(p)` returns a copy with a new id, `" (copy)"` added to the name
and `last_run` None; a name longer than 60 characters is shortened first, so the copy never
shares the original's files. `projects.same_file_name(a, b)` is True when two names give the
same file names (equal after `paths.safe_filename`, ignoring case); the GUI requires every
project name to be unique by that test.

Only one Squish window may edit the project list: `projects.take_window_lock()` takes an OS
lock on `<data dir>/squish-window.lock` and returns a `WindowLock` (with `release()`), or None
when another window holds it. If the lock file can't be made, Squish still opens. A leftover
lock file never blocks (the OS lock dies with the process).

`last_run` (written by the GUI after a successful run that wrote files, and by the CLI after a
saved project's run that wrote files without `--out`) is `projects.last_run_from_result(result)`:
the RunResult without the `failed` list: `finished_at, elapsed_s, output_folder, log_path,
files_found, files_read, from_cache, stats, files` plus `failed_count` (files and folders) and
`failed_folders` (folders that couldn't be opened), and (v1.1) `doc_problem_count` (the
`doc_problems` entries), `doc_problem_folders` (those that are folders,
`engine.is_doc_folder_problem`), `doc_folder_is_output` (the documents folder was skipped
because it is the output folder, `projects.is_docs_folder_output_problem`) and
`doc_digest_failed`. The CLI saves it with
`projects.record_last_run(id, result) -> (saved, note)`, which takes the window lock (so nothing
is saved while a Squish window is open: `note` says so) and never saves over a project list that
couldn't be read. A run that writes no files leaves
`last_run` as it was (the earlier digest files are still there). When the GUI starts a run it
adds `previous_files` (the paths in `last_run.files`) to the copy of the project it hands to
the engine; it is never saved.

### Part sizes (characters per output file; ~3.5 characters per token)

| key | chars | shown as | description |
|---|---|---|---|
| small | 200,000 | Small — ~57k tokens per file | Several smaller files; drag in only the ones you need. |
| medium | 480,000 | Medium — ~137k tokens per file (default) | Each file fits comfortably in one Claude chat. |
| large | 1,000,000 | Large — ~286k tokens per file | Fewer, bigger files - only if Claude accepts very large files. |
| single | no limit | One file | Everything in a single file, however big it gets. |

`digest.PART_SIZES` holds this table; the GUI imports it. It is an ordered dict keyed by the
setting value; each entry is `{"key", "chars" (None = no limit), "label", "description"}`.
Sizes are counted in characters, including the part's own header. Token estimates (`est_tokens`,
the results table, Copy text) are characters ÷ `engine.CHARS_PER_TOKEN` (3.5: digests are dense
with dates, times and codes, so they split into more tokens than prose).

User-facing wording (labels, descriptions, `header_note`, Help, README) avoids unexplained
jargon: it says "characters", not "chars"; it describes what a level does in plain words
("short thank-you emails shrink to one line") rather than naming `(ack)` (the digest's own
"How to read" lines explain `(ack)` to Claude); and it relates sizes to a Claude chat, not to
"context" or model names. README and Help say once what a token is: "Tokens are how Claude
measures text, roughly 3.5 characters each; one Claude chat holds about 150k tokens" (150k =
`gui.ONE_CHAT_TOKENS`).

### Squeeze levels

| | light | standard (default) | max |
|---|---|---|---|
| per-email new-text cap | none | 1,500 chars | 500 chars |
| cap cut point | — | opening + fact sentences, see below | opening + fact sentences, see below |
| pure acknowledgements ("Thanks JP", "Noted") | kept in full | one short `(ack)` line | dropped (counted) unless carrying a document (then only `[att: …]`) or a recovered email (`(ack)` + `↳`) |
| recipients shown | people (≤3) else orgs | people (≤2) else orgs | orgs only |
| attachments | every non-inline file on every email | non-inline; a name already listed in the thread shows as `N as above` unless the email says it is updated/revised/amended/a markup/`Rev X`; 2+ phone-camera photos show as `N photos` plus their numbers (`9 photos IMG_9720-9728`); re-sent ones add them after `N as above:` | document types only, same `as above` rule |
| recovered quoted/forwarded emails | yes, capped 1,500 | yes, capped 550 | only under "thin" emails (< 60 chars of their own text, e.g. "FYI, see below"): the newest quoted email, capped 250 |

Cap cut point: `cleaning.cap_text` keeps the opening (40% of the limit, plus the rest of a
list's intro line), then the later sentences or table rows with the most facts (dollar amounts —
totals first —, RFI and drawing numbers, dates, quantities, questions and requests, and
decisions and hold points: rejected, not to proceed, no further, on hold, shall/must not;
accepted, closed, until and variation count for less), then, in order, whatever else fits (whole
sentences). A kept sentence that refers back ("This brings the total to …") keeps the
sentence before it, so a figure keeps its subject, and a short list item (up to 120 characters)
stays whole. A sentence or table row of 30+ characters that the same sender already showed word
for word earlier in the thread (in their last 10 shown emails, `digest.SEEN_EMAILS`) is dropped
first, unless it is in the opening. A long table or list run with facts is first split
at its list items (or after a figure that runs into a capitalised word where a line break was
joined, but not inside a date such as "14 March"), and a sentence longer than max(40% of the
limit, 120 characters) that holds facts is split at clause breaks (`,` `;` `:` dashes,
"and/which/provided …", cutting at the first break past 30% of it), so its facts can be kept.
Dropped stretches are marked ` … ` and a trimmed ending ` …`; a trimmed text never ends on an
empty list number. Plain prose is simply cut at a sentence.

`digest.SQUEEZE_LEVELS` is the same kind of ordered dict: `key`, `label`, `description`, plus the
settings above as `cap` (None = none), `ack` (`keep`/`short`/`drop`), `max_people` (0 = orgs
only), `attachments` (`all`/`grouped`/`docs`), `recover`, `recover_cap`, `recover_thin_only`
and `header_note` (what the level trims, added to each part's "Quoted history…" line).
"Document types" at max are drawings, reports, spreadsheets, archives and attached emails (not
photos).

## Digest format (digest.py)

Every part file stands alone (the user may drag just one). UTF-8, `\n` line endings.

```
SQUISH EMAIL DIGEST | Riverside Depot | part 1 of 2
Covers 2024-08-22 to 2025-11-30 | 812 emails in 403 threads (this part: 410 emails, 201 threads, 2024-08-22 to 2025-05-14)
Source: H:\Projects\...\01 Emails | squeeze: standard | made 2026-10-06 14:05
Dropped: 30 duplicate copies, 150 meeting responses, 15 auto-replies, 1 receipt
How to read: "## subject (N emails, first to last date)" starts a thread (oldest first; no count for one email). Each line is one email:
  YY-MM-DD HH:MM FROM>TO: new text only (local time; date omitted when same as the line above).
  TO = To recipients only (not Cc); an org code alone (e.g. SLR) = several people there.
  Quoted history, signatures and disclaimers are removed; "…" marks trimmed text (emails over ~1,500 characters are cut to their opening plus the sentences with figures, dates and questions).
  [att: ...] attachments. "(ack)" = short thanks/acknowledgement. "↳" = an earlier email
  recovered from inside a reply, forward or attachment because it was not filed on its own.
  "N as above" in [att: ...] = N files whose names were listed earlier in the thread, sent again.
  "•" = list item or table cell; <link> = a link was here.
People (ORG.Initials):
  SLR = slrconsulting.com: AC=Alex Chen, JP=Jordan Price, KT=Kim Tran
  RC = riverside.example: SB=Sam Brown, PL=Pat Lee
  ACMEPU = acmepumps.com.au: MH=Morgan H.
  CW = civilworks.example

## Culvert Headwall Dimensions (3 emails, 26-02-20 to 26-02-24)
26-02-20 10:34 RC.SB>ACMEPU: Here's my calc on the required headwall length ... [att: calc.pdf]
26-02-23 18:22 ACMEPU.MH>RC.SB: Hi Sam, correct, confirming that the headwalls are 300mm ...
26-02-24 09:00 SLR.AC>RC,CW: (meeting 26-03-02 10:00-11:00 @ Site office)

## Site induction
26-02-25 08:10 SLR.JP>SLR: Induction is booked for all staff on Monday 7am.
```

Rules:

- **Threading**: normalise subject — strip any number of `RE:/FW:/FWD:/AW:/WG:/TR:/SV:`
  prefixes (with optional `[n]`), meeting-reply prefixes (`Accepted:/Declined:/Tentative:/New Time
  Proposed:/Canceled:`), `[EXTERNAL]`/`EXTERNAL:`/`[Pending]` tags, Mail Manager filing
  tags in square brackets or parentheses anywhere in the subject (`[Filed 24 Nov 2025 10:50]`,
  `(Filed 24 Nov 2025 10:50)`) and document-management filing tags such as `[ABC-W.FID1234567]`,
  collapse whitespace (tabs too), compare case-insensitively. Thread title shown = the first
  subject in the thread without `RE:`/`FW:`, cleaned. Threads are ordered by first email; emails
  within a thread by date (unknown dates last, shown as `(no date)`). A subject is split into
  separate conversations where an email comes more than 21 days (`THREAD_GAP_DAYS`) after the
  previous email with that subject and either is not a reply (plain or `FW:`), or is a reply whose
  first quoted email (with a sender and a date) is not in the conversation so far — it answers
  something else, often an email that was never filed (matched as recovered emails are) — unless
  it quotes, at any depth, an email already in the conversation (so a late forward of the
  conversation stays with it). So generic subjects ("Attached Image", a bare project name,
  "Invoice") don't merge exchanges months apart. A reply with no quoted email, or whose quoted
  sender or date can't be read, stays; undated emails never start a new conversation.
- **Dedup**: same Message-ID; otherwise, when either copy has no Message-ID, same (sender, first
  200 chars of cleaned body, names of non-inline attachments, thread subject, first quoted email's
  sender and minute, meeting time and place — so two "See attached." emails with different
  drawings, or two "FYI" forwards of different emails, stay apart), with times within 2 minutes
  (`TIME_TOLERANCE_MIN`) and the same To/Cc addresses (a copy with no addresses matches any): the
  Sent Items copy and a received copy of one email can be seconds apart across a minute boundary.
  Two emails with different Message-IDs (a corrected resend) are never merged. Mail Manager files
  the same email in several folders — count duplicates. Of the
  copies, the most complete one is kept (has an address, a date, more attachments).
- **People**: alias = `ORGCODE.Initials` from the display name (first+last initials, e.g. JB);
  collisions get the next letter of the surname (`SBr`) then a number. A one-word display name
  (`Reception`) gets `Re`, then `Rec`, `Rece` …, then `Re2`. ORGCODE from `org_codes`, else the
  second-level domain uppercased, max 6 chars (`acmepumps.com.au` -> `ACMEPU`; country codes and
  labels such as `com`, `gov`, `govt`, `gob`, `ne`, `or` are skipped: `example.govt.nz` ->
  `EXAMPL`). When two different organisations would share a code, each gets as many more letters
  of its name as it takes to tell them apart (`cityofnorthvale`, `cityofsouthvale` -> `CITYOFN`,
  `CITYOFS`), a digit only as a last resort; same-name organisations that differ by state or
  government label get that added (`roads.nsw.gov.au`, `roads.vic.gov.au` -> `ROADSNSW`,
  `ROADSVIC`); a code set in `org_codes` is never reused for another organisation. Senders with
  no SMTP address are resolved by display name against addresses seen elsewhere in the corpus
  (senders, To/Cc lists and quoted `From:`/`To:` headers; `Smith, Jane` = `Jane Smith` =
  `JaneSmith`; an Outlook alias such as `janesmith` also matches the address `janesmith@...`); a
  name-only sender prefers an address in an `org_codes` org, and a shared-mailbox name
  (Accounts, Admin, Info, Projects, Reception …) is never matched to an outside org; if still
  unknown, org `?`. Name matching ignores accents (`José` = `Jose`); aliases keep the letters as
  written, use letters only and are unique ignoring case (a second `SLR.MA` beside `SLR.Ma`
  becomes `SLR.MAd`). Addresses in the same org with the same display name are one person (one
  alias). Busiest people get the shortest aliases. The legend in each part lists only people
  whose alias appears in that part; an org whose people appear only as an org code (recipient
  lists) gets a bare `CODE = domain` line.
- **Body cleaning** (cleaning.py): cut quoted history (Outlook `From:`/`Sent:` header blocks —
  only when `Sent:`/`Date:` holds a clock time, so a From/To/Date block typed in the sender's
  own text stays; a From name may contain a digit (`Monash 2 Team`), but not a time or date —,
  `-----Original Message-----`, `On … wrote:` (only with an email address or a clock time in it,
  so "On 3 March the contractor wrote:" stays), `____` rules, `Begin forwarded message:`, `>`
  lines (but `>` before a number, `=` or `<` is a value, not a quote mark)); remove signatures
  (sign-off lines, a line starting with the sender's own name after content,
  phone/mobile/address/pronoun/"my work days are" lines, `|`-separated title lines, logos
  `[cid:…]`), disclaimers and `[EXTERNAL]`/CAUTION banners, Teams/Zoom join boilerplate,
  SafeLinks/URLs (replace with `<link>` or the bare domain; a link to a bare homepage is
  dropped), `<tel:…>`, zero-width and non-breaking spaces; normalise smart quotes/dashes to
  ASCII and the text to NFC; drop emoji. Bullet/numbered list items are joined with ` • `; other
  line breaks become a single space. Runs of short unpunctuated lines (3+, or 2+ after a line
  ending in `:`) are list items too — that is how HTML lists and table cells arrive — but
  hard-wrapped plain-text paragraphs are joined as prose, not turned into bullets. After an
  Outlook table's header run, sentence-like paragraphs of up to 200 characters are cells too;
  the run stops at a line that continues a long hard-wrapped line. A middle dot is a bullet only
  at the start of a line (`kN·m` keeps it). A signature is cut only where nothing sentence-like
  follows, except when the evidence is strong (sign-off then the sender's own name, the sender's
  name then a title/phone line, or a sign-off then a phone/email block): then everything up to
  the next greeting (`Hi Sam,` — an embedded draft) or the end is removed. A sender's name
  inside a list or table, a lone `RPEQ`, or a line of numbers/dates/dollars is never treated as
  a signature. Table cells are not signature evidence: a lone label (`A`, `d)`), a name or
  initials cell before a number, text followed by a later sign-off, or a folder path is kept —
  but a colleague's signature block (name then a phone/email line) pasted before the sender's
  own sign-off is still removed. The stripper also keeps the paragraph after a line ending in
  `:`, tables with numbers, `Label: value` lines and a bullet on its own line. Contact details
  the sender passes on deliberately are kept: a line introducing them ("you can reach Sam on …",
  "his details are:") and a bare email address with nothing signature-like before it.
  P.S./NB/Note/Update lines after the sign-off are kept, unless they are really signature text
  (working days, a company or address, contact details, a disclaimer). Microsoft 365
  share/booking boilerplate (`Open <link> Share <link>`, "… invited you to view a file") is
  removed (a lone `Open` cell is kept). Disclaimer phrases are strong or weak: a weak one drops
  a line only when that line is also about the email itself; Teams/Zoom filters remove join
  boilerplate only, not sentences such as "Microsoft Teams is down today". Emoji: ✅ becomes ✓,
  ❌/❎ become ✗, ⚠ becomes `(!)`, coloured circles/squares become `[red]`, `[amber]`, `[yellow]`,
  `[green]`; engineering symbols (⌀, the GD&T symbols, ☐), arrows and callout numbers are kept;
  other emoji are dropped. A body that is only a signature, or only a greeting (`Hi Sam,`),
  counts as empty. A greeting on its own line ends at that line and is shown as `Hi Sam, …` ("Hi
  Sam" then "Agreed." gives `Hi Sam, Agreed.`; `Hi Sam Rejected.` on one line is kept as text).
  Text before the greeting is kept unless it all looks like a signature (a first line starting
  with the sender's own name does not drop it). An email with nothing left shows `(no text)` (or
  `(meeting invite)` for a bare Teams invite without meeting data — see Meetings).
- **Inline attachments**: `image001.png`-style names, `Outlook-*.png` (including Outlook names
  with commas), `~WRL*.tmp`, `~WRD*`, `emailsig*`, bare-UUID image names, logo/banner/signature
  names that are image files, and anything the reader flagged `inline` are never listed.
- **Meetings**: an email whose record has `meeting` data shows it as
  `(meeting YY-MM-DD HH:MM-HH:MM @ place)` when it has no text of its own, or
  `[meeting YY-MM-DD HH:MM-HH:MM @ place] text` when it has (`… to YY-MM-DD HH:MM` when it ends
  on another day, `YY-MM-DD all day` for an all-day event, place left out when blank and cut
  at 80 chars). A cancellation (`IPM.Schedule.Meeting.Canceled`) says `meeting cancelled …`.
  The label is added after the per-email cap so it is never trimmed, and such an email is never
  an `(ack)`. `(meeting invite)` is used only when there is no meeting data.
- **Recovered quoted emails**: when an email's quoted section contains an earlier email (header
  with From + Sent/Date) that is **not** present as its own record in the corpus (match on sender
  name/email + timestamp within 2 minutes unless the two texts plainly differ (they part within
  the opening greeting), or body prefix with the whole cleaned text the same or one starting the
  other; a quoted email never matches the email that contains it),
  render it once, under the email that contained it, as `  ↳ YY-MM-DD HH:MM ALIAS: text`, cleaned
  and capped. Emails filed but left out by the engine's date filter (`also_filed`) count as filed,
  so a dated digest never shows them as `↳`. Emails attached to an email (attachment `email`) that
  are not filed are recovered the same way under the email that carries them, with their exact
  time (no whole-hour offset), and so are the emails quoted inside them (at max, only under thin
  emails: the newest quoted email and the first attached one); attached and quoted ones are listed
  together, oldest first. Each quoted email is
  recovered at most once per digest. Up to 15 levels deep per email (`RECOVER_DEPTH`), listed
  oldest first; quoted emails already filed are skipped before they are cleaned. A whole-hour
  offset (the quoting computer's time zone may differ from the reader's) counts as a match only
  when the two texts open the same way and, when the shorter has under 40 letters, it has at
  least 60% of the longer one's letters (so a short email an hour away can't hide a longer one);
  the closest time wins. A quoted header's time is in the time zone of the person who quoted it,
  so each person's usual whole-hour offset per month is learned from quoted emails that are also
  filed, and applied to the `↳` times of headers they wrote. When there is no evidence, the
  offset learned within that email is used. A shift that would put a `↳` time after the email
  above it is dropped. A recovered email matches an earlier recovered one by time only when
  their texts also open the same way. Undated short quotes are recovered only once (recognised
  by sender and text). Quoted meeting invites keep their `When:` and a physical `Where:` as
  `@ place` (shortened as in a meeting label). Pure acknowledgements are not recovered (except at
  light).
- **Inline replies**: when an email says its answers are in the quoted text ("see my comments
  below in red", "responses inline"; not "your response below"), each quoted email, newest first,
  is compared with its filed original, ignoring links. It looks at up to 3 levels
  (`INLINE_DEPTH`) and stops at the first quoted email that is not filed. Lines and trailing words
  it adds, minus answers already shown under an earlier email (emails are handled oldest first),
  are appended as `[inline replies: …]`, cleaned with the quoted author's name (so their
  signature is not taken for an answer). An answer typed after a question on the same line, or
  on its own line(s) under a point, shows as `re "first words…": answer`; a run that cleans to
  nothing, or that starts with its own list number (an edited point), is not labelled. The
  email's own text and its inline replies are capped separately within the per-email cap, so the
  label and the closing bracket always survive. If the original is not filed, it is simply
  recovered with the answers.
- **Noise** (`drop_noise`): meeting responses (`IPM.Schedule.Meeting.Resp.*`, `Accepted:/Declined:/Tentative:`
  subjects), auto-replies (`Automatic reply:`, out-of-office), NDRs/receipts (`Undeliverable:`,
  `Read:`, `Delivered:`, `REPORT.*`), and system notifications from `noreply@*.microsoft.com`-style
  senders are dropped and counted in the header (`Dropped: 120 auto-replies/meeting responses/...`).
  Meeting *requests* are kept with Teams join boilerplate removed, and so are meeting responses
  that carry a typed comment, shown as `(accepted) can we add Sam?` in the meeting's thread.
  Teams chat notifications are kept, not dropped: emails whose subject is "<Name> sent a message"
  / "sent N messages", or whose sender is "<Name> in Teams" at `teams.mail.microsoft` or
  `*.teams.microsoft.com`. Each is shown as one line from that colleague (a group chat's first
  name is resolved only when it is unique), with the chat text only and the Teams footer removed.
  Other noreply Microsoft notifications are still dropped.
- **Focus keywords**: case-insensitive match on subject, body, meeting label and attachment
  names (not inline images) of any email in the thread. Subjects are matched after cleaning (no
  `RE:`/`FW:`, `[EXTERNAL]` or `[Filed …]` tags). "Body" is the cleaned text plus the cleaned text
  of its quoted emails (all 15 levels) and of emails attached to it, so words that only appear in
  signatures and disclaimers (e.g. the company name) do not match. Keywords and text are
  normalised the same way (curly quotes and dashes made plain, accents folded, runs of spaces
  collapsed). A keyword matches from the start of a word, and one that ends in a number does not
  match a longer number ("RFI 12" does not match "RFI 125"). A keyword that is letters then a
  number also matches the usual ways of writing it: "RFI 12" finds `RFI-012`, `RFI_12`,
  `RFI #12`, `RFI12` and `RFI No. 12`. `cleaning.keyword_list` (also used for the file name) is
  unchanged.
- **Thread headings**: `## <title>` alone for a thread of one email with nothing recovered;
  otherwise `## <title> (N emails[ + R recovered], <first> to <last>)` (the span includes the
  recovered emails' dates; one date when they are equal). A thread continued in the next part is
  `## <title> (continued)`.
- **Header**: lines as in the example; `Covers` and the part's own dates include recovered
  emails' dates; `part n of m` and `(this part: N emails, N threads, <first> to <last>)` (the
  part's own dates; a thread counts in every part it appears in) only when there is more than
  one part; then `Focus keywords: …`; then, when the engine's date filter left emails out,
  `Dates: only 2025-01-20 to 2025-01-31 - 8 email files outside these dates are not included`;
  then, when the engine left out emails in folders it has no access to, `Not included: 40 emails
  in folders Squish could not open (no access)`; then, when the engine couldn't read some email
  files, `Not included: 3 email files Squish could not read (damaged or locked), so emails may
  be missing`; then `Dropped: 30 duplicate copies, 150 meeting responses, 15 auto-replies, 1
  receipt, 30 notifications, 12 thank-you/ack emails, 40 emails in threads without the focus
  keywords` (only the non-zero items, singular for 1). The "How to read" lines explain `(ack)`,
  `↳`, `[inline replies: …]` (with `re "…":`), `N as above`, `•`, `<link>`, `(no text)` and
  `(meeting invite)` only in parts that contain them (size estimates while splitting track this
  per part). The People legend explains `?` (`? = address unknown`, plus `(a bare ? = no name or
  address)` when a sender has neither), also when `?` appears only in recipient lists.
- **Splitting**: never split a thread across parts unless one thread alone exceeds the part
  size; then continue it in the next part under `## <subject> (continued)`.
- File names: `Squish - <Project> - <YYYY-MM-DD> to <YYYY-MM-DD>.txt`, or with ` (part 1 of 3)`
  before `.txt`; each part is named with its own date range, recovered emails included
  (`undated` if it has no dated email). `<Project>` is `paths.output_name(name)`: the safe file
  name, or for a name over 42 characters its first 42 characters plus ` ~` and a 6-character
  code made from the whole name, so deep network folders stay under 260 characters. A dated run
  adds ` (only <from> to <to>)`, ` (only from <from>)` or ` (only up to <to>)`
  (`engine.date_label`) after the dates, and a run with focus keywords adds
  ` (focus <keywords>)` after that, so neither can ever overwrite the full digest or be taken for it
  (`Squish - Job - 2025-01-02 to 2025-01-30 (only 2025-01-01 to 2025-01-31) (focus pump).txt`).
  The keyword text is at most 40 characters (`engine.focus_label`: a longer list is cut and gets
  ` ~` plus a 6-character code made from all the keywords, so two lists that start the same way
  never share files). When a digest path would be longer than 250 characters
  (`engine.PATH_BUDGET`; File Explorer and dragging stop at 260), the keyword text (never the
  dates tag) is cut further at a word, down to `focus ~<code>` (code made from the whole label);
  the project name part is not shortened further, so a very deep output folder can still pass
  the limit (the GUI then advises). After writing the new parts, the engine deletes the older
  files they replace — see Engine. Only names that follow this pattern are ever deleted: the
  rest of the name must be `<date> to <date>` or `undated`, optionally with ` (only …)`,
  ` (focus …)` and ` (part n of m)`, so another project called `<Project> - B` is never touched.

`digest.build_digest(records, project, source_label="", now=None, outside_dates=0, cancel=None,
progress=None, also_filed=None, no_access_emails=0, unreadable_files=0, doc_ids=None) -> dict`:

```python
{
  "parts": [ {"text": str, "first_date": "YYYY-MM-DD", "last_date": "YYYY-MM-DD",
              "emails": int, "threads": int} ],
  "stats": {"emails_in": int, "emails_used": int, "duplicates": int, "noise_dropped": int,
            "acks_dropped": int, "filtered_out": int, "threads": int, "recovered_quoted": int,
            "raw_chars": int, "output_chars": int,
            "acks": int},         # acknowledgement emails seen (shown as (ack), kept or dropped)
  "aliases": {record path: "ORG.Initials"},   # see Email digest cross-references (v1.1)
}
```

`build_digest` does no file I/O and is deterministic for a given `now` (the same output for any
`PYTHONHASHSEED`). `outside_dates` is the number of email files the engine's date filter left out
(for the `Dates:` header line); `also_filed` is those records themselves (never shown, only used
so that quoting them doesn't "recover" them); `no_access_emails` is the number of emails left
out because Squish has no access to their folder, and `unreadable_files` the number of email
files the engine couldn't read (each for its own `Not included:` header line). `cancel` is a
`threading.Event`; when it is set, `digest.DigestCancelled` is raised. `progress(done, total)` is
called as the emails are cleaned and quoted emails recovered (Cancel is checked at the same
points, every 50 emails, and also between threads while the parts are packed). `doc_ids`: see
Documents (v1.1), Email digest cross-references.

## Engine (engine.py)

`run_project(project, progress=None, cancel=None) -> RunResult`

- `progress(stage, done, total, message)` — stage in `"scan" | "read" | "documents" | "digest" |
  "write"` (`"documents"` only when documents are on, see Documents (v1.1)).
  Called from the worker thread; the GUI marshals it to the UI thread. Passed on at most ~20
  times a second per stage (`engine.PROGRESS_INTERVAL`), except that the first and last call of
  a step with a known total (done 0, done == total) always go through, so a new step's message
  is never lost.
- `cancel` — a `threading.Event`; checked while files are read (at least every 0.5 s,
  `engine.CANCEL_CHECK_S`, so Cancel waits only for the files already being read), inside
  `build_digest` and while the parts are written to temp files (not during the swap that puts
  them in place, which stays all or nothing). With documents on it is also checked inside a
  document being read (`docs.extract`'s `stop`: between zip members, PDF pages, sheets, slides
  and Word paragraphs) and inside `build_documents_digest` between documents; a document cut
  short by Cancel is never cached. Cancelled runs write no digest files and no run
  log (files already read are kept in the cache so the next run is quicker) and return
  `{"cancelled": True, ...}`.
- The output folder may not be the email folder or a folder inside it
  (`engine.output_inside_source(source, out_dir, resolve_links=True)` compares them as typed and
  with links / mapped drives resolved): the run stops with a `SquishError` before anything is
  written. The GUI shows the same problem under the output folder box (comparing the folders
  only as typed). Then, still before the scan, `engine.check_output_folder(out_dir)` creates the
  output folder if needed and writes and removes a small test file, so a folder that can't be
  saved to is found in seconds, not after the read stage; its message (and the write stage's
  "Can't create the output folder") ends "Choose another folder with Browse..., or clear the
  'Save digests to' box …".
- Folder texts are tidied with `paths.clean_folder_text` (spaces and the double quotes that
  Explorer's "Copy as path" adds; a pasted `file://` link, also in `<...>`, becomes the plain
  path with `paths.file_link_to_path`), in the engine, CLI and GUI.
- Scan: walk the source folder (recursively if `include_subfolders`) using `paths.long_path`,
  pick `.msg`/`.eml` (case-insensitive), skip `~$` temp files. Folder missing/unreachable, or no
  email files in it -> raise `SquishError` with a plain-English message. Subfolders that can't
  be opened are listed in `failed` (`engine.is_folder_problem(message)` tells them from files):
  "no access to this folder" only for a genuine access error (Windows errors 5 and 65, or a
  plain EACCES elsewhere), else "folder could not be opened" (locks, sharing violations and
  network errors, which Windows also reports as PermissionError). File sizes and modified times
  come from the folder listing, so unchanged files need no extra call to the file server.
- Cache: `paths.cache_dir()/<project id>.json.gz` maps source path -> `{mtime, size, record}`;
  unchanged files are not re-read. `CACHE_VERSION` is 4; bump it when EmailRecord changes. The
  cache is written as ASCII JSON (odd file names round-trip), via a temp file; temp files more
  than an hour old (from a save that never finished) are removed after a save. Entries for files
  that failed this time are kept; entries for files that are gone are dropped only after a
  complete scan.
- Read with a small thread pool (network drives are I/O bound). Failures are collected; a few
  are not fatal. But the run **stops** with `SquishError("Squish stopped because … Nothing was
  changed …")`, keeping the previous digest files and saving the cache, when: the source folder
  is unreachable by the end of the run; a subfolder that held emails last time can't be opened
  (except "no access" folders: trying again won't help); no file could be read; or at least 5 and
  more than 20% of the files couldn't be *opened* (files that were read but aren't proper emails
  don't count, so a few permanently damaged files never block a project). The first line is
  always "Squish stopped because <reason>."; the rest says "Nothing was changed - your previous
  digest files are still there." only when an earlier run read this project (its cache is not
  empty), else "No digest was written.", and when every problem file was opened but isn't a
  readable email it says they look damaged or aren't Outlook emails (see View run log) instead
  of the network/VPN advice (`engine._stopped_message`). The run log is still written and its
  path is on the error (`SquishError.log_path`, '' if none), so the window's View run log shows
  it.
- A "no access" folder that held N emails last time doesn't stop the run, but the loss is said
  everywhere: its `failed` entry adds "- it held N emails last time; they are not in this
  digest", the run log has a note, `stats["no_access_emails"]` is the total and the digest header
  has a `Not included:` line (its cached emails are kept in the cache but not used).
- Date filter on the email's local date; undated emails are kept. The emails it leaves out are
  passed to `build_digest` as `also_filed` (and counted as `outside_dates`). The number of email
  files that couldn't be read (not folders) is passed as `unreadable_files` and kept in
  `stats["unreadable_files"]`. A run that leaves no emails after the date/keyword filters writes
  and deletes nothing (the run log says so).
- Write parts to the output folder (created if needed): all parts go to temp files first, the
  files they replace are moved aside, then the new ones are put in place; any failure puts the
  folder back as it was and raises `SquishError`. Characters that can't be saved are replaced.
  Then the older files the new ones replace are deleted. `<cache>/<project id>.outputs.json`
  records, per output folder, which files each kind of run wrote (kind = `""` for all dates
  without focus keywords, else the dates and keywords). A run replaces only the files of the
  same kind — whatever the project was called then, so renaming leaves no stale files — and an
  all-dates run also removes older `Squish - <Project> - …` digest files that no filtered run
  wrote. If that record is missing, the project's `previous_files` (same folder, same kind: a
  dated or focus run — `(only …)` or `(focus …)` in the name — or an all-dates run) stand in for
  it.
- Write a run log to `paths.logs_dir()/<safe project name> - last run.txt` (counts, timings,
  notes such as a cache that couldn't be saved, then "Folders that could not be opened" and
  "Files that could not be read" listed separately).

RunResult:

```python
{
  "cancelled": bool,
  "files": [{"path": str, "chars": int, "bytes": int,
             "est_tokens": int,           # chars / engine.CHARS_PER_TOKEN (3.5)
             "first_date": str, "last_date": str, "emails": int, "threads": int,
             "kind": "emails" | "documents"}],   # (v1.1; a documents file also has "documents")
  "output_folder": str,
  "files_found": int, "files_read": int, "from_cache": int,
  "failed": [[path, error_message], ...],
  "doc_problems": [[where, reason], ...],  # (v1.1) documents / documents folders not read
  "doc_digest_failed": bool,               # (v1.1) the documents digest could not be made
  "stats": {... digest stats ...,
            "outside_dates": int,         # emails dropped by the date filter
            "no_access_emails": int,      # emails in "no access" folders, left out
            "unreadable_files": int,      # email files that couldn't be read (not folders)
            "documents": int, "doc_drawings": int, "doc_other": int,   # (v1.1, see
            "doc_versions": int, "doc_failed": int,                     #  Documents)
            "doc_found": int},            # (v1.1, only when documents were wanted) the files the
                                          #   documents digest would list (after focus keywords)
  "elapsed_s": float,
  "log_path": str,
  "finished_at": "YYYY-MM-DD HH:MM",
}
```

`SquishError(Exception)` lives in engine.py for user-facing errors; `log_path` is the run log
written before a run stopped ('' if none).

## Paths (paths.py)

`clean_folder_text(text)` / `file_link_to_path(link)`: see Engine (`file:///H:/Jobs/01%20Emails`
-> `H:\Jobs\01 Emails`, `file://server/share` -> `\\server\share`).
`data_dir()` holds `projects.json` and `squish-window.lock` (Windows `%APPDATA%\Squish`, which
may roam). `local_data_dir()` holds `cache/` and `logs/` (Windows `%LOCALAPPDATA%\Squish`,
which never roams; elsewhere the same as `data_dir()`). `SQUISH_DATA_DIR` overrides both (the
tests set it). `safe_filename(name)`: bad characters become spaces, at most 80 characters, no
trailing space or dot, device names get a `_` right after the device name, also before a dot
(`CON` -> `CON_`, `Con.Ltd` -> `Con_.Ltd`, `AUX. Building` -> `AUX_. Building`; CON, PRN, AUX,
NUL, COM0-9 and LPT0-9, plus the superscript ¹²³ forms). `output_name(name)` and
`default_output_folder(name)` (`Documents/Squish/<output_name>`) as above.
`default_name_for_folder(folder)` and `GENERIC_FOLDER_RE`: a generic folder name ("01 Emails",
"Correspondence") gets its parent's name in front ("Riverside Depot - 01 Emails").

## CLI (cli.py)

```
python Squish.pyw run "Project name"                 # run a saved project
python Squish.pyw run --source DIR [--out DIR] [--name NAME] [--squeeze standard]
                      [--part-size medium] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
                      [--keywords "a,b"] [--org "slrconsulting.com=SLR"] [--no-subfolders]
                      [--no-docs] [--docs-folder DIR]
python Squish.pyw list                               # list saved projects
python Squish.pyw --create-shortcuts [--console]     # Desktop + Start Menu shortcuts (Windows);
                                                     # --console prints the result instead of a message box
python Squish.pyw --version
```

Without `--name`, a `--source` run is named by `paths.default_name_for_folder`, and gets a stable
id from the folder path (so repeated runs reuse the cache). A saved project name can't be
combined with `--source`, `--name` or `--no-subfolders` (exit 2): the run would reuse that
project's cache and replace its digest files with a digest of something else. A `--source` run
whose name gives the same file names as a saved project (`projects.same_file_name`) is refused
(exit 2): it would replace that project's digest files and run log; run the saved project by
name, or use `--name`. Other options apply to that run only. A saved project's run that wrote
files (without `--out`) is recorded as its `last_run` with `projects.record_last_run` (see
Project); a note on stderr says when it couldn't be. Exit codes: 0 ok, 1 error (including a run
that stopped, or one that left no emails and so wrote nothing), 2 bad arguments, 130 cancelled
with Ctrl+C.

## GUI (gui.py)

Left: project list (+ New, Duplicate, Delete). Right: the selected project's settings, a large
**Squish!** button (Cancel while running), progress bar and status, and a results table of the
output files (name, type, size, ~tokens, date range, contents) with buttons **Show in folder** (Explorer with the
file selected — drag from there into Claude), **Copy file** (puts the file on the clipboard so it
can be pasted into Claude with Ctrl+V; Windows via PowerShell `Set-Clipboard -LiteralPath`),
**Copy text**, **Open folder**, and **View run log**. Settings auto-save; a failed save stays on
screen and is retried every 5 s, and closing with unsaved changes asks first. Menu: Project ›
New / Duplicate / Delete; Tools › Create desktop shortcut, Create Start Menu shortcut, Open data
folder (the project list), Open logs folder (run logs and crash reports); Help › How to use,
About. Work runs in a background thread; UI never freezes. Windows: DPI aware, AppUserModelID
set so the taskbar shows the Squish icon.

- Only one window: `gui.main()` takes `projects.take_window_lock()`; a second copy brings the
  open window to the front on Windows, or says "Squish is already open".
- The window is sized to the screen's work area (above the taskbar), up to about 1180x900; on
  short screens a compact layout leaves out the header band, the drag tip and the plain grey
  hints (warnings, errors and the email count still show), uses tighter spacing and opens
  maximised on Windows, so the results table keeps its room.
- The status line under Squish! is one line; a longer message (e.g. "Squish stopped …") keeps
  the rest behind a **Details...** button. The stopped run's log is attached to View run log. A
  stopped (or crashed) run's reason and log come back when its project is shown again; changing
  a setting clears the reason (the log stays), and a new run clears both. Project names in
  status messages are cut to 30 characters (`gui.short_name`; the Cancel button keeps its
  18-character rule); a background run's error keeps the full name behind Details....
- The Done message gives the next step, with the same one-chat rule as the tip under the table,
  and names the run's date/keyword filter. It is amber, not green, when emails may be missing
  (files that couldn't be read, folders that couldn't be opened, emails in a "no access"
  folder), the documents folder couldn't be read (then it adds `gui.docs_folder_warning`: "The
  documents folder (or a folder in it) couldn't be opened, so its documents are missing - see
  run log.", or "The documents folder is the output folder, so it was skipped - see run log.")
  or the documents digest couldn't be made (`gui.docs_digest_warning`: "The documents file
  couldn't be made this time (see run log), so only the emails file was written - any
  documents file already in the folder is from an earlier run."; "The documents file couldn't
  be made either (see run log)." when no file was written). When an emails file and a documents file don't fit one chat it adds "(separate
  chats can't link emails to their attachments)". The results of a dated or focus run (`(only
  …)` or `(focus …)` in the file name, or dates left out) add "This file has only part of the
  emails - for the full digest, clear the dates and focus keywords and click Squish!". A run
  whose filters left no emails but wrote a documents file says "… emails read, but none were
  left to write, so this run made only a documents file (any earlier emails files were kept)",
  names its filter "only documents mentioning …" and ends "only part of the documents". While dates or focus keywords are set, the Emails
  tab says so (an amber warning for keywords), because they stay set until cleared.
- Every final run message (done, stopped, cancelled, nothing written, crash) names the running
  project when another project is on screen.
- Closing during a run asks first, then waits up to 120 s (`CLOSE_WAIT_S`, "Stopping…") for the
  run to stop cleanly and still saves a finished run's `last_run`; clicking the close button
  again closes at once.
- A second click on Squish!/Cancel, New or Duplicate, or a second Ctrl+N, within 0.8 s
  (`CLICK_GAP_S`) is ignored, so a double-click neither cancels the run it just started nor
  makes two projects (F5, Ctrl+Return and the menus are not affected).
- Browsing to, pasting (on leaving the box or Enter) or Squishing a folder names a project still
  called "New project" after the folder (never replacing a name the user typed). Both folder
  boxes show the end of a long path.
- The output folder box warns (and Squish! refuses) when the output folder is the emails folder
  or inside it (`engine.output_inside_source`). The window compares the folders only as typed
  (resolving links can hang for a long time on a disconnected network drive); the engine
  re-checks with links resolved in the run's own thread before anything is written.
- The results summary counts files that couldn't be read and folders that couldn't be opened
  separately, and says when emails in a "no access" folder were left out. When none of the last
  run's files can be found and their folder can't be reached, it says "Can't reach <folder>. If
  it's on a network drive, check you're connected to the office network or VPN. Your digest
  files are probably still there." (only a reachable folder with the files gone says they were
  moved or deleted).
- Project names must be unique by `projects.same_file_name` (after `safe_filename`, ignoring
  case). Long names are cut in the middle in the sidebar.
- The tip under the results table changes with several files: one new Claude chat per file, or
  "small enough for one chat" when together they are ≤ 150k tokens, except after a Small run,
  which always says one new Claude chat per file (people choose Small when Claude said the file
  was too big); the Done message and Help say the same. Help's "too big" tip also says what a
  token is (see Part sizes).
- While a run is going, the window tracks its stage (Cancel says what it is waiting for, the
  digest stage shows a real progress bar). Other projects can be viewed; Squish!/F5 there says
  'Already squishing "<name>"', the button reads 'Cancel "<name>"' and progress lines name the
  running project.
- The run is given `previous_files` (see Project). A run that writes no files keeps showing the
  earlier files. Show in folder / Copy file detect a path of 260+ characters on Windows and open
  the folder or give advice instead of failing.

## Shortcuts (shortcut.py)

`create_desktop_shortcut()` / `create_start_menu_shortcut()` -> `(ok, message)`. Makes
`Squish.lnk` (target: the `pythonw.exe` next to `sys.executable`, or the Microsoft Store
alias; arguments: the quoted path of `Squish.pyw`; icon `assets/squish.ico`). Three ways are
tried in turn, and a way only counts as working when the reported `.lnk` file exists:

1. **Windows' shortcut API** (`IShellLinkW` + `IPersistFile`, plus `IPropertyStore` for the
   taskbar app ID `paths.APP_ID`), called with ctypes in a helper process
   (`native_command()`: the console `python.exe` next to `pythonw.exe`, `-I -c`
   `NATIVE_BOOTSTRAP`, which imports Squish from `SQUISH_SC_APPDIR` and runs
   `native_main()`), so a crash there can never take Squish down. The folder comes from
   `SHGetFolderPathW` (`CSIDL_DESKTOPDIRECTORY` / `CSIDL_PROGRAMS`, so a OneDrive Desktop
   works), and `SHChangeNotify` makes Explorer show the new icon at once. It needs no
   PowerShell or Windows Script Host, so Constrained Language Mode and AppLocker script rules
   don't stop it.
2. **Windows PowerShell** + `WScript.Shell`, folder from
   `[Environment]::GetFolderPath('Desktop'|'Programs')`; it also writes the app ID with a small
   C# helper compiled by `Add-Type` (`SQUISH_SC_APPID` / `SQUISH_SC_CS`); if that fails the
   shortcut still works.
3. A temporary **VBScript** run by `cscript` (can't set the app ID).

Values are passed only in `SQUISH_SC_*` environment variables, never pasted into script text.
When all three fail, the message lists each way's reason and the manual fix (Send to >
Desktop). A Microsoft Store Python's `pythonw.exe` under `Program Files\WindowsApps` is
replaced by its app alias in `%LOCALAPPDATA%\Microsoft\WindowsApps`.
The native way was tested under Wine with Windows Python 3.12 (Wine doesn't store the app ID).

**Installer.** `Install Squish.bat` copies the unzipped folder to
`%LOCALAPPDATA%\Programs\Squish` (robocopy `/E` for the top level, which never deletes
anything already there, and `/MIR` only for Squish's own `squish_app` and `assets` folders,
skipping `__pycache__`; xcopy if robocopy is missing or fails), then makes the shortcuts for
that copy, so it doesn't matter where the
download was unzipped (Downloads included) and the download can be deleted. Running it again
updates Squish; projects and settings live in `%APPDATA%\Squish` and caches in
`%LOCALAPPDATA%\Squish`, which it never touches. If the copy fails, the shortcuts point at
the unzipped folder instead and the installer says not to delete it. `Squish.pyw --create-shortcuts`
exits 0 when both shortcuts were made, 2 when only the Start Menu one failed (the installer
then says the Desktop one is there) and 1 when the Desktop one failed (the installer says so
and opens the installed folder in Explorer). Its output goes straight to the console, so
non-ASCII paths show correctly. Run again from the installed copy, it skips the copy and
doesn't say the download can be deleted. Explorer is told about a new shortcut with
`SHCNF_FLUSHNOWAIT`. A helper's reported path is checked; if it isn't there (cscript writes
in the console code page), the expected path from `SHGetFolderPathW` counts when that file was
written during the attempt.
Non-Windows: `(False, "Desktop shortcuts are only created on Windows")`.

## Documents (v1.1): condensing Word, Excel, PowerPoint and PDF files

Squish also condenses **documents**: files attached to the emails, and loose files in a
documents folder. They go into a **separate digest** next to the email digest, so the email
file stays small; the user drags in one or both. Everything above still applies; this section
adds to it.

### Settings (new Project keys, defaults filled by `projects.load_projects`)

```python
{
  "docs_from_attachments": True,   # condense documents attached to the emails
  "docs_folder": "",               # optional folder of loose documents ("" = none); may equal source_folder
  "docs_include_subfolders": True,
}
```

The squeeze level and file size apply to the documents digest too. Focus keywords keep a
document when a keyword is in its name or text, or when it is attached to an email in a kept
thread. The date range filters attachments by their email's date; loose files are not
date-filtered (the header says so).

### Supported types (`docs.py`, standard library only)

| Type | How | Notes |
|---|---|---|
| .docx .docm .dotx | zipfile + XML | headings (`#`/`##`), paragraphs, list items (`•`), tables (`cell | cell` rows), text boxes, headers/footers once, tracked insertions kept and deletions dropped (a deleted list item takes no number), hidden text (`w:vanish`) left out, comments as `[comment: …]`; footnotes appended; Symbol, Wingdings and Wingdings 2 characters (`w:sym`, and text runs set in those fonts - exact font names, so `Segoe UI Symbol` is not remapped) mapped to Unicode (`☑`, `☐`, `✓`, `μ` …); equations written linearly (`P/A`, `wL^2/8`, `√(f'c)`, `V_uc`, `∑_(i=1)^n`) and a superscript number after a digit as `10^6` / `10^-7` (`1st`, `m2` stay as they are); legacy form fields: check boxes `☒` / `☐`, drop-downs as the chosen entry; an empty content control as `[blank]` (not its prompt); SmartArt text as list items; a text watermark as `Watermark: …` at the start of the Header line; a list level redefined by a `w:lvlOverride` numbered in its own format (`(A)`) |
| .xlsx .xlsm | zipfile + XML | each visible sheet as compact rows `cell | cell` (cached values, never formulas; shared strings; inline strings; booleans; dates via the cell's number format -> `YYYY-MM-DD`; numbers without float noise; a number format's literal text and leading zeros are kept - `-35.2 kN`, `RFI-007`, `00123`, `$4513426.78` - but thousands separators, rounding, fractions, exponents, conditions and scaling commas are not applied); a cell's note as `[note: …]` after its text, notes on empty cells in a last `Notes:` row; empty rows/columns and hidden columns skipped (hidden rows are kept); a sparse row as `header: value` cells (see `row` below); text boxes and shapes as `Text box: …` paras after a visible sheet's rows (charts are not read); hidden sheets listed by name only; rows beyond the level's cap noted `(+N more rows)` |
| .pptx | zipfile + XML | `Slide N: title` then text and notes; hidden slides are kept, with `(hidden)` after the title; auto-numbered paragraphs keep their numbers (`1.`, `a)`, `(1)`, `I.`) |
| .pdf | `pypdf` when installed, else the built-in `pdftext.py` (see PDF below) | page text, including form-field values, typed comments and the text drawn by stamps (`REVISE AND RESUBMIT`) and markup shapes (a text field without an appearance is read from its value); review comments (notes, clouds, highlights …) as `[comment: …]` lines at the end of their page - not when the page already has their words (a short comment must match whole words), nor hidden ones, review-status entries, pop-ups or links; at most 200 per page, 2,000 characters each; pages beyond the level's page cap noted; scanned PDFs (no text layer) -> status `no_text`; encrypted -> read when it opens without a password (an owner password only: RC4 40/128-bit, AES-128 and AES-256, by both readers; pypdf needs the optional `cryptography` package for AES, else the built-in reader reads it), else `protected` |
| .txt .csv .md .rtf | direct (RTF via msgfile's RTF-to-text) | .rtf: Symbol and Wingdings characters mapped as in Word (`msgfile.rtf_to_html_or_text`'s `symbol_char` hook; emails don't pass it, so their text is unchanged) |
| .zip | zipfile | member names listed (capped 60); documents inside (one level, total uncompressed ≤ 50 MB, ≤ 40 files) condensed like attachments; they share `docs.ZIP_TIME_MAX` (120 s, checked between them): documents not reached are `error` "not read: the zip took too long to read" and the zip gets `"retry"` |
| .doc .xls .ppt (old binary formats), .dwg, images, anything else | not read | listed by name, size and where it came from in the "Other files" section |

`docs.extract(name, data=None, path=None, stop=None) -> DocText` (one of `data` bytes or
`path`; never raises, never hangs: size cap `DOC_MAX_BYTES` 40 MB, page cap 300, zip-bomb
guards, time is bounded by caps). `stop` is an optional function that returns True when the
reading should stop (the engine passes Cancel): it is asked between zip members, PDF pages
(both readers), sheets, slides and Word paragraphs, and a stopped document comes back as status
`error`, note "reading was cancelled":

```python
{
  "kind": "docx" | "xlsx" | "pptx" | "pdf" | "text" | "zip" | "other",
  "status": "ok" | "no_text" | "protected" | "too_big" | "unsupported" | "error",
  "note": str,                  # plain-English reason when status != ok, e.g. "PDF reader not available"
  "title": str,                 # document title property, "" if none
  "pages": int or None,         # PDF pages / slides / sheets
  "drawing": bool,              # a PDF whose pages are A3 or larger and that does not read like a
                                #   document, or named like a drawing (see below)
  "blocks": [ {"type": "heading"|"para"|"item"|"row"|"sheet"|"page"|"slide"|"member", "text": str,
               "level": int} ],     # structure kept so the renderer can cap smartly
  "chars": int,                 # total text characters before any cap
  "reader": "builtin" | "pypdf" | "pdftext",
  # PDF only: "large_pages": bool, "prose_pages": bool  # the name-independent parts of "drawing"
                              #   (most pages A3+; most pages hold sentences), for docs.drawing_for_name
  # "retry": True             # only when a time limit cut the reading short (a PDF, or a zip holding
                              #   one), or pypdf was busy with a PDF it was too slow to read: used this
                              #   run, cached, and read once more next run; a second time-limited
                              #   reading is kept for good (see the documents cache below)
}
```

The extracted text is stored uncapped up to `DOC_TEXT_MAX` (400,000 characters); caps are
applied when rendering. Reading stops once that much text is kept, so `chars` is then a lower
bound. A document inside a zip keeps at most 50,000 characters.

Blocks in detail (texts are tidied: single spaces, no control characters; only `para` and
`item` texts may hold `\n`):

- `heading`: `level` 1-9 (Word: Title and `heading N` styles or outline levels; PDF: numbered
  `6.2 Title` lines - not dates such as `13 December 2024` or priced rows such as `1 Site
  walkover $2,450.00` -, short ALL-CAPS lines - not register rows starting with a drawing or
  document number, such as `RD-ST-1002 GENERAL NOTES B A1` - and common report headings). The text includes Word's
  list number when the heading is numbered (`"6.2 Allowable bearing pressure"`).
- `item`: a list item; the text starts with its label (`"• "`, `"1. "`, `"a) "`), `level` is
  the list depth (0 = top).
- `row`: a table or sheet row, non-empty cells joined with `" | "` (sheets keep empty cells
  between filled ones so columns line up: `"Total | | 4513426.78"`); `level` is the row's
  index in its table or sheet, so `0` is the header row. Below a sheet's header row (the
  fullest of its first 10 rows), a row with a run of more than `docs.SPARSE_RUN` (8) empty
  cells - a programme's bars, a matrix - is written as its label cells followed by `header:
  value` for each filled cell (`GANTT_TASK_24 | W97: x`; the column letter when the header
  cell is empty; headers cut to 40 characters), so a value's column needs no counting.
- `sheet`: `text` = sheet name, `level` = sheet number; extra keys `"rows"` (all rows in the
  sheet that hold a value - formatted empty rows don't count; at most 5,000 are kept as `row`
  blocks) and `"hidden": True` for a hidden
  sheet (no rows follow). A .csv file is one `sheet` named after the file.
- `page`: a PDF page mark, `text` "", `level` = page number; that page's heading/para/item
  blocks follow. Wrapped lines are joined into paragraphs (a line ending in an amount such as
  `$2,450.00` ends one: it is a priced table row; so does a table row - `docs.table_row`: a line
  ending in a figure, unit, date or revision and sheet size with 3+ numbers, or ending in a
  revision and sheet size - before a line starting with a capital or a digit); a page's first
  and last lines (its header/footer) and contents-list lines stay separate blocks so the digest
  can drop them. A page's review comments (`[comment: …]`) come last, each its own `para`.
- `slide`: `text` = slide title ("" if none), `level` = slide number; then the slide's other
  text (`para`), tables (`row`) and `para` "Notes: …". Date/footer/slide-number placeholders
  are skipped. A hidden slide is kept: `(hidden)` after its title and `"hidden": True`.
- `member`: a zip member, `text` = its path in the zip, extra key `"size"` (bytes) and, for a
  document read from the zip, `"doc"` (a nested DocText) and `"sha1"` (of its bytes, so the
  digest can tell a copy of a document it lists with its own ID). A zip inside the zip is listed, not
  opened. More than 60 other names -> one `para` `"(+N more files)"`.

Word: a table of contents (`TOC N` styles or a Table of Contents content control) is skipped;
footnote/endnote marks become `[1]`/`[e1]` with the notes as `para` blocks `"[1] …"` after the
body, then one `"Header: … | …"` and one `"Footer: …"` para (each distinct line once;
page-number-only lines dropped; a text watermark starts the Header line, `Header: Watermark:
NOT FOR CONSTRUCTION | …`). `pages` is None for Word (the saved page count is often stale).
Excel: dates `YYYY-MM-DD`, `YYYY-MM-DD HH:MM` when the format shows the time, times `HH:MM`;
percent formats as shown (`35%`, rounded half away from zero: `0.125` with `0%` is `13%`);
numbers to 15 significant digits, as Excel shows them, without float noise.

PDF: under 20 characters per page **read** on average, and no page with 200 or more
(`docs.TYPED_PAGE_MIN`) -> `no_text`; a mostly scanned PDF with typed pages keeps its text,
status `ok`, with the note "pages 2-81 have no text (scanned?)" ("76 of 80 pages have no text
(scanned?)" when the empty pages are scattered); a PDF cut short by a time or size limit before
any text was found is `error` with the reader's note (never "scanned"). pypdf runs in a helper
thread, one read at a time, and is abandoned after 60 s (`docs.PDF_TIME_MAX`), or at once on
Cancel (waiting for it looks at Cancel every `docs.PYPDF_WAIT_S`, 0.2 s; the helper thread
finishes its current page in the background): the pages read so far are used, with a note.
While an abandoned read is still running pypdf is skipped, and a PDF read without it for that
reason gets `"retry"` (so a run started straight after a Cancel doesn't cache a lesser
reading). The built-in reader is tried as well when pypdf can't decrypt the file, stops early
or finds next to no text (the reading with more text wins), and reads drawings (A3+ pages,
spotted from the page sizes) first, as pypdf can take minutes and gigabytes on a CAD sheet: a
drawing's built-in reading is kept whenever it found text (whatever its note, e.g. a damaged
PDF or one cut at the size budget), and pypdf is used only when the built-in reader found
none. In any PDF, pypdf leaves pages with more than 1 MB of stored content
(`docs.PDF_HEAVY_PAGE_BYTES`: CAD figures in a report) to the built-in reader. After a good
pypdf read the built-in reader re-checks it for up to 10 s (`docs.PDF_RECHECK_TIME`): pages
with form fields, typed comments, stamps or markup comments, and heavy pages, take its text
when it has more; a line whose characters it has in another order (a symbol drawn after the
rest of its line: "85 m … μ"; the two readings are lined up with difflib, so their line counts
need not match; look-alike characters count as the same, by NFKC - pypdf 5 gives the micro sign
`µ` where the built-in reader has the Greek `μ`) is taken from it; and when pypdf met composite fonts, or Symbol/Wingdings fonts,
without a text map (which it turns into punctuation) the built-in reading is used if it is
complete and has at least half the characters, else the note "some text may be garbled (PDF
fonts without a text map)" is added. Private-use symbol codes (Word's Symbol and Wingdings
fonts) are translated with pdftext's tables. The built-in reader reads page and form content up
to the 200 MB document budget (a stream cut there marks the page incomplete), keeps real hyphens
at line ends (only soft hyphens go), gives right-to-left scripts in visual order (pypdf gives
reading order) and checks its time budget while expanding font character maps. A read cut short
by a time limit sets `"retry": True`.

A status of `ok` may still carry a note (`"partly read: …"`, `"stopped after 12 pages (slow to
read)"`). When the text limit is reached the note is `"partly read: text limit reached"` (`"… at
sheet X"` for a workbook); a sheet cut at the 5,000-row cap gives `"partly read: first 5000 of
8687 rows of sheet X"` (a .csv: `"partly read: first 5000 of 8687 rows"`) and a PDF longer than
the page cap `"partly read: first 300 of 412 pages"` (the first such cut gives the note); sheet and page marks are still added after it (they are labels, not
text), so a workbook still lists every sheet. Tidying removes control characters, soft hyphens
and zero-width spaces but keeps zero-width joiners (emoji and Indic scripts need them). An empty file is
`no_text` ("empty file"). The content decides the reader when it disagrees with the extension
(a PDF named .docx is read as a PDF; an old binary .doc named .docx is `unsupported`; a
password-protected Office file is `protected`).

Safety limits (per file): XML parts with a DTD or entity declarations, or declaring an unusual
encoding, are refused; XML nesting ≤ 200; ≤ 1,500,000 XML elements and ≤ 150,000 Word
paragraphs/tables; ≤ 64 MB parsed per XML part and ≤ 512 MB unzipped in all; a member that
claims or reaches a compression ratio above 1100:1 is refused (zip bomb); ≤ 20,000 zip entries.
A damaged or hostile auxiliary part (styles, comments, a slide) is skipped; a bad main part
makes the status `error`. `docs.is_supported(name)`, `docs.looks_like_drawing_name(name)`,
`docs.is_drawing(name, pages, page_sizes, page_texts=None)`, `docs.reads_like_document(name,
page_texts)`, `docs.drawing_for_name(doc, name)` (`is_drawing` for a PDF's DocText shown under
`name`, from its `large_pages` / `prose_pages`; a DocText without them keeps its `drawing`) and
`docs.table_row(line)` are public helpers. Set `SQUISH_NO_PYPDF=1` to always
use the built-in PDF reader.

`docs.backend_status()` -> e.g. `"PDF: pypdf 5.1.0"` or `"PDF: built-in reader (install pypdf for best results)"`.

### Reading attachments

When `docs_from_attachments` is on, the readers keep the **bytes** of attachments whose
names have a supported extension (`docs.SUPPORTED_EXT`) and are ≤ `DOC_MAX_BYTES`, and the
engine (not the reader) hands them to `docs.extract`. Each such attachment in the EmailRecord
gets `"sha1": str` (of the bytes) and `"doc_size": int`; the bytes themselves are never cached
or kept after extraction. The built-in .msg reader reads attachment data streams
(`__substg1.0_37010102`) only for those attachments. Records read without documents have no
`sha1`; when documents are switched on later, those emails are re-read (the cache entry
records `"docs": True/False`). `CACHE_VERSION` is bumped (4).

In detail: `readers.read_email(path, want_docs=False)`; with `want_docs` each such attachment
also gets a transient `"_data": bytes` that the engine removes before caching
(`readers.wants_doc_data(name, size)` is the test; inline attachments, attached emails and an
S/MIME container never get bytes; attachments of an attached email are not read).
`msgfile.read_msg(path, want_data=None)` / `read_msg_bytes(data, want_data=None)`:
`want_data(name, data stream size)` is asked for each attachment of the filed email and a True
answer puts the bytes in the attachment's `"data"` (a big .msg is still read in place: only
those streams are read). extract-msg uses `attachment.data`; .eml uses the decoded payload.
The engine condenses each email's documents in the read thread that read it, at most
`engine.EXTRACT_AT_ONCE` (2) at a time, and drops the bytes, so attachment bytes are never all
held at once. A cached email read with documents whose documents are no longer in the
documents cache is read again. A `.msg` attachment that is only a link to a shared file
(OneDrive / SharePoint "cloud" attachments: attach methods 2, 3, 4 and 7, by reference or by web
reference) gets `"link": True` from both .msg readers; the engine lists it under Other files as
"a link to a shared file, not attached (not read)" (status `unsupported`), so it is not counted
as a document that could not be read. A method-1 attachment with no data is still an error.

Extracted documents are cached by content in `paths.cache_dir()/<project id>.docs.json.gz`
(`{"version": 1, "docs": {sha1: {"doc": DocText, "used": "YYYY-MM-DD", optional "retried":
true, optional "pypdf": true}}, "files": {path: {"size", "mtime", "sha1"}}}`, written atomically
like the read cache; a damaged or other-version file is ignored, and so is an entry of the wrong
shape (`engine._doc_shape_ok`: a dict with a status; title, note and kind text or None; pages
and chars whole numbers or None; blocks that are dicts whose text, type and sha1 are text or
None and whose level, rows and size are whole numbers or None; zip members' DocTexts of the
same shape), which is read again; entries unused for 30 days are dropped on save, "used"
meaning referred to by any of the project's emails or loose files, inside the dates or not).
A DocText with `"retry"` is used by the run and saved, and read once more on the next run (the
email that carries it is read again then); if that reading is cut short too, the fuller of the
two readings is kept for good (`"retried": true`); an entry due to be read again that a run
didn't reach (e.g. it was cancelled) stays due. A PDF read with the built-in reader while pypdf
was missing is read again (once) when pypdf is available; one the built-in reader read while
pypdf was there is marked `"pypdf": true` and kept. A DocText is cached by content, but whether
a PDF is a drawing also depends on its name, so the engine decides `drawing` again from the
name the digest shows (`docs.drawing_for_name`; the oldest email's attachment name, else the
first loose path), on a copy: identical bytes under several names are classed the same way
whatever name they were first read under. Loose files are cached by path + size + mtime -> sha1
in the same file. An identical file attached
to many emails, or both attached and in the documents folder, is extracted and shown
**once**. Files whose bytes are not read (too big, unsupported types) count as identical when
name and size match.

### Documents digest (`docdigest.py`, pure, no I/O)

`docdigest.build_documents_digest(docs, project, source_label="", now=None, cancel=None,
progress=None, not_read_folders=None, docs_folders=None) -> dict` with the same shape as
`build_digest` (`parts`, `stats`). `cancel` is a `threading.Event` (checked between documents;
`digest.DigestCancelled` is raised); `progress(done, total)` is called from 0 to total as the
documents are prepared and condensed (about twice per document); `not_read_folders` lists
documents folders (or folders in them) that could not be opened, named in the header;
`docs_folders` are the documents folders, for naming loose files (default: taken from
`source_label`, which cannot tell a ` + ` in a folder name from the separator). `docs` is a list of
`{"id": "D12", "name", "sha1", "size", "doc": DocText, "sources": [...]}` where a source is either
`{"kind": "email", "date", "sender_alias", "sender_name", "sender_email", "subject", "thread"}` or
`{"kind": "file", "path", "mtime"}`. IDs are assigned by first appearance in time (attachments by
email date, then loose files by path) and are stable for a given input. Documents that could not
be read have `"id": ""`. `source_label` is `"<emails folder> (attachments) + <documents folder>"`
(the engine also passes the documents folder as `docs_folders`).

```
SQUISH DOCUMENTS DIGEST | Riverside Depot | part 1 of 2
Covers documents from 2024-08-22 to 2025-11-30 | 64 documents (12 attached to several emails, 3 later versions shown as changes), 9 drawings, 31 other files (this part: 30 documents, 2024-08-22 to 2025-03-30)
Source: H:\Jobs\Riverside\01 Emails (attachments) + H:\Jobs\Riverside\04 Reports | squeeze: standard | made 2026-10-06 14:05
How to read: "## D12 name (type)" starts a document. "From:" = where it came from: YY-MM-DD email SENDER
  "subject" (+N more emails that also attached it), or the documents folder. The email digest marks the
  same file [att: name =D12]. Long documents are condensed: headings ("# " lines), the opening and the
  sentences/rows with figures, dates, references and requirements are kept in order; "…" = text left
  out; "|" separates table cells; "•" = list item.
  (+ one line each, only in parts that use them: "(+N more rows)", "Changes from D7:", "## D30 > name",
  "## Drawings", "## Other files")
People (ORG.Initials, as in the email digest):
  RC = riverside.example: SB=Sam Brown

## D12 Geotechnical Investigation Report Rev B.pdf (PDF, 38 pages) "Geotechnical Investigation"
From: 25-03-04 email RC.SB "Geotech report" (+2 more emails); also documents folder\Superseded (modified 25-03-10)
Riverside Depot Upgrade - Prepared for Riverside Council - Project 623.0001 - Rev B - 14 March 2025
# 1 Executive summary
The site is underlain by up to 1.2 m of fill over stiff clay. … Pad footings may be designed for an allowable bearing pressure of 150 kPa.
# 2 Introduction …
# 6 Foundations
Groundwater was measured at 2.4 m below ground level in BH3. …
(pages 61-80 not shown)

## D15 Geotechnical Investigation Report Rev C.pdf (PDF, 39 pages)
From: 25-04-10 email RC.SB "Geotech report Rev C"
Changes from D12:
# 6 Foundations
+ Pad footings may be designed for an allowable bearing pressure of 120 kPa.
- Pad footings may be designed for an allowable bearing pressure of 150 kPa.
+ Groundwater inflows are expected in excavations deeper than 2 m.

## D16 BoQ.xlsx (Excel, 2 sheets)
From: 25-04-11 email RC.SB "Tender pricing"
# Sheet BoQ (402 rows)
Item | Description | Unit | Qty | Rate | Amount
1.1 | Excavation zone 1 | m3 | 448 | 70.14 | 31422.72
…
| Total (excl. GST) | | | | 45885466.27
(+280 more rows)

## D30 Transmittal 14.zip (zip, 7 files)
From: 25-05-02 email SLR.KP "Transmittal 14"
Files: SI-14/Site instruction.docx (below), Locked.pdf (encrypted inside the zip), photos/ 5 photos IMG_0000,0001,0002,0003,0004
## D30 > SI-14/Site instruction.docx (Word)
# Site instruction 14
Excavations deeper than 1.5 m require shoring. • Contractor to submit shoring design.

## Drawings
D20 623.0001-ST-1200 Layout [H].pdf (1 sheet, 25-02-20 email SLR.KP) - title: DEPOT SLAB SETOUT PLAN, for construction

## Other files
Site plan.dwg (2.1 MB; 25-02-20 email RC.SB; CAD drawing, not read)
Old report.doc (340 KB; documents folder; old Word format (.doc), not read)
```

Rules:
- **Caps per document** by squeeze level (`docdigest.DOC_CAPS`: text characters; spreadsheets also
  rows per sheet; PDFs also pages): light 30,000 / 400 rows / 200 pages; standard 8,000 / 120 rows /
  60 pages; max 2,500 / 30 rows / 15 pages (pages after the cap: `(pages 61-80 not shown)`).
  The text is cut into sentences and rows (`cleaning.fact_pieces`, the way `cleaning.cap_text`
  sees text: a long sentence with facts is split at its clauses) and kept in this order while it
  fits: sheet names, header rows and total rows of sheets; headings, top level first, up to 20% of
  the cap (a heading that only repeats the one before it with `(cont.)` / `(continued)` is
  dropped; `docdigest.FOLD_HEADS` (3) or more headings that differ only in their numbers -
  `Borehole BH1` … `Borehole BH12` - show once in the outline, `# Borehole BH1 (+11 more like
  it)`, and another of them is shown only together with text kept under it; not in `Changes
  from`); the opening, in
  order, up to 25%; then the pieces with the most facts
  (`docdigest.doc_fact_score` = `cleaning.fact_score` plus dates with a year, standards
  (`AS 3600`), structural designations (`360UB56.7`, `200PFC`, `150x150x9.0 SHS`, `N16-200`,
  `SL92`, `M24`, `8.8/S`, `300PLUS`), sizes without units (`2100 x 2100 x 700`), ratios
  (`span/250`, `1V:1H`), clause references, shall/must/minimum, risks/recommendations/conclusions,
  design values, and figures with units cleaning does not know (`400 mg/kg`, `18 kN/m3`, `2,400
  ohm.cm`, `120 µS/cm`, `85 µm` …) and pH values (`pH 5.4`); a piece with a figure, date or reference comes first; text under an executive
  summary / summary / conclusions / key findings heading counts double extra (`SECTION_BONUS` ×
  2), under other summary / conclusion / recommendation headings (and their sub-headings) extra,
  and text in appendices (and a References list) less; a table row in a report counts at most as
  much as a good sentence (under a summary heading it gets only the usual extra), but a row under
  a header row, outside an appendix, scores at least `HARD_BONUS` + 0.5 per figure (up to 3) when
  it has 2+ figures, or `HARD_BONUS` + 1 when it starts with an item code (`HP7`, `BH14`); a row is
  kept only when its header row fits too; a clause cut from the middle of a sentence
  (`cleaning.fact_pieces(..., sentences=True)` says which sentence it came from) is kept only
  together with the start of its sentence; text that repeats one pattern with only
  its figures changed - more than `docdigest.REPEAT_SHAPES` (6) pieces with the same words, such
  as pages of member checks or inspection records - counts for less, × 6 / how many, and so do
  the odd ends of such a listing cut into pieces (`m N*=249 kN … OK`, words all from a lowered
  piece of the same paragraph), so it does not crowd out the sentences around it); then whatever
  follows the opening, in order. A sentence
  repeated word for word is kept once.
  Output: `# ` heading lines (slides `# Slide 3: title`, sheets `# Sheet name (N rows)`), a line
  per paragraph with list items joined by ` • `, table rows on their own lines, `…` where text
  was left out (at the end of the line before the gap, or on its own line between rows). A
  spreadsheet keeps its header row(s), its total rows and the most informative rows, in order,
  then `(+N more rows)`: rows whose cells are unusual for their column count most (a written-out
  answer among `As per drawing`s), open items most of all (a cell `Open` / `Pending` /
  `Outstanding` / `Overdue` / `In progress` / `On hold` / `Not started` that fewer than half the
  column's filled cells share adds `docdigest.OPEN_RARE`; also in `Changes from`, where a row
  whose open status was closed since the earlier version counts the same), then rows with more
  figures (a sparse row's `header: value` cell counts by its value); cells of three or more
  words are compared with their numbers ignored (`Refer to response to RFI-010.` and `… RFI-053.`
  are the same template answer); when a workbook has several sheets each first gets an equal
  share. Word's `Header: …` / `Footer: …` lines are kept, without page numbers and labels such as
  "Commercial in confidence" (and a `Watermark:` label left empty by that), unless all their
  words are in the file name, the title or the first three paragraphs. Across documents, a plain
  sentence of 40+ characters (no figure, date, amount or reference) already shown in an earlier
  document is left out (text paragraphs only: an action carried into the next minutes is kept).
  A DocText note is shown as `Note: …` under the `From:` line.
- **Boilerplate** removed before capping: PDF page headers/footers - one of the first or last
  three lines of a page that comes back on 30%+ of the pages word for word or with only its page
  number changed (`Page n of m`, `Sheet n of m`, or a lone number that goes up with the page); a
  page's very first or last line, or a line holding `Page/Sheet n of m`, also when other figures
  change, if one figure stays the same on every page (a job number or date in a running header:
  `Job 24117 | Calc C-03 | 02/05/25`) and it has no quantity with a unit. Other lines whose
  figures change from page to page (lot labels, 7-day results, certificate, borehole and RL
  lines) are page content. A page's review comments (`[comment: …]`) are not counted as its
  last lines and are never joined to other lines. Also page numbers ("Page 3 of 12"), tables of
  contents (runs of 3+ dot-leader lines whose page numbers - digits up to the page count + 20, or
  lower-case roman numerals - start by page 10, `docdigest.TOC_FIRST_PAGE`, and never fall; a
  Contents / List of figures / tables / appendices / plates / photographs heading and the lines
  after it while their page numbers never fall; runs of 3+ numbered lines - a section number and
  a title with no other figures - ending in rising page numbers near the start, so table rows
  such as `400-500 5 6 6` or `MARK C1-02 460UB74.6 … QTY 1` stay; a lone dot-leader line,
  `Minimum cover (mm) .......... 40`, is a value, and a List of Drawings is kept),
  cover-page, copyright,
  limitation-of-liability and contact wording (`©`, `(c) Example Consulting Pty Ltd`, "all
  rights reserved", "sole use", "shall only be used for the purposes", "no
  liability", "third party", "professional judgement", "results relate only to", "confers no
  rights", ABN / www / phone lines (a phone label needs 8+ digits; a `Level 2,` address needs a
  street word) …; when such a paragraph stops mid-sentence, a short
  lower-case line straight after it is its end and goes too; a contract, fee proposal or
  specification clause - a named party (Contractor, Client, Principal …) with shall / must /
  agrees to / is liable / liability … is limited, not about "this report/document" - is kept
  even with liability or approval wording, unless it is under a Limitations / Disclaimer
  heading; "without the (prior written) consent / approval of" counts as disclaimer wording only
  together with wording about using the document - this report / document / drawing,
  reproduce, copy, rely, disclose, third party … - so site instructions and hold points, `Props
  shall not be removed without the approval of the engineer`, are kept), every sentence without a figure
  under a `Limitations` /
  `Disclaimer` / `Copyright` / `Important information` heading, repeated identical
  paragraphs (30+ characters), empty table cells (Word/PDF tables; sheets keep empty cells between
  filled ones, except sparse rows, see `row` blocks). PDF lines split by a ragged wrap are joined
  again (a long line that does not end a sentence or with an amount such as `$2,450.00` runs on -
  but a complete row - a table row, `docs.table_row`, a `label .... value` line, or one of two
  lines that each start with a drawing number - does not run on into a line starting with a
  capital or a digit, and is never gathered as a cell); across a page break a line is
  joined only when it carries on the sentence (it starts in lower case, the page stopped on a
  word a sentence cannot end on, or a date was split: `on 12` / `March`), and not when the
  sentence ended in a footer line that was dropped; a paragraph joined across pages counts for
  the page cap by the last page it reaches. Runs of short table-cell lines are gathered into one
  paragraph, and a number split at a line end (`623.0001-ST- 1200`) is put back together.
- **Versions**: documents with the same normalised name (`docdigest.family_key`: case, spaces,
  extension, revision markers such as `Rev B`, `_v2`, `[C]`, `(1)`, dates - also a year-month,
  `2025-03` - and words like draft / final / issued ignored) form a family, put oldest first: by
  the revision in their names when every one has one (`P1`, `P2`, then letters `A`, `B` …, then
  numbers `0`, `1` …, `v2`), else by date (a date or year-month in the name, else the day first
  sent or saved), then by ID - so an old revision
  kept in a `Superseded` folder (numbered after the new one that came by email) is still the
  older one, and the newer one shows `Changes from` it. Versions are compared sentence by sentence
  (headings and rows as they are, punctuation, bullets and line wrapping ignored), over the whole
  text, with difflib. A later version is compared with the latest earlier one in the same format
  (Word with Word, PDF with PDF, else the latest), and when ≥ 60% similar shows only `Changes
  from Dn:`: the heading of each section with a change kept under it (`# …`; a renumbered
  heading - `6.11 X` now `6.13 X` - is not a change, and only added or removed headings take the
  heading share), `+ …` new or changed text and `- …` removed text (cut to 120 characters, at most 40 / 20 / 8 removed passages for light / standard
  / max), capped like a document; in a passage that is mostly the same only the changed
  sentence is shown (`+ Pad footings may be designed for an allowable bearing pressure of 120
  kPa.`; in a sentence over 300 characters the changed words with six words either side), and a
  changed row is shown whole as `+` new row, followed - when the same first cell (item number) is
  once in each version - by `-` that first cell and only the cells that changed (`- RFI-077 | … |
  Open | …`; no `-` line when the row only gained cells); the "How to read" line says `"-" =
  removed text, or for a changed row its old cells ("…" = cells that did not change)`. Text that moved shows as `- X` where it
  was and `+ X` where it is (a removed passage is not a repeat of the same text added elsewhere,
  unless it repeats within either version). A changed stretch of more than
  `docdigest.WORD_DIFF_MAX` (2,000,000) old words × new words is not compared word by word (it
  is shown whole, and how alike it is is judged by the words both share), so a register or
  programme whose every row changed is compared quickly. Text that is only cut up differently
  counts as the same: `Changes from D12: none (the text is the same).` A document read only in
  part (a text, row, page or time limit: its note, `"retry"`, or fewer pages or rows kept than it
  has) is never `Same text as` another, and its changes are titled `Changes from D12 in the part
  read (the rest was not read, so not compared):`; a .csv's sheet name (its file name) is not
  compared. A file with the same name
  as an earlier one in another format (`Report Rev B.docx` and `Report Rev B.pdf`) and ≥ 75%
  similar is one document: `The same document as D12 in another format (90% of the text reads the
  same; differences in layout and tables are not shown).` (`Same text as D12 (in another
  format).` when identical). A document with exactly the same text as an earlier one, under any
  name, shows `Same text as D12.` A version that is too different is shown in full. (Known
  limitation: versions are compared after repeated paragraphs are dropped - only a first copy
  is kept - so a change to text repeated within a document can show oddly; a repeated text that
  seems to move is shown only where it first changed.)
- **Drawings**: a PDF with most pages A3 or larger - unless it reads like a document
  (`docs.reads_like_document`: named like a report, programme, register … without a drawing
  number, or most pages hold 300+ characters of sentences not repeated on other pages, as an A3
  report does) -, or a name matching drawing-number patterns (e.g. `-ST-`, `-CI-`, `-C-`, `DWG`,
  `-SK`, sheet/rev markers) and ≤ 5 pages, goes to the `## Drawings` index: one line with the
  ID, name, sheet count, source and, when found in the title block text, title (unless the name
  already has it; a title such as `COVER SHEET AND DRAWING SCHEDULE` is kept whole),
  revision with that revision row's date and description (`rev D 29.04.25 FOOTINGS REVISED TO
  SUIT GEOTECH REV C`; rows the PDF merged into one paragraph are split apart, and the BY / CHK /
  APP initials are left off; the row is left out when it only repeats the status, and a bare revision
  when the name already has it: the file name's `[H]` / `Rev H` wins), and status such as "for
  construction"; a drawing set (more than one sheet) lists its sheets instead (`sheets:
  RD-ST-1001 B COVER SHEET AND DRAWING SCHEDULE; RD-ST-1002 B GENERAL NOTES; …`, at most
  `docdigest.SHEETS_MAX` (20), then `(+N more)`). Light also keeps up to 600 characters of notes
  after `| notes:`, leaving out grid labels and dimension strings (6+ tokens of which under a
  fifth are words), title block cells and the revision row shown, and a note that is on 3 or
  more drawings is shown on the first only. Drawings inside a zip are listed as `D30 > name (1
  sheet) - …` (the `D30 >` says where they are); a member with the same bytes (sha1) as a
  drawing or document listed with its own ID is only named, `D30 > name (same as D12)`, and not
  counted again (a document inside the zip shows `## D30 > name (type)` / `Same as D12.`).
- **Zip files** are documents: a `Files:` line (camera photos grouped as in the email digest,
  `(below)` for documents shown under it, the reason for documents that could not be read, at
  most 12 names per folder; a folder that holds the whole zip is named once, `Files (in IFC
  2025-02-14/): …`, and left out of the names below; a folder with several entries is named
  once, `folder/: a, b` with `; ` between folders; two or more drawings in a folder are counted,
  `16 drawings (see Drawings)`), then each document inside as `## D30 > path (type)` with its own
  cap of max(15% of the cap, min(the cap, 2 × cap ÷ number of documents)).
- **Other files** (unsupported, protected, no text, too big, errors) go into one `## Other files`
  section, one line each: `name (size; source; reason)` - except the loose files of types Squish
  doesn't read in one folder, which share one line when there are more than
  `docdigest.OTHER_GROUP_MIN` (6) of them or 2 or more are images: `documents folder\Photos: 150
  photos IMG_1001-1150, 30 photos DSC01-30, 60 .dwg, Old report.doc (2.3 MB; not read)` (camera
  photos grouped by the name before their number; a type with up to `OTHER_NAMES_MAX` (3) files
  is named, more are counted; `.msg` and `.eml` files are counted together, `9 saved emails`). Attachments and files that couldn't be read keep their own line;
  the counts (`doc_other`) are per file either way.
- **Sources**: `From:` shows the first email (`YY-MM-DD email ALIAS "thread"`, the subject cut
  to 70 characters) with `(+N more emails)`, then the first file (`documents folder\sub`, or the
  file's own folder when it is not under a documents folder, with `(modified YY-MM-DD)`) with
  `(+N more copies)`. No sources -> no `From:` line. A document title property that adds to the
  file name is shown after the type in quotes.
- **Header**: `Covers documents from <first> to <last>` (source dates: email dates and file
  modified dates) and the counts; `part n of m` and `(this part: …)` only when there is more than
  one part; `Focus keywords: …` and a `Dates: only documents attached to emails dated …; files in
  the documents folder are included whatever their date` line when the project has them (the
  engine has already applied them); `Not included: documents in folders Squish could not open:
  <folder>; <folder>` when `not_read_folders` is given. The legend lists only the aliases shown on that part's
  `From:` and drawing lines, with names and domains from the sources; a source without a
  `sender_alias` gets one worked out like the email digest's (`digest.People`).
- **Splitting**: a document is never split across parts unless it alone exceeds the part size;
  it then continues under `## D12 name (continued)` (a line too long for a part is cut after a
  sentence). The Drawings and Other files lists continue under `## Drawings (continued)` /
  `## Other files (continued)`. Each part: `{"text", "first_date", "last_date", "documents"
  (documents + drawings in it), "other_files", "emails": 0, "threads": 0}`.
- **Stats**: `documents`, `doc_drawings`, `doc_other`, `doc_versions` (documents shown as changes,
  as the same text, or as another format of an earlier one), `doc_failed` (other files with status error / too_big / protected /
  no_text), `doc_multi_email`, `raw_chars` (all documents' `chars`), `output_chars`.
- No document and no drawing with readable contents -> no parts (other files alone are not worth a
  digest). Deterministic for a given `now` (any `PYTHONHASHSEED`).

### Email digest cross-references

`build_digest(..., doc_ids=None)`: `doc_ids` maps `(record path, attachment index)` -> `"D12"`.
When given, an attachment whose contents are in the documents digest is written as
`name =D12` inside `[att: …]` (a name already listed in the thread is listed again when its
contents are a different document, i.e. another ID), and the "How to read" of each part that
has such a mark gains the line `"name =D12" in [att: ...] = what the file says is in the
documents digest under "## D12".` Without `doc_ids` (None or empty) the email digest is unchanged.

The result of `build_digest` also has `"aliases"`: `{record path: sender alias}` exactly as the
email digest shows each sender (a copy dropped as a duplicate gets the alias of the copy shown; a
sender the digest does not show, e.g. left out by the focus keywords, gets an alias no shown
person has; `?` when nothing is known). `digest.alias_for_records(records, project,
also_filed=None)` returns the same mapping without rendering (a second pass over the emails, so
the engine uses the result's `"aliases"`).

### Engine

- New progress stage `"documents"` between `"read"` and `"digest"` (extracting, with
  `done/total` and cancel checks between documents). Loose files are scanned in the scan stage.
  Attachments are condensed during the read stage (see Reading attachments); the documents
  stage condenses the loose files (a small thread pool) and puts the documents in order.
- Documents folder scan (`engine.scan_documents`): recursive per `docs_include_subfolders`;
  skips the email files the email scan reads (inside the emails folder, per its Include
  subfolders; other saved .msg/.eml files are listed under Other files, "saved email, not read -
  Squish reads emails only from the Emails tab's folder", with a run-log note naming up to 5 of
  their folders and how to include them), Squish digest files, `~$` files, temp / lock / backup /
  shortcut files (`.tmp`, `.dwl`, `.dwl2`, `.bak`, `.sv$`, `.lnk`, `.url`), hidden and system
  files and folders, and the output folder with everything in it when it is inside the
  documents folder. Only the output folder itself is refused as a documents folder ("documents
  folder not read: it is the output folder"; `engine.same_folder(a, b)` compares folders as
  typed); a documents folder inside the output folder is read. A documents folder that is
  missing, is the output folder, or has folders that can't be opened never stops a run: each
  problem is a `doc_problems` entry (`engine.is_doc_folder_problem(message)` tells folders from
  documents), the folders that couldn't be opened are passed to `build_documents_digest` as
  `not_read_folders` (its header's `Not included:` line; not the output folder, which holds no
  project documents), and each gets a run-log note: "Documents folder not read: <folder> (no
  access to this folder; it held N files last time), so the documents digest has only the
  attachments (its header says so). Run Squish again when the folder can be opened." ("… has
  only the files that could be read" for a folder inside it; "… its documents are not in any
  documents digest written by this run" when none was written; the file count comes from the
  documents cache). The output folder's note is "Documents folder not read: <folder> (it is the
  output folder), so its files are not in the documents digest. Choose another documents folder
  or output folder." The previous documents digest is still replaced, as for any run.
- Which documents: attachments with a supported extension, plus attachments that are other
  document types worth listing (`engine.OTHER_DOC_EXT`: old Office formats, CAD, ...; images and
  other files are only listed in the email digest); every other file in the documents folder.
- IDs `D1`, `D2` … go only to documents whose contents were read (status `ok`), by first
  appearance (attachments by their email's date, undated last, then loose files by path).
  Other files have `"id": ""`. A documents digest is made only when at least one document was
  read; `doc_ids` is passed to `build_digest` only when there are IDs (so without documents
  the email digest is unchanged). If the documents digest can't be made (an error, noted in the
  run log) the email digest is built again without `doc_ids`, so it never points at a missing
  file; RunResult (and `last_run`) get `"doc_digest_failed": true`, and any earlier documents
  file is kept (the run log, the window and the CLI say it is from an earlier run).
- `build_documents_digest` gets `cancel`, a `progress` that reports "Squishing N files..." (the
  documents and other files it is given, as in "Found N files" at the end of the documents
  stage) in the digest stage, `not_read_folders` and `docs_folders=[documents folder]`; a cancelled
  documents digest returns no files and writes nothing. The run log's documents line comes from
  its counts, like the window's summary: "Documents: 65 documents (incl. 30 drawings), 8 other
  files listed; 12 condensed this run (the others came from the documents cache or are file
  types that are only listed)".
- The `docs` list given to `build_documents_digest` has, per document, also `"size"` (bytes),
  and email sources also have `"sender_name"`, `"sender_email"` and `"path"`; `"thread"` is the
  cleaned subject and `"sender_alias"` comes from the email digest's result `"aliases"`
  (`{record path: alias}`, the same as `digest.alias_for_records`), else `""` (docdigest then
  aliases the senders from their names). One email filed in several folders is one source. A
  file source's
  `"mtime"` is local ISO 8601 with offset, like EmailRecord dates.
- Dates and focus keywords are applied by the engine, before `build_documents_digest`
  (docdigest must not filter again; it only says so in its header): attachments go with their
  email's date (loose files are not dated); with focus keywords a document is kept when a
  keyword (matched as in the email digest) is in its name or text, or when its `=Dn` appears
  in the email digest (it is attached to an email in a kept thread). A run whose filters leave
  no emails can still write a documents digest.
- Output file names: `Squish - <Project> - documents - <dates>.txt` (plus the usual
  `(part N of M)`, `(only …)`, `(focus …)`); written and replaced with the same all-or-nothing
  swap (one swap for both digests) and `.outputs.json` bookkeeping as the email digest (its own
  kind: key `"documents"` or `"documents | <filter_key>"`, so an email-only run never deletes a
  documents digest and vice versa; `engine.digest_file_kind(filename)`).
- RunResult `files[i]` gains `"kind": "emails" | "documents"` (a documents file also has
  `"documents"`, the part's document count); `stats` gains `documents`, `doc_drawings`,
  `doc_other`, `doc_versions`, `doc_failed` (docdigest's own numbers under those names win,
  else the engine counts; all 0 when no documents digest was written) and, only when documents
  were wanted, `doc_found` (the files the documents digest would list, after the focus
  keywords: the window's "No documents file: …" sentence uses it); RunResult gains
  `"doc_problems": [[where, reason], ...]` (documents whose contents couldn't be read - error,
  too big, protected, no text - as `"<name> (attached to <email path>)"` or the file's path,
  and documents folder problems). A document that fails to extract never stops a run (it is
  listed under Other files with its reason, and in the run log's "Documents that could not be
  read" section).
- No documents found -> no documents digest (not an error).

### CLI and GUI

- CLI: `--no-docs` (skip attachments), `--docs-folder DIR` (also with a saved project, for that
  run only). The summary adds `Documents: 64 documents, 9 drawings, 31 other files` (+ `(N later
  versions shown as changes)`), documents folder problems, `N documents could not be read (see
  the run log)` and, when the documents digest failed, "The documents digest could not be made
  (see the run log); any earlier documents file was kept." `list` also shows a project's
  `documents folder: …` and `attachments: not condensed` when that box is off.
- GUI: a **Documents** tab (second, after Emails): "Condense Word, Excel, PowerPoint and PDF
  attachments" checkbox, a "Documents folder" (entry + Browse + Include subfolders, with a
  background file count like the emails folder: `gui.count_document_files` uses
  `engine.scan_documents`, skipping the output folder, and returns `(documents, other files,
  email files)`: documents Squish condenses (`docs.is_supported`) apart from other files, which
  are only listed: "64 documents found (incl. subfolders) - plus 31 other files, listed, not
  read."; with only other files: "No Word, Excel, PowerPoint, PDF or text files here - just 12
  other files (e.g. photos, CAD, old .doc/.xls), which Squish can't read, so this folder alone
  won't make a documents file." (amber); like a run it is given the Emails tab's folder and
  Include subfolders, so saved emails the email scan doesn't read count as other files; the
  email files left out are counted only when nothing else is found, for "Only emails here
  (164 email files - Squish reads emails from the Emails tab's folder), no other documents
  found." in grey (amber only when subfolders couldn't be opened); a folder that is missing or
  is the output folder itself (`gui.docs_folder_problem`, `engine.same_folder`) gets an amber
  warning, never an error, as it never stops a run; while the box is blank its hint follows the
  attachments box (`gui.docs_folder_blank_hint`: "Leave blank to condense only the attachments."
  / "Leave blank for no documents folder."), and a "PDF reader" line from
  `docs.backend_status()` ("checking..." until known; "Installed (pypdf 5.1.0) - PDFs are read
  with the better reader.", or plain advice when pypdf is missing: "PDFs are read with Squish's
  built-in reader. For better results, run Install Squish.bat again or ask IT to install the
  pypdf package."; off Windows: "… install the pypdf package (pip install pypdf)."). The
  count and the PDF line also show in the compact layout. The results table gets a **Type**
  column (Emails / Documents), and its last column is **Contains** ("1,876 emails" /
  "64 documents"); the File column takes the room the others leave. The summary calls the
  email digest "1 emails file, ~60k tokens" when there is a documents file too, and adds "65
  documents (incl. 30 drawings) in 1 documents file, ~55k tokens, plus 8 other files listed (2
  of them couldn't be read - see run log)." (drawings count as documents, as in a part's
  `documents`; the documents that couldn't be read are among the other files, so they are
  counted there, with a sentence of their own only when there are more of them than other files
  or no documents file was written) and "The documents folder (or a folder in it) couldn't be
  opened (see run log)." ("The documents folder is the output folder, so it was skipped (see run
  log)." for that case); a run that wanted documents but wrote no documents file says why
  (`gui.no_documents_note`, from `stats["doc_found"]`): "No documents file: none of the N files
  found could be read (see run log).", "No documents file: no documents match the focus
  keywords." or "No documents file: no documents were found." (a failed documents digest has
  `gui.docs_digest_warning` instead); a dated run whose documents file has dates outside the run's adds
  `gui.undated_docs_note`: "The documents file also has the files in the documents folder,
  whatever their date (attachments follow the dates)." The read stage shows "Reading emails and
  attachments 55 of 164..." when attachments are condensed, and the documents stage "Condensing
  documents: 12 of 64 files..." (the count includes the other files). With both kinds of file, the tip and the Done message say: drag in the
  emails file first; add the documents file when you need what the documents say (or, when
  together they are too big for one chat, a new chat for each file, starting with the emails
  file); the tip, the Done message and Help add that Claude links emails to their attachments
  (the `=D12` marks) only when both files are in the same chat - for that, narrow the run with
  dates or Focus keywords. The Emails tab's dates hint (when a documents folder is set) and
  Help's "Only need a period?" tip say the files in the documents folder are included whatever
  their date. Each Squeeze level's text ends with what it does to documents, from
  `docdigest.DOC_CAPS` ("Long documents condensed to about 8,000 characters each."; not in the
  compact layout, `gui.squeeze_options(docs_notes=False)`, so the Squeeze tab stays as short as
  in v1.0 and the results table keeps its rows), and the focus hint says "conversations (and
  documents)". The welcome screen says Squish makes small text files from a project's emails
  and documents.
- `Install Squish.bat` also tries `pip install --user --upgrade pypdf` and then, as a separate
  step that may fail without losing pypdf (not `pypdf[crypto]`), `cryptography` (for AES-secured
  PDFs; log in `%TEMP%\squish-pip-crypto-log.txt`); `requirements.txt` lists both as optional.
