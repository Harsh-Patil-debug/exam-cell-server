# test_tickets.py
# Student-side flow: taking a token, validation, duplicate prevention, and cancelling
# with the cancel code.

from .base import API, TODAY, QueueTestCase


class TakeTokenTests(QueueTestCase):
    def test_walk_in_token_is_issued_in_sequence(self):
        first = self.take_token().json()
        second = self.take_token(student="Meera Iyer").json()
        self.assertEqual(first["ticket"]["token"], "TK-101")
        self.assertEqual(second["ticket"]["token"], "TK-102")
        self.assertEqual(first["ticket"]["status"], "waiting")
        self.assertEqual(first["ticket"]["date"], TODAY)

    def test_new_ticket_reports_its_position_and_estimate(self):
        self.take_token()
        ticket = self.take_token().json()["ticket"]
        self.assertEqual(ticket["position"], 1)
        self.assertIsNotNone(ticket["estWaitMin"])

    def test_walk_in_ignores_any_slot_or_date_sent(self):
        ticket = self.take_token(slot="11:30 AM", date="2030-01-20").json()["ticket"]
        self.assertIsNone(ticket["slot"])
        self.assertEqual(ticket["date"], TODAY)

    def test_rejects_unknown_service_and_mode(self):
        self.assertEqual(self.take_token(service="hacking").status_code, 400)
        self.assertEqual(self.take_token(mode="vip").status_code, 400)

    def test_rejects_nosql_payloads(self):
        student = self.register()
        response = self.client.post(f"{API}/tickets/", {"service": {"$ne": None}}, format="json",
                                    **self.bearer(student["studentKey"]))
        self.assertEqual(response.status_code, 400)

    def test_name_and_roll_come_from_the_account_not_the_request(self):
        student = self.register(name="Aditi Rao")
        response = self.client.post(f"{API}/tickets/", {
            "service": "hall-ticket", "mode": "walk-in", "student": "Someone Else", "roll": "HACKED01",
        }, format="json", **self.bearer(student["studentKey"]))
        ticket = response.json()["ticket"]
        self.assertEqual((ticket["student"], ticket["roll"]), ("Aditi Rao", student["roll"]))

    def test_student_name_is_encrypted_in_the_ticket_document(self):
        self.take_token(student="Aditi Rao")
        doc = self.db.tickets.find_one({})
        self.assertNotIn("Aditi", str(doc))
        self.assertTrue(doc["student_enc"].startswith("v1:"))

    def test_staff_cannot_take_tokens(self):
        response = self.client.post(f"{API}/tickets/", {"service": "hall-ticket", "mode": "walk-in"},
                                    format="json", **self.staff_header())
        self.assertEqual(response.status_code, 403)

    def test_must_be_logged_in_to_take_a_token(self):
        response = self.client.post(f"{API}/tickets/", {"service": "hall-ticket", "mode": "walk-in"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_token_issuing_is_rate_limited(self):
        codes = [self.take_token().status_code for _ in range(12)]
        self.assertIn(429, codes)


class DuplicateTicketTests(QueueTestCase):
    def test_student_cannot_hold_two_open_tickets_on_one_day(self):
        key = self.register()["studentKey"]
        self.assertEqual(self.take_token(key=key).status_code, 201)
        response = self.take_token(key=key, service="revaluation")
        self.assertEqual(response.status_code, 409)
        self.assertIn("TK-101", response.json()["message"])

    def test_student_can_take_a_new_ticket_after_cancelling(self):
        key = self.register()["studentKey"]
        self.cancel(self.take_token(key=key).json())
        self.assertEqual(self.take_token(key=key).status_code, 201)

    def test_student_can_take_a_new_ticket_after_being_served(self):
        key = self.register()["studentKey"]
        self.take_token(key=key)
        self.staff_post("/counters/1/call-next/")
        self.staff_post("/counters/1/complete/")
        self.assertEqual(self.take_token(key=key).status_code, 201)

    def test_student_can_hold_a_ticket_today_and_an_appointment_another_day(self):
        key = self.register()["studentKey"]
        self.assertEqual(self.take_token(key=key).status_code, 201)
        self.assertEqual(self.book("11:30 AM", date="2030-01-16", key=key).status_code, 201)


class CancelTokenTests(QueueTestCase):
    def test_owner_can_cancel_and_ticket_leaves_the_queue(self):
        body = self.take_token().json()
        self.assertEqual(self.cancel(body).status_code, 200)
        self.assertEqual(self.state()["tickets"], [])
        detail = self.api_get(f"/tickets/{body['ticket']['id']}/").json()
        self.assertEqual(detail["ticket"]["status"], "cancelled")

    def test_another_student_cannot_cancel_or_view_it(self):
        body = self.take_token().json()
        intruder = self.register(name="Other Student")["studentKey"]
        self.assertEqual(self.cancel(body, token=intruder).status_code, 404)
        self.assertEqual(self.api_get(f"/tickets/{body['ticket']['id']}/", intruder).status_code, 404)
        self.assertEqual(len(self.state()["tickets"]), 1)

    def test_unknown_ticket_is_404(self):
        response = self.cancel({"ticket": {"id": 9999}}, token=self.register()["studentKey"])
        self.assertEqual(response.status_code, 404)

    def test_my_ticket_follows_the_account(self):
        student = self.register()
        self.assertIsNone(self.api_get("/tickets/mine/", student["studentKey"]).json()["ticket"])
        issued = self.take_token(key=student["studentKey"]).json()["ticket"]
        mine = self.api_get("/tickets/mine/", student["studentKey"]).json()["ticket"]
        self.assertEqual(mine["id"], issued["id"])
        self.staff_post("/counters/1/call-next/")
        self.staff_post("/counters/1/complete/")
        mine = self.api_get("/tickets/mine/", student["studentKey"]).json()["ticket"]
        self.assertEqual(mine["status"], "completed")

    def test_cannot_cancel_once_called_to_a_counter(self):
        body = self.take_token().json()
        self.staff_post("/counters/1/call-next/")
        response = self.cancel(body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["ticketStatus"], "serving")

    def test_cannot_cancel_twice(self):
        body = self.take_token().json()
        self.cancel(body)
        self.assertEqual(self.cancel(body).status_code, 409)

    def test_ticket_detail_reports_position_in_line(self):
        self.take_token()
        self.take_token()
        third = self.take_token().json()["ticket"]
        ticket = self.api_get(f"/tickets/{third['id']}/").json()["ticket"]
        self.assertEqual(ticket["position"], 2)
