# email_handler.py
# Transactional email via Brevo's HTTPS API (same provider/approach as khelomore-server —
# raw SMTP is silently blocked on hosts like Render).
#
# Local development without BREVO_API_KEY: the email is NOT sent; the OTP is printed to the
# server console instead so sign-up/login can still be completed. That fallback only
# exists while DEBUG=True — in production a missing key is logged as a delivery failure.

import html

import requests
from django.conf import settings

BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"

PURPOSE_TEXT = {
    "signup": ("Verify your email", "Use this code to finish creating your account."),
    "login": ("Your login code", "Use this code to finish signing in."),
    "password_reset": ("Reset your password", "Use this code to set a new password."),
}


def email_configured() -> bool:
    return bool(settings.BREVO_API_KEY and settings.EMAIL_SENDER_ADDRESS)


def _send(recipient: str, subject: str, html_body: str) -> bool:
    if not email_configured():
        print(f"[EMAIL] Brevo not configured — not sending '{subject}' to {recipient}")
        return False
    try:
        response = requests.post(
            BREVO_API_URL,
            headers={"accept": "application/json", "api-key": settings.BREVO_API_KEY, "content-type": "application/json"},
            json={
                "sender": {"name": settings.EMAIL_SENDER_NAME, "email": settings.EMAIL_SENDER_ADDRESS},
                "to": [{"email": recipient}],
                "subject": subject,
                "htmlContent": html_body,
            },
            timeout=15,
        )
        if response.status_code >= 400:
            print(f"[EMAIL] Brevo API error {response.status_code}: {response.text}")
            return False
        return True
    except requests.RequestException as e:
        print(f"[EMAIL] Brevo request failed: {e}")
        return False


def send_otp_email(recipient: str, otp: str, name: str, purpose: str) -> bool:
    title, line = PURPOSE_TEXT.get(purpose, ("Your verification code", "Use this code to continue."))
    if not email_configured() and settings.DEBUG:
        # Local-dev fallback only (see module docstring).
        print(f"\n[EMAIL][DEV] {title} for {recipient}: {otp}  (valid {settings.OTP_EXPIRY_MINUTES} min)\n")
        return True
    # The name comes from user input — escape before putting it in HTML.
    safe_name = html.escape(name or "there")
    body = f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:24px">
      <h2 style="margin:0 0 12px">Smart Exam Cell</h2>
      <p>Hi {safe_name},</p>
      <p>{line}</p>
      <p style="font-size:32px;font-weight:bold;letter-spacing:8px;margin:24px 0">{otp}</p>
      <p style="color:#666">This code expires in {settings.OTP_EXPIRY_MINUTES} minutes. If you didn't request it,
      you can ignore this email.</p>
    </div>
    """
    return _send(recipient, f"{title} — Smart Exam Cell", body)


def send_new_ticket_staff_email(recipient: str, *, token: str, student: str, roll: str, service: str,
                                mode: str, date: str, slot, position, est_wait_min, waiting_total: int) -> bool:
    """Tells a staff member a student has just joined the queue (or booked a slot)."""
    if mode == "appointment":
        subject = f"New appointment {token}: {student} — {date} at {slot}"
        kind = f"Appointment on <b>{html.escape(date)}</b> at <b>{html.escape(str(slot))}</b>"
    else:
        subject = f"New in queue {token}: {student}"
        kind = "Walk-in (joined the live queue)"
    if not email_configured() and settings.DEBUG:
        print(f"\n[EMAIL][DEV] {subject} -> {recipient}\n")
        return True
    rows = [
        ("Token", html.escape(token)),
        ("Student", html.escape(student)),
        ("Roll number", html.escape(roll or "—")),
        ("Service", html.escape(service)),
        ("Type", kind),
    ]
    if position is not None:
        rows.append(("Position", "Next in line" if position == 0 else f"{position} ahead of them"))
    if est_wait_min is not None:
        rows.append(("Est. wait", "Can be called now" if est_wait_min == 0 else f"~{est_wait_min} min"))
    rows.append(("Waiting now", str(waiting_total)))
    table = "".join(
        f'<tr><td style="padding:6px 12px 6px 0;color:#666">{k}</td><td style="padding:6px 0"><b>{v}</b></td></tr>'
        for k, v in rows
    )
    body = f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:24px">
      <h2 style="margin:0 0 12px">Smart Exam Cell — Staff</h2>
      <p>A student has just joined the queue.</p>
      <table style="border-collapse:collapse;margin:16px 0">{table}</table>
      <p style="color:#666">Open the staff dashboard to call them when a counter is free.</p>
    </div>
    """
    return _send(recipient, f"{subject} — Smart Exam Cell", body)


def send_appointment_cancelled_email(recipient: str, name: str, token: str, date: str, slot: str,
                                     service: str, reason: str) -> bool:
    subject = f"Your appointment {token} on {date} at {slot} was cancelled"
    if not email_configured() and settings.DEBUG:
        print(f"\n[EMAIL][DEV] {subject} ({reason}) -> {recipient}\n")
        return True
    body = f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:24px">
      <h2 style="margin:0 0 12px">Smart Exam Cell</h2>
      <p>Hi {html.escape(name or "there")},</p>
      <p>The exam cell has cancelled your appointment <b>{html.escape(token)}</b>
         ({html.escape(service)}) on <b>{html.escape(date)}</b> at <b>{html.escape(slot)}</b>.</p>
      <p style="background:#f4f4f5;border-radius:8px;padding:12px"><b>Reason:</b> {html.escape(reason)}</p>
      <p>You can book a new slot or join the live queue from the student portal.</p>
    </div>
    """
    return _send(recipient, f"{subject} — Smart Exam Cell", body)


def send_called_email(recipient: str, name: str, token: str, counter_id: int, service: str, transferred: bool) -> bool:
    """'It's your turn' — sent to the one student who was just called to a counter."""
    if transferred:
        subject = f"{token}: please go to Counter {counter_id} instead"
        line = f"Your token has been moved. Please proceed to <b>Counter {counter_id}</b> now."
    else:
        subject = f"{token}: it's your turn — go to Counter {counter_id}"
        line = f"It's your turn. Please proceed to <b>Counter {counter_id}</b> now."
    if not email_configured() and settings.DEBUG:
        print(f"\n[EMAIL][DEV] {subject} -> {recipient}\n")
        return True
    safe_name = html.escape(name or "there")
    body = f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:24px">
      <h2 style="margin:0 0 12px">Smart Exam Cell</h2>
      <p>Hi {safe_name},</p>
      <p>{line}</p>
      <div style="border:2px solid #4f46e5;border-radius:12px;padding:16px;margin:20px 0;text-align:center">
        <div style="font-size:36px;font-weight:bold;color:#4f46e5">{html.escape(token)}</div>
        <div style="font-size:20px;margin-top:4px">Counter {counter_id}</div>
        <div style="color:#666;margin-top:4px">{html.escape(service)}</div>
      </div>
      <p style="color:#666">If you don't reach the counter soon, staff may mark your token as missed.</p>
    </div>
    """
    return _send(recipient, f"{subject} — Smart Exam Cell", body)
