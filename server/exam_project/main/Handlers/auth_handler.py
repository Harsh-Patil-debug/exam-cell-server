# auth_handler.py
# Accounts, login/signup, sessions and Google sign-in for both roles.
#
# Modelled on khelomore-server's auth_handler:
#   - Traditional signup and login are both two-step: credentials first, then a 6-digit
#     email OTP. No token is issued until the OTP is verified.
#   - Per-account login lockout after MAX_LOGIN_ATTEMPTS bad passwords; OTPs expire, are
#     rate-limited on resend, and are invalidated after MAX_OTP_ATTEMPTS wrong guesses.
#   - Sessions = short-lived JWT access token + opaque rotating refresh token. Refresh tokens
#     are single-use; presenting an already-used one revokes the whole login "family"
#     (standard stolen-token signal). Logout revokes the access token's jti and the family.
#   - Google sign-in via the server-side authorization-code flow.
#
# Differences from khelomore, on purpose:
#   - Personal data is encrypted at rest (see crypto.py); emails are found via blind index.
#   - No AES "envelope" around request bodies: its key has to ship in the frontend bundle,
#     so it can't protect anything HTTPS doesn't already. Transport security is TLS.
#   - Google hands the browser a single-use, 60-second login code, not the tokens themselves,
#     so no session token ever appears in a URL (browser history, proxy logs, Referer).
#
# Roles: "student" (collection `students`, gets a roll number) and "staff" (collection
# `staff`, may only sign up with an email listed in STAFF_EMAILS).

import secrets
import uuid
from datetime import timedelta
from urllib.parse import urlencode, urlparse

import jwt
import requests
from bson import ObjectId
from django.conf import settings
from django.core import signing
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

from . import clock, crypto, input_validation
from .db_connection import db_main
from .email_handler import send_otp_email
from .errors import QueueError
from .students import next_roll_number

ROLES = ("student", "staff")
JWT_ALGORITHM = "HS256"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_STATE_SALT = "exam-cell.google-oauth-state"
GOOGLE_STATE_MAX_AGE = 600
OAUTH_CODE_TTL_SECONDS = 60

_indexes_ensured = False


class AuthError(Exception):
    """Authentication failed — always a 401 to the client."""


# ── Setup ─────────────────────────────────────────────────────────────────────

def _collection(role: str):
    if role == "student":
        return db_main.students
    if role == "staff":
        return db_main.staff
    raise QueueError("Please choose whether you are a student or staff member.", 400)


INDEX_CONFLICT_CODES = {85, 86}  # IndexOptionsConflict, IndexKeySpecsConflict


def _create_index(coll, key, **options):
    """create_index that migrates an older index of the same name but different options
    (e.g. `roll_1` from before roll became a partial index): MongoDB refuses to create the
    new one over it, which would otherwise take every auth request down."""
    try:
        coll.create_index(key, **options)
    except OperationFailure as e:
        if e.code not in INDEX_CONFLICT_CODES:
            raise
        coll.drop_index(f"{key}_1")
        coll.create_index(key, **options)


# Indexes left behind by earlier versions of the schema. They must go: e.g. `key_hash_1`
# (unique, from when students were identified by a browser key) treats every new account's
# missing key_hash as null, so the second signup ever would collide on it.
LEGACY_INDEXES = {"students": ("key_hash_1",)}


def _drop_legacy_indexes():
    for coll_name, index_names in LEGACY_INDEXES.items():
        coll = getattr(db_main, coll_name)
        existing = coll.index_information()
        for index_name in index_names:
            if index_name in existing:
                coll.drop_index(index_name)
                print(f"[ExamCell] Dropped legacy index {coll_name}.{index_name}")


def _duplicate_on(error: DuplicateKeyError, field: str) -> bool:
    """True if the duplicate-key error was on `field` (not some other unique index)."""
    details = error.details or {}
    return field in (details.get("keyPattern") or {}) or f"{field}_1" in str(details.get("errmsg", ""))


