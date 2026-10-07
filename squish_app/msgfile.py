"""Built-in reader for Outlook .msg files. Standard library only.

Squish prefers the optional ``extract-msg`` package, but that cannot always be
installed on a locked-down work laptop, so this module can read the parts of a
.msg file that Squish needs on its own.

A .msg file is an OLE "Compound File" (a little file system inside one file,
described in Microsoft's [MS-CFB] document). Inside it, every email property is
stored as described in [MS-OXMSG]:

* ``__substg1.0_XXXXTTTT`` streams hold variable-size properties, where XXXX
  is the property id and TTTT its type (001F = UTF-16 text, 001E = 8-bit text,
  0102 = binary);
* ``__properties_version1.0`` holds the fixed-size ones (numbers, flags, dates);
* ``__recip_version1.0_#XXXXXXXX`` storages are the recipients and
  ``__attach_version1.0_#XXXXXXXX`` storages the attachments;
* ``__nameid_version1.0`` maps "named" properties (such as a meeting's start
  time) to the property ids 0x8000 and up that this message uses for them.

Only reading is supported. Damaged files raise ``MsgFileError`` rather than
hanging or using lots of memory.

Main entry points: ``read_msg(path)`` and ``read_msg_bytes(data)``, which
return a plain dict of message fields (see ``_read_message``), plus
``decompress_rtf`` and ``rtf_to_html_or_text`` for the compressed RTF body.
"""

import array
import codecs
import datetime
import email.header
import io
import os
import re
import struct
import sys
import uuid

__all__ = [
    "MsgFileError",
    "CompoundFile",
    "read_msg",
    "read_msg_bytes",
    "decompress_rtf",
    "rtf_to_html_or_text",
    "decode_html_bytes",
    "codec_for_codepage",
]


class MsgFileError(Exception):
    """The file is not a readable Outlook .msg file."""


# --------------------------------------------------------------------------
# Compound File Binary format ([MS-CFB])
# --------------------------------------------------------------------------

_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_MAXREGSECT = 0xFFFFFFFA
_ENDOFCHAIN = 0xFFFFFFFE
_FREESECT = 0xFFFFFFFF
_NOSTREAM = 0xFFFFFFFF

_TYPE_EMPTY = 0
_TYPE_STORAGE = 1
_TYPE_STREAM = 2
_TYPE_ROOT = 5

# Files up to this size are read in one go (one trip to the file server).
# Bigger ones (usually emails with drawings attached) are read in place, so
# attachment data is never downloaded. Same size as readers.EXTRACT_MSG_MAX_BYTES.
_IN_MEMORY_LIMIT = 2 * 1024 * 1024
_READ_BUFFER = 64 * 1024   # fewer, bigger reads of the FAT, directory and mini stream
_MAX_DIRECTORY_ENTRIES = 500000
_DEFAULT_READ_LIMIT = 64 * 1024 * 1024  # never read more than this from one stream


def _uint32_array(data):
    """Little-endian bytes -> array of unsigned 32-bit ints."""
    code = "I" if array.array("I").itemsize == 4 else "L"
    arr = array.array(code)
    arr.frombytes(data[: len(data) - len(data) % 4])
    if sys.byteorder == "big":
        arr.byteswap()
    return arr


class _Entry(object):
    """One directory entry: a storage (folder) or a stream (file)."""

    __slots__ = ("sid", "name", "kind", "left", "right", "child", "start", "size")

    def __init__(self, sid, name, kind, left, right, child, start, size):
        self.sid = sid
        self.name = name
        self.kind = kind
        self.left = left
        self.right = right
        self.child = child
        self.start = start
        self.size = size

    def is_storage(self):
        return self.kind in (_TYPE_STORAGE, _TYPE_ROOT)

    def is_stream(self):
        return self.kind == _TYPE_STREAM

    def __repr__(self):
        return "<_Entry %d %r kind=%d size=%d>" % (self.sid, self.name, self.kind, self.size)


