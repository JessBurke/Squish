"""Tests for readers.py: HTML to text, .eml reading and .msg reading (synthetic files)."""

import json
import os
import re
import shutil
import struct
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from squish_app import readers
from tests import msg_builder as mb

UTC = timezone.utc

RECORD_KEYS = set([
    "path", "date", "sender_name", "sender_email", "to", "cc", "subject", "body",
    "attachments", "message_id", "in_reply_to", "conversation_topic", "item_class",
    "auto_reply", "meeting", "reader",
])

SURROGATE = re.compile("[\ud800-\udfff]")


def has_surrogates(value):
    """True if any string inside ``value`` holds a lone surrogate character."""
    if isinstance(value, str):
        return bool(SURROGATE.search(value))
    if isinstance(value, list):
        return any(has_surrogates(v) for v in value)
    if isinstance(value, dict):
        return any(has_surrogates(v) for v in value.values())
    return False


def parse_iso(text):
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise AssertionError("date is not timezone-aware: %r" % text)
    return dt


class TempDirMixin(object):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="squish-readers-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        mode = "wb" if isinstance(data, bytes) else "w"
        kwargs = {} if isinstance(data, bytes) else {"encoding": "utf-8", "newline": ""}
        with open(path, mode, **kwargs) as fh:
            fh.write(data)
        return path


# --------------------------------------------------------------------------
# HTML -> text
# --------------------------------------------------------------------------

class HtmlToTextTests(unittest.TestCase):

    def test_blocks_breaks_and_entities(self):
        html = ("<html><body><p>First&nbsp;line &amp; more</p><div>Second<br>Third</div>"
                "<p>&nbsp;</p><p>Caf&eacute; &#8211; done</p></body></html>")
        self.assertEqual(readers.html_to_text(html),
                         "First line & more\nSecond\nThird\n\nCafé – done")

    def test_skips_head_style_script(self):
        html = ("<html><head><title>T</title><style>p {color: red}</style></head>"
                "<body><script>var x = 1;</script><p>Visible</p></body></html>")
        self.assertEqual(readers.html_to_text(html), "Visible")

    def test_unclosed_head_does_not_hide_body(self):
        self.assertEqual(readers.html_to_text("<head><meta charset=utf-8><body><p>Body text</p>"),
                         "Body text")

    def test_data_table_rows_become_lines_with_cells(self):
        html = ("<table><tr><th>Item</th><th>Status</th><th>Due</th></tr>"
                "<tr><td><p class=MsoNormal>RFI 12</p></td><td>Open</td><td>3 March</td></tr>"
                "<tr><td>RFI 13</td><td></td><td>10 March</td></tr></table><p>After</p>")
        self.assertEqual(readers.html_to_text(html),
                         "Item | Status | Due\nRFI 12 | Open | 3 March\nRFI 13 |  | 10 March\nAfter")

    def test_layout_table_keeps_lines(self):
        html = ("<table><tr><td><img src='logo.png'></td>"
                "<td><p>Alex Example</p><p>Senior Engineer</p></td></tr></table>")
        self.assertEqual(readers.html_to_text(html), "Alex Example\nSenior Engineer")

    def test_unclosed_cells_and_rows(self):
        html = "<table><tr><td>a<td>b<tr><td>c<td>d</table>"
        self.assertEqual(readers.html_to_text(html), "a | b\nc | d")

    def test_lists(self):
        html = "<ul><li>One</li><li>Two</li></ul><ol><li>First<li>Second</ol>"
        self.assertEqual(readers.html_to_text(html), "• One\n• Two\n1. First\n2. Second")

    def test_numbered_lists_keep_their_numbers(self):
        # A list continued after a paragraph, lettered options and set numbers:
        # replies like "we prefer (b)" or "item 3" must still point at the right item.
        html = ('<p>Responses:</p><ol><li>Pipe is 375mm</li><li>Cover is 600mm</li></ol>'
                '<p>Items 3 and 4 need the surveyor:</p>'
                '<ol start="3"><li>Invert level TBC</li><li>Pit location TBC</li></ol>'
                '<ol type="a"><li>Option A</li><li>Option B</li></ol><p>We prefer (b).</p>'
                '<ol style="margin:0; list-style-type: upper-roman"><li>Stage one</li>'
                '<li>Stage two</li></ol>'
                '<ol start="x"><li value="12">RFI 12 closed</li><li>RFI 13 open</li></ol>')
        self.assertEqual(readers.html_to_text(html),
                         "Responses:\n1. Pipe is 375mm\n2. Cover is 600mm\n"
                         "Items 3 and 4 need the surveyor:\n3. Invert level TBC\n4. Pit location TBC\n"
                         "a. Option A\nb. Option B\nWe prefer (b).\n"
                         "I. Stage one\nII. Stage two\n12. RFI 12 closed\n13. RFI 13 open")
        self.assertEqual([readers._list_label(n, "a") for n in (1, 26, 27, 53)], ["a", "z", "aa", "ba"])
        self.assertEqual([readers._list_label(n, "I") for n in (4, 9, 14, 1994)],
                         ["IV", "IX", "XIV", "MCMXCIV"])
        self.assertEqual(readers._list_label(0, "a"), "0")

    def test_links_like_outlook_plain_text(self):
        html = ('<p>See <a href="https://example.com/doc">the drawing</a> and '
                '<a href="https://example.com">https://example.com</a> or '
                '<a href="mailto:a@example.com">a@example.com</a></p>')
        self.assertEqual(readers.html_to_text(html),
                         "See the drawing <https://example.com/doc> and https://example.com "
                         "or a@example.com")

    def test_rule_and_whitespace(self):
        html = "<p>  lots   of\n\n spaces </p><hr><p>quoted</p>"
        self.assertEqual(readers.html_to_text(html), "lots of spaces\n" + "_" * 32 + "\nquoted")

    def test_empty_and_broken(self):
        self.assertEqual(readers.html_to_text(""), "")
        self.assertEqual(readers.html_to_text("plain words"), "plain words")
        self.assertIn("text", readers.html_to_text("<p>text<!-- unterminated"))


# --------------------------------------------------------------------------
# .eml
# --------------------------------------------------------------------------

EML_MULTIPART = """\
From: "Example, Alex" <Alex.Example@Example.com>
To: Sam Sample <sam@example.org>, pat@example.net
Cc: =?utf-8?q?Ren=C3=A9e_Test?= <renee@example.com>
Subject: RE: Site access
Date: Tue, 04 Mar 2025 15:06:07 +1100
Message-ID: <reply-2@example.com>
In-Reply-To: <orig-1@example.com>
Thread-Topic: Site access
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="b1"

--b1
Content-Type: text/plain; charset="utf-8"

Hi Sam,
Gate code is 1234.

--b1
Content-Type: text/html; charset="utf-8"

<html><body><p>HTML version</p></body></html>
--b1--
"""

EML_HTML_TABLE = """\
From: Alex Example <alex@example.com>
To: sam@example.org
Subject: Register
Date: Wed, 05 Mar 2025 09:00:00 +0000
MIME-Version: 1.0
Content-Type: text/html; charset="utf-8"

<html><head><style>td {border:1px}</style></head><body>
<p>Latest register:</p>
<table><tr><td>Doc</td><td>Rev</td></tr><tr><td>S-101</td><td>C</td></tr></table>
</body></html>
"""

