# appointments.py
# Staff-side appointment management: see every booking for a date (with per-slot
# occupancy) and cancel one with a reason.
#
# Appointments are ordinary tickets with mode="appointment" — on their day they join the
# live queue and Call Next serves them ahead of walk-ins once due (see queue_state). This
# module is the planning view on top of that: the future-day bookings the live queue
# doesn't show yet, and the ability to cancel.

from datetime import timedelta

from django.conf import settings

from . import clock
from .db_connection import db_main, run_transaction
from .errors import QueueError
from .events import log_event
from .notifications import notify_appointment_cancelled
from .queue_state import ensure_business_day, load_forecast, serialize_ticket
from .services import SLOT_MINUTES

MAX_REASON_LENGTH = 200


def _parse_day(date_value):
    try:
        day = clock.parse_date(date_value)
    except ValueError as e:
        raise QueueError(str(e), 400)
    # Staff can look back a week (what happened to Monday's bookings?) and as far ahead as
    # students can book.
    today = clock.today()
    if day < today - timedelta(days=7) or day > today + timedelta(days=settings.BOOKING_WINDOW_DAYS):
        raise QueueError(f"Pick a date from 7 days ago up to {settings.BOOKING_WINDOW_DAYS} days ahead.", 400)
    return day.isoformat()


def list_appointments_handler(date_value):
    """Every appointment on a date, in slot order, plus how full each slot is."""
    try:
        ensure_business_day()
        day = _parse_day(date_value)
        docs = list(db_main.tickets.find({"date": day, "mode": "appointment"}).sort([("slot_minutes", 1), ("id", 1)]))
        forecasts = {}
        if day == clock.today_str():
            _, forecasts, *_ = load_forecast()

        booked = {b["slot"]: b.get("count", 0) for b in db_main.slot_bookings.find({"date": day})}
        now = clock.now_utc()
        slots = [{
            "slot": label,
            "booked": booked.get(label, 0),
            "capacity": settings.SLOT_CAPACITY,
            "past": clock.slot_start_utc(day, minutes) <= now,
        } for label, minutes in SLOT_MINUTES.items()]

        appointments = []
        for doc in docs:
            item = serialize_ticket(doc, full=True, estimate=forecasts.get(doc["id"]))
            item["bookedAt"] = clock.to_ms(doc.get("created_at"))
            item["closeReason"] = doc.get("close_reason")
            item["cancelNote"] = doc.get("cancel_note")
            appointments.append(item)

        counts = {}
        for a in appointments:
            counts[a["status"]] = counts.get(a["status"], 0) + 1
        return {
            "status": "success",
            "date": day,
            "isToday": day == clock.today_str(),
            "appointments": appointments,
            "slots": slots,
            "counts": counts,
        }, 200
    except QueueError as e:
        return e.response()
    except Exception as e:
        return {"status": "error", "message": f"Failed to load appointments: {str(e)}"}, 500


def staff_cancel_appointment_handler(ticket_id: int, data, principal):
    """Cancels a still-waiting appointment: frees its slot place, logs who and why, and
    emails the student."""
    reason = (data.get("reason") or "").strip() if isinstance(data.get("reason"), str) else ""
    if len(reason) < 3:
        return {"status": "error", "message": "Please give a short reason — it's sent to the student."}, 400
    if len(reason) > MAX_REASON_LENGTH:
        return {"status": "error", "message": f"Reason must be at most {MAX_REASON_LENGTH} characters."}, 400
    try:
        ensure_business_day()

        def txn(session):
            ticket = db_main.tickets.find_one_and_update(
                {"id": ticket_id, "mode": "appointment", "status": "waiting"},
                {"$set": {"status": "cancelled", "closed_at": clock.now_utc(), "close_reason": "staff",
                          "cancel_note": reason, "cancelled_by": principal["_id"]},
                 "$unset": {"active_key": ""}},
                session=session,
            )
            if not ticket:
                current = db_main.tickets.find_one({"id": ticket_id}, {"status": 1, "mode": 1}, session=session)
                if not current or current.get("mode") != "appointment":
                    raise QueueError("Appointment not found.", 404)
                raise QueueError(f"This appointment can't be cancelled (it is {current['status']}).", 409)
            db_main.slot_bookings.update_one(
                {"_id": f"{ticket['date']}|{ticket['slot']}", "count": {"$gt": 0}},
                {"$inc": {"count": -1}},
                session=session,
            )
            log_event(session, "cancelled", ticket, by="staff", staff=principal["_id"], reason=reason)
            return ticket

        ticket = run_transaction(txn)
        notify_appointment_cancelled(ticket, reason)  # after commit (see notifications.py)
        return {"status": "success", "message": f"Appointment {ticket['token']} cancelled; the student has been emailed."}, 200
    except QueueError as e:
        return e.response()
    except Exception as e:
        return {"status": "error", "message": f"Failed to cancel appointment: {str(e)}"}, 500