def ensure_indexes():
    global _indexes_ensured
    if _indexes_ensured:
        return
    _drop_legacy_indexes()
    for coll in (db_main.students, db_main.staff):
        _create_index(coll, "email_index", unique=True)
        _create_index(coll, "google_index", unique=True, partialFilterExpression={"google_index": {"$exists": True}})
    # Pending (unverified) students have no roll yet — only issued numbers must be unique.
    _create_index(db_main.students, "roll", unique=True, partialFilterExpression={"roll": {"$exists": True}})
    _create_index(db_main.refresh_tokens, "token_hash", unique=True)
    _create_index(db_main.refresh_tokens, "family_id")
    _create_index(db_main.refresh_tokens, "expires_at", expireAfterSeconds=0)
    _create_index(db_main.revoked_tokens, "jti", unique=True)
    _create_index(db_main.revoked_tokens, "expires_at", expireAfterSeconds=0)
    _create_index(db_main.oauth_codes, "code_hash", unique=True)
    _create_index(db_main.oauth_codes, "expires_at", expireAfterSeconds=0)
    _indexes_ensured = True


def reset_index_cache():
    """For tests, which drop the collections between cases."""
    global _indexes_ensured
    _indexes_ensured = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_role(role):
    if role not in ROLES:
        raise QueueError("Please choose whether you are a student or staff member.", 400)
    return role


def staff_email_allowed(email: str) -> bool:
    return crypto.normalise_email(email) in settings.STAFF_EMAILS


def _staff_not_allowed():
    return QueueError(
        "This email isn't authorised for staff access. Ask the exam cell administrator to add it.", 403,
    )


def _find_by_email(role: str, email: str):
    return _collection(role).find_one({"email_index": crypto.email_index(email)})


def _user_email(user) -> str:
    return crypto.decrypt_field(user["email_enc"], "email")


def _user_name(user) -> str:
    return crypto.decrypt_field(user["name_enc"], "name")


def public_user(user, role: str):
    data = {
        "id": str(user["_id"]),
        "role": role,
        "name": _user_name(user),
        "email": _user_email(user),
        "authProvider": user.get("auth_provider", "password"),
        "hasPassword": bool(user.get("password_hash")),
    }
    if role == "student":
        data["roll"] = user.get("roll")
    return data


def _check_not_suspended(user):
    if user.get("status") == "Suspended":
        raise QueueError("This account has been suspended. Please contact the exam cell.", 403)


def _activate(coll, user, role: str, session=None):
    """Pending -> Active. A student's roll number is assigned here, on activation, so
    abandoned (never-verified) sign-ups don't burn numbers."""
    updates = {"status": "Active", "activated_at": clock.now_utc()}
    if role == "student" and not user.get("roll"):
        updates["roll"] = next_roll_number(session)
    return coll.find_one_and_update(
        {"_id": user["_id"]}, {"$set": updates}, return_document=ReturnDocument.AFTER, session=session,
    )


def _issue_otp(coll, user, purpose: str):
    """Generates, stores (hashed) and emails a fresh OTP. purpose: signup | login | password_reset."""
    field = "reset_otp" if purpose == "password_reset" else "otp"
    sent_at = clock.aware(user.get(f"{field}_sent_at"))
    if sent_at:
        waited = (clock.now_utc() - sent_at).total_seconds()
        if waited < settings.OTP_RESEND_COOLDOWN_SECONDS:
            remaining = int(settings.OTP_RESEND_COOLDOWN_SECONDS - waited) + 1
            raise QueueError(f"Please wait {remaining} seconds before requesting another code.", 429,
                             retryAfter=remaining)
    code = crypto.generate_otp()
    now = clock.now_utc()
    coll.update_one(
        {"_id": user["_id"]},
        {"$set": {
            f"{field}_hash": crypto.hash_otp(code),
            f"{field}_expiry": now + timedelta(minutes=settings.OTP_EXPIRY_MINUTES),
            f"{field}_sent_at": now,
            f"{field}_purpose": purpose,
        }, "$unset": {f"{field}_attempts": ""}},
    )
    send_otp_email(_user_email(user), code, _user_name(user), purpose)


