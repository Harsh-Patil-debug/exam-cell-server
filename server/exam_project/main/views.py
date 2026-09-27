"""
Smart Exam Cell Queue System — API Views
─────────────────────────────────────────────────────────────────────────────
All views are thin wrappers. Business logic lives exclusively in Handlers/.
Each View calls a handler function and returns the result as a DRF Response.
─────────────────────────────────────────────────────────────────────────────
"""

from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from .Handlers import status_check, db_check, services, queue_state, tickets, counters, appointments, auth_handler, auth_middleware, input_validation
from .Handlers.errors import QueueError


def _body(request):
    """request.data, or {} if the client sent a non-object JSON body."""
    return request.data if isinstance(request.data, dict) else {}


# ── Status ─────────────────────────────────────────────────────────────────────

class StatusCheckView(APIView):
    """GET /status/ — Server health check (public)"""
    def get(self, request):
        response = status_check.status_check()
        return Response(response)


# ── Database ───────────────────────────────────────────────────────────────────

class DbCheckView(APIView):
    """GET /db/ — MongoDB read/write connectivity check (Staff only)"""
    def get(self, request):
        # SECURITY: this performs a live write/delete against the DB and can leak internal
        # infra details (hostnames, driver errors) via the raw exception message.
        _, error_response = auth_middleware.authenticate_staff_request(request)
        if error_response:
            return error_response
        response = db_check.db_check()
        return Response(response)


# ── Auth ───────────────────────────────────────────────────────────────────────

