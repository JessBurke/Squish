"""Test helper: write small PDF files in memory. Standard library only.

Enough of the PDF format (ISO 32000) to test the built-in PDF reader:
pages of any size, text in the standard fonts (WinAnsi encoding), a
Type0 / Identity-H font with a /ToUnicode map, compressed content streams,
PDF 1.5 cross-reference streams and object streams, incremental updates,
damaged cross-reference tables, an "encrypted" marker and real encryption
(RC4 40/128-bit, AES-128, AES-256, with an owner password and an empty or
real user password). All content is synthetic.

Quick use::

    data = simple_pdf(["Hello World", "Second page"])

or, for full control::

    w = PdfWriter()
    font = w.add(standard_font("Helvetica"))
    page = page_dict(w, text_content(["Hello"]), {"F1": font}, size=A4)
    data = w.build(w.add(catalog(w, [page])))
"""

import base64
import hashlib
import struct
import zlib

A4 = (595.28, 841.89)
A3 = (841.89, 1190.55)
A1_LANDSCAPE = (2383.94, 1683.78)
LETTER = (612, 792)


class Name(str):
    """A PDF name, written /Like#20This."""


class Raw(bytes):
    """Bytes written into the file exactly as given (e.g. a hex string)."""


class Ref:
    """A reference to an indirect object: written "12 0 R"."""

    def __init__(self, num):
        self.num = num

    def __repr__(self):
        return "Ref(%d)" % self.num


class Stream:
    """A stream object. ``filters`` are applied when the file is written
    (e.g. ["ASCII85Decode", "FlateDecode"] means compress, then ASCII85)."""

    def __init__(self, data, entries=None, filters=None, parms=None, length=None):
        self.data = data
        self.entries = dict(entries or {})
        self.filters = list(filters or [])
        self.parms = parms
        self.length = length          # write a wrong /Length on purpose

    def encoded(self):
        data = self.data
        for name in reversed(self.filters):
            data = ENCODERS[name](data)
        return data


# --------------------------------------------------------------------------
# Encoders for stream filters
# --------------------------------------------------------------------------

def flate(data):
    return zlib.compress(data)


def ascii_hex(data):
    return data.hex().encode("ascii") + b">"


def ascii85(data):
    return base64.a85encode(data) + b"~>"


def run_length(data):
    """RunLengthDecode encoder: runs of 3+ equal bytes, literal pieces otherwise."""
    out = bytearray()
    i = 0
    while i < len(data):
        run = 1
        while i + run < len(data) and data[i + run] == data[i] and run < 128:
            run += 1
        if run >= 3:
            out += bytes((257 - run, data[i]))
            i += run
            continue
        start = i
        while i < len(data) and i - start < 128:
            if i + 2 < len(data) and data[i] == data[i + 1] == data[i + 2]:
                break
            i += 1
        out += bytes((i - start - 1,)) + data[start:i]
    return bytes(out) + b"\x80"


def lzw(data, early_change=1):
    """LZWDecode encoder (starts with a clear code, clears again when the table is full, ends with EOD)."""
    table = {bytes((i,)): i for i in range(256)}
    next_code = 258
    width = 9
    acc = 0
    nbits = 0
    out = bytearray()

    def put(code, size):
        nonlocal acc, nbits
        acc = (acc << size) | code
        nbits += size
        while nbits >= 8:
            nbits -= 8
            out.append((acc >> nbits) & 0xFF)

    put(256, width)
    word = b""
    for byte in data:
        grown = word + bytes((byte,))
        if grown in table:
            word = grown
            continue
        put(table[word], width)
        table[grown] = next_code
        next_code += 1
        size = next_code - 1 + early_change
        width = 9 if size < 512 else 10 if size < 1024 else 11 if size < 2048 else 12
        word = bytes((byte,))
        if next_code >= 4093:                 # table full: start again
            put(256, width)
            table = {bytes((i,)): i for i in range(256)}
            next_code = 258
            width = 9
    if word:
        put(table[word], width)
        next_code += 1
        size = next_code - 1 + early_change
        width = 9 if size < 512 else 10 if size < 1024 else 11 if size < 2048 else 12
    put(257, width)
    if nbits:
        out.append((acc << (8 - nbits)) & 0xFF)
    return bytes(out)


ENCODERS = {"FlateDecode": flate, "ASCIIHexDecode": ascii_hex,
            "ASCII85Decode": ascii85, "RunLengthDecode": run_length,
            "LZWDecode": lzw}


# --------------------------------------------------------------------------
# Writing values
# --------------------------------------------------------------------------

