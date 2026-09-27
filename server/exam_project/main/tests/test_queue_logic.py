# test_queue_logic.py
# The rules that decide who is served when, and the numbers shown to students and staff:
# appointment booking + capacity, queue ordering, wait estimates and averages computed
# from stored timestamps, the day rollover, and the audit log.

from django.test import override_settings

from .base import API, TODAY, TOMORROW, QueueTestCase


class AppointmentBookingTests(QueueTestCase):
    def test_books_a_future_slot_today(self):
        response = self.book("11:30 AM")
        self.assertEqual(response.status_code, 201)
        ticket = response.json()["ticket"]
        self.assertEqual((ticket["slot"], ticket["date"], ticket["mode"]), ("11:30 AM", TODAY, "appointment"))

    def test_rejects_slot_that_already_started(self):
        # Clock is 11:00 AM.
        self.assertEqual(self.book("10:30 AM").status_code, 400)
        self.assertEqual(self.book("11:00 AM").status_code, 400)

    def test_rejects_bad_past_and_too_far_dates(self):
        self.assertEqual(self.book("11:30 AM", date="2030-01-14").status_code, 400)
        self.assertEqual(self.book("11:30 AM", date="2030-03-01").status_code, 400)
        self.assertEqual(self.book("11:30 AM", date="15/01/2030").status_code, 400)
        self.assertEqual(self.book("11:30 AM", date="2030-02-30").status_code, 400)
        self.assertEqual(self.book("11:30 AM", date=None).status_code, 400)

    def test_rejects_unknown_slot(self):
        self.assertEqual(self.book("3:00 AM").status_code, 400)

    @override_settings(SLOT_CAPACITY=2)
    def test_slot_capacity_is_enforced_and_released_on_cancel(self):
        first = self.book("11:30 AM").json()
        self.assertEqual(self.book("11:30 AM").status_code, 201)
        self.assertEqual(self.book("11:30 AM").status_code, 409)
        self.assertEqual(self.book("11:45 AM").status_code, 201)  # other slots unaffected
        self.cancel(first)
        self.assertEqual(self.book("11:30 AM").status_code, 201)

    @override_settings(SLOT_CAPACITY=2)
    def test_slot_availability_reports_remaining_and_past(self):
        self.book("11:30 AM")
        slots = {s["slot"]: s for s in self.api_get(f"/slots/?date={TODAY}").json()["slots"]}
        self.assertEqual(slots["11:30 AM"]["remaining"], 1)
        self.assertTrue(slots["11:30 AM"]["available"])
        self.assertTrue(slots["10:00 AM"]["past"])
        self.assertFalse(slots["10:00 AM"]["available"])
        tomorrow = self.api_get(f"/slots/?date={TOMORROW}").json()["slots"]
        self.assertTrue(all(s["available"] for s in tomorrow))

    def test_slot_availability_validates_date(self):
        self.assertEqual(self.api_get(f"/slots/?date=2030-01-01").status_code, 400)
        self.assertEqual(self.api_get(f"/slots/").status_code, 400)

    def test_future_appointment_is_not_in_todays_queue(self):
        body = self.book("11:30 AM", date=TOMORROW).json()
        self.assertEqual(self.state()["tickets"], [])
        self.assertIsNone(self.staff_post("/counters/1/call-next/").json()["ticket"])
        # Numbered in tomorrow's sequence, and trackable by the student.
        self.assertEqual(body["ticket"]["token"], "TK-101")
        detail = self.api_get(f"/tickets/{body['ticket']['id']}/").json()["ticket"]
        self.assertEqual(detail["date"], TOMORROW)
        self.assertIsNone(detail["position"])


