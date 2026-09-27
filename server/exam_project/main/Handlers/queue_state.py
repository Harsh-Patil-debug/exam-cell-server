# queue_state.py
# Shared queue logic: collection setup, the business-day rollover, queue ordering, wait
# estimates and stats (all computed from stored timestamps), the full-state snapshot every
# screen polls, and the staff reset.
#
# Collections:
#   tickets        — one doc per issued token. `id` is a permanent, globally unique integer;
#                    `token` (TK-101…) is the daily display number and restarts each day.
#   counters       — _id is the counter number; serving_id points at the ticket being served.
#   queue_meta     — {_id: "queue"}: id sequence, current business date, last call made,
#                    and when staff last reset the queue.
#   token_counters — {_id: "YYYY-MM-DD"}: per-day token number sequence.
#   slot_bookings  — {_id: "YYYY-MM-DD|10:30 AM"}: appointment count per slot (capacity).
#   ticket_events  — audit log (see events.py).

import heapq
from datetime import timedelta

from django.conf import settings
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from . import clock, crypto
from .db_connection import db_main, run_transaction
from .events import log_events

META_ID = "queue"
FIRST_TOKEN_NUMBER = 101
OPEN_STATUSES = ["waiting", "serving"]
CLOSED_VISIBLE_STATUSES = ["completed", "skipped"]
# How many recent tickets the running averages are taken over — recent enough to follow
# the pace of the day, large enough that one unusually long case doesn't swing it.
AVERAGE_SAMPLE_SIZE = 20
# A counter left "serving" over lunch shouldn't turn into a 3-hour average service time.
MAX_SAMPLE_MINUTES = 60
# A "service" closed within seconds of the call is a mis-click or an immediate hand-back,
# not a real service — counting it would drag the average (and every forecast) to ~0.
MIN_SERVICE_SAMPLE_MINUTES = 0.5
# Forecasts never assume a counter can serve a student in under a minute.
MIN_AVG_SERVICE_MINUTES = 1.0

_setup_done = False
_known_business_date = None


# ── Setup ─────────────────────────────────────────────────────────────────────

def ensure_setup():
    """Lazy + idempotent: indexes, the meta doc and the counters are created on first use,
    so a fresh database needs no migration step. Kept outside transactions — index
    creation isn't allowed inside one."""
    global _setup_done
    if _setup_done:
        return
    db_main.tickets.create_index("id", unique=True)
    db_main.tickets.create_index([("date", ASCENDING), ("status", ASCENDING)])
    db_main.tickets.create_index("closed_at")
    db_main.tickets.create_index("called_at")
    # One open ticket per roll number per day. active_key only exists while a ticket is
    # waiting/serving (it's $unset on close), so the partial index ignores closed history.
    db_main.tickets.create_index(
        "active_key", unique=True, partialFilterExpression={"active_key": {"$exists": True}},
    )
    db_main.ticket_events.create_index([("ticket_id", ASCENDING), ("at", ASCENDING)])
    db_main.ticket_events.create_index("at")
    db_main.slot_bookings.create_index("date")
    db_main.queue_meta.update_one(
        {"_id": META_ID},
        {"$setOnInsert": {
            "next_id": 0,
            "business_date": clock.today_str(),
            "last_called": None,
            "reset_at": None,
        }},
        upsert=True,
    )
    for counter_id in range(1, settings.COUNTER_COUNT + 1):
        db_main.counters.update_one(
            {"_id": counter_id},
            {"$setOnInsert": {"name": f"Counter {counter_id}", "serving_id": None}},
            upsert=True,
        )
    _setup_done = True


def reset_setup_cache():
    """For tests, which drop the collections between cases."""
    global _setup_done, _known_business_date
    _setup_done = False
    _known_business_date = None