EML_INLINE_AND_ATTACHMENT = """\
From: Alex Example <alex@example.com>
To: sam@example.org
Subject: Photos
Date: Thu, 06 Mar 2025 10:00:00 +1000
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="mixed"

--mixed
Content-Type: multipart/related; boundary="rel"

--rel
Content-Type: text/html; charset="utf-8"

<p>Logo below</p><img src="cid:logo123@x"><img src="cid:image001.png@01DB">
--rel
Content-Type: image/png; name="company-logo.png"
Content-ID: <logo123@x>
Content-Disposition: inline; filename="company-logo.png"
Content-Transfer-Encoding: base64

iVBORw0KGgo=
--rel
Content-Type: image/png; name="image001.png"
Content-ID: <image001.png@01DB>
Content-Transfer-Encoding: base64

iVBORw0KGgo=
--rel--

--mixed
Content-Type: application/pdf; name="Inspection report.pdf"
Content-Disposition: attachment; filename="Inspection report.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjQKJcfsj6IK
--mixed
Content-Type: image/jpeg; name="IMG_1234.jpg"
Content-Disposition: attachment; filename="IMG_1234.jpg"
Content-ID: <photo1@x>
Content-Transfer-Encoding: base64

/9j/4AAQSkZJRg==
--mixed
Content-Type: message/rfc822

From: someone@example.net
Subject: Original request

Body of the attached email.
--mixed--
"""


EML_WITH_ATTACHED_EMAIL = """\
From: Sam Sample <sam@example.org>
To: Alex Example <alex@example.com>
Subject: FW: Variation 3
Date: Mon, 05 May 2025 09:00:00 +1000
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="outer"

--outer
Content-Type: text/plain; charset=utf-8

As discussed.
--outer
Content-Type: message/rfc822

From: Pat Client <pat@client.example>
To: Sam Sample <sam@example.org>
Subject: RE: Variation 3
Date: Fri, 02 May 2025 13:04:00 +1000
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="inner"

--inner
Content-Type: text/plain; charset=utf-8

Yes, approved - proceed with option B.
--inner
Content-Type: message/rfc822

From: Sam Sample <sam@example.org>
Subject: Variation 3

Older text.
--inner--
--outer
Content-Type: message/rfc822

Subject: Empty one

--outer--
"""


def eml_with_headers(extra_headers, subject="Hello"):
    return ("From: Alex Example <alex@example.com>\nTo: sam@example.org\nSubject: %s\n"
            "Date: Thu, 06 Mar 2025 10:00:00 +1000\n%s\nBody text\n" % (subject, extra_headers))