def _check_otp(coll, user, code, field: str):
    """Validates an OTP with expiry and attempt limiting. Raises QueueError on failure."""
    stored = user.get(f"{field}_hash")
    expiry = clock.aware(user.get(f"{field}_expiry"))
    if not stored or not expiry:
        raise QueueError("No verification code was requested. Please request a new one.", 400)
    if clock.now_utc() > expiry:
        raise QueueError("This code has expired. Please request a new one.", 400)
    if not isinstance(code, str) or not crypto.verify_otp(stored, code.strip()):
        attempts = int(user.get(f"{field}_attempts", 0)) + 1
        if attempts >= settings.MAX_OTP_ATTEMPTS:
            coll.update_one({"_id": user["_id"]}, {"$unset": {
                f"{field}_hash": "", f"{field}_expiry": "", f"{field}_attempts": "",
            }})
            raise QueueError("Too many incorrect attempts. Please request a new code.", 429)
        coll.update_one({"_id": user["_id"]}, {"$set": {f"{field}_attempts": attempts}})
        raise QueueError("Incorrect verification code.", 400)


def _otp_response(email: str, message: str):
    return {
        "status": "success",
        "message": message,
        "email": crypto.normalise_email(email),
        "otpExpiresInSeconds": settings.OTP_EXPIRY_MINUTES * 60,
        "resendCooldownSeconds": settings.OTP_RESEND_COOLDOWN_SECONDS,
    }


# ── Tokens ────────────────────────────────────────────────────────────────────

def _refresh_lifetime(role: str) -> int:
    # Staff sessions control the live queue — shorter "stay signed in", like khelomore's
    # admin bucket.
    return settings.STAFF_REFRESH_TOKEN_EXP_SECONDS if role == "staff" else settings.STUDENT_REFRESH_TOKEN_EXP_SECONDS


def generate_access_token(user_id: str, role: str) -> str:
    now = clock.now_utc()
    payload = {
        "sub": user_id,
        "role": role,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.ACCESS_TOKEN_EXP_SECONDS)).timestamp()),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=JWT_ALGORITHM)


def _issue_refresh_token(user_id: str, role: str, family_id=None) -> str:
    raw = secrets.token_urlsafe(48)
    db_main.refresh_tokens.insert_one({
        "token_hash": crypto.hash_token(raw),
        "user_id": user_id,
        "role": role,
        "family_id": family_id or uuid.uuid4().hex,
        "used": False,
        "created_at": clock.now_utc(),
        "expires_at": clock.now_utc() + timedelta(seconds=_refresh_lifetime(role)),
    })
    return raw


def _session_response(user, role: str, message: str):
    ensure_indexes()
    user_id = str(user["_id"])
    return {
        "status": "success",
        "message": message,
        "accessToken": generate_access_token(user_id, role),
        "refreshToken": _issue_refresh_token(user_id, role),
        "accessTokenExpiresIn": settings.ACCESS_TOKEN_EXP_SECONDS,
        "user": public_user(user, role),
    }


def _load_active_user(user_id: str, role: str):
    try:
        oid = ObjectId(user_id)
    except Exception:
        return None
    user = _collection(role).find_one({"_id": oid})
    if not user or user.get("status") != "Active":
        return None
    if role == "staff" and not staff_email_allowed(_user_email(user)):
        return None  # removed from STAFF_EMAILS — access ends at the next token check
    return user


