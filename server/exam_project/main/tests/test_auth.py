# test_auth.py
# Signup / login with email OTP, sessions (refresh rotation, reuse detection, logout),
# password reset, the staff allowlist, Google sign-in, and what's actually stored.

from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.test import override_settings

from ..Handlers import auth_handler, crypto
from .base import API, STAFF_EMAIL, QueueTestCase


class OTPCapture:
    """Patches the email sender and remembers the last code sent to each address."""

    def __init__(self, test):
        self.codes = {}
        patcher = mock.patch.object(auth_handler, "send_otp_email", side_effect=self._capture)
        patcher.start()
        test.addCleanup(patcher.stop)

    def _capture(self, recipient, code, name, purpose):
        self.codes[recipient] = (code, purpose)
        return True

    def code(self, email):
        return self.codes[email][0]


class AuthTestCase(QueueTestCase):
    def setUp(self):
        super().setUp()
        self.otp = OTPCapture(self)

    def post(self, path, data, token=None):
        headers = self.bearer(token) if token else {}
        return self.client.post(f"{API}/auth/{path}", data, format="json", **headers)

    def signup(self, role="student", name="Aditi Rao", email="aditi@examcell.test", password="secret123"):
        return self.post("register/", {"role": role, "name": name, "email": email, "password": password})

    def verify(self, email="aditi@examcell.test", role="student", code=None):
        return self.post("verify-otp/", {"role": role, "email": email, "otp": code or self.otp.code(email)})

    def signup_and_verify(self, **kwargs):
        self.signup(**kwargs)
        return self.verify(email=kwargs.get("email", "aditi@examcell.test"), role=kwargs.get("role", "student"))


class SignupTests(AuthTestCase):
    def test_signup_sends_otp_and_issues_no_session_until_verified(self):
        response = self.signup()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("accessToken", response.json())
        self.assertEqual(self.otp.codes["aditi@examcell.test"][1], "signup")
        doc = self.db.students.find_one({})
        self.assertEqual(doc["status"], "Pending")
        self.assertNotIn("roll", doc)  # assigned only on activation

    def test_verify_activates_assigns_roll_and_starts_session(self):
        self.signup()
        body = self.verify().json()
        self.assertTrue(body["isNew"])
        self.assertTrue(body["accessToken"] and body["refreshToken"])
        self.assertEqual(body["user"]["role"], "student")
        self.assertEqual(body["user"]["name"], "Aditi Rao")
        self.assertEqual(body["user"]["roll"], "203000001")
        me = self.api_get("/auth/me/", body["accessToken"]).json()["user"]
        self.assertEqual(me["email"], "aditi@examcell.test")

    def test_roll_numbers_are_sequential_and_unique(self):
        a = self.signup_and_verify(email="a@examcell.test").json()["user"]["roll"]
        b = self.signup_and_verify(email="b@examcell.test").json()["user"]["roll"]
        self.assertEqual((a, b), ("203000001", "203000002"))

    def test_signup_validates_input(self):
        self.assertEqual(self.signup(name="").status_code, 400)
        self.assertEqual(self.signup(email="not-an-email").status_code, 400)
        self.assertEqual(self.signup(password="short1").status_code, 400)
        self.assertEqual(self.signup(password="onlyletters").status_code, 400)
        self.assertEqual(self.signup(role="admin").status_code, 400)

    def test_duplicate_active_email_is_rejected(self):
        self.signup_and_verify()
        self.assertEqual(self.signup().status_code, 409)

    def test_signing_up_again_before_verifying_replaces_the_pending_account(self):
        self.signup(name="First Try")
        self.advance(minutes=1)
        self.signup(name="Second Try")
        self.assertEqual(self.db.students.count_documents({}), 1)
        self.assertEqual(self.verify().json()["user"]["name"], "Second Try")

    def test_same_email_can_be_a_student_and_staff_separately(self):
        self.signup_and_verify(email=STAFF_EMAIL.replace("staff", "both"))
        # Stored per role: the student account lives in `students`, never in `staff`.
        self.assertEqual(self.db.staff.count_documents({"role": "student"}), 0)
        self.assertEqual(self.db.students.find_one({})["role"], "student")


