# clock.py
# Every time/date decision in the queue goes through here — the exam cell's business day,
# appointment slot times, durations. Two reasons for one module:
#   1. "Today" is the exam cell's local (IST) day, not UTC — a UTC day would roll the queue
#      over at 5:30 AM.
#   2. Tests patch clock.now_utc to freeze or move time; every caller uses clock.now_utc()
#      through the module (never `from .clock import now_utc`) so that patch reaches them.

import math
import re
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_ist() -> datetime:
    return now_utc().astimezone(IST)


def today() -> date:
    return now_ist().date()


def today_str() -> str:
    return today().isoformat()


def day_start_utc(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=IST).astimezone(timezone.utc)


def slot_start_utc(day_str: str, slot_minutes: int) -> datetime:
    """UTC instant an appointment slot starts, e.g. ("2026-09-27", 630) -> 10:30 AM IST."""
    return day_start_utc(date.fromisoformat(day_str)) + timedelta(minutes=slot_minutes)


def parse_date(value) -> date:
    """Strict YYYY-MM-DD only. Raises ValueError with a user-facing message."""
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise ValueError("Date must be in YYYY-MM-DD format.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("Please pick a valid date.") from None


def aware(dt):
    """pymongo hands datetimes back naive (implicitly UTC) — make them comparable with
    clock.now_utc()."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_ms(dt):
    dt = aware(dt)
    return int(dt.timestamp() * 1000) if dt else None


def minutes_between(start, end) -> float:
    return (aware(end) - aware(start)).total_seconds() / 60


def ceil_minutes(value: float) -> int:
    return max(0, math.ceil(value))