class QueueOrderTests(QueueTestCase):
    def test_walk_ins_are_first_come_first_served(self):
        a = self.take_token().json()["ticket"]
        b = self.take_token().json()["ticket"]
        self.assertEqual([t["id"] for t in self.waiting()], [a["id"], b["id"]])

    def test_due_appointment_goes_ahead_of_walk_ins(self):
        walk_in = self.take_token().json()["ticket"]
        appointment = self.book("11:30 AM").json()["ticket"]
        # 11:00 — appointment not due yet, walk-in is first.
        self.assertEqual(self.waiting()[0]["id"], walk_in["id"])
        # 11:26 — inside the 5-minute grace window before 11:30, appointment jumps ahead.
        self.advance(minutes=26)
        self.assertEqual(self.waiting()[0]["id"], appointment["id"])
        called = self.staff_post("/counters/1/call-next/").json()["ticket"]
        self.assertEqual(called["id"], appointment["id"])

    def test_early_appointment_is_served_when_nobody_else_waits(self):
        appointment = self.book("2:00 PM").json()["ticket"]
        called = self.staff_post("/counters/1/call-next/").json()["ticket"]
        self.assertEqual(called["id"], appointment["id"])

    def test_due_appointments_are_ordered_by_slot(self):
        later = self.book("11:30 AM").json()["ticket"]
        earlier = self.book("11:15 AM").json()["ticket"]
        self.advance(minutes=30)
        self.assertEqual([t["id"] for t in self.waiting()], [earlier["id"], later["id"]])

    def test_public_and_staff_positions_match_call_order(self):
        for _ in range(3):
            self.take_token()
        waiting = self.waiting(self.state(staff=False))
        self.assertEqual([t["position"] for t in waiting], [0, 1, 2])
        called = self.staff_post("/counters/1/call-next/").json()["ticket"]
        self.assertEqual(called["id"], waiting[0]["id"])


class EstimateTests(QueueTestCase):
    def test_average_service_time_comes_from_real_timestamps(self):
        state = self.state()
        self.assertTrue(state["avgServiceIsEstimate"])
        self.assertEqual(state["avgServiceMinutes"], 5.0)  # DEFAULT_SERVICE_MINUTES fallback

        self.take_token()
        self.take_token()
        self.staff_post("/counters/1/call-next/")
        self.advance(minutes=8)
        self.staff_post("/counters/1/call-next/")  # completes the first after 8 min
        self.advance(minutes=4)
        self.staff_post("/counters/1/complete/")  # second took 4 min
        state = self.state()
        self.assertFalse(state["avgServiceIsEstimate"])
        self.assertEqual(state["avgServiceMinutes"], 6.0)

    def test_instant_completions_dont_collapse_the_average(self):
        # A mis-click "complete" right after calling must not turn every forecast into 0.
        self.take_token()
        self.take_token()
        self.staff_post("/counters/1/call-next/")
        self.staff_post("/counters/1/complete/")  # 0 seconds — ignored
        state = self.state()
        self.assertTrue(state["avgServiceIsEstimate"])
        self.assertEqual(state["avgServiceMinutes"], 5.0)
        self.staff_post("/counters/1/call-next/")
        self.advance(seconds=40)
        self.staff_post("/counters/1/complete/")  # 40 s — real but floored to 1 min
        self.assertEqual(self.state()["avgServiceMinutes"], 1.0)

    def test_average_wait_comes_from_real_timestamps(self):
        self.assertIsNone(self.state()["avgWaitMinutes"])
        self.take_token()
        self.advance(minutes=10)
        self.staff_post("/counters/1/call-next/")
        self.assertEqual(self.state()["avgWaitMinutes"], 10.0)

    def test_forecast_fills_free_counters_then_waits_for_them_to_free_up(self):
        # 3 idle counters, 5 waiting, default 5 min/student: the first three are called
        # straight away, the next two when the first counters finish.
        for _ in range(5):
            self.take_token()
        self.assertEqual([t["estWaitMin"] for t in self.waiting()], [0, 0, 0, 5, 5])

    def test_forecast_counts_down_as_current_students_are_served(self):
        for _ in range(4):
            self.take_token()
        for counter in (1, 2, 3):
            self.staff_post(f"/counters/{counter}/call-next/")
        first = self.waiting()[0]
        self.assertEqual(first["estWaitMin"], 5)
        call_at = first["estCallAt"]
        self.advance(minutes=3)
        first = self.waiting()[0]
        self.assertEqual(first["estWaitMin"], 2)
        # The predicted call time itself stays put while nothing changes.
        self.assertEqual(first["estCallAt"], call_at)

    def test_forecast_uses_real_service_pace(self):
        # Two students served in 10 min each -> average 10, so the next wait doubles.
        for _ in range(2):
            self.take_token()
        self.staff_post("/counters/1/call-next/")
        self.advance(minutes=10)
        self.staff_post("/counters/1/call-next/")
        self.advance(minutes=10)
        self.staff_post("/counters/1/complete/")
        for _ in range(4):
            self.take_token()
        for counter in (1, 2, 3):
            self.staff_post(f"/counters/{counter}/call-next/")
        self.assertEqual(self.state()["avgServiceMinutes"], 10.0)
        self.assertEqual(self.waiting()[0]["estWaitMin"], 10)

    def test_forecast_assumes_overrunning_student_finishes_any_moment(self):
        for _ in range(4):
            self.take_token()
        for counter in (1, 2, 3):
            self.staff_post(f"/counters/{counter}/call-next/")
        self.advance(minutes=12)  # well past the 5-minute average
        self.assertEqual(self.waiting()[0]["estWaitMin"], 0)

    @override_settings(COUNTER_COUNT=1)
    def test_forecast_lets_an_appointment_that_becomes_due_jump_ahead(self):
        # One counter, serving someone until ~11:05. Waiting: W1, W2 (walk-ins) and an
        # 11:15 appointment (due from 11:10). W1 goes at 11:05; at 11:10 the appointment
        # is due and goes before W2, who is pushed to 11:15.
        self.take_token()
        self.staff_post("/counters/1/call-next/")
        w1 = self.take_token().json()["ticket"]
        w2 = self.take_token().json()["ticket"]
        appt = self.book("11:15 AM").json()["ticket"]
        est = {t["id"]: t["estWaitMin"] for t in self.waiting()}
        self.assertEqual((est[w1["id"]], est[appt["id"]], est[w2["id"]]), (5, 10, 15))

    def test_appointment_estimate_is_never_before_its_slot(self):
        appointment = self.book("1:00 PM").json()["ticket"]
        self.take_token()
        mine = next(t for t in self.waiting() if t["id"] == appointment["id"])
        self.assertEqual(mine["estWaitMin"], 115)  # 11:00 -> 12:55 (slot minus 5-min grace)

    def test_future_day_appointment_tracker_counts_down_to_slot(self):
        body = self.book("11:00 AM", date=TOMORROW).json()
        ticket = self.api_get(f"/tickets/{body['ticket']['id']}/").json()["ticket"]
        self.assertIsNone(ticket["position"])
        self.assertEqual(ticket["estWaitMin"], 24 * 60 - 5)

    def test_join_preview_matches_what_a_new_walk_in_actually_gets(self):
        for _ in range(4):
            self.take_token()
        preview = self.state(staff=False)["joinPreview"]
        self.assertEqual((preview["ahead"], preview["estWaitMin"]), (4, 5))
        ticket = self.take_token().json()["ticket"]
        self.assertEqual(ticket["position"], preview["ahead"])
        self.assertEqual(ticket["estWaitMin"], preview["estWaitMin"])
        self.assertEqual(ticket["estCallAt"], preview["estCallAt"])

    def test_join_preview_goes_behind_due_appointments_only(self):
        self.assertEqual(self.book("2:00 PM").status_code, 201)  # stays not due
        self.assertEqual(self.book("11:15 AM").status_code, 201)
        self.advance(minutes=10)  # 11:10 — the 11:15 appointment is now due
        self.assertEqual(self.state()["joinPreview"]["ahead"], 1)


