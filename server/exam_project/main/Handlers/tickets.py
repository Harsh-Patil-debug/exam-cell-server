# tickets.py
# Student-side ticket handlers: take a token (walk-in or appointment), look one up, cancel
# your own, and check appointment slot availability.
#
# Every ticket belongs to the logged-in student account that took it: name and roll number
# come from the account (never from request input), the name is stored encrypted, and only
# the owner can cancel or track it.

from datetime import timedelta

from django.conf import settings
from pymongo.errors import DuplicateKeyError

from . import clock, crypto
from .db_connection import db_main, run_transaction
from .errors import QueueError
from .events import log_event
from .notifications import notify_staff_new_ticket
from .queue_state import ensure_business_day, load_forecast, next_ticket_numbers, serialize_ticket
from .services import MODES, SERVICE_IDS, SLOT_MINUTES

OPEN_STATUSES = ["waiting", "serving"]


def _slot_booking_id(day: str, slot: str) -> str:
    return f"{day}|{slot}"


def _validate_appointment(date_value, slot):
    """Returns (day_str, slot_minutes) or raises QueueError."""
    try:
        day = clock.parse_date(date_value)
    except ValueError as e:
        raise QueueError(str(e))
    today = clock.today()
    if day < today:
        raise QueueError("Appointments can't be booked for a past date.")
    if day > today + timedelta(days=settings.BOOKING_WINDOW_DAYS):
        raise QueueError(f"Appointments can be booked at most {settings.BOOKING_WINDOW_DAYS} days ahead.")
    if not isinstance(slot, str) or slot not in SLOT_MINUTES:
        raise QueueError("Please pick a valid appointment slot.")
    minutes = SLOT_MINUTES[slot]
    if clock.slot_start_utc(day.isoformat(), minutes) <= clock.now_utc():
        raise QueueError("That slot has already started — please pick a later one.")
    return day.isoformat(), minutes


def _ticket_estimate(ticket):
    """(students_ahead, minutes_until_called, predicted_call_time) for one ticket, from the
    same forecast as the full queue view — the tracker and the display always agree."""
    if ticket["status"] != "waiting":
        return None
    if ticket.get("date") != clock.today_str():
        # Booked for a later day: not in today's line yet, so no position — but the student
        # still gets a countdown to when they can be called (slot start minus the grace
        # window — the same rule the forecast applies on the day itself).
        if ticket.get("slot_minutes") is None:
            return None
        call_at = (clock.slot_start_utc(ticket["date"], ticket["slot_minutes"])
                   - timedelta(minutes=settings.APPOINTMENT_GRACE_MINUTES))
        return None, clock.ceil_minutes(clock.minutes_between(clock.now_utc(), call_at)), call_at
    _, forecasts, *_ = load_forecast()
    return forecasts.get(ticket["id"])


def take_token_handler(data, principal):
    """principal: the authenticated student (see auth_middleware). Name and roll number come
    from the account — anything similar in the request body is ignored."""
    service = data.get("service")
    mode = data.get("mode", "walk-in")

    try:
        if not isinstance(service, str) or service not in SERVICE_IDS:
            raise QueueError("Please select a valid service.")
        if not isinstance(mode, str) or mode not in MODES:
            raise QueueError("Mode must be 'walk-in' or 'appointment'.")

        ensure_business_day()
        if mode == "appointment":
            day, slot_minutes = _validate_appointment(data.get("date"), data.get("slot"))
            slot = data.get("slot")
        else:
            day, slot, slot_minutes = clock.today_str(), None, None

        student_id = principal["_id"]
        roll = principal["roll"]

        def txn(session):
            if slot is not None:
                booking_id = _slot_booking_id(day, slot)
                db_main.slot_bookings.update_one(
                    {"_id": booking_id},
                    {"$setOnInsert": {"date": day, "slot": slot, "count": 0}},
                    upsert=True, session=session,
                )
                # Conditional $inc: the count can only go up while below capacity, so two
                # students can't both take the last place in a slot.
                taken = db_main.slot_bookings.update_one(
                    {"_id": booking_id, "count": {"$lt": settings.SLOT_CAPACITY}},
                    {"$inc": {"count": 1}},
                    session=session,
                )
                if taken.modified_count == 0:
                    raise QueueError("That slot is fully booked — please pick another.", 409)

            ticket_id, number = next_ticket_numbers(session, day)
            doc = {
                "id": ticket_id,
                "token": f"TK-{number}",
                "token_number": number,
                "date": day,
                "service": service,
                # Encrypted at rest (AES-256-GCM); decrypted only when serialized.
                "student_enc": crypto.encrypt_field(principal["name"], "name"),
                "roll": roll,
                "student_ref": student_id,
                "mode": mode,
                "status": "waiting",
                "counter": None,
                "slot": slot,
                "slot_minutes": slot_minutes,
                "created_at": clock.now_utc(),
                "called_at": None,
                "closed_at": None,
                # One open ticket per student per day (unique partial index; $unset on close).
                "active_key": f"{day}|{student_id}",
            }
            db_main.tickets.insert_one(doc, session=session)
            log_event(session, "issued", doc, mode=mode, service=service, slot=slot, day=day)
            return doc

        try:
            doc = run_transaction(txn)
        except DuplicateKeyError:
            existing = db_main.tickets.find_one({"active_key": f"{day}|{student_id}"}, {"token": 1}) or {}
            raise QueueError(
                f"You already have an active token for this day "
                f"({existing.get('token', 'unknown')}). Cancel it first to take a new one.",
                409,
            )

        estimate = _ticket_estimate(doc)
        # After commit only (see notifications.py): tell the staff someone new is waiting.
        waiting_total = db_main.tickets.count_documents({"date": clock.today_str(), "status": "waiting"})
        notify_staff_new_ticket(doc, estimate, waiting_total)
        return {
            "status": "success",
            # The student is looking at their own ticket — no masking needed.
            "ticket": serialize_ticket(doc, full=True, estimate=estimate),
        }, 201
    except QueueError as e:
        return e.response()
    except Exception as e:
        return {"status": "error", "message": f"Failed to issue token: {str(e)}"}, 500