class EmlTests(TempDirMixin, unittest.TestCase):

    def test_multipart_plain_and_html(self):
        rec = readers.read_email(self.write("a.eml", EML_MULTIPART))
        self.assertEqual(set(rec), RECORD_KEYS)
        self.assertEqual(rec["reader"], "eml")
        self.assertEqual(rec["subject"], "RE: Site access")
        self.assertEqual(rec["sender_name"], "Example, Alex")
        self.assertEqual(rec["sender_email"], "alex.example@example.com")
        self.assertEqual(rec["to"], [["Sam Sample", "sam@example.org"],
                                     ["pat@example.net", "pat@example.net"]])
        self.assertEqual(rec["cc"], [["Renée Test", "renee@example.com"]])
        self.assertEqual(rec["body"], "Hi Sam,\nGate code is 1234.\n")
        self.assertEqual(parse_iso(rec["date"]), datetime(2025, 3, 4, 4, 6, 7, tzinfo=UTC))
        self.assertEqual(rec["message_id"], "<reply-2@example.com>")
        self.assertEqual(rec["in_reply_to"], "<orig-1@example.com>")
        self.assertEqual(rec["conversation_topic"], "Site access")
        self.assertEqual(rec["item_class"], "IPM.Note")
        self.assertFalse(rec["auto_reply"])
        self.assertEqual(rec["attachments"], [])
        self.assertEqual(rec["path"], os.path.abspath(os.path.join(self.tmp, "a.eml")))

    def test_html_only_with_table(self):
        rec = readers.read_email(self.write("b.eml", EML_HTML_TABLE))
        self.assertEqual(rec["body"], "Latest register:\nDoc | Rev\nS-101 | C")

    def test_inline_images_and_attachments(self):
        rec = readers.read_email(self.write("c.eml", EML_INLINE_AND_ATTACHMENT))
        self.assertEqual(rec["body"], "Logo below")
        atts = dict((a["name"], a) for a in rec["attachments"])
        self.assertEqual(sorted(atts), ["IMG_1234.jpg", "Inspection report.pdf",
                                        "Original request.eml", "company-logo.png", "image001.png"])
        self.assertTrue(atts["company-logo.png"]["inline"])      # cid referenced from the HTML
        self.assertTrue(atts["image001.png"]["inline"])
        self.assertFalse(atts["Inspection report.pdf"]["inline"])
        self.assertEqual(atts["Inspection report.pdf"]["size"], 15)
        self.assertFalse(atts["IMG_1234.jpg"]["inline"])         # has a cid but not used in the body
        self.assertFalse(atts["Original request.eml"]["inline"])
        self.assertIsNone(atts["Original request.eml"]["size"])
        attached = atts["Original request.eml"]["email"]
        self.assertEqual(attached["sender_email"], "someone@example.net")
        self.assertEqual(attached["body"].strip(), "Body of the attached email.")
        self.assertEqual([n for n, a in sorted(atts.items()) if a["email"]], ["Original request.eml"])

    def test_attached_email_is_read_one_level_deep(self):
        rec = readers.read_email(self.write("fw.eml", EML_WITH_ATTACHED_EMAIL))
        self.assertEqual(rec["body"].strip(), "As discussed.")
        self.assertEqual([a["name"] for a in rec["attachments"]],
                         ["RE: Variation 3.eml", "Empty one.eml"])
        attached = rec["attachments"][0]["email"]
        self.assertEqual(sorted(attached), ["body", "cc", "date", "sender_email", "sender_name",
                                            "subject", "to"])
        self.assertEqual(attached["sender_name"], "Pat Client")
        self.assertEqual(attached["sender_email"], "pat@client.example")
        self.assertEqual(parse_iso(attached["date"]), datetime(2025, 5, 2, 3, 4, tzinfo=UTC))
        self.assertEqual(attached["to"], [["Sam Sample", "sam@example.org"]])
        self.assertEqual(attached["subject"], "RE: Variation 3")
        self.assertEqual(attached["body"].strip(), "Yes, approved - proceed with option B.")
        self.assertNotIn("Older text", attached["body"])   # only one level is read
        # No sender and no text: nothing worth keeping.
        self.assertIsNone(rec["attachments"][1]["email"])

    def test_attached_email_text_is_capped(self):
        with mock.patch.object(readers, "EMBEDDED_BODY_MAX", 10):
            rec = readers.read_email(self.write("fw.eml", EML_WITH_ATTACHED_EMAIL))
        self.assertEqual(rec["attachments"][0]["email"]["body"], "Yes, appro")

    def test_auto_reply_headers(self):
        cases = [
            ("Auto-Submitted: auto-replied", True),
            ("Auto-Submitted: no", False),
            ("X-Auto-Response-Suppress: All", True),
            ("Precedence: junk", True),
            ("Precedence: bulk", False),
            ("X-Mailer: Something", False),
        ]
        for i, (header, expected) in enumerate(cases):
            with self.subTest(header=header):
                rec = readers.read_email(self.write("h%d.eml" % i, eml_with_headers(header)))
                self.assertEqual(rec["auto_reply"], expected)
        rec = readers.read_email(self.write("oof.eml", eml_with_headers("", "Automatic reply: Away")))
        self.assertTrue(rec["auto_reply"])

    def test_non_utf8_charsets(self):
        latin = ("From: a@example.com\nSubject: =?iso-8859-1?q?Caf=E9?=\n"
                 "Date: Thu, 06 Mar 2025 10:00:00 +0000\nMIME-Version: 1.0\n"
                 "Content-Type: text/plain; charset=iso-8859-1\n"
                 "Content-Transfer-Encoding: quoted-printable\n\nSoup du jour: velout=E9\n")
        rec = readers.read_email(self.write("latin.eml", latin))
        self.assertEqual(rec["subject"], "Café")
        self.assertEqual(rec["body"].strip(), "Soup du jour: velouté")
        cp1252 = (b"From: a@example.com\nSubject: Quotes\nMIME-Version: 1.0\n"
                  b"Content-Type: text/plain; charset=windows-1252\n"
                  b"Content-Transfer-Encoding: 8bit\n\n\x93Smart\x94 \x96 quotes\n")
        rec = readers.read_email(self.write("cp1252.eml", cp1252))
        self.assertEqual(rec["body"].strip(), "“Smart” – quotes")
        unknown = (b"From: a@example.com\nSubject: Odd\nMIME-Version: 1.0\n"
                   b"Content-Type: text/plain; charset=x-made-up\n\nCaf\xc3\xa9\n")
        rec = readers.read_email(self.write("unknown.eml", unknown))
        self.assertEqual(rec["body"].strip(), "Café")

    def test_missing_and_odd_dates(self):
        rec = readers.read_email(self.write("nodate.eml", "From: a@example.com\nSubject: x\n\nbody\n"))
        self.assertEqual(rec["date"], "")
        rec = readers.read_email(self.write("baddate.eml", "From: a@example.com\nDate: not a date\n\nbody\n"))
        self.assertEqual(rec["date"], "")
        # "-0000" means "time zone unknown": the time is taken as UTC.
        rec = readers.read_email(self.write("naive.eml", "Date: Thu, 06 Mar 2025 10:00:00 -0000\n\nbody\n"))
        self.assertEqual(parse_iso(rec["date"]), datetime(2025, 3, 6, 10, 0, tzinfo=UTC))
        # No Date: header, but a Received: header.
        received = ("Received: from mx.example.com by mail.example.org; Fri, 07 Mar 2025 08:30:00 +1000\n"
                    "From: a@example.com\nSubject: y\n\nbody\n")
        rec = readers.read_email(self.write("recv.eml", received))
        self.assertEqual(parse_iso(rec["date"]), datetime(2025, 3, 6, 22, 30, tzinfo=UTC))

    def test_name_only_addresses(self):
        # Some exports write just the display name, with no address.
        rec = readers.read_email(self.write("names.eml", "From: Sam Brown\nTo: Kim Lee, pat@example.com\n"
                                                         "Cc: /O=EXAMPLE/OU=X/CN=RECIPIENTS/CN=JO\n"
                                                         "Subject: x\n\nbody\n"))
        self.assertEqual((rec["sender_name"], rec["sender_email"]), ("Sam Brown", ""))
        self.assertEqual(rec["to"], [["Kim Lee", ""], ["pat@example.com", "pat@example.com"]])
        self.assertEqual(rec["cc"], [])

    def test_meeting_and_report_item_classes(self):
        invite = ("From: a@example.com\nSubject: Coordination meeting\nMIME-Version: 1.0\n"
                  "Content-Type: multipart/alternative; boundary=\"c\"\n\n--c\n"
                  "Content-Type: text/plain\n\nJoin us\n--c\nContent-Type: text/calendar; method=REQUEST\n\n"
                  "BEGIN:VCALENDAR\nMETHOD:REQUEST\nEND:VCALENDAR\n--c--\n")
        rec = readers.read_email(self.write("invite.eml", invite))
        self.assertEqual(rec["item_class"], "IPM.Schedule.Meeting.Request")
        self.assertEqual(rec["attachments"], [])
        reply = invite.replace("METHOD:REQUEST", "METHOD:REPLY\nATTENDEE;PARTSTAT=DECLINED:mailto:b@example.com")
        rec = readers.read_email(self.write("reply.eml", reply))
        self.assertEqual(rec["item_class"], "IPM.Schedule.Meeting.Resp.Neg")
        ndr = ("From: postmaster@example.com\nSubject: Undeliverable: Hi\nMIME-Version: 1.0\n"
               "Content-Type: multipart/report; report-type=delivery-status; boundary=\"r\"\n\n"
               "--r\nContent-Type: text/plain\n\nCould not deliver\n--r\n"
               "Content-Type: message/delivery-status\n\nStatus: 5.1.1\n--r--\n")
        rec = readers.read_email(self.write("ndr.eml", ndr))
        self.assertEqual(rec["item_class"], "REPORT.IPM.Note.NDR")
        self.assertEqual(rec["attachments"], [])

    def test_raw_8bit_utf8_headers_are_repaired(self):
        raw = ("From: Zoë Müller <zoe@example.com>\r\nTo: Sam Sample <sam@example.org>\r\n"
               "Cc: Renée Test <renee@example.com>\r\nSubject: Café culvert – design\r\n"
               "Date: Mon, 3 Mar 2025 10:00:00 +1100\r\nMIME-Version: 1.0\r\n"
               "Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n"
               "\r\nBody text é here.\r\n").encode("utf-8")
        rec = readers.read_email(self.write("8bit.eml", raw))
        self.assertEqual(rec["sender_name"], "Zoë Müller")
        self.assertEqual(rec["cc"], [["Renée Test", "renee@example.com"]])
        self.assertFalse(has_surrogates(rec))
        rec["body"].encode("utf-8")

    def test_signature_part_is_not_listed(self):
        signed = ("From: a@example.com\nSubject: Levels\nMIME-Version: 1.0\n"
                  "Content-Type: multipart/signed; protocol=\"application/pkcs7-signature\"; "
                  "boundary=\"b\"\n\n--b\nContent-Type: multipart/mixed; boundary=\"c\"\n\n"
                  "--c\nContent-Type: text/plain\n\nLevels confirmed at RL 12.40.\n"
                  "--c\nContent-Type: application/pdf; name=\"levels.pdf\"\n"
                  "Content-Disposition: attachment; filename=\"levels.pdf\"\n\n%PDF\n--c--\n"
                  "--b\nContent-Type: application/pkcs7-signature; name=\"smime.p7s\"\n"
                  "Content-Disposition: attachment; filename=\"smime.p7s\"\n\nMIAGCSqG\n--b--\n")
        rec = readers.read_email(self.write("signed.eml", signed))
        self.assertEqual(rec["body"].strip(), "Levels confirmed at RL 12.40.")
        visible = [a["name"] for a in rec["attachments"] if not a["inline"]]
        self.assertEqual(visible, ["levels.pdf"])

    def test_dispatch_by_extension(self):
        path = self.write("UPPER.EML", EML_MULTIPART)
        self.assertEqual(readers.read_email(path)["subject"], "RE: Site access")
        with self.assertRaises(ValueError):
            readers.read_email(self.write("notes.txt", "hello"))
        with self.assertRaises(OSError):
            readers.read_email(os.path.join(self.tmp, "missing.eml"))

    def test_backend_status(self):
        self.assertTrue(readers.backend_status().startswith("Outlook .msg: "))
        with mock.patch.dict(os.environ, {"SQUISH_NO_EXTRACT_MSG": "1"}):
            self.assertEqual(readers.backend_status(), "Outlook .msg: built-in reader")