def ensure_business_day():
    """
    Rolls the queue over to a new day on the first request after midnight (IST): every
    ticket from a previous day still waiting or being served is expired, counters holding
    them are freed, and yesterday's "now calling" banner is cleared. Token numbers restart
    at TK-101 automatically because they're sequenced per date.

    Safe with several server processes: only the request that flips business_date (a
    conditional update) does the work, all inside one transaction.
    """
    global _known_business_date
    ensure_setup()
    today = clock.today_str()
    if _known_business_date == today:
        return

    def txn(session):
        flipped = db_main.queue_meta.find_one_and_update(
            {"_id": META_ID, "business_date": {"$ne": today}},
            {"$set": {"business_date": today, "last_called": None, "reset_at": None}},
            session=session,
        )
        if flipped is None:
            return  # already today's date (another request/process rolled it over)
        stale = list(db_main.tickets.find(
            {"status": {"$in": OPEN_STATUSES}, "date": {"$lt": today}},
            {"id": 1, "token": 1},
            session=session,
        ))
        if not stale:
            return
        ids = [t["id"] for t in stale]
        db_main.tickets.update_many(
            {"id": {"$in": ids}},
            {"$set": {"status": "expired", "closed_at": clock.now_utc(), "close_reason": "day_end"},
             "$unset": {"active_key": ""}},
            session=session,
        )
        db_main.counters.update_many({"serving_id": {"$in": ids}}, {"$set": {"serving_id": None}}, session=session)
        log_events(session, "expired", stale, reason="day_end")

    run_transaction(txn)
    _known_business_date = today


def next_ticket_numbers(session, day: str):
    """(permanent id, daily token number) — both atomic $inc, so no two tickets can ever
    share either, even when issued in the same instant."""
    meta = db_main.queue_meta.find_one_and_update(
        {"_id": META_ID}, {"$inc": {"next_id": 1}},
        return_document=ReturnDocument.AFTER, session=session,
    )
    counter = db_main.token_counters.find_one_and_update(
        {"_id": day}, {"$inc": {"issued": 1}},
        upsert=True, return_document=ReturnDocument.AFTER, session=session,
    )
    return meta["next_id"], FIRST_TOKEN_NUMBER - 1 + counter["issued"]


def set_last_called(session, token: str, counter_id: int):
    db_main.queue_meta.update_one(
        {"_id": META_ID},
        {"$set": {"last_called": {
            "token": token,
            "counter": counter_id,
            # Epoch milliseconds, matching the frontend's Date.now() — the display compares
            # this value to decide whether a call is new and should be announced.
            "at": clock.to_ms(clock.now_utc()),
        }}},
        session=session,
    )


def session_start(meta):
    """Start of the current queue session: today's midnight, or the last staff reset if
    that was later. Stats and the visible history only count from here."""
    start = clock.day_start_utc(clock.today())
    reset_at = clock.aware(meta.get("reset_at")) if meta else None
    return max(start, reset_at) if reset_at else start


# ── Ordering ──────────────────────────────────────────────────────────────────

def appointment_due(ticket, now) -> bool:
    if ticket.get("mode") != "appointment" or ticket.get("slot_minutes") is None:
        return False
    starts = clock.slot_start_utc(ticket["date"], ticket["slot_minutes"])
    return now >= starts - timedelta(minutes=settings.APPOINTMENT_GRACE_MINUTES)


def queue_sort_key(ticket, now):
    """
    The one definition of queue order, used both to pick who "Call Next" serves and to
    report each student's position — so the position a student sees is exactly the order
    they'll be called in:
      1. appointments whose slot is due (from APPOINTMENT_GRACE_MINUTES before), by slot
      2. walk-ins, first come first served
      3. appointments not due yet, by slot — only reached when nobody else is waiting
    """
    if ticket.get("mode") == "appointment" and ticket.get("slot_minutes") is not None:
        group = 0 if appointment_due(ticket, now) else 2
        return (group, ticket["slot_minutes"], ticket["id"])
    return (1, 0, ticket["id"])


