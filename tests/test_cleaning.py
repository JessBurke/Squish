"""Tests for squish_app.cleaning (synthetic emails only)."""

import os
import tempfile
import time
import unittest
from datetime import datetime

from squish_app import cleaning as c

_TMP = None


def setUpModule():
    global _TMP
    _TMP = tempfile.TemporaryDirectory()
    os.environ["SQUISH_DATA_DIR"] = _TMP.name


def tearDownModule():
    _TMP.cleanup()


SIGNATURE = """Alex Citizen
Senior Engineer | Civil & Structural
M: 0400 111 222 <tel:0400%20111%20222>
E: alex.citizen@example-consulting.com <mailto:alex.citizen@example-consulting.com>
Example Consulting Pty Ltd
Level 3, 10 Sample Street, Exampletown, QLD, Australia 4000
 <https://eur01.safelinks.protection.outlook.com/?url=https%3A%2F%2Fwww.linkedin.com%2Fcompany%2Fexample&data=05%7C02>
Example Consulting acknowledges the traditional custodians of Country and pays respect to Elders past and present.
This email is confidential and intended only for the addressee. If you have received this email in error, please notify the sender and delete it.
"""

OUTLOOK_QUOTE = """
From: Sam Builder <sam.builder@example-builders.com.au>
Sent: Thursday, 23 October 2025 10:08 AM
To: Alex Citizen <alex.citizen@example-consulting.com>; Jo Planner <jo@example-consulting.com>
Cc: Pat Owner <pat@example-client.com.au>
Subject: RE: Slab pour 3

Hi Alex,

Can you confirm the bar spacing for pour 3?

Thanks,
Sam
"""


def clean(body, sender="Alex Citizen"):
    return c.clean_email(body, sender)["text"]


class SubjectTests(unittest.TestCase):
    def test_prefixes_are_removed(self):
        for s in ["RE: Slab pour 3", "FW: Slab pour 3", "Fwd: Slab pour 3", "RE: RE: FW: Slab pour 3",
                  "AW: Slab pour 3", "RE[2]: Slab pour 3", "re:Slab pour 3", "  RE :  Slab   pour 3 "]:
            self.assertEqual(c.clean_subject(s), "Slab pour 3", s)

    def test_external_tags_are_removed(self):
        self.assertEqual(c.clean_subject("[EXTERNAL] RE: Slab pour 3"), "Slab pour 3")
        self.assertEqual(c.clean_subject("RE: EXTERNAL: Slab pour 3"), "Slab pour 3")
        self.assertEqual(c.clean_subject("[Pending]RE: Slab pour 3"), "Slab pour 3")

    def test_mail_manager_filing_tags_are_removed(self):
        variants = ["RE: Slab pour 3 [Filed 24 Nov 2025 10:50]",
                    "RE: Slab pour 3 (Filed 24 Nov 2025 10:50)",
                    "[Filed 2 Mar 2026 9:05] RE: Slab pour 3",
                    "RE: Slab pour 3 [filed 24 nov 2025 10:50]",
                    "RE: Slab pour 3 [EXAMPLE-W.FID1234567]"]
        for s in variants:
            self.assertEqual(c.clean_subject(s), "Slab pour 3", s)

    def test_meeting_reply_prefixes_join_the_meeting_thread(self):
        for s in ["Accepted: Site meeting", "Declined: Site meeting", "Tentative: Site meeting",
                  "New Time Proposed: Site meeting", "Canceled: Site meeting"]:
            self.assertEqual(c.thread_key(s), "site meeting", s)

    def test_thread_key_ignores_case_and_whitespace(self):
        self.assertEqual(c.thread_key("RE: Slab\tPour  3"), c.thread_key("slab pour 3 [Filed 1 Jan 2026 09:00]"))

    def test_empty_subject(self):
        self.assertEqual(c.clean_subject(""), "(no subject)")
        self.assertEqual(c.clean_subject("RE:"), "(no subject)")

    def test_brackets_that_are_not_tags_are_kept(self):
        self.assertEqual(c.clean_subject("RE: Retaining wall (Stage 2) [Rev B]"), "Retaining wall (Stage 2) [Rev B]")

    def test_reply_subjects(self):
        for s in ["RE: Pour", "Re[2]: Pour", "AW: Pour", "[EXTERNAL] RE: Pour", "RE: FW: Pour",
                  "Accepted: Site meeting", "RE: Pour [Filed 4 Mar 2025 10:00]"]:
            self.assertTrue(c.is_reply_subject(s), s)
        for s in ["Pour", "FW: Pour", "Fwd: Pour", "FW: RE: Pour", "", "Re-pour of slab 3", "[EXTERNAL] Pour"]:
            self.assertFalse(c.is_reply_subject(s), s)


class NormaliseTests(unittest.TestCase):
    def test_smart_punctuation_and_spaces(self):
        text = c.normalise_text("It\u2019s \u201cdone\u201d \u2013 1.5\u20132 weeks\u2026\u00a0ok\u200b\r\n")
        self.assertEqual(text, "It's \"done\" - 1.5-2 weeks... ok\n")

    def test_emoji_removed_ticks_kept(self):
        self.assertEqual(c.normalise_text("Thanks \U0001F60A \u2705 \u2713 done"), "Thanks  \u2713 \u2713 done")
        self.assertEqual(c.normalise_text("Formwork \u2705 / Reinforcement \u274c / Cover \u2714"),
                         "Formwork \u2713 / Reinforcement \u2717 / Cover \u2714")

    def test_technical_symbols_and_check_boxes_kept(self):
        self.assertEqual(c.normalise_text("\u2300300 pipe"), "\u2300300 pipe")
        self.assertEqual(c.normalise_text("\u2610 open item \u2611 done \u2612 no"), "\u2610 open item \u2611 done \u2612 no")
        self.assertEqual(c.normalise_text("\u2316 position \u2312 arc"), "\u2316 position \u2312 arc")
        self.assertEqual(c.normalise_text("\u231b wait"), " wait")