def literal(data):
    """bytes -> PDF literal string "(...)" with escapes."""
    out = bytearray(b"(")
    for byte in data:
        if byte in b"()\\":
            out += b"\\" + bytes((byte,))
        elif byte < 32 or byte > 126:
            out += b"\\%03o" % byte
        else:
            out.append(byte)
    return bytes(out + b")")


def win_ansi(text):
    """Text -> literal string in WinAnsi (Windows-1252), for the standard fonts."""
    return Raw(literal(text.encode("cp1252")))


def text_string(text):
    """A document text string (title...): PDFDocEncoding if ASCII, else UTF-16BE with a BOM."""
    try:
        return Raw(literal(text.encode("ascii")))
    except UnicodeEncodeError:
        return Raw(b"<feff" + text.encode("utf-16-be").hex().encode("ascii") + b">")


def name_bytes(name):
    out = bytearray(b"/")
    for byte in name.encode("utf-8"):
        if byte <= 32 or byte >= 127 or byte in b"()<>[]{}/%#":
            out += b"#%02X" % byte
        else:
            out.append(byte)
    return bytes(out)


def number_bytes(value):
    if isinstance(value, int):
        return str(value).encode("ascii")
    text = ("%.4f" % value).rstrip("0").rstrip(".")
    return (text if text not in ("", "-", "-0") else "0").encode("ascii")


def serialize(value):
    """Python value -> PDF syntax."""
    if isinstance(value, Raw):
        return bytes(value)
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if value is None:
        return b"null"
    if isinstance(value, Ref):
        return b"%d 0 R" % value.num
    if isinstance(value, Name):
        return name_bytes(value)
    if isinstance(value, (int, float)):
        return number_bytes(value)
    if isinstance(value, str):
        return text_string(value)
    if isinstance(value, bytes):
        return literal(value)
    if isinstance(value, (list, tuple)):
        return b"[" + b" ".join(serialize(v) for v in value) + b"]"
    if isinstance(value, dict):
        parts = [name_bytes(k) + b" " + serialize(v) for k, v in value.items()]
        return b"<<" + b" ".join(parts) + b">>"
    raise TypeError("cannot write %r" % (value,))


def stream_bytes(stream, crypt=None):
    """A stream object's bytes; ``crypt(data)`` encrypts the encoded data."""
    data = stream.encoded()
    if crypt is not None:
        data = crypt(data)
    entries = dict(stream.entries)
    if stream.filters:
        names = [Name(f) for f in stream.filters]
        entries["Filter"] = names[0] if len(names) == 1 else names
    if stream.parms is not None:
        entries["DecodeParms"] = stream.parms
    entries["Length"] = len(data) if stream.length is None else stream.length
    return serialize(entries) + b"\nstream\n" + data + b"\nendstream"


# --------------------------------------------------------------------------
# The file
# --------------------------------------------------------------------------

