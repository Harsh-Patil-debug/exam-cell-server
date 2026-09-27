# students.py
# Roll numbers. Never typed in — the server assigns every student account a unique,
# sequential roll number (year + 5-digit sequence, e.g. 202600001) when the account is
# activated (email verified, or first Google sign-in). The unique index on students.roll
# (see auth_handler.ensure_indexes) backs the atomic sequence up.

from pymongo import ReturnDocument

from . import clock
from .db_connection import db_main

ROLL_SEQUENCE_ID = "students"


def next_roll_number(session=None) -> str:
    sequence = db_main.queue_meta.find_one_and_update(
        {"_id": ROLL_SEQUENCE_ID},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
        session=session,
    )
    return f"{clock.today().year}{sequence['seq']:05d}"