def authenticate(token: str):
    """Validates an access token and returns the principal dict. Raises AuthError."""
    try:
        # Signature is verified by PyJWT; the time claims are checked below against
        # clock.now_utc() — the single source of time for the whole app (and what tests
        # freeze), instead of PyJWT reading the system clock separately.
        payload = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[JWT_ALGORITHM],
            options={"verify_exp": False, "verify_iat": False, "require": ["exp", "iat", "jti", "sub"]},
        )
    except jwt.InvalidTokenError:
        raise AuthError("Invalid session.")
    now_ts = clock.now_utc().timestamp()
    if not isinstance(payload.get("exp"), int) or now_ts >= payload["exp"]:
        raise AuthError("Session expired.")
    if not isinstance(payload.get("iat"), int) or payload["iat"] > now_ts + 60:
        raise AuthError("Invalid session.")
    jti, role, sub = payload.get("jti"), payload.get("role"), payload.get("sub")
    if not jti or role not in ROLES or not sub:
        raise AuthError("Invalid session.")
    if db_main.revoked_tokens.find_one({"jti": jti}):
        raise AuthError("Session has been signed out.")
    user = _load_active_user(sub, role)
    if not user:
        raise AuthError("Account is not active.")
    principal = {
        "id": sub,
        "_id": user["_id"],
        "role": role,
        "name": _user_name(user),
        "email": _user_email(user),
    }
    if role == "student":
        principal["roll"] = user.get("roll")
    return principal


def refresh_handler(raw_refresh):
    """Rotates a refresh token. Single-use; reuse revokes the whole family."""
    if not isinstance(raw_refresh, str) or not raw_refresh:
        return {"status": "error", "message": "Session expired. Please log in again."}, 401
    ensure_indexes()
    token_hash = crypto.hash_token(raw_refresh)
    # Atomic claim — two concurrent refreshes with the same token can't both succeed.
    doc = db_main.refresh_tokens.find_one_and_update(
        {"token_hash": token_hash, "used": False}, {"$set": {"used": True, "used_at": clock.now_utc()}},
    )
    if doc is None:
        existing = db_main.refresh_tokens.find_one({"token_hash": token_hash})
        if existing and existing.get("used"):
            db_main.refresh_tokens.update_many(
                {"family_id": existing["family_id"]},
                {"$set": {"used": True, "revoked_reason": "reuse_detected"}},
            )
        return {"status": "error", "message": "Session expired. Please log in again."}, 401
    expires_at = clock.aware(doc.get("expires_at"))
    if expires_at is None or clock.now_utc() > expires_at:
        return {"status": "error", "message": "Session expired. Please log in again."}, 401
    user = _load_active_user(doc["user_id"], doc["role"])
    if not user:
        db_main.refresh_tokens.update_many({"family_id": doc["family_id"]}, {"$set": {"used": True}})
        return {"status": "error", "message": "Account is not active."}, 401
    return {
        "status": "success",
        "accessToken": generate_access_token(doc["user_id"], doc["role"]),
        "refreshToken": _issue_refresh_token(doc["user_id"], doc["role"], family_id=doc["family_id"]),
        "accessTokenExpiresIn": settings.ACCESS_TOKEN_EXP_SECONDS,
        "user": public_user(user, doc["role"]),
    }, 200


def logout_handler(access_token, raw_refresh):
    """Revokes the access token (by jti, until it would have expired anyway) and the whole
    refresh family. Safe with missing/garbage input."""
    ensure_indexes()
    if isinstance(access_token, str) and access_token:
        try:
            payload = jwt.decode(access_token, settings.JWT_SECRET, algorithms=[JWT_ALGORITHM],
                                 options={"verify_exp": False, "verify_iat": False})
            if payload.get("jti"):
                expires = clock.now_utc() + timedelta(seconds=settings.ACCESS_TOKEN_EXP_SECONDS)
                db_main.revoked_tokens.update_one(
                    {"jti": payload["jti"]}, {"$set": {"jti": payload["jti"], "expires_at": expires}}, upsert=True,
                )
        except jwt.InvalidTokenError:
            pass
    if isinstance(raw_refresh, str) and raw_refresh:
        doc = db_main.refresh_tokens.find_one({"token_hash": crypto.hash_token(raw_refresh)})
        if doc:
            db_main.refresh_tokens.update_many(
                {"family_id": doc["family_id"]}, {"$set": {"used": True, "revoked_reason": "logout"}},
            )
    return {"status": "success", "message": "Logged out."}, 200


