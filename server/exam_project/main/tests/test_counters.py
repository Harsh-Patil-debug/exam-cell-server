# test_counters.py
# Staff-side flow: call next, complete, skip, transfer, reset — and that concurrent staff
# actions can never corrupt the queue.

from concurrent.futures import ThreadPoolExecutor

from ..Handlers import counters as counter_handlers
from ..Handlers.db_connection import supports_transactions
from .base import TOMORROW, QueueTestCase


class CounterFlowTests(QueueTestCase):
    def setUp(self):
        super().setUp()
        self.first = self.take_token().json()["ticket"]
        self.second = self.take_token(student="Meera Iyer").json()["ticket"]

    def counter(self, state, counter_id):
        return next(c for c in state["counters"] if c["id"] == counter_id)

    def test_counters_are_created_on_first_use(self):
        state = self.state()
        self.assertEqual([c["name"] for c in state["counters"]], ["Counter 1", "Counter 2", "Counter 3"])

    def test_call_next_serves_first_in_line_and_records_last_call(self):
        body = self.staff_post("/counters/1/call-next/").json()
        self.assertEqual(body["ticket"]["token"], self.first["token"])
        self.assertIsNotNone(body["ticket"]["calledAt"])
        state = self.state()
        self.assertEqual(self.counter(state, 1)["servingId"], self.first["id"])
        self.assertEqual(state["lastCalled"]["token"], self.first["token"])
        self.assertEqual(state["lastCalled"]["counter"], 1)

    def test_two_counters_never_get_the_same_student(self):
        a = self.staff_post("/counters/1/call-next/").json()["ticket"]
        b = self.staff_post("/counters/2/call-next/").json()["ticket"]
        self.assertNotEqual(a["id"], b["id"])

    def test_call_next_completes_the_current_student_first(self):
        self.staff_post("/counters/1/call-next/")
        body = self.staff_post("/counters/1/call-next/").json()
        self.assertEqual(body["completed"]["token"], self.first["token"])
        self.assertEqual(body["ticket"]["token"], self.second["token"])
        self.assertEqual(self.state()["servedToday"], 1)

    def test_call_next_on_empty_queue_returns_no_ticket(self):
        self.staff_post("/counters/1/call-next/")
        self.staff_post("/counters/2/call-next/")
        body = self.staff_post("/counters/3/call-next/").json()
        self.assertIsNone(body["ticket"])

    def test_complete_and_skip_update_stats(self):
        self.staff_post("/counters/1/call-next/")
        self.staff_post("/counters/2/call-next/")
        self.assertEqual(self.staff_post("/counters/1/complete/").status_code, 200)
        self.assertEqual(self.staff_post("/counters/2/skip/").status_code, 200)
        state = self.state()
        self.assertEqual((state["servedToday"], state["skippedToday"]), (1, 1))
        self.assertTrue(all(c["servingId"] is None for c in state["counters"]))

    def test_complete_on_idle_counter_is_a_conflict(self):
        self.assertEqual(self.staff_post("/counters/1/complete/").status_code, 409)

    def test_unknown_counter_is_404(self):
        self.assertEqual(self.staff_post("/counters/99/call-next/").status_code, 404)
        self.assertEqual(self.staff_post("/counters/99/complete/").status_code, 404)

    def test_transfer_to_free_counter_moves_student(self):
        self.staff_post("/counters/1/call-next/")
        body = self.staff_post("/counters/1/transfer/", {"to_counter": 3}).json()
        self.assertFalse(body["requeued"])
        state = self.state()
        self.assertIsNone(self.counter(state, 1)["servingId"])
        self.assertEqual(self.counter(state, 3)["servingId"], self.first["id"])
        self.assertEqual(state["lastCalled"]["counter"], 3)

    def test_transfer_to_busy_counter_requeues_student_at_their_original_place(self):
        self.staff_post("/counters/1/call-next/")
        self.staff_post("/counters/2/call-next/")
        third = self.take_token().json()["ticket"]
        body = self.staff_post("/counters/1/transfer/", {"to_counter": 2}).json()
        self.assertTrue(body["requeued"])
        self.assertEqual(body["ticket"]["status"], "waiting")
        # First-come order is by ticket, so the returned student goes ahead of later arrivals.
        self.assertEqual([t["id"] for t in self.waiting()], [self.first["id"], third["id"]])

    def test_transfer_rejects_same_or_invalid_counter(self):
        self.staff_post("/counters/1/call-next/")
        self.assertEqual(self.staff_post("/counters/1/transfer/", {"to_counter": 1}).status_code, 400)
        self.assertEqual(self.staff_post("/counters/1/transfer/", {"to_counter": {"$gt": 0}}).status_code, 400)
        self.assertEqual(self.staff_post("/counters/1/transfer/", {"to_counter": 99}).status_code, 404)
        # A rejected transfer must leave the student exactly where they were.
        self.assertEqual(self.counter(self.state(), 1)["servingId"], self.first["id"])


class ResetTests(QueueTestCase):
    def test_reset_closes_queue_keeps_history_and_restarts_numbering(self):
        self.take_token()
        self.take_token()
        self.staff_post("/counters/1/call-next/")
        body = self.staff_post("/queue/reset/").json()
        self.assertEqual(body["closedTickets"], 2)
        state = self.state()
        self.assertEqual(state["tickets"], [])
        self.assertIsNone(state["lastCalled"])
        self.assertEqual((state["servedToday"], state["skippedToday"]), (0, 0))
        self.assertTrue(all(c["servingId"] is None for c in state["counters"]))
        self.assertEqual(self.take_token().json()["ticket"]["token"], "TK-101")
        # History kept, not deleted.
        self.assertEqual(self.db.tickets.count_documents({"close_reason": "reset"}), 2)

    def test_reset_frees_todays_slots_but_keeps_future_appointments(self):
        self.book("11:30 AM")
        future = self.book("11:30 AM", date=TOMORROW).json()["ticket"]
        self.staff_post("/queue/reset/")
        self.assertEqual(self.db.slot_bookings.count_documents({"date": "2030-01-15"}), 0)
        self.assertEqual(self.db.tickets.find_one({"id": future["id"]})["status"], "waiting")


class ConcurrencyTests(QueueTestCase):
    def test_simultaneous_call_next_on_every_counter_hands_out_distinct_students(self):
        if not supports_transactions():
            self.skipTest("Needs a replica set (Atlas) for transactional retries.")
        for _ in range(3):
            self.take_token()
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(counter_handlers.call_next_handler, [1, 2, 3]))
        self.assertTrue(all(code == 200 for _, code in results), results)
        ids = [body["ticket"]["id"] for body, _ in results]
        self.assertEqual(len(set(ids)), 3)
        state = self.state()
        self.assertEqual(sorted(c["servingId"] for c in state["counters"]), sorted(ids))

    def test_simultaneous_call_next_on_one_counter_never_orphans_a_ticket(self):
        if not supports_transactions():
            self.skipTest("Needs a replica set (Atlas) for transactional retries.")
        for _ in range(4):
            self.take_token()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(counter_handlers.call_next_handler, [1, 1, 1, 1]))
        # Whatever order they ran in: exactly one student is at counter 1, and every other
        # called student was completed — none left "serving" with no counter.
        serving = list(self.db.tickets.find({"status": "serving"}))
        self.assertEqual(len(serving), 1)
        self.assertEqual(self.db.counters.find_one({"_id": 1})["serving_id"], serving[0]["id"])
        self.assertEqual(self.db.tickets.count_documents({"status": "completed"}), 3)