class QuoteTests(unittest.TestCase):
    def test_outlook_header_block_is_cut(self):
        body = "Hi Sam,\n\nYes, 200 centres top and bottom.\n" + OUTLOOK_QUOTE
        self.assertEqual(clean(body), "Hi Sam, Yes, 200 centres top and bottom.")

    def test_original_message_separator(self):
        body = ("Approved.\n\n-----Original Message-----\nFrom: Sam Builder\nSent: Monday, 3 March 2025 9:00 AM\n"
                "To: Alex Citizen\nSubject: Variation 4\n\nPlease approve variation 4.\n")
        self.assertEqual(clean(body), "Approved.")

    def test_on_wrote_header(self):
        body = ("Will do.\n\nOn Mon, 2 Jun 2025 at 1:17 PM Sam Builder <sam.builder@example-builders.com.au> wrote:\n"
                "> Please send the pile layout.\n")
        self.assertEqual(clean(body), "Will do.")

    def test_underscore_rule_before_header(self):
        body = ("See attached.\n\n________________________________\nFrom: Sam Builder <sam@example-builders.com.au>\n"
                "Sent: Friday, 7 March 2025 4:00 PM\nTo: Alex Citizen\nSubject: Drawings\n\nOld text\n")
        self.assertEqual(clean(body), "See attached.")

    def test_gt_quoted_lines_removed(self):
        body = "Agreed, go ahead.\n> Shall we pour on Friday?\n> Sam\n"
        self.assertEqual(clean(body), "Agreed, go ahead.")

    def test_inline_answers_between_gt_lines_kept(self):
        body = "> Is the slab 200 thick?\nYes, 200.\n> And N12 bars?\nNo, N16.\n"
        self.assertEqual(clean(body), "Yes, 200. No, N16.")

    def test_forwarded_thunderbird_style(self):
        body = ("FYI\n\n-------- Forwarded Message --------\nSubject:\n\nFW: Fee\n\nDate:\n\nTue, 4 Mar 2025 12:35:25 +1000\n\n"
                "From:\n\nsam@example-builders.com.au <mailto:sam@example-builders.com.au>\n\nTo:\n\ninfo@example.org\n\n"
                "Please quote for the survey.\n")
        out = c.clean_email(body, "Alex Citizen")
        self.assertEqual(out["text"], "FYI")
        self.assertEqual(len(out["quoted"]), 1)
        self.assertEqual(out["quoted"][0]["sender_email"], "sam@example-builders.com.au")
        self.assertEqual(out["quoted"][0]["date"], datetime(2025, 3, 4, 12, 35))
        self.assertIn("Please quote for the survey.", out["quoted"][0]["body"])

    def test_begin_forwarded_message(self):
        body = ("Over to you.\n\nBegin forwarded message:\n\nFrom: Sam Builder <sam@example-builders.com.au>\n"
                "Subject: Site access\nDate: 5 March 2025 at 7:45:00 AM AEST\nTo: Alex Citizen\n\nGate code is 1234.\n")
        out = c.clean_email(body, "Alex Citizen")
        self.assertEqual(out["text"], "Over to you.")
        self.assertEqual(out["quoted"][0]["sender_name"], "Sam Builder")
        self.assertEqual(out["quoted"][0]["date"], datetime(2025, 3, 5, 7, 45))

    def test_prose_from_line_is_not_a_header(self):
        body = ("Hi Sam,\n\nFrom: our point of view the wall is fine.\nThe footing needs 50 cover from: the base.\n"
                "Regards,\nAlex\n")
        self.assertEqual(clean(body), "Hi Sam, From: our point of view the wall is fine. "
                                      "The footing needs 50 cover from: the base.")

    def test_parse_quoted_reads_nested_emails(self):
        body = ("Noted.\n" + OUTLOOK_QUOTE +
                "\nFrom: Alex Citizen <alex.citizen@example-consulting.com>\nSent: Wednesday, October 22, 2025 3:15 PM\n"
                "To: Sam Builder <sam.builder@example-builders.com.au>\nSubject: Slab pour 3\n\nPour 3 drawings attached.\n")
        out = c.clean_email(body, "Alex Citizen")
        q = out["quoted"]
        self.assertEqual(len(q), 2)
        self.assertEqual((q[0]["sender_name"], q[0]["sender_email"]), ("Sam Builder", "sam.builder@example-builders.com.au"))
        self.assertEqual(q[0]["date"], datetime(2025, 10, 23, 10, 8))
        self.assertEqual(q[0]["to"][1], ["Jo Planner", "jo@example-consulting.com"])
        self.assertEqual(q[1]["date"], datetime(2025, 10, 22, 15, 15))
        self.assertEqual(c.clean_text(q[0]["body"], q[0]["sender_name"]), "Hi Alex, Can you confirm the bar spacing for pour 3?")

    def test_on_wrote_wrapped_over_two_lines(self):
        body = ("Will do.\n\nOn Mon, 2 Jun 2025 at 1:17 PM Sam Builder <sam.builder@example-builders.com.au>\n"
                "wrote:\n> Please send the pile layout.\n")
        out = c.clean_email(body, "Alex Citizen")
        self.assertEqual(out["text"], "Will do.")
        self.assertEqual(out["quoted"][0]["sender_email"], "sam.builder@example-builders.com.au")

    def test_long_whitespace_runs_and_headers_are_fast(self):
        start = time.time()
        c.clean_email(("On" + " " * 300 + "x\n") * 200, "Alex Citizen")
        c.clean_email("Note.\n\nFrom: Sam Builder <sam@example.org>\nSent: Monday, 3 March 2025 9:00 AM\nTo: "
                      + "a" * 50000 + "\nSubject: x\n\nHi.\n", "Alex Citizen")
        c.parse_address_list(", ".join("Person Number%d" % i for i in range(5000)))
        self.assertLess(time.time() - start, 3.0)

    def test_parse_quoted_depth_limit(self):
        block = ("From: P{0} Person <p{0}@example.org>\nSent: Monday, 3 March 2025 9:0{0} AM\nTo: Someone\n"
                 "Subject: x\n\nMessage {0}.\n\n")
        quoted = "".join(block.format(i) for i in range(5))
        self.assertEqual(len(c.parse_quoted(quoted, 3)), 3)

    def test_mailto_wrapped_from_line(self):
        quoted = ("From: Sam Builder <sam@example-builders.com.au <mailto:sam@example-builders.com.au> > \n"
                  "Sent: 22 May 2025 3:15 PM\nTo: Alex Citizen; Jo Planner\nSubject: Test\n\nHello.\n")
        q = c.parse_quoted(quoted)[0]
        self.assertEqual(q["sender_email"], "sam@example-builders.com.au")
        self.assertEqual(q["to"], [["Alex Citizen", ""], ["Jo Planner", ""]])
        self.assertEqual(q["date"], datetime(2025, 5, 22, 15, 15))


class SignatureTests(unittest.TestCase):
    def test_signoff_and_full_signature_block(self):
        body = "Hi Sam,\n\nPour 3 is approved.\n\nKind regards,\n\nAlex\n\n" + SIGNATURE
        self.assertEqual(clean(body), "Hi Sam, Pour 3 is approved.")

    def test_sender_name_line_without_signoff(self):
        body = "Hi Sam, please speak to Pat about this.\n\n" + SIGNATURE
        self.assertEqual(clean(body), "Hi Sam, please speak to Pat about this.")

    def test_signoff_with_name_on_same_line(self):
        self.assertEqual(clean("Drawings attached.\n\nCheers, Alex\n" + SIGNATURE), "Drawings attached.")

    def test_pipe_title_and_phone_lines(self):
        body = ("Revised levels attached.\n\nSam Builder | Project Manager\nExample Builders |\n"
                "M: 0411 222 333 | E: sam@example-builders.com.au\n")
        self.assertEqual(clean(body, "Sam Builder"), "Revised levels attached.")

    def test_pronouns_and_work_days(self):
        body = ("Invoice approved.\n\nJo Planner (she/her)\nPlease note my work days are Monday to Wednesday\n"
                "D +61 7 5555 0000\n")
        self.assertEqual(clean(body, "Jo Planner"), "Invoice approved.")

    def test_logo_cid_and_links(self):
        body = ("See the folder 03 Client Feedback <https://example.sharepoint.com/sites/x/y?z=1> "
                "and www.example.org/page for details. [cid:image001.png@01DB1234.5678]\n")
        self.assertEqual(clean(body), "See the folder 03 Client Feedback <link> and example.org for details.")

    def test_safelink_bare_url_becomes_domain(self):
        body = ("Report: https://eur01.safelinks.protection.outlook.com/?url=https%3A%2F%2Fdocs.example.org%2Fr1&data=05\n")
        self.assertEqual(clean(body), "Report: docs.example.org")

    def test_tel_and_mailto_dropped(self):
        body = "Call me on 0400 111 222 <tel:0400%20111%20222> or email sam@example.org <mailto:sam@example.org>.\n"
        self.assertEqual(clean(body), "Call me on 0400 111 222 or email sam@example.org.")

    def test_caution_banner_and_safety_tip(self):
        body = ("CAUTION: This email originated from outside of the organisation. Do not click links or open "
                "attachments unless you recognise the sender.\nYou don't often get email from sam@example.org. "
                "Learn why this is important <https://aka.ms/LearnAboutSenderIdentification>\n\nHi Alex, see attached.\n")
        self.assertEqual(clean(body), "Hi Alex, see attached.")

    def test_teams_join_block_removed(self):
        body = ("Quick catch-up on the pour sequence.\n\n" + "_" * 80 + "\nMicrosoft Teams Need help? "
                "<https://aka.ms/JoinTeamsMeeting>\nJoin the meeting now <https://teams.microsoft.com/l/x>\n"
                "Meeting ID: 123 456 789 012\nPasscode: aB3dE5\n" + "_" * 32 + "\nDial in by phone\n"
                "+61 2 5555 0000,,123456# Australia, Sydney\nFind a local number <https://dialin.example>\n"
                "Phone conference ID: 123 456#\nFor organizers: Meeting options <https://x> | Reset dial-in PIN\n"
                + "_" * 80 + "\n")
        self.assertEqual(clean(body), "Quick catch-up on the pour sequence.")

    def test_sent_from_my_phone(self):
        self.assertEqual(clean("Yes go ahead\n\nSent from my iPhone\n"), "Yes go ahead")

    def test_signature_pasted_above_message(self):
        body = "Alex Citizen\nSenior Engineer\nM: 0400 111 222\n\nHi All\nThe site visit is moved to Tuesday.\n"
        self.assertEqual(clean(body), "Hi All, The site visit is moved to Tuesday.")

    def test_embedded_draft_after_signoff_is_kept(self):
        body = ("See below email I am planning to send.\n\nCheers,\nAlex\n\nHi Sam,\n\n"
                "We will proceed using the survey you provided.\n\nRegards,\nAlex\n" + SIGNATURE)
        self.assertEqual(clean(body), "See below email I am planning to send. Hi Sam, "
                                      "We will proceed using the survey you provided.")

    def test_signature_only_body_is_empty(self):
        self.assertEqual(clean("Kind Regards,\nAlex Citizen\nSenior Engineer\nExample Consulting\n"), "")

    def test_forward_with_only_the_senders_signature_is_empty(self):
        jane = "Jane Citizen"
        bodies = ["\n\nJane Citizen\t\nAssociate Consultant\t\n - \nO\n+61 2 9999 0000\nE\njane@example.com\n"
                  "Example Pty Ltd\n",
                  "Jane Citizen\nCPEng NER RPEQ\nAssociate Consultant\nM 0400 000 000\n",
                  "Jane Citizen\n | Project Manager\n M: 0400 000 000",
                  "Hi Sam\n\nJane Citizen\nSenior Engineer\nE jane@x.example"]
        for body in bodies:
            self.assertEqual(clean(body, jane), "", body)

    def test_text_around_a_signature_block_is_kept(self):
        jane = "Jane Citizen"
        self.assertTrue(clean("Jane Citizen\nSenior Engineer\nM: 0400 000 000\n\nPlease see the attached report and "
                              "let me know if you have any questions about it.", jane).endswith(
            "Please see the attached report and let me know if you have any questions about it."))
        self.assertEqual(clean("Jane Citizen - site notes\nPour delayed to Friday\nThanks", jane),
                         "Jane Citizen - site notes Pour delayed to Friday")
        self.assertEqual(clean("Jane Citizen | Senior Engineer\nM: 0400 000 000\n\nHi Sam,\nPlease review the drawings.",
                               jane), "Hi Sam, Please review the drawings.")
        self.assertIn("Rev B attached", clean("Jane Citizen\nSenior Engineer\n\nRev B attached", jane))
        self.assertEqual(clean("Thanks, Jane", jane), "Thanks, Jane")
        self.assertTrue(c.is_ack("Thanks, Jane"))

    def test_signature_with_label_lines_still_removed(self):
        body = ("Pour 3 approved.\n\nAlex Citizen\nD\n+61 7 5555 0000\nE\nalex@example.com\n"
                "Example Consulting Pty Ltd\nLevel 3, 10 Sample Street, Exampletown QLD 4000\n")
        self.assertEqual(clean(body), "Pour 3 approved.")
        self.assertEqual(clean("See attached.\n\nRegards\nAlex\nMobile:\n0400 111 222\nEmail:\nalex@example.com"),
                         "See attached.")
        self.assertEqual(clean("Please see below:\n\nAlex Citizen\nSenior Engineer\nM: 0400 111 222"),
                         "Please see below:")
        self.assertEqual(clean("Revised levels attached.\n\nSam Builder | Project Manager\nT 02 4200 0000 | M 0411 222 333",
                               "Sam Builder"), "Revised levels attached.")

    def test_colleagues_signature_before_the_senders_signoff_is_removed(self):
        body = ("Kim has some advice:\n\nUse the 300 pipe.\n\nKim Tran\nSenior Engineer\n - \nTransport\nM\n"
                "0400 222 333\nE\nkim@example.com\nExample Consulting Pty Ltd\nExampletown\nCheers\nAlex\n")
        out = clean(body)
        self.assertTrue(out.startswith("Kim has some advice: Use the 300 pipe."), out)
        for junk in ("Transport", "0400", "Pty Ltd", "Exampletown", "Cheers"):
            self.assertNotIn(junk, out)


