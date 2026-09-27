# crypto.py
# Everything secret or personal that touches the database goes through here.
#
#   Passwords          -> Argon2id (argon2-cffi), OWASP parameters. One-way.
#   Emails, names      -> AES-256-GCM field encryption, fresh 96-bit nonce per value.
#                         GCM is authenticated: a tampered ciphertext fails to decrypt
#                         instead of silently decrypting to garbage (CBC can't detect that).
#   Email lookups      -> blind index: HMAC-SHA256(BLIND_INDEX_KEY, normalised email). Lets
#                         login find an account without the email ever being stored in
#                         plaintext, and without being reversible from a DB dump.
#   OTP codes          -> HMAC-SHA256(BLIND_INDEX_KEY, "otp:" + code) — keyed, so a leaked
#                         DB can't be brute-forced offline across the 10^6 code space.
#   Refresh tokens,
#   Google login codes -> SHA-256 (they're 256+ bits of randomness — a keyed hash adds
#                         nothing, and the raw value is never stored).
#
# Two independent keys (FIELD_ENCRYPTION_KEY, BLIND_INDEX_KEY) so compromise of one
# doesn't expose the other's data. Ciphertexts carry a version prefix ("v1:") so the key
# can be rotated later without guessing which key produced which value.

import base64
import hashlib
import hmac
import os
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings

CIPHERTEXT_VERSION = "v1"
NONCE_BYTES = 12

# Argon2id (argon2-cffi's default type) with OWASP's recommended minimum: m=19 MiB, t=2,
# p=1 — the same sizing khelomore-server settled on so a login can't blow through a
# free-tier host's worker timeout.
password_hasher = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)


def _decode_key(name: str) -> bytes:
    raw = getattr(settings, name, "")
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception:
        raise RuntimeError(f"{name} must be base64-encoded.") from None
    if len(key) != 32:
        raise RuntimeError(f"{name} must decode to exactly 32 bytes (got {len(key)}).")
    return key


def _field_key() -> bytes:
    return _decode_key("FIELD_ENCRYPTION_KEY")


def _index_key() -> bytes:
    return _decode_key("BLIND_INDEX_KEY")


# ── Passwords ────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    if not stored_hash or not password:
        return False
    try:
        return password_hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(stored_hash: str) -> bool:
    """True when the stored hash used older/weaker parameters — upgraded on next login."""
    try:
        return password_hasher.check_needs_rehash(stored_hash)
    except Exception:
        return False


# ── Field encryption (AES-256-GCM) ───────────────────────────────────────────

def encrypt_field(plaintext: str, context: str = "") -> str:
    """
    Encrypts a string for storage. `context` is bound in as GCM associated data (e.g.
    "email", "name") so a ciphertext copied from one field into another fails to decrypt
    rather than being read as the wrong kind of value.
    """
    if plaintext is None:
        return None
    nonce = os.urandom(NONCE_BYTES)
    ciphertext = AESGCM(_field_key()).encrypt(nonce, plaintext.encode("utf-8"), context.encode("utf-8"))
    return f"{CIPHERTEXT_VERSION}:{base64.b64encode(nonce + ciphertext).decode('ascii')}"


def decrypt_field(stored: str, context: str = "") -> str:
    if stored is None:
        return None
    version, _, payload = stored.partition(":")
    if version != CIPHERTEXT_VERSION or not payload:
        raise ValueError("Unrecognised ciphertext format.")
    blob = base64.b64decode(payload)
    nonce, ciphertext = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    return AESGCM(_field_key()).decrypt(nonce, ciphertext, context.encode("utf-8")).decode("utf-8")


# ── Keyed / plain hashes ─────────────────────────────────────────────────────

def normalise_email(email: str) -> str:
    return email.strip().lower()


def blind_index(value: str, purpose: str) -> str:
    """Deterministic keyed hash for equality lookups on encrypted fields. `purpose`
    separates index spaces (the same email under "email" and "google" hashes differently)."""
    message = f"{purpose}:{value}".encode("utf-8")
    return hmac.new(_index_key(), message, hashlib.sha256).hexdigest()


def email_index(email: str) -> str:
    return blind_index(normalise_email(email), "email")


def hash_otp(code: str) -> str:
    return blind_index(code, "otp")


def verify_otp(stored_hash: str, code: str) -> bool:
    if not stored_hash or not code:
        return False
    return hmac.compare_digest(hash_otp(code), stored_hash)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_otp() -> str:
    # secrets, not random — OTPs are authentication secrets.
    return f"{secrets.randbelow(1_000_000):06d}"


def generate_key_b64() -> str:
    """For .env setup: python -c "from exam_project.main.Handlers.crypto import generate_key_b64; print(generate_key_b64())" """
    return base64.b64encode(os.urandom(32)).decode("ascii")