class CompoundFile(object):
    """Read-only access to the storages and streams of an OLE compound file.

    ``fileobj`` must support seek() and read(). Use ``root`` and
    ``children(storage)`` to navigate, and ``read(entry)`` to get a stream.
    """

    def __init__(self, fileobj, file_size=None):
        self._f = fileobj
        if file_size is None:
            fileobj.seek(0, os.SEEK_END)
            file_size = fileobj.tell()
        self._file_size = file_size
        self._children_cache = {}
        self._mini_stream = None
        self._read_header()
        self._read_fat()
        self._read_directory()
        self._read_minifat()

    # ---- low level -----------------------------------------------------

    def _read_at(self, offset, length):
        self._f.seek(offset)
        return self._f.read(length)

    def _sector_offset(self, sector):
        return (sector + 1) << self._shift

    def _read_header(self):
        if self._file_size < 512:
            raise MsgFileError("file is too small to be an Outlook .msg file")
        hdr = self._read_at(0, 512)
        if len(hdr) < 512 or hdr[:8] != _SIGNATURE:
            raise MsgFileError("not an Outlook .msg file (no OLE signature)")
        _minor, major, byte_order, shift, mini_shift = struct.unpack_from("<HHHHH", hdr, 24)
        if byte_order != 0xFFFE:
            raise MsgFileError("unsupported OLE byte order")
        if shift not in (9, 12):
            raise MsgFileError("unsupported OLE sector size")
        if mini_shift != 6:
            raise MsgFileError("unsupported OLE mini sector size")
        (_n_dir, n_fat, first_dir, _txn, cutoff, first_minifat, n_minifat,
         first_difat, n_difat) = struct.unpack_from("<IIIIIIIII", hdr, 40)
        self._major = major
        self._shift = shift
        self.sector_size = 1 << shift
        self.mini_sector_size = 1 << mini_shift
        self._mini_cutoff = cutoff if 0 < cutoff <= (1 << 20) else 4096
        self._first_dir = first_dir
        self._first_minifat = first_minifat
        self._n_minifat = n_minifat
        # Sectors that exist in the file (the last one may be cut short).
        self._n_sectors = max(0, (self._file_size - self.sector_size + self.sector_size - 1)
                              // self.sector_size)
        if n_fat > self._n_sectors:
            raise MsgFileError("OLE header claims more FAT sectors than the file holds")
        self._n_fat = n_fat
        self._header_difat = struct.unpack_from("<109I", hdr, 76)
        self._first_difat = first_difat
        self._n_difat = n_difat

    def _check_sector(self, sector):
        if sector >= self._n_sectors:
            raise MsgFileError("OLE sector number %d is outside the file" % sector)

    def _read_fat(self):
        """Collect the FAT sector numbers (header DIFAT + DIFAT chain), then the FAT."""
        fat_sectors = [s for s in self._header_difat[: self._n_fat] if s <= _MAXREGSECT]
        per_sector = self.sector_size // 4
        sector = self._first_difat
        seen = set()
        while sector <= _MAXREGSECT and len(fat_sectors) < self._n_fat:
            if sector in seen:
                raise MsgFileError("OLE DIFAT chain loops")
            if len(seen) > self._n_sectors:
                raise MsgFileError("OLE DIFAT chain is too long")
            seen.add(sector)
            self._check_sector(sector)
            values = _uint32_array(self._read_at(self._sector_offset(sector), self.sector_size))
            fat_sectors.extend(s for s in values[: per_sector - 1] if s <= _MAXREGSECT)
            sector = values[per_sector - 1] if len(values) == per_sector else _ENDOFCHAIN
        fat_sectors = fat_sectors[: self._n_fat]
        if not fat_sectors:
            raise MsgFileError("OLE file has no FAT")
        parts = []
        for s in fat_sectors:
            self._check_sector(s)
            parts.append(self._read_at(self._sector_offset(s), self.sector_size))
        self._fat = _uint32_array(b"".join(parts))

    def _chain(self, start, table, limit=None):
        """Follow a sector chain in a FAT or mini FAT. Stops after ``limit`` sectors."""
        chain = []
        seen = set()
        sector = start
        table_len = len(table)
        while sector != _ENDOFCHAIN and sector != _FREESECT:
            if sector >= table_len:
                raise MsgFileError("broken OLE sector chain")
            if sector in seen:
                raise MsgFileError("OLE sector chain loops")
            seen.add(sector)
            chain.append(sector)
            if limit is not None and len(chain) >= limit:
                break
            sector = table[sector]
        return chain

    def _read_chain(self, chain, nbytes):
        """Read regular sectors (merging neighbours into one read)."""
        out = []
        size = self.sector_size
        i = 0
        while i < len(chain):
            j = i
            while j + 1 < len(chain) and chain[j + 1] == chain[j] + 1:
                j += 1
            self._check_sector(chain[j])
            out.append(self._read_at(self._sector_offset(chain[i]), (j - i + 1) * size))
            i = j + 1
        return b"".join(out)[:nbytes]

    def _read_directory(self):
        chain = self._chain(self._first_dir, self._fat)
        if not chain:
            raise MsgFileError("OLE file has no directory")
        if len(chain) * self.sector_size // 128 > _MAX_DIRECTORY_ENTRIES:
            raise MsgFileError("OLE directory is unreasonably large")
        data = self._read_chain(chain, len(chain) * self.sector_size)
        entries = []
        for sid in range(len(data) // 128):
            off = sid * 128
            name_len = struct.unpack_from("<H", data, off + 64)[0]
            kind = data[off + 66]
            left, right, child = struct.unpack_from("<III", data, off + 68)
            start = struct.unpack_from("<I", data, off + 116)[0]
            size = struct.unpack_from("<Q", data, off + 120)[0]
            if self.sector_size == 512:
                size &= 0xFFFFFFFF  # version 3 files: high half is undefined
            name_len = min(max(name_len - 2, 0), 62)
            name = data[off:off + name_len].decode("utf-16-le", "replace")
            entries.append(_Entry(sid, name, kind, left, right, child, start, size))
        if not entries or entries[0].kind != _TYPE_ROOT:
            raise MsgFileError("OLE file has no root entry")
        self._entries = entries
        self.root = entries[0]

    def _read_minifat(self):
        if self._first_minifat > _MAXREGSECT or self._n_minifat == 0:
            self._minifat = array.array("I")
            return
        chain = self._chain(self._first_minifat, self._fat, limit=self._n_minifat)
        self._minifat = _uint32_array(self._read_chain(chain, len(chain) * self.sector_size))

    def _get_mini_stream(self):
        """The mini stream (holds all streams smaller than 4096 bytes), read once."""
        if self._mini_stream is None:
            root = self.root
            size = root.size
            if size > self._file_size:
                raise MsgFileError("OLE mini stream is larger than the file")
            if size == 0 or root.start > _MAXREGSECT:
                self._mini_stream = b""
            else:
                n = (size + self.sector_size - 1) // self.sector_size
                chain = self._chain(root.start, self._fat, limit=n)
                self._mini_stream = self._read_chain(chain, size)
        return self._mini_stream

    # ---- public --------------------------------------------------------

    def children(self, storage):
        """Dict of UPPER-CASE name -> entry for the direct children of a storage."""
        cached = self._children_cache.get(storage.sid)
        if cached is not None:
            return cached
        result = {}
        entries = self._entries
        stack = [storage.child]
        seen = set()
        while stack:
            sid = stack.pop()
            if sid == _NOSTREAM or sid >= len(entries) or sid in seen:
                continue
            seen.add(sid)
            entry = entries[sid]
            if entry.kind in (_TYPE_STORAGE, _TYPE_STREAM):
                result[entry.name.upper()] = entry
            stack.append(entry.left)
            stack.append(entry.right)
        self._children_cache[storage.sid] = result
        return result

    def read(self, entry, max_bytes=None):
        """Return the contents of a stream entry (at most ``max_bytes``)."""
        if not entry.is_stream():
            raise MsgFileError("%r is not a stream" % entry.name)
        size = entry.size
        if max_bytes is None:
            max_bytes = _DEFAULT_READ_LIMIT
        nbytes = min(size, max_bytes)
        if nbytes <= 0:
            return b""
        if size < self._mini_cutoff:
            mini = self._get_mini_stream()
            msize = self.mini_sector_size
            n = (nbytes + msize - 1) // msize
            chain = self._chain(entry.start, self._minifat, limit=n)
            parts = []
            for s in chain:
                off = s * msize
                if off >= len(mini):
                    raise MsgFileError("OLE mini sector is outside the mini stream")
                parts.append(mini[off:off + msize])
            return b"".join(parts)[:nbytes]
        if size > self._file_size:
            raise MsgFileError("OLE stream %r is larger than the file" % entry.name)
        n = (nbytes + self.sector_size - 1) // self.sector_size
        chain = self._chain(entry.start, self._fat, limit=n)
        return self._read_chain(chain, nbytes)


# --------------------------------------------------------------------------
# MAPI properties ([MS-OXMSG], ids from [MS-OXPROPS])
# --------------------------------------------------------------------------

PT_SHORT = 0x0002
PT_LONG = 0x0003
PT_BOOLEAN = 0x000B
PT_I8 = 0x0014
PT_STRING8 = 0x001E
PT_UNICODE = 0x001F
PT_SYSTIME = 0x0040
PT_BINARY = 0x0102

PR_MESSAGE_CLASS = 0x001A
PR_SUBJECT = 0x0037
PR_CLIENT_SUBMIT_TIME = 0x0039
PR_SENT_REPRESENTING_NAME = 0x0042
PR_SENT_REPRESENTING_ADDRTYPE = 0x0064
PR_SENT_REPRESENTING_EMAIL_ADDRESS = 0x0065
PR_CONVERSATION_TOPIC = 0x0070
PR_TRANSPORT_MESSAGE_HEADERS = 0x007D
PR_SENDER_NAME = 0x0C1A
PR_SENDER_ADDRTYPE = 0x0C1E
PR_SENDER_EMAIL_ADDRESS = 0x0C1F
PR_RECIPIENT_TYPE = 0x0C15
PR_DISPLAY_CC = 0x0E03
PR_DISPLAY_TO = 0x0E04
PR_MESSAGE_DELIVERY_TIME = 0x0E06
PR_MESSAGE_FLAGS = 0x0E07
PR_ATTACH_SIZE = 0x0E20
PR_BODY = 0x1000
PR_RTF_COMPRESSED = 0x1009
PR_HTML = 0x1013
PR_INTERNET_MESSAGE_ID = 0x1035
PR_IN_REPLY_TO_ID = 0x1042
PR_DISPLAY_NAME = 0x3001
PR_ADDRTYPE = 0x3002
PR_EMAIL_ADDRESS = 0x3003
PR_CREATION_TIME = 0x3007
PR_STORE_SUPPORT_MASK = 0x340D
PR_ATTACH_FILENAME = 0x3704
PR_ATTACH_METHOD = 0x3705
PR_ATTACH_LONG_FILENAME = 0x3707
PR_ATTACH_MIME_TAG = 0x370E
PR_ATTACH_CONTENT_ID = 0x3712
PR_ATTACH_FLAGS = 0x3714
PR_SMTP_ADDRESS = 0x39FE
PR_INTERNET_CPID = 0x3FDE
PR_MESSAGE_CODEPAGE = 0x3FFD
PR_SENDER_SMTP_ADDRESS = 0x5D01
PR_SENT_REPRESENTING_SMTP_ADDRESS = 0x5D02
PR_ATTACHMENT_HIDDEN = 0x7FFE

MSGFLAG_UNSENT = 0x0008
ATTACH_EMBEDDED_MSG = 5

# Meeting properties. PR_START_DATE / PR_END_DATE are ordinary properties; the
# others are "named" properties in the PSETID_Appointment set ([MS-OXOCAL]).
PR_START_DATE = 0x0060
PR_END_DATE = 0x0061
PSETID_APPOINTMENT = uuid.UUID("00062002-0000-0000-C000-000000000046")
LID_LOCATION = 0x8208
LID_APPOINTMENT_START_WHOLE = 0x820D
LID_APPOINTMENT_END_WHOLE = 0x820E
MEETING_CLASSES = ("ipm.schedule.meeting.request", "ipm.schedule.meeting.canceled",
                    "ipm.appointment")

# Clear-signed S/MIME emails keep their whole content (text and attachments)
# as MIME inside one attachment called smime.p7m. Its bytes are kept (up to
# this size) so readers.py can unpack it; other attachments are never loaded.
_MAX_SMIME_BYTES = 32 * 1024 * 1024

_FILETIME_EPOCH = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)

_CODEPAGE_NAMES = {
    20127: "ascii",
    20866: "koi8_r",
    21866: "koi8_u",
    28591: "cp1252",   # Latin-1 labelled text is almost always Windows-1252
    28592: "iso8859_2",
    28595: "iso8859_5",
    28597: "iso8859_7",
    28599: "iso8859_9",
    28603: "iso8859_13",
    28605: "iso8859_15",
    50220: "iso2022_jp",
    50221: "iso2022_jp",
    50222: "iso2022_jp",
    50225: "iso2022_kr",
    51932: "euc_jp",
    51949: "euc_kr",
    52936: "hz",
    54936: "gb18030",
    936: "gbk",
    1200: "utf-16-le",
    1201: "utf-16-be",
    10000: "mac_roman",
    65000: "utf-7",
    65001: "utf-8",
}


def codec_for_codepage(codepage, default="cp1252"):
    """Windows code page number -> Python codec name (``default`` if unknown)."""
    if not codepage:
        return default
    name = _CODEPAGE_NAMES.get(codepage, "cp%d" % codepage)
    try:
        codecs.lookup(name)
    except LookupError:
        return default
    return name


# PR_INTERNET_CPID is the MIME charset of the message; 8-bit MAPI strings use
# the matching Windows ("ANSI") code page instead, e.g. iso-2022-jp -> 932.
_ANSI_FOR_INTERNET = {
    20127: 1252, 28591: 1252, 28605: 1252, 65001: 1252, 65000: 1252,
    28592: 1250, 28595: 1251, 20866: 1251, 21866: 1251, 28597: 1253,
    28599: 1254, 28598: 1255, 38598: 1255, 28596: 1256, 28594: 1257,
    28603: 1257, 50220: 932, 50221: 932, 50222: 932, 51932: 932, 20932: 932,
    52936: 936, 20936: 936, 50225: 949, 51949: 949, 10000: 1252,
}


def ansi_codepage_for(internet_cpid):
    """Windows code page used for 8-bit strings, given PR_INTERNET_CPID."""
    if not internet_cpid:
        return None
    return _ANSI_FOR_INTERNET.get(internet_cpid, internet_cpid)


# UTF-16 and UTF-32: never the encoding of 8-bit (PT_STRING8) text.
_WIDE_CODEPAGES = (1200, 1201, 12000, 12001)


def eight_bit_codepage(codepage, internet_cpid=None):
    """The code page to read 8-bit text with, or None if nothing says.

    ``codepage`` is PR_MESSAGE_CODEPAGE (or an RTF \\ansicpg). A missing or
    UTF-16/UTF-32 label falls back to the Windows code page matching
    ``internet_cpid`` (PR_INTERNET_CPID); a UTF-16/UTF-32 one there too gives
    None. Plain ASCII is read as Windows-1252, which also covers the odd
    smart quote.
    """
    if not codepage or codepage in _WIDE_CODEPAGES:
        codepage = ansi_codepage_for(internet_cpid)
    if not codepage or codepage in _WIDE_CODEPAGES:
        return None
    if codepage == 20127:
        return 1252
    return codepage


def decode_8bit(data, codec):
    """8-bit text bytes -> str. Text labelled UTF-8 that is not valid UTF-8
    (Windows-1252 bytes under a wrong label) is read as Windows-1252."""
    if codec == "utf-8":
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason == "unexpected end of data":   # only a character cut off at the end
                return data.decode("utf-8", "replace")
            return data.decode("cp1252", "replace")
    return data.decode(codec, "replace")


def filetime_to_datetime(value):
    """Windows FILETIME (100 ns ticks since 1601, UTC) -> aware datetime or None."""
    if not value or value >= 0x7FFFFFFFFFFFFFFF:
        return None
    try:
        dt = _FILETIME_EPOCH + datetime.timedelta(microseconds=value // 10)
    except OverflowError:
        return None
    # Outlook uses 4501-01-01 for "no date"; anything before 1971 is junk too.
    if dt.year < 1971 or dt.year >= 4500:
        return None
    return dt


def _clean_string(text):
    """Drop the NUL padding some writers leave in strings."""
    if not text:
        return ""
    return text.replace("\x00", "")


class _PropertyBag(object):
    """The MAPI properties of one object (message, recipient or attachment)."""

    def __init__(self, cf, storage, header_size, codepage=None):
        self.cf = cf
        self.storage = storage
        self.streams = cf.children(storage)
        self.fixed = {}
        entry = self.streams.get("__PROPERTIES_VERSION1.0")
        if entry is not None and entry.is_stream():
            data = cf.read(entry, max_bytes=4 * 1024 * 1024)
            for off in range(header_size, len(data) - 15, 16):
                tag = struct.unpack_from("<I", data, off)[0]
                ptype = tag & 0xFFFF
                pid = tag >> 16
                if pid not in self.fixed:
                    self.fixed[pid] = (ptype, data[off + 8:off + 16])
        if codepage is None:
            codepage = eight_bit_codepage(self.integer(PR_MESSAGE_CODEPAGE),
                                          self.integer(PR_INTERNET_CPID))
        self.codepage = codepage
        self.codec = codec_for_codepage(codepage)

    def _stream(self, pid, ptype, max_bytes=None):
        entry = self.streams.get("__SUBSTG1.0_%04X%04X" % (pid, ptype))
        if entry is None or not entry.is_stream():
            return None
        return self.cf.read(entry, max_bytes=max_bytes)

    def has(self, pid, ptype):
        return ("__SUBSTG1.0_%04X%04X" % (pid, ptype)) in self.streams

    def string(self, pid, max_bytes=None):
        """A text property (UTF-16 or 8-bit), or "" if missing."""
        data = self._stream(pid, PT_UNICODE, max_bytes)
        if data is not None:
            if len(data) % 2:
                data = data[:-1]
            return _clean_string(data.decode("utf-16-le", "replace"))
        data = self._stream(pid, PT_STRING8, max_bytes)
        if data is not None:
            return _clean_string(decode_8bit(data, self.codec))
        return ""

    def binary(self, pid, max_bytes=None):
        return self._stream(pid, PT_BINARY, max_bytes)

    def integer(self, pid):
        """A PT_LONG / PT_SHORT / PT_I8 property as an int, or None."""
        item = self.fixed.get(pid)
        if item is None:
            return None
        ptype, raw = item
        if ptype == PT_LONG:
            return struct.unpack_from("<I", raw)[0]
        if ptype == PT_SHORT:
            return struct.unpack_from("<H", raw)[0]
        if ptype == PT_I8:
            return struct.unpack_from("<Q", raw)[0]
        return None

    def boolean(self, pid):
        item = self.fixed.get(pid)
        if item is None or item[0] != PT_BOOLEAN:
            return False
        return struct.unpack_from("<H", item[1])[0] != 0

    def time(self, pid):
        item = self.fixed.get(pid)
        if item is None or item[0] != PT_SYSTIME:
            return None
        return filetime_to_datetime(struct.unpack_from("<Q", item[1])[0])

    def sub_storages(self, prefix):
        """Child storages whose name starts with ``prefix``, in name order."""
        prefix = prefix.upper()
        found = [(name, e) for name, e in self.streams.items()
                 if e.is_storage() and name.startswith(prefix)]
        found.sort()
        return [e for _name, e in found]


# --------------------------------------------------------------------------
# Message fields
# --------------------------------------------------------------------------

_MAX_TEXT_BYTES = 16 * 1024 * 1024
_MAX_RTF_BYTES = 16 * 1024 * 1024
_MAX_EMBED_DEPTH = 5


def read_msg(path, want_data=None):
    """Read a .msg file and return its fields as a dict (see ``_read_message``).

    ``path`` is used as given (callers pass a long-path-safe string on Windows).
    A small file is read in one go; a big one is read in place, so only its
    directory and property streams are read (attachment data is not
    downloaded from the file server, except the attachments ``want_data``
    asks for).

    ``want_data(name, size)``, if given, is asked about each attachment of the
    filed email (not of attached emails); when it returns True the attachment's
    bytes are read into its "data" entry (used for documents, see docs.py).
    """
    with open(path, "rb", buffering=_READ_BUFFER) as f:
        size = os.fstat(f.fileno()).st_size
        if size <= _IN_MEMORY_LIMIT:
            data = f.read()
            return read_msg_bytes(data, want_data)
        return _read_message(CompoundFile(f, size), None, top_level=True, want_data=want_data)


def read_msg_bytes(data, want_data=None):
    """Like ``read_msg`` but for the bytes of a .msg file."""
    cf = CompoundFile(io.BytesIO(data), len(data))
    return _read_message(cf, None, top_level=True, want_data=want_data)


def _read_message(cf, storage, top_level=True, parent_codepage=None, depth=0, want_data=None):
    """Read the fields Squish needs from a message storage.

    Returns a dict:
      subject, conversation_topic, item_class, message_id, in_reply_to,
      headers (raw transport headers text),
      sender_name, sender_addresses (list, best first, may include X500),
      on_behalf_name, on_behalf_addresses,
      display_to, display_cc,
      recipients: [{"name", "addresses", "type"}]  (type 1 To, 2 Cc, 3 Bcc),
      attachments: [{"name", "size", "content_id", "hidden", "flags", "mime",
                     "method", "embedded"}],
      submit_time, delivery_time, creation_time (aware datetimes or None),
      unsent (bool),
      body (plain text or None), html (str or None),
      rtf (("html" | "text", str) or None; only when there is no other body),
      html_cids (set of lower-case cid references found in the HTML),
      meeting_start, meeting_end (aware datetimes or None) and meeting_location
      (str): the meeting's own time and place, only for the top-level message of
      a meeting request, cancellation or appointment (None, None, "" otherwise).
    An attachment dict also has "data" (bytes) for the smime.p7m attachment of
    a clear-signed S/MIME email and for the attachments ``want_data(name,
    size)`` asks for (depth 0 only; None for every other attachment), and
    "message": for an email attached to the filed email (depth 0 only), that
    email's own fields as returned here (its attached emails get their name
    only, never a "message"); None for every other attachment, or when the
    attached email can't be read. An attached email is named by its subject.
    """
    if storage is None:
        storage = cf.root
    header_size = 32 if top_level else 24
    props = _PropertyBag(cf, storage, header_size)
    if props.codepage is None and parent_codepage:
        props.codepage = parent_codepage
        props.codec = codec_for_codepage(parent_codepage)

    flags = props.integer(PR_MESSAGE_FLAGS) or 0
    fields = {
        "subject": props.string(PR_SUBJECT),
        "conversation_topic": props.string(PR_CONVERSATION_TOPIC),
        "item_class": props.string(PR_MESSAGE_CLASS).strip(),
        "message_id": props.string(PR_INTERNET_MESSAGE_ID).strip(),
        "in_reply_to": props.string(PR_IN_REPLY_TO_ID).strip(),
        "headers": props.string(PR_TRANSPORT_MESSAGE_HEADERS, max_bytes=1024 * 1024),
        "sender_name": props.string(PR_SENDER_NAME),
        "sender_addresses": [props.string(PR_SENDER_SMTP_ADDRESS),
                             props.string(PR_SENDER_EMAIL_ADDRESS)],
        "on_behalf_name": props.string(PR_SENT_REPRESENTING_NAME),
        "on_behalf_addresses": [props.string(PR_SENT_REPRESENTING_SMTP_ADDRESS),
                                props.string(PR_SENT_REPRESENTING_EMAIL_ADDRESS)],
        "display_to": props.string(PR_DISPLAY_TO),
        "display_cc": props.string(PR_DISPLAY_CC),
        "recipients": [],
        "attachments": [],
        "submit_time": props.time(PR_CLIENT_SUBMIT_TIME),
        "delivery_time": props.time(PR_MESSAGE_DELIVERY_TIME),
        "creation_time": props.time(PR_CREATION_TIME),
        "unsent": bool(flags & MSGFLAG_UNSENT),
        "body": None,
        "html": None,
        "rtf": None,
        "html_cids": set(),
        "meeting_start": None,
        "meeting_end": None,
        "meeting_location": "",
    }
    if top_level and fields["item_class"].lower().startswith(MEETING_CLASSES):
        try:
            _read_meeting(cf, props, fields)
        except MsgFileError:
            pass  # a damaged meeting property: keep the email without the meeting time
    smime = fields["item_class"].lower().startswith("ipm.note.smime")

    for rstore in props.sub_storages("__recip_version1.0_"):
        rprops = _PropertyBag(cf, rstore, 8, codepage=props.codepage)
        fields["recipients"].append({
            "name": rprops.string(PR_DISPLAY_NAME),
            "addresses": [rprops.string(PR_SMTP_ADDRESS), rprops.string(PR_EMAIL_ADDRESS)],
            "type": (rprops.integer(PR_RECIPIENT_TYPE) or 1) & 0x0F,
        })

    any_cid = False
    for astore in props.sub_storages("__attach_version1.0_"):
        aprops = _PropertyBag(cf, astore, 8, codepage=props.codepage)
        method = aprops.integer(PR_ATTACH_METHOD) or 0
        name = (aprops.string(PR_ATTACH_LONG_FILENAME)
                or aprops.string(PR_ATTACH_FILENAME)
                or aprops.string(PR_DISPLAY_NAME))
        embedded = False
        message = None
        if method == ATTACH_EMBEDDED_MSG:
            embedded = True
            inner = aprops.streams.get("__SUBSTG1.0_3701000D")
            subject = ""
            if inner is not None and inner.is_storage() and depth == 0:
                # An email attached to the filed email: read it too (one level
                # only), so its sender, date and text are not lost.
                message = _read_attached_message(cf, inner, props.codepage)
                subject = (message or {}).get("subject", "").strip()
            elif inner is not None and inner.is_storage() and depth < _MAX_EMBED_DEPTH:
                subject = _embedded_subject(cf, inner, props.codepage)
            name = subject or aprops.string(PR_DISPLAY_NAME) or name or "attached message"
        content_id = aprops.string(PR_ATTACH_CONTENT_ID).strip()
        any_cid = any_cid or bool(content_id)
        size = aprops.integer(PR_ATTACH_SIZE)
        data_entry = aprops.streams.get("__SUBSTG1.0_37010102")
        if data_entry is not None and not data_entry.is_stream():
            data_entry = None
        if size is None and data_entry is not None:
            size = data_entry.size
        mime = aprops.string(PR_ATTACH_MIME_TAG).strip().lower()
        data = None
        if (smime and data_entry is not None and data_entry.size <= _MAX_SMIME_BYTES
                and (name.strip().lower() == "smime.p7m" or mime == "multipart/signed")):
            data = cf.read(data_entry, max_bytes=_MAX_SMIME_BYTES)
        elif (want_data is not None and depth == 0 and not embedded and data_entry is not None
              and want_data(name.strip(), data_entry.size)):
            # A document the caller wants (e.g. a PDF report): read just this stream.
            data = _read_attachment_data(cf, data_entry)
        fields["attachments"].append({
            "name": name.strip(),
            "size": size,
            "content_id": content_id,
            "hidden": aprops.boolean(PR_ATTACHMENT_HIDDEN),
            "flags": aprops.integer(PR_ATTACH_FLAGS) or 0,
            "mime": mime,
            "method": method,
            "embedded": embedded,
            "data": data,
            "message": message,
        })

    body = None
    if props.has(PR_BODY, PT_UNICODE) or props.has(PR_BODY, PT_STRING8):
        body = props.string(PR_BODY, max_bytes=_MAX_TEXT_BYTES)
    fields["body"] = body
    need_html = not (body and body.strip())
    if need_html or any_cid:
        fields["html"] = _read_html(props)
        if fields["html"]:
            fields["html_cids"] = find_cids(fields["html"])
    if need_html and not (fields["html"] and fields["html"].strip()):
        raw = props.binary(PR_RTF_COMPRESSED, max_bytes=_MAX_RTF_BYTES)
        if raw:
            try:
                fields["rtf"] = rtf_to_html_or_text(decompress_rtf(raw), props.codepage)
            except MsgFileError:
                fields["rtf"] = None
    return fields


def _read_attachment_data(cf, entry):
    """The bytes of an attachment's data stream, or None if it can't be read
    (a damaged attachment never stops the email being read)."""
    try:
        return cf.read(entry, max_bytes=entry.size)
    except MsgFileError:
        return None


def _read_meeting(cf, props, fields):
    """Fill meeting_start / meeting_end / meeting_location from the properties."""
    ids = named_property_ids(cf)

    def named(lid):
        return ids.get((PSETID_APPOINTMENT, lid))

    start_id = named(LID_APPOINTMENT_START_WHOLE)
    end_id = named(LID_APPOINTMENT_END_WHOLE)
    location_id = named(LID_LOCATION)
    start = props.time(start_id) if start_id is not None else None
    end = props.time(end_id) if end_id is not None else None
    fields["meeting_start"] = start or props.time(PR_START_DATE)
    fields["meeting_end"] = end or props.time(PR_END_DATE)
    if location_id is not None:
        fields["meeting_location"] = props.string(location_id, max_bytes=4096).strip()


def named_property_ids(cf):
    """{(property set uuid, numeric name): property id} from __nameid_version1.0.

    Outlook stores "named" properties (most meeting details) under ids from
    0x8000 up that differ from file to file; this table says which is which
    ([MS-OXMSG] 2.2.3). Properties named by a string are left out. Returns {}
    if there is no table; a damaged one raises MsgFileError.
    """
    storage = cf.children(cf.root).get("__NAMEID_VERSION1.0")
    if storage is None or not storage.is_storage():
        return {}
    streams = cf.children(storage)
    guid_entry = streams.get("__SUBSTG1.0_00020102")
    names_entry = streams.get("__SUBSTG1.0_00030102")
    if guid_entry is None or names_entry is None:
        return {}
    if not (guid_entry.is_stream() and names_entry.is_stream()):
        return {}
    guids = cf.read(guid_entry, max_bytes=1024 * 1024)
    entries = cf.read(names_entry, max_bytes=4 * 1024 * 1024)
    ids = {}
    for off in range(0, len(entries) - 7, 8):
        lid, info = struct.unpack_from("<II", entries, off)
        if info & 1:
            continue  # named by a string, not a number
        guid_index = (info >> 1) & 0x7FFF
        if guid_index < 3:
            continue  # 1 = PS_MAPI, 2 = PS_PUBLIC_STRINGS: not used by Squish
        start = (guid_index - 3) * 16
        if start + 16 > len(guids):
            continue
        guid = uuid.UUID(bytes_le=bytes(guids[start:start + 16]))
        ids[(guid, lid)] = 0x8000 + (info >> 16)
    return ids


def _read_attached_message(cf, storage, codepage):
    """The fields of an email attached to the filed email, or None if it can't
    be read. A damaged attached email never stops the filed email being read."""
    try:
        return _read_message(cf, storage, top_level=False, parent_codepage=codepage, depth=1)
    except Exception:
        return None


def _embedded_subject(cf, storage, codepage):
    """Subject of an attached (embedded) message, read cheaply."""
    try:
        inner = _PropertyBag(cf, storage, 24)
        if inner.codepage is None and codepage:
            inner.codec = codec_for_codepage(codepage)
        return inner.string(PR_SUBJECT).strip()
    except MsgFileError:
        return ""


def _read_html(props):
    """PR_HTML is normally binary, sometimes text. Returns str or None."""
    data = props.binary(PR_HTML, max_bytes=_MAX_TEXT_BYTES)
    if data is not None:
        return decode_html_bytes(data, props.integer(PR_INTERNET_CPID))
    if props.has(PR_HTML, PT_UNICODE) or props.has(PR_HTML, PT_STRING8):
        return props.string(PR_HTML, max_bytes=_MAX_TEXT_BYTES)
    return None


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-:.]+)""", re.I)
_CID_RE = re.compile(r"""cid:([^"'\s>)]+)""", re.I)


def find_cids(html_text):
    """Lower-case content ids referenced as ``cid:...`` in HTML."""
    if not html_text:
        return set()
    return set(m.group(1).strip().lower() for m in _CID_RE.finditer(html_text))


def _normalise_charset(name):
    name = (name or "").strip().lower()
    if name in ("iso-8859-1", "iso8859-1", "latin1", "latin-1", "us-ascii", "ascii",
                "windows-1252", "cp1252"):
        return "cp1252"
    if name in ("utf8", "unicode-1-1-utf-8"):
        return "utf-8"
    if name in ("unicode", "utf-16"):
        return "utf-16"
    try:
        return codecs.lookup(name).name
    except LookupError:
        return ""


def decode_html_bytes(data, codepage=None):
    """Decode HTML bytes: BOM, then <meta charset>, then code page, UTF-8, Windows-1252."""
    if data is None:
        return None
    if isinstance(data, str):
        return data.replace("\x00", "")
    if data.startswith(codecs.BOM_UTF8):
        return data[3:].decode("utf-8", "replace")
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        return data.decode("utf-16", "replace").replace("\x00", "")
    candidates = []
    m = _META_CHARSET.search(data[:8192])
    if m:
        candidates.append(_normalise_charset(m.group(1).decode("ascii", "replace")))
    if codepage:
        candidates.append(codec_for_codepage(codepage, default=""))
    candidates.extend(["utf-8", "cp1252"])
    for name in candidates:
        if not name or name.startswith("utf-16"):
            continue
        try:
            return data.decode(name).replace("\x00", "")
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("cp1252", "replace").replace("\x00", "")


# --------------------------------------------------------------------------
# Compressed RTF ([MS-OXRTFCP]) and RTF -> HTML / text ([MS-OXRTFEX])
# --------------------------------------------------------------------------

_LZFU_PREBUF = (
    b"{\\rtf1\\ansi\\mac\\deff0\\deftab720{\\fonttbl;}{\\f0\\fnil \\froman "
    b"\\fswiss \\fmodern \\fscript \\fdecor MS Sans SerifSymbolArialTimes New "
    b"RomanCourier{\\colortbl\\red0\\green0\\blue0\r\n\\par "
    b"\\pard\\plain\\f0\\fs20\\b\\i\\u\\tab\\tx"
)
_COMPRESSED = 0x75465A4C    # "LZFu"
_UNCOMPRESSED = 0x414C454D  # "MELA"


def decompress_rtf(data, max_size=_MAX_RTF_BYTES):
    """Decompress a PR_RTF_COMPRESSED stream (LZFu or uncompressed MELA)."""
    if len(data) < 16:
        raise MsgFileError("compressed RTF is too short")
    comp_size, raw_size, comp_type, _crc = struct.unpack_from("<IIII", data, 0)
    if raw_size > max_size:
        raise MsgFileError("compressed RTF claims an unreasonable size")
    if comp_type == _UNCOMPRESSED:
        return bytes(data[16:16 + raw_size])
    if comp_type != _COMPRESSED:
        raise MsgFileError("unknown compressed RTF format")
    dictionary = bytearray(4096)
    dictionary[:len(_LZFU_PREBUF)] = _LZFU_PREBUF
    write = len(_LZFU_PREBUF)
    out = bytearray()
    end = min(len(data), comp_size + 4)
    pos = 16
    while pos < end and len(out) < raw_size:
        control = data[pos]
        pos += 1
        for bit in range(8):
            if pos >= end:
                break
            if control & (1 << bit):
                if pos + 1 >= end:
                    pos = end
                    break
                ref = (data[pos] << 8) | data[pos + 1]
                pos += 2
                offset = ref >> 4
                length = (ref & 0x0F) + 2
                if offset == write:
                    return bytes(out[:raw_size])  # end-of-data marker
                for i in range(length):
                    byte = dictionary[(offset + i) & 0xFFF]
                    out.append(byte)
                    dictionary[write] = byte
                    write = (write + 1) & 0xFFF
            else:
                byte = data[pos]
                pos += 1
                out.append(byte)
                dictionary[write] = byte
                write = (write + 1) & 0xFFF
    return bytes(out[:raw_size])


_RTF_TOKEN = re.compile(
    rb"\\([a-zA-Z]{1,32})(-?[0-9]{1,10})? ?"   # 1, 2: control word + number
    rb"|\\'([0-9a-fA-F]{2})"                    # 3: hex byte
    rb"|\\([^a-zA-Z'])"                          # 4: control symbol
    rb"|([{}])"                                  # 5: group
    rb"|[\r\n]+"                                 # ignored line breaks
    rb"|([^\\{}\r\n]+)",                         # 6: text
    re.S,
)

_RTF_SKIP_DESTINATIONS = set([
    "fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "header",
    "footer", "headerl", "headerr", "footerl", "footerr", "listtable",
    "listoverridetable", "rsidtbl", "generator", "xmlnstbl", "fldinst",
    "themedata", "colorschememapping", "latentstyles", "datastore", "filetbl",
    "revtbl", "mmathpr", "pgdsctbl", "bkmkstart", "bkmkend", "nonshppict",
    "mhtmltag", "htmlbase", "objdata", "picprop", "shpinst", "userprops",
])

_RTF_SYMBOLS = {
    "par": "\n", "line": "\n", "sect": "\n", "page": "\n", "row": "\n",
    "tab": "\t", "cell": " | ", "emdash": "—", "endash": "–",
    "lquote": "‘", "rquote": "’", "ldblquote": "“",
    "rdblquote": "”", "bullet": "•", "emspace": " ", "enspace": " ",
    "qmspace": " ",
}


# Font charset (\fcharsetN) -> Windows code page.
_RTF_CHARSET_CODEPAGES = {
    77: 10000, 128: 932, 129: 949, 130: 1361, 134: 936, 136: 950, 161: 1253,
    162: 1254, 163: 1258, 177: 1255, 178: 1256, 186: 1257, 204: 1251,
    222: 874, 238: 1250, 255: 437,
}


class _RtfGroup(object):
    """Settings that RTF scopes to a {group}."""

    __slots__ = ("skip", "htmlrtf", "htmltag", "uc", "font", "fonttbl")

    def __init__(self):
        self.skip = False      # inside a destination we don't want (font table, pictures...)
        self.htmlrtf = False   # \htmlrtf: RTF-only content of HTML-wrapping RTF
        self.htmltag = False   # {\*\htmltagN ...}: original HTML markup
        self.uc = 1            # fallback characters after each \uN
        self.font = None       # current font number (\fN)
        self.fonttbl = False   # inside the font table

    def copy(self):
        other = _RtfGroup()
        other.skip = self.skip
        other.htmlrtf = self.htmlrtf
        other.htmltag = self.htmltag
        other.uc = self.uc
        other.font = self.font
        other.fonttbl = self.fonttbl
        return other


def rtf_to_html_or_text(rtf, codepage=None):
    """Turn decompressed RTF into ("html", text) if it wraps HTML, else ("text", text).

    Best effort: enough to recover readable text from emails that have no
    other body. Formatting is dropped.
    """
    if isinstance(rtf, str):
        rtf = rtf.encode("latin-1", "replace")
    head = rtf[:2048]
    html_mode = b"\\fromhtml" in head
    m = re.search(rb"\\ansicpg([0-9]{1,5})", head)
    default_codec = codec_for_codepage(eight_bit_codepage(int(m.group(1)) if m else codepage))

    out = []
    pending = bytearray()   # 8-bit text waiting to be decoded
    font_codecs = {}        # font number -> codec, from the font table
    table_font = [None]     # font being defined in the font table
    state = _RtfGroup()
    stack = []
    skip_chars = 0          # fallback characters to skip after \uN
    expect_destination = False  # just saw "\*"
    group_start = False     # just saw "{"

    def visible():
        if state.skip:
            return False
        if state.htmltag:
            return True
        return not (html_mode and state.htmlrtf)

    def flush():
        if pending:
            codec = font_codecs.get(state.font, default_codec)
            out.append(pending.decode(codec, "replace"))
            del pending[:]

    for m in _RTF_TOKEN.finditer(rtf):
        word, num, hexbyte, symbol, brace, text = m.groups()
        if word is None and hexbyte is None and symbol is None and brace is None and text is None:
            continue  # a raw line break: ignored in RTF
        if brace is not None:
            flush()
            if brace == b"{":
                if len(stack) > 1000:
                    raise MsgFileError("RTF groups are nested too deeply")
                stack.append(state)
                state = state.copy()
                group_start = True
                expect_destination = False
                skip_chars = 0
                continue
            if stack:
                state = stack.pop()
            group_start = False
            expect_destination = False
            continue

        if word is not None:
            word = word.decode("ascii").lower()
            was_group_start = group_start
            group_start = False
            if state.fonttbl:
                # Font table entries look like {\f1\fnil\fcharset134 SimSun;}
                if word == "f" and num is not None:
                    table_font[0] = int(num)
                elif word == "fcharset" and num is not None and table_font[0] is not None:
                    cp = _RTF_CHARSET_CODEPAGES.get(int(num))
                    if cp:
                        font_codecs[table_font[0]] = codec_for_codepage(cp, default_codec)
                elif word == "cpg" and num is not None and table_font[0] is not None:
                    font_codecs[table_font[0]] = codec_for_codepage(int(num), default_codec)
                continue
            if expect_destination or was_group_start:
                expect_destination_now = expect_destination
                expect_destination = False
                if word.startswith("htmltag"):
                    flush()
                    state.htmltag = True
                    state.skip = False
                    continue
                if word == "fonttbl":
                    flush()
                    state.skip = True
                    state.fonttbl = True
                    continue
                if word in _RTF_SKIP_DESTINATIONS or expect_destination_now:
                    flush()
                    state.skip = True
                    continue
            if word == "f" and num is not None:
                flush()
                state.font = int(num)
                continue
            if word == "htmlrtf":
                flush()
                state.htmlrtf = (num is None or num != b"0")
                continue
            if word == "u" and num is not None:
                flush()
                code = int(num)
                if code < 0:
                    code += 65536
                if visible():
                    try:
                        out.append(chr(code))
                    except (ValueError, OverflowError):
                        pass  # damaged RTF: a character number that doesn't exist
                skip_chars = state.uc
                continue
            if word == "uc" and num is not None:
                state.uc = max(0, int(num))
                continue
            sym = _RTF_SYMBOLS.get(word)
            if sym is not None:
                flush()
                if visible() and not (html_mode and not state.htmltag and sym == " | "):
                    out.append(sym)
                continue
            continue  # any other control word: formatting, ignore

        group_start = False
        if hexbyte is not None:
            if skip_chars:
                skip_chars -= 1
                continue
            if visible():
                pending.append(int(hexbyte, 16))
            continue
        if symbol is not None:
            if symbol == b"*":
                expect_destination = True
                continue
            if skip_chars:
                skip_chars -= 1
                continue
            if not visible():
                continue
            if symbol in (b"\\", b"{", b"}"):
                pending.extend(symbol)
            elif symbol == b"~":
                flush()
                out.append(" ")
            elif symbol == b"_":
                pending.extend(b"-")
            elif symbol in (b"\n", b"\r"):
                flush()
                out.append("\n")
            continue
        if text is not None:
            if skip_chars:
                n = min(skip_chars, len(text))
                text = text[n:]
                skip_chars -= n
            if text and visible():
                pending.extend(text)
            continue
    flush()
    result = "".join(out).replace("\x00", "")
    # Characters beyond U+FFFF (emoji) arrive as two \uN halves: join them, and
    # turn any unpaired half into U+FFFD so the text can always be saved.
    result = result.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return ("html" if html_mode else "text", result)


# --------------------------------------------------------------------------
# Small helpers shared with readers.py
# --------------------------------------------------------------------------

def decode_header_text(value):
    """Decode RFC 2047 encoded words in a header value; never raises."""
    if not value:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(value)))
    except Exception:
        return value