class KeepContentTests(unittest.TestCase):
    def test_thanks_as_first_word_of_real_email(self):
        body = "Thanks for the drawings. The slab thickness on DWG-ST-1200 Rev C should be 250 mm, not 200 mm.\n"
        self.assertEqual(clean(body), body.strip())

    def test_thanks_line_followed_by_content(self):
        body = "Hi Sam,\n\nThanks\n\nI have reviewed RFI-042. Please use N16 at 200 centres.\n\nRegards,\nAlex\n"
        self.assertEqual(clean(body), "Hi Sam, Thanks I have reviewed RFI-042. Please use N16 at 200 centres.")

    def test_numbered_and_bulleted_lists(self):
        body = ("Items from the inspection:\n1. Bar spacing at 250 max\n2. Remove debris\n*\tStarter bars at 275 centres\n"
                "\u00b7         Cover 65 mm\no\tSub item\n- Dash item\n")
        self.assertEqual(clean(body), "Items from the inspection: \u2022 1. Bar spacing at 250 max \u2022 2. Remove debris "
                                      "\u2022 Starter bars at 275 centres \u2022 Cover 65 mm \u2022 Sub item \u2022 Dash item")

    def test_short_line_runs_become_list_items(self):
        body = "Subprojects set up as per below:\n\n623.0001 - Site visit\n\n623.0002 - Engineering\n"
        self.assertEqual(clean(body), "Subprojects set up as per below: \u2022 623.0001 - Site visit \u2022 623.0002 - Engineering")

    def test_fee_table_numbers_dates_and_dollars_kept(self):
        body = ("Time to move:\nName\nDATE\nHOURS\nAMOUNT\nAlex Citizen\n10-02-25\n2\n$320.00\nJo Planner\n"
                "21-02-25\n1.5\n$1,050.00\nThe files are saved here: H:\\Projects\\Budget\\Feb.\n\nThanks,\nAlex\n")
        out = clean(body)
        for bit in ("Alex Citizen", "10-02-25", "$320.00", "$1,050.00", "Jo Planner", "1.5", "H:\\Projects\\Budget\\Feb"):
            self.assertIn(bit, out)

    def test_sender_name_inside_a_list_is_not_a_signature(self):
        body = ("Proposed attendees:\n*\tPat Owner - Client\n*\tAlex Citizen - Example Consulting\n"
                "*\tSam Builder - Builder\nI'll confirm the room tomorrow.\n\nAlex Citizen\nM: 0400 111 222\n")
        out = clean(body)
        self.assertIn("Sam Builder - Builder", out)
        self.assertTrue(out.endswith("I'll confirm the room tomorrow."))

    def test_single_credential_in_content_is_not_a_signature(self):
        body = "Forecast this week:\nMe - Leachate RPEQ sign off 4hrs\nJo - 6hrs\nCan you add your hours please?\n"
        self.assertIn("Can you add your hours please?", clean(body))

    def test_rfi_and_drawing_numbers_kept(self):
        body = "Response to RFI-117: refer DWG 623.0001-ST-3041 Rev F, detail 4/ST-3050.\n"
        self.assertEqual(clean(body), body.strip())

    def test_tables_keep_all_rows(self):
        cases = [
            ("Hi Sam,\n\nPlease move these hours to stage 2:\nJP\n2.5\nAP\n1.5\nJP\n3\n\nThanks\nJane\n",
             "Jane Porter", ["JP \u2022 2.5", "AP \u2022 1.5", "JP \u2022 3"]),
            ("Hi Kim,\n\nPit lid sizes:\nReference\nItem\nValue\nA\nFrame width\n640\nB\nHinge offset\n75\n"
             "C\nCover depth\n410 mm\n\nRegards\nSam\n", "Sam Builder", ["Frame width \u2022 640", "B \u2022 Hinge",
                                                                     "410 mm"]),
            ("Agenda for Tuesday\na)\nSite access\nb)\nPour sequence\nc)\nDrainage\nd)\nFee update\ne)\nRisks\n\n"
             "Regards\nAlex\n", "Alex Citizen", ["d) \u2022 Fee update", "e) \u2022 Risks"]),
            ("Please move these hours\nJP\n2.5\nJP\n1\nKT\n3\n", "Jane Porter", ["JP \u2022 1", "KT \u2022 3"]),
        ]
        for body, sender, wanted in cases:
            out = clean(body, sender)
            for w in wanted:
                self.assertIn(w, out, body)

    def test_own_name_inside_a_table_then_real_signature(self):
        body = ("Invoice breakdown for March\nDesign\nJo Smith\n10\n$1,500.00\nAlex Citizen\n6\n$900.00\nTotal\n16\n"
                "$2,400.00\n\nAlex Citizen\nSenior Engineer\nM 0400 111 222\n")
        out = clean(body)
        self.assertTrue(out.endswith("Total \u2022 16 \u2022 $2,400.00"), out)
        self.assertNotIn("Senior Engineer", out)

    def test_folder_path_after_signoff_is_kept(self):
        body = ("Drawings are saved in the folder below.\n\nCheers\nAlex\nM 0400 000 000\n"
                "\\\\server\\projects\\1234\\ISSUED FOR CONSTRUCTION\n")
        out = clean(body)
        self.assertIn("\\\\server\\projects\\1234\\ISSUED FOR CONSTRUCTION", out)
        self.assertNotIn("0400", out)

    def test_paragraph_after_a_colon_is_content(self):
        cases = [
            ("Hi Sam,\n\nPump quotes received:\nSupplier | Price | Lead time\nAcme Pumps | $45,000 | 6 weeks\n"
             "FlowCo | $39,500 | 14 weeks\n\nRegards,\nAlex", ["FlowCo | $39,500 | 14 weeks"]),
            ("Hi Sam,\n\nQuote received from:\nAcme Pumps Pty Ltd\nPrice $45,000 ex GST\nLead time 6 weeks\n",
             ["Acme Pumps Pty Ltd", "Lead time 6 weeks"]),
            ("Hi Sam,\n\nKim is the site contact, please call her directly:\n0411 222 333\n\nRegards,\nAlex",
             ["0411 222 333"]),
            ("Attendees for Tuesday:\nKim Tran\nAlex Citizen\nJo Smith\n", ["Alex Citizen", "Jo Smith"]),
            ("Inspection by:\nAlex Citizen | Structural\nKim Tran | Geotech\n", ["Alex Citizen | Structural",
                                                                              "Kim Tran | Geotech"]),
            ("DETAILS:\nPrimary Client: Example Pty Ltd\nSite: Unit 4, 10 Sample Street, Exampletown NSW 2526\n\n"
             "Regards,\nAlex", ["Primary Client: Example Pty Ltd", "Exampletown NSW 2526"]),
        ]
        for body, wanted in cases:
            out = clean(body)
            for w in wanted:
                self.assertIn(w, out, body)
        self.assertNotIn("Regards", clean(cases[0][0]))

    def test_table_at_the_end_without_a_colon(self):
        body = "Hi Sam,\n\nSupplier | Price | Lead time\nAcme Pumps | $45,000 | 6 weeks\nFlowCo | $39,500 | 14 weeks\n"
        self.assertIn("FlowCo | $39,500 | 14 weeks", clean(body))

    def test_bullet_on_its_own_line(self):
        body = "Items to close out:\n*\nFormwork stripping\n*\nPipework testing\n\nThanks\nAlex"
        self.assertEqual(clean(body), "Items to close out: \u2022 Formwork stripping \u2022 Pipework testing")

    def test_site_talk_that_sounds_like_a_disclaimer_is_kept(self):
        for line in ["Warning: the outside face of the retaining wall at CH 340 has cracked, please keep workers clear.",
                     "Attention all: the attachments show the revised pit locations for stage 2.",
                     "We met the Traditional Owners on site today to walk the creek alignment.",
                     "Please notify us by Friday if the invert levels change.",
                     "Please check the sheet sizes before printing the A1 set.",
                     "I believe the intended recipient of this letter is Kim.",
                     "The contractor is not responsible for any damage to existing services shown on the plans.",
                     "There was unauthorised access to the site overnight, the gate lock was cut."]:
            self.assertEqual(clean("Hi Sam,\n" + line), "Hi Sam, " + line)
        self.assertEqual(clean("Hi all,\nMicrosoft Teams is down today so please call my mobile instead.\n"
                               "Site meeting moved to 2pm"),
                         "Hi all, Microsoft Teams is down today so please call my mobile instead. Site meeting moved to 2pm")

    def test_email_footers_are_still_removed(self):
        body = ("Hi Sam, see attached.\nIf you are not the intended recipient you must not use, copy or disclose this "
                "email.\nExample Co acknowledges the Traditional Owners and Custodians of Country throughout Australia.\n"
                "Privacy Statement <https://example.com/privacy>\nWARNING: External email. Think before you click links.\n")
        self.assertEqual(clean(body), "Hi Sam, see attached.")

    def test_share_notification_boilerplate_removed(self):
        body = ("Jo Smith invited you to edit a folder\nHere's the folder that Jo Smith shared with you.\n"
                "Please drop the survey files in here by Friday.\nStage 2 survey\n"
                "This invite will only work for you and people with existing access.\n"
                "Open <https://example.sharepoint.com/x> Share <https://example.sharepoint.com/y>\n"
                "This email is generated through Example Co's use of Microsoft 365 and may contain content that is "
                "controlled by Example Co.\nBook time to meet with me <https://outlook.example.com/book>\n")
        self.assertEqual(clean(body, "Jo Smith"), "Please drop the survey files in here by Friday. Stage 2 survey")
        self.assertEqual(clean("Please share with you team the folder for stage 2.\nOpen\n", "Jo Smith"),
                         "Please share with you team the folder for stage 2. Open")