def ordered_waiting(session=None, now=None):
    now = now or clock.now_utc()
    waiting = list(db_main.tickets.find(
        {"date": clock.today_str(), "status": "waiting"}, session=session,
    ))
    return sorted(waiting, key=lambda t: queue_sort_key(t, now))


# ── Stats & estimates ─────────────────────────────────────────────────────────

def _average_minutes(docs, start_field, end_field, ignore_below=0.0):
    samples = []
    for d in docs:
        if d.get(start_field) and d.get(end_field):
            minutes = clock.minutes_between(d[start_field], d[end_field])
            if minutes < ignore_below:
                continue
            samples.append(min(max(minutes, 0), MAX_SAMPLE_MINUTES))
    return sum(samples) / len(samples) if samples else None


def compute_stats(since, session=None):
    """Everything here is derived from stored timestamps, not constants:
    - avgServiceMinutes: called_at -> closed_at of the last completed services
    - avgWaitMinutes: created_at -> called_at of the last walk-ins called
      (appointments are excluded — they were booked hours or days ahead on purpose)"""
    served = db_main.tickets.count_documents({"status": "completed", "closed_at": {"$gte": since}}, session=session)
    skipped = db_main.tickets.count_documents({"status": "skipped", "closed_at": {"$gte": since}}, session=session)
    recent_completed = db_main.tickets.find(
        {"status": "completed", "closed_at": {"$gte": since}, "called_at": {"$ne": None}},
        {"called_at": 1, "closed_at": 1}, session=session,
    ).sort("closed_at", DESCENDING).limit(AVERAGE_SAMPLE_SIZE)
    recent_called = db_main.tickets.find(
        {"mode": "walk-in", "called_at": {"$gte": since}},
        {"created_at": 1, "called_at": 1}, session=session,
    ).sort("called_at", DESCENDING).limit(AVERAGE_SAMPLE_SIZE)

    avg_service = _average_minutes(
        recent_completed, "called_at", "closed_at", ignore_below=MIN_SERVICE_SAMPLE_MINUTES,
    )
    avg_wait = _average_minutes(recent_called, "created_at", "called_at")
    if avg_service is not None:
        avg_service = max(avg_service, MIN_AVG_SERVICE_MINUTES)
    return {
        "servedToday": served,
        "skippedToday": skipped,
        "avgServiceMinutes": round(avg_service if avg_service is not None else settings.DEFAULT_SERVICE_MINUTES, 1),
        "avgServiceIsEstimate": avg_service is None,
        "avgWaitMinutes": round(avg_wait, 1) if avg_wait is not None else None,
    }


def forecast(waiting, counters, serving, avg_service, now):
    """
    Predicts when each waiting student will be called by replaying the queue forward in
    time, using the same rule Call Next uses:

      - Each counter becomes free at: now if idle, otherwise its current student's real
        called_at + the average service time (never earlier than now — an overrunning
        student is assumed to finish any moment).
      - Whenever the earliest counter frees up, the ticket picked is whichever sorts first
        under queue_sort_key AT THAT MOMENT — so an appointment that becomes due while
        walk-ins are waiting correctly jumps ahead of them in the forecast too.
      - An appointment is never predicted before its slot opens (slot - grace).

    Returns {ticket_id: (students_ahead_now, minutes_until_called, predicted_call_time)}.
    Because it's anchored to real timestamps, the prediction counts down on its own as time
    passes and self-corrects every time staff call, complete or skip someone.
    """
    avg = timedelta(minutes=avg_service)
    grace = timedelta(minutes=settings.APPOINTMENT_GRACE_MINUTES)
    called_at_by_id = {t["id"]: clock.aware(t.get("called_at")) for t in serving}

    free_at = []
    for c in counters:
        called_at = called_at_by_id.get(c.get("serving_id"))
        free_at.append(max(called_at + avg, now) if called_at else now)
    if not free_at:
        free_at = [now]
    heapq.heapify(free_at)

    ahead_now = {t["id"]: i for i, t in enumerate(sorted(waiting, key=lambda t: queue_sort_key(t, now)))}
    remaining = list(waiting)
    result = {}
    while remaining:
        counter_free = heapq.heappop(free_at)
        ticket = min(remaining, key=lambda t: queue_sort_key(t, counter_free))
        remaining.remove(ticket)
        call_at = counter_free
        if ticket.get("mode") == "appointment" and ticket.get("slot_minutes") is not None:
            call_at = max(call_at, clock.slot_start_utc(ticket["date"], ticket["slot_minutes"]) - grace)
        result[ticket["id"]] = (
            ahead_now.get(ticket["id"], 0),
            clock.ceil_minutes(clock.minutes_between(now, call_at)),
            call_at,
        )
        heapq.heappush(free_at, call_at + avg)
    return result


