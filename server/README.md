# Smart Exam Cell Queue System — Backend

Django + DRF + MongoDB backend for the Smart Exam Cell Queue System (`pixel-perfect-preview` frontend).
Architecture follows the thin-views + Handlers pattern: views are pure HTTP wrappers, all business logic lives in Handlers/.

## Structure

```
pixel-perfect-server/
├── requirements.txt
├── render.yaml                 ← Render deploy blueprint
└── server/
    ├── manage.py
    ├── .env                    ← copy from .env.example and fill in values
    ├── seed_demo_queue.py      ← loads the 7 sample students (optional)
    ├── check_collections.py    ← prints collections + counts in the configured DB
    ├── server/                 ← Django project config
    │   ├── settings.py
    │   ├── urls.py             ← mounts /api/v1/main/
    │   ├── wsgi.py
    │   └── asgi.py
    └── exam_project/
        └── main/
            ├── views.py        ← thin view wrappers (no logic here)
            ├── urls.py         ← all API route definitions
            ├── tests/          ← runs against the isolated *_test database
            └── Handlers/       ← all business logic lives here
                ├── db_connection.py
                ├── status_check.py
                ├── db_check.py
                ├── auth_middleware.py
                ├── input_validation.py
                ├── clock.py         ← IST business day, slot times (patched in tests)
                ├── errors.py
                ├── events.py        ← audit log
                ├── services.py
                ├── students.py      ← roll number assignment
                ├── queue_state.py   ← ordering, forecast, stats, rollover, reset
                ├── tickets.py       ← take / track / cancel, slot availability
                └── counters.py      ← call next / complete / skip / transfer
```

## MongoDB collections

| Collection | Contents |
|------------|----------|
| `students` | Server-assigned roll number (year + 5-digit sequence, e.g. `202600001`), SHA-256 of the private student key, last name used |
| `tickets` | One doc per token: permanent `id`, daily `token` (`TK-101`…), `date`, service, student name + roll, mode, slot, status (`waiting` / `serving` / `completed` / `skipped` / `cancelled` / `expired`), counter, `created_at` / `called_at` / `closed_at`, SHA-256 of the cancel code |
| `counters` | `_id` = counter number, `name`, `serving_id` |
| `queue_meta` | `{_id: "queue"}`: ticket id sequence, business date, last call, last reset. `{_id: "students"}`: roll number sequence |
| `token_counters` | `{_id: "YYYY-MM-DD"}`: that day's token number sequence (TK-101 restarts daily) |
| `slot_bookings` | `{_id: "YYYY-MM-DD\|10:30 AM"}`: appointments booked in that slot (capacity) |
| `ticket_events` | Audit log: issued / called / transferred / requeued / completed / skipped / cancelled / expired |

Indexes, the meta doc and the counters are created automatically on first request — a fresh database needs no setup.

## Queue rules

- **Identity** — the portal registers each browser once and gets a unique roll number back. Tickets carry that roll number; it is never typed in. One open ticket per student per day.
- **Order** (the same rule drives Call Next and every position shown): appointments whose slot is due (from `APPOINTMENT_GRACE_MINUTES` before) by slot → walk-ins first come first served → appointments not yet due, only when nobody else is waiting.
- **Appointments** — today up to `BOOKING_WINDOW_DAYS` ahead, only slots that haven't started, at most `SLOT_CAPACITY` per slot (enforced atomically; a cancel gives the place back). A future-day appointment joins that day's queue.
- **Wait forecast** — each busy counter frees up at its student's real `called_at` + the day's average service time; the queue is then replayed forward with the ordering rule above, so every waiting student gets a predicted call time (`estCallAt`) that counts down on its own and self-corrects on every staff action.
- **Averages** — service time = `called_at → closed_at` of the last 20 completed students (under 30 s ignored as mis-clicks, 60 min cap, 1 min floor; `DEFAULT_SERVICE_MINUTES` until the first real one). Wait time = `created_at → called_at` of the last 20 walk-ins.
- **Day rollover** — the first request after midnight IST expires every ticket still open from the previous day and frees its counter. Nothing is deleted; reset also only closes tickets.
- **Consistency** — every multi-document change is one MongoDB transaction (retried automatically on conflicts between staff terminals). On a standalone local `mongod` it falls back to single-document atomic updates with a warning.