class AckTests(unittest.TestCase):
    def test_acks(self):
        for t in ["Thanks JP", "Thanks Alex.", "Noted", "Received, thank you", "Thanks mate - appreciated.",
                  "Perfect, thanks!", "Cheers", "Ok noted, thanks.", "Thank you so much, Sam."]:
            self.assertTrue(c.is_ack(t), t)

    def test_not_acks(self):
        for t in ["Thanks, I will send the drawings tomorrow.", "Thanks - can you resend Rev B?",
                  "Noted, the pour is now 12 June.", "Approved", "Will do", "Thanks Sam, please hold the pour",
                  "", "Received from the builder FYI"]:
            self.assertFalse(c.is_ack(t), t)

    def test_answers_and_decisions_are_not_acks(self):
        for t in ["Approved, thanks", "Declined thanks", "No thanks", "Thanks - Rejected", "Agreed, thanks JP",
                  "Thanks, Confirmed", "Great, Proceed", "Thanks Cancelled", "Looks good thanks", "Received, Will Do",
                  "Yes, thanks Sam.", "That looks great, thank you.", "Thanks Sam, Approved.", "Ok thanks, Confirmed",
                  "Noted. Go ahead"]:
            self.assertFalse(c.is_ack(t), t)
        for t in ["No worries, thanks", "Thanks, no problem", "Noted.", "Thanks Sam"]:
            self.assertTrue(c.is_ack(t), t)


class CapTests(unittest.TestCase):
    def test_cut_at_sentence_boundary(self):
        text = "First sentence is here. " * 10 + "Last sentence goes past the cap and should be dropped."
        out = c.cap_text(text, 100)
        self.assertTrue(out.endswith("here. \u2026") or out.endswith("here \u2026") or out.endswith(". \u2026") is False)
        self.assertTrue(out.endswith(" \u2026"))
        self.assertLessEqual(len(out), 102)
        self.assertEqual(out[:-2].rstrip()[-1], ".")

    def test_abbreviation_is_not_a_sentence_end(self):
        text = "We need e.g. bigger bars here and there and everywhere around the pit walls. Then more text " * 3
        out = c.cap_text(text, 90)
        self.assertFalse(out.startswith("We need e.g. \u2026"))
        self.assertTrue(out.endswith("walls. \u2026"))

    def test_bullet_boundary_fallback(self):
        text = "Items \u2022 " + " \u2022 ".join("item number %d with words" % i for i in range(20))
        out = c.cap_text(text, 120)
        self.assertTrue(out.endswith("words \u2026"))

    def test_short_text_untouched(self):
        self.assertEqual(c.cap_text("Short.", 100), "Short.")
        self.assertEqual(c.cap_text("x" * 300, None), "x" * 300)

    def test_figures_after_the_cut_are_kept(self):
        text = "We reviewed the options for the pump station. " * 12 + "The agreed fee for stage 2 is $12,500 excl GST."
        out = c.cap_text(text, 300)
        self.assertIn("$12,500", out)
        self.assertLessEqual(len(out), 302)
        self.assertIn(" \u2026 The agreed fee", out)

    def test_question_after_the_cut_is_kept(self):
        text = "The formwork crew finished early on the north side of the slab. " * 10 + "Can you confirm the pour date?"
        out = c.cap_text(text, 300)
        self.assertTrue(out.endswith("Can you confirm the pour date?"), out)

    def test_fee_table_rows_keep_their_labels(self):
        rows = " ".join("\u2022 Task %d \u2022 %d \u2022 $%d,000.00" % (i, i, i) for i in range(1, 30))
        text = "Hi all, breakdown below for your review. " + rows + " \u2022 Total \u2022 435 \u2022 $435,000.00"
        out = c.cap_text(text, 300)
        self.assertIn("\u2022 Total \u2022 435 \u2022 $435,000.00", out)
        self.assertTrue(out.startswith("Hi all, breakdown below for your review."))

    def test_cut_never_ends_on_a_list_number(self):
        text = "Items to fix: \u2022 4. Long item text about the pit lid and its frame. \u2022 5. Next item " + "x " * 80
        out = c.cap_text(text, 90)
        self.assertFalse(out.rstrip(" \u2026").endswith("5."), out)
        self.assertTrue(out.endswith("frame. \u2026"), out)


class AttachmentTests(unittest.TestCase):
    def test_inline_names(self):
        for n in ["image001.png", "image804421.png", "image.png", "Outlook-A close up.png", "Outlook-qdiwjh5m.png",
                  "~WRL0001.tmp", "ATT00001.png", "emailbanner_Logo-2024.png", "Icon_Web_32x32.png", "attachment",
                  "linkedin.png"]:
            self.assertTrue(c.is_inline_attachment(n), n)
            self.assertTrue(c.is_inline_attachment({"name": n, "size": 100, "inline": False}), n)

    def test_real_attachments(self):
        for n in ["Pile layout - Rev 3.pdf", "IMG_3067.jpeg", "Site photo 01.jpg", "Fee proposal.docx",
                  "RE: Tunnel slab", "SWMS for signature.pdf", "Design Certificate - signature page.pdf",
                  "Logo Signage Plan.pdf", "Site Banner Layout.dwg", "Slab 1A.jpg", "Site Photo_03.jpg"]:
            self.assertFalse(c.is_inline_attachment(n), n)

    def test_more_inline_names(self):
        for n in ["company_logo.jpg", "Outlook-Logo, comp.png", "~WRD0000.jpg", "council_emailsig_650x120px.jpg",
                  "0f8fad5b-d9cb-469f-a165-70867728950e"]:
            self.assertTrue(c.is_inline_attachment(n), n)

    def test_camera_photo_names(self):
        for n in ["IMG_3067.jpeg", "IMG_1234.JPG", "WhatsApp Image 2025-03-04 at 10.22.33.jpeg",
                  "processed-0f8fad5b-d9cb-469f-a165-70867728950e.jpeg", "PXL_20250304_102233123.jpg"]:
            self.assertTrue(c.is_camera_photo(n), n)
        for n in ["Site Photo_03.jpg", "Slab 1A.jpg", "IMG_3067.pdf"]:
            self.assertFalse(c.is_camera_photo(n), n)

    def test_reader_inline_flag(self):
        self.assertTrue(c.is_inline_attachment({"name": "photo.jpg", "size": 1000, "inline": True}))

    def test_document_types(self):
        self.assertTrue(c.is_document("Calc.pdf"))
        self.assertTrue(c.is_document("model.dwg"))
        self.assertTrue(c.is_document("RE: an attached email"))
        self.assertFalse(c.is_document("IMG_3067.jpeg"))


