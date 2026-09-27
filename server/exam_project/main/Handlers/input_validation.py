# input_validation.py
# Shared server-side validation for user-submitted form fields. The student portal's form
# is only a UX nicety — anyone can call the API directly (curl, Postman), so these are the
# real, unbypassable checks.
#
# Every function returns None if the value is acceptable, otherwise a human-readable error
# string.

import re

# Letters (any script), spaces, and the punctuation real names use — rejects markup and
# control characters that would otherwise be rendered on the public lobby display.
STUDENT_NAME_RE = re.compile(r"^[^\W\d_]+(?:[ .'\-]+[^\W\d_]+)*\.?$", re.UNICODE)

MAX_STUDENT_NAME_LENGTH = 80
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128


def validate_person_name(name) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return "Please enter your full name."
    name = " ".join(name.split())
    if len(name) < 2:
        return "Please enter your full name."
    if len(name) > MAX_STUDENT_NAME_LENGTH:
        return f"Name must be at most {MAX_STUDENT_NAME_LENGTH} characters."
    if not STUDENT_NAME_RE.match(name):
        return "Name may only contain letters, spaces, dots, apostrophes and hyphens."
    return None


def parse_positive_int(value, field_name: str):
    """
    Returns (parsed_int, None) or (None, error_message). Rejects bools and dicts — the
    exact shape a NoSQL-injection attempt ({"$ne": null}) would send instead of a number.
    """
    if isinstance(value, bool) or isinstance(value, (dict, list)) or value is None:
        return None, f"{field_name} must be a positive whole number."
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None, f"{field_name} must be a positive whole number."
    if parsed <= 0:
        return None, f"{field_name} must be a positive whole number."
    return parsed, None


def validate_email(email) -> str | None:
    if not isinstance(email, str) or not email.strip():
        return "Please enter your email address."
    email = email.strip()
    if len(email) > 254 or not EMAIL_RE.match(email):
        return "Please enter a valid email address."
    return None


def validate_password_strength(password) -> str | None:
    if not isinstance(password, str) or not password:
        return "Please enter a password."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return "Password must contain at least one letter and one number."
    return None