## Setup

```bash
cd server

# Create virtual environment
python -m venv venv
venv\Scripts\activate       # Windows

# Install dependencies
pip install -r ../requirements.txt

# Configure — set MONGO_URL, DJANGO_SECRET_KEY, JWT_SECRET, FIELD_ENCRYPTION_KEY, BLIND_INDEX_KEY, STAFF_EMAILS, GOOGLE_*
copy .env.example .env

# Run migrations (for Django internals)
python manage.py migrate

# Start dev server
python manage.py runserver 0.0.0.0:8000

# Run tests (uses MONGO_DB_NAME_TEST, never the real database)
python manage.py test exam_project.main.tests
```

## Accounts & security

- **Roles** — `student` and `staff`, stored in separate collections (`students`, `staff`), every document also carries `role`. Students get a unique roll number (`202600001`…) when their email is verified; staff can only sign up / log in with an email listed in `STAFF_EMAILS`.
- **Signup / login** — email + password, then a 6-digit email OTP (Brevo; printed to the server console in local dev). Login lockout after 5 bad passwords (15 min), OTPs expire in 10 min, 5 wrong guesses kill the code, 45 s resend cooldown. Forgot/reset password by OTP (signs out every session).
- **Google** — `GET auth/google/login/?role=&return_url=` → Google → `auth/google/callback/` → frontend `/auth/callback?code=` (single-use, 60 s) → `POST auth/google/exchange/`. No token ever appears in a URL. Existing accounts with the same email are linked.
- **Sessions** — 30-min JWT access token (`Authorization: Bearer`) + rotating single-use refresh token (30 days students / 24 h staff). Reusing an old refresh token revokes the whole session; logout revokes both.
- **At rest** — passwords: Argon2id. Emails & names (accounts and tickets): AES-256-GCM (`FIELD_ENCRYPTION_KEY`). Email lookup: HMAC-SHA256 blind index + OTP hashes (`BLIND_INDEX_KEY`). Refresh tokens / Google login codes: SHA-256. Keep both keys safe and never change them on a populated database.

## API Endpoints

All under `/api/v1/main/`. "Student" / "Staff" = requires a logged-in account of that role.

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `status/` | — | Server health check |
| GET | `services/` | — | Services and slot labels |
| POST | `auth/register/` | — | `{role, name, email, password}` → OTP emailed |
| POST | `auth/login/` | — | `{role, email, password}` → OTP emailed |
| POST | `auth/verify-otp/` | — | `{role, email, otp}` → `{accessToken, refreshToken, user}` |
| POST | `auth/resend-otp/` | — | `{role, email}` |
| POST | `auth/forgot-password/` | — | `{role, email}` |
| POST | `auth/reset-password/` | — | `{role, email, otp, password}` |
| POST | `auth/refresh/` | — | `{refreshToken}` → rotated pair |
| POST | `auth/logout/` | Bearer | `{refreshToken}` |
| GET | `auth/me/` | Any | Current account |
| GET | `auth/google/login/` | — | Redirect to Google |
| GET | `auth/google/callback/` | — | Google's redirect target |
| POST | `auth/google/exchange/` | — | `{code}` → session |
| GET | `queue/` | Any | Full snapshot (other students masked unless staff) |
| POST | `queue/reset/` | Staff | Close today's tickets, free counters |
| POST | `tickets/` | Student | `{service, mode, date?, slot?}` |
| GET | `tickets/mine/` | Student | The student's current ticket + forecast |
| GET | `tickets/<id>/` | Any | Staff: any ticket; student: own only |
| POST | `tickets/<id>/cancel/` | Student | Own ticket, while waiting |
| GET | `slots/?date=YYYY-MM-DD` | Any | Places left per slot |
| POST | `counters/<id>/call-next/` | Staff | |
| POST | `counters/<id>/complete/` | Staff | |
| POST | `counters/<id>/skip/` | Staff | |
| POST | `counters/<id>/transfer/` | Staff | `{to_counter}` |
| GET | `db/` | Staff | MongoDB read/write check |