class PdfWriter:
    """Collects numbered objects and writes them out as a PDF file."""

    def __init__(self, version="1.4"):
        self.version = version
        self.objects = {}
        self.next_num = 1

    def reserve(self):
        ref = Ref(self.next_num)
        self.next_num += 1
        return ref

    def add(self, value):
        ref = self.reserve()
        self.objects[ref.num] = value
        return ref

    def set(self, ref, value):
        self.objects[ref.num] = value

    def build(self, root, info=None, xref_stream=False, object_streams=False,
              encrypt=False, prefix=b"", break_xref=None, predictor=False,
              trailer_extra=None, hybrid=False, user_password=b"", owner_password=b"owner",
              encrypt_metadata=True):
        """Write the whole file.

        root: Ref of the catalog. xref_stream: PDF 1.5 cross-reference stream
        (always used with object_streams). encrypt: True adds an /Encrypt
        dictionary but does NOT really encrypt (a marker whose password check
        fails); "rc4-40", "rc4-128", "rc4-128-v4" (crypt filters), "aes-128",
        "aes-256" (revision 6) or "aes-256-r5" really encrypt every string and
        stream, with ``owner_password`` and ``user_password`` (empty: the file
        opens without one). prefix: junk bytes
        before the "%PDF" header. break_xref: "offsets" (wrong offsets),
        "missing" (no xref table, trailer or startxref), "startxref"
        (startxref points nowhere). predictor: PNG predictor on the xref
        stream. hybrid: a "hybrid-reference" file: object streams, listed in
        an xref stream named by /XRefStm, plus a classic table in which those
        objects appear as free.
        """
        out = bytearray(prefix + b"%PDF-" + self.version.encode() + b"\n%\xe2\xe3\xcf\xd3\n")
        trailer = {"Root": root}
        if info is not None:
            trailer["Info"] = info
        crypt = None
        if isinstance(encrypt, str):
            crypt = Encryptor(encrypt, user_password, owner_password, encrypt_metadata)
            trailer["Encrypt"] = self.add(crypt.dictionary())
            trailer["ID"] = [Raw(b"<" + crypt.file_id.hex().encode() + b">")] * 2
        elif encrypt:
            trailer["Encrypt"] = self.add({
                "Filter": Name("Standard"), "V": 1, "R": 2, "P": -44,
                "O": Raw(b"<" + b"ab" * 32 + b">"), "U": Raw(b"<" + b"cd" * 32 + b">")})
            trailer["ID"] = [Raw(b"<0123456789abcdef0123456789abcdef>")] * 2
        if trailer_extra:
            trailer.update(trailer_extra)
        offsets = {}
        packed = {}
        if object_streams or hybrid:
            xref_stream = not hybrid
            loose = [num for num, value in sorted(self.objects.items())
                     if not isinstance(value, Stream) and not (encrypt and num == trailer["Encrypt"].num)]
            stm_num = self.next_num
            self.next_num += 1
            header = []
            body = bytearray()
            for index, num in enumerate(loose):
                header.append(b"%d %d" % (num, len(body)))
                body += serialize(self.objects[num]) + b"\n"
                packed[num] = (stm_num, index)
            first = len(b" ".join(header)) + 1
            content = b" ".join(header) + b"\n" + bytes(body)
            stm = Stream(content, {"Type": Name("ObjStm"), "N": len(loose), "First": first},
                         filters=["FlateDecode"])
            objects = dict((n, v) for n, v in self.objects.items() if n not in packed)
            objects[stm_num] = stm
        else:
            objects = dict(self.objects)
        for num in sorted(objects):
            offsets[num] = len(out)
            value = objects[num]
            if crypt is not None and num != trailer["Encrypt"].num:
                body = crypt.object_bytes(num, value)
            else:
                body = stream_bytes(value) if isinstance(value, Stream) else serialize(value)
            out += b"%d 0 obj\n" % num + body + b"\nendobj\n"
        if break_xref == "missing":
            return bytes(out)
        size = max(list(offsets) + list(packed) + [0]) + 1
        shift = 37 if break_xref == "offsets" else 0
        if hybrid:
            hidden_num = size
            size += 1
            rows = b"".join(struct.pack(">BIH", 2, packed[num][0], packed[num][1]) if num in packed
                            else struct.pack(">BIH", 0, 0, 0) for num in range(size))
            hidden = Stream(rows, {"Type": Name("XRef"), "Size": size, "W": [1, 4, 2]}, filters=["FlateDecode"])
            offsets[hidden_num] = len(out)
            trailer["XRefStm"] = len(out)
            out += b"%d 0 obj\n" % hidden_num + stream_bytes(hidden) + b"\nendobj\n"
        if xref_stream:
            xref_num = size
            size += 1
            offsets[xref_num] = len(out)
            rows = bytearray()
            for num in range(size):
                if num in packed:
                    rows += struct.pack(">BIH", 2, packed[num][0], packed[num][1])
                elif num in offsets:
                    rows += struct.pack(">BIH", 1, offsets[num] + shift, 0)
                else:
                    rows += struct.pack(">BIH", 0, 0, 65535)
            entries = {"Type": Name("XRef"), "Size": size, "W": [1, 4, 2]}
            entries.update(trailer)
            parms = None
            if predictor:
                rows = _png_up(bytes(rows), 7)
                parms = {"Predictor": 12, "Columns": 7}
            xref = Stream(bytes(rows), entries, filters=["FlateDecode"], parms=parms)
            start = len(out)
            out += b"%d 0 obj\n" % xref_num + stream_bytes(xref) + b"\nendobj\n"
        elif hybrid:
            # Only the objects outside object streams are in the table (one
            # subsection per run of numbers); the others are in /XRefStm.
            start = len(out)
            out += b"xref\n0 1\n0000000000 65535 f \n"
            nums = sorted(offsets)
            runs = []
            for num in nums:
                if runs and runs[-1][-1] == num - 1:
                    runs[-1].append(num)
                else:
                    runs.append([num])
            for run in runs:
                out += b"%d %d\n" % (run[0], len(run))
                for num in run:
                    out += b"%010d 00000 n \n" % (offsets[num] + shift)
            trailer["Size"] = size
            out += b"trailer\n" + serialize(trailer) + b"\n"
        else:
            start = len(out)
            out += b"xref\n0 %d\n0000000000 65535 f \n" % size
            for num in range(1, size):
                if num in offsets:
                    out += b"%010d 00000 n \n" % (offsets[num] + shift)
                else:
                    out += b"0000000000 65535 f \n"
            trailer["Size"] = size
            out += b"trailer\n" + serialize(trailer) + b"\n"
        if break_xref == "startxref":
            start = len(out) + 1000
        out += b"startxref\n%d\n%%%%EOF\n" % start
        return bytes(out)

    def update(self, base, changes, root, info=None):
        """Append an incremental update to ``base`` (bytes of a file built by
        this writer): ``changes`` maps Ref -> new value (new or replaced objects)."""
        prev = int(base[base.rindex(b"startxref") + 9:].split()[0])
        out = bytearray(base)
        offsets = {}
        for ref, value in changes.items():
            self.objects[ref.num] = value
            offsets[ref.num] = len(out)
            body = stream_bytes(value) if isinstance(value, Stream) else serialize(value)
            out += b"%d 0 obj\n" % ref.num + body + b"\nendobj\n"
        start = len(out)
        out += b"xref\n"
        for num in sorted(offsets):
            out += b"%d 1\n%010d 00000 n \n" % (num, offsets[num])
        trailer = {"Size": max(self.next_num, max(offsets) + 1), "Root": root, "Prev": prev}
        if info is not None:
            trailer["Info"] = info
        out += b"trailer\n" + serialize(trailer) + b"\nstartxref\n%d\n%%%%EOF\n" % start
        return bytes(out)


