"""Test helper: build small Outlook .msg files (OLE compound files) in memory.

Only what the tests need: a minimal [MS-CFB] writer (header, FAT, DIFAT,
mini FAT, mini stream, directory) and a builder for MAPI message trees
([MS-OXMSG]). All content is synthetic.
"""

import struct
import uuid
from datetime import datetime, timezone

ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF
FATSECT = 0xFFFFFFFD
DIFSECT = 0xFFFFFFFC
NOSTREAM = 0xFFFFFFFF
SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


# --------------------------------------------------------------------------
# Compound file writer
# --------------------------------------------------------------------------

def build_cfb(tree, sector_size=512, mini_cutoff=4096, return_layout=False):
    """Build a compound file from ``{name: bytes | {nested tree}}``.

    Returns the file bytes, or (bytes, layout) with ``return_layout=True``
    where layout has "fat_offsets" (file offset of each FAT sector) and
    "entries" (list of dicts with name/start/size per directory entry).
    """
    per_sector = sector_size // 4
    entries = [{"name": "Root Entry", "type": 5, "data": None, "kids": []}]

    def add(parent, subtree):
        for name, value in subtree.items():
            index = len(entries)
            if isinstance(value, dict):
                entries.append({"name": name, "type": 1, "data": None, "kids": []})
                entries[parent]["kids"].append(index)
                add(index, value)
            else:
                entries.append({"name": name, "type": 2, "data": bytes(value), "kids": []})
                entries[parent]["kids"].append(index)

    add(0, tree)

    # Sibling "trees": a right-leaning chain sorted the way CFB compares names.
    for e in entries:
        e["left"] = e["right"] = e["child"] = NOSTREAM
    for e in entries:
        kids = sorted(e["kids"], key=lambda i: (len(entries[i]["name"]), entries[i]["name"].upper()))
        if kids:
            e["child"] = kids[0]
            for a, b in zip(kids, kids[1:]):
                entries[a]["right"] = b

    sectors = []   # bytes of each regular sector
    fat = []       # FAT entry for each regular sector

    def allocate(data):
        """Store ``data`` in new consecutive sectors; return the first sector number."""
        if not data:
            return ENDOFCHAIN
        first = len(sectors)
        count = (len(data) + sector_size - 1) // sector_size
        for i in range(count):
            chunk = data[i * sector_size:(i + 1) * sector_size]
            sectors.append(chunk.ljust(sector_size, b"\0"))
            fat.append(first + i + 1 if i < count - 1 else ENDOFCHAIN)
        return first

    # Streams: small ones into the mini stream, big ones into regular sectors.
    mini = bytearray()
    minifat = []
    for e in entries:
        if e["type"] != 2:
            e["start"], e["size"] = (0 if e["type"] == 1 else ENDOFCHAIN), 0
            continue
        data = e["data"]
        e["size"] = len(data)
        if not data:
            e["start"] = ENDOFCHAIN
        elif len(data) < mini_cutoff:
            first = len(minifat)
            count = (len(data) + 63) // 64
            mini.extend(data.ljust(count * 64, b"\0"))
            for i in range(count):
                minifat.append(first + i + 1 if i < count - 1 else ENDOFCHAIN)
            e["start"] = first
        else:
            e["start"] = allocate(data)
    root = entries[0]
    root["start"] = allocate(bytes(mini)) if mini else ENDOFCHAIN
    root["size"] = len(mini)
    minifat_bytes = struct.pack("<%dI" % len(minifat), *minifat) if minifat else b""
    first_minifat = allocate(minifat_bytes) if minifat_bytes else ENDOFCHAIN
    n_minifat = (len(minifat_bytes) + sector_size - 1) // sector_size

    # Directory.
    directory = bytearray()
    for e in entries:
        name = e["name"].encode("utf-16-le")[:62]
        record = bytearray(128)
        record[0:len(name)] = name
        struct.pack_into("<HBB", record, 64, len(name) + 2, e["type"], 1)
        struct.pack_into("<III", record, 68, e["left"], e["right"], e["child"])
        struct.pack_into("<IQ", record, 116, e["start"] & 0xFFFFFFFF, e["size"])
        directory.extend(record)
    per_dir_sector = sector_size // 128
    while len(directory) // 128 % per_dir_sector:
        empty = bytearray(128)
        struct.pack_into("<III", empty, 68, NOSTREAM, NOSTREAM, NOSTREAM)
        directory.extend(empty)
    first_dir = allocate(bytes(directory))
    n_dir_sectors = len(directory) // sector_size

    # FAT and DIFAT sectors (they must also describe themselves).
    n_fat = n_difat = 0
    while True:
        total = len(sectors) + n_fat + n_difat
        need_fat = (total + per_sector - 1) // per_sector
        need_difat = max(0, (need_fat - 109 + per_sector - 2) // (per_sector - 1))
        if need_fat == n_fat and need_difat == n_difat:
            break
        n_fat, n_difat = need_fat, need_difat
    fat_start = len(sectors)
    fat_sector_numbers = list(range(fat_start, fat_start + n_fat))
    difat_sector_numbers = list(range(fat_start + n_fat, fat_start + n_fat + n_difat))
    fat.extend([FATSECT] * n_fat)
    fat.extend([DIFSECT] * n_difat)
    fat.extend([FREESECT] * (n_fat * per_sector - len(fat)))
    for i in range(n_fat):
        chunk = fat[i * per_sector:(i + 1) * per_sector]
        sectors.append(struct.pack("<%dI" % per_sector, *chunk))
    extra = fat_sector_numbers[109:]
    for i, number in enumerate(difat_sector_numbers):
        chunk = extra[i * (per_sector - 1):(i + 1) * (per_sector - 1)]
        chunk = chunk + [FREESECT] * (per_sector - 1 - len(chunk))
        nxt = difat_sector_numbers[i + 1] if i + 1 < len(difat_sector_numbers) else ENDOFCHAIN
        sectors.append(struct.pack("<%dI" % per_sector, *(chunk + [nxt])))

    # Header.
    header = bytearray(512)
    header[0:8] = SIGNATURE
    major = 4 if sector_size == 4096 else 3
    shift = 12 if sector_size == 4096 else 9
    struct.pack_into("<HHHHH", header, 24, 0x3E, major, 0xFFFE, shift, 6)
    struct.pack_into("<IIIIIIIII", header, 40,
                     n_dir_sectors if major == 4 else 0, n_fat, first_dir, 0, mini_cutoff,
                     first_minifat, n_minifat,
                     difat_sector_numbers[0] if difat_sector_numbers else ENDOFCHAIN, n_difat)
    head_difat = fat_sector_numbers[:109] + [FREESECT] * (109 - min(109, n_fat))
    struct.pack_into("<109I", header, 76, *head_difat)
    data = bytes(header).ljust(sector_size, b"\0") + b"".join(sectors)
    if not return_layout:
        return data
    layout = {
        "fat_offsets": [(n + 1) * sector_size for n in fat_sector_numbers],
        "entries": [{"name": e["name"], "start": e["start"], "size": e["size"]} for e in entries],
        "sector_size": sector_size,
    }
    return data, layout


# --------------------------------------------------------------------------
# MAPI message builder
# --------------------------------------------------------------------------

PT_LONG = 0x0003
PT_BOOLEAN = 0x000B
PT_STRING8 = 0x001E
PT_UNICODE = 0x001F
PT_SYSTIME = 0x0040
PT_BINARY = 0x0102

PSETID_APPOINTMENT = uuid.UUID("00062002-0000-0000-C000-000000000046")


def filetime(dt):
    """Aware datetime -> FILETIME int."""
    delta = dt - datetime(1601, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10000000 + delta.microseconds * 10


class PropertySet(object):
    """Collects the properties of one MAPI object (message, recipient, attachment)."""

    def __init__(self, unicode=True, codepage="cp1252"):
        self.unicode = unicode
        self.codepage = codepage
        self.streams = {}
        self.fixed = []     # (tag, flags, 8-byte value)

    def string(self, pid, text):
        if text is None:
            return
        if self.unicode:
            data, ptype = text.encode("utf-16-le"), PT_UNICODE
        else:
            data, ptype = text.encode(self.codepage), PT_STRING8
        self.streams["__substg1.0_%04X%04X" % (pid, ptype)] = data
        self.fixed.append(((pid << 16) | ptype, 6, struct.pack("<II", len(data) + 2, 0)))

    def binary(self, pid, data):
        if data is None:
            return
        self.streams["__substg1.0_%04X%04X" % (pid, PT_BINARY)] = data
        self.fixed.append(((pid << 16) | PT_BINARY, 6, struct.pack("<II", len(data), 0)))

    def long(self, pid, value):
        self.fixed.append(((pid << 16) | PT_LONG, 6, struct.pack("<II", value & 0xFFFFFFFF, 0)))

    def boolean(self, pid, value):
        self.fixed.append(((pid << 16) | PT_BOOLEAN, 6, struct.pack("<HHI", 1 if value else 0, 0, 0)))

    def time(self, pid, dt):
        self.fixed.append(((pid << 16) | PT_SYSTIME, 6, struct.pack("<Q", filetime(dt))))

    def tree(self, header):
        props = bytearray(header)
        for tag, flags, value in self.fixed:
            props.extend(struct.pack("<II", tag, flags) + value)
        result = dict(self.streams)
        result["__properties_version1.0"] = bytes(props)
        return result


def message_tree(subject="", body=None, html=None, rtf_compressed=None,
                 sender_name=None, sender_email=None, sender_smtp=None,
                 on_behalf_name=None, on_behalf_email=None, on_behalf_smtp=None,
                 recipients=(), attachments=(), submit_time=None, delivery_time=None,
                 creation_time=None, message_class="IPM.Note", headers=None,
                 message_id=None, in_reply_to=None, conversation_topic=None,
                 display_to=None, display_cc=None, unicode=True, codepage=None,
                 internet_cpid=None, message_flags=None, embedded=False,
                 start_date=None, end_date=None, named=(), text_codec=None):
    """A dict tree for build_cfb() describing one message.

    recipients: [(name, smtp, email_address, type)] (type 1 To, 2 Cc, 3 Bcc)
    attachments: [{"long_name", "short_name", "display_name", "data", "content_id",
                   "mime", "hidden", "flags", "size", "method", "embedded": message_tree(...)}]
                 "data" is the attachment's bytes, written as its data stream
                 (__substg1.0_37010102; 4096 bytes or more go in regular sectors;
                 None writes no data stream, as for a cloud attachment);
                 "method" is PR_ATTACH_METHOD (default 1, or 5 for "embedded"; 7 is
                 an Outlook cloud attachment, a link to a shared file);
                 "size" is PR_ATTACH_SIZE (left out unless given). file_attachment()
                 makes one the way Outlook does. An "embedded" message may have
                 attachments of its own.
    start_date / end_date: PR_START_DATE / PR_END_DATE (aware datetimes)
    named: [(property set uuid, numeric name, "time" | "string", value)] named
           properties; they get the ids 0x8000, 0x8001, ... in that order.
    text_codec: codec used for 8-bit strings (default "cp<codepage>", or cp1252),
           e.g. to label Windows-1252 text with a misleading code page.
    """
    codec = text_codec or ("cp1252" if codepage is None else ("cp%d" % codepage))
    p = PropertySet(unicode, codec)
    p.string(0x001A, message_class)
    p.string(0x0037, subject)
    p.string(0x1000, body)
    p.string(0x0C1A, sender_name)
    p.string(0x0C1F, sender_email)
    p.string(0x5D01, sender_smtp)
    p.string(0x0042, on_behalf_name)
    p.string(0x0065, on_behalf_email)
    p.string(0x5D02, on_behalf_smtp)
    p.string(0x007D, headers)
    p.string(0x1035, message_id)
    p.string(0x1042, in_reply_to)
    p.string(0x0070, conversation_topic if conversation_topic is not None else subject)
    p.string(0x0E04, display_to)
    p.string(0x0E03, display_cc)
    if html is not None:
        p.binary(0x1013, html if isinstance(html, bytes) else html.encode("utf-8"))
    if rtf_compressed is not None:
        p.binary(0x1009, rtf_compressed)
    if submit_time is not None:
        p.time(0x0039, submit_time)
    if delivery_time is not None:
        p.time(0x0E06, delivery_time)
    if creation_time is not None:
        p.time(0x3007, creation_time)
    if codepage is not None:
        p.long(0x3FFD, codepage)
    if internet_cpid is not None:
        p.long(0x3FDE, internet_cpid)
    if message_flags is not None:
        p.long(0x0E07, message_flags)
    if start_date is not None:
        p.time(0x0060, start_date)
    if end_date is not None:
        p.time(0x0061, end_date)
    guids, entries = [], b""
    for index, (guid, lid, kind, value) in enumerate(named):
        if guid not in guids:
            guids.append(guid)
        guid_index = 3 + guids.index(guid)
        entries += struct.pack("<II", lid, (index << 16) | (guid_index << 1))
        if kind == "time":
            p.time(0x8000 + index, value)
        else:
            p.string(0x8000 + index, value)
    p.long(0x340D, 0x00040000 if unicode else 0)   # STORE_UNICODE_OK

    tree = {}
    for i, (name, smtp, address, rtype) in enumerate(recipients):
        r = PropertySet(unicode, codec)
        r.string(0x3001, name)
        r.string(0x39FE, smtp)
        r.string(0x3003, address)
        r.string(0x3002, "EX" if (address or "").startswith("/") else "SMTP")
        r.long(0x0C15, rtype)
        r.long(0x3000, i)
        tree["__recip_version1.0_#%08X" % i] = r.tree(b"\0" * 8)
    for i, a in enumerate(attachments):
        at = PropertySet(unicode, codec)
        at.string(0x3707, a.get("long_name"))
        at.string(0x3704, a.get("short_name"))
        at.string(0x3001, a.get("display_name"))
        at.string(0x3712, a.get("content_id"))
        at.string(0x370E, a.get("mime"))
        if a.get("hidden") is not None:
            at.boolean(0x7FFE, a["hidden"])
        if a.get("flags") is not None:
            at.long(0x3714, a["flags"])
        inner = a.get("embedded")
        method = a.get("method")
        at.long(0x3705, method if method is not None else (5 if inner is not None else 1))
        if a.get("size") is not None:
            at.long(0x0E20, a["size"])
        if inner is None:
            at.binary(0x3701, a.get("data", b""))
        att_tree = at.tree(b"\0" * 8)
        if inner is not None:
            att_tree["__substg1.0_3701000D"] = inner
        tree["__attach_version1.0_#%08X" % i] = att_tree

    header = bytearray(24 if embedded else 32)
    struct.pack_into("<IIII", header, 8, len(recipients), len(attachments),
                     len(recipients), len(attachments))
    tree.update(p.tree(bytes(header)))
    if not embedded:
        tree["__nameid_version1.0"] = {
            "__substg1.0_00020102": b"".join(g.bytes_le for g in guids),
            "__substg1.0_00030102": entries,
            "__substg1.0_00040102": b"",
        }
    return tree


def file_attachment(name, data, mime=None, **extra):
    """An attachments entry for a file, as Outlook writes one: long and short
    (8.3) names, the display name, a MIME type and PR_ATTACH_SIZE, which is the
    size of the whole attachment object (a little more than the data)."""
    base, dot, ext = name.rpartition(".")
    short = (base or ext)[:6].upper().replace(" ", "_") + "~1" + ("." + ext[:3].upper() if dot else "")
    entry = {"long_name": name, "short_name": short, "display_name": name, "data": data,
             "mime": mime or "application/octet-stream", "size": len(data) + 312}
    entry.update(extra)
    return entry


def build_msg(**kwargs):
    """Bytes of a .msg file; keyword arguments as for message_tree()."""
    sector_size = kwargs.pop("sector_size", 512)
    return build_cfb(message_tree(**kwargs), sector_size=sector_size)
