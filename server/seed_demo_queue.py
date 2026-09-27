"""
Loads a demo queue (7 sample students, 3 of them called to counters) — handy for showing
the display board without real students. Creates verified demo student accounts
(demo1@examcell.local … demo7@examcell.local, no password — they can't log in) and goes
through the real handlers, so numbering, encryption and the audit log all apply.

Usage (from server/):
    python seed_demo_queue.py            # adds demo tickets to today's empty queue
    python seed_demo_queue.py --force    # resets today's queue first

Never run --force against a live exam day's database.
"""
import os
import sys

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'server.settings')
django.setup()

from exam_project.main.Handlers import clock, crypto  # noqa: E402
from exam_project.main.Handlers.auth_handler import create_active_account  # noqa: E402
from exam_project.main.Handlers.counters import call_next_handler  # noqa: E402
from exam_project.main.Handlers.db_connection import db_main, MONGO_DB_NAME  # noqa: E402
from exam_project.main.Handlers.queue_state import ensure_business_day, reset_queue_handler  # noqa: E402
from exam_project.main.Handlers.tickets import take_token_handler  # noqa: E402

DEMO = [
    ("hall-ticket", "Aarav Sharma"),
    ("revaluation", "Meera Iyer"),
    ("duplicate", "Rohan Gupta"),
    ("exam-form", "Sana Qureshi"),
    ("grievance", "Vikram Nair"),
    ("hall-ticket", "Ishita Bose"),
    ("revaluation", "Karan Mehta"),
]


def demo_student(n: int, name: str):
    email = f"demo{n}@examcell.local"
    user = db_main.students.find_one({"email_index": crypto.email_index(email)})
    if not user:
        user = create_active_account("student", name, email)
    return {"_id": user["_id"], "role": "student", "name": name, "roll": user["roll"]}


def main():
    ensure_business_day()
    open_today = db_main.tickets.count_documents({"date": clock.today_str(), "status": {"$in": ["waiting", "serving"]}})
    if open_today and "--force" not in sys.argv:
        print(f"[seed] '{MONGO_DB_NAME}' already has {open_today} open ticket(s) today — re-run with --force to reset first.")
        return
    if open_today:
        reset_queue_handler()

    for n, (service, name) in enumerate(DEMO, start=1):
        body, code = take_token_handler({"service": service, "mode": "walk-in"}, demo_student(n, name))
        if code != 201:
            print(f"[seed] Could not add {name}: {body.get('message')}")
    for counter_id in (1, 2, 3):
        call_next_handler(counter_id)
    print(f"[seed] Loaded {len(DEMO)} demo tickets into '{MONGO_DB_NAME}' (3 called to counters).")


if __name__ == "__main__":
    main()