class AuthThrottled(APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'


class RegisterView(AuthThrottled):
    """POST /auth/register/ — {role, name, email, password} → emails a signup OTP (no session yet)"""
    def post(self, request):
        body, code = auth_handler.register_handler(_body(request))
        return Response(body, status=code)


class LoginView(AuthThrottled):
    """POST /auth/login/ — {role, email, password} → emails a login OTP (no session yet)"""
    def post(self, request):
        body, code = auth_handler.login_handler(_body(request))
        return Response(body, status=code)


class VerifyOTPView(AuthThrottled):
    """POST /auth/verify-otp/ — {role, email, otp} → {accessToken, refreshToken, user}"""
    def post(self, request):
        body, code = auth_handler.verify_otp_handler(_body(request))
        return Response(body, status=code)


class ResendOTPView(AuthThrottled):
    """POST /auth/resend-otp/ — {role, email}"""
    def post(self, request):
        body, code = auth_handler.resend_otp_handler(_body(request))
        return Response(body, status=code)


class ForgotPasswordView(AuthThrottled):
    """POST /auth/forgot-password/ — {role, email} → emails a reset OTP if the account exists"""
    def post(self, request):
        body, code = auth_handler.forgot_password_handler(_body(request))
        return Response(body, status=code)


class ResetPasswordView(AuthThrottled):
    """POST /auth/reset-password/ — {role, email, otp, password}"""
    def post(self, request):
        body, code = auth_handler.reset_password_handler(_body(request))
        return Response(body, status=code)


class RefreshView(AuthThrottled):
    """POST /auth/refresh/ — {refreshToken} → rotated {accessToken, refreshToken, user}"""
    def post(self, request):
        body, code = auth_handler.refresh_handler(_body(request).get("refreshToken"))
        return Response(body, status=code)


class LogoutView(APIView):
    """POST /auth/logout/ — {refreshToken}; revokes the bearer access token and the session"""
    def post(self, request):
        body, code = auth_handler.logout_handler(
            auth_middleware.bearer_token(request), _body(request).get("refreshToken"),
        )
        return Response(body, status=code)


class MeView(APIView):
    """GET /auth/me/ — the logged-in account"""
    def get(self, request):
        principal, error_response = auth_middleware.authenticate_request(request)
        if error_response:
            return error_response
        body, code = auth_handler.me_handler(principal)
        return Response(body, status=code)


class GoogleLoginView(APIView):
    """GET /auth/google/login/?role=student|staff&return_url=... — redirects to Google"""
    def get(self, request):
        try:
            url = auth_handler.google_login_url(
                request.query_params.get("role"), request.query_params.get("return_url"),
            )
        except QueueError as e:
            body, code = e.response()
            return Response(body, status=code)
        response = HttpResponse(status=302)
        response["Location"] = url
        return response


class GoogleCallbackView(APIView):
    """GET /auth/google/callback/ — Google redirects here; we redirect on to the frontend
    with a single-use login code (or an error)"""
    def get(self, request):
        response = HttpResponse(status=302)
        response["Location"] = auth_handler.google_callback(
            request.query_params.get("code"), request.query_params.get("state"),
        )
        return response


class GoogleExchangeView(AuthThrottled):
    """POST /auth/google/exchange/ — {code} → {accessToken, refreshToken, user}"""
    def post(self, request):
        body, code = auth_handler.google_exchange_handler(_body(request))
        return Response(body, status=code)


# ── Services ───────────────────────────────────────────────────────────────────

class ServiceListView(APIView):
    """GET /services/ — Exam cell services and appointment slots (public)"""
    def get(self, request):
        body, code = services.get_services_handler()
        return Response(body, status=code)


# ── Queue ──────────────────────────────────────────────────────────────────────

class QueueStateView(APIView):
    """GET /queue/ — Full queue snapshot polled by every screen (any logged-in account;
    other students' names and roll numbers are masked unless the caller is staff)"""
    def get(self, request):
        principal, error_response = auth_middleware.authenticate_request(request)
        if error_response:
            return error_response
        body, code = queue_state.get_queue_state_handler(full=principal["role"] == "staff")
        return Response(body, status=code)


class QueueResetView(APIView):
    """POST /queue/reset/ — Close today's tickets and free all counters (Staff only)"""
    def post(self, request):
        _, error_response = auth_middleware.authenticate_staff_request(request)
        if error_response:
            return error_response
        body, code = queue_state.reset_queue_handler()
        return Response(body, status=code)


# ── Tickets ────────────────────────────────────────────────────────────────────

class TicketCreateView(APIView):
    """POST /tickets/ — Take a walk-in token or book an appointment (Students only)"""
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'token'

    def post(self, request):
        principal, error_response = auth_middleware.authenticate_student_request(request)
        if error_response:
            return error_response
        body, code = tickets.take_token_handler(_body(request), principal)
        return Response(body, status=code)


class MyTicketView(APIView):
    """GET /tickets/mine/ — The student's current ticket with live position and forecast"""
    def get(self, request):
        principal, error_response = auth_middleware.authenticate_student_request(request)
        if error_response:
            return error_response
        body, code = tickets.get_my_ticket_handler(principal)
        return Response(body, status=code)


class SlotAvailabilityView(APIView):
    """GET /slots/?date=YYYY-MM-DD — Appointment slots with places left for a date"""
    def get(self, request):
        _, error_response = auth_middleware.authenticate_request(request)
        if error_response:
            return error_response
        body, code = tickets.get_slot_availability_handler(request.query_params.get("date"))
        return Response(body, status=code)


class TicketDetailView(APIView):
    """GET /tickets/<id>/ — One ticket (staff: any; students: their own)"""
    def get(self, request, ticket_id):
        principal, error_response = auth_middleware.authenticate_request(request)
        if error_response:
            return error_response
        body, code = tickets.get_ticket_handler(ticket_id, principal)
        return Response(body, status=code)


class TicketCancelView(APIView):
    """POST /tickets/<id>/cancel/ — Cancel your own waiting token (Students only)"""
    def post(self, request, ticket_id):
        principal, error_response = auth_middleware.authenticate_student_request(request)
        if error_response:
            return error_response
        body, code = tickets.cancel_ticket_handler(ticket_id, principal)
        return Response(body, status=code)


# ── Appointments (Staff) ───────────────────────────────────────────────────────

class AppointmentListView(APIView):
    """GET /appointments/?date=YYYY-MM-DD — All bookings on a date + slot occupancy (Staff only)"""
    def get(self, request):
        _, error_response = auth_middleware.authenticate_staff_request(request)
        if error_response:
            return error_response
        body, code = appointments.list_appointments_handler(request.query_params.get("date"))
        return Response(body, status=code)


class AppointmentCancelView(APIView):
    """POST /appointments/<id>/cancel/ — {reason}; frees the slot and emails the student (Staff only)"""
    def post(self, request, ticket_id):
        principal, error_response = auth_middleware.authenticate_staff_request(request)
        if error_response:
            return error_response
        body, code = appointments.staff_cancel_appointment_handler(ticket_id, _body(request), principal)
        return Response(body, status=code)


# ── Counters (Staff) ───────────────────────────────────────────────────────────

class CounterCallNextView(APIView):
    """POST /counters/<id>/call-next/ — Complete current student and call the next one (Staff only)"""
    def post(self, request, counter_id):
        _, error_response = auth_middleware.authenticate_staff_request(request)
        if error_response:
            return error_response
        body, code = counters.call_next_handler(counter_id)
        return Response(body, status=code)


class CounterCompleteView(APIView):
    """POST /counters/<id>/complete/ — Mark the current student as served (Staff only)"""
    def post(self, request, counter_id):
        _, error_response = auth_middleware.authenticate_staff_request(request)
        if error_response:
            return error_response
        body, code = counters.close_current_handler(counter_id, "completed")
        return Response(body, status=code)


class CounterSkipView(APIView):
    """POST /counters/<id>/skip/ — Mark the current student as no-show (Staff only)"""
    def post(self, request, counter_id):
        _, error_response = auth_middleware.authenticate_staff_request(request)
        if error_response:
            return error_response
        body, code = counters.close_current_handler(counter_id, "skipped")
        return Response(body, status=code)


class CounterTransferView(APIView):
    """POST /counters/<id>/transfer/ — Move the current student to another counter (Staff only)"""
    def post(self, request, counter_id):
        _, error_response = auth_middleware.authenticate_staff_request(request)
        if error_response:
            return error_response
        to_counter, error = input_validation.parse_positive_int(_body(request).get("to_counter"), "Target counter")
        if error:
            return Response({"status": "error", "message": error}, status=400)
        body, code = counters.transfer_handler(counter_id, to_counter)
        return Response(body, status=code)