class NoiseTests(unittest.TestCase):
    def rec(self, **kw):
        r = {"subject": "Slab", "item_class": "IPM.Note", "auto_reply": False, "sender_email": "sam@example.org"}
        r.update(kw)
        return r

    def test_noise_kinds(self):
        self.assertEqual(c.noise_kind(self.rec(subject="Accepted: Site meeting")), "meeting responses")
        self.assertEqual(c.noise_kind(self.rec(subject="Declined: Site meeting")), "meeting responses")
        self.assertEqual(c.noise_kind(self.rec(item_class="IPM.Schedule.Meeting.Resp.Pos")), "meeting responses")
        self.assertEqual(c.noise_kind(self.rec(subject="Automatic reply: Slab")), "auto-replies")
        self.assertEqual(c.noise_kind(self.rec(auto_reply=True)), "auto-replies")
        self.assertEqual(c.noise_kind(self.rec(subject="Undeliverable: Slab")), "receipts")
        self.assertEqual(c.noise_kind(self.rec(subject="Read: Slab")), "receipts")
        self.assertEqual(c.noise_kind(self.rec(item_class="REPORT.IPM.Note.NDR")), "receipts")
        self.assertEqual(c.noise_kind(self.rec(sender_email="no-reply@teams.mail.microsoft")), "notifications")
        self.assertEqual(c.noise_kind(self.rec(sender_email="noreply@emeaemail.teams.microsoft.com")), "notifications")

    def test_real_mail_and_meeting_requests_are_not_noise(self):
        self.assertEqual(c.noise_kind(self.rec()), "")
        self.assertEqual(c.noise_kind(self.rec(item_class="IPM.Schedule.Meeting.Request", subject="Site meeting")), "")
        self.assertEqual(c.noise_kind(self.rec(subject="Read me first: pour sequence")), "")


class AddressTests(unittest.TestCase):
    def test_parse_address_list(self):
        got = c.parse_address_list('"Citizen, Alex" <Alex.Citizen@Example.com>; Jo Planner <jo@example.com>; '
                                   "Pat Owner; 'Sam Builder' <sam@example.org>")
        self.assertEqual(got, [["Citizen, Alex", "alex.citizen@example.com"], ["Jo Planner", "jo@example.com"],
                               ["Pat Owner", ""], ["Sam Builder", "sam@example.org"]])

    def test_unquoted_last_first_is_glued_back(self):
        self.assertEqual(c.parse_address_list("Citizen, Alex <alex@example.com>"),
                         [["Citizen, Alex", "alex@example.com"]])

    def test_tidy_display_name(self):
        self.assertEqual(c.tidy_display_name("Citizen, Alex (EXT)"), "Alex Citizen")
        self.assertEqual(c.tidy_display_name("'Alex Citizen'"), "Alex Citizen")
        self.assertEqual(c.tidy_display_name("AlexCitizen"), "Alex Citizen")
        self.assertEqual(c.tidy_display_name("Alex Citizen | Example Group"), "Alex Citizen")
        self.assertEqual(c.tidy_display_name("", "alex.citizen@example.com"), "Alex Citizen")
        self.assertEqual(c.tidy_display_name("alex.citizen@example.com"), "Alex Citizen")
        self.assertEqual(c.name_key("Citizen, Alex"), c.name_key("Alex Citizen"))

    def test_name_key_ignores_accents_and_keeps_other_scripts(self):
        self.assertEqual(c.name_key("Jos\u00e9 \u00c1lvarez"), c.name_key("Jose Alvarez"))
        self.assertEqual(c.name_key("Jose Alvarez"), "josealvarez")
        self.assertTrue(c.name_key("\u041e\u043b\u044c\u0433\u0430 \u0418\u0432\u0430\u043d\u043e\u0432\u0430"))

    def test_loose_dates(self):
        cases = {
            "Thursday, 23 October 2025 10:08 AM": datetime(2025, 10, 23, 10, 8),
            "Wednesday, 10 September 2025 14:22:33": datetime(2025, 9, 10, 14, 22),
            "Thursday, May 22, 2025 3:15 PM": datetime(2025, 5, 22, 15, 15),
            "22 May 2025 12:05 AM": datetime(2025, 5, 22, 0, 5),
            "Tue, 4 Mar 2025 12:35:25 +1000": datetime(2025, 3, 4, 12, 35),
            "Mon, Jun 2, 2025 at 1:17 PM": datetime(2025, 6, 2, 13, 17),
            "2026-07-02T14:44:23+10:00": datetime(2026, 7, 2, 14, 44),
            "23/10/2025 9:30 am": datetime(2025, 10, 23, 9, 30),
        }
        for text, want in cases.items():
            self.assertEqual(c.parse_loose_date(text), want, text)
        self.assertIsNone(c.parse_loose_date("sometime soon"))


class KeywordTests(unittest.TestCase):
    def test_keyword_list(self):
        self.assertEqual(c.keyword_list("RFI, pour\nNCR;  "), ["rfi", "pour", "ncr"])
        self.assertEqual(c.keyword_list(""), [])

    def test_keywords_use_plain_punctuation(self):
        self.assertEqual(c.keyword_list("St John\u2019s, culvert \u2013 stage"), ["st john's", "culvert - stage"])
        self.assertEqual(c.keyword_list("\U0001F600, pit"), ["pit"])


class UnicodeTests(unittest.TestCase):
    def test_middle_dot_is_a_bullet_only_at_the_start_of_a_line(self):
        self.assertEqual(c.normalise_text("Moment 120 kN\u00b7m at Col\u00b7legi"), "Moment 120 kN\u00b7m at Col\u00b7legi")
        self.assertEqual(c.normalise_text("Items:\n\u00b7 pit lids\n  \u00b7\tgrates"), "Items:\n\u2022 pit lids\n  \u2022\tgrates")

    def test_callout_numbers_arrows_and_status_marks_kept(self):
        self.assertEqual(c.normalise_text("Refer note \u2776 and \u2777, A \u27a1 B, \u2b05 back"),
                         "Refer note \u2776 and \u2777, A \u27a1 B, \u2b05 back")
        self.assertEqual(c.normalise_text("\u26a0\ufe0f Do not excavate"), "(!) Do not excavate")
        self.assertEqual(c.normalise_text("\U0001f534 High \U0001f7e0 Moderate \U0001f7e2 Low"),
                         "[red] High [amber] Moderate [green] Low")
        self.assertEqual(c.normalise_text("Step 1\ufe0f\u20e3 first"), "Step 1 first")

    def test_decomposed_accents_are_composed(self):
        self.assertEqual(c.normalise_text("Jose\u0301 Nu\u0301n\u0303ez"), "Jos\u00e9 N\u00fa\u00f1ez")
        self.assertEqual(c.tidy_display_name("Rene\u0301e Le\u0301vesque"), "Ren\u00e9e L\u00e9vesque")

    def test_fold_for_match(self):
        self.assertEqual(c.fold_for_match("Caf\u00e9  Fit-out\u2013Stage\n2"), "cafe fit-out-stage 2")


class HeaderBlockTests(unittest.TestCase):
    def test_booking_details_are_not_a_quoted_email(self):
        body = ("Hi Sam,\n\nCrane booking details for the headwall units:\nFrom: 7:00am\nTo: 3:00pm\n"
                "Date: Tuesday 18 March 2025\nLocation: Gate 2, Riverside depot\n"
                "Please confirm the crane can arrive by 6:30am and that the 50 t unit is available.\n\nRegards\nAlex")
        got = c.clean_email(body, "Alex Citizen")
        self.assertEqual(got["quoted"], [])
        self.assertIn("7:00am", got["text"])
        self.assertIn("Please confirm the crane can arrive", got["text"])
        travel = "Travel booked:\nFrom: Sydney\nTo: Wollongong\nDate: 12/03/2025\nSeat 4A."
        self.assertEqual(c.clean_email(travel, "Alex Citizen")["quoted"], [])

    def test_header_with_labels_on_their_own_lines_is_still_quoted(self):
        body = ("Noted.\n\nFrom:\nSam Builder <sam@example-builders.com.au>\nSent:\nMonday, 3 March 2025 9:00 AM\n"
                "To: Alex Citizen <alex@example-consulting.com>\nSubject: Pour\n\nPour is booked.\n")
        got = c.clean_email(body, "Alex Citizen")
        self.assertEqual(got["text"], "Noted.")
        self.assertEqual(len(got["quoted"]), 1)
        self.assertEqual(got["quoted"][0]["date"], datetime(2025, 3, 3, 9, 0))
        self.assertEqual(got["quoted"][0]["sender_email"], "sam@example-builders.com.au")