# --------------------------------------------------------------------------
# .msg
# --------------------------------------------------------------------------

X500_ALEX = "/O=EXCHANGELABS/OU=EXCHANGE ADMINISTRATIVE GROUP (FYDIBOHF23SPDLT)/CN=RECIPIENTS/CN=ALEX"
X500_SAM = "/O=EXCHANGELABS/OU=EXCHANGE ADMINISTRATIVE GROUP (FYDIBOHF23SPDLT)/CN=RECIPIENTS/CN=SAM"


def sample_msg_bytes(**overrides):
    inner = mb.message_tree(subject="RFI 7 - original", body="original", embedded=True)
    kwargs = dict(
        subject="RE: RFI 7 - pile caps",
        body="Hi Sam,\r\nRevised detail attached.\r\n",
        html="<p>Hi Sam,</p><p>Revised detail attached.<img src=\"cid:image001.png@01DB\"></p>",
        sender_name="Alex Example", sender_email=X500_ALEX,
        on_behalf_name="Alex Example",
        headers=("Received: from x by y; Tue, 4 Mar 2025 05:06:08 +0000\r\n"
                 "From: Alex Example <Alex@Example.com>\r\n"
                 "To: Sam Sample <sam@example.org>\r\nMessage-ID: <hdr-id@example.com>\r\n"
                 "In-Reply-To: <rfi7@example.com>\r\n"),
        recipients=[("Sam Sample", None, X500_SAM, 1),
                    ("'Pat Person'", "Pat@Example.net", "Pat@Example.net", 2),
                    ("Blind Copy", "bcc@example.net", "bcc@example.net", 3)],
        attachments=[
            {"long_name": "Pile cap detail rev C.pdf", "data": b"%PDF-1.4 ...", "mime": "application/pdf"},
            {"long_name": "image001.png", "data": b"\x89PNG", "content_id": "image001.png@01DB",
             "mime": "image/png"},
            {"long_name": "logo.jpg", "data": b"\xff\xd8", "hidden": True},
            {"display_name": "RFI 7 - original", "embedded": inner},
        ],
        submit_time=datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC),
        conversation_topic="RFI 7 - pile caps",
    )
    kwargs.update(overrides)
    return mb.build_msg(**kwargs)


SIGNED_MIME = (
    b'Content-Type: multipart/signed; protocol="application/pkcs7-signature"; '
    b'micalg=sha-256; boundary="BB"\r\n\r\n'
    b'--BB\r\nContent-Type: multipart/mixed; boundary="CC"\r\n\r\n'
    b'--CC\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n'
    b'Hi Sam, the headwall levels are confirmed at RL 12.40.\r\n'
    b'--CC\r\nContent-Type: application/pdf; name="levels.pdf"\r\n'
    b'Content-Disposition: attachment; filename="levels.pdf"\r\n'
    b'Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK\r\n--CC--\r\n'
    b'--BB\r\nContent-Type: application/pkcs7-signature; name="smime.p7s"\r\n'
    b'Content-Transfer-Encoding: base64\r\n\r\nMIAGCSqGSIb3DQEHAqCAMIACAQEx\r\n--BB--\r\n')


def meeting_msg_bytes(message_class="IPM.Schedule.Meeting.Request", location="Site office",
                      named=True, **overrides):
    start = datetime(2025, 3, 13, 3, 0, tzinfo=UTC)
    end = datetime(2025, 3, 13, 4, 30, tzinfo=UTC)
    props = []
    if named:
        props = [(mb.PSETID_APPOINTMENT, 0x820D, "time", start),
                 (mb.PSETID_APPOINTMENT, 0x820E, "time", end),
                 (mb.PSETID_APPOINTMENT, 0x8208, "string", location)]
    kwargs = dict(subject="Culvert design review", message_class=message_class,
                  body="Agenda: headwall levels", sender_name="Alex Example",
                  sender_smtp="alex@example.com",
                  recipients=[("Sam Sample", "sam@example.org", "sam@example.org", 1)],
                  submit_time=datetime(2025, 3, 1, 1, 0, tzinfo=UTC),
                  start_date=start, end_date=end, named=props)
    kwargs.update(overrides)
    return mb.build_msg(**kwargs)