# --------------------------------------------------------------------------
# Real encryption (the standard security handler, ISO 32000-2 7.6)
# --------------------------------------------------------------------------

PASSWORD_PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")


def rc4(key, data):
    """RC4 (written out here so the tests do not rely on the reader's own copy)."""
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out.append(byte ^ s[(s[i] + s[j]) & 0xFF])
    return bytes(out)


def aes_cbc_encrypt(key, iv, data):
    """AES-CBC of whole blocks. The block cipher is the reader's (pdftext has
    its own FIPS-197 known-answer tests); everything around it is written
    here independently."""
    from squish_app import pdftext
    return pdftext._aes_cbc_encrypt(key, iv, data)


def _padded(password):
    return (password + PASSWORD_PAD)[:32]


def _hash_r6(password, salt, user_key=b""):
    """ISO 32000-2 algorithm 2.B (AES-256, revision 6)."""
    k = hashlib.sha256(password + salt + user_key).digest()
    i = 0
    while True:
        e = aes_cbc_encrypt(k[:16], k[16:32], (password + k + user_key) * 64)
        k = [hashlib.sha256, hashlib.sha384, hashlib.sha512][int.from_bytes(e[:16], "big") % 3](e).digest()
        i += 1
        if i >= 64 and e[-1] <= i - 32:
            return k[:32]


