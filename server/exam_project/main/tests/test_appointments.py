# test_appointments.py
# Staff appointment management: listing bookings for any date with slot occupancy, and
# cancelling with a reason (slot freed, student emailed, audit-logged).

from django.test import override_settings

from .base import TODAY, TOMORROW, QueueTestCase


class AppointmentListTests(QueueTestCase):
    def test_lists_a_future_days_bookings_in_slot_order_with_full_details(self):
        late = self.book("2:00 PM", date=TOMORROW, student="Rohan Gupta").json()["ticket"]
        early = self.book("10:30 AM", date=TOMORROW, student="Meera Iyer").json()["ticket"]
        body = self.api_get(f"/appointments/?date={TOMORROW}").json()
        self.assertFalse(body["isToday"])
        self.assertEqual([a["id"] for a in body["appointments"]], [early["id"], late["id"]])
        first = body["appointments"][0]
        self.assertEqual((first["student"], first["slot"], first["status"]), ("Meera Iyer", "10:30 AM", "waiting"))
        self.assertIsNotNone(first["bookedAt"])

    def test_walk_ins_are_not_listed(self):
        self.take_token()
        self.book("11:30 AM")
        body = self.api_get(f"/appointments/?date={TODAY}").json()
        self.assertEqual(len(body["appointments"]), 1)
        self.assertTrue(body["isToday"])

    @override_settings(SLOT_CAPACITY=2)
    def test_slot_occupancy(self):
        self.book("10:30 AM", date=TOMORROW)
        self.book("10:30 AM", date=TOMORROW)
        slots = {s["slot"]: s for s in self.api_get(f"/appointments/?date={TOMORROW}").json()["slots"]}
        self.assertEqual((slots["10:30 AM"]["booked"], slots["10:30 AM"]["capacity"]), (2, 2))
        self.assertEqual(slots["10:45 AM"]["booked"], 0)

    def test_todays_appointments_carry_the_live_forecast(self):
        self.book("1:00 PM")
        appt = self.api_get(f"/appointments/?date={TODAY}").json()["appointments"][0]
        self.assertEqual(appt["estWaitMin"], 115)

    def test_status_counts_include_served_and_cancelled(self):
        self.book("11:15 AM")
        other = self.book("11:30 AM").json()
        self.cancel(other)
        self.staff_post("/counters/1/call-next/")
        counts = self.api_get(f"/appointments/?date={TODAY}").json()["counts"]
        self.assertEqual(counts, {"serving": 1, "cancelled": 1})

    def test_date_is_validated(self):
        self.assertEqual(self.api_get("/appointments/?date=nonsense").status_code, 400)
        self.assertEqual(self.api_get("/appointments/?date=2029-01-01").status_code, 400)

    def test_students_cannot_see_the_appointment_list(self):
        student = self.register()["studentKey"]
        self.assertEqual(self.api_get(f"/appointments/?date={TODAY}", student).status_code, 403)


class StaffCancelTests(QueueTestCase):
    def cancel_as_staff(self, ticket_id, reason="Exam cell closed for inspection"):
        return self.staff_post(f"/appointments/{ticket_id}/cancel/", {"reason": reason})

    @override_settings(SLOT_CAPACITY=1)
    def test_cancel_frees_the_slot_emails_the_student_and_is_logged(self):
        student = self.register(name="Aditi Rao", email="aditi@examcell.test")
        ticket = self.book("10:30 AM", date=TOMORROW, key=student["studentKey"]).json()["ticket"]
        self.assertEqual(self.book("10:30 AM", date=TOMORROW).status_code, 409)  # full

        self.assertEqual(self.cancel_as_staff(ticket["id"]).status_code, 200)
        self.assertEqual(self.book("10:30 AM", date=TOMORROW).status_code, 201)  # freed

        email = self.sent_cancel_emails[0]
        self.assertEqual((email["recipient"], email["token"], email["slot"]), ("aditi@examcell.test", ticket["token"], "10:30 AM"))
        self.assertEqual(email["reason"], "Exam cell closed for inspection")

        doc = self.db.tickets.find_one({"id": ticket["id"]})
        self.assertEqual((doc["status"], doc["close_reason"], doc["cancel_note"]), ("cancelled", "staff", "Exam cell closed for inspection"))
        event = self.db.ticket_events.find_one({"ticket_id": ticket["id"], "action": "cancelled"})
        self.assertEqual(event["by"], "staff")

    def test_student_can_book_again_after_staff_cancel(self):
        key = self.register()["studentKey"]
        ticket = self.book("10:30 AM", date=TOMORROW, key=key).json()["ticket"]
        self.cancel_as_staff(ticket["id"])
        self.assertEqual(self.book("11:30 AM", date=TOMORROW, key=key).status_code, 201)

    def test_reason_is_required(self):
        ticket = self.book("10:30 AM", date=TOMORROW).json()["ticket"]
        self.assertEqual(self.cancel_as_staff(ticket["id"], reason="").status_code, 400)
        self.assertEqual(self.cancel_as_staff(ticket["id"], reason="x" * 300).status_code, 400)

    def test_cannot_cancel_once_served_or_twice(self):
        ticket = self.book("11:15 AM").json()["ticket"]
        self.staff_post("/counters/1/call-next/")
        self.assertEqual(self.cancel_as_staff(ticket["id"]).status_code, 409)
        other = self.book("2:00 PM", date=TOMORROW).json()["ticket"]
        self.cancel_as_staff(other["id"])
        self.assertEqual(self.cancel_as_staff(other["id"]).status_code, 409)

    def test_walk_in_tokens_cannot_be_cancelled_here(self):
        ticket = self.take_token().json()["ticket"]
        self.assertEqual(self.cancel_as_staff(ticket["id"]).status_code, 404)

    def test_students_cannot_use_staff_cancel(self):
        ticket = self.book("10:30 AM", date=TOMORROW).json()["ticket"]
        student = self.register()["studentKey"]
        response = self.client.post(f"/api/v1/main/appointments/{ticket['id']}/cancel/", {"reason": "nope"},
                                    format="json", **self.bearer(student))
        self.assertEqual(response.status_code, 403)