class GreetingLineTests(unittest.TestCase):
    def test_answer_after_a_greeting_line_is_kept(self):
        self.assertEqual(clean("Hi Sam\n\nAgreed.\n\nCheers\nJane", "Jane Citizen"), "Hi Sam, Agreed.")
        text = clean("Hi Sam\n\nRejected, thanks.\n\nJane", "Jane Citizen")
        self.assertEqual(text, "Hi Sam, Rejected, thanks.")
        self.assertFalse(c.is_ack(c.strip_greeting(text)))
        self.assertFalse(c.is_ack_reply(text))

    def test_greeting_and_name_only_is_still_empty(self):
        for body in ("Hi Sam\n\nJane", "Hi all\n\nJane", "Hi Sam\n\nJane Citizen", "Hi Sam,\n\nKind regards\nJane"):
            self.assertEqual(clean(body, "Jane Citizen"), "", body)

    def test_thanks_after_a_greeting_line_is_still_an_ack(self):
        text = clean("Hi Sam\n\nThanks\n\nJane", "Jane Citizen")
        self.assertEqual(text, "Hi Sam, Thanks")
        self.assertTrue(c.is_ack_reply(text))
        self.assertFalse(c.is_ack_reply("Hi Sam Agreed, thanks"))


class SignatureAboveMessageTests(unittest.TestCase):
    def test_signature_block_above_the_greeting_is_removed(self):
        body = "Jane Citizen\nSenior Engineer\nM: 0400 111 222\n\nHi all,\nThe site visit is moved to Tuesday.\n"
        self.assertEqual(clean(body, "Jane Citizen"), "Hi all, The site visit is moved to Tuesday.")

    def test_heading_with_the_senders_name_is_kept(self):
        body = ("Jane Citizen - leave dates\n23 Dec 2025 to 6 Jan 2026 inclusive\nBackup: Pat Lee 0412 000 111\n\n"
                "Hi all,\nI will be on leave as above.\n")
        out = clean(body, "Jane Citizen")
        self.assertIn("23 Dec 2025 to 6 Jan 2026", out)
        self.assertIn("I will be on leave as above.", out)


class WrappedTextTests(unittest.TestCase):
    PARA = ("We have reviewed the revised culvert drawings and note that the headwall length on sheet 12 does not "
            "match the calculation package issued last week and the wingwall detail on sheet 14 is missing the "
            "reinforcement schedule. Please update the drawing so that it agrees with the calculations.")

    def test_hard_wrapped_paragraph_gets_no_bullets(self):
        import textwrap
        for width in (60, 66):
            body = "Hi Sam,\n\n" + textwrap.fill(self.PARA, width) + "\n\nRegards\nAlex"
            out = clean(body)
            self.assertNotIn("\u2022", out, width)
            self.assertIn("note that the headwall length on sheet 12 does not match", out)

    def test_short_items_still_become_a_list(self):
        self.assertIn("Drawings issued \u2022 Calcs pending \u2022 Site visit Tuesday",
                      clean("Status\nDrawings issued\nCalcs pending\nSite visit Tuesday\n"))
        self.assertIn("Status: \u2022 Drawings issued \u2022 Calcs pending", clean("Status:\nDrawings issued\nCalcs pending\n"))


class SpeedTests(unittest.TestCase):
    def test_long_label_value_paragraph_is_fast(self):
        rows = "\n".join("Document %d:\nDrawing C-%04d Rev %d" % (i, i, i % 5) for i in range(1000))
        start = time.time()
        out = clean("Transmittal register:\n" + rows + "\n")
        self.assertLess(time.time() - start, 2)
        self.assertIn("Drawing C-0999 Rev 4", out)
        self.assertIn("Document 500:", out)

    def test_many_links_on_one_line_are_fast(self):
        text = " ".join("see item %d <https://example.org/item/%d>" % (i, i) for i in range(3000))
        start = time.time()
        out = c.replace_links(text)
        self.assertLess(time.time() - start, 2)
        self.assertEqual(out.count("<link>"), 3000)
        self.assertEqual(c.replace_links("www.example.org <https://www.example.org/x> and Report <https://x.org/r>"),
                         "example.org and Report <link>")


class TeamsChatTests(unittest.TestCase):
    BODY = ("Can you check the pour sequence?\n\nHi,\n\tAlex sent a message in chat <https://teams.example/x>\n"
            "Can you check the pour sequence?\n\t<https://teams.example/y>\nReply in Teams <https://teams.example/z>\n"
            "Install Microsoft Teams now\nThis email was sent from an unmonitored mailbox.\n")

    def test_teams_chat_is_not_noise(self):
        r = {"sender_name": "Alex Citizen in Teams", "sender_email": "no-reply@teams.mail.microsoft",
             "subject": "Alex Citizen sent a message", "item_class": "IPM.Note"}
        self.assertTrue(c.is_teams_chat(r))
        self.assertEqual(c.noise_kind(r), "")
        r = {"sender_name": "Teams", "sender_email": "no-reply@teams.mail.microsoft",
             "subject": "You have new messages", "item_class": "IPM.Note"}
        self.assertFalse(c.is_teams_chat(r))
        self.assertEqual(c.noise_kind(r), "notifications")

    def test_chat_messages_are_read_without_the_footer(self):
        self.assertEqual(c.teams_chat_messages(self.BODY), [("Alex", "Can you check the pour sequence?")])
        two = ("Hi,\n\tJo + 1 sent a message in Site chat <https://teams.example/x>\nPour is at 6am.\n\n"
               "\tSam sent a message in Site chat <https://teams.example/x>\nPump arrives 5:30.\n"
               "Reply in Teams <https://teams.example/z>\n")
        self.assertEqual(c.teams_chat_messages(two), [("Jo", "Pour is at 6am."), ("Sam", "Pump arrives 5:30.")])


class FactPiecesTests(unittest.TestCase):
    def test_sentence_numbers(self):
        text = ("Pad footings founded in the stiff natural clay below the fill may be designed for an allowable "
                "bearing pressure of 150 kPa, provided the bases are clean and dry, and the excavations are "
                "inspected by the geotechnical engineer before 12 May. The site is level.")
        plain = c.fact_pieces(text, 60)
        spans, numbers = c.fact_pieces(text, 60, sentences=True)
        self.assertEqual(spans, plain)
        self.assertEqual(len(numbers), len(spans))
        self.assertGreater(len(spans), 2)                   # the long sentence was cut at its clauses
        self.assertEqual(numbers[0], numbers[1])            # ... so its clauses share a number
        self.assertNotEqual(numbers[-1], numbers[0])        # 'The site is level.' is another sentence
        self.assertEqual(c.fact_pieces("", 60, sentences=True), ([], []))
        self.assertEqual(c.fact_pieces("", 60), [])