def _owns(ticket, principal) -> bool:
    return ticket.get("student_ref") == principal["_id"]


def get_ticket_handler(ticket_id: int, principal):
    """Staff can look up any ticket; a student only their own (others look like 404)."""
    try:
        ensure_business_day()
        doc = db_main.tickets.find_one({"id": ticket_id})
        is_staff = principal["role"] == "staff"
        if not doc or (not is_staff and not _owns(doc, principal)):
            return {"status": "error", "message": "Ticket not found."}, 404
        return {
            "status": "success",
            "ticket": serialize_ticket(doc, full=True, estimate=_ticket_estimate(doc)),
        }, 200
    except Exception as e:
        return {"status": "error", "message": f"Failed to load ticket: {str(e)}"}, 500


def get_my_ticket_handler(principal):
    """
    The ticket the student's tracker should show, from the database — works on any device
    they log in on:
      1. an open ticket for today (waiting / being served),
      2. else their next upcoming appointment on a later day,
      3. else the ticket they finished today (completed / missed), so they see the outcome.
    """
    try:
        ensure_business_day()
        today = clock.today_str()
        mine = {"student_ref": principal["_id"]}
        doc = (
            db_main.tickets.find_one({**mine, "date": today, "status": {"$in": OPEN_STATUSES}})
            or db_main.tickets.find_one(
                {**mine, "date": {"$gt": today}, "status": "waiting"}, sort=[("date", 1), ("slot_minutes", 1)],
            )
            or db_main.tickets.find_one(
                {**mine, "date": today, "status": {"$in": ["completed", "skipped"]}}, sort=[("closed_at", -1)],
            )
        )
        return {
            "status": "success",
            "ticket": serialize_ticket(doc, full=True, estimate=_ticket_estimate(doc)) if doc else None,
        }, 200
    except Exception as e:
        return {"status": "error", "message": f"Failed to load your ticket: {str(e)}"}, 500


def cancel_ticket_handler(ticket_id: int, principal):
    try:
        ensure_business_day()
        doc = db_main.tickets.find_one({"id": ticket_id}, {"student_ref": 1})
        if not doc or not _owns(doc, principal):
            return {"status": "error", "message": "Ticket not found."}, 404

        def txn(session):
            # Conditional on still waiting: if staff call this ticket to a counter at the
            # same moment, the cancel loses rather than leaving a counter pointing at a
            # cancelled ticket.
            ticket = db_main.tickets.find_one_and_update(
                {"id": ticket_id, "status": "waiting"},
                {"$set": {"status": "cancelled", "closed_at": clock.now_utc(), "close_reason": "student"},
                 "$unset": {"active_key": ""}},
                session=session,
            )
            if not ticket:
                current = db_main.tickets.find_one({"id": ticket_id}, {"status": 1}, session=session) or {}
                raise QueueError(
                    f"Ticket can no longer be cancelled (it is {current.get('status', 'closed')}).", 409,
                    ticketStatus=current.get("status"),
                )
            if ticket.get("slot"):
                # Give the appointment place back so someone else can book it.
                db_main.slot_bookings.update_one(
                    {"_id": _slot_booking_id(ticket["date"], ticket["slot"]), "count": {"$gt": 0}},
                    {"$inc": {"count": -1}},
                    session=session,
                )
            log_event(session, "cancelled", ticket)

        run_transaction(txn)
        return {"status": "success", "message": "Token cancelled."}, 200
    except QueueError as e:
        return e.response()
    except Exception as e:
        return {"status": "error", "message": f"Failed to cancel token: {str(e)}"}, 500


def get_slot_availability_handler(date_value):
    """Every slot for a date with how many places are left, and whether it's still
    bookable (not full, not already started)."""
    try:
        day = clock.parse_date(date_value)
    except ValueError as e:
        return {"status": "error", "message": str(e)}, 400
    today = clock.today()
    if day < today or day > today + timedelta(days=settings.BOOKING_WINDOW_DAYS):
        return {
            "status": "error",
            "message": f"Pick a date between today and {settings.BOOKING_WINDOW_DAYS} days ahead.",
        }, 400
    try:
        day_str = day.isoformat()
        booked = {b["slot"]: b.get("count", 0) for b in db_main.slot_bookings.find({"date": day_str})}
        now = clock.now_utc()
        slots = []
        for label, minutes in SLOT_MINUTES.items():
            remaining = max(settings.SLOT_CAPACITY - booked.get(label, 0), 0)
            started = clock.slot_start_utc(day_str, minutes) <= now
            slots.append({
                "slot": label,
                "remaining": remaining,
                "past": started,
                "available": remaining > 0 and not started,
            })
        return {
            "status": "success",
            "date": day_str,
            "capacity": settings.SLOT_CAPACITY,
            "maxDate": (today + timedelta(days=settings.BOOKING_WINDOW_DAYS)).isoformat(),
            "slots": slots,
        }, 200
    except Exception as e:
        return {"status": "error", "message": f"Failed to load slots: {str(e)}"}, 500