class Encryptor:
    """Encrypts a file's strings and streams like Acrobat's "restrict editing"."""

    KINDS = {"rc4-40": (1, 2, 5), "rc4-128": (2, 3, 16), "rc4-128-v4": (4, 4, 16),
             "aes-128": (4, 4, 16), "aes-256": (5, 6, 32), "aes-256-r5": (5, 5, 32)}

    def __init__(self, kind, user_password=b"", owner_password=b"owner", encrypt_metadata=True):
        self.kind = kind
        self.v, self.r, self.size = self.KINDS[kind]
        self.aes = kind.startswith("aes")
        self.encrypt_metadata = encrypt_metadata
        self.p = -3904                       # printing and copying not allowed
        self.file_id = hashlib.md5(kind.encode() + user_password + owner_password).digest()
        self.counter = 0
        if self.v == 5:
            self._aes256(user_password, owner_password)
        else:
            self._rc4_family(user_password, owner_password)

    def _rc4_family(self, user, owner):
        # Algorithm 3: the /O entry from the owner password.
        key = hashlib.md5(_padded(owner)).digest()
        if self.r >= 3:
            for _ in range(50):
                key = hashlib.md5(key[:self.size]).digest()
        key = key[:self.size]
        o = rc4(key, _padded(user))
        if self.r >= 3:
            for i in range(1, 20):
                o = rc4(bytes(b ^ i for b in key), o)
        self.o = o
        # Algorithm 2: the file key from the user password.
        digest = hashlib.md5(_padded(user) + o + struct.pack("<i", self.p) + self.file_id)
        if self.r >= 4 and not self.encrypt_metadata:
            digest.update(b"\xff" * 4)
        key = digest.digest()
        if self.r >= 3:
            for _ in range(50):
                key = hashlib.md5(key[:self.size]).digest()
        self.key = key[:self.size]
        # Algorithms 4 and 5: the /U entry.
        if self.r == 2:
            self.u = rc4(self.key, PASSWORD_PAD)
        else:
            u = rc4(self.key, hashlib.md5(PASSWORD_PAD + self.file_id).digest())
            for i in range(1, 20):
                u = rc4(bytes(b ^ i for b in self.key), u)
            self.u = u + bytes(16)

    def _aes256(self, user, owner):
        if self.r == 5:
            hash_ = lambda pw, salt, extra=b"": hashlib.sha256(pw + salt + extra).digest()
        else:
            hash_ = _hash_r6
        self.key = hashlib.sha256(b"file key" + self.file_id).digest()
        salts = hashlib.sha512(self.file_id).digest()
        self.u = hash_(user, salts[0:8]) + salts[0:8] + salts[8:16]
        self.ue = aes_cbc_encrypt(hash_(user, salts[8:16]), bytes(16), self.key)
        self.o = hash_(owner, salts[16:24], self.u) + salts[16:24] + salts[24:32]
        self.oe = aes_cbc_encrypt(hash_(owner, salts[24:32], self.u), bytes(16), self.key)
        perms = struct.pack("<i", self.p) + b"\xff\xff\xff\xff" + (b"T" if self.encrypt_metadata else b"F") + b"adb" + bytes(4)
        self.perms = aes_cbc_encrypt(self.key, bytes(16), perms)       # (one block: ECB)

    def dictionary(self):
        """The /Encrypt dictionary."""
        hexed = lambda data: Raw(b"<" + data.hex().encode() + b">")
        enc = {"Filter": Name("Standard"), "V": self.v, "R": self.r, "P": self.p,
               "O": hexed(self.o), "U": hexed(self.u)}
        if self.v == 2:
            enc["Length"] = self.size * 8
        if self.v >= 4:
            cfm = {"rc4-128-v4": "V2", "aes-128": "AESV2"}.get(self.kind, "AESV3")
            enc["CF"] = {"StdCF": {"CFM": Name(cfm), "Length": self.size, "AuthEvent": Name("DocOpen")}}
            enc["StmF"] = enc["StrF"] = Name("StdCF")
            enc["Length"] = self.size * 8
            if not self.encrypt_metadata:
                enc["EncryptMetadata"] = False
        if self.v == 5:
            enc["UE"], enc["OE"], enc["Perms"] = hexed(self.ue), hexed(self.oe), hexed(self.perms)
        return enc

    def encrypt(self, num, data):
        """Encrypt one string or stream of object ``num`` (generation 0)."""
        if self.v == 5:
            key = self.key
        else:
            key = hashlib.md5(self.key + struct.pack("<i", num)[:3] + b"\x00\x00"
                              + (b"sAlT" if self.aes else b"")).digest()[:min(self.size + 5, 16)]
        if not self.aes:
            return rc4(key, data)
        self.counter += 1
        iv = hashlib.md5(b"iv %d %d" % (num, self.counter)).digest()
        pad = 16 - len(data) % 16
        return iv + aes_cbc_encrypt(key, iv, data + bytes((pad,)) * pad)

    def value(self, num, value):
        """A value with its strings encrypted (written as hex strings)."""
        if isinstance(value, Raw):
            raw = bytes(value)
            if raw[:1] == b"(" or (raw[:1] == b"<" and raw[:2] != b"<<"):
                return Raw(b"<" + self.encrypt(num, _string_bytes(raw)).hex().encode() + b">")
            return value
        if isinstance(value, (Name, Ref)) or value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return self.value(num, text_string(value))
        if isinstance(value, bytes):
            return Raw(b"<" + self.encrypt(num, value).hex().encode() + b">")
        if isinstance(value, (list, tuple)):
            return [self.value(num, v) for v in value]
        if isinstance(value, dict):
            return dict((k, self.value(num, v)) for k, v in value.items())
        return value

    def object_bytes(self, num, value):
        """The body of object ``num``, encrypted."""
        if isinstance(value, Stream):
            if value.entries.get("Type") == "XRef":
                return stream_bytes(value)
            plain_metadata = value.entries.get("Type") == "Metadata" and not self.encrypt_metadata
            copy = Stream(value.data, self.value(num, value.entries), value.filters, value.parms, value.length)
            return stream_bytes(copy, None if plain_metadata else (lambda data: self.encrypt(num, data)))
        return serialize(self.value(num, value))