def _revoke_all_sessions(user_id: str):
    db_main.refresh_tokens.update_many({"user_id": user_id}, {"$set": {"used": True, "revoked_reason": "password_reset"}})


# ── Traditional signup / login ───────────────────────────────────────────────

def register_handler(data):
    """Signup step 1: create a Pending account and email an OTP. No session yet."""
    try:
        role = _validate_role(data.get("role"))
        name, email, password = data.get("name"), data.get("email"), data.get("password")
        error = (
            input_validation.validate_person_name(name)
            or input_validation.validate_email(email)
            or input_validation.validate_password_strength(password)
        )
        if error:
            raise QueueError(error, 400)
        if role == "staff" and not staff_email_allowed(email):
            raise _staff_not_allowed()
        ensure_indexes()
        coll = _collection(role)
        name = " ".join(name.split())
        email = crypto.normalise_email(email)

        existing = _find_by_email(role, email)
        if existing and existing.get("status") != "Pending":
            raise QueueError("An account with this email already exists. Please log in instead.", 409)

        fields = {
            "role": role,
            "email_index": crypto.email_index(email),
            "email_enc": crypto.encrypt_field(email, "email"),
            "name_enc": crypto.encrypt_field(name, "name"),
            "password_hash": crypto.hash_password(password),
            "auth_provider": "password",
            "status": "Pending",
        }
        if existing:
            # Signing up again before verifying: replace the details, send a new code.
            coll.update_one({"_id": existing["_id"]}, {"$set": fields})
            user = coll.find_one({"_id": existing["_id"]})
        else:
            try:
                result = coll.insert_one({**fields, "created_at": clock.now_utc()})
            except DuplicateKeyError as e:
                # Only an email collision means "already exists" — anything else is a real
                # server problem and must not be disguised as one.
                if not _duplicate_on(e, "email_index"):
                    raise
                raise QueueError("An account with this email already exists. Please log in instead.", 409)
            user = coll.find_one({"_id": result.inserted_id})
        _issue_otp(coll, user, "signup")
        return _otp_response(email, "We've emailed you a 6-digit verification code."), 200
    except QueueError as e:
        return e.response()


def login_handler(data):
    """Login step 1: check the password, then email an OTP. No session yet."""
    try:
        role = _validate_role(data.get("role"))
        email, password = data.get("email"), data.get("password")
        if not isinstance(email, str) or not isinstance(password, str) or not email or not password:
            raise QueueError("Please enter your email and password.", 400)
        ensure_indexes()
        coll = _collection(role)
        invalid = QueueError("Invalid email or password.", 401)
        user = _find_by_email(role, email)
        if not user:
            # Spend the same Argon2 time as a real check, so response timing doesn't reveal
            # which emails have accounts.
            crypto.verify_password(crypto.hash_password("timing-equaliser"), password)
            raise invalid
        _check_not_suspended(user)

        locked_until = clock.aware(user.get("login_locked_until"))
        if locked_until and clock.now_utc() < locked_until:
            raise QueueError("Too many failed login attempts. Please try again later.", 429)
        if not user.get("password_hash"):
            raise QueueError("This account uses Google sign-in. Use \"Continue with Google\", or reset your password to add one.", 400)
        if not crypto.verify_password(user["password_hash"], password):
            attempts = int(user.get("login_attempts", 0)) + 1
            update: dict = {"login_attempts": attempts}
            if attempts >= settings.MAX_LOGIN_ATTEMPTS:
                update["login_locked_until"] = clock.now_utc() + timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
                update["login_attempts"] = 0
            coll.update_one({"_id": user["_id"]}, {"$set": update})
            raise invalid
        if role == "staff" and not staff_email_allowed(email):
            raise _staff_not_allowed()

        cleanup = {"$unset": {"login_attempts": "", "login_locked_until": ""}}
        if crypto.password_needs_rehash(user["password_hash"]):
            cleanup["$set"] = {"password_hash": crypto.hash_password(password)}
        coll.update_one({"_id": user["_id"]}, cleanup)

        _issue_otp(coll, user, "signup" if user.get("status") == "Pending" else "login")
        return _otp_response(email, "We've emailed you a 6-digit login code."), 200
    except QueueError as e:
        return e.response()