class IndexMigrationTests(AuthTestCase):
    def test_legacy_conflicting_index_is_migrated(self):
        # A database from before roll became a partial index.
        self.db.students.drop()
        self.db.students.create_index("roll", unique=True)
        auth_handler.reset_index_cache()
        self.assertEqual(self.signup().status_code, 200)
        info = self.db.students.index_information()["roll_1"]
        self.assertIn("partialFilterExpression", info)

    def test_legacy_key_hash_index_does_not_block_new_signups(self):
        # The pre-auth schema had a unique index on key_hash; new accounts have no key_hash,
        # so with that index left in place the 2nd signup collided on null and was wrongly
        # reported as "email already exists".
        self.db.students.drop()
        self.db.students.insert_one({"key_hash": "legacy", "name": "Old Record", "roll": "202600001"})
        self.db.students.create_index("key_hash", unique=True)
        auth_handler.reset_index_cache()
        for n in range(3):
            response = self.signup(email=f"new{n}@examcell.test")
            self.assertEqual(response.status_code, 200, response.json())
        self.assertNotIn("key_hash_1", self.db.students.index_information())

    def test_non_email_duplicate_is_not_reported_as_existing_account(self):
        from pymongo.errors import DuplicateKeyError
        error = DuplicateKeyError("E11000", 11000, {"keyPattern": {"key_hash": 1}})
        with mock.patch.object(self.db.students.__class__, "insert_one", side_effect=error):
            response = self.signup(email="other@examcell.test")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("already exists", response.json()["message"])


class StorageSecurityTests(AuthTestCase):
    def test_personal_data_is_encrypted_and_password_is_argon2id(self):
        self.signup_and_verify()
        doc = self.db.students.find_one({})
        raw = str(doc)
        self.assertNotIn("aditi@examcell.test", raw)
        self.assertNotIn("Aditi Rao", raw)
        self.assertNotIn("secret123", raw)
        self.assertTrue(doc["password_hash"].startswith("$argon2id$"))
        self.assertTrue(doc["email_enc"].startswith("v1:"))
        self.assertEqual(crypto.decrypt_field(doc["email_enc"], "email"), "aditi@examcell.test")

    def test_otp_and_tokens_are_never_stored_raw(self):
        self.signup()
        code = self.otp.code("aditi@examcell.test")
        self.assertNotIn(code, str(self.db.students.find_one({})))
        body = self.verify(code=code).json()
        self.assertIsNone(self.db.refresh_tokens.find_one({"token_hash": body["refreshToken"]}))
        self.assertIsNotNone(self.db.refresh_tokens.find_one({"token_hash": crypto.hash_token(body["refreshToken"])}))

    def test_tampered_ciphertext_fails_to_decrypt(self):
        stored = crypto.encrypt_field("aditi@examcell.test", "email")
        tampered = stored[:-4] + ("AAAA" if not stored.endswith("AAAA") else "BBBB")
        with self.assertRaises(Exception):
            crypto.decrypt_field(tampered, "email")
        with self.assertRaises(Exception):
            crypto.decrypt_field(stored, "name")  # wrong field context


class LoginTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.signup_and_verify()

    def login(self, password="secret123", email="aditi@examcell.test", role="student"):
        return self.post("login/", {"role": role, "email": email, "password": password})

    def test_login_requires_password_then_otp(self):
        self.advance(minutes=1)  # past the resend cooldown from signup
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("accessToken", response.json())
        body = self.verify().json()
        self.assertFalse(body["isNew"])
        self.assertEqual(body["user"]["roll"], "203000001")

    def test_wrong_password_and_unknown_email_look_the_same(self):
        self.assertEqual(self.login(password="wrongpass1").json()["message"], "Invalid email or password.")
        self.assertEqual(self.login(email="nobody@examcell.test").json()["message"], "Invalid email or password.")

    def test_student_account_cannot_log_in_as_staff(self):
        self.assertEqual(self.login(role="staff").status_code, 401)

    def test_repeated_wrong_passwords_lock_the_account(self):
        for _ in range(5):
            self.login(password="wrongpass1")
        self.assertEqual(self.login().status_code, 429)  # even the right password
        self.advance(minutes=16)
        self.assertEqual(self.login().status_code, 200)

    def test_wrong_otp_attempts_invalidate_the_code(self):
        self.advance(minutes=1)
        self.login()
        for _ in range(4):
            self.assertEqual(self.verify(code="000000").status_code, 400)
        self.assertEqual(self.verify(code="000000").status_code, 429)
        self.assertEqual(self.verify().status_code, 400)  # the real code is gone too

    def test_otp_expires(self):
        self.advance(minutes=1)
        self.login()
        self.advance(minutes=11)
        self.assertEqual(self.verify().status_code, 400)

    def test_resend_is_rate_limited(self):
        self.advance(minutes=1)
        self.login()
        self.assertEqual(self.post("resend-otp/", {"role": "student", "email": "aditi@examcell.test"}).status_code, 429)


class SessionTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.session = self.signup_and_verify().json()

    def test_refresh_rotates_tokens(self):
        body = self.post("refresh/", {"refreshToken": self.session["refreshToken"]}).json()
        self.assertNotEqual(body["refreshToken"], self.session["refreshToken"])
        self.assertEqual(self.api_get("/auth/me/", body["accessToken"]).status_code, 200)

    def test_reusing_a_refresh_token_revokes_the_whole_session(self):
        rotated = self.post("refresh/", {"refreshToken": self.session["refreshToken"]}).json()
        # Old token presented again = stolen copy -> both the old and the new one die.
        self.assertEqual(self.post("refresh/", {"refreshToken": self.session["refreshToken"]}).status_code, 401)
        self.assertEqual(self.post("refresh/", {"refreshToken": rotated["refreshToken"]}).status_code, 401)

    def test_access_token_expires(self):
        self.advance(minutes=31)
        self.assertEqual(self.api_get("/auth/me/", self.session["accessToken"]).status_code, 401)

    def test_logout_revokes_access_and_refresh(self):
        self.post("logout/", {"refreshToken": self.session["refreshToken"]}, token=self.session["accessToken"])
        self.assertEqual(self.api_get("/auth/me/", self.session["accessToken"]).status_code, 401)
        self.assertEqual(self.post("refresh/", {"refreshToken": self.session["refreshToken"]}).status_code, 401)

    def test_suspended_account_loses_access(self):
        self.db.students.update_one({}, {"$set": {"status": "Suspended"}})
        self.assertEqual(self.api_get("/auth/me/", self.session["accessToken"]).status_code, 401)


class PasswordResetTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.session = self.signup_and_verify().json()
        self.advance(minutes=1)

    def test_reset_sets_new_password_and_signs_out_everywhere(self):
        self.assertEqual(self.post("forgot-password/", {"role": "student", "email": "aditi@examcell.test"}).status_code, 200)
        response = self.post("reset-password/", {
            "role": "student", "email": "aditi@examcell.test",
            "otp": self.otp.code("aditi@examcell.test"), "password": "newsecret456",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.post("refresh/", {"refreshToken": self.session["refreshToken"]}).status_code, 401)
        self.advance(minutes=1)
        login = self.post("login/", {"role": "student", "email": "aditi@examcell.test", "password": "newsecret456"})
        self.assertEqual(login.status_code, 200)

    def test_forgot_password_does_not_reveal_whether_account_exists(self):
        known = self.post("forgot-password/", {"role": "student", "email": "aditi@examcell.test"}).json()["message"]
        unknown = self.post("forgot-password/", {"role": "student", "email": "nobody@examcell.test"}).json()["message"]
        self.assertEqual(known, unknown)


class StaffAccessTests(AuthTestCase):
    def test_staff_signup_requires_allowlisted_email(self):
        response = self.signup(role="staff", email="random@examcell.test")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.db.staff.count_documents({}), 1)  # only the fixture staff account

    def test_allowlisted_staff_can_sign_up_and_has_no_roll(self):
        with override_settings(STAFF_EMAILS={STAFF_EMAIL, "newstaff@examcell.test"}):
            body = self.signup_and_verify(role="staff", email="newstaff@examcell.test", name="New Staff").json()
            self.assertEqual(body["user"]["role"], "staff")
            self.assertNotIn("roll", body["user"])
            self.assertEqual(self.staff_post("/counters/1/complete/").status_code, 409)  # staff access works
            doc = self.db.staff.find_one({"email_index": crypto.email_index("newstaff@examcell.test")})
            self.assertEqual(doc["role"], "staff")

    def test_removing_email_from_allowlist_ends_staff_access(self):
        with override_settings(STAFF_EMAILS=set()):
            self.assertEqual(self.staff_post("/queue/reset/").status_code, 401)