def _string_bytes(raw):
    """The bytes of a string written as "(...)" or "<...>"."""
    if raw[:1] == b"<":
        digits = bytes(c for c in raw[1:-1] if c not in b" \n\r\t")
        return bytes.fromhex(digits.decode() + ("0" if len(digits) % 2 else ""))
    out = bytearray()
    body = raw[1:-1]
    i = 0
    while i < len(body):
        c = body[i]
        if c == 0x5C and i + 1 < len(body):
            nxt = body[i + 1:i + 2]
            if nxt.isdigit():
                digits = body[i + 1:i + 4]
                n = 0
                while n < len(digits) and 0x30 <= digits[n] <= 0x37:
                    n += 1
                out.append(int(digits[:n], 8) & 0xFF)
                i += 1 + n
                continue
            out += {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}.get(nxt, nxt)
            i += 2
            continue
        out.append(c)
        i += 1
    return bytes(out)


def _png_up(data, columns):
    """PNG 'Up' predictor rows (filter byte 2), as used for xref streams."""
    out = bytearray()
    prev = bytes(columns)
    for i in range(0, len(data), columns):
        row = data[i:i + columns]
        out.append(2)
        out += bytes((b - a) & 0xFF for a, b in zip(prev, row))
        prev = row
    return bytes(out)


# --------------------------------------------------------------------------
# Building blocks: fonts, pages, content
# --------------------------------------------------------------------------

def standard_font(base="Helvetica", encoding="WinAnsiEncoding", differences=None, widths=False):
    """A simple font dictionary for one of the standard 14 fonts.

    differences: e.g. [1, "T", "e", "s", "t"] (numbers are codes, strings glyph names).
    widths: True writes Helvetica-like /Widths for codes 32-126.
    """
    font = {"Type": Name("Font"), "Subtype": Name("Type1"), "BaseFont": Name(base)}
    if differences is not None:
        diffs = [d if isinstance(d, int) else Name(d) for d in differences]
        font["Encoding"] = {"Type": Name("Encoding"), "Differences": diffs}
        if encoding:
            font["Encoding"]["BaseEncoding"] = Name(encoding)
    elif encoding:
        font["Encoding"] = Name(encoding)
    if widths:
        font["FirstChar"] = 32
        font["LastChar"] = 126
        font["Widths"] = [556] * 95
    return font


def identity_font(writer, text, base="TestSans", ligatures=None, to_unicode=True):
    """A Type0 font with Identity-H encoding covering the characters of ``text``.

    CIDs are given out from 3 upwards (like glyph ids in a real font). The
    /ToUnicode map uses bfrange for runs of consecutive characters, bfchar
    for the rest, and the array form of bfrange for ``ligatures`` (e.g.
    ["fi", "ffl"]), which get their own CIDs.

    Returns (font Ref, encode) where encode(text) gives the hex string to
    show; ligatures in the text are encoded as one CID each.
    """
    chars = sorted(set(text))
    cids = {}
    for i, char in enumerate(chars):
        cids[char] = 3 + i
    ligatures = list(ligatures or [])
    lig_start = 3 + len(chars) + 10
    for i, lig in enumerate(ligatures):
        cids[lig] = lig_start + i

    # bfrange for runs where CID and Unicode both go up by one; bfchar otherwise.
    ranges, singles = [], []
    run = [chars[0]] if chars else []
    for char in chars[1:]:
        if ord(char) == ord(run[-1]) + 1 and cids[char] == cids[run[-1]] + 1 and (ord(char) & 0xFF) != 0:
            run.append(char)
        else:
            (ranges if len(run) > 1 else singles).append(run)
            run = [char]
    if run:
        (ranges if len(run) > 1 else singles).append(run)
    lines = [b"/CIDInit /ProcSet findresource begin", b"12 dict begin", b"begincmap",
             b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
             b"/CMapName /Adobe-Identity-UCS def", b"/CMapType 2 def",
             b"1 begincodespacerange", b"<0000> <FFFF>", b"endcodespacerange"]

    def utf16(s):
        return b"<" + s.encode("utf-16-be").hex().upper().encode() + b">"

    if singles:
        lines.append(b"%d beginbfchar" % len(singles))
        for (char,) in singles:
            lines.append(b"<%04X> " % cids[char] + utf16(char))
        lines.append(b"endbfchar")
    if ranges or ligatures:
        lines.append(b"%d beginbfrange" % (len(ranges) + (1 if ligatures else 0)))
        for run in ranges:
            lines.append(b"<%04X> <%04X> " % (cids[run[0]], cids[run[-1]]) + utf16(run[0]))
        if ligatures:
            lines.append(b"<%04X> <%04X> [" % (lig_start, lig_start + len(ligatures) - 1)
                         + b" ".join(utf16(lig) for lig in ligatures) + b"]")
        lines.append(b"endbfrange")
    lines += [b"endcmap", b"CMapName currentdict /CMap defineresource pop", b"end", b"end"]
    cmap = writer.add(Stream(b"\n".join(lines) + b"\n", filters=["FlateDecode"]))
    descendant = writer.add({
        "Type": Name("Font"), "Subtype": Name("CIDFontType2"), "BaseFont": Name(base),
        "CIDSystemInfo": {"Registry": b"Adobe", "Ordering": b"Identity", "Supplement": 0},
        "DW": 600, "W": [3, [500] * (len(chars) + 20)],
        "FontDescriptor": writer.add({"Type": Name("FontDescriptor"), "FontName": Name(base),
                                      "Flags": 32, "FontBBox": [0, -200, 1000, 800],
                                      "ItalicAngle": 0, "Ascent": 800, "Descent": -200,
                                      "CapHeight": 700, "StemV": 80})})
    font = {"Type": Name("Font"), "Subtype": Name("Type0"), "BaseFont": Name(base),
            "Encoding": Name("Identity-H"), "DescendantFonts": [descendant]}
    if to_unicode:
        font["ToUnicode"] = cmap
    ref = writer.add(font)

    def encode(s):
        out = []
        i = 0
        while i < len(s):
            for lig in sorted(ligatures, key=len, reverse=True):
                if s.startswith(lig, i):
                    out.append(cids[lig])
                    i += len(lig)
                    break
            else:
                out.append(cids[s[i]])
                i += 1
        return Raw(b"<" + b"".join(b"%04X" % c for c in out) + b">")

    return ref, encode