def verify_otp_handler(data):
    """Step 2 of both signup and login: check the OTP, activate if new, start a session."""
    try:
        role = _validate_role(data.get("role"))
        email, code = data.get("email"), data.get("otp")
        if not isinstance(email, str) or not email:
            raise QueueError("Email is required.", 400)
        ensure_indexes()
        coll = _collection(role)
        user = _find_by_email(role, email)
        if not user:
            raise QueueError("No verification in progress for this email. Please start again.", 404)
        _check_not_suspended(user)
        if role == "staff" and not staff_email_allowed(email):
            raise _staff_not_allowed()
        _check_otp(coll, user, code, "otp")

        coll.update_one({"_id": user["_id"]}, {"$unset": {
            "otp_hash": "", "otp_expiry": "", "otp_attempts": "", "otp_sent_at": "", "otp_purpose": "",
        }})
        is_new = user.get("status") == "Pending"
        if is_new:
            user = _activate(coll, user, role)
        body = _session_response(user, role, "Account created." if is_new else "Logged in.")
        body["isNew"] = is_new
        return body, 200
    except QueueError as e:
        return e.response()


def resend_otp_handler(data):
    try:
        role = _validate_role(data.get("role"))
        email = data.get("email")
        if not isinstance(email, str) or not email:
            raise QueueError("Email is required.", 400)
        ensure_indexes()
        coll = _collection(role)
        user = _find_by_email(role, email)
        # Only resend if a login/signup code is actually in progress for this account.
        if user and user.get("otp_hash") and user.get("status") != "Suspended":
            _issue_otp(coll, user, user.get("otp_purpose") or "login")
        return _otp_response(email, "If a verification is in progress, a new code has been sent."), 200
    except QueueError as e:
        return e.response()


def forgot_password_handler(data):
    """Always the same answer whether or not the account exists (no email enumeration)."""
    try:
        role = _validate_role(data.get("role"))
        email = data.get("email")
        error = input_validation.validate_email(email)
        if error:
            raise QueueError(error, 400)
        ensure_indexes()
        coll = _collection(role)
        user = _find_by_email(role, email)
        if user and user.get("status") == "Active":
            try:
                _issue_otp(coll, user, "password_reset")
            except QueueError as e:
                if e.status_code != 429:
                    raise
                # Cooldown: stay silent rather than confirm the account exists.
        return _otp_response(email, "If an account exists for this email, a reset code has been sent."), 200
    except QueueError as e:
        return e.response()


def reset_password_handler(data):
    try:
        role = _validate_role(data.get("role"))
        email, code, new_password = data.get("email"), data.get("otp"), data.get("password")
        error = input_validation.validate_password_strength(new_password)
        if error:
            raise QueueError(error, 400)
        if not isinstance(email, str) or not email:
            raise QueueError("Email is required.", 400)
        ensure_indexes()
        coll = _collection(role)
        user = _find_by_email(role, email)
        if not user or user.get("status") != "Active":
            raise QueueError("Invalid or expired reset code. Please request a new one.", 400)
        _check_otp(coll, user, code, "reset_otp")
        coll.update_one({"_id": user["_id"]}, {
            "$set": {"password_hash": crypto.hash_password(new_password)},
            "$unset": {
                "reset_otp_hash": "", "reset_otp_expiry": "", "reset_otp_attempts": "",
                "reset_otp_sent_at": "", "reset_otp_purpose": "",
                "login_attempts": "", "login_locked_until": "",
            },
        })
        # A password reset signs out every existing session for the account.
        _revoke_all_sessions(str(user["_id"]))
        return {"status": "success", "message": "Password updated. Please log in with your new password."}, 200
    except QueueError as e:
        return e.response()