class MsgTests(TempDirMixin, unittest.TestCase):

    def read_builtin(self, path):
        with mock.patch.dict(os.environ, {"SQUISH_NO_EXTRACT_MSG": "1"}):
            return readers.read_email(path)

    def test_builtin_reader_record(self):
        rec = self.read_builtin(self.write("a.msg", sample_msg_bytes()))
        self.assertEqual(set(rec), RECORD_KEYS)
        self.assertEqual(rec["reader"], "builtin_msg")
        self.assertEqual(rec["subject"], "RE: RFI 7 - pile caps")
        self.assertEqual(rec["sender_name"], "Alex Example")
        self.assertEqual(rec["sender_email"], "alex@example.com")     # X500 skipped, header used
        self.assertEqual(rec["to"], [["Sam Sample", "sam@example.org"]])  # resolved by name
        self.assertEqual(rec["cc"], [["Pat Person", "pat@example.net"]])  # quotes stripped, Bcc dropped
        self.assertEqual(parse_iso(rec["date"]), datetime(2025, 3, 4, 5, 6, 7, tzinfo=UTC))
        self.assertEqual(rec["body"], "Hi Sam,\nRevised detail attached.\n")
        self.assertEqual(rec["message_id"], "<hdr-id@example.com>")
        self.assertEqual(rec["in_reply_to"], "<rfi7@example.com>")
        self.assertEqual(rec["conversation_topic"], "RFI 7 - pile caps")
        self.assertEqual(rec["item_class"], "IPM.Note")
        self.assertFalse(rec["auto_reply"])
        self.assertEqual([(a["name"], a["inline"]) for a in rec["attachments"]],
                         [("Pile cap detail rev C.pdf", False), ("image001.png", True),
                          ("logo.jpg", True), ("RFI 7 - original.msg", False)])
        self.assertEqual(rec["attachments"][0]["size"], 12)
        self.assertIsNone(rec["meeting"])

    def test_meeting_request_time_and_place(self):
        rec = self.read_builtin(self.write("meet.msg", meeting_msg_bytes()))
        self.assertEqual(rec["item_class"], "IPM.Schedule.Meeting.Request")
        meeting = rec["meeting"]
        self.assertEqual(sorted(meeting), ["end", "location", "start"])
        self.assertEqual(parse_iso(meeting["start"]), datetime(2025, 3, 13, 3, 0, tzinfo=UTC))
        self.assertEqual(parse_iso(meeting["end"]), datetime(2025, 3, 13, 4, 30, tzinfo=UTC))
        self.assertEqual(meeting["location"], "Site office")
        # The send time stays the email's date.
        self.assertEqual(parse_iso(rec["date"]), datetime(2025, 3, 1, 1, 0, tzinfo=UTC))

    def test_meeting_without_named_properties_uses_start_and_end_date(self):
        rec = self.read_builtin(self.write("plain.msg", meeting_msg_bytes(named=False)))
        self.assertEqual(parse_iso(rec["meeting"]["start"]), datetime(2025, 3, 13, 3, 0, tzinfo=UTC))
        self.assertEqual(rec["meeting"]["location"], "")

    def test_meeting_cancellation_and_appointment_but_not_responses(self):
        for item_class in ("IPM.Schedule.Meeting.Canceled", "IPM.Appointment"):
            rec = self.read_builtin(self.write("c.msg", meeting_msg_bytes(item_class)))
            self.assertEqual(rec["meeting"]["location"], "Site office", item_class)
        rec = self.read_builtin(self.write("r.msg", meeting_msg_bytes("IPM.Schedule.Meeting.Resp.Pos")))
        self.assertIsNone(rec["meeting"])
        rec = self.read_builtin(self.write("n.msg", meeting_msg_bytes("IPM.Note")))
        self.assertIsNone(rec["meeting"])

    def test_rtf_emoji_is_one_character(self):
        rtf = b"{\\rtf1\\ansi\\ansicpg1252 Done \\u-10179?\\u-8704? see RFI 12\\par}"
        compressed = struct.pack("<IIII", len(rtf) + 12, len(rtf), 0x414C454D, 0) + rtf
        rec = self.read_builtin(self.write("emoji.msg", sample_msg_bytes(
            body=None, html=None, rtf_compressed=compressed)))
        self.assertEqual(rec["body"], "Done \U0001F600 see RFI 12")
        self.assertFalse(has_surrogates(rec))

    def test_signed_msg_is_unpacked_by_builtin_reader(self):
        data = mb.build_msg(subject="Levels", message_class="IPM.Note.SMIME.MultipartSigned",
                            sender_name="Sam Sample", sender_smtp="sam@example.org",
                            submit_time=datetime(2025, 3, 1, tzinfo=UTC),
                            attachments=[{"long_name": "smime.p7m", "mime": "multipart/signed",
                                          "data": SIGNED_MIME}])
        rec = self.read_builtin(self.write("signed.msg", data))
        self.assertEqual(rec["body"].strip(), "Hi Sam, the headwall levels are confirmed at RL 12.40.")
        visible = [(a["name"], a["size"]) for a in rec["attachments"] if not a["inline"]]
        self.assertEqual(visible, [("levels.pdf", 9)])
        self.assertEqual(rec["sender_name"], "Sam Sample")       # from the .msg, not the MIME
        self.assertEqual(parse_iso(rec["date"]), datetime(2025, 3, 1, tzinfo=UTC))

    def test_opaque_or_plain_p7m_is_left_alone(self):
        # Encrypted / opaque-signed content is binary, not MIME: keep the attachment.
        data = mb.build_msg(subject="Secret", message_class="IPM.Note.SMIME",
                            attachments=[{"long_name": "smime.p7m", "mime": "application/pkcs7-mime",
                                          "data": b"\x30\x82\x01\x00 binary"}])
        rec = self.read_builtin(self.write("opaque.msg", data))
        self.assertEqual(rec["body"], "")
        # Not an S/MIME item class: an attachment named smime.p7m is not unpacked.
        data = mb.build_msg(subject="Note", message_class="IPM.Note",
                            attachments=[{"long_name": "smime.p7m", "mime": "multipart/signed",
                                          "data": SIGNED_MIME}])
        rec = self.read_builtin(self.write("note.msg", data))
        self.assertEqual([a["name"] for a in rec["attachments"]], ["smime.p7m"])

    def test_signature_files_count_as_inline(self):
        self.assertTrue(readers.is_inline_attachment("smime.p7s"))
        self.assertTrue(readers.is_inline_attachment("sig.bin", mime="application/pkcs7-signature"))
        self.assertTrue(readers.is_inline_attachment("x", mime="application/x-pkcs7-signature"))
        self.assertFalse(readers.is_inline_attachment("Drawing C-101.pdf", mime="application/pdf"))

    def test_big_msg_uses_builtin_reader_first(self):
        data = sample_msg_bytes(attachments=[{"long_name": "C-001.pdf", "data": b"\0" * 3000000}])
        path = self.write("big.msg", data)

        def must_not_run(module, p, **kw):
            raise AssertionError("extract-msg should not be used for a big file")

        with mock.patch.object(readers, "_get_extract_msg", return_value=object()), \
                mock.patch.object(readers, "_fields_via_extract_msg", side_effect=must_not_run):
            rec = readers.read_email(path)
        self.assertEqual(rec["reader"], "builtin_msg")
        self.assertEqual([(a["name"], a["size"]) for a in rec["attachments"]],
                         [("C-001.pdf", 3000000)])

    def test_big_msg_falls_back_to_extract_msg(self):
        data = sample_msg_bytes(attachments=[{"long_name": "C-001.pdf", "data": b"\0" * 3000000}])
        path = self.write("big.msg", data)

        def builtin_fails(p):
            raise readers.msgfile.MsgFileError("damaged")

        with mock.patch.object(readers, "_get_extract_msg", return_value=object()), \
                mock.patch.object(readers, "_fields_via_extract_msg",
                                  side_effect=lambda module, p, **kw: readers.msgfile.read_msg_bytes(data)), \
                mock.patch.object(readers.msgfile, "read_msg", side_effect=builtin_fails):
            rec = readers.read_email(path)
        self.assertEqual(rec["reader"], "extract_msg")
        # Both failing: the built-in reader's error is the one reported.
        with mock.patch.object(readers, "_get_extract_msg", return_value=object()), \
                mock.patch.object(readers, "_fields_via_extract_msg", side_effect=RuntimeError("x")), \
                mock.patch.object(readers.msgfile, "read_msg", side_effect=builtin_fails):
            with self.assertRaises(readers.msgfile.MsgFileError):
                readers.read_email(path)

    def test_html_body_used_when_no_plain_body(self):
        data = sample_msg_bytes(body=None, html="<div>Line one</div><div>Line two</div>")
        rec = self.read_builtin(self.write("h.msg", data))
        self.assertEqual(rec["body"], "Line one\nLine two")

    def test_rtf_body_used_when_nothing_else(self):
        rtf = b"{\\rtf1\\ansi\\ansicpg1252 Concrete pour\\par moved to Monday\\par}"
        import struct
        compressed = struct.pack("<IIII", len(rtf) + 12, len(rtf), 0x414C454D, 0) + rtf
        data = sample_msg_bytes(body=None, html=None, rtf_compressed=compressed)
        rec = self.read_builtin(self.write("r.msg", data))
        self.assertEqual(rec["body"], "Concrete pour\nmoved to Monday")

    def test_out_of_office_class_and_headers(self):
        data = sample_msg_bytes(message_class="IPM.Note.Rules.OofTemplate.Microsoft")
        self.assertTrue(self.read_builtin(self.write("oof.msg", data))["auto_reply"])
        data = sample_msg_bytes(headers="From: a@example.com\r\nAuto-Submitted: auto-generated\r\n")
        self.assertTrue(self.read_builtin(self.write("auto.msg", data))["auto_reply"])
        data = sample_msg_bytes(message_class="IPM.Schedule.Meeting.Request",
                                headers="X-Auto-Response-Suppress: All\r\n")
        rec = self.read_builtin(self.write("meet.msg", data))
        self.assertFalse(rec["auto_reply"])
        self.assertEqual(rec["item_class"], "IPM.Schedule.Meeting.Request")

    def test_date_fallbacks(self):
        data = sample_msg_bytes(submit_time=None, headers="",
                                delivery_time=datetime(2025, 1, 2, 3, 4, tzinfo=UTC))
        rec = self.read_builtin(self.write("d1.msg", data))
        self.assertEqual(parse_iso(rec["date"]), datetime(2025, 1, 2, 3, 4, tzinfo=UTC))
        data = sample_msg_bytes(submit_time=None, headers="", recipients=[],
                                creation_time=datetime(2024, 6, 1, tzinfo=UTC))
        rec = self.read_builtin(self.write("d2.msg", data))
        self.assertEqual(parse_iso(rec["date"]), datetime(2024, 6, 1, tzinfo=UTC))
        data = sample_msg_bytes(submit_time=None, headers="")
        self.assertEqual(self.read_builtin(self.write("d3.msg", data))["date"], "")

    def test_recipients_from_headers_when_no_recipient_storages(self):
        data = sample_msg_bytes(recipients=[], display_to="Sam Sample",
                                headers="To: Sam Sample <sam@example.org>, other@example.com\r\n")
        rec = self.read_builtin(self.write("nr.msg", data))
        self.assertEqual(rec["to"], [["Sam Sample", "sam@example.org"],
                                     ["other@example.com", "other@example.com"]])

    def test_corrupt_msg_raises(self):
        path = self.write("bad.msg", b"this is not an outlook file" * 50)
        with self.assertRaises(Exception):
            self.read_builtin(path)

    def test_falls_back_to_builtin_when_extract_msg_fails(self):
        path = self.write("fb.msg", sample_msg_bytes())

        def broken(module, p):
            raise RuntimeError("extract-msg could not read this file")

        with mock.patch.object(readers, "_get_extract_msg", return_value=object()), \
                mock.patch.object(readers, "_fields_via_extract_msg", side_effect=broken):
            rec = readers.read_email(path)
        self.assertEqual(rec["reader"], "builtin_msg")
        self.assertEqual(rec["subject"], "RE: RFI 7 - pile caps")

    def test_extract_msg_and_builtin_agree(self):
        try:
            import extract_msg  # noqa: F401
        except ImportError:
            self.skipTest("extract-msg is not installed")
        path = self.write("both.msg", sample_msg_bytes())
        with mock.patch.dict(os.environ, {"SQUISH_NO_EXTRACT_MSG": ""}):
            readers._extract_msg_state["checked"] = False
            via_package = readers.read_email(path)
        builtin = self.read_builtin(path)
        self.assertEqual(via_package["reader"], "extract_msg")
        via_package.pop("reader")
        builtin.pop("reader")
        self.assertEqual(via_package, builtin)

    def test_extract_msg_and_builtin_agree_on_meetings(self):
        try:
            import extract_msg  # noqa: F401
        except ImportError:
            self.skipTest("extract-msg is not installed")
        for item_class in ("IPM.Schedule.Meeting.Request", "IPM.Appointment"):
            path = self.write("meet.msg", meeting_msg_bytes(item_class))
            with mock.patch.dict(os.environ, {"SQUISH_NO_EXTRACT_MSG": ""}):
                readers._extract_msg_state["checked"] = False
                via_package = readers.read_email(path)
            builtin = self.read_builtin(path)
            self.assertEqual(via_package["reader"], "extract_msg")
            self.assertIsNotNone(builtin["meeting"])
            self.assertEqual(via_package["meeting"], builtin["meeting"], item_class)