class CapFactTests(unittest.TestCase):
    FILLER = ("We had a good meeting with the team on site. The weather held up for most of the day. "
              "Everyone was briefed on the plan. The site is tidy and access is good. ")

    def test_long_sentence_with_totals_is_kept(self):
        total = ("The claim for this period covering the concept and detailed design stages comes to $64,250.00, "
                 "which takes the amount claimed so far to $151,780.00 against the agreed contract sum of "
                 "$187,400.00, so $35,620.00 is still to be claimed for the construction support stage "
                 "and the final reporting.")
        out = c.cap_text(self.FILLER * 2 + total + " " + self.FILLER, 550)
        self.assertIn("$64,250.00", out)
        self.assertLessEqual(len(out), 552)

    def test_a_lists_intro_line_is_kept(self):
        items = ["Lodge the signed form naming the principal contractor for the job",
                 "Send the written approval of the owners next door for the haul road",
                 "Send the traffic control plan stamped by the road manager for the job",
                 "Send the erosion and sediment control plan for the laydown area",
                 "Send the certificate of currency for the contractor insurance policy",
                 "Send the building approval issued for the detention basin works",
                 "Send the condition survey of the old fence along the eastern line",
                 "A tree protection plan is required for the two figs near the gate by Friday",
                 "A signed set-out plan is required from the licensed surveyor by Friday",
                 "An inspection and test plan is required for the pipe laying works by Friday",
                 "A work method statement is required for the deep excavation works by Friday",
                 "Contact details are required for the site supervisor after hours by Friday"]
        text = ("Hi Sam, Thank you for your email. Listed below are the items we still need before the pre-start "
                "meeting under the permit conditions: \u2022 " + " \u2022 ".join(items) +
                " \u2022 Could you please send the above to us.")
        out = c.cap_text(text, 550)
        self.assertIn("Listed below are the items we still need", out)
        self.assertLessEqual(len(out), 552)

    def test_figure_rows_without_sentence_ends_are_kept(self):
        text = ("Hi Pat, please see the monthly claim summary for the project below as discussed at the meeting "
                "on site last week with the builder and the client. \u2022 Stage A design \u2022 complete "
                "\u2022 Stage B design \u2022 in progress \u2022 Stage C documentation \u2022 not started "
                "\u2022 Stage D documentation \u2022 not started \u2022 Stage E reporting \u2022 not started "
                "\u2022 No claim was made for stage B last month \u2022 The site works are on hold until the permit "
                "\u2022 Claim for this month = $64,250.00 This takes the claimed total = $151,780.00 "
                "Agreed contract sum is $187,400.00 Leaves $35,620.00 to claim for the remaining stages "
                "of the project and the final reports \u2022 Stage A work in progress is $27,115.00")
        out = c.cap_text(text, 550)
        self.assertIn("$64,250.00", out)
        self.assertLessEqual(len(out), 552)

    def test_facts_in_a_long_run_on_sentence_are_kept(self):
        run_on = ("The contractor has proposed an alternative culvert arrangement using precast units instead of the "
                  "cast in place box section shown on the tender drawings, and they advise that this would reduce the "
                  "time needed on site by around three weeks, and that it would avoid the need for the temporary "
                  "diversion channel that was causing concern with the landowner, which in turn would mean a saving "
                  "of $48,000 to the client on the current estimate, provided we approve the change by Friday "
                  "14 March so that the precaster can lock in the casting bed for the units.")
        text = "Hi Jane, Quick one on the culvert. " + run_on + " Can you confirm?"
        for limit in (550, 250):
            out = c.cap_text(text, limit)
            self.assertIn("$48,000", out, limit)
            self.assertIn("Friday 14 March", out, limit)
            self.assertLessEqual(len(out), limit + 2)
        self.assertGreaterEqual(len(c.cap_text(text, 550)), 300)

    def test_requests_after_a_long_plain_sentence_are_kept_in_order(self):
        plain = ("The team had a long discussion about the general approach to the design of the outlet "
                 "structure and how it might look once the landscaping around it has been completed and the "
                 "planting has had a chance to grow in over the coming seasons, which everyone agreed was "
                 "worth thinking about early on in the process rather than leaving until later. ") * 2
        text = ("Hi Jane, Notes from today. " + plain + "Please send the updated outlet levels. "
                "Can you confirm the pipe class? We will issue the drawings next week.")
        out = c.cap_text(text, 550)
        self.assertLessEqual(len(out), 552)
        self.assertIn(" \u2026 ", out)
        for bit in ("Please send the updated outlet levels.", "Can you confirm the pipe class?",
                    "We will issue the drawings next week."):
            self.assertIn(bit, out)
        self.assertLess(out.index("Please send"), out.index("Can you confirm"))

    def test_never_over_the_limit_and_in_order(self):
        import random
        rnd = random.Random(7)
        words = ["slab", "pour", "$12,500", "RFI 12", "Friday 14 March", "300mm", "please", "and", "which",
                 "the", "pit", "level", "Can you confirm?", "drawing C-101", "\u2022", ",", "."]
        for n in range(200):
            text = " ".join(rnd.choice(words) for _ in range(rnd.randint(50, 400)))
            for limit in (250, 550, 1500):
                out = c.cap_text(text, limit)
                self.assertLessEqual(len(out), limit + 2)
                pos = 0
                for piece in out.rstrip(" \u2026").split(" \u2026 "):
                    piece = piece.strip(" ,;:-")
                    at = text.find(piece, pos)
                    self.assertGreaterEqual(at, 0, piece)
                    pos = at + len(piece)

    def test_hold_point_is_kept(self):
        text = ("Hi Bob, notes from the pre-pour inspection of the level 2 slab this morning. " + self.FILLER * 2
                + "Please tidy the laydown area near the gate. Please send the updated pour sequence. The site team "
                "would like to pour regardless and sort the paperwork out later. No further pours are to go ahead "
                "until the mix design has been submitted and reviewed. " + self.FILLER
                + "Please check the edge form set-out on the east side. Please keep the access path clear. "
                + self.FILLER)
        out = c.cap_text(text, 500)
        self.assertIn("No further pours are to go ahead", out)
        self.assertLessEqual(len(out), 502)

    def test_short_list_items_keep_their_outcome(self):
        items = []
        for n in range(1, 31):
            outcome = "Accepted." if n % 2 else "Rejected, resubmit with calcs."
            items.append("RFI-%d, slab edge detail at grid %d. %s" % (100 + n, n, outcome))
        text = "Hi team, the RFI register as of today: \u2022 " + " \u2022 ".join(items) + " \u2022 Thanks, Jo"
        out = c.cap_text(text, 1500)
        self.assertLessEqual(len(out), 1502)
        self.assertNotRegex(out, r"grid \d+\. \u2026")       # no kept item without its outcome
        self.assertRegex(out, r"grid \d+\. Rejected, resubmit with calcs\.")

    def test_a_sentence_that_refers_back_keeps_its_subject(self):
        text = ("Hi Sam, notes on the claim. " + self.FILLER * 3
                + "The drafter has spent a lot of time on the culvert headwall design over the summer. "
                "This brings the total to 173 hours, which at $200/hr equals $34,600. " + self.FILLER * 2)
        out = c.cap_text(text, 500)
        self.assertIn("The drafter has spent a lot of time on the culvert headwall design over the summer. "
                      "This brings the total to 173 hours", out)
        self.assertLessEqual(len(out), 502)

    def test_sentences_the_sender_already_showed_are_dropped_first(self):
        text = (self.FILLER * 3 + "The contractor is responsible for coordinating the shop drawings. ") * 2
        self.assertEqual(c.cap_text(text, 550), c.cap_text(text, 550, ""))
        old = ("Hi Jo, the plan for the week. " + self.FILLER + "Formwork for the east wall goes up on Tuesday "
               "morning. The crane is booked for the precast panels on Wednesday. The pump is booked for Thursday "
               "at 6am sharp. ")
        new = old + ("The contractor is responsible for coordinating the shop drawings with the precaster. "
                     "Any delay to the shop drawings is at the contractor's cost.")
        out = c.cap_text(new, 330, seen="Earlier: " + old)
        self.assertIn("Any delay to the shop drawings is at the contractor's cost.", out)
        self.assertIn("The contractor is responsible for coordinating the shop drawings", out)
        self.assertNotIn("The crane is booked", out)               # shown just above, not in the opening
        self.assertIn(" \u2026 ", out)
        self.assertTrue(out.startswith("Hi Jo, the plan for the week."))
        self.assertLessEqual(len(out), 332)


class ContactsGivenOnPurposeTests(unittest.TestCase):
    JO_SIG = "Jo Planner\nSenior Planner\nM: 0400 111 222\nE: jo.planner@example-consulting.com\n"

    def test_contact_details_passed_on_are_kept(self):
        body = ("Hi Sam,\n\nPat is the case manager at Council. Please see their details below.\n\n"
                "Pat Lee | Senior Engineer\n\nPlanning Department\n\nT| 07 5550 1234\n\nE| pat.lee@council.example\n\n"
                "Thanks,\nJo\n\n" + self.JO_SIG)
        out = clean(body, "Jo Planner")
        self.assertIn("T| 07 5550 1234", out)
        self.assertIn("E| pat.lee@council.example", out)
        self.assertNotIn("0400 111 222", out)

    def test_a_body_of_addresses_is_kept(self):
        out = clean("Hi Sam,\n\npat.lee@council.example\nkim.ng@council.example\n\nRegards\n" + self.JO_SIG,
                    "Jo Planner")
        self.assertIn("pat.lee@council.example", out)
        self.assertIn("kim.ng@council.example", out)
        self.assertNotIn("0400", out)

    def test_own_details_below_are_still_a_signature(self):
        out = clean("Hi Sam,\n\nHappy to help - contact me on the details below.\n\n" + self.JO_SIG, "Jo Planner")
        self.assertEqual(out, "Hi Sam, Happy to help - contact me on the details below.")

    def test_own_address_after_the_signoff_is_removed(self):
        out = clean("Hi Sam,\n\nThe report is attached.\n\nCheers\nAlex\nalex@example.com\n")
        self.assertEqual(out, "Hi Sam, The report is attached.")


