# test_errors.py
# Failures reach the frontend as JSON it can show — never a Django HTML error page.

from unittest import mock

from ..Handlers import auth_handler
from .base import API, QueueTestCase


class ErrorResponseTests(QueueTestCase):
    def test_database_unreachable_is_a_clean_503(self):
        with mock.patch.object(auth_handler, "ensure_indexes",
                               side_effect=ConnectionError("[ExamCell] MongoDB not available")):
            response = self.client.post(f"{API}/auth/register/", {
                "role": "student", "name": "Aditi Rao", "email": "a@examcell.test", "password": "secret123",
            }, format="json")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "db_unavailable")

    def test_unexpected_error_is_a_json_500(self):
        with mock.patch.object(auth_handler, "ensure_indexes", side_effect=RuntimeError("boom")):
            response = self.client.post(f"{API}/auth/register/", {
                "role": "student", "name": "Aditi Rao", "email": "a@examcell.test", "password": "secret123",
            }, format="json")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertNotIn("boom", response.json()["message"])  # internals aren't leaked