def truetype_with_cmap(glyphs):
    """A minimal TrueType font program holding only a 'cmap' table (format 4).

    glyphs maps characters to glyph ids. Real fonts have outlines too; a PDF
    reader that maps glyph ids back to characters only needs the cmap.
    """
    codes = sorted((ord(char), gid) for char, gid in glyphs.items()) + [(0xFFFF, 0)]
    count = len(codes)
    ends = struct.pack(">%dH" % count, *[code for code, gid in codes])
    starts = ends
    deltas = struct.pack(">%dh" % count, *[((gid - code + 0x8000) & 0xFFFF) - 0x8000 if code != 0xFFFF else 1
                                          for code, gid in codes])
    offsets = bytes(2 * count)
    body = struct.pack(">HHHH", 2 * count, 0, 0, 0) + ends + b"\x00\x00" + starts + deltas + offsets
    subtable = struct.pack(">HHH", 4, 6 + len(body), 0) + body
    cmap = struct.pack(">HHHHI", 0, 1, 3, 1, 12) + subtable
    header = struct.pack(">IHHHH", 0x00010000, 1, 16, 0, 0)
    record = b"cmap" + struct.pack(">III", 0, 12 + 16, len(cmap))
    return header + record + cmap


def text_content(lines, font="F1", size=12, x=72, y=760, leading=None):
    """A content stream showing ``lines`` (str) one below the other, WinAnsi encoded."""
    leading = size * 1.2 if leading is None else leading
    out = [b"BT", b"/%s %s Tf" % (font.encode(), number_bytes(size)),
           b"%s TL" % number_bytes(leading),
           b"%s %s Td" % (number_bytes(x), number_bytes(y))]
    for i, line in enumerate(lines):
        if i:
            out.append(b"T*")
        out.append(serialize(win_ansi(line)) + b" Tj")
    out.append(b"ET")
    return b"\n".join(out) + b"\n"


def page_dict(writer, content, fonts=None, size=A4, compress=True, parent=None,
              xobjects=None, extra=None, filters=None):
    """Add a page's content stream; returns the page dictionary (not yet added)."""
    if filters is None:
        filters = ["FlateDecode"] if compress else []
    contents = writer.add(Stream(content, filters=filters))
    resources = {}
    if fonts:
        resources["Font"] = fonts
    if xobjects:
        resources["XObject"] = xobjects
    page = {"Type": Name("Page"), "MediaBox": [0, 0, size[0], size[1]],
            "Resources": resources, "Contents": contents}
    if parent is not None:
        page["Parent"] = parent
    if extra:
        page.update(extra)
    return page