class OutlookTableTests(unittest.TestCase):
    def test_sentence_cells_after_a_header_row_are_cells(self):
        body = ('Updated table below:\n\nNo\n\nItem\n\nComment\n\n1\n\nSlab pour 2\n\nNot inspected by us.\n\n2\n\n'
                'Slab pour 3\n\nAttached under "2".\n\n')
        self.assertIn('Not inspected by us. \u2022 2 \u2022 Slab pour 3 \u2022 Attached under "2".', clean(body))

    def test_hard_wrapped_text_after_a_short_list_stays_prose(self):
        body = ("Items:\nSlab\nWall\nRoof\nThe contractor has asked whether the method statement for the deep\n"
                "excavation can be resubmitted after work starts on site next week, as discussed today.\n")
        self.assertIn("for the deep excavation can be resubmitted", clean(body))

    def test_a_long_paragraph_after_the_table_stays_prose(self):
        long_para = ("The pour sequence was discussed at length with the contractor and the client, and everyone "
                     "agreed that the east slab goes first, followed by the west slab once the cores have been "
                     "tested and the results have been reviewed by the engineer.")
        body = "Status:\n\nTask\n\nOwner\n\nDue\n\nPour 1\n\nBuilder\n\nMonday\n\n" + long_para + "\n"
        out = clean(body)
        self.assertIn("\u2022 Monday " + long_para[:30], out)
        self.assertNotIn("\u2022 The pour sequence", out)

    def test_wrapped_lines_each_their_own_paragraph_stay_prose(self):
        body = ("Contacts:\n\nAlex - attached\n\nJo - details below\n\nKim - by phone\n\n"
                "He has long site supervision experience, which has given him a good working\n\n"
                "knowledge of the council drainage standards and the paperwork that is needed to\n\n"
                "meet the programme dates.\n")
        out = clean(body)
        self.assertIn("a good working knowledge of the council drainage standards", out)


class LinkAndPhotoNameTests(unittest.TestCase):
    def test_links_to_a_bare_homepage_are_dropped(self):
        self.assertEqual(c.replace_links("Visit <https://example.com/> today"), "Visit today")
        self.assertEqual(c.replace_links("see <https://example.com/doc?id=1>"), "see <link>")
        self.assertEqual(c.replace_links("See here   <https://x.com/a>"), "See here <link>")

    def test_pasted_images_with_a_timestamp_are_camera_photos(self):
        self.assertTrue(c.is_camera_photo("Image - 2026-01-02T101010.123.jpg"))
        self.assertTrue(c.is_camera_photo("Image - 2026-01-02 101010.jpg"))

    def test_a_long_run_of_tabs_is_fast(self):
        text = "a" + "\t" * 200000 + "b"
        start = time.time()
        self.assertEqual(c.replace_links(text), text)
        self.assertLess(time.time() - start, 2)


class OnWroteInTheTextTests(unittest.TestCase):
    def test_on_date_x_wrote_in_the_senders_own_text_is_kept(self):
        body = ("Hi Sam,\nOn 3 March the contractor wrote:\n\"The slab has cracked.\"\n"
                "We propose epoxy injection at $12,000.\nPlease approve by Friday.")
        got = c.clean_email(body, "Alex Citizen")
        self.assertIn("$12,000", got["text"])
        self.assertIn("approve by Friday", got["text"])
        self.assertEqual(got["quoted"], [])

    def test_wrapped_on_line_is_kept(self):
        got = c.clean_email("On 5 March we inspected the slab.\nAs the geotech wrote:\nbearing is fine.\n"
                            "Please issue drawings.", "Alex Citizen")
        self.assertIn("Please issue drawings.", got["text"])
        self.assertIn("bearing is fine.", got["text"])


class PlaceholderDateTests(unittest.TestCase):
    def test_placeholder_years_are_not_dates(self):
        for text in ("0001-01-01 00:00", "Friday, 31 December 9999 11:59 PM", "Fri, 31 Dec 9999 23:59:00 +0000",
                     "Monday, 1 January 0001 12:00 AM"):
            self.assertIsNone(c.parse_loose_date(text), text)
        self.assertEqual(c.parse_loose_date("Mon, 3 Mar 2025 09:15:30 +1000"), datetime(2025, 3, 3, 9, 15))


class OneLineReplyTests(unittest.TestCase):
    def test_one_line_answers_after_a_greeting_are_kept(self):
        for body in ("Hi Sam Rejected.", "Hi Sam Agreed", "Hi Pat Friday.", "Hi Sam Option B.",
                     "Hi Sam Rejected. Resubmit."):
            self.assertEqual(clean(body, "Jane Citizen"), body)
        self.assertEqual(clean("Hi Sam Rejected.\n\nJane", "Jane Citizen"), "Hi Sam Rejected.")
        self.assertEqual(c.strip_greeting("Hi Sam Rejected."), "Rejected.")
        self.assertFalse(c.is_ack_reply("Hi Sam Rejected."))

    def test_greetings_alone_are_still_empty(self):
        for body in ("Dear Mr Smith", "Hi Sam and Jo", "Good morning Sam", "Morning all"):
            self.assertEqual(clean(body, "Jane Citizen"), "", body)


class SenderNameWithDigitTests(unittest.TestCase):
    def test_from_name_with_a_digit_starts_a_quoted_email(self):
        body = ("Thanks, noted.\n\nFrom: Level 3 Reception\nSent: Monday, 3 March 2025 9:15 AM\nTo: Sam Brown\n"
                "Subject: Parking\n\nYour parking permit for bay 12 is ready, $150.\n")
        got = c.clean_email(body, "Sam Brown")
        self.assertEqual(got["text"], "Thanks, noted.")
        self.assertEqual(len(got["quoted"]), 1)
        self.assertEqual(got["quoted"][0]["sender_name"], "Level 3 Reception")
        for value in ("7:00am", "12/03/2025", "0900", "Tuesday 18 March", "18 March"):
            self.assertFalse(c._could_be_sender(value), value)
        for value in ("Crew 2 Supervisor", "3D Visuals", "Monash 2 Team"):
            self.assertTrue(c._could_be_sender(value), value)


class GreaterThanValueTests(unittest.TestCase):
    def test_values_starting_with_a_greater_than_sign_are_kept(self):
        out = clean("Test results for the subgrade:\n>95% compaction achieved on all lots\n"
                    "> 600 mm cover required over the main\n<5 kPa settlement")
        self.assertIn(">95% compaction", out)
        self.assertIn("> 600 mm cover", out)

    def test_quoted_lines_are_still_dropped(self):
        self.assertEqual(clean("Agreed.\n> I think we should\n> use the 300 pipe\n>> earlier text"), "Agreed.")

    def test_a_quoted_value_keeps_its_sign(self):
        q = c.parse_quoted("From: Sam Builder <sam@example-builders.com.au>\nSent: Monday, 3 March 2025 9:15 AM\n"
                           "To: Alex\nSubject: Tests\n\n>95% compaction on lot 4\n")
        self.assertIn(">95% compaction", q[0]["body"])


class PostscriptTests(unittest.TestCase):
    def test_ps_after_a_bare_thanks_is_kept(self):
        out = clean("Hi Sam,\n\nPlease find attached the revised drawings.\n\nThanks\n\nPS: crane booked for 14 March.\n",
                    "Jane Citizen")
        self.assertIn("PS: crane booked for 14 March.", out)

    def test_note_after_signoff_and_name_is_kept(self):
        out = clean("Hi Sam,\n\nRevised drawings attached.\n\nCheers\nJane\n\nNote: the footing is now 900 mm deep.\n",
                    "Jane Citizen")
        self.assertIn("Note: the footing is now 900 mm deep.", out)
        self.assertNotIn("Cheers", out)

    def test_ps_after_a_full_signature_is_kept(self):
        sig = "Jane Citizen\nSenior Engineer\nM: 0400 111 222\nE: jane@example.com\nExample Pty Ltd\n"
        out = clean("Hi Sam,\n\nRevised drawings attached.\n\nKind regards\n\n" + sig +
                    "\nPS - the crane is booked for 14 March, cost $4,500.\n", "Jane Citizen")
        self.assertEqual(out, "Hi Sam, Revised drawings attached. PS - the crane is booked for 14 March, cost $4,500.")

    def test_signature_without_a_ps_is_still_removed(self):
        sig = "Jane Citizen\nSenior Engineer\nM: 0400 111 222\nE: jane@example.com\nExample Pty Ltd\n"
        self.assertEqual(clean("Hi Sam,\n\nRevised drawings attached.\n\nKind regards\n\n" + sig +
                               "Note: my working days are Monday to Thursday\n", "Jane Citizen"),
                         "Hi Sam, Revised drawings attached.")


class QuotedInviteTests(unittest.TestCase):
    def test_where_is_read(self):
        q = c.parse_quoted("From: Sam Builder <sam@example-builders.com.au>\nSent: Monday, 3 March 2025 9:15 AM\n"
                           "To: Alex\nSubject: Site walk\nWhen: Tuesday, 4 March 2025 10:00 AM-11:00 AM\n"
                           "Where: Site office, 1 Example St\n\nSee you there.\n")
        self.assertEqual(q[0]["where"], "Site office, 1 Example St")
        self.assertTrue(q[0]["when"].startswith("Tuesday, 4 March 2025"))


if __name__ == "__main__":
    unittest.main()
