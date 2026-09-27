# events.py
# Append-only audit trail of everything that happens to a ticket (issued, called,
# completed, skipped, transferred, cancelled, expired, reset). Written inside the same
# transaction as the change itself, so the log can never disagree with the queue.

from .db_connection import db_main
from . import clock


def log_event(session, action: str, ticket=None, **details):
    doc = {
        "action": action,
        "at": clock.now_utc(),
        "date": clock.today_str(),
        **details,
    }
    if ticket is not None:
        doc["ticket_id"] = ticket["id"]
        doc["token"] = ticket["token"]
    db_main.ticket_events.insert_one(doc, session=session)


def log_events(session, action: str, tickets, **details):
    now = clock.now_utc()
    docs = [
        {"action": action, "at": now, "date": clock.today_str(), "ticket_id": t["id"], "token": t["token"], **details}
        for t in tickets
    ]
    if docs:
        db_main.ticket_events.insert_many(docs, session=session)