def catalog(writer, pages, pages_extra=None):
    """Add a page tree holding ``pages`` (dicts or Refs) and a catalog; returns the catalog dict."""
    tree = writer.reserve()
    kids = []
    for page in pages:
        if isinstance(page, dict):
            page = dict(page)
            page["Parent"] = tree
            page = writer.add(page)
        kids.append(page)
    node = {"Type": Name("Pages"), "Kids": kids, "Count": len(kids)}
    if pages_extra:
        node.update(pages_extra)
    writer.set(tree, node)
    return {"Type": Name("Catalog"), "Pages": tree}


def simple_pdf(pages, title=None, font="Helvetica", size=A4, **build_options):
    """A PDF with one page per item of ``pages`` (str with "\\n" between lines).

    Text is 12 pt ``font`` in WinAnsi encoding. Extra keyword arguments go
    to PdfWriter.build (xref_stream, object_streams, encrypt...).
    """
    version = "1.5" if build_options.get("xref_stream") or build_options.get("object_streams") else "1.4"
    writer = PdfWriter(version)
    font_ref = writer.add(standard_font(font))
    page_dicts = []
    for text in pages:
        lines = text.split("\n")
        page_dicts.append(page_dict(writer, text_content(lines, y=size[1] - 72),
                                    {"F1": font_ref}, size=size))
    root = writer.add(catalog(writer, page_dicts))
    info = writer.add({"Title": title, "Producer": "squish tests"}) if title is not None else None
    return writer.build(root, info=info, **build_options)


def markup_pdf(body=("Shop drawing SD-104 Rev 0", "Base plate BP1 25 mm thick, 4 x M24 bolts.")):
    """A page sent back from review (as Bluebeam or Acrobat write it): a stamp
    drawn by its appearance, a sticky note, clouds, typed text without an
    appearance, a highlight copying the page text, a reply, plus annotations
    that hold no comment (a pop-up, a link, a review-status entry, a hidden note).
    All synthetic."""
    w = PdfWriter()
    font = w.add(standard_font("Helvetica"))
    stamp = w.add(Stream(b"BT /Helv 14 Tf 4 6 Td (REVISE AND RESUBMIT) Tj ET",
                         {"Type": Name("XObject"), "Subtype": Name("Form"), "BBox": [0, 0, 200, 30],
                          "Resources": {"Font": {"Helv": font}}}))
    note = w.add({"Type": Name("Annot"), "Subtype": Name("Text"), "Rect": [500, 700, 520, 720],
                  "Contents": b"Anchor bolt embedment to be 450 mm, not 300 mm"})

    def annot(subtype, contents=None, **extra):
        entry = {"Type": Name("Annot"), "Subtype": Name(subtype), "Rect": [100, 300, 300, 330]}
        if contents is not None:
            entry["Contents"] = contents
        entry.update(extra)
        return w.add(entry)
    annots = [
        note,
        annot("Square", b"Plate thickness to be 32 mm per calc C-07"),
        annot("Stamp", b"Revise and resubmit", AP={"N": stamp}),
        annot("FreeText", b"Grout 40 mm non-shrink", DA=b"/Helv 10 Tf 0 g"),
        annot("Highlight", body[1].encode("latin-1")),             # copies the page text
        annot("Popup", b"Anchor bolt embedment to be 450 mm, not 300 mm", Parent=note),
        annot("Link", b"Not a comment"),
        annot("Text", b"Accepted set by Sam Brown", IRT=note, StateModel=b"Review", State=b"Accepted"),
        annot("Text", b"Hidden reviewer note", F=2),
        annot("Polygon", text_string("Add shear key \u2013 see SK-02")),
        annot("Text", b"Agreed, revise", IRT=note),                  # a reply is a comment
        annot("Square", b"Plate thickness to be 32 mm per calc C-07"),   # the same comment twice
    ]
    lines = [b"BT /F1 12 Tf 72 %d Td " % (760 - 20 * i) + serialize(win_ansi(line)) + b" Tj ET"
             for i, line in enumerate(body)]
    page = page_dict(w, b"\n".join(lines), {"F1": font}, extra={"Annots": annots})
    return w.build(w.add(catalog(w, [page])))


MARKUP_COMMENTS = [
    "[comment: Anchor bolt embedment to be 450 mm, not 300 mm]",
    "[comment: Plate thickness to be 32 mm per calc C-07]",
    "[comment: Grout 40 mm non-shrink]",
    "[comment: Add shear key \u2013 see SK-02]",
    "[comment: Agreed, revise]"]
