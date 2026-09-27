# test_notifications.py
# The student who is called — and only that student — gets a "go to counter N" email,
# recorded in the notifications collection.

from unittest import mock

from ..Handlers import notifications
from django.test import override_settings

from .base import STAFF_EMAIL, TOMORROW, QueueTestCase


class CalledEmailTests(QueueTestCase):
    def test_called_student_gets_an_email_with_token_and_counter(self):
        student = self.register(name="Aditi Rao", email="aditi@examcell.test")
        ticket = self.take_token(key=student["studentKey"], service="revaluation").json()["ticket"]
        self.staff_post("/counters/2/call-next/")
        self.assertEqual(len(self.sent_called_emails), 1)
        email = self.sent_called_emails[0]
        self.assertEqual(email["recipient"], "aditi@examcell.test")
        self.assertEqual(email["name"], "Aditi Rao")
        self.assertEqual((email["token"], email["counter_id"]), (ticket["token"], 2))
        self.assertEqual(email["service"], "Revaluation & Photocopy")
        self.assertFalse(email["transferred"])

    def test_only_the_called_student_is_emailed(self):
        first = self.register(email="first@examcell.test")
        second = self.register(email="second@examcell.test")
        self.take_token(key=first["studentKey"])
        self.take_token(key=second["studentKey"])
        self.staff_post("/counters/1/call-next/")
        self.assertEqual([e["recipient"] for e in self.sent_called_emails], ["first@examcell.test"])

    def test_each_call_is_recorded(self):
        self.take_token()
        self.staff_post("/counters/1/call-next/")
        record = self.db.notifications.find_one({"type": "called"})
        self.assertEqual((record["type"], record["status"], record["counter"]), ("called", "sent", 1))
        # The notification record holds no personal data — just the ticket reference.
        self.assertNotIn("examcell.test", str(record))

    def test_no_email_when_nobody_is_waiting(self):
        self.staff_post("/counters/1/call-next/")
        self.assertEqual(self.sent_called_emails, [])

    def test_transfer_to_free_counter_emails_the_new_counter(self):
        self.take_token()
        self.staff_post("/counters/1/call-next/")
        self.staff_post("/counters/1/transfer/", {"to_counter": 3})
        self.assertEqual(len(self.sent_called_emails), 2)
        self.assertEqual((self.sent_called_emails[1]["counter_id"], self.sent_called_emails[1]["transferred"]), (3, True))

    def test_requeue_on_busy_counter_sends_nothing(self):
        self.take_token()
        self.take_token()
        self.staff_post("/counters/1/call-next/")
        self.staff_post("/counters/2/call-next/")
        self.staff_post("/counters/1/transfer/", {"to_counter": 2})
        self.assertEqual(len(self.sent_called_emails), 2)  # only the two original calls

    def test_email_failure_is_recorded_and_does_not_break_the_call(self):
        self.take_token()
        with mock.patch.object(notifications, "send_called_email", return_value=False):
            response = self.staff_post("/counters/1/call-next/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db.notifications.find_one({"type": "called"})["status"], "failed")

    def test_email_crash_does_not_break_the_call(self):
        self.take_token()
        with mock.patch.object(notifications, "send_called_email", side_effect=RuntimeError("smtp down")):
            response = self.staff_post("/counters/1/call-next/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db.notifications.find_one({"type": "called"})["status"], "failed")

    def test_demo_accounts_are_not_called_by_email(self):
        from ..Handlers.auth_handler import create_active_account
        demo = create_active_account("student", "Demo Student", "demo1@examcell.local")  # no password -> "seed"
        self.take_token(key=self._handle(demo, "student"))
        self.staff_post("/counters/1/call-next/")
        self.assertEqual(self.sent_called_emails, [])
        self.assertEqual(self.db.notifications.find_one({"type": "called"})["status"], "skipped")


@override_settings(STAFF_EMAILS={STAFF_EMAIL, "second.staff@examcell.test"})
class StaffNewTicketEmailTests(QueueTestCase):
    def test_every_staff_member_is_emailed_when_a_student_joins(self):
        ticket = self.take_token(student="Aditi Rao", service="duplicate").json()["ticket"]
        self.assertEqual(sorted(e["recipient"] for e in self.sent_staff_emails),
                         sorted([STAFF_EMAIL, "second.staff@examcell.test"]))
        email = self.sent_staff_emails[0]
        self.assertEqual((email["token"], email["student"], email["roll"]), (ticket["token"], "Aditi Rao", ticket["roll"]))
        self.assertEqual(email["service"], "Duplicate Marksheet / Transcript")
        self.assertEqual((email["mode"], email["position"], email["waiting_total"]), ("walk-in", 0, 1))

    def test_email_carries_live_position_and_wait(self):
        for _ in range(4):
            self.take_token()
        latest = self.sent_staff_emails[-1]
        self.assertEqual((latest["position"], latest["est_wait_min"], latest["waiting_total"]), (3, 5, 4))

    def test_appointment_booking_email_has_date_and_slot(self):
        self.book("2:00 PM", date=TOMORROW)
        email = self.sent_staff_emails[0]
        self.assertEqual((email["mode"], email["date"], email["slot"]), ("appointment", TOMORROW, "2:00 PM"))

    def test_rejected_ticket_sends_nothing(self):
        self.take_token(service="hacking")
        self.assertEqual(self.sent_staff_emails, [])

    def test_recorded_once_per_ticket_without_staff_addresses(self):
        self.take_token()
        record = self.db.notifications.find_one({"type": "new_ticket_staff"})
        self.assertEqual((record["status"], record["detail"]), ("sent", "2/2 staff emailed"))
        self.assertNotIn("examcell.test", str(record))

    def test_staff_email_failure_does_not_block_the_student(self):
        with mock.patch.object(notifications, "send_new_ticket_staff_email", side_effect=RuntimeError("down")):
            response = self.take_token()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.db.notifications.find_one({"type": "new_ticket_staff"})["status"], "failed")

    def test_can_be_switched_off(self):
        with override_settings(STAFF_NEW_TICKET_EMAILS=False):
            self.take_token()
        self.assertEqual(self.sent_staff_emails, [])