WALK_IN_PROBE_ID = float("inf")


def load_forecast(session=None, now=None):
    """Everything needed to report positions and waits, read from the database in one
    place so the queue view, a single ticket's tracker and the "join now" preview can never
    disagree. Returns (ordered_waiting, forecasts, counters, serving, stats, join_preview)."""
    now = now or clock.now_utc()
    meta = db_main.queue_meta.find_one({"_id": META_ID}, session=session) or {}
    stats = compute_stats(session_start(meta), session)
    counters = list(db_main.counters.find(session=session).sort("_id", 1))
    serving = list(db_main.tickets.find(
        {"date": clock.today_str(), "status": "serving"}, session=session,
    ).sort("counter", 1))
    waiting = ordered_waiting(session, now)
    forecasts = forecast(waiting, counters, serving, stats["avgServiceMinutes"], now)

    # "If I join the live queue right now": a hypothetical walk-in added at the back of
    # the walk-in line, run through the same forecast.
    probe = {"id": WALK_IN_PROBE_ID, "mode": "walk-in"}
    probe_forecast = forecast(waiting + [probe], counters, serving, stats["avgServiceMinutes"], now)
    ahead, wait_min, call_at = probe_forecast[WALK_IN_PROBE_ID]
    join_preview = {"ahead": ahead, "estWaitMin": wait_min, "estCallAt": clock.to_ms(call_at)}
    return waiting, forecasts, counters, serving, stats, join_preview


# ── Serialization ─────────────────────────────────────────────────────────────

def _mask_name(name: str) -> str:
    parts = name.split()
    if len(parts) <= 1:
        return name
    return f"{parts[0]} {parts[-1][0]}."


def _mask_roll(roll: str) -> str:
    if len(roll) <= 4:
        return roll
    return roll[:4] + "•" * (len(roll) - 4)


def serialize_ticket(doc, full: bool, estimate=None):
    """
    Frontend Ticket shape (camelCase). Student name and roll number are only sent in full
    to staff — the lobby display and student portal are public screens, so everyone else
    gets "Aarav S." / "CS21••••".
    """
    student = crypto.decrypt_field(doc["student_enc"], "name") if doc.get("student_enc") else ""
    roll = doc.get("roll") or ""
    created_at = clock.aware(doc.get("created_at"))
    joined_min = 0
    if created_at is not None:
        joined_min = max(0, int(clock.minutes_between(clock.day_start_utc(clock.today()), created_at)))
    position, est_wait, est_call_at = estimate if estimate else (None, None, None)
    return {
        "id": doc["id"],
        "token": doc["token"],
        "service": doc["service"],
        "student": student if full else _mask_name(student),
        "roll": roll if full else _mask_roll(roll),
        "mode": doc["mode"],
        "status": doc["status"],
        "counter": doc.get("counter"),
        "slot": doc.get("slot"),
        "date": doc.get("date"),
        "joinedMin": joined_min,
        "calledAt": clock.to_ms(doc.get("called_at")),
        "closedAt": clock.to_ms(doc.get("closed_at")),
        # Students ahead in line / estimated minutes until called — waiting tickets only.
        "position": position,
        "estWaitMin": est_wait,
        # Predicted call time (epoch ms) — lets the tracker count down every second
        # between polls instead of jumping in whole minutes.
        "estCallAt": clock.to_ms(est_call_at),
    }


