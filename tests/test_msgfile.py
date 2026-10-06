"""Tests for the built-in .msg reader (msgfile.py). All files are synthetic."""

import io
import os
import struct
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from squish_app import msgfile
from tests import msg_builder as mb

UTC = timezone.utc
X500 = "/O=EXCHANGELABS/OU=EXCHANGE ADMINISTRATIVE GROUP/CN=RECIPIENTS/CN=ALEX.EXAMPLE"


def open_cfb(data):
    return msgfile.CompoundFile(io.BytesIO(data), len(data))


class CompoundFileTests(unittest.TestCase):

    def test_streams_in_mini_stream_and_regular_sectors(self):
        small = b"small stream data"
        big = bytes(range(256)) * 40          # 10,240 bytes -> regular sectors
        data = mb.build_cfb({"small": small, "big": big, "folder": {"inner": b"x" * 70}})
        cf = open_cfb(data)
        root = cf.children(cf.root)
        self.assertEqual(sorted(root), ["BIG", "FOLDER", "SMALL"])
        self.assertEqual(cf.read(root["SMALL"]), small)
        self.assertEqual(cf.read(root["BIG"]), big)
        inner = cf.children(root["FOLDER"])["INNER"]
        self.assertEqual(cf.read(inner), b"x" * 70)
        self.assertEqual(cf.read(root["BIG"], max_bytes=10), big[:10])

    def test_4096_byte_sectors(self):
        big = b"0123456789" * 1000
        data = mb.build_cfb({"a": b"tiny", "b": big}, sector_size=4096)
        cf = open_cfb(data)
        self.assertEqual(cf.sector_size, 4096)
        streams = cf.children(cf.root)
        self.assertEqual(cf.read(streams["A"]), b"tiny")
        self.assertEqual(cf.read(streams["B"]), big)

    def test_large_file_uses_extra_difat_sectors(self):
        # > 109 FAT sectors (512-byte sectors) forces DIFAT sectors after the header.
        payload = b"\x5a" * (110 * 128 * 512)
        data, layout = mb.build_cfb({"payload": payload, "note": b"hello"}, return_layout=True)
        self.assertGreater(len(layout["fat_offsets"]), 109)
        cf = open_cfb(data)
        streams = cf.children(cf.root)
        self.assertEqual(cf.read(streams["NOTE"]), b"hello")
        self.assertEqual(cf.read(streams["PAYLOAD"], max_bytes=64), payload[:64])
        self.assertEqual(len(cf.read(streams["PAYLOAD"])), len(payload))

    def test_not_an_ole_file(self):
        for junk in (b"", b"hello", b"PK\x03\x04" + b"\0" * 2000, os.urandom(4096)):
            with self.assertRaises(msgfile.MsgFileError):
                msgfile.read_msg_bytes(junk)

    def test_truncated_file_raises(self):
        data = mb.build_msg(subject="Truncated", body="b" * 9000)
        with self.assertRaises(msgfile.MsgFileError):
            msgfile.read_msg_bytes(data[:1500])

    def test_sector_chain_loop_raises_instead_of_hanging(self):
        big = b"y" * 5000   # ten 512-byte sectors in a chain
        data, layout = mb.build_cfb({"big": big}, return_layout=True)
        entry = [e for e in layout["entries"] if e["name"] == "big"][0]
        start = entry["start"]
        patched = bytearray(data)
        # Point the 3rd sector of the chain back at the first one.
        fat_offset = layout["fat_offsets"][0]
        struct.pack_into("<I", patched, fat_offset + 4 * (start + 2), start)
        cf = open_cfb(bytes(patched))
        stream = cf.children(cf.root)["BIG"]
        t0 = time.time()
        with self.assertRaises(msgfile.MsgFileError):
            cf.read(stream)
        self.assertLess(time.time() - t0, 2)

    def test_directory_sibling_cycle_terminates(self):
        data, layout = mb.build_cfb({"a": b"1", "b": b"2"}, return_layout=True)
        patched = bytearray(data)
        # Directory starts at the sector after the mini stream and mini FAT; find entry "b"
        # by scanning for its UTF-16 name and make its right sibling point at entry "a".
        name_b = "b".encode("utf-16-le") + b"\0\0"
        pos = patched.find(name_b + b"\0" * 60)
        self.assertGreater(pos, 0)
        a_sid = [i for i, e in enumerate(layout["entries"]) if e["name"] == "a"][0]
        struct.pack_into("<I", patched, pos + 72, a_sid)
        cf = open_cfb(bytes(patched))
        self.assertEqual(sorted(cf.children(cf.root)), ["A", "B"])

    def test_stream_bigger_than_file_is_rejected(self):
        data, layout = mb.build_cfb({"big": b"z" * 5000}, return_layout=True)
        patched = bytearray(data)
        pos = patched.find("big".encode("utf-16-le") + b"\0\0")
        struct.pack_into("<I", patched, pos + 120, 0x7FFFFFF0)
        cf = open_cfb(bytes(patched))
        with self.assertRaises(msgfile.MsgFileError):
            cf.read(cf.children(cf.root)["BIG"])