def with_attached_email_bytes():
    """A filed email that only says "See attached", with the client's reply attached."""
    nested = mb.message_tree(subject="Variation 3", body="Older text.", embedded=True)
    inner = mb.message_tree(
        subject="RE: Variation 3", body="Yes, approved - proceed with option B.\r\n",
        sender_name="Pat Client", sender_smtp="pat@client.example",
        recipients=[("Sam Sample", "sam@example.org", "sam@example.org", 1)],
        submit_time=datetime(2025, 5, 2, 3, 4, tzinfo=UTC), embedded=True,
        attachments=[{"display_name": "Variation 3", "embedded": nested}])
    return mb.build_msg(subject="FW: Variation 3", body="See attached.\r\n",
                        sender_name="Sam Sample", sender_smtp="sam@example.org",
                        submit_time=datetime(2025, 5, 5, 0, 0, tzinfo=UTC),
                        attachments=[{"display_name": "RE: X", "embedded": inner}])


def rtf_only_msg_bytes():
    """A filed email with only an HTML-wrapping RTF body (no plain or HTML body)."""
    rtf = (b"{\\rtf1\\ansi\\ansicpg1252\\fromhtml1 {\\*\\htmltag19 <html>}"
           b"{\\*\\htmltag50 <body>}\\htmlrtf {\\htmlrtf0 {\\*\\htmltag64 <p>}"
           b"Pour moved to Friday 7am{\\*\\htmltag72 </p>}}\\htmlrtf0 {\\*\\htmltag58 </body>}}")
    compressed = struct.pack("<IIII", len(rtf) + 12, len(rtf), 0x414C454D, 0) + rtf
    return sample_msg_bytes(body=None, html=None, rtf_compressed=compressed, attachments=[])


def extract_msg_installed():
    with mock.patch.dict(os.environ, {"SQUISH_NO_EXTRACT_MSG": ""}):
        readers._extract_msg_state["checked"] = False
        return readers._get_extract_msg() is not None