class GoogleSignInTests(AuthTestCase):
    RETURN_URL = "http://localhost:8080/auth/callback"

    def start(self, role="student"):
        with override_settings(GOOGLE_CLIENT_ID="test-client", GOOGLE_CLIENT_SECRET="test-secret"):
            response = self.client.get(f"{API}/auth/google/login/", {"role": role, "return_url": self.RETURN_URL})
        self.assertEqual(response.status_code, 302)
        return parse_qs(urlparse(response["Location"]).query)["state"][0]

    def callback(self, state, email="gstudent@gmail.com", name="Google Student", sub="google-sub-1"):
        claims = {"email": email, "email_verified": True, "name": name, "sub": sub}
        with override_settings(GOOGLE_CLIENT_ID="test-client", GOOGLE_CLIENT_SECRET="test-secret"), \
                mock.patch.object(auth_handler, "_verify_google_code", return_value=claims):
            response = self.client.get(f"{API}/auth/google/callback/", {"code": "google-code", "state": state})
        self.assertEqual(response.status_code, 302)
        return parse_qs(urlparse(response["Location"]).query)

    def test_google_signup_creates_active_student_with_roll(self):
        params = self.callback(self.start())
        body = self.post("google/exchange/", {"code": params["code"][0]}).json()
        self.assertTrue(body["isNew"])
        self.assertEqual(body["user"]["role"], "student")
        self.assertEqual(body["user"]["authProvider"], "google")
        self.assertEqual(body["user"]["roll"], "203000001")

    def test_login_code_is_single_use(self):
        code = self.callback(self.start())["code"][0]
        self.assertEqual(self.post("google/exchange/", {"code": code}).status_code, 200)
        self.assertEqual(self.post("google/exchange/", {"code": code}).status_code, 400)

    def test_login_code_expires(self):
        code = self.callback(self.start())["code"][0]
        self.advance(minutes=2)
        self.assertEqual(self.post("google/exchange/", {"code": code}).status_code, 400)

    def test_google_links_to_existing_password_account(self):
        self.signup_and_verify(email="gstudent@gmail.com")
        code = self.callback(self.start())["code"][0]
        body = self.post("google/exchange/", {"code": code}).json()
        self.assertFalse(body["isNew"])
        self.assertEqual(self.db.students.count_documents({}), 1)

    def test_google_staff_requires_allowlist(self):
        params = self.callback(self.start(role="staff"), email="notstaff@gmail.com")
        self.assertIn("error", params)
        self.assertNotIn("code", params)

    def test_tampered_state_is_rejected(self):
        params = self.callback(self.start() + "x")
        self.assertIn("error", params)

    def test_redirect_only_to_our_frontend(self):
        with override_settings(GOOGLE_CLIENT_ID="test-client", GOOGLE_CLIENT_SECRET="test-secret"):
            response = self.client.get(f"{API}/auth/google/login/",
                                       {"role": "student", "return_url": "https://evil.example/steal"})
        self.assertEqual(response.status_code, 400)
