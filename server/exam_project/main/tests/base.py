# base.py
# Shared scaffolding for the backend test suite.
#
# Runs against an isolated MongoDB test database (see manage.py: MONGO_DB_NAME is swapped
# to ExamCellQueueDB_test for `manage.py test` runs) — never touches real queue data.
# Each test starts from empty collections with the clock frozen at a known moment, then
# exercises the real DRF views over HTTP as real logged-in student / staff accounts.

import itertools
from datetime import datetime, timedelta, timezone
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from pymongo.database import Database
from rest_framework.test import APIClient

from ..Handlers import auth_handler, clock, notifications, queue_state
from ..Handlers.db_connection import get_db, MONGO_DB_NAME

API = "/api/v1/main"
COLLECTIONS = (
    "tickets", "counters", "queue_meta", "token_counters", "slot_bookings", "ticket_events",
    "students", "staff", "refresh_tokens", "revoked_tokens", "oauth_codes", "notifications",
)
STAFF_EMAIL = "staff@examcell.test"

# Tuesday 15 Jan 2030, 11:00 AM IST — mid-morning, inside the 10:00–16:00 slot window.
FROZEN_START = datetime(2030, 1, 15, 11, 0, tzinfo=clock.IST)
TODAY = "2030-01-15"
TOMORROW = "2030-01-16"

_emails = itertools.count(1)


@override_settings(STAFF_EMAILS={STAFF_EMAIL}, NOTIFICATIONS_SYNC=True)
class QueueTestCase(SimpleTestCase):
    db: Database

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        assert MONGO_DB_NAME.endswith("_test"), (
            f"Refusing to run tests against non-test database '{MONGO_DB_NAME}' — "
            "check manage.py's MONGO_DB_NAME override for `test` runs."
        )
        db = get_db()
        assert db is not None, "MongoDB test database is not reachable — check MONGO_URL."
        cls.db = db

    def setUp(self):
        self.client = APIClient()
        # DRF's throttle state lives in Django's cache, which is process-wide — without
        # this, the rate limits would leak between test methods.
        cache.clear()
        for name in COLLECTIONS:
            self.db[name].drop()
        queue_state.reset_setup_cache()
        auth_handler.reset_index_cache()
        notifications.reset_index_cache()
        self.now = FROZEN_START
        patcher = mock.patch.object(clock, "now_utc", side_effect=lambda: self.now.astimezone(timezone.utc))
        patcher.start()
        self.addCleanup(patcher.stop)
        # Never send real email from tests — record what would have been sent instead.
        self.sent_called_emails = []
        mailer = mock.patch.object(
            notifications, "send_called_email",
            side_effect=lambda **kw: self.sent_called_emails.append(kw) or True,
        )
        mailer.start()
        self.addCleanup(mailer.stop)
        self.sent_staff_emails = []
        staff_mailer = mock.patch.object(
            notifications, "send_new_ticket_staff_email",
            side_effect=lambda recipient, **kw: self.sent_staff_emails.append({"recipient": recipient, **kw}) or True,
        )
        staff_mailer.start()
        self.addCleanup(staff_mailer.stop)
        self.sent_cancel_emails = []
        cancel_mailer = mock.patch.object(
            notifications, "send_appointment_cancelled_email",
            side_effect=lambda **kw: self.sent_cancel_emails.append(kw) or True,
        )
        cancel_mailer.start()
        self.addCleanup(cancel_mailer.stop)
        self._ticket_owner = {}
        # Account handles -> (user_id, role). bearer() mints a fresh access token for these on
        # every request, so tests that move the clock hours/days ahead aren't tripped up by
        # the real 30-minute token expiry (which test_auth covers on its own).
        self._accounts = {}
        staff = auth_handler.create_active_account("staff", "Exam Cell Staff", STAFF_EMAIL, "staffpass123")
        self.staff_token = self._handle(staff, "staff")

    @classmethod
    def tearDownClass(cls):
        for name in COLLECTIONS:
            cls.db[name].drop()
        queue_state.reset_setup_cache()
        auth_handler.reset_index_cache()
        super().tearDownClass()

    # ── helpers ──────────────────────────────────────────────────────────────

    def advance(self, **delta):
        self.now = self.now + timedelta(**delta)

    def to_ms(self, dt):
        return int(dt.timestamp() * 1000)

    def _handle(self, user, role):
        handle = f"account:{user['_id']}"
        self._accounts[handle] = (str(user["_id"]), role)
        return handle

    def bearer(self, token):
        if token in self._accounts:
            token = auth_handler.generate_access_token(*self._accounts[token])
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    def staff_header(self):
        return self.bearer(self.staff_token)

    def register(self, name="Aarav Sharma", email=None, password="studentpass1"):
        """A new verified student account. Returns {studentKey, roll, name, email, id} —
        `studentKey` is the handle take_token(key=...) / bearer() log in with."""
        email = email or f"student{next(_emails)}@examcell.test"
        user = auth_handler.create_active_account("student", name, email, password)
        return {
            "studentKey": self._handle(user, "student"),
            "roll": user["roll"],
            "name": name,
            "email": email,
            "id": str(user["_id"]),
        }

    def take_token(self, service="hall-ticket", student="Aarav Sharma", mode="walk-in", slot=None, date=None, key=None):
        """Takes a token as a student (a brand-new account unless `key` is given — one open
        ticket per student per day is enforced, and most tests aren't about that)."""
        token = key if key is not None else self.register(name=student)["studentKey"]
        payload = {"service": service, "mode": mode}
        if slot is not None:
            payload["slot"] = slot
        if date is not None:
            payload["date"] = date
        response = self.client.post(f"{API}/tickets/", payload, format="json", **self.bearer(token))
        if response.status_code == 201:
            self._ticket_owner[response.json()["ticket"]["id"]] = token
        return response

    def book(self, slot, date=TODAY, **kwargs):
        return self.take_token(mode="appointment", slot=slot, date=date, **kwargs)

    def cancel(self, body, token=None):
        ticket_id = body["ticket"]["id"]
        token = token or self._ticket_owner[ticket_id]
        return self.client.post(f"{API}/tickets/{ticket_id}/cancel/", {}, format="json", **self.bearer(token))

    def staff_post(self, path, data=None):
        return self.client.post(f"{API}{path}", data or {}, format="json", **self.staff_header())

    def api_get(self, path, token=None):
        """Authenticated GET — as staff unless a token is given."""
        return self.client.get(f"{API}{path}", **self.bearer(token or self.staff_token))

    def state(self, staff=True):
        token = self.staff_token if staff else self.register(name="Viewer Student")["studentKey"]
        return self.api_get("/queue/", token).json()

    def waiting(self, state=None):
        state = state or self.state()
        return [t for t in state["tickets"] if t["status"] == "waiting"]