class AttachedEmailMsgTests(TempDirMixin, unittest.TestCase):

    def read_builtin(self, path):
        with mock.patch.dict(os.environ, {"SQUISH_NO_EXTRACT_MSG": "1"}):
            return readers.read_email(path)

    def check(self, rec):
        self.assertEqual(rec["body"], "See attached.\n")
        self.assertEqual(len(rec["attachments"]), 1)
        att = rec["attachments"][0]
        self.assertEqual(att["name"], "RE: Variation 3.msg")
        self.assertFalse(att["inline"])
        attached = att["email"]
        self.assertEqual(attached["sender_name"], "Pat Client")
        self.assertEqual(attached["sender_email"], "pat@client.example")
        self.assertEqual(parse_iso(attached["date"]), datetime(2025, 5, 2, 3, 4, tzinfo=UTC))
        self.assertEqual(attached["to"], [["Sam Sample", "sam@example.org"]])
        self.assertEqual(attached["cc"], [])
        self.assertEqual(attached["subject"], "RE: Variation 3")
        self.assertEqual(attached["body"], "Yes, approved - proceed with option B.\n")
        self.assertNotIn("Older text", attached["body"])

    def test_builtin_reader(self):
        rec = self.read_builtin(self.write("fw.msg", with_attached_email_bytes()))
        self.assertEqual(rec["reader"], "builtin_msg")
        self.check(rec)

    def test_extract_msg_reader_agrees(self):
        if not extract_msg_installed():
            self.skipTest("extract-msg is not installed")
        path = self.write("fw.msg", with_attached_email_bytes())
        with mock.patch.dict(os.environ, {"SQUISH_NO_EXTRACT_MSG": ""}):
            readers._extract_msg_state["checked"] = False
            rec = readers.read_email(path)
        self.assertEqual(rec["reader"], "extract_msg")
        self.check(rec)
        builtin = self.read_builtin(path)
        rec.pop("reader")
        builtin.pop("reader")
        self.assertEqual(rec, builtin)

    def test_attached_email_survives_the_cache(self):
        rec = self.read_builtin(self.write("fw.msg", with_attached_email_bytes()))
        self.assertEqual(json.loads(json.dumps(rec)), rec)


class ExtractMsgDateAndSpeedTests(TempDirMixin, unittest.TestCase):

    def read_both(self, data):
        path = self.write("x.msg", data)
        with mock.patch.dict(os.environ, {"SQUISH_NO_EXTRACT_MSG": "1"}):
            builtin = readers.read_email(path)
        with mock.patch.dict(os.environ, {"SQUISH_NO_EXTRACT_MSG": ""}):
            readers._extract_msg_state["checked"] = False
            via_package = readers.read_email(path)
        return builtin, via_package

    def test_placeholder_dates_are_not_dates(self):
        em_datetime = readers._em_datetime
        self.assertIsNone(em_datetime(datetime(4500, 12, 31, 23, 59)))       # Outlook "None"
        self.assertIsNone(em_datetime(datetime(4501, 1, 1, 10, 59, tzinfo=UTC)))
        self.assertIsNone(em_datetime(datetime(1601, 1, 1)))                 # zero FILETIME
        self.assertIsNone(em_datetime(None))
        self.assertIsNone(em_datetime("not a date"))
        real = datetime(2025, 6, 2, 1, 30, tzinfo=UTC)
        self.assertIs(em_datetime(real), real)
        self.assertEqual(em_datetime("Mon, 02 Jun 2025 11:30:00 +1000"), real)

    def test_extract_msg_skips_outlook_none_and_zero_dates(self):
        if not extract_msg_installed():
            self.skipTest("extract-msg is not installed")
        # (a) A filed draft: Outlook shows Received as "None" (4501-01-01).
        draft = sample_msg_bytes(message_flags=0x8, submit_time=None, headers="",
                                 delivery_time=datetime(4501, 1, 1, tzinfo=UTC),
                                 creation_time=datetime(2025, 6, 2, 1, 30, tzinfo=UTC))
        # (b) A zero send time (1601-01-01).
        zero = sample_msg_bytes(submit_time=datetime(1601, 1, 1, tzinfo=UTC), headers="",
                                delivery_time=datetime(2025, 3, 5, 1, 0, tzinfo=UTC))
        for data, expected in ((draft, datetime(2025, 6, 2, 1, 30, tzinfo=UTC)),
                               (zero, datetime(2025, 3, 5, 1, 0, tzinfo=UTC))):
            builtin, via_package = self.read_both(data)
            self.assertEqual(via_package["reader"], "extract_msg")
            self.assertEqual(parse_iso(builtin["date"]), expected)
            self.assertEqual(via_package["date"], builtin["date"])

    def test_extract_msg_is_opened_without_rtf_deencapsulation(self):
        seen = {}

        class FakeModule(object):
            @staticmethod
            def openMsg(path, **kwargs):
                seen.update(kwargs)
                return "opened"

        self.assertEqual(readers._open_with_extract_msg(FakeModule, "x.msg"), "opened")
        self.assertIsNone(seen["deencapsulationFunc"](b"{\\rtf1}", 1))
        self.assertFalse(seen["strict"])

    def test_rtf_only_body_without_rtfde(self):
        if not extract_msg_installed():
            self.skipTest("extract-msg is not installed")
        import RTFDE
        calls = []

        def must_not_run(*args, **kwargs):
            calls.append(args)   # (extract-msg swallows errors raised here)
            raise RuntimeError("RTFDE should not be used")

        with mock.patch.object(RTFDE.DeEncapsulator, "deencapsulate", must_not_run):
            builtin, via_package = self.read_both(rtf_only_msg_bytes())
        self.assertEqual(calls, [])
        self.assertEqual(via_package["reader"], "extract_msg")
        self.assertEqual(builtin["body"], "Pour moved to Friday 7am")
        self.assertEqual(via_package["body"], builtin["body"])


class TransportHeaderTests(TempDirMixin, unittest.TestCase):

    BLOCK = ("Microsoft Mail Internet Headers Version 2.0\r\n"
             "Received: from mail.example.invalid ([192.0.2.1]) by mx.example.invalid;\r\n"
             "\t Tue, 4 Mar 2025 05:06:08 +0000\r\n"
             "From: \"Sam Brown\" <sam@riverside.example.invalid>\r\n"
             "To: Alex Example <alex@example.invalid>\r\n"
             "Auto-Submitted: auto-replied\r\n"
             "\r\n")

    def headers(self, text):
        return readers._header_getter(readers._parse_transport_headers(text))

    def test_microsoft_mail_prefix_line_is_skipped(self):
        for text in (self.BLOCK, "\ufeff\r\n" + self.BLOCK, self.BLOCK.replace("\r\n", "\n")):
            get = self.headers(text)
            self.assertEqual(get("From"), '"Sam Brown" <sam@riverside.example.invalid>')
            self.assertEqual(get("Auto-Submitted"), "auto-replied")

    def test_plain_headers_unchanged_and_junk_ignored(self):
        get = self.headers(self.BLOCK.split("\r\n", 1)[1])
        self.assertEqual(get("To"), "Alex Example <alex@example.invalid>")
        self.assertIsNone(readers._parse_transport_headers(""))
        self.assertIsNone(readers._parse_transport_headers("no headers here\r\nat all"))

    def test_msg_with_prefixed_headers(self):
        data = sample_msg_bytes(headers=self.BLOCK, sender_name="Sam Brown", sender_email=X500_SAM,
                                on_behalf_name=None)
        with mock.patch.dict(os.environ, {"SQUISH_NO_EXTRACT_MSG": "1"}):
            rec = readers.read_email(self.write("p.msg", data))
        # The sender's SMTP address comes from the headers (the .msg only has X500).
        self.assertEqual(rec["sender_email"], "sam@riverside.example.invalid")
        self.assertTrue(rec["auto_reply"])