class DayRolloverTests(QueueTestCase):
    def test_new_day_expires_leftovers_frees_counters_and_restarts_numbering(self):
        self.take_token()
        self.take_token()
        tomorrow_appt = self.book("10:30 AM", date=TOMORROW).json()["ticket"]
        self.staff_post("/counters/1/call-next/")

        self.advance(days=1)  # 11:00 AM the next day
        state = self.state()
        self.assertEqual([t["id"] for t in state["tickets"]], [tomorrow_appt["id"]])  # yesterday's gone
        self.assertTrue(all(c["servingId"] is None for c in state["counters"]))
        self.assertIsNone(state["lastCalled"])
        self.assertEqual((state["servedToday"], state["skippedToday"]), (0, 0))
        self.assertEqual(self.db.tickets.count_documents({"status": "expired", "close_reason": "day_end"}), 2)
        # Tomorrow's sequence already issued TK-101 to the appointment.
        self.assertEqual(self.take_token().json()["ticket"]["token"], "TK-102")

    def test_student_with_expired_ticket_can_take_a_new_one_next_day(self):
        key = self.register()["studentKey"]
        self.take_token(key=key)
        self.advance(days=1)
        self.assertEqual(self.take_token(key=key).status_code, 201)


class AuditLogTests(QueueTestCase):
    def test_every_action_is_logged_in_order(self):
        body = self.take_token().json()
        self.staff_post("/counters/1/call-next/")
        self.staff_post("/counters/1/transfer/", {"to_counter": 2})
        self.staff_post("/counters/2/complete/")
        actions = [e["action"] for e in self.db.ticket_events.find({"ticket_id": body["ticket"]["id"]}).sort("at", 1)]
        self.assertEqual(actions, ["issued", "called", "transferred", "completed"])