def me_handler(principal):
    user = _collection(principal["role"]).find_one({"_id": principal["_id"]})
    return {"status": "success", "user": public_user(user, principal["role"])}, 200


# ── Google sign-in ────────────────────────────────────────────────────────────

def google_configured() -> bool:
    return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)


def _google_redirect_uri() -> str:
    return f"{settings.BACKEND_URL.rstrip('/')}/api/v1/main/auth/google/callback/"


def allowed_return_url(url) -> bool:
    """Only redirect back to our own frontend origins — never an arbitrary site, which
    would hand it the one-time login code."""
    if not isinstance(url, str) or not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return origin in settings.FRONTEND_ORIGINS


def _with_query(url: str, **params) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(params)}"


def google_login_url(role, return_url):
    """Builds the Google consent URL. Role + return URL travel in a signed `state`, so they
    can't be tampered with on the way back."""
    _validate_role(role)
    if not google_configured():
        raise QueueError("Google sign-in isn't configured on the server yet.", 503)
    if not allowed_return_url(return_url):
        raise QueueError("Unauthorised redirect target.", 400)
    state = signing.dumps({"role": role, "return_url": return_url, "nonce": secrets.token_urlsafe(8)},
                          salt=GOOGLE_STATE_SALT)
    return GOOGLE_AUTH_URL + "?" + urlencode({
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": _google_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "prompt": "select_account",
        "state": state,
    })


def _verify_google_code(code: str):
    """Exchanges the authorization code and verifies the ID token. Returns its claims."""
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    response = requests.post(GOOGLE_TOKEN_URL, data={
        "code": code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": _google_redirect_uri(),
        "grant_type": "authorization_code",
    }, timeout=15)
    if response.status_code != 200:
        print(f"[GOOGLE] Token exchange failed: {response.text}")
        raise QueueError("Google sign-in failed. Please try again.", 400)
    raw_id_token = response.json().get("id_token")
    if not raw_id_token:
        raise QueueError("Google sign-in failed. Please try again.", 400)
    claims = id_token.verify_oauth2_token(raw_id_token, google_requests.Request(), settings.GOOGLE_CLIENT_ID)
    if not claims.get("email") or not claims.get("email_verified"):
        raise QueueError("Your Google account's email address isn't verified.", 400)
    return claims


def google_get_or_create(role: str, email: str, name: str, google_sub: str, _retried: bool = False):
    """Finds (by Google id, then email) or creates the account. Google has verified the
    email, so an existing password account with that email is linked, and a Pending one is
    activated."""
    ensure_indexes()
    coll = _collection(role)
    email = crypto.normalise_email(email)
    if role == "staff" and not staff_email_allowed(email):
        raise _staff_not_allowed()
    google_index = crypto.blind_index(google_sub, "google")

    user = coll.find_one({"google_index": google_index}) or _find_by_email(role, email)
    if user:
        _check_not_suspended(user)
        if user.get("google_index") != google_index:
            coll.update_one({"_id": user["_id"]}, {"$set": {"google_index": google_index}})
        if user.get("status") == "Pending":
            user = _activate(coll, user, role)
        return coll.find_one({"_id": user["_id"]}), False

    clean_name = " ".join((name or "").split())
    if input_validation.validate_person_name(clean_name):
        clean_name = email.split("@")[0]
    try:
        result = coll.insert_one({
            "role": role,
            "email_index": crypto.email_index(email),
            "email_enc": crypto.encrypt_field(email, "email"),
            "name_enc": crypto.encrypt_field(clean_name, "name"),
            "google_index": google_index,
            "auth_provider": "google",
            "status": "Pending",
            "created_at": clock.now_utc(),
        })
    except DuplicateKeyError as e:
        # Created by a concurrent request a moment ago — use that one (once; anything else is
        # a real error, not a race, and retrying it would loop forever).
        if _retried or not (_duplicate_on(e, "email_index") or _duplicate_on(e, "google_index")):
            raise
        return google_get_or_create(role, email, name, google_sub, _retried=True)
    user = _activate(coll, coll.find_one({"_id": result.inserted_id}), role)
    return user, True


