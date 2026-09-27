# counters.py
# Staff-side counter handlers: call next, complete, skip / no-show, transfer.
#
# Every action touches several documents (the counter, one or two tickets, the last-call
# banner, the audit log) and runs as ONE transaction: either all of it happens or none of
# it does. When two staff terminals act at the same moment, MongoDB detects the write
# conflict and run_transaction() retries the loser against the fresh state — so two
# counters can never be handed the same student, and no counter is ever left pointing at a
# ticket that isn't being served.

from pymongo import ReturnDocument

from . import clock
from .db_connection import db_main, run_transaction
from .errors import QueueError
from .events import log_event
from .notifications import notify_called
from .queue_state import ensure_business_day, ordered_waiting, serialize_ticket, set_last_called


def _get_counter(session, counter_id: int):
    counter = db_main.counters.find_one({"_id": counter_id}, session=session)
    if not counter:
        raise QueueError(f"Counter {counter_id} does not exist.", 404)
    return counter


def _close_serving(session, counter, final_status: str):
    """Closes the ticket at this counter (completed / skipped) and frees the counter.
    Returns the closed ticket, or None if the counter was idle."""
    ticket_id = counter.get("serving_id")
    if not ticket_id:
        return None
    ticket = db_main.tickets.find_one_and_update(
        {"id": ticket_id, "status": "serving"},
        {"$set": {"status": final_status, "closed_at": clock.now_utc()},
         "$unset": {"active_key": ""}},
        return_document=ReturnDocument.AFTER,
        session=session,
    )
    db_main.counters.update_one({"_id": counter["_id"]}, {"$set": {"serving_id": None}}, session=session)
    if ticket:
        log_event(session, final_status, ticket, counter=counter["_id"])
    return ticket


def _seat(session, ticket, counter_id: int, action: str, **details):
    """Puts a waiting ticket at a free counter and announces it."""
    now = clock.now_utc()
    seated = db_main.tickets.find_one_and_update(
        {"id": ticket["id"], "status": "waiting"},
        {"$set": {"status": "serving", "counter": counter_id, "called_at": now}},
        return_document=ReturnDocument.AFTER,
        session=session,
    )
    counter = db_main.counters.update_one(
        {"_id": counter_id, "serving_id": None},
        {"$set": {"serving_id": ticket["id"]}},
        session=session,
    )
    if not seated or counter.modified_count == 0:
        # Only reachable without transactions (standalone MongoDB), where another request
        # can interleave; inside a transaction the conflict is retried automatically.
        raise QueueError("The queue changed while calling — please try again.", 409)
    set_last_called(session, seated["token"], counter_id)
    log_event(session, action, seated, counter=counter_id, **details)
    return seated


def call_next_handler(counter_id: int):
    """Completes the counter's current student (if any), then calls whoever is first in
    queue order (due appointments, then walk-ins, then early appointments)."""
    try:
        ensure_business_day()

        def txn(session):
            counter = _get_counter(session, counter_id)
            completed = _close_serving(session, counter, "completed")
            waiting = ordered_waiting(session)
            if not waiting:
                return None, completed
            return _seat(session, waiting[0], counter_id, "called"), completed

        ticket, completed = run_transaction(txn)
        if ticket:
            # After commit only — see notifications.py.
            notify_called(ticket, counter_id)
        body = {
            "status": "success",
            "ticket": serialize_ticket(ticket, full=True) if ticket else None,
            "completed": serialize_ticket(completed, full=True) if completed else None,
        }
        if not ticket:
            body["message"] = "No students currently waiting in queue."
        return body, 200
    except QueueError as e:
        return e.response()
    except Exception as e:
        return {"status": "error", "message": f"Failed to call next token: {str(e)}"}, 500


def close_current_handler(counter_id: int, final_status: str):
    """Mark Completed (final_status="completed") or Skip / No-Show ("skipped")."""
    try:
        ensure_business_day()

        def txn(session):
            counter = _get_counter(session, counter_id)
            ticket = _close_serving(session, counter, final_status)
            if not ticket:
                raise QueueError(f"Counter {counter_id} has no active token.", 409)
            return ticket

        ticket = run_transaction(txn)
        return {"status": "success", "ticket": serialize_ticket(ticket, full=True)}, 200
    except QueueError as e:
        return e.response()
    except Exception as e:
        return {"status": "error", "message": f"Failed to update token: {str(e)}"}, 500


def transfer_handler(from_counter: int, to_counter: int):
    """
    Moves the student at from_counter to to_counter. If to_counter is busy, the student goes
    back into the waiting line instead (same behaviour as the original frontend-only
    queue) — keeping their original place, since queue order is by ticket, not by time
    they were returned.
    """
    if from_counter == to_counter:
        return {"status": "error", "message": "Choose a different counter to transfer to."}, 400
    try:
        ensure_business_day()

        def txn(session):
            source = _get_counter(session, from_counter)
            target = _get_counter(session, to_counter)
            ticket_id = source.get("serving_id")
            if not ticket_id:
                raise QueueError(f"Counter {from_counter} has no active token.", 409)

            db_main.counters.update_one({"_id": from_counter}, {"$set": {"serving_id": None}}, session=session)
            ticket = db_main.tickets.find_one_and_update(
                {"id": ticket_id, "status": "serving"},
                {"$set": {"status": "waiting", "counter": None, "called_at": None}},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if not ticket:
                raise QueueError("The queue changed while transferring — please try again.", 409)

            if target.get("serving_id"):
                log_event(session, "requeued", ticket, from_counter=from_counter, to_counter=to_counter)
                return ticket, True
            return _seat(session, ticket, to_counter, "transferred", from_counter=from_counter), False

        ticket, requeued = run_transaction(txn)
        if not requeued:
            notify_called(ticket, to_counter, transferred=True)
        return {
            "status": "success",
            "ticket": serialize_ticket(ticket, full=True),
            "requeued": requeued,
            "message": (
                f"Counter {to_counter} is busy — {ticket['token']} was returned to the queue."
                if requeued else f"Transferred {ticket['token']} to Counter {to_counter}."
            ),
        }, 200
    except QueueError as e:
        return e.response()
    except Exception as e:
        return {"status": "error", "message": f"Failed to transfer token: {str(e)}"}, 500