class EncodedAddressTests(TempDirMixin, unittest.TestCase):
    """An encoded "Last, First" name holds a comma once decoded: the addresses
    must be split before the names are decoded."""

    HEADERS = ("From: =?iso-8859-1?Q?M=FCller=2C_J=F6rg?= <jorg@acme.example>\r\n"
               "To: =?utf-8?Q?Tr=C3=A2n=2C_Kim?= <kim@riverside.example>,\r\n"
               " \"Lee, Pat\" <pat@riverside.example>\r\n"
               "Cc: =?utf-8?B?Q2hhbiwgV2Fp?= <wai@riverside.example>\r\n"
               "Cc: Sam Sample <sam@example.org>\r\n")

    def test_msg_without_mapi_sender_or_recipients_uses_the_headers(self):
        data = mb.build_msg(subject="Levels", body="See below.", headers=self.HEADERS,
                            submit_time=datetime(2025, 3, 4, 5, 6, tzinfo=UTC))
        with mock.patch.dict(os.environ, {"SQUISH_NO_EXTRACT_MSG": "1"}):
            rec = readers.read_email(self.write("h.msg", data))
        self.assertEqual((rec["sender_name"], rec["sender_email"]),
                         ("M\u00fcller, J\u00f6rg", "jorg@acme.example"))
        self.assertEqual(rec["to"], [["Tr\u00e2n, Kim", "kim@riverside.example"],
                                     ["Lee, Pat", "pat@riverside.example"]])
        self.assertEqual(rec["cc"], [["Chan, Wai", "wai@riverside.example"],
                                     ["Sam Sample", "sam@example.org"]])

    def test_eml_fallback_parser(self):
        import email
        msg = email.message_from_string(self.HEADERS.replace("\r\n", "\n") + "\nbody\n")
        self.assertEqual(readers._eml_addresses(msg, "From"),
                         [("M\u00fcller, J\u00f6rg", "jorg@acme.example")])
        self.assertEqual(readers._eml_addresses(msg, "Cc"),
                         [("Chan, Wai", "wai@riverside.example"), ("Sam Sample", "sam@example.org")])

    def test_eml_file(self):
        text = self.HEADERS.replace("\r\n", "\n") + "Subject: Levels\nDate: Tue, 04 Mar 2025 12:00:00 +0000\n\nbody\n"
        rec = readers.read_email(self.write("h.eml", text))
        self.assertEqual(rec["sender_name"], "M\u00fcller, J\u00f6rg")
        self.assertEqual(rec["to"][0], ["Tr\u00e2n, Kim", "kim@riverside.example"])


class EightBitMsgTests(TempDirMixin, unittest.TestCase):
    """Old 8-bit .msg files are read by the built-in reader, which reads their
    text as Windows-1252 (extract-msg reads code page 28591 as strict Latin-1)."""

    SUBJECT = "Levels \u2013 \u201cfinal\u201d"
    BODY = "The \u201cfinal\u201d levels \u2013 RL 12.5 \u2014 are agreed. Cost \u20ac1,200. Don\u2019t change.\r\n"

    def eight_bit(self, codepage=28591, **kwargs):
        return mb.build_msg(subject=self.SUBJECT, body=self.BODY, unicode=False, codepage=codepage,
                            text_codec="cp1252", sender_name="Alex Example",
                            sender_smtp="alex@example.com",
                            submit_time=datetime(2025, 3, 4, 5, 6, tzinfo=UTC), **kwargs)

    def check(self, rec):
        self.assertEqual(rec["subject"], self.SUBJECT)
        self.assertIn("Cost \u20ac1,200. Don\u2019t change.", rec["body"])
        self.assertFalse(re.search("[\x80-\x9f\u3000-\u9fff\ufffd]", rec["subject"] + rec["body"]))

    def test_8bit_msg_goes_to_the_builtin_reader(self):
        for codepage in (28591, 1200, 1252):
            path = self.write("old%d.msg" % codepage, self.eight_bit(codepage))
            with mock.patch.dict(os.environ, {"SQUISH_NO_EXTRACT_MSG": ""}):
                readers._extract_msg_state["checked"] = False
                rec = readers.read_email(path)
            with self.subTest(codepage=codepage):
                self.check(rec)
                if extract_msg_installed():
                    self.assertEqual(rec["reader"], "builtin_msg")

    def test_unicode_msg_with_an_8bit_attached_email_goes_to_the_builtin_reader(self):
        if not extract_msg_installed():
            self.skipTest("extract-msg is not installed")
        inner = mb.message_tree(subject=self.SUBJECT, body=self.BODY, unicode=False, codepage=28591,
                                text_codec="cp1252", sender_name="Pat Client",
                                sender_smtp="pat@client.example", embedded=True,
                                submit_time=datetime(2025, 3, 4, 5, 6, tzinfo=UTC))
        data = mb.build_msg(subject="FW: levels", body="See attached.", sender_name="Sam Sample",
                            sender_smtp="sam@example.org",
                            attachments=[{"display_name": "levels", "embedded": inner}])
        with mock.patch.dict(os.environ, {"SQUISH_NO_EXTRACT_MSG": ""}):
            readers._extract_msg_state["checked"] = False
            rec = readers.read_email(self.write("fw.msg", data))
        self.assertEqual(rec["reader"], "builtin_msg")
        attached = rec["attachments"][0]["email"]
        self.assertIn("Cost \u20ac1,200", attached["body"])

    def test_extract_msg_is_the_last_try_for_an_8bit_file(self):
        data = self.eight_bit()
        path = self.write("old.msg", data)
        calls = []

        def via_package(module, p, allow_8bit=False):
            calls.append(allow_8bit)
            if not allow_8bit:
                raise readers._EightBitMessage("8-bit strings")
            return readers.msgfile.read_msg_bytes(data)

        def builtin_fails(p):
            raise readers.msgfile.MsgFileError("damaged")

        with mock.patch.object(readers, "_get_extract_msg", return_value=object()), \
                mock.patch.object(readers, "_fields_via_extract_msg", side_effect=via_package), \
                mock.patch.object(readers.msgfile, "read_msg", side_effect=builtin_fails):
            rec = readers.read_email(path)
        self.assertEqual(rec["reader"], "extract_msg")
        self.assertEqual(calls, [False, True])


class SurrogateTests(unittest.TestCase):

    def test_fix_surrogates(self):
        fix = readers.fix_surrogates
        self.assertEqual(fix("plain text é"), "plain text é")
        # Raw UTF-8 bytes kept by Python as escapes.
        escaped = "Zoë".encode("utf-8").decode("ascii", "surrogateescape")
        self.assertEqual(fix(escaped + " Müller"), "Zoë Müller")
        # A raw Windows-1252 byte that isn't UTF-8.
        self.assertEqual(fix(b"Caf\xe9".decode("ascii", "surrogateescape")), "Café")
        # An emoji split into two halves is joined; a lone half is replaced.
        self.assertEqual(fix("ok \ud83d\ude00 done"), "ok \U0001F600 done")
        self.assertEqual(fix("ok \ud83d done"), "ok \ufffd done")
        self.assertEqual(fix("ok \ud83d\udca9"), "ok \U0001F4A9")

    def test_read_email_cleans_every_string(self):
        bad = {"subject": "a\udce9", "to": [["Zo\udcc3\udcab", "z@example.com"]],
               "attachments": [{"name": "x\ud800.pdf", "size": 1, "inline": False}]}
        with mock.patch.object(readers, "_read_eml", return_value=bad):
            rec = readers.read_email("anything.eml")
        self.assertEqual(rec["to"], [["Zoë", "z@example.com"]])
        self.assertFalse(has_surrogates(rec))


if __name__ == "__main__":
    unittest.main()
