# services.py
# Exam cell services and appointment slots. Kept in sync with SERVICES / SLOTS in the
# frontend's src/lib/queue-store.ts — the backend is the one that enforces them.

SERVICES = [
    {
        "id": "hall-ticket",
        "name": "Hall Ticket Correction",
        "desc": "Name, photo, subject or seat detail corrections",
    },
    {
        "id": "exam-form",
        "name": "Examination Form Issues",
        "desc": "Submission errors, fee or subject mapping problems",
    },
    {
        "id": "revaluation",
        "name": "Revaluation & Photocopy",
        "desc": "Apply for recheck, revaluation or answer script copy",
    },
    {
        "id": "duplicate",
        "name": "Duplicate Marksheet / Transcript",
        "desc": "Reissue of marksheets, transcripts and certificates",
    },
    {
        "id": "grievance",
        "name": "General Examination Grievance",
        "desc": "Any other exam cell concern or clarification",
    },
]

SERVICE_IDS = {s["id"] for s in SERVICES}

MODES = {"walk-in", "appointment"}


def _build_slots():
    """15-minute slots from 10:00 AM to 3:45 PM — same labels the portal renders — mapped
    to minutes after midnight so slot times can be compared and ordered."""
    out = {}
    for m in range(10 * 60, 16 * 60, 15):
        h24, mm = divmod(m, 60)
        h12 = 12 if h24 % 12 == 0 else h24 % 12
        out[f"{h12}:{mm:02d} {'AM' if h24 < 12 else 'PM'}"] = m
    return out


SLOT_MINUTES = _build_slots()
SLOTS = list(SLOT_MINUTES)


def get_services_handler():
    return {
        "status": "success",
        "services": SERVICES,
        "slots": SLOTS,
    }, 200