class MessageFieldTests(unittest.TestCase):

    def test_read_message_fields(self):
        sent = datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC)
        data = mb.build_msg(
            subject="RE: Footing inspection",
            body="Hi Sam,\r\nThe footings are ready for inspection.\r\n",
            sender_name="Alex Example", sender_email=X500, sender_smtp="alex@example.com",
            on_behalf_name="Alex Example",
            recipients=[("Sam Sample", "sam@example.org", "sam@example.org", 1),
                        ("Pat Person", None, "/O=EXCHANGELABS/CN=PAT", 2),
                        ("Hidden Copy", "bcc@example.net", "bcc@example.net", 3)],
            submit_time=sent, delivery_time=datetime(2025, 3, 4, 5, 6, 9, tzinfo=UTC),
            message_id="<m1@example.com>", in_reply_to="<m0@example.com>",
            conversation_topic="Footing inspection",
            headers="From: Alex Example <alex@example.com>\r\nAuto-Submitted: no\r\n")
        f = msgfile.read_msg_bytes(data)
        self.assertEqual(f["subject"], "RE: Footing inspection")
        self.assertEqual(f["conversation_topic"], "Footing inspection")
        self.assertEqual(f["item_class"], "IPM.Note")
        self.assertEqual(f["message_id"], "<m1@example.com>")
        self.assertEqual(f["in_reply_to"], "<m0@example.com>")
        self.assertIn("footings are ready", f["body"])
        self.assertEqual(f["sender_name"], "Alex Example")
        self.assertEqual(f["sender_addresses"][0], "alex@example.com")
        self.assertEqual(f["submit_time"], sent)
        self.assertEqual(f["delivery_time"], datetime(2025, 3, 4, 5, 6, 9, tzinfo=UTC))
        self.assertIn("Auto-Submitted: no", f["headers"])
        self.assertEqual([r["name"] for r in f["recipients"]], ["Sam Sample", "Pat Person", "Hidden Copy"])
        self.assertEqual([r["type"] for r in f["recipients"]], [1, 2, 3])
        self.assertEqual(f["recipients"][0]["addresses"][0], "sam@example.org")
        self.assertIsNone(f["html"])   # plain body present, no cids: HTML not needed

    def test_attachments_and_embedded_message(self):
        inner = mb.message_tree(subject="Original RFI 12", body="inner", embedded=True)
        data = mb.build_msg(
            subject="Attachments", body="see attached",
            html="<p>see <img src=\"cid:image001.png@01DA\"></p>",
            attachments=[
                {"long_name": "Calcs rev B.pdf", "short_name": "CALCSR~1.PDF",
                 "data": b"%PDF" + b"0" * 300, "mime": "application/pdf"},
                {"long_name": "image001.png", "data": b"\x89PNG....", "content_id": "image001.png@01DA",
                 "mime": "image/png", "hidden": True, "flags": 4, "size": 999},
                {"short_name": "SHORT.TXT", "data": b"abc"},
                {"display_name": "Forwarded", "embedded": inner},
            ])
        f = msgfile.read_msg_bytes(data)
        atts = f["attachments"]
        self.assertEqual([a["name"] for a in atts],
                         ["Calcs rev B.pdf", "image001.png", "SHORT.TXT", "Original RFI 12"])
        self.assertEqual(atts[0]["size"], 304)        # from the data stream size
        self.assertEqual(atts[1]["size"], 999)        # PR_ATTACH_SIZE wins
        self.assertTrue(atts[1]["hidden"])
        self.assertEqual(atts[1]["flags"], 4)
        self.assertEqual(atts[1]["content_id"], "image001.png@01DA")
        self.assertTrue(atts[3]["embedded"])
        self.assertEqual(atts[3]["method"], 5)
        self.assertIn("image001.png@01da", f["html_cids"])
        self.assertEqual(atts[3]["message"]["body"], "inner")
        self.assertIsNone(atts[0]["message"])

    def test_attached_email_is_read_one_level_deep(self):
        nested = mb.message_tree(subject="Older chain", body="older text", embedded=True)
        inner = mb.message_tree(
            subject="RE: Variation 3", body="Yes, approved - proceed with option B.",
            sender_name="Pat Client", sender_smtp="pat@client.example",
            recipients=[("Sam Sample", "sam@example.org", "sam@example.org", 1)],
            submit_time=datetime(2025, 5, 2, 3, 4, tzinfo=UTC), embedded=True,
            attachments=[{"display_name": "Older chain", "embedded": nested},
                         {"long_name": "markup.pdf", "data": b"%PDF"}])
        data = mb.build_msg(subject="FW: Variation 3", body="See attached.",
                            attachments=[{"display_name": "RE: X", "embedded": inner}])
        att = msgfile.read_msg_bytes(data)["attachments"][0]
        self.assertEqual(att["name"], "RE: Variation 3")   # named by its own subject
        message = att["message"]
        self.assertEqual(message["sender_name"], "Pat Client")
        self.assertEqual(message["sender_addresses"][0], "pat@client.example")
        self.assertEqual(message["submit_time"], datetime(2025, 5, 2, 3, 4, tzinfo=UTC))
        self.assertEqual(message["body"], "Yes, approved - proceed with option B.")
        self.assertEqual(message["recipients"][0]["name"], "Sam Sample")
        # Its own attached email gets its name only.
        self.assertEqual([a["name"] for a in message["attachments"]], ["Older chain", "markup.pdf"])
        self.assertEqual([a["message"] for a in message["attachments"]], [None, None])

    def test_unreadable_attached_email_keeps_its_name(self):
        inner = mb.message_tree(subject="Client reply", body="text", embedded=True)
        data = mb.build_msg(subject="FW", body="see attached",
                            attachments=[{"display_name": "Client reply", "embedded": inner}])
        real = msgfile._read_message

        def failing_inner(cf, storage, top_level=True, parent_codepage=None, depth=0):
            if depth:
                raise msgfile.MsgFileError("damaged attached email")
            return real(cf, storage, top_level, parent_codepage, depth)

        with mock.patch.object(msgfile, "_read_message", side_effect=failing_inner):
            f = msgfile.read_msg_bytes(data)
        self.assertEqual(f["body"], "see attached")
        self.assertEqual(f["attachments"][0]["name"], "Client reply")
        self.assertIsNone(f["attachments"][0]["message"])

    def test_html_only_message(self):
        data = mb.build_msg(subject="HTML only",
                            html=b"<html><head><meta charset=\"windows-1252\"></head>"
                                 b"<body><p>Caf\xe9 \x93quoted\x94</p></body></html>")
        f = msgfile.read_msg_bytes(data)
        self.assertIsNone(f["body"])
        self.assertIn("Café “quoted”", f["html"])

    def test_rtf_only_message(self):
        rtf = (b"{\\rtf1\\ansi\\ansicpg1252\\fromhtml1 {\\*\\htmltag19 <html>}"
               b"{\\*\\htmltag50 <body>}\\htmlrtf {\\htmlrtf0 {\\*\\htmltag64 <p>}"
               b"Pour on Friday{\\*\\htmltag72 </p>}}\\htmlrtf0 {\\*\\htmltag58 </body>}}")
        compressed = struct.pack("<IIII", len(rtf) + 12, len(rtf), 0x414C454D, 0) + rtf
        data = mb.build_msg(subject="RTF", body=" \r\n", rtf_compressed=compressed)
        f = msgfile.read_msg_bytes(data)
        kind, text = f["rtf"]
        self.assertEqual(kind, "html")
        self.assertIn("<p>Pour on Friday</p>", text)

    def test_unsent_flag_and_missing_dates(self):
        data = mb.build_msg(subject="Draft", body="draft", message_flags=0x8,
                            creation_time=datetime(2024, 1, 2, 3, 4, tzinfo=UTC))
        f = msgfile.read_msg_bytes(data)
        self.assertTrue(f["unsent"])
        self.assertIsNone(f["submit_time"])
        self.assertEqual(f["creation_time"], datetime(2024, 1, 2, 3, 4, tzinfo=UTC))

    def test_8bit_strings_use_message_codepage(self):
        data = mb.build_msg(subject="“Quoted” – café", body="Body ’s",
                            unicode=False, codepage=1252)
        f = msgfile.read_msg_bytes(data)
        self.assertEqual(f["subject"], "“Quoted” – café")
        self.assertEqual(f["body"], "Body ’s")

    def test_8bit_strings_without_message_codepage_use_internet_codepage(self):
        # Shift-JIS strings, only PR_INTERNET_CPID = 50220 (iso-2022-jp) present.
        tree = mb.message_tree(subject="x", body="y", unicode=False, internet_cpid=50220)
        tree["__substg1.0_0037001E"] = "日本語 subject".encode("cp932")
        f = msgfile.read_msg_bytes(mb.build_cfb(tree))
        self.assertEqual(f["subject"], "日本語 subject")

    def test_8bit_code_page_labels_that_would_garble_the_text(self):
        # Windows-1252 bytes under labels that don't fit 8-bit text.
        subject, body = "Levels \u2013 \u201cfinal\u201d", "Cost \u20ac1,200. Don\u2019t change."
        for label, cpid in ((28591, None), (1200, None), (1201, None), (20127, None),
                            (65001, None), (1200, 20127)):
            data = mb.build_msg(subject=subject, body=body, unicode=False, codepage=label,
                                internet_cpid=cpid, text_codec="cp1252")
            f = msgfile.read_msg_bytes(data)
            with self.subTest(codepage=label, internet_cpid=cpid):
                self.assertEqual(f["subject"], subject)
                self.assertEqual(f["body"], body)
                for text in (f["subject"], f["body"]):
                    self.assertFalse([ch for ch in text if "\x80" <= ch <= "\x9f"
                                      or "\u3000" <= ch <= "\u9fff" or ch == "\ufffd"])
        # UTF-16 label: the Windows code page of PR_INTERNET_CPID is used instead.
        data = mb.build_msg(subject="\u041e\u0442\u0447\u0451\u0442", body="x", unicode=False,
                            codepage=1200, internet_cpid=1251, text_codec="cp1251")
        self.assertEqual(msgfile.read_msg_bytes(data)["subject"], "\u041e\u0442\u0447\u0451\u0442")
        # Real UTF-8 under a UTF-8 label stays UTF-8.
        data = mb.build_msg(subject="Caf\u00e9 \u2013 ok", body="x", unicode=False, codepage=65001,
                            text_codec="utf-8")
        self.assertEqual(msgfile.read_msg_bytes(data)["subject"], "Caf\u00e9 \u2013 ok")

    def test_4096_sector_message(self):
        data = mb.build_msg(subject="Big sectors", body="text " * 2000, sector_size=4096)
        f = msgfile.read_msg_bytes(data)
        self.assertEqual(f["subject"], "Big sectors")
        self.assertEqual(f["body"], "text " * 2000)

    def test_read_msg_from_disk(self):
        data = mb.build_msg(subject="On disk", body="hello")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.msg")
            with open(path, "wb") as fh:
                fh.write(data)
            self.assertEqual(msgfile.read_msg(path)["subject"], "On disk")

    def test_big_msg_is_read_in_place_without_its_attachment_data(self):
        # An email with a drawing attached: only the directory and property
        # streams are read from the file server, not the 3 MB attachment.
        data = mb.build_msg(subject="Drawings attached", body="See attached C-101.",
                            attachments=[{"long_name": "C-101.pdf", "data": b"x" * 3000000}])
        self.assertGreater(len(data), msgfile._IN_MEMORY_LIMIT)
        counted = {"bytes": 0}
        real_open = open

        class CountingFile(io.FileIO):
            def readinto(self, buffer):
                n = io.FileIO.readinto(self, buffer)
                counted["bytes"] += n or 0
                return n

        def counting_open(path, mode="r", buffering=-1, **kwargs):
            if mode != "rb":
                return real_open(path, mode, buffering, **kwargs)
            raw = CountingFile(path, "r")
            return io.BufferedReader(raw, buffering if buffering > 0 else io.DEFAULT_BUFFER_SIZE)

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "big.msg")
            with open(path, "wb") as fh:
                fh.write(data)
            with mock.patch.object(msgfile, "open", counting_open, create=True):
                in_place = msgfile.read_msg(path)
        whole = msgfile.read_msg_bytes(data)
        for key in ("subject", "body"):
            self.assertEqual(in_place[key], whole[key])
        self.assertEqual([(a["name"], a["size"]) for a in in_place["attachments"]],
                         [(a["name"], a["size"]) for a in whole["attachments"]])
        self.assertEqual(in_place["attachments"][0]["name"], "C-101.pdf")
        self.assertLess(counted["bytes"], len(data) // 4)


class CompressedRtfTests(unittest.TestCase):
    # The two worked examples from [MS-OXRTFCP] section 3.

    def test_spec_example_1(self):
        data = bytes.fromhex("2d0000002b0000004c5a4675f1c5c7a703000a00726370673132354232"
                             "0af32068656c090020627705b06c647d0a800fa0")
        self.assertEqual(msgfile.decompress_rtf(data),
                         b"{\\rtf1\\ansi\\ansicpg1252\\pard hello world}\r\n")

    def test_spec_example_2_with_run(self):
        data = bytes.fromhex("1a0000001c0000004c5a4675e2d44b51410004205758595a0d6e7d010eb0")
        self.assertEqual(msgfile.decompress_rtf(data), b"{\\rtf1 WXYZWXYZWXYZWXYZWXYZ}")

    def test_uncompressed_mela(self):
        raw = b"{\\rtf1 plain}"
        data = struct.pack("<IIII", len(raw) + 12, len(raw), 0x414C454D, 0) + raw
        self.assertEqual(msgfile.decompress_rtf(data), raw)

    def test_bad_headers(self):
        with self.assertRaises(msgfile.MsgFileError):
            msgfile.decompress_rtf(b"short")
        with self.assertRaises(msgfile.MsgFileError):
            msgfile.decompress_rtf(struct.pack("<IIII", 4, 4, 0x12345678, 0) + b"abcd")
        with self.assertRaises(msgfile.MsgFileError):
            msgfile.decompress_rtf(struct.pack("<IIII", 4, 0x7FFFFFFF, 0x75465A4C, 0) + b"abcd")


class RtfTextTests(unittest.TestCase):

    def test_plain_rtf_to_text(self):
        rtf = (b"{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl{\\f0\\fswiss Arial;}}"
               b"{\\colortbl;\\red255\\green0\\blue0;}"
               b"\\pard Hello\\par World \\'e9\\u8212?\\tab end\\par}\x00")
        kind, text = msgfile.rtf_to_html_or_text(rtf)
        self.assertEqual(kind, "text")
        self.assertEqual(text, "Hello\nWorld é—\tend\n")

    def test_html_encapsulated_rtf(self):
        rtf = (b"{\\rtf1\\ansi\\ansicpg1252\\fromhtml1 \\deff0{\\fonttbl{\\f0\\fswiss Arial;}}\r\n"
               b"{\\*\\htmltag19 <html>}{\\*\\htmltag34 <head>}"
               b"{\\*\\htmltag161 <style>p\\{color:red\\}</style>}{\\*\\htmltag41 </head>}\r\n"
               b"{\\*\\htmltag50 <body>}\\htmlrtf {\\htmlrtf0 {\\*\\htmltag64 <p>}"
               b"Hello \\'e9 world{\\*\\htmltag72 </p>}\\htmlrtf \\par \\htmlrtf0}"
               b"{\\*\\htmltag58 </body>}{\\*\\htmltag27 </html>}}")
        kind, html = msgfile.rtf_to_html_or_text(rtf)
        self.assertEqual(kind, "html")
        self.assertIn("<style>p{color:red}</style>", html)
        self.assertIn("<p>Hello é world</p>", html)
        self.assertNotIn("\\par", html)

    def test_font_charset_selects_codepage(self):
        rtf = (b"{\\rtf1\\ansi\\ansicpg1252{\\fonttbl{\\f0 Arial;}{\\f1\\fcharset134 SimSun;}}"
               b"\\f0 Hi {\\f1 \\'c4\\'e3\\'ba\\'c3} there}")
        kind, text = msgfile.rtf_to_html_or_text(rtf)
        self.assertEqual(text, "Hi 你好 there")

    def test_damaged_unicode_number_is_skipped(self):
        # Damaged RTF: \u with a number far too big for a character. The "?"
        # after it is the usual fallback character, skipped as always.
        for word in (b"\\u9999999999", b"\\u-9999999999", b"\\u1114112"):
            kind, text = msgfile.rtf_to_html_or_text(b"{\\rtf1\\ansi Hello " + word + b"? world}")
            self.assertEqual(text, "Hello  world")

    def test_unicode_with_fallback_skipping(self):
        rtf = b"{\\rtf1\\uc2 A\\u8364\\'80\\'80B\\uc0\\u8211 C}"
        self.assertEqual(msgfile.rtf_to_html_or_text(rtf)[1], "A€B–C")

    def test_characters_beyond_ffff_are_joined(self):
        # Emoji are written as two \\uN halves (a surrogate pair).
        rtf = b"{\\rtf1\\ansi Hi \\u-10179?\\u-8704? there\\par}"
        self.assertEqual(msgfile.rtf_to_html_or_text(rtf, 1252)[1], "Hi \U0001F600 there\n")
        # Half a pair can't be shown: it becomes the replacement character.
        rtf = b"{\\rtf1\\ansi Hi \\u-10179? there}"
        text = msgfile.rtf_to_html_or_text(rtf, 1252)[1]
        self.assertEqual(text, "Hi \ufffd there")
        text.encode("utf-8")


class MeetingTests(unittest.TestCase):

    START = datetime(2025, 3, 13, 3, 0, tzinfo=UTC)
    END = datetime(2025, 3, 13, 4, 30, tzinfo=UTC)

    def build(self, message_class="IPM.Schedule.Meeting.Request", named=None, **kwargs):
        if named is None:
            named = [(mb.PSETID_APPOINTMENT, 0x8208, "string", "Site office"),
                     (mb.PSETID_APPOINTMENT, 0x820D, "time", self.START),
                     (mb.PSETID_APPOINTMENT, 0x820E, "time", self.END)]
        return mb.build_msg(subject="Design review", message_class=message_class,
                            body="Agenda", named=named, **kwargs)

    def test_named_property_ids(self):
        other = msgfile.uuid.UUID("00020329-0000-0000-C000-000000000046")
        data = self.build(named=[(other, 0x1234, "string", "x"),
                                 (mb.PSETID_APPOINTMENT, 0x820D, "time", self.START)])
        ids = msgfile.named_property_ids(open_cfb(data))
        self.assertEqual(ids, {(other, 0x1234): 0x8000,
                               (msgfile.PSETID_APPOINTMENT, 0x820D): 0x8001})

    def test_meeting_request_fields(self):
        fields = msgfile.read_msg_bytes(self.build())
        self.assertEqual(fields["meeting_start"], self.START)
        self.assertEqual(fields["meeting_end"], self.END)
        self.assertEqual(fields["meeting_location"], "Site office")

    def test_start_and_end_date_are_the_fallback(self):
        fields = msgfile.read_msg_bytes(self.build(named=[], start_date=self.START,
                                                   end_date=self.END))
        self.assertEqual((fields["meeting_start"], fields["meeting_end"]), (self.START, self.END))
        self.assertEqual(fields["meeting_location"], "")

    def test_only_meeting_items_get_meeting_fields(self):
        for item_class in ("IPM.Note", "IPM.Schedule.Meeting.Resp.Pos"):
            fields = msgfile.read_msg_bytes(self.build(item_class, start_date=self.START))
            self.assertEqual((fields["meeting_start"], fields["meeting_end"],
                              fields["meeting_location"]), (None, None, ""), item_class)

    def test_damaged_name_table_keeps_the_email(self):
        tree = mb.message_tree(subject="Design review", message_class="IPM.Appointment",
                               body="Agenda", start_date=self.START)
        tree["__nameid_version1.0"]["__substg1.0_00030102"] = struct.pack("<II", 0x820D, 3 << 1)
        fields = msgfile.read_msg_bytes(mb.build_cfb(tree))   # GUID stream is empty
        self.assertEqual(fields["subject"], "Design review")
        self.assertEqual(fields["meeting_start"], self.START)


class SignedMessageTests(unittest.TestCase):

    def test_p7m_bytes_kept_only_for_smime_items(self):
        att = [{"long_name": "smime.p7m", "mime": "multipart/signed", "data": b"MIME here"},
               {"long_name": "drawing.pdf", "data": b"%PDF"}]
        fields = msgfile.read_msg_bytes(mb.build_msg(
            subject="Signed", message_class="IPM.Note.SMIME.MultipartSigned", attachments=att))
        self.assertEqual([a["data"] for a in fields["attachments"]], [b"MIME here", None])
        fields = msgfile.read_msg_bytes(mb.build_msg(subject="Plain", attachments=att))
        self.assertEqual([a["data"] for a in fields["attachments"]], [None, None])


class HelperTests(unittest.TestCase):

    def test_decode_html_bytes(self):
        self.assertEqual(msgfile.decode_html_bytes(b"<meta charset=utf-8>\xc3\xa9"), "<meta charset=utf-8>é")
        self.assertEqual(msgfile.decode_html_bytes(b"<p>\x93x\x94</p>"), "<p>“x”</p>")
        self.assertEqual(msgfile.decode_html_bytes(b"\xc3\xa9"), "é")
        self.assertEqual(msgfile.decode_html_bytes("é already text"), "é already text")
        sjis = "日本".encode("cp932")
        self.assertEqual(msgfile.decode_html_bytes(sjis, 932), "日本")

    def test_codepages(self):
        self.assertEqual(msgfile.codec_for_codepage(65001), "utf-8")
        self.assertEqual(msgfile.codec_for_codepage(1252), "cp1252")
        self.assertEqual(msgfile.codec_for_codepage(99999), "cp1252")
        self.assertEqual(msgfile.ansi_codepage_for(50220), 932)
        self.assertEqual(msgfile.ansi_codepage_for(20127), 1252)
        # 8-bit text is never UTF-16/UTF-32; ASCII is read as Windows-1252.
        self.assertEqual(msgfile.eight_bit_codepage(1252), 1252)
        self.assertEqual(msgfile.eight_bit_codepage(1200), None)
        self.assertEqual(msgfile.eight_bit_codepage(1201, 50220), 932)
        self.assertEqual(msgfile.eight_bit_codepage(None, 28592), 1250)
        self.assertEqual(msgfile.eight_bit_codepage(20127), 1252)
        self.assertEqual(msgfile.eight_bit_codepage(None), None)
        self.assertEqual(msgfile.decode_8bit("\u2019".encode("cp1252"), "utf-8"), "\u2019")
        self.assertEqual(msgfile.decode_8bit("caf\u00e9".encode("utf-8"), "utf-8"), "caf\u00e9")

    def test_filetime(self):
        ft = mb.filetime(datetime(2026, 2, 20, 0, 34, tzinfo=UTC))
        self.assertEqual(msgfile.filetime_to_datetime(ft), datetime(2026, 2, 20, 0, 34, tzinfo=UTC))
        self.assertIsNone(msgfile.filetime_to_datetime(0))
        self.assertIsNone(msgfile.filetime_to_datetime(mb.filetime(datetime(4501, 1, 1, tzinfo=UTC))))


@unittest.skipUnless(os.environ.get("SQUISH_MSG_SAMPLES"), "set SQUISH_MSG_SAMPLES to a folder of .msg files")
class SampleFolderTests(unittest.TestCase):
    """Optional: read every .msg in $SQUISH_MSG_SAMPLES (compares with extract-msg if installed)."""

    def test_samples(self):
        folder = os.environ["SQUISH_MSG_SAMPLES"]
        names = sorted(n for n in os.listdir(folder) if n.lower().endswith(".msg"))
        self.assertTrue(names, "no .msg files in %s" % folder)
        try:
            import extract_msg
        except ImportError:
            extract_msg = None
        for name in names:
            path = os.path.join(folder, name)
            with self.subTest(file=name):
                fields = msgfile.read_msg(path)
                self.assertIsInstance(fields["subject"], str)
                if extract_msg is None:
                    continue
                try:
                    msg = extract_msg.openMsg(path, strict=False)
                except Exception:
                    continue
                try:
                    if getattr(msg, "areStringsUnicode", False):
                        self.assertEqual(fields["subject"], (msg.subject or "").replace("\x00", ""))
                finally:
                    msg.close()


if __name__ == "__main__":
    unittest.main()
