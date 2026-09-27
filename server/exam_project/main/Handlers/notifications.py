# notifications.py
# Emails sent when the queue changes:
#   - notify_called: the one student who was just called to a counter.
#   - notify_staff_new_ticket: every staff member (STAFF_EMAILS), when a student joins the
#     queue or books an appointment.
#
# Called only AFTER the call's transaction has committed — never inside it:
# run_transaction() may retry its callback on a write conflict, and sending from in there
# could email the same student twice (or about a call that then rolled back).
#
# Sent on a background thread so a slow email provider never delays the staff member's
# "Call Next" click. Every attempt is recorded in the `notifications` collection
# (sent / failed / skipped) — an audit trail of who was told to come to which counter.

import threading

from bson import ObjectId
from django.conf import settings

from . import clock, crypto
from .db_connection import db_main
from .email_handler import send_appointment_cancelled_email, send_called_email, send_new_ticket_staff_email
from .services import SERVICES

SERVICE_NAMES = {s["id"]: s["name"] for s in SERVICES}

_indexes_ensured = False


def _ensure_indexes():
    global _indexes_ensured
    if not _indexes_ensured:
        db_main.notifications.create_index([("ticket_id", 1), ("created_at", 1)])
        _indexes_ensured = True


def reset_index_cache():
    """For tests, which drop the collections between cases."""
    global _indexes_ensured
    _indexes_ensured = False


def _record(ticket, counter_id: int | None, kind: str, status: str, detail: str = ""):
    db_main.notifications.insert_one({
        "ticket_id": ticket["id"],
        "token": ticket["token"],
        "student_ref": ticket.get("student_ref"),
        "type": kind,
        "channel": "email",
        "counter": counter_id,
        "status": status,
        "detail": detail,
        "created_at": clock.now_utc(),
    })


def _deliver(ticket, counter_id: int, transferred: bool):
    kind = "transferred" if transferred else "called"
    try:
        _ensure_indexes()
        student_ref = ticket.get("student_ref")
        student = db_main.students.find_one({"_id": ObjectId(student_ref)}) if student_ref else None
        if not student or not student.get("email_enc"):
            _record(ticket, counter_id, kind, "skipped", "no student account")
            return
        if student.get("auth_provider") == "seed":
            # Demo accounts from seed_demo_queue.py have made-up addresses.
            _record(ticket, counter_id, kind, "skipped", "demo account")
            return
        sent = send_called_email(
            recipient=crypto.decrypt_field(student["email_enc"], "email"),
            name=crypto.decrypt_field(student["name_enc"], "name"),
            token=ticket["token"],
            counter_id=counter_id,
            service=SERVICE_NAMES.get(ticket.get("service"), "Exam cell service"),
            transferred=transferred,
        )
        _record(ticket, counter_id, kind, "sent" if sent else "failed",
                "" if sent else "email provider rejected or unreachable")
    except Exception as e:
        # A notification problem must never surface as a failed staff action.
        print(f"[ExamCell] Could not notify {ticket.get('token')}: {e}")
        try:
            _record(ticket, counter_id, kind, "failed", str(e)[:200])
        except Exception:
            pass


def _run(target, *args):
    if settings.NOTIFICATIONS_SYNC:
        target(*args)
    else:
        threading.Thread(target=target, args=args, daemon=True).start()


def notify_called(ticket, counter_id: int, transferred: bool = False):
    """Emails the student on `ticket` that they've been called to `counter_id`."""
    _run(_deliver, ticket, counter_id, transferred)


# ── New ticket -> staff ──────────────────────────────────────────────────────

def staff_recipients():
    """Everyone on the STAFF_EMAILS allowlist — the people who run the counters, whether
    or not they've created their account yet."""
    return sorted(settings.STAFF_EMAILS)


def _deliver_new_ticket(ticket, estimate, waiting_total: int):
    recipients = staff_recipients()
    if not recipients:
        _record(ticket, None, "new_ticket_staff", "skipped", "no staff emails configured")
        return
    position, est_wait_min, _ = estimate if estimate else (None, None, None)
    sent = failed = 0
    for recipient in recipients:
        try:
            ok = send_new_ticket_staff_email(
                recipient,
                token=ticket["token"],
                student=crypto.decrypt_field(ticket["student_enc"], "name") if ticket.get("student_enc") else "",
                roll=ticket.get("roll") or "",
                service=SERVICE_NAMES.get(ticket.get("service"), "Exam cell service"),
                mode=ticket.get("mode", "walk-in"),
                date=ticket.get("date", ""),
                slot=ticket.get("slot"),
                position=position,
                est_wait_min=est_wait_min,
                waiting_total=waiting_total,
            )
        except Exception as e:
            print(f"[ExamCell] Could not email staff about {ticket.get('token')}: {e}")
            ok = False
        sent, failed = sent + ok, failed + (not ok)
    # One record per new ticket; staff addresses aren't copied into it.
    status = "sent" if not failed else ("failed" if not sent else "partial")
    try:
        _record(ticket, None, "new_ticket_staff", status, f"{sent}/{len(recipients)} staff emailed")
    except Exception as e:
        print(f"[ExamCell] Could not record staff notification for {ticket.get('token')}: {e}")


def notify_staff_new_ticket(ticket, estimate, waiting_total: int):
    """Emails every staff member that a student just joined the queue / booked a slot."""
    if not settings.STAFF_NEW_TICKET_EMAILS:
        return
    _run(_deliver_new_ticket, ticket, estimate, waiting_total)


# ── Appointment cancelled by staff -> student ────────────────────────────────

def _deliver_appointment_cancelled(ticket, reason: str):
    kind = "appointment_cancelled"
    try:
        student_ref = ticket.get("student_ref")
        student = db_main.students.find_one({"_id": ObjectId(student_ref)}) if student_ref else None
        if not student or not student.get("email_enc") or student.get("auth_provider") == "seed":
            _record(ticket, None, kind, "skipped", "no reachable student account")
            return
        sent = send_appointment_cancelled_email(
            recipient=crypto.decrypt_field(student["email_enc"], "email"),
            name=crypto.decrypt_field(student["name_enc"], "name"),
            token=ticket["token"],
            date=ticket.get("date", ""),
            slot=ticket.get("slot") or "",
            service=SERVICE_NAMES.get(ticket.get("service"), "Exam cell service"),
            reason=reason,
        )
        _record(ticket, None, kind, "sent" if sent else "failed")
    except Exception as e:
        print(f"[ExamCell] Could not email cancellation for {ticket.get('token')}: {e}")
        try:
            _record(ticket, None, kind, "failed", str(e)[:200])
        except Exception:
            pass


def notify_appointment_cancelled(ticket, reason: str):
    """Emails the student that staff cancelled their appointment, and why."""
    _run(_deliver_appointment_cancelled, ticket, reason)
