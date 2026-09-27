# test_access_control.py
# Every endpoint enforces the right role, and non-staff never see full student identities.

from .base import API, QueueTestCase

STAFF_ONLY = [
    ("post", "/counters/1/call-next/"),
    ("post", "/counters/1/complete/"),
    ("post", "/counters/1/skip/"),
    ("post", "/counters/1/transfer/"),
    ("post", "/queue/reset/"),
    ("get", "/db/"),
]
LOGGED_IN_ONLY = [
    ("get", "/queue/"),
    ("get", "/slots/?date=2030-01-15"),
    ("get", "/tickets/mine/"),
    ("post", "/tickets/"),
    ("get", "/auth/me/"),
]


class AccessControlTests(QueueTestCase):
    def test_endpoints_reject_anonymous_requests(self):
        for method, path in STAFF_ONLY + LOGGED_IN_ONLY:
            response = getattr(self.client, method)(f"{API}{path}")
            self.assertEqual(response.status_code, 401, path)

    def test_endpoints_reject_forged_tokens(self):
        for method, path in STAFF_ONLY + LOGGED_IN_ONLY:
            response = getattr(self.client, method)(f"{API}{path}", **self.bearer("not.a.jwt"))
            self.assertEqual(response.status_code, 401, path)

    def test_students_cannot_use_staff_endpoints(self):
        student = self.register()["studentKey"]
        for method, path in STAFF_ONLY:
            response = getattr(self.client, method)(f"{API}{path}", **self.bearer(student))
            self.assertEqual(response.status_code, 403, path)

    def test_staff_cannot_use_student_only_endpoints(self):
        self.assertEqual(self.api_get("/tickets/mine/").status_code, 403)

    def test_student_view_masks_other_students(self):
        self.take_token(student="Aarav Sharma")
        public = self.state(staff=False)["tickets"][0]
        self.assertEqual(public["student"], "Aarav S.")
        self.assertTrue(public["roll"].startswith("2030") and public["roll"].endswith("•••••"))

    def test_staff_view_shows_full_student_identity(self):
        student = self.register(name="Aarav Sharma")
        self.take_token(key=student["studentKey"])
        staff = self.state(staff=True)["tickets"][0]
        self.assertEqual(staff["student"], "Aarav Sharma")
        self.assertEqual(staff["roll"], student["roll"])

    def test_public_status_and_services_are_open(self):
        self.assertEqual(self.client.get(f"{API}/status/").status_code, 200)
        body = self.client.get(f"{API}/services/").json()
        self.assertEqual(len(body["services"]), 5)
        self.assertEqual(body["slots"][0], "10:00 AM")
        self.assertEqual(body["slots"][-1], "3:45 PM")