# ── Handlers ──────────────────────────────────────────────────────────────────

def build_state(full: bool, session=None):
    now = clock.now_utc()
    meta = db_main.queue_meta.find_one({"_id": META_ID}, session=session) or {}
    since = session_start(meta)
    today = clock.today_str()

    waiting, forecasts, counters_raw, serving, stats, join_preview = load_forecast(session, now)
    closed = list(db_main.tickets.find(
        {"status": {"$in": CLOSED_VISIBLE_STATUSES}, "closed_at": {"$gte": since}}, session=session,
    ).sort("closed_at", 1))

    # Waiting tickets are listed in true queue order, so the frontend's "next in line"
    # views and position counts match exactly who Call Next will serve.
    tickets = (
        [serialize_ticket(t, full) for t in serving]
        + [serialize_ticket(t, full, forecasts[t["id"]]) for t in waiting]
        + [serialize_ticket(t, full) for t in closed]
    )
    token_counter = db_main.token_counters.find_one({"_id": today}, session=session) or {}
    return {
        "status": "success",
        "businessDate": today,
        "serverTime": clock.to_ms(now),
        "tickets": tickets,
        "counters": [
            {"id": c["_id"], "name": c["name"], "servingId": c.get("serving_id")}
            for c in counters_raw
        ],
        "nextNumber": FIRST_TOKEN_NUMBER + token_counter.get("issued", 0),
        "lastCalled": meta.get("last_called"),
        "joinPreview": join_preview,
        **stats,
    }


def get_queue_state_handler(full: bool):
    """Full snapshot in the frontend's State shape: today's open tickets in queue order,
    everything closed this session, counters and stats."""
    try:
        ensure_business_day()
        # Read in one snapshot transaction so tickets, counters and stats are mutually
        # consistent even while staff are changing them.
        return run_transaction(lambda session: build_state(full, session)), 200
    except Exception as e:
        return {"status": "error", "message": f"Failed to load queue: {str(e)}"}, 500


def reset_queue_handler():
    """
    Staff-only: closes every open ticket for today (history is kept, marked expired with
    reason "reset"), frees all counters, releases today's appointment slots and restarts
    today's token numbers at TK-101. Future-dated appointments are untouched.
    """
    try:
        ensure_business_day()
        today = clock.today_str()

        def txn(session):
            open_tickets = list(db_main.tickets.find(
                {"status": {"$in": OPEN_STATUSES}, "date": {"$lte": today}},
                {"id": 1, "token": 1}, session=session,
            ))
            if open_tickets:
                db_main.tickets.update_many(
                    {"id": {"$in": [t["id"] for t in open_tickets]}},
                    {"$set": {"status": "expired", "closed_at": clock.now_utc(), "close_reason": "reset"},
                     "$unset": {"active_key": ""}},
                    session=session,
                )
            db_main.counters.update_many({}, {"$set": {"serving_id": None}}, session=session)
            db_main.slot_bookings.delete_many({"date": today}, session=session)
            db_main.token_counters.update_one({"_id": today}, {"$set": {"issued": 0}}, upsert=True, session=session)
            db_main.queue_meta.update_one(
                {"_id": META_ID},
                {"$set": {"last_called": None, "reset_at": clock.now_utc()}},
                session=session,
            )
            log_events(session, "expired", open_tickets, reason="reset")
            return len(open_tickets)

        closed = run_transaction(txn)
        return {"status": "success", "message": "Queue has been reset.", "closedTickets": closed}, 200
    except Exception as e:
        return {"status": "error", "message": f"Failed to reset queue: {str(e)}"}, 500