def google_callback(code, state):
    """Returns the URL to send the browser back to: the frontend callback page with either
    a one-time `code` or an `error`."""
    try:
        payload = signing.loads(state or "", salt=GOOGLE_STATE_SALT, max_age=GOOGLE_STATE_MAX_AGE)
    except signing.BadSignature:
        return _with_query(f"{settings.FRONTEND_URL.rstrip('/')}/auth/callback",
                           error="Sign-in link expired or was tampered with. Please try again.")
    return_url, role = payload["return_url"], payload["role"]
    if not allowed_return_url(return_url):
        return _with_query(f"{settings.FRONTEND_URL.rstrip('/')}/auth/callback", error="Unauthorised redirect target.")
    try:
        if not code:
            raise QueueError("Google sign-in was cancelled.", 400)
        claims = _verify_google_code(code)
        user, is_new = google_get_or_create(role, claims["email"], claims.get("name", ""), claims["sub"])
        raw_code = secrets.token_urlsafe(32)
        db_main.oauth_codes.insert_one({
            "code_hash": crypto.hash_token(raw_code),
            "user_id": str(user["_id"]),
            "role": role,
            "is_new": is_new,
            "expires_at": clock.now_utc() + timedelta(seconds=OAUTH_CODE_TTL_SECONDS),
        })
        return _with_query(return_url, code=raw_code)
    except QueueError as e:
        return _with_query(return_url, error=e.message)
    except Exception as e:
        print(f"[GOOGLE] Sign-in error: {e}")
        return _with_query(return_url, error="Google sign-in failed. Please try again.")


def google_exchange_handler(data):
    """Swaps the one-time code from the callback redirect for a session. Single use."""
    code = data.get("code")
    if not isinstance(code, str) or not code:
        return {"status": "error", "message": "Sign-in code missing."}, 400
    ensure_indexes()
    doc = db_main.oauth_codes.find_one_and_delete({
        "code_hash": crypto.hash_token(code), "expires_at": {"$gt": clock.now_utc()},
    })
    if not doc:
        return {"status": "error", "message": "Sign-in link expired. Please try again."}, 400
    user = _load_active_user(doc["user_id"], doc["role"])
    if not user:
        return {"status": "error", "message": "Account is not active."}, 403
    body = _session_response(user, doc["role"], "Signed in with Google.")
    body["isNew"] = doc.get("is_new", False)
    return body, 200


# ── Direct account creation (seed script / tests) ────────────────────────────

def create_active_account(role: str, name: str, email: str, password: str | None = None):
    """Creates an already-verified account, bypassing OTP. Never exposed over HTTP."""
    ensure_indexes()
    coll = _collection(_validate_role(role))
    email = crypto.normalise_email(email)
    doc = {
        "role": role,
        "email_index": crypto.email_index(email),
        "email_enc": crypto.encrypt_field(email, "email"),
        "name_enc": crypto.encrypt_field(" ".join(name.split()), "name"),
        "auth_provider": "password" if password else "seed",
        "status": "Pending",
        "created_at": clock.now_utc(),
    }
    if password:
        doc["password_hash"] = crypto.hash_password(password)
    result = coll.insert_one(doc)
    return _activate(coll, coll.find_one({"_id": result.inserted_id}), role)
